/** A real channel turn finishes behind its held runner receipt, then survives backend loss. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi, answersInput, messageText } from '../fixtures/astraApi';
import { channelCallback } from '../fixtures/channelCallback';
import {
  nativeTranscriptRows,
  sessionInputRows,
  sessionResponseRows,
  type SessionResponseRow,
} from '../fixtures/dbOracle';
import { absoluteBaseUrl, apiPath } from '../fixtures/env';
import {
  channelMessageInputs,
  channelOutboxRows,
  sessionChannelInboundRows,
  type ChannelInboundRow,
} from '../fixtures/ingressRecords';
import {
  installInputAckGate, readInputAckGate, signalInputAckGateRelease, waitForInputAckGateEntered,
  type InputAckGateState,
} from '../fixtures/inputAckGate';
import { PlatformApi } from '../fixtures/platformApi';
import { requireSandboxHandle, restartServerContainer, type SandboxHandle } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('expected an object');
  return value as Record<string, unknown>;
}

function nativeRootRows(sessionId: string) {
  return nativeTranscriptRows(sessionId)
    .filter((row) => row.subpath == null)
    .map((row) => ({ seq: Number(row.seq), uuid: row.uuid, entry: object(JSON.parse(String(row.entry_json))) }))
    .sort((a, b) => a.seq - b.seq);
}

function nativeInputs(sessionId: string, prompt: string) {
  return nativeRootRows(sessionId)
    .filter((row) => {
      if (row.entry.type !== 'user') return false;
      const content = object(row.entry.message).content;
      const text = typeof content === 'string' ? content : (content as Array<Record<string, unknown>>)
        .filter((block) => block.type === 'text').map((block) => String(block.text || '')).join('');
      return text === prompt;
    });
}

/** The inputs the Session accepted, by their identity and content. */
function inputs(sessionId: string) {
  return sessionInputRows(sessionId).map((row) => ({
    input_id: row.input_id, client_message_id: row.client_message_id, source: row.source, content: row.content,
  }));
}

/** A message record's facts that change only when the message itself does. */
function messageRecord(row: ChannelInboundRow) {
  return {
    item_id: row.item_id, state: row.state, session_id: row.session_id, generation: row.generation,
    attempts: row.attempts, content: row.content, frozen_content: row.frozen_content,
  };
}

function responseWindow(gate: InputAckGateState, inputId: string) {
  expect(gate.errors || []).toEqual([]);
  expect(gate.state).not.toBe('timed_out');
  const bySequence = new Map<number, Record<string, unknown>>();
  for (const frame of gate.wire_events || []) {
    const sequence = Number(frame.seq);
    expect(Number.isSafeInteger(sequence)).toBe(true);
    expect(sequence).toBeGreaterThan(0);
    if (bySequence.has(sequence)) expect(frame, 'replay must preserve the original SDK envelope').toEqual(bySequence.get(sequence));
    else bySequence.set(sequence, frame);
  }
  const frames = [...bySequence.values()].sort((a, b) => Number(a.seq) - Number(b.seq));
  const inputs = frames.filter((frame) => frame.message_type === 'UserMessage'
    && object(frame.message).uuid === inputId);
  expect(inputs.length).toBeLessThanOrEqual(1);
  const input = inputs[0];
  const following = input ? frames.filter((frame) => Number(frame.seq) > Number(input.seq)) : [];
  const nextInput = following.find((frame) => frame.message_type === 'UserMessage'
    && object(frame.message).parent_tool_use_id == null);
  const results = following.filter((frame) => frame.message_type === 'ResultMessage'
    && (!nextInput || Number(frame.seq) < Number(nextInput.seq)));
  return { input: input || null, results };
}

