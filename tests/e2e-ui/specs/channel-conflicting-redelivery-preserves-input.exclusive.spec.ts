/** Same external message identity cannot replace input or stop later source work. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText, visibleMessages } from '../fixtures/astraApi';
import { documentsByField, sessionEvents, waitForTurnTerminalProof } from '../fixtures/dbOracle';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';
import { satoriSource } from '../fixtures/satoriSource';

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
  return documentsByField('channel_inbound', '$.deployment_id', deploymentId);
}

function messageRows() {
  return inboundRows().filter((row) => String(row.dedup_key)
    .startsWith(`e2e:${source!.botId}:message:`));
}

function messageRow(messageId: string) {
  const matches = messageRows().filter((row) => row.dedup_key
    === `e2e:${source!.botId}:message:${messageId}`);
  expect(matches, `one durable source message ${messageId}`).toHaveLength(1);
  return matches[0];
}

function commands() {
  return sessionEvents(sessionId).filter((row) => row.event_type === 'command.accepted'
    && object(row.payload).command_type === 'StartTurn');
}

function nativeInputs(content: string) {
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId)
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

async function waitAccepted(messageId: string) {
  await expect.poll(() => {
    source!.healthy();
    const row = messageRows().find((item) => item.dedup_key
      === `e2e:${source!.botId}:message:${messageId}`);
    if (row?.session_id && !sessionId) {
      sessionId = String(row.session_id);
      sessions.push(sessionId);
    }
    if (!sessionId || !row) return 0;
    return commands().filter((command) => object(command.payload).client_message_id
      === `${row._id}::a0`).length;
  }, { timeout: 60_000, message: 'the later legitimate source message must enter the native turn path' }).toBe(1);
  const row = messageRow(messageId);
  expect(row.session_id).toBe(sessionId);
  return commands().find((command) => object(command.payload).client_message_id === `${row._id}::a0`)!;
}

async function waitSettled(messageId: string, command: Record<string, unknown>, deliveryCount: number) {
  await waitForTurnTerminalProof(sessionId, String(command.turn_id), 'COMPLETED', 60_000);
  await expect.poll(() => {
    source!.healthy();
    return { state: messageRow(messageId).state, deliveries: source!.deliveries.length };
  }, { timeout: 30_000 }).toEqual({ state: 'SETTLED', deliveries: deliveryCount });
  const commandId = String(command.causation_id);
  const receipts = sessionEvents(sessionId).filter((row) => row.causation_id === commandId
    && ['input.delivered', 'input.consumed'].includes(String(row.event_type)));
  expect(receipts.map((row) => row.event_type).sort()).toEqual(['input.consumed', 'input.delivered']);
  expect(receipts.every((row) => object(row.payload).input_id === object(command.payload).input_id)).toBe(true);
  expect(object(receipts.find((row) => row.event_type === 'input.consumed')!.payload).content)
    .toBe(object(command.payload).content);
  const outbox = documentsByField('channel_outbox', '$.session_id', sessionId)
    .filter((row) => row.command_id === commandId);
  expect(outbox).toHaveLength(1);
  expect(outbox[0].state).toBe('DELIVERED');
  evidence[`receipts-${deliveryCount}`] = receipts;
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
        journal: failed && sessionId ? sessionEvents(sessionId) : [],
        native: failed && sessionId ? documentsByField('transcript_entries', '$.platform_session_id', sessionId) : [],
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
      === `e2e:${source!.botId}:message:${contextId}`)?.state;
  }).toBe('IGNORED');
  const originalContext = messageRow(contextId);
  expect(object(originalContext.payload).content).toBe(contextText);
  expect(originalContext.session_id).toBeNull();
  expect(originalContext.command_id).toBeNull();
  expect(source!.deliveries).toHaveLength(0);
  evidence.originalContext = originalContext;

  // Same external identity/time/actor, changed body, fresh transport cursor.
  // A legitimate wake after the duplicate is the source-progress barrier.
  sendMessage(contextId, changedContext, base);
  sendMessage(firstId, `${mention}${firstText}`, base + 1000);
  const first = await waitAccepted(firstId);
  const firstContent = String(object(first.payload).content);
  expect(firstContent).toContain(contextText);
  expect(firstContent).toContain(firstText);
  expect(firstContent).not.toContain(changedContext);
  expect(messageRow(contextId).payload).toEqual(originalContext.payload);
  expect(commands()).toEqual([first]);
  await waitSettled(firstId, first, 1);
  await expect.poll(() => nativeInputs(firstContent).length).toBe(1);
  const originalNative = nativeInputs(firstContent);
  const originalFirst = messageRow(firstId);
  expect(originalFirst.state).toBe('SETTLED');
  evidence.originalFirst = originalFirst;
  evidence.originalNative = originalNative;
  evidence.originalCommand = first;

  sendMessage(firstId, `${mention}${changedInput}`, base + 1000);
  sendMessage(laterId, `${mention}${laterText}`, base + 2000);
  const later = await waitAccepted(laterId);
  expect(object(later.payload).content).toBe(laterText);
  await waitSettled(laterId, later, 2);
  await source!.waitSent(5);
  await expect.poll(() => nativeInputs(laterText).length).toBe(1);

  expect(messageRows().map((row) => row.dedup_key).sort()).toEqual(
    [contextId, firstId, laterId].map((messageId) => `e2e:${source!.botId}:message:${messageId}`).sort(),
  );
  expect(messageRow(contextId).payload).toEqual(originalContext.payload);
  expect(messageRow(contextId).state).toBe('IGNORED');
  expect(messageRow(contextId).session_id).toBeNull();
  expect(messageRow(contextId).command_id).toBeNull();
  expect(messageRow(contextId).context_submitted).toBe(true);
  expect(messageRow(firstId).payload).toEqual(originalFirst.payload);
  expect(messageRow(firstId).frozen_input).toEqual(originalFirst.frozen_input);
  expect(messageRow(firstId).command_id).toBe(originalFirst.command_id);
  expect(messageRow(firstId).turn_id).toBe(originalFirst.turn_id);
  expect(commands()).toEqual([first, later]);
  expect(first.causation_id).not.toBe(later.causation_id);
  expect(nativeInputs(firstContent)).toEqual(originalNative);
  expect(source!.deliveries).toHaveLength(2);
  expect(source!.deliveries.every((delivery) => delivery.channel_id === 'room'
    && delivery.content.trim().length > 0)).toBe(true);
  expect(documentsByField('channel_outbox', '$.session_id', sessionId)).toHaveLength(2);
  expect(documentsByField('sessions', '$.agent_id', agentId).map((row) => row.session_id)).toEqual([sessionId]);

  // The official adapter also emits login events. They are not user messages.
  const lifecycle = inboundRows().filter((row) => !String(row.dedup_key)
    .startsWith(`e2e:${source!.botId}:message:`));
  for (const row of lifecycle) {
    expect(row.state).toBe('IGNORED');
    expect(row.session_id).toBeNull();
    expect(row.command_id).toBeNull();
    expect(object(row.payload).provider_ignore_reason)
      .toMatch(/^channel event 'login-(?:added|updated)' is not message-created$/);
  }

  const history = visibleMessages(await api.getMessages(sessionId, 100));
  expect(history.filter((row) => row.role === 'user').map(messageText)).toEqual([firstContent, laterText]);
  const replies = history.filter((row) => row.role === 'assistant');
  expect(replies).toHaveLength(2);
  expect(replies.every((row) => messageText(row).trim().length > 0)).toBe(true);
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
  expect(commands()).toEqual([first, later]);
  expect(nativeInputs(firstContent)).toEqual(originalNative);
  evidence.final = { inbound: inboundRows(), commands: commands(), history,
    native: documentsByField('transcript_entries', '$.platform_session_id', sessionId), lifecycle };
});
