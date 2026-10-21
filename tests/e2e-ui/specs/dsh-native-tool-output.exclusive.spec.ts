/** Native tool messages and step boundaries survive platform projection and reload. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import {
  AstraApi, answersInput, messageParts, messageText, messageToolCalls, type MessageRecord,
} from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { sessionInputRows, sessionOutputRows, sessionResponseRows } from '../fixtures/dbOracle';
import { events, messageProseBlocks, textBlockValues, textBlocks } from '../fixtures/dshOutput';
import type { NativeEvent } from '../fixtures/dshOutput';
import { engineProfileFor } from '../fixtures/engineProfile';
import { nativeRows } from '../fixtures/nativeChildLifecycle';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

/**
 * The usage the harness last reported for one turn.
 *
 * Each model attempt settles as `assistant/message` or `assistant/attempt`
 * with that step's usage; the turn's usage is the last one reported, not a sum
 * the supplier never gave.
 */
function nativeTurnUsage(source: NativeEvent[], nativeTurn: unknown): unknown {
  const settled = source.filter((event) => (event.type === 'assistant/message' || event.type === 'assistant/attempt')
    && event.data.turn === nativeTurn && event.data.usage && typeof event.data.usage === 'object');
  return settled.at(-1)?.data.usage;
}

/** The input the page sent with `clientMessageId`, as the platform recorded it. */
function pageInputId(sessionId: string, clientMessageId: string): string {
  const rows = sessionInputRows(sessionId).filter((row) => row.client_message_id === clientMessageId);
  expect(rows, 'the page message is one accepted input').toHaveLength(1);
  return rows[0]!.input_id;
}

/** The ended reply to the page's message; how it ended is asserted by the caller. */
async function replyToPage(api: AstraApi, sessionId: string, clientMessageId: string): Promise<MessageRecord> {
  return api.waitForReplyTo(sessionId, pageInputId(sessionId, clientMessageId));
}

function expectNativeDelivery(
  sessionId: string, answer: MessageRecord, prompt: string, clientMessageId: string, source: NativeEvent[],
): { terminal: NativeEvent; inputId: string; nativeTurn: unknown } {
  // The platform's record: the person's one message, taken by the runtime into
  // the response history shows for it.
  const accepted = sessionInputRows(sessionId).filter((row) => row.client_message_id === clientMessageId);
  expect(accepted).toEqual([expect.objectContaining({ content: prompt, state: 'admitted', response_id: answer.id })]);
  const inputId = accepted[0]!.input_id;
  expect(answersInput(answer, inputId), 'the reply answers exactly this input').toBe(true);
  expect(answer.metadata?.input_ids).toEqual([inputId]);

  const inputs = source.filter((event) => event.type === 'user/message'
    && (event.data.source as Record<string, unknown> | undefined)?.kind === 'user'
    && textBlocks(event.data.content) === prompt);
  expect(inputs).toHaveLength(1);
  const input = inputs[0]!;
  const rpcId = String((input.data.source as Record<string, unknown>).rpcId || '');
  expect(rpcId).not.toBe('');
  // UserMessage has no turn field in the supplier schema. Its position
  // inside the native start/end pair identifies the consuming turn.
  const opened = source.filter((event) => event.type === 'turn/start' && event.seq < input.seq);
  expect(opened).toHaveLength(1);
  const nativeTurn = opened[0]!.data.turn;
  expect(nativeTurn).toEqual(expect.any(Number));
  const terminals = source.filter((event) => event.type === 'turn/end' && event.data.turn === nativeTurn);
  expect(terminals).toHaveLength(1);
  expect(opened[0]!.seq).toBeLessThan(input.seq);
  const terminal = terminals[0]!;
  expect(terminal.seq).toBeGreaterThan(input.seq);
  expect(terminal.data.reason).toEqual({ kind: 'completed' });

  const frames = sessionOutputRows(sessionId, answer.id).map((row) => row.part);
  // The response names its input from its first part, although the native
  // turn records the message only once its first step has started.
  expect(frames[0]).toMatchObject({ type: 'start', messageId: answer.id,
    messageMetadata: { response_id: answer.id, input_ids: [inputId], origin: 'input' } });
  const stepIndex = frames.findIndex((frame) => frame.type === 'start-step');
  const textIndex = frames.findIndex((frame) => frame.type === 'text-delta');
  expect(stepIndex).toBeGreaterThan(0);
  expect(textIndex).toBeGreaterThan(stepIndex);
  const stepsBeforeEcho = source.filter((event) => event.type === 'step/start'
    && event.data.turn === nativeTurn && event.seq < input.seq);
  expect(stepsBeforeEcho, 'this real turn must exercise the native pre-echo step').toHaveLength(1);
  expect(frames.filter((frame) => frame.type === 'start-step')).toHaveLength(
    source.filter((event) => event.type === 'step/start' && event.data.turn === nativeTurn).length,
  );
  expect(frames.filter((frame) => frame.type === 'finish')).toEqual([
    expect.objectContaining({ finishReason: 'stop' }),
  ]);
  // Nothing of this response is written after its end.
  expect(frames.at(-1)).toMatchObject({ type: 'finish', finishReason: 'stop' });
  expect(frames.filter((frame) => frame.type === 'data-raw-event' || frame.type === 'error')).toEqual([]);
  expect(messageParts(answer, 'data-raw-event')).toEqual([]);
  // The platform's record of the ending carries the supplier's own last usage.
  const usage = nativeTurnUsage(source, nativeTurn);
  expect(usage).toEqual(expect.any(Object));
  expect(sessionResponseRows(sessionId).filter((row) => row.response_id === answer.id)).toEqual([
    expect.objectContaining({ origin: 'input', status: 'ended', outcome: 'completed', native_reason: 'completed',
      input_ids: [inputId], usage }),
  ]);
  expect(answer.metadata).toMatchObject({ origin: 'input', status: 'ended', outcome: 'completed' });
  return { terminal, inputId, nativeTurn };
}

