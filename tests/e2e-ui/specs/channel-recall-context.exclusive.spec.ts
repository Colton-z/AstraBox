/** Donor recall boundaries through the official Satori adapter, real engine and DB. */
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
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('expected object');
  return value as Record<string, unknown>;
}

function inboundRows() { return documentsByField('channel_inbound', '$.deployment_id', deploymentId); }
function messagesFor(messageId: string) {
  return inboundRows().filter((row) => row.dedup_key === `e2e:${source!.botId}:message:${messageId}`);
}
function expectOnlyUserMessages(messageIds: string[]) {
  const prefix = `e2e:${source!.botId}:message:`;
  const rows = inboundRows();
  expect(rows.filter((row) => String(row.dedup_key).startsWith(prefix))
    .map((row) => row.dedup_key).sort()).toEqual(messageIds.map((id) => `${prefix}${id}`).sort());
  // The official adapter emits login lifecycle events before user messages.
  // They remain durable source receipts, never Agent tasks or retained context.
  for (const row of rows.filter((item) => !String(item.dedup_key).startsWith(prefix))) {
    expect(String(row.dedup_key)).toMatch(/:event:login-(added|updated):/);
    expect(row).toMatchObject({ state: 'IGNORED', session_id: null, command_id: null, turn_id: null });
    expect(object(row.payload).retain_context).toBe(false);
    expect(String(object(row.payload).provider_ignore_reason)).toMatch(/login-(added|updated)/);
  }
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

async function waitAccepted(messageId: string) {
  await expect.poll(() => {
    source!.healthy();
    const row = messagesFor(messageId)[0];
    if (row?.session_id && !sessionId) {
      sessionId = String(row.session_id);
      sessions.push(sessionId);
    }
    if (!sessionId || !row) return 0;
    const token = `${row._id}::a0`;
    return commands().filter((command) => object(command.payload).client_message_id === token).length;
  }, { timeout: 60_000, message: 'the original channel message must become one accepted StartTurn' }).toBe(1);
  const row = messagesFor(messageId)[0];
  return commands().find((command) => object(command.payload).client_message_id === `${row._id}::a0`)!;
}

async function waitSettled(command: Record<string, unknown>, expectedDeliveries: number) {
  await waitForTurnTerminalProof(sessionId, String(command.turn_id), 'COMPLETED', 60_000);
  await expect.poll(() => {
    source!.healthy();
    return source!.deliveries.length;
  }, { timeout: 30_000 }).toBe(expectedDeliveries);
  expect(source!.deliveries.every((delivery) => delivery.channel_id === 'room'
    && delivery.content.trim().length > 0)).toBe(true);
  const commandId = String(command.causation_id);
  const inputId = String(object(command.payload).input_id);
  const receipts = sessionEvents(sessionId).filter((row) => row.causation_id === commandId
    && ['input.delivered', 'input.consumed'].includes(String(row.event_type)));
  expect(receipts.map((row) => row.event_type).sort()).toEqual(['input.consumed', 'input.delivered']);
  expect(receipts.every((row) => object(row.payload).input_id === inputId)).toBe(true);
  expect(object(receipts.find((row) => row.event_type === 'input.consumed')!.payload).content)
    .toBe(object(command.payload).content);
  const outbox = documentsByField('channel_outbox', '$.session_id', sessionId)
    .filter((row) => row.command_id === commandId);
  expect(outbox).toHaveLength(1);
  await expect.poll(() => documentsByField('channel_outbox', '$.session_id', sessionId)
    .find((row) => row.command_id === commandId)?.state).toBe('DELIVERED');
  evidence.receipts = receipts;
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
  await expect.poll(() => messagesFor(contextId)[0]?.state).toBe('IGNORED');
  expect(messagesFor(contextId)[0]?.session_id).toBeNull();
  recallMessage(contextId, base + 1000, 2);
  await expect.poll(() => object(messagesFor(contextId)[0]?.recall || {}).timestamp).toBe(recalledAt);
  const recall = object(messagesFor(contextId)[0].recall);
  expect(recall.event_id).toBe(`e2e:${source!.botId}:event:message-deleted:sn:2`);
  expect(String(recall.event_id)).not.toEqual(String(messagesFor(contextId)[0].dedup_key));
  expectOnlyUserMessages([contextId]);
  expect(source!.deliveries).toHaveLength(0);
  // The same source event traverses a fresh gateway ACK cursor. A later
  // accepted wake proves it did not poison or stop the source with a conflict.
  recallMessage(contextId, base + 1000, 2);
  await source!.waitSent(3);
  // Deliberately include the secret inside the original quote. The adapter
  // must omit quoted bodies and platform context must use the durable recall.
  sendMessage(wakeId, `<quote id="${contextId}">${secret}</quote><at id="${source!.botId}"/>${wakeText}`, base + 2000);
  const command = await waitAccepted(wakeId);
  const content = String(object(command.payload).content);
  expect(content).toContain(marker);
  expect(content).toContain(wakeText);
  expect(content).not.toContain(secret);
  expect(commands()).toHaveLength(1);
  await waitSettled(command, 1);
  await expect.poll(() => nativeInputs(content).length).toBe(1);
  expect(JSON.stringify(object(messagesFor(wakeId)[0].frozen_input))).not.toContain(secret);
  expect(messagesFor(contextId)[0].context_submitted).toBe(true);
  const history = visibleMessages(await api.getMessages(sessionId, 100));
  expect(history.filter((row) => row.role === 'user').map(messageText)).toEqual([content]);
  expect(commands()).toEqual([command]);
  await openSessionView(page, sessionId);
  await expect(page.getByText(marker, { exact: false }).first()).toBeVisible();
  await expect(page.getByText(secret, { exact: false })).toHaveCount(0);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByText(marker, { exact: false }).first()).toBeVisible();
  await expect(page.getByText(secret, { exact: false })).toHaveCount(0);
  evidence.original = { command, native: nativeInputs(content), recall, marker };
  evidence.inbound = inboundRows();
  evidence.history = history;
});

