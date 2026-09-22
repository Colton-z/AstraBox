/** A real channel turn finishes behind its held runner receipt, then survives backend loss. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { channelCallback } from '../fixtures/channelCallback';
import { documentsByField, sessionEvents } from '../fixtures/dbOracle';
import { absoluteBaseUrl, apiPath } from '../fixtures/env';
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
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId)
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
    const ready = await api.waitForSession(sessionId, (row) => row.state === 'READY' && !row.current_turn_id, 30_000);
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
    const accepted = sessionEvents(sessionId).filter((event) => event.event_type === 'command.accepted'
      && event.causation_id === held.command_id);
    expect(accepted).toHaveLength(1);
    expect(object(accepted[0].payload)).toMatchObject({ input_id: held.input_id, content: prompt });
    const turnId = String(accepted[0].turn_id || '');
    expect(turnId).not.toEqual('');
    const box = sandbox;
    await expect.poll(() => responseWindow(readInputAckGate(box), held.input_id).results.length, {
      timeout: 30_000, message: 'the original native input must reach a real Result while its receipt remains held',
    }).toBe(1);
    const beforeWindow = responseWindow(readInputAckGate(sandbox), held.input_id);
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
    const originalCommands = sessionEvents(sessionId).filter((event) => event.event_type === 'command.accepted');
    const receipts = sessionEvents(sessionId).filter((event) => ['input.delivered', 'input.consumed'].includes(String(event.event_type))
      && event.causation_id === held.command_id);
    expect(receipts, 'the platform must not have received the held delivery receipt').toEqual([]);
    expect(recipient.deliveries).toHaveLength(1);
    expect(readInputAckGate(sandbox).state).toBe('entered');
    evidence.beforeRestart = { sessionId, turnId, beforeWindow, originalInput, originalAnswers, originalAnswer, originalCommands, receipts };
    const inbound = documentsByField('channel_inbound', '$.session_id', sessionId)
      .filter((row) => object(row.payload).content === prompt);
    expect(inbound).toHaveLength(1);
    expect(inbound[0].state).toBe('DISPATCHING');
    expect(Number(inbound[0].lease_expires_epoch)).toBeGreaterThan(Date.now() / 1000);
    evidence.inboundBeforeStop = inbound[0];

    serverCommand(['stop', '--time', '10']);
    stopped = true;
    expect(serverCommand(['inspect', '--format', '{{.State.Running}}'])).toBe('false');
    const handedBack = documentsByField('channel_inbound', '$.session_id', sessionId)
      .find((row) => row._id === inbound[0]._id);
    evidence.inboundAfterStop = handedBack;
    expect(handedBack).toMatchObject({
      state: 'DISPATCHING', generation: inbound[0].generation,
      owner_token: inbound[0].owner_token, attempts: inbound[0].attempts,
    });
    expect(Number(handedBack!.lease_expires_epoch), 'stopped worker must hand back its durable task').toBeLessThanOrEqual(Date.now() / 1000);
    signalInputAckGateRelease(sandbox);
    await restartServerContainer(absoluteBaseUrl(), 60_000, server);
    stopped = false;
    await expect.poll(() => recipient.deliveries.length, {
      timeout: 45_000, message: 'the recovered original turn must reach the real channel recipient',
    }).toBe(2);
    expect(recipient.errors).toEqual([]);
    const settled = await api.waitForSession(sessionId, (row) => row.state === 'READY' && !row.current_turn_id, 30_000);
    expect(settled).toMatchObject({ last_turn_id: turnId, last_turn_status: 'COMPLETED' });
    expect(settled.last_error ?? null).toBeNull();
    expect(nativeInputs(sessionId, prompt)).toEqual([originalInput]);
    expect(nativeRootRows(sessionId).filter((row) => row.seq > originalInput.seq && row.entry.type === 'assistant'))
      .toEqual(originalAnswers);
    expect(sessionEvents(sessionId).filter((event) => event.event_type === 'command.accepted')).toEqual(originalCommands);
    const recoveredInbound = documentsByField('channel_inbound', '$.session_id', sessionId)
      .find((row) => row._id === inbound[0]._id);
    expect(recoveredInbound).toMatchObject({ state: 'SETTLED', attempts: inbound[0].attempts });
    expect(Number(recoveredInbound!.generation)).toBeGreaterThan(Number(inbound[0].generation));
    const finalGate = readInputAckGate(sandbox);
    expect(finalGate.state).toBe('released');
    expect(finalGate.timed_out_at).toBeUndefined();
    expect(responseWindow(finalGate, held.input_id)).toEqual(beforeWindow);
    const outboxes = documentsByField('channel_outbox', '$.session_id', sessionId);
    expect(outboxes).toHaveLength(2);
    expect(outboxes.filter((row) => row.turn_id === turnId)).toEqual([
      expect.objectContaining({ command_id: held.command_id, state: 'DELIVERED' }),
    ]);
    const history = await api.getMessages(sessionId, 50);
    expect(history.has_more).toBe(false);
    expect(history.messages.filter((row) => row.role === 'user').map(messageText)).toEqual([baselinePrompt, prompt]);
    const replies = history.messages.filter((row) => row.role === 'assistant');
    expect(replies).toHaveLength(2);
    expect(replies[1].turn_id).toBe(turnId);
    expect(messageText(replies[1])).toBe(originalAnswer);
    expect(recipient.deliveries.map((row) => row.text)).toEqual(replies.map((row) => messageText(row).trim()));
    await openSessionView(page, sessionId);
    await expect(page.getByTestId('user-message')).toHaveText([baselinePrompt, prompt]);
    await expect(page.getByTestId('assistant-text')).toHaveText(replies.map(messageText));
    expect(recipient.deliveries).toHaveLength(2);
    evidence.recovered = { settled, recoveredInbound, finalGate, outboxes, history };
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
