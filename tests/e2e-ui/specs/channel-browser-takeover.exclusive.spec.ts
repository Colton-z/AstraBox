/** A channel-created conversation stays native and live when its owner types in the console. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText, visibleMessages } from '../fixtures/astraApi';
import { documentsByField, sessionEvents, waitForTurnTerminalProof } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { satoriSource } from '../fixtures/satoriSource';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { expectPromptDelivered, openSessionView, startPromptDelivery } from '../fixtures/sessionPage';
import { aiStreamBodies, mirrorSseBodies, type SseBody } from '../fixtures/sseBodies';

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
let sessionId = '';
let source: Awaited<ReturnType<typeof satoriSource>> | null = null;
const evidence: Record<string, unknown> = {};

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('expected an evidence object');
  return value as Record<string, unknown>;
}

function inboundRows() { return documentsByField('channel_inbound', '$.deployment_id', deploymentId); }
function commands() {
  return sessionEvents(sessionId).filter((row) => row.event_type === 'command.accepted'
    && object(row.payload).command_type === 'StartTurn');
}

function nativeRows() {
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .filter((row) => row.subpath == null)
    .map((row) => ({ uuid: String(row.uuid), seq: Number(row.seq), session_id: String(row.session_id),
      entry: object(JSON.parse(String(row.entry_json))) }))
    .sort((left, right) => left.seq - right.seq);
}

function nativeText(entry: Record<string, unknown>) {
  if (!entry.message || typeof entry.message !== 'object') return '';
  const content = object(entry.message).content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.filter((block) => block.type === 'text').map((block) => String(block.text || '')).join('');
}

function nativeInputs(content: string) {
  return nativeRows().filter((row) => row.entry.type === 'user' && nativeText(row.entry) === content);
}

function linkedAssistantRows(inputUuid: string) {
  const descendants = new Set([inputUuid]);
  return nativeRows().filter((row) => {
    if (!descendants.has(String(row.entry.parentUuid || ''))) return false;
    // Preserve attachment/system links; a different ordinary input starts a new reply.
    if (row.entry.type === 'user' && nativeText(row.entry)) return false;
    descendants.add(row.uuid);
    return row.entry.type === 'assistant' && row.entry.isSidechain !== true && nativeText(row.entry).trim();
  });
}

function frames(body: SseBody): Record<string, unknown>[] {
  return body.text.split('\n').slice(0, -1)
    .filter((line) => line.startsWith('data:') && line.slice(5).trim() !== '[DONE]')
    .map((line) => object(JSON.parse(line.slice(5).trim())));
}

function framesSince(bodies: SseBody[], baseline: number[]) {
  return bodies.flatMap((body, index) => frames(body).slice(baseline[index] || 0));
}

function inputReceipts(command: Record<string, unknown>) {
  const result = sessionEvents(sessionId).filter((row) => row.causation_id === command.causation_id
    && ['input.delivered', 'input.consumed'].includes(String(row.event_type)));
  expect(result.map((row) => row.event_type).sort()).toEqual(['input.consumed', 'input.delivered']);
  expect(result.every((row) => object(row.payload).input_id === object(command.payload).input_id)).toBe(true);
  const consumed = object(result.find((row) => row.event_type === 'input.consumed')!.payload);
  expect(consumed.content).toBe(object(command.payload).content);
  return { result, consumed };
}

test.beforeEach(async ({ request }) => {
  agentId = ''; deploymentId = ''; sessionId = '';
  for (const key of Object.keys(evidence)) delete evidence[key];
  source = await satoriSource();
  agentId = (await new AstraApi(request).createColdTestAgent(`channel-browser-${randomUUID()}`)).agent_id;
  deploymentId = (await new PlatformApi(request).createDeployment(agentId, {
    scene: 'channel:satori', prompt_prefix: '', attention_policy: 'mentions',
    channel_config: { endpoint: source.endpoint }, credentials: { token: source.token },
  })).deployment_id;
  await source.waitConnected();
});

test.afterEach(async ({ request, page }, info) => {
  try {
    const failed = ['failed', 'timedOut', 'interrupted'].includes(String(info.status));
    await info.attach('channel-browser-takeover-evidence', {
      body: JSON.stringify({ agentId, deploymentId, sessionId, ...evidence,
        retainedInbound: failed && deploymentId ? inboundRows() : [],
        journal: failed && sessionId ? sessionEvents(sessionId) : [],
        native: failed && sessionId ? nativeRows() : [],
        history: failed && sessionId ? await new AstraApi(request).getMessages(sessionId, 100) : null,
        browserStreams: failed && !page.isClosed() ? await aiStreamBodies(page) : [],
        deliveries: source?.deliveries, sourceErrors: source?.errors }),
      contentType: 'application/json',
    });
  } finally { if (source) await source.close(); source = null; }
});

onPassOnly(async ({ request }) => {
  if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('a channel-created conversation accepts one browser turn without replacing its native session or replaying Store history', async ({ request, page }) => {
  const api = new AstraApi(request);
  const id = randomUUID();
  const messageId = `channel-parent-${id}`;
  const channelPrompt = `Conversation ${id}. In one short sentence explain what a bookmark is. Do not use tools.`;
  const browserPrompt = `Conversation ${id}. In one short sentence explain what a notebook is. Do not use tools.`;
  const timestamp = Date.now();
  const messageKey = `e2e:${source!.botId}:message:${messageId}`;
  source!.send({ type: 'message-created', timestamp,
    channel: { id: 'room', type: 0 }, user: { id: 'participant', name: 'Participant' },
    message: { id: messageId, content: `<at id="${source!.botId}"/>${channelPrompt}`, created_at: timestamp } });
  await expect.poll(() => {
    source!.healthy();
    const row = inboundRows().find((item) => item.dedup_key === messageKey);
    if (row?.session_id && !sessionId) {
      sessionId = String(row.session_id);
      sessions.push(sessionId);
    }
    return { state: row?.state, deliveries: source!.deliveries.length };
  }, { timeout: 90_000, message: 'the actual channel must create the Session and deliver its original reply' })
    .toEqual({ state: 'SETTLED', deliveries: 1 });
  const originalInbound = inboundRows().find((row) => row.dedup_key === messageKey)!;
  const parentCommands = commands();
  expect(parentCommands).toHaveLength(1);
  const parent = parentCommands[0];
  expect(object(parent.payload).content).toBe(channelPrompt);
  expect(parent.causation_id).toBe(originalInbound.command_id);
  await waitForTurnTerminalProof(sessionId, String(parent.turn_id), 'COMPLETED', 30_000);
  expect(source!.deliveries[0].channel_id).toBe('room');
  expect(source!.deliveries[0].content.trim()).not.toBe('');
  await expect.poll(() => nativeInputs(channelPrompt).length).toBe(1);
  const originalNative = nativeRows();
  const before = await api.adminSessionDetail(sessionId);
  const sandboxId = String(before.sandbox_id || '').trim();
  const nativeSessionKey = String(before.engine_session_key || '').trim();
  expect(sandboxId).not.toBe('');
  expect(nativeSessionKey).not.toBe('');
  expect(before.has_local_runtime).toBe(true);
  expect(before.runtime_identity?.sandbox_id).toBe(sandboxId);
  expect(String(before.runtime_identity?.isolated_session_id || '')).toBe('');
  expect(originalNative.every((row) => row.session_id === nativeSessionKey)).toBe(true);
  evidence.original = { originalInbound, parent, originalNative, sandboxId, nativeSessionKey,
    receipts: inputReceipts(parent) };

  await mirrorSseBodies(page);
  await openSessionView(page, sessionId);
  await expect(page.getByTestId('user-message')).toHaveText([channelPrompt]);
  await expect(page.getByTestId('assistant-text')).toHaveCount(1);
  // Virtuoso can mount text in a hidden measurement pass. Capture the actual
  // nonempty rendered baseline, not textContent followed by an empty innerText.
  let firstRenderedReply = '';
  await expect.poll(async () => {
    firstRenderedReply = await page.getByTestId('assistant-text').innerText();
    return firstRenderedReply.trim();
  }, { message: 'the original channel reply must render before browser continuation' }).not.toBe('');
  evidence.firstRenderedReply = firstRenderedReply;
  const baseline = (await aiStreamBodies(page)).map((body) => frames(body).length);
  const sentInputs: Record<string, unknown>[] = [];
  page.on('request', (sent) => {
    if (sent.method() === 'POST' && new URL(sent.url()).pathname === apiPath(`/sessions/${sessionId}/turn-inputs`)) {
      sentInputs.push(object(sent.postDataJSON()));
    }
  });
  const pending = await startPromptDelivery(page, sessionId, browserPrompt);
  const response = await expectPromptDelivered(pending);
  const receipt = object(object(await response.json()).data);
  expect(sentInputs).toHaveLength(1);
  expect(sentInputs[0]).toMatchObject({ content: browserPrompt, client_message_id: pending.clientMessageId });
  await expect.poll(() => commands().filter((command) => object(command.payload).client_message_id
    === pending.clientMessageId).length).toBe(1);
  const browserCommand = commands().find((command) => object(command.payload).client_message_id
    === pending.clientMessageId)!;
  expect(browserCommand.causation_id).toBe(receipt.command_id);
  expect(browserCommand.turn_id).not.toBe(parent.turn_id);
  const terminal = await waitForTurnTerminalProof(sessionId, String(browserCommand.turn_id), 'COMPLETED', 60_000);
  const receipts = inputReceipts(browserCommand);
  await expect.poll(async () => framesSince(await aiStreamBodies(page), baseline)
    .filter((frame) => frame.type === 'data-result').length, { timeout: 30_000 }).toBe(1);
  await expect.poll(async () => (await aiStreamBodies(page)).some((body) =>
    frames(body).some((frame) => frame.type === 'data-input-consumed'
      && object(frame.data).clientMessageId === pending.clientMessageId)
    && body.text.split('\n').some((line) => line.trim() === 'data: [DONE]')),
  { message: 'the actual browser reply stream must close after its Result' }).toBe(true);
  const bodies = await aiStreamBodies(page);
  const browserFrames = framesSince(bodies, baseline);
  const resultFrames = browserFrames.filter((frame) => frame.type === 'data-result');
  expect(resultFrames).toHaveLength(1);
  const resultData = object(resultFrames[0].data);
  expect(typeof resultData.duration_ms).toBe('number');
  expect(typeof resultData.total_cost_usd).toBe('number');
  expect(resultData.num_turns).toBe(1);
  const durationLabel = `${(Number(resultData.duration_ms) / 1000).toFixed(1)}s`;
  const costLabel = `$${Number(resultData.total_cost_usd).toFixed(4)}`;
  const consumedFrames = browserFrames.filter((frame) => frame.type === 'data-input-consumed');
  expect(consumedFrames).toHaveLength(1);
  const consumed = object(consumedFrames[0].data);
  expect(consumed).toMatchObject({ inputId: receipts.consumed.input_id,
    responseMessageId: receipts.consumed.response_message_id,
    clientMessageId: pending.clientMessageId, content: browserPrompt });
  expect(String(consumed.inputId)).toMatch(/^[0-9a-f-]{36}$/);
  expect(String(consumed.responseMessageId)).toMatch(/^[0-9a-f-]{36}$/);
  expect(browserFrames.filter((frame) => frame.type === 'error')).toEqual([]);
  expect(browserFrames.map((frame) => frame.type)).not.toContain('data-session-store-reload');
  const streamedText = browserFrames.filter((frame) => frame.type === 'text-delta')
    .map((frame) => String(frame.delta || '')).join('');
  expect(streamedText.trim(), 'the browser must receive the new reply, not just refresh old history').not.toBe('');
  const nativeTerminals = sessionEvents(sessionId).filter((row) => row.causation_id === browserCommand.causation_id
    && row.event_type === 'engine.diagnostic' && object(row.payload).event_type === 'engine.terminal');
  expect(nativeTerminals).toHaveLength(1);
  expect(object(object(nativeTerminals[0].payload).raw).terminal_reason).toBe('completed');

  await expect.poll(() => nativeInputs(browserPrompt).length).toBe(1);
  const browserInput = nativeInputs(browserPrompt)[0];
  const nativeReplies = linkedAssistantRows(browserInput.uuid);
  const completedReplies = nativeReplies.filter((row) => object(row.entry.message).stop_reason === 'end_turn');
  expect(completedReplies).toHaveLength(1);
  expect(nativeText(completedReplies[0].entry)).toBe(streamedText);
  const after = await api.adminSessionDetail(sessionId);
  expect(after.session_id).toBe(sessionId);
  expect(after.sandbox_id).toBe(sandboxId);
  expect(after.engine_session_key).toBe(nativeSessionKey);
  expect(after.has_local_runtime).toBe(true);
  expect(after.runtime_identity?.sandbox_id).toBe(sandboxId);
  expect(String(after.runtime_identity?.isolated_session_id || '')).toBe('');
  const afterNative = nativeRows();
  expect(afterNative.length).toBeGreaterThan(originalNative.length);
  // SDK queue/metadata entries need not carry a UUID. Preserve their complete
  // committed prefix, while retaining UUID uniqueness for native messages.
  expect(afterNative.slice(0, originalNative.length)).toEqual(originalNative);
  for (const row of originalNative) {
    expect(afterNative.filter((current) => current.seq === row.seq)).toEqual([row]);
    if (typeof row.entry.uuid === 'string') {
      expect(afterNative.filter((current) => current.uuid === row.uuid)).toEqual([row]);
    }
  }
  expect(afterNative.every((row) => row.session_id === nativeSessionKey)).toBe(true);
  expect(commands()).toEqual([parent, browserCommand]);
  expect(documentsByField('sessions', '$.agent_id', agentId).map((row) => row.session_id)).toEqual([sessionId]);
  expect(inboundRows().filter((row) => row.dedup_key === messageKey)).toEqual([originalInbound]);
  const allInbound = inboundRows();
  expect(allInbound.filter((row) => String(row.dedup_key)
    .startsWith(`e2e:${source!.botId}:message:`)).map((row) => row.dedup_key)).toEqual([messageKey]);
  for (const lifecycle of allInbound.filter((row) => row.dedup_key !== messageKey)) {
    expect(lifecycle.state).toBe('IGNORED');
    expect(lifecycle.session_id).toBeNull();
    expect(lifecycle.command_id).toBeNull();
    expect(object(lifecycle.payload).retain_context).toBe(false);
    expect(object(lifecycle.payload).provider_ignore_reason)
      .toMatch(/^channel event 'login-(?:added|updated)' is not message-created$/);
  }
  expect(source!.deliveries).toHaveLength(1);
  expect(documentsByField('channel_outbox', '$.session_id', sessionId)).toHaveLength(1);
  const history = visibleMessages(await api.getMessages(sessionId, 100));
  expect(history.filter((row) => row.role === 'user').map(messageText)).toEqual([channelPrompt, browserPrompt]);
  const replies = history.filter((row) => row.role === 'assistant');
  expect(replies).toHaveLength(2);
  expect(replies.filter((row) => row.turn_id === browserCommand.turn_id).map(messageText)).toEqual([streamedText]);
  await expect(page.getByTestId('user-message')).toHaveText([channelPrompt, browserPrompt]);
  await expect(page.getByTestId('assistant-text')).toHaveCount(2);
  await expect(page.getByTestId('assistant-text').last()).toHaveText(/\S/, { useInnerText: true });
  const browserReply = page.getByTestId('assistant-message').last();
  await expect(browserReply.getByText(durationLabel, { exact: true })).toBeVisible();
  await expect(browserReply.getByText(costLabel)).toBeVisible();
  const renderedReplies = await page.getByTestId('assistant-text').allInnerTexts();
  expect(renderedReplies[0]).toBe(firstRenderedReply);
  evidence.completed = { receipt, browserCommand, receipts, terminal, bodies, browserFrames, allInbound,
    nativeTerminals, browserInput, nativeReplies, afterNative, history, renderedReplies };
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('user-message')).toHaveText([channelPrompt, browserPrompt]);
  await expect(page.getByTestId('assistant-text')).toHaveText(renderedReplies, { useInnerText: true });
  await expect(browserReply.getByText(durationLabel, { exact: true })).toBeVisible();
  await expect(browserReply.getByText(costLabel)).toBeVisible();
  expect(sentInputs).toHaveLength(1);
  expect(commands()).toEqual([parent, browserCommand]);
  expect(nativeInputs(browserPrompt)).toEqual([browserInput]);
});