test('DSH retains native tool results and step boundaries across reload', async ({ request, page }) => {
  const api = new AstraApi(request);
  const profile = engineProfileFor('deepseek_harness');
  agentId = (await api.createAgent({ name: `__e2e_dsh_tools_${randomUUID()}`,
    runtime_profile_name: profile.runtime_profile_name, model: profile.model, prepared_capacity: 0 })).agent_id;
  const sessionId = (await api.startConversation(agentId)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  const detail = await api.adminSessionDetail(sessionId);
  const workspace = String(detail.runtime_identity?.workspace_dir || '').replace(/\/+$/, '');
  expect(workspace).toMatch(/^\//);
  const file = `research-${randomUUID()}.txt`;
  const marker = `RESEARCH_${randomUUID()}`;
  const note = `${marker}\nRevenue rose from 120 to 150, an increase of 25%.`;
  await api.uploadFileText(sessionId, workspace, file, note);
  await openSessionView(page, sessionId);
  const prompt = `Use bash once to run cat ${workspace}/${file}. Quote the entire research note verbatim in your reply. `
    + 'Do not use any other tools or delegate.';
  const delivery = await sendPrompt(page, sessionId, prompt);
  const clientMessageId = String(delivery.request().postDataJSON().client_message_id || '');
  expect(clientMessageId).not.toBe('');
  const answer = await replyToPage(api, sessionId, clientMessageId);
  expect(messageText(answer)).toContain(marker);
  await api.waitForSessionReady(sessionId);

  const native = () => nativeRows(sessionId).flatMap((row) => events(JSON.parse(String(row.entry_json))))
    .sort((a, b) => a.seq - b.seq);
  await expect.poll(() => native().filter((event) => event.type === 'turn/end')).toHaveLength(1);
  const source = native();
  const headers = nativeRows(sessionId).map((row) => JSON.parse(String(row.entry_json)) as Record<string, unknown>)
    .filter((entry) => entry.type === 'session');
  expect(headers).toHaveLength(1);
  const nativeSessionId = String(headers[0]!.id || '');
  expect(nativeSessionId).not.toBe('');
  const calls = source.filter((event) => event.type === 'tool/call');
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.data.name).toBe('bash');
  expect(JSON.parse(String(call.data.arguments)).command).toContain(`${workspace}/${file}`);
  const results = source.filter((event) => event.type === 'tool/result')
    .map((event) => event.data.message as Record<string, unknown>);
  expect(results).toHaveLength(1);
  const result = results[0]!;
  expect(result.role).toBe('tool');
  expect(result.toolCallId).toBe(call.data.callId);
  expect(result.isError ?? false).toBe(false);
  expect(textBlocks(result.content)).toContain(note);

  const frames = sessionOutputRows(sessionId, answer.id).map((row) => row.part);
  expect(frames.filter((frame) => frame.type === 'tool-input-start')).toEqual([
    expect.objectContaining({ toolCallId: call.data.callId, toolName: 'bash', dynamic: true }),
  ]);
  expect(frames.filter((frame) => frame.type === 'tool-input-available')).toEqual([
    expect.objectContaining({ toolCallId: call.data.callId, toolName: 'bash',
      input: JSON.parse(String(call.data.arguments)), dynamic: true }),
  ]);
  expect(frames.filter((frame) => frame.type === 'tool-output-available')).toEqual([
    expect.objectContaining({ toolCallId: result.toolCallId,
      output: { content: result.content, isError: false } }),
  ]);
  const steps = source.filter((event) => event.type === 'step/start' || event.type === 'step/end');
  expect(steps.filter((event) => event.type === 'step/start').length).toBeGreaterThanOrEqual(2);
  expect(frames.filter((frame) => frame.type === 'start-step' || frame.type === 'finish-step')
    .map((frame) => frame.type)).toEqual(steps.map((event) => event.type === 'step/start' ? 'start-step' : 'finish-step'));
  expect(source.find((event) => event.type === 'turn/end')!.data.reason).toEqual({ kind: 'completed' });
  const messages = source.filter((event) => event.type === 'assistant/message');
  const expectedText = messages
    .flatMap((event) => textBlockValues((event.data.message as Record<string, unknown>).content));
  expect(expectedText.join('\n')).toContain(marker);
  const retained = (await api.getMessages(sessionId)).messages.find((message) => message.id === answer.id)!;
  expect(messageProseBlocks(retained)).toEqual(expectedText);
  const firstDelivery = expectNativeDelivery(sessionId, retained, prompt, clientMessageId, source);
  // History holds the call once, with the input the supplier recorded and the
  // output it returned.
  expect(messageToolCalls(retained)).toEqual([expect.objectContaining({
    toolCallId: call.data.callId, toolName: 'bash', input: JSON.parse(String(call.data.arguments)),
    state: 'output-available', output: { content: result.content, isError: false },
  })]);

  const nativeBlocks = messages.flatMap((event) =>
    (event.data.message as { content: Record<string, unknown>[] }).content);
  const reasoning = nativeBlocks.filter((block) => block.type === 'reasoning');
  expect(reasoning.length, 'the native thinking route must exercise reasoning custody').toBeGreaterThan(0);
  expect(reasoning.some((block) => typeof block.text === 'string' && block.text.length > 0)).toBe(true);
  await test.info().attach('native-output-coverage', {
    body: JSON.stringify({ sessionId, responseId: answer.id, nativeReasoningBlocks: reasoning.length,
      nativeToolCalls: calls.length, nativeSteps: steps.filter((event) => event.type === 'step/start').length }),
    contentType: 'application/json',
  });
  for (const kind of ['text', 'reasoning']) {
    const opened = frames.filter((frame) => frame.type === `${kind}-start`).map((frame) => frame.id);
    const closed = frames.filter((frame) => frame.type === `${kind}-end`).map((frame) => frame.id);
    expect(opened).toEqual(closed);
    expect(new Set(opened).size).toBe(opened.length);
    const text = opened.map((id) => frames.filter((frame) => frame.type === `${kind}-delta` && frame.id === id)
      .map((frame) => frame.delta).join(''));
    expect(text).toEqual(nativeBlocks.filter((block) => block.type === kind).map((block) => block.text));
    // History folds the same parts the stream carried: every native block, in
    // order and verbatim, including an empty one.
    expect(messageParts(retained, kind).map((part) => part.text)).toEqual(text);
  }
  let stepOpen = false;
  const blocks = new Set<unknown>();
  for (const frame of frames) {
    if (frame.type === 'start-step') {
      expect(stepOpen).toBe(false);
      stepOpen = true;
    } else if (frame.type === 'finish-step') {
      expect(stepOpen).toBe(true);
      expect(blocks.size).toBe(0);
      stepOpen = false;
    } else if (frame.type === 'text-start' || frame.type === 'reasoning-start') {
      expect(stepOpen).toBe(true);
      blocks.add(frame.id);
    } else if (frame.type === 'text-end' || frame.type === 'reasoning-end') {
      expect(blocks.delete(frame.id)).toBe(true);
    }
  }
  expect(stepOpen).toBe(false);
  expect(blocks.size).toBe(0);

  for (const reload of [false, true]) {
    if (reload) await page.reload();
    const reply = page.locator(`[data-message-id="${answer.id}"]`);
    await expect(reply).toContainText(marker);
    await expect(reply.getByTestId('assistant-turn-process')).toBeAttached();
    await revealAssistantProcess(page, { within: reply });
    const card = reply.locator(`[data-tool-call-id="${String(call.data.callId)}"]`);
    await expect(card).toBeVisible();
    const trigger = card.locator('button[aria-expanded]').first();
    if (await trigger.getAttribute('aria-expanded') !== 'true') await trigger.click();
    await expect(card).toContainText(marker);
  }

  // The response ends at the native turn's terminal. Only a second delivery
  // can prove the conversation takes its next message after that boundary,
  // as a response of its own.
  const nextReceipt = `NEXT_${randomUUID()}`;
  const nextPrompt = `Reply exactly ${nextReceipt}. Do not use tools or delegate.`;
  const nextDelivery = await sendPrompt(page, sessionId, nextPrompt);
  const nextClientMessageId = String(nextDelivery.request().postDataJSON().client_message_id || '');
  expect(nextClientMessageId).not.toBe('');
  const nextAnswer = await replyToPage(api, sessionId, nextClientMessageId);
  expect(messageText(nextAnswer)).toBe(nextReceipt);
  await api.waitForSessionReady(sessionId);
  const laterNative = () => native().filter((event) => event.seq > firstDelivery.terminal.seq);
  await expect.poll(() => laterNative().filter((event) => event.type === 'turn/end')).toHaveLength(1);
  const nextRetained = (await api.getMessages(sessionId)).messages
    .find((message) => message.id === nextAnswer.id)!;
  const secondDelivery = expectNativeDelivery(sessionId, nextRetained, nextPrompt, nextClientMessageId, laterNative());
  expect(secondDelivery.inputId).not.toBe(firstDelivery.inputId);
  expect(nextRetained.id).not.toBe(retained.id);
  expect(secondDelivery.nativeTurn).not.toBe(firstDelivery.nativeTurn);
  const nextNativeBlocks = laterNative().filter((event) => event.type === 'assistant/message')
    .flatMap((event) => textBlockValues((event.data.message as Record<string, unknown>).content));
  expect(nextNativeBlocks).toEqual([nextReceipt]);
  expect(messageProseBlocks(nextRetained)).toEqual(nextNativeBlocks);
  expect(laterNative().filter((event) => event.type === 'tool/call')).toEqual([]);
  const finalHistory = (await api.getMessages(sessionId)).messages;
  expect(finalHistory.map((message) => ({ role: message.role, id: message.id }))).toEqual([
    { role: 'user', id: clientMessageId },
    { role: 'assistant', id: retained.id },
    { role: 'user', id: nextClientMessageId },
    { role: 'assistant', id: nextRetained.id },
  ]);
  expect(finalHistory.find((message) => message.id === retained.id)!.parts).toEqual(retained.parts);
  expect(nativeRows(sessionId).map((row) => JSON.parse(String(row.entry_json)) as Record<string, unknown>)
    .filter((entry) => entry.type === 'session').map((entry) => entry.id),
  'the next message is answered by the same native Session').toEqual([nativeSessionId]);
  for (const reload of [false, true]) {
    if (reload) await page.reload();
    const nextReply = page.locator(`[data-message-id="${nextRetained.id}"]`);
    await expect(nextReply.getByTestId('assistant-text')).toHaveText(nextReceipt);
    await expect(page.locator(`[data-message-id="${retained.id}"]`)).toContainText(marker);
  }
  await test.info().attach('dsh-native-delivery-scene', {
    body: JSON.stringify({ sessionId, nativeSessionId, clientMessageId, nextClientMessageId,
      firstDelivery, secondDelivery, native: native(), history: finalHistory,
      firstFrames: sessionOutputRows(sessionId, retained.id), nextFrames: sessionOutputRows(sessionId, nextRetained.id),
      inputs: sessionInputRows(sessionId), responses: sessionResponseRows(sessionId) }),
    contentType: 'application/json',
  });
});