test('a channel turn completed before its runner receipt recovers after backend restart without another input', async ({
  page, request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const recipient = await channelCallback();
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const serverCommand = (args: string[]) => execFileSync('docker', [...args, server], {
    encoding: 'utf8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
  const evidence: Record<string, unknown> = {};
  let sessionId = '';
  let sandbox: SandboxHandle | null = null;
  let stopped = false;
  try {
    const unique = randomUUID();
    const baselinePrompt = 'In one short plain sentence, explain what a bookmark is. Do not use tools.';
    const prompt = `Conversation label ${unique}. In one short plain sentence, explain what a notebook is. Do not use tools.`;
    agentId = (await api.createColdTestAgent(`__e2e_channel_receipt_${unique}`)).agent_id;
    const deployment = await platform.createDeployment(agentId, { scene: 'channel:generic_json', prompt_prefix: '' });
    deploymentId = deployment.deployment_id;
    expect(deployment.secret).toBeTruthy();
    const trigger = async (text: string, messageId: string) => {
      const response = await request.post(apiPath(`/deployments/${deploymentId}/trigger`), {
        headers: { 'x-channel-secret': String(deployment.secret) },
        data: { text, message_id: messageId, conversation_id: unique, reply: { callback_url: recipient.url } },
      });
      expect(response.ok(), await response.text()).toBe(true);
      const receipt = (await response.json()).data;
      expect(receipt.status).toBe('accepted');
      expect(String(receipt.session_id || '')).not.toEqual('');
      return String(receipt.session_id);
    };
    sessionId = await trigger(baselinePrompt, `baseline-${unique}`);
    sessions.push(sessionId);
    await expect.poll(() => recipient.deliveries.length, { timeout: 60_000 }).toBe(1);
    expect(recipient.errors).toEqual([]);
    const ready = await api.waitForSessionReady(sessionId, 30_000);
    expect(ready.last_error ?? null).toBeNull();
    const detail = await api.adminSessionDetail(sessionId);
    expect(String(detail.runtime_identity?.isolated_session_id || ''), 'the fault owns the whole runner').toBe('');
    sandbox = await requireSandboxHandle(api, String(ready.sandbox_id));
    evidence.install = installInputAckGate(sandbox, { sessionId, maxWaitSeconds: 90 });
    expect((await api.adminEvictRuntime(sessionId)).evicted).toBe(sessionId);

    expect(await trigger(prompt, unique)).toBe(sessionId);
    const entered = await waitForInputAckGateEntered(sandbox, sessionId, 30_000);
    evidence.entered = entered;
    const held = entered.entered!;
    expect(held.ack_duplicate).toBe(false);
    // The held receipt is for the input the channel message became: its item
    // id is the input's client message id, and the runner wire carries the
    // input id as both the command and the SDK message identity.
    const inbound = sessionChannelInboundRows(sessionId).filter((row) => row.content === prompt);
    expect(inbound).toHaveLength(1);
    expect(inbound[0].state, 'the Session holds the message, so the channel has sent it').toBe('SETTLED');
    const messageInputs = channelMessageInputs(inbound[0].item_id);
    expect(messageInputs.map((row) => ({ session_id: row.session_id, input_id: row.input_id, content: row.content })))
      .toEqual([{ session_id: sessionId, input_id: held.input_id, content: prompt }]);
    expect(held.command_id).toBe(held.input_id);
    const inputId = held.input_id;
    const box = sandbox;
    await expect.poll(() => responseWindow(readInputAckGate(box), inputId).results.length, {
      timeout: 30_000, message: 'the original native input must reach a real Result while its receipt remains held',
    }).toBe(1);
    const beforeWindow = responseWindow(readInputAckGate(sandbox), inputId);
    expect(beforeWindow.input).not.toBeNull();
    expect(object(beforeWindow.results[0].message)).toMatchObject({ is_error: false, stop_reason: 'end_turn' });
    expect(Number(beforeWindow.results[0].store_sequence)).toBeGreaterThan(0);
    await expect.poll(() => nativeInputs(sessionId, prompt).length).toBe(1);
    const originalInput = nativeInputs(sessionId, prompt)[0];
    const originalAnswers = nativeRootRows(sessionId).filter((row) => row.seq > originalInput.seq && row.entry.type === 'assistant');
    const originalAnswer = originalAnswers.flatMap((row) => {
      const content = object(row.entry.message).content as Array<Record<string, unknown>>;
      return content.filter((block) => block.type === 'text').map((block) => String(block.text || ''));
    }).join('');
    expect(originalAnswer.trim()).not.toEqual('');
    const originalInputs = inputs(sessionId);
    const heldInput = sessionInputRows(sessionId).find((row) => row.input_id === inputId);
    expect(
      { state: heldInput?.state, delivered_at: heldInput?.delivered_at, response_id: heldInput?.response_id },
      'the platform must not have received the held delivery receipt',
    ).toEqual({ state: 'pending', delivered_at: null, response_id: null });
    expect(sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(inputId)),
      'no response is recorded for an input the platform has not seen taken').toEqual([]);
    expect(recipient.deliveries).toHaveLength(1);
    expect(readInputAckGate(sandbox).state).toBe('entered');
    const inboundBeforeStop = messageRecord(inbound[0]);
    evidence.beforeRestart = { sessionId, inputId, beforeWindow, originalInput, originalAnswers, originalAnswer,
      originalInputs, heldInput, inbound: inbound[0] };

    serverCommand(['stop', '--time', '10']);
    stopped = true;
    expect(serverCommand(['inspect', '--format', '{{.State.Running}}'])).toBe('false');
    signalInputAckGateRelease(sandbox);
    await restartServerContainer(absoluteBaseUrl(), 60_000, server);
    stopped = false;
    await expect.poll(() => recipient.deliveries.length, {
      timeout: 45_000, message: 'the recovered original turn must reach the real channel recipient',
    }).toBe(2);
    expect(recipient.errors).toEqual([]);
    let answered: SessionResponseRow | undefined;
    await expect.poll(() => {
      const rows = sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(inputId));
      answered = rows[0];
      return rows.map((row) => `${row.status}/${row.outcome ?? ''}`).join(',');
    }, { timeout: 30_000, message: 'exactly one response must answer the original input and complete' })
      .toBe('ended/completed');
    const settled = await api.waitForSessionReady(sessionId, 30_000);
    expect(settled.last_response).toMatchObject({ response_id: answered!.response_id, outcome: 'completed' });
    expect(settled.last_error ?? null).toBeNull();
    expect(sessionInputRows(sessionId).find((row) => row.input_id === inputId))
      .toMatchObject({ state: 'admitted', response_id: answered!.response_id });
    expect(nativeInputs(sessionId, prompt)).toEqual([originalInput]);
    expect(nativeRootRows(sessionId).filter((row) => row.seq > originalInput.seq && row.entry.type === 'assistant'))
      .toEqual(originalAnswers);
    expect(inputs(sessionId), 'recovery must not accept a second input').toEqual(originalInputs);
    const recoveredInbound = sessionChannelInboundRows(sessionId).find((row) => row.item_id === inbound[0].item_id);
    expect(recoveredInbound && messageRecord(recoveredInbound)).toEqual(inboundBeforeStop);
    expect(channelMessageInputs(inbound[0].item_id).map((row) => row.input_id)).toEqual([inputId]);
    const finalGate = readInputAckGate(sandbox);
    expect(finalGate.state).toBe('released');
    expect(finalGate.timed_out_at).toBeUndefined();
    expect(responseWindow(finalGate, inputId)).toEqual(beforeWindow);
    const outboxes = channelOutboxRows(sessionId);
    expect(outboxes).toHaveLength(2);
    const history = await api.getMessages(sessionId, 50);
    expect(history.has_more).toBe(false);
    expect(history.messages.filter((row) => row.role === 'user').map(messageText)).toEqual([baselinePrompt, prompt]);
    const replies = history.messages.filter((row) => row.role === 'assistant');
    expect(replies).toHaveLength(2);
    expect(answersInput(replies[1], inputId)).toBe(true);
    expect(replies[1].id).toBe(answered!.response_id);
    expect(outboxes.filter((row) => row.response_id === answered!.response_id)).toEqual([
      expect.objectContaining({ response_id: replies[1].id, state: 'DELIVERED' }),
    ]);
    expect(messageText(replies[1])).toBe(originalAnswer);
    expect(recipient.deliveries.map((row) => row.text)).toEqual(replies.map((row) => messageText(row).trim()));
    await openSessionView(page, sessionId);
    await expect(page.getByTestId('user-message')).toHaveText([baselinePrompt, prompt]);
    await expect(page.getByTestId('assistant-text')).toHaveText(replies.map(messageText));
    expect(recipient.deliveries).toHaveLength(2);
    evidence.recovered = { settled, answered, recoveredInbound, finalGate, outboxes, history };
  } finally {
    try {
      if (stopped) serverCommand(['start']);
    } finally {
      await recipient.close();
      if (sandbox && !evidence.recovered) {
        try { evidence.gateAtCleanup = readInputAckGate(sandbox); }
        catch (error) { evidence.gateAtCleanup = { unavailable: String(error) }; }
      }
      await test.info().attach('channel-completed-before-receipt-recovery', {
        body: JSON.stringify({ sessionId, ...evidence, deliveries: recipient.deliveries, errors: recipient.errors }),
        contentType: 'application/json',
      });
    }
  }
});
