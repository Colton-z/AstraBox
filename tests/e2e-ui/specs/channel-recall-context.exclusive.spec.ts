/** Donor recall boundaries through the official Satori adapter, real engine and DB. */
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
import { channelInboundRows, channelMessageInputs, channelOutboxRows } from '../fixtures/ingressRecords';
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
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('expected object');
  return value as Record<string, unknown>;
}

function inboundRows() { return channelInboundRows(deploymentId); }
function messagesFor(messageId: string) {
  return inboundRows().filter((row) => row.dedup_key === `e2e:${source!.botId}:channel:room:message:${messageId}`);
}
function expectOnlyUserMessages(messageIds: string[]) {
  const prefix = `e2e:${source!.botId}:channel:room:message:`;
  const rows = inboundRows();
  expect(rows.filter((row) => String(row.dedup_key).startsWith(prefix))
    .map((row) => row.dedup_key).sort()).toEqual(messageIds.map((id) => `${prefix}${id}`).sort());
  // The official adapter emits login lifecycle events before user messages.
  // They remain durable source receipts, never Agent inputs or retained context.
  for (const row of rows.filter((item) => !String(item.dedup_key).startsWith(prefix))) {
    expect(String(row.dedup_key)).toMatch(/:event:login-(added|updated):/);
    expect(row).toMatchObject({ state: 'IGNORED', session_id: null, retain_context: false });
    expect(channelMessageInputs(row.item_id)).toEqual([]);
    expect(String(row.provider_ignore_reason)).toMatch(/login-(added|updated)/);
  }
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

function sendMessage(messageId: string, content: string, timestamp: number, room = 'room') {
  source!.send({ type: 'message-created', timestamp,
    channel: { id: room, type: 0 }, user: { id: 'participant', name: 'Participant' },
    message: { id: messageId, content, created_at: timestamp } });
}
function recallMessage(messageId: string, timestamp: number, sn: number) {
  source!.send({ type: 'message-deleted', timestamp, sn,
    channel: { id: 'room', type: 0 }, user: { id: 'participant', name: 'Participant' },
    message: { id: messageId } });
}

/** The one input a channel message became: its item id is the input's client message id. */
async function waitAccepted(messageId: string): Promise<SessionInputRow> {
  await expect.poll(() => {
    source!.healthy();
    const row = messagesFor(messageId)[0];
    if (row?.session_id && !sessionId) {
      sessionId = String(row.session_id);
      sessions.push(sessionId);
    }
    if (!sessionId || !row) return 0;
    return sessionInputRows(sessionId).filter((input) => input.client_message_id === row.item_id).length;
  }, { timeout: 60_000, message: 'the original channel message must become one accepted input' }).toBe(1);
  const row = messagesFor(messageId)[0];
  const input = sessionInputRows(sessionId).find((item) => item.client_message_id === row.item_id)!;
  expect(input.source).toBe('channel');
  return input;
}

/**
 * The message's response ran to completion, the runtime took exactly the
 * accepted input, and its one delivery reached the channel.
 */
async function waitSettled(input: SessionInputRow, expectedDeliveries: number): Promise<SessionResponseRow> {
  let response: SessionResponseRow | undefined;
  await expect.poll(() => {
    response = sessionResponseRows(sessionId).find((row) => row.input_ids.includes(input.input_id));
    return response ? `${response.status}/${response.outcome ?? ''}` : 'no response yet';
  }, { timeout: 60_000, message: 'the response answering the message must complete' }).toBe('ended/completed');
  await expect.poll(() => {
    source!.healthy();
    return source!.deliveries.length;
  }, { timeout: 30_000 }).toBe(expectedDeliveries);
  expect(source!.deliveries.every((delivery) => delivery.channel_id === 'room'
    && delivery.content.trim().length > 0)).toBe(true);
  const taken = sessionInputRows(sessionId).find((row) => row.input_id === input.input_id)!;
  expect(taken).toMatchObject({ state: 'admitted', response_id: response!.response_id, content: input.content });
  // The runtime's own record of what it dequeued for this input.
  const consumed = sessionOutputRows(sessionId, response!.response_id)
    .map((row) => row.part)
    .filter((part) => part.type === 'data-input-consumed' && object(part.data).inputId === input.input_id);
  expect(consumed).toHaveLength(1);
  expect(object(consumed[0].data).content).toBe(input.content);
  // A delivery row belongs to the response it reports: one per response,
  // whatever input or engine work produced it.
  const outbox = channelOutboxRows(sessionId).filter((row) => row.response_id === response!.response_id);
  expect(outbox).toHaveLength(1);
  await expect.poll(() => channelOutboxRows(sessionId)
    .find((row) => row.response_id === response!.response_id)?.state).toBe('DELIVERED');
  evidence.settled = { input: taken, response, consumed };
  return response!;
}

test.beforeEach(async ({ request }) => {
  agentId = ''; deploymentId = ''; sessionId = '';
  for (const key of Object.keys(evidence)) delete evidence[key];
  source = await satoriSource();
  agentId = (await new AstraApi(request).createColdTestAgent(`recall-${randomUUID()}`)).agent_id;
  deploymentId = (await new PlatformApi(request).createDeployment(agentId, {
    scene: 'channel:satori', prompt_prefix: '', attention_policy: 'mentions',
    channel_config: { endpoint: source.endpoint }, credentials: { token: source.token },
  })).deployment_id;
  await source.waitConnected();
});

test.afterEach(async ({ request }, info) => {
  try {
    const failed = ['failed', 'timedOut', 'interrupted'].includes(String(info.status));
    await info.attach('channel-recall-boundary-evidence', {
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

test('channel recall before Agent submission replaces retained context with a timestamped marker', async ({ request, page }) => {
  const api = new AstraApi(request);
  const unique = randomUUID();
  const contextId = `context-${unique}`;
  const wakeId = `wake-${unique}`;
  const secret = `RECALLED_SECRET_${unique}`;
  const base = Date.now();
  const recalledAt = new Date(base + 1000).toISOString();
  const marker = `[该消息已于 ${recalledAt} 被撤回]`;
  const wakeText = `Conversation ${unique}. Explain what a notebook is in one short sentence. Do not use tools.`;
  sendMessage(contextId, secret, base);
  await expect.poll(() => messagesFor(contextId)[0]?.state).toBe('OBSERVED');
  expect(messagesFor(contextId)[0]?.session_id).toBeNull();
  recallMessage(contextId, base + 1000, 2);
  await expect.poll(() => messagesFor(contextId)[0]?.recall_timestamp).toBe(recalledAt);
  const recalled = messagesFor(contextId)[0];
  expect(recalled.recall_event_id).toBe(`e2e:${source!.botId}:event:message-deleted:sn:2`);
  expect(String(recalled.recall_event_id)).not.toEqual(String(recalled.dedup_key));
  expectOnlyUserMessages([contextId]);
  expect(source!.deliveries).toHaveLength(0);
  // The same source event traverses a fresh gateway ACK cursor. A later
  // accepted wake proves it did not poison or stop the source with a conflict.
  recallMessage(contextId, base + 1000, 2);
  await source!.waitSent(3);
  // Deliberately include the secret inside the original quote. The adapter
  // must omit quoted bodies and platform context must use the durable recall.
  sendMessage(wakeId, `<quote id="${contextId}">${secret}</quote><at id="${source!.botId}"/>${wakeText}`, base + 2000);
  const input = await waitAccepted(wakeId);
  const content = input.content;
  expect(content).toContain(marker);
  expect(content).toContain(wakeText);
  expect(content).not.toContain(secret);
  expect(inputs()).toHaveLength(1);
  await waitSettled(input, 1);
  await expect.poll(() => nativeInputs(content).length).toBe(1);
  const wake = messagesFor(wakeId)[0];
  expect(wake.frozen_content).toBe(content);
  expect(wake.frozen_context).toEqual([`e2e:${source!.botId}:channel:room:message:${contextId}`]);
  expect(JSON.stringify({ content: wake.frozen_content, context: wake.frozen_context })).not.toContain(secret);
  expect(messagesFor(contextId)[0].context_submitted).toBe(true);
  expect(channelMessageInputs(recalled.item_id), 'the recalled message is never an input itself').toEqual([]);
  const history = visibleMessages(await api.getMessages(sessionId, 100));
  expect(history.filter((row) => row.role === 'user').map(messageText)).toEqual([content]);
  expect(inputs()).toEqual([
    { input_id: input.input_id, client_message_id: input.client_message_id, source: 'channel', content },
  ]);
  await openSessionView(page, sessionId);
  await expect(page.getByText(marker, { exact: false }).first()).toBeVisible();
  await expect(page.getByText(secret, { exact: false })).toHaveCount(0);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByText(marker, { exact: false }).first()).toBeVisible();
  await expect(page.getByText(secret, { exact: false })).toHaveCount(0);
  evidence.original = { input, native: nativeInputs(content), recalled, marker };
  evidence.inbound = inboundRows();
  evidence.history = history;
});

test('channel recall after its input is accepted preserves that input without sending another', async ({ request, page }) => {
  const api = new AstraApi(request);
  const unique = randomUUID();
  const base = Date.now();
  const parentId = `parent-${unique}`;
  const wakeId = `direct-${unique}`;
  const parentText = `Conversation ${unique}. Explain what a bookmark is in one short sentence. Do not use tools.`;
  sendMessage(parentId, `<at id="${source!.botId}"/>${parentText}`, base);
  const parent = await waitAccepted(parentId);
  await waitSettled(parent, 1);
  const originalSessionId = sessionId;
  const text = `Conversation ${unique}. Explain what a notebook is in one short sentence. Do not use tools.`;
  sendMessage(wakeId, `<quote id="${parentId}"/><at id="${source!.botId}"/>${text}`, base + 1000);
  const input = await waitAccepted(wakeId);
  expect(messagesFor(wakeId)[0].session_id).toBe(originalSessionId);
  expect(input.content).toBe(text);
  const originalInputs = inputs();
  expect(originalInputs).toHaveLength(2);
  const originalFrozen = {
    content: messagesFor(wakeId)[0].frozen_content, context: messagesFor(wakeId)[0].frozen_context,
  };
  const recalledAt = new Date(base + 2000).toISOString();
  recallMessage(wakeId, base + 2000, 3);
  await expect.poll(() => messagesFor(wakeId)[0]?.recall_timestamp).toBe(recalledAt);
  await waitSettled(input, 2);
  await expect.poll(() => nativeInputs(text).length).toBe(1);
  const originalNative = nativeInputs(text);
  expect(inputs()).toEqual(originalInputs);
  expect({ content: messagesFor(wakeId)[0].frozen_content, context: messagesFor(wakeId)[0].frozen_context })
    .toEqual(originalFrozen);
  expect(channelMessageInputs(messagesFor(wakeId)[0].item_id).map((row) => row.input_id),
    'the recalled message keeps the one input it was').toEqual([input.input_id]);
  expectOnlyUserMessages([parentId, wakeId]);
  const history = visibleMessages(await api.getMessages(sessionId, 100));
  expect(history.filter((row) => row.role === 'user').map(messageText)).toEqual([parentText, text]);
  await openSessionView(page, sessionId);
  await expect(page.getByText(text, { exact: false }).first()).toBeVisible();
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByText(text, { exact: false }).first()).toBeVisible();
  await expect(page.getByText(`[该消息已于 ${recalledAt} 被撤回]`, { exact: false })).toHaveCount(0);
  expect(nativeInputs(text)).toEqual(originalNative);
  expect(inputs()).toEqual(originalInputs);
  evidence.original = { originalInputs, originalFrozen, originalNative, parent, input, recalledAt };
  evidence.inbound = inboundRows();
  evidence.history = history;
});