test('channel recall after StartTurn preserves the original accepted input without opening another turn', async ({ request, page }) => {
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
  const command = await waitAccepted(wakeId);
  expect(messagesFor(wakeId)[0].session_id).toBe(originalSessionId);
  expect(object(command.payload).content).toBe(text);
  const originalCommands = commands();
  expect(originalCommands).toHaveLength(2);
  const originalFrozen = messagesFor(wakeId)[0].frozen_input;
  const recalledAt = new Date(base + 2000).toISOString();
  recallMessage(wakeId, base + 2000, 3);
  await expect.poll(() => object(messagesFor(wakeId)[0]?.recall || {}).timestamp).toBe(recalledAt);
  await waitSettled(command, 2);
  await expect.poll(() => nativeInputs(text).length).toBe(1);
  const originalNative = nativeInputs(text);
  expect(commands()).toEqual(originalCommands);
  expect(messagesFor(wakeId)[0].frozen_input).toEqual(originalFrozen);
  expectOnlyUserMessages([parentId, wakeId]);
  const history = visibleMessages(await api.getMessages(sessionId, 100));
  expect(history.filter((row) => row.role === 'user').map(messageText)).toEqual([parentText, text]);
  await openSessionView(page, sessionId);
  await expect(page.getByText(text, { exact: false }).first()).toBeVisible();
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByText(text, { exact: false }).first()).toBeVisible();
  await expect(page.getByText(`[该消息已于 ${recalledAt} 被撤回]`, { exact: false })).toHaveCount(0);
  expect(nativeInputs(text)).toEqual(originalNative);
  expect(commands()).toEqual(originalCommands);
  evidence.original = { originalCommands, originalFrozen, originalNative, parent, command, recalledAt };
  evidence.inbound = inboundRows();
  evidence.history = history;
});
