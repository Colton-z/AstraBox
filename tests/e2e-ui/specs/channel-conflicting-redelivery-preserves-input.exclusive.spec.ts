/** Same external message identity cannot replace input or stop later source work. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText, visibleMessages } from '../fixtures/astraApi';
import {
  nativeTranscriptRows,
  sessionInputRows,
  sessionOutputRows,
  sessionResponseRows,
  type SessionInputRow,
  type SessionResponseRow,
} from '../fixtures/dbOracle';
import {
  agentSessionRows,
  channelInboundRows,
  channelMessageInputs,
  channelOutboxRows,
  type ChannelInboundRow,
} from '../fixtures/ingressRecords';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';
import { satoriReplyTexts, satoriSource } from '../fixtures/satoriSource';

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
let sessionId = '';
let source: Awaited<ReturnType<typeof satoriSource>> | null = null;
const evidence: Record<string, unknown> = {};

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('expected an evidence object');
  }
  return value as Record<string, unknown>;
}

function inboundRows() {
  return channelInboundRows(deploymentId);
}

function messageRows() {
  return inboundRows().filter((row) => String(row.dedup_key)
    .startsWith(`e2e:${source!.botId}:channel:room:message:`));
}

function messageRow(messageId: string) {
  const matches = messageRows().filter((row) => row.dedup_key
    === `e2e:${source!.botId}:channel:room:message:${messageId}`);
  expect(matches, `one durable source message ${messageId}`).toHaveLength(1);
  return matches[0];
}

/** What a recorded message says, as its first signal recorded it. */
function messageFacts(row: ChannelInboundRow) {
  return {
    content: row.content, reply_text: row.reply_text, participant: row.participant,
    conversation: row.conversation, retain_context: row.retain_context,
    provider_ignore_reason: row.provider_ignore_reason,
  };
}

/** The inputs the Session accepted, by their identity and content: one per message that ran. */
function inputs() {
  return sessionInputRows(sessionId).map((row) => ({
    input_id: row.input_id, client_message_id: row.client_message_id, source: row.source, content: row.content,
  }));
}

function nativeInputs(content: string) {
  return nativeTranscriptRows(sessionId)
    .filter((row) => row.subpath == null)
    .map((row) => ({ uuid: row.uuid, seq: row.seq, entry: object(JSON.parse(String(row.entry_json))) }))
    .filter((row) => {
      if (row.entry.type !== 'user') return false;
      const raw = object(row.entry.message).content;
      const text = typeof raw === 'string' ? raw : (raw as Array<Record<string, unknown>>)
        .filter((block) => block.type === 'text').map((block) => String(block.text || '')).join('');
      return text === content;
    });
}

function sendMessage(messageId: string, content: string, timestamp: number) {
  source!.send({ type: 'message-created', timestamp,
    channel: { id: 'room', type: 0 }, user: { id: 'participant', name: 'Participant' },
    message: { id: messageId, content, created_at: timestamp } });
}

/** The one input a channel message became: its item id is the input's client message id. */
async function waitAccepted(messageId: string): Promise<SessionInputRow> {
  await expect.poll(() => {
    source!.healthy();
    const row = messageRows().find((item) => item.dedup_key
      === `e2e:${source!.botId}:channel:room:message:${messageId}`);
    if (row?.session_id && !sessionId) {
      sessionId = String(row.session_id);
      sessions.push(sessionId);
    }
    if (!sessionId || !row) return 0;
    return sessionInputRows(sessionId).filter((input) => input.client_message_id === row.item_id).length;
  }, { timeout: 60_000, message: 'the later legitimate source message must become one input to the Session' }).toBe(1);
  const row = messageRow(messageId);
  expect(row.session_id).toBe(sessionId);
  const input = sessionInputRows(sessionId).find((item) => item.client_message_id === row.item_id)!;
  expect(input.source).toBe('channel');
  return input;
}

/**
 * The message's response ran to completion, the runtime took exactly the
 * accepted input, and its one delivery reached the channel.
 */
async function waitSettled(
  messageId: string, input: SessionInputRow, deliveryCount: number,
): Promise<SessionResponseRow> {
  let response: SessionResponseRow | undefined;
  await expect.poll(() => {
    response = sessionResponseRows(sessionId).find((row) => row.input_ids.includes(input.input_id));
    return response ? `${response.status}/${response.outcome ?? ''}` : 'no response yet';
  }, { timeout: 60_000, message: 'the response answering the message must complete' }).toBe('ended/completed');
  await expect.poll(() => {
    source!.healthy();
    return { state: messageRow(messageId).state, deliveries: source!.deliveries.length };
  }, { timeout: 30_000 }).toEqual({ state: 'SETTLED', deliveries: deliveryCount });
  const taken = sessionInputRows(sessionId).find((row) => row.input_id === input.input_id)!;
  expect(taken).toMatchObject({ state: 'admitted', response_id: response!.response_id, content: input.content });
  // The runtime's own record of what it dequeued for this input.
  const consumed = sessionOutputRows(sessionId, response!.response_id)
    .map((row) => row.part)
    .filter((part) => part.type === 'data-input-consumed' && object(part.data).inputId === input.input_id);
  expect(consumed).toHaveLength(1);
  expect(object(consumed[0].data).content).toBe(input.content);
  const outbox = channelOutboxRows(sessionId).filter((row) => row.response_id === response!.response_id);
  expect(outbox, 'one delivery belongs to the response it reports').toHaveLength(1);
  await expect.poll(() => channelOutboxRows(sessionId)
    .find((row) => row.response_id === response!.response_id)?.state).toBe('DELIVERED');
  evidence[`settled-${deliveryCount}`] = { input: taken, response, consumed };
  return response!;
}

