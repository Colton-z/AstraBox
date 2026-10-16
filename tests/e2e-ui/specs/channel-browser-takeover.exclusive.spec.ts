/** A channel-created conversation stays native and live when its owner types in the console. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, answersInput, messageText, visibleMessages } from '../fixtures/astraApi';
import {
  nativeTranscriptRows,
  sessionInputRows,
  sessionResponseRows,
  type SessionInputRow,
  type SessionResponseRow,
} from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import {
  agentSessionRows,
  channelInboundRows,
  channelMessageInputs,
  channelOutboxRows,
  type ChannelInboundRow,
} from '../fixtures/ingressRecords';
import { PlatformApi } from '../fixtures/platformApi';
import { satoriReplyTexts, satoriSource } from '../fixtures/satoriSource';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { expectPromptDelivered, openSessionView, startPromptDelivery } from '../fixtures/sessionPage';
import { sessionStreamBodies, mirrorSseBodies, type SseBody } from '../fixtures/sseBodies';

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

function inboundRows() { return channelInboundRows(deploymentId); }

/** A message record's facts that change only when the message itself does. */
function messageRecord(row: ChannelInboundRow) {
  return {
    item_id: row.item_id, dedup_key: row.dedup_key, state: row.state, session_id: row.session_id,
    generation: row.generation, attempts: row.attempts, content: row.content,
    frozen_content: row.frozen_content, frozen_context: row.frozen_context,
    context_submitted: row.context_submitted,
  };
}

/** The inputs the Session accepted, by their identity and content. */
function inputs() {
  return sessionInputRows(sessionId).map((row) => ({
    input_id: row.input_id, client_message_id: row.client_message_id, source: row.source, content: row.content,
  }));
}