test.beforeEach(async ({ request }) => {
  agentId = ''; deploymentId = ''; sessionId = '';
  for (const key of Object.keys(evidence)) delete evidence[key];
  source = await satoriSource();
  agentId = (await new AstraApi(request).createColdTestAgent(`conflict-${randomUUID()}`)).agent_id;
  deploymentId = (await new PlatformApi(request).createDeployment(agentId, {
    scene: 'channel:satori', prompt_prefix: '', attention_policy: 'mentions',
    channel_config: { endpoint: source.endpoint }, credentials: { token: source.token },
  })).deployment_id;
  await source.waitConnected();
});

test.afterEach(async ({ request }, info) => {
  try {
    const failed = ['failed', 'timedOut', 'interrupted'].includes(String(info.status));
    await info.attach('channel-conflicting-redelivery-evidence', {
      body: JSON.stringify({ agentId, deploymentId, sessionId, ...evidence,
        retainedInbound: failed && deploymentId ? inboundRows() : [],
        inputs: failed && sessionId ? sessionInputRows(sessionId) : [],
        responses: failed && sessionId ? sessionResponseRows(sessionId) : [],
        native: failed && sessionId ? nativeTranscriptRows(sessionId) : [],
        retainedHistory: failed && sessionId ? await new AstraApi(request).getMessages(sessionId, 100) : null,
        deliveries: source?.deliveries, sourceErrors: source?.errors }),
      contentType: 'application/json',
    });
  } finally { if (source) await source.close(); source = null; }
});

onPassOnly(async ({ request }) => {
  if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('conflicting redelivery preserves ignored context and settled input while later channel messages complete', async ({ request, page }) => {
  const api = new AstraApi(request);
  const id = randomUUID();
  const contextId = `context-${id}`;
  const firstId = `first-${id}`;
  const laterId = `later-${id}`;
  const base = Date.now();
  const contextText = `A bookmark keeps a reader's place. Original reference ${id}.`;
  const changedContext = `REPLACED_CONTEXT_${id}`;
  const firstText = `Conversation ${id}. Explain what a bookmark is in one short sentence. Do not use tools.`;
  const changedInput = `REPLACED_ACCEPTED_INPUT_${id}`;
  const laterText = `Conversation ${id}. Explain what a notebook is in one short sentence. Do not use tools.`;
  const mention = `<at id="${source!.botId}"/>`;

  sendMessage(contextId, contextText, base);
  await expect.poll(() => {
    source!.healthy();
    return messageRows().find((row) => row.dedup_key
      === `e2e:${source!.botId}:channel:room:message:${contextId}`)?.state;
  }).toBe('OBSERVED');
  const originalContext = messageRow(contextId);
  expect(originalContext.content).toBe(contextText);
  expect(originalContext.session_id).toBeNull();
  expect(channelMessageInputs(originalContext.item_id), 'retained context is not an input').toEqual([]);
  expect(source!.deliveries).toHaveLength(0);
  evidence.originalContext = originalContext;

  // Same external identity/time/actor, changed body, fresh transport cursor.
  // A legitimate wake after the duplicate is the source-progress barrier.
  sendMessage(contextId, changedContext, base);
  sendMessage(firstId, `${mention}${firstText}`, base + 1000);
  const first = await waitAccepted(firstId);
  const firstContent = first.content;
  expect(firstContent).toContain(contextText);
  expect(firstContent).toContain(firstText);
  expect(firstContent).not.toContain(changedContext);
  expect(messageFacts(messageRow(contextId))).toEqual(messageFacts(originalContext));
  expect(inputs()).toEqual([
    { input_id: first.input_id, client_message_id: first.client_message_id, source: 'channel', content: firstContent },
  ]);
  await waitSettled(firstId, first, 1);
  await expect.poll(() => nativeInputs(firstContent).length).toBe(1);
  const originalNative = nativeInputs(firstContent);
  const originalFirst = messageRow(firstId);
  expect(originalFirst.state).toBe('SETTLED');
  expect(originalFirst.frozen_content, 'the input as sent is frozen with the message').toBe(firstContent);
  expect(originalFirst.frozen_context).toEqual([`e2e:${source!.botId}:channel:room:message:${contextId}`]);
  evidence.originalFirst = originalFirst;
  evidence.originalNative = originalNative;
  evidence.originalInput = first;

  sendMessage(firstId, `${mention}${changedInput}`, base + 1000);
  sendMessage(laterId, `${mention}${laterText}`, base + 2000);
  const later = await waitAccepted(laterId);
  expect(later.content).toBe(laterText);
  await waitSettled(laterId, later, 2);
  await source!.waitSent(5);
  await expect.poll(() => nativeInputs(laterText).length).toBe(1);

  expect(messageRows().map((row) => row.dedup_key).sort()).toEqual(
    [contextId, firstId, laterId].map((messageId) => `e2e:${source!.botId}:channel:room:message:${messageId}`).sort(),
  );
  expect(messageFacts(messageRow(contextId))).toEqual(messageFacts(originalContext));
  expect(messageRow(contextId).state).toBe('OBSERVED');
  expect(messageRow(contextId).session_id).toBeNull();
  expect(channelMessageInputs(originalContext.item_id)).toEqual([]);
  expect(messageRow(contextId).context_submitted).toBe(true);
  expect(messageFacts(messageRow(firstId))).toEqual(messageFacts(originalFirst));
  expect(messageRow(firstId).frozen_content).toEqual(originalFirst.frozen_content);
  expect(messageRow(firstId).frozen_context).toEqual(originalFirst.frozen_context);
  expect(channelMessageInputs(originalFirst.item_id).map((row) => row.input_id),
    'the redelivered message keeps its one input').toEqual([first.input_id]);
  expect(inputs()).toEqual([
    { input_id: first.input_id, client_message_id: first.client_message_id, source: 'channel', content: firstContent },
    { input_id: later.input_id, client_message_id: later.client_message_id, source: 'channel', content: laterText },
  ]);
  expect(nativeInputs(firstContent)).toEqual(originalNative);
  expect(source!.deliveries).toHaveLength(2);
  expect(source!.deliveries.every((delivery) => delivery.channel_id === 'room'
    && delivery.content.trim().length > 0)).toBe(true);
  expect(channelOutboxRows(sessionId)).toHaveLength(2);
  expect(agentSessionRows(agentId).map((row) => row.session_id)).toEqual([sessionId]);

  // The official adapter also emits login events. They are not user messages.
  const lifecycle = inboundRows().filter((row) => !String(row.dedup_key)
    .startsWith(`e2e:${source!.botId}:channel:room:message:`));
  for (const row of lifecycle) {
    expect(row.state).toBe('IGNORED');
    expect(row.session_id).toBeNull();
    expect(channelMessageInputs(row.item_id)).toEqual([]);
    expect(row.provider_ignore_reason)
      .toMatch(/^channel event 'login-(?:added|updated)' is not message-created$/);
  }

  const history = visibleMessages(await api.getMessages(sessionId, 100));
  expect(history.filter((row) => row.role === 'user').map(messageText)).toEqual([firstContent, laterText]);
  const replies = history.filter((row) => row.role === 'assistant');
  expect(replies).toHaveLength(2);
  expect(replies.every((row) => messageText(row).trim().length > 0)).toBe(true);
  expect(await satoriReplyTexts(page, source!.deliveries, firstId))
    .toEqual(replies.map((reply) => messageText(reply).trim()));
  expect(channelOutboxRows(sessionId).map((row) => row.response_id).sort())
    .toEqual(replies.map((reply) => reply.id).sort());
  await openSessionView(page, sessionId);
  const renderedInputs = page.getByTestId('user-message');
  await expect(renderedInputs).toHaveCount(2);
  await expect(renderedInputs.first()).toBeVisible();
  // The retained group context is a Markdown list, not a visible literal '-'.
  // Validate its complete paragraphs/list item before capturing the UI baseline.
  await expect(renderedInputs.first().locator('p')).toHaveText([
    '以下是 Bot 未参与期间、当前会话中的群聊记录（按时间顺序）：',
    `当前唤起 Bot 的消息：\n${firstText}`,
  ]);
  await expect(renderedInputs.first().getByRole('listitem')).toHaveText([
    `[${new Date(base).toISOString()}] e2e:participant：${contextText}`,
  ]);
  await expect(renderedInputs.last()).toHaveText(laterText);
  await expect(renderedInputs.last()).toBeVisible();
  const originalRenderedInputs = await renderedInputs.allInnerTexts();
  evidence.originalRenderedInputs = originalRenderedInputs;
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(renderedInputs).toHaveText(originalRenderedInputs, { useInnerText: true });
  await expect(page.getByText(changedContext, { exact: false })).toHaveCount(0);
  await expect(page.getByText(changedInput, { exact: false })).toHaveCount(0);
  expect(inputs().map((row) => row.input_id)).toEqual([first.input_id, later.input_id]);
  expect(nativeInputs(firstContent)).toEqual(originalNative);
  evidence.final = { inbound: inboundRows(), inputs: sessionInputRows(sessionId),
    responses: sessionResponseRows(sessionId), history,
    native: nativeTranscriptRows(sessionId), lifecycle };
});