function nativeRows() {
  return nativeTranscriptRows(sessionId)
    .filter((row) => row.subpath == null)
    .map((row) => ({ uuid: String(row.uuid), seq: Number(row.seq), session_id: row.native_session_id,
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

/** Wait until the response answering `input` has ended, and return it. */
async function completedResponse(input: { input_id: string }, timeout: number): Promise<SessionResponseRow> {
  let response: SessionResponseRow | undefined;
  await expect.poll(() => {
    response = sessionResponseRows(sessionId).find((row) => row.input_ids.includes(input.input_id));
    return response ? `${response.status}/${response.outcome ?? ''}` : 'no response yet';
  }, { timeout, message: 'the response answering the input must complete' }).toBe('ended/completed');
  return response!;
}

/** The runtime took the input: the platform records it admitted, answered by `response`. */
function takenBy(input: { input_id: string }, response: SessionResponseRow): SessionInputRow {
  const taken = sessionInputRows(sessionId).find((row) => row.input_id === input.input_id);
  expect(taken).toMatchObject({ state: 'admitted', response_id: response.response_id });
  expect(response.input_ids).toEqual([input.input_id]);
  return taken!;
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
        inputs: failed && sessionId ? sessionInputRows(sessionId) : [],
        responses: failed && sessionId ? sessionResponseRows(sessionId) : [],
        native: failed && sessionId ? nativeRows() : [],
        history: failed && sessionId ? await new AstraApi(request).getMessages(sessionId, 100) : null,
        browserStreams: failed && !page.isClosed() ? await sessionStreamBodies(page) : [],
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
  const messageKey = `e2e:${source!.botId}:channel:room:message:${messageId}`;
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
  const parentInputs = inputs();
  expect(parentInputs).toHaveLength(1);
  const parent = parentInputs[0];
  expect(parent).toMatchObject({ client_message_id: originalInbound.item_id, source: 'channel', content: channelPrompt });
  expect(channelMessageInputs(originalInbound.item_id).map((row) => row.input_id)).toEqual([parent.input_id]);
  const parentResponse = await completedResponse(parent, 30_000);
  const parentTaken = takenBy(parent, parentResponse);
  expect(source!.deliveries[0].channel_id).toBe('room');
  expect(source!.deliveries[0].content.trim()).not.toBe('');
  await expect.poll(() => nativeInputs(channelPrompt).length).toBe(1);
  const originalNative = nativeRows();
  const before = await api.adminSessionDetail(sessionId);
  const sandboxId = String(before.sandbox_id || '').trim();
  const nativeSessionKey = String(before.engine_session_key || '').trim();
  expect(sandboxId).not.toBe('');
  expect(nativeSessionKey).not.toBe('');
  expect(before.runtime_identity?.sandbox_id).toBe(sandboxId);
  expect(String(before.runtime_identity?.isolated_session_id || '')).toBe('');
  expect(originalNative.every((row) => row.session_id === nativeSessionKey)).toBe(true);
  evidence.original = { originalInbound, parent: parentTaken, parentResponse, originalNative, sandboxId,
    nativeSessionKey };

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
  const baseline = (await sessionStreamBodies(page)).map((body) => frames(body).length);
  const sentInputs: Record<string, unknown>[] = [];
  page.on('request', (sent) => {
    if (sent.method() === 'POST' && new URL(sent.url()).pathname === apiPath(`/sessions/${sessionId}/inputs`)) {
      sentInputs.push(object(sent.postDataJSON()));
    }
  });
  const pending = await startPromptDelivery(page, sessionId, browserPrompt);
  const response = await expectPromptDelivered(pending);
  const receipt = object(object(await response.json()).data);
  expect(sentInputs).toHaveLength(1);
  expect(sentInputs[0]).toMatchObject({ content: browserPrompt, client_message_id: pending.clientMessageId });
  expect(receipt).toMatchObject({ session_id: sessionId, client_message_id: pending.clientMessageId, duplicate: false });
  await expect.poll(() => inputs().filter((input) => input.client_message_id
    === pending.clientMessageId).length).toBe(1);
  const browserInput = inputs().find((input) => input.client_message_id === pending.clientMessageId)!;
  expect(browserInput).toMatchObject({ input_id: receipt.input_id, source: 'console', content: browserPrompt });
  const browserResponse = await completedResponse(browserInput, 60_000);
  expect(browserResponse.response_id).not.toBe(parentResponse.response_id);
  const browserTaken = takenBy(browserInput, browserResponse);
  await expect.poll(async () => framesSince(await sessionStreamBodies(page), baseline)
    .filter((frame) => frame.type === 'data-result').length, { timeout: 30_000 }).toBe(1);
  await expect.poll(async () => (await sessionStreamBodies(page)).some((body) =>
    frames(body).some((frame) => frame.type === 'data-input-consumed'
      && object(frame.data).inputId === browserInput.input_id)
    && body.text.split('\n').some((line) => line.trim() === 'data: [DONE]')),
  { message: 'the actual browser reply stream must close after its Result' }).toBe(true);
  const bodies = await sessionStreamBodies(page);
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
  expect(consumed).toMatchObject({ inputId: browserInput.input_id,
    responseMessageId: browserResponse.response_id, content: browserPrompt });
  expect(String(consumed.inputId)).toMatch(/^[0-9a-f-]{36}$/);
  expect(String(consumed.responseMessageId)).toMatch(/^[0-9a-f-]{36}$/);
  expect(browserFrames.filter((frame) => frame.type === 'error')).toEqual([]);
  const streamedText = browserFrames.filter((frame) => frame.type === 'text-delta')
    .map((frame) => String(frame.delta || '')).join('');
  expect(streamedText.trim(), 'the browser must receive the new reply, not just refresh old history').not.toBe('');
  // The vendor's own ending of the browser reply, as the Claude adapter records it.
  expect(browserResponse).toMatchObject({ origin: 'input', outcome: 'completed', native_reason: 'completed' });

  await expect.poll(() => nativeInputs(browserPrompt).length).toBe(1);
  const browserNative = nativeInputs(browserPrompt)[0];
  const nativeReplies = linkedAssistantRows(browserNative.uuid);
  const completedReplies = nativeReplies.filter((row) => object(row.entry.message).stop_reason === 'end_turn');
  expect(completedReplies).toHaveLength(1);
  expect(nativeText(completedReplies[0].entry)).toBe(streamedText);
  const after = await api.adminSessionDetail(sessionId);
  expect(after.session_id).toBe(sessionId);
  expect(after.sandbox_id).toBe(sandboxId);
  expect(after.engine_session_key).toBe(nativeSessionKey);
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
  expect(inputs()).toEqual([parent, browserInput]);
  expect(agentSessionRows(agentId).map((row) => row.session_id)).toEqual([sessionId]);
  expect(inboundRows().filter((row) => row.dedup_key === messageKey).map(messageRecord))
    .toEqual([messageRecord(originalInbound)]);
  const allInbound = inboundRows();
  expect(allInbound.filter((row) => String(row.dedup_key)
    .startsWith(`e2e:${source!.botId}:channel:room:message:`)).map((row) => row.dedup_key)).toEqual([messageKey]);
  for (const lifecycle of allInbound.filter((row) => row.dedup_key !== messageKey)) {
    expect(lifecycle.state).toBe('IGNORED');
    expect(lifecycle.session_id).toBeNull();
    expect(channelMessageInputs(lifecycle.item_id)).toEqual([]);
    expect(lifecycle.retain_context).toBe(false);
    expect(lifecycle.provider_ignore_reason)
      .toMatch(/^channel event 'login-(?:added|updated)' is not message-created$/);
  }
  const history = visibleMessages(await api.getMessages(sessionId, 100));
  expect(history.filter((row) => row.role === 'user').map(messageText)).toEqual([channelPrompt, browserPrompt]);
  const replies = history.filter((row) => row.role === 'assistant');
  expect(replies).toHaveLength(2);
  expect(replies.filter((row) => answersInput(row, browserInput.input_id)).map(messageText)).toEqual([streamedText]);
  await expect.poll(() => {
    source!.healthy();
    return satoriReplyTexts(page, source!.deliveries, messageId);
  }, { timeout: 30_000 }).toEqual(replies.map((reply) => messageText(reply).trim()));
  const outboxes = channelOutboxRows(sessionId);
  expect(outboxes).toHaveLength(2);
  expect(outboxes.map((row) => row.response_id).sort()).toEqual(replies.map((reply) => reply.id).sort());
  await expect(page.getByTestId('user-message')).toHaveText([channelPrompt, browserPrompt]);
  await expect(page.getByTestId('assistant-text')).toHaveCount(2);
  await expect(page.getByTestId('assistant-text').last()).toHaveText(/\S/, { useInnerText: true });
  const browserReply = page.getByTestId('assistant-message').last();
  await expect(browserReply.getByText(durationLabel, { exact: true })).toBeVisible();
  await expect(browserReply.getByText(costLabel)).toBeVisible();
  const renderedReplies = await page.getByTestId('assistant-text').allInnerTexts();
  expect(renderedReplies[0]).toBe(firstRenderedReply);
  evidence.completed = { receipt, browserInput: browserTaken, browserResponse, bodies, browserFrames, allInbound,
    browserNative, nativeReplies, afterNative, history, renderedReplies };
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('user-message')).toHaveText([channelPrompt, browserPrompt]);
  await expect(page.getByTestId('assistant-text')).toHaveText(renderedReplies, { useInnerText: true });
  await expect(browserReply.getByText(durationLabel, { exact: true })).toBeVisible();
  await expect(browserReply.getByText(costLabel)).toBeVisible();
  expect(sentInputs).toHaveLength(1);
  expect(inputs()).toEqual([parent, browserInput]);
  expect(nativeInputs(browserPrompt)).toEqual([browserNative]);
});
