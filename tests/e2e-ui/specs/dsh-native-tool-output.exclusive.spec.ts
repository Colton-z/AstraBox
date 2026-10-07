/** Native tool messages and step boundaries survive platform projection and reload. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { framesForTurn, sessionEvents } from '../fixtures/dbOracle';
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

function expectNativeDelivery(
  sessionId: string, answer: { turn_id: string; message_id: string; blocks?: Record<string, unknown>[] },
  prompt: string, clientMessageId: string, source: NativeEvent[], nativeSessionId: string,
): { terminal: NativeEvent; inputId: string; nativeTurn: unknown } {
  const journal = sessionEvents(sessionId);
  const accepted = journal.filter((event) => event.event_type === 'command.accepted'
    && (event.payload as Record<string, unknown>).client_message_id === clientMessageId
    && (event.payload as Record<string, unknown>).content === prompt);
  expect(accepted).toHaveLength(1);
  const inputId = String((accepted[0]!.payload as Record<string, unknown>).input_id || '');
  expect(inputId).not.toBe('');
  expect(accepted[0]!.turn_id).toBe(answer.turn_id);
  const consumed = journal.filter((event) => event.event_type === 'input.consumed'
    && (event.payload as Record<string, unknown>).input_id === inputId);
  expect(consumed).toEqual([expect.objectContaining({
    turn_id: answer.turn_id,
    payload: expect.objectContaining({ input_id: inputId, client_message_id: clientMessageId,
      content: prompt, response_message_id: answer.message_id }),
  })]);
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

  const frames = framesForTurn(answer.turn_id).map((row) => row.payload as Record<string, unknown>);
  const boundaries = frames.filter((frame) => frame.type === 'data-input-consumed');
  expect(boundaries).toEqual([expect.objectContaining({ data: expect.objectContaining({
    inputId, responseMessageId: answer.message_id, content: prompt,
  }) })]);
  const consumptionIndex = frames.indexOf(boundaries[0]!);
  const stepIndex = frames.findIndex((frame) => frame.type === 'start-step');
  const textIndex = frames.findIndex((frame) => frame.type === 'text-delta');
  expect(stepIndex).toBeGreaterThan(consumptionIndex);
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
  expect(framesForTurn(answer.turn_id).filter((row) => (row.payload as Record<string, unknown>).type === 'data-result')).toEqual([
    expect.objectContaining({
      engine_kind: 'deepseek_harness', engine_turn_id: rpcId,
      engine_output_cursor: expect.objectContaining({ sessionId: nativeSessionId, seq: terminal.seq }),
      payload: expect.objectContaining({ data: expect.objectContaining({ finish_reason: 'stop' }) }),
    }),
  ]);
  expect(frames.filter((frame) => frame.type === 'data-raw-event' || frame.type === 'error')).toEqual([]);
  expect((answer.blocks ?? []).filter((block) => block.type === 'raw_event')).toEqual([]);
  return { terminal, inputId, nativeTurn };
}

test('DSH retains native tool results and step boundaries across reload', async ({ request, page }) => {
  const api = new AstraApi(request);
  const profile = engineProfileFor('deepseek_harness');
  agentId = (await api.createAgent({ name: `__e2e_dsh_tools_${randomUUID()}`,
    environment_name: profile.environment_name, model: profile.model, prepared_capacity: 0 })).agent_id;
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
  const answer = await api.waitForAssistantMessageMatching(sessionId, 0,
    (message) => messageText(message).includes(marker));
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

  const frames = framesForTurn(answer.turn_id).map((row) => row.payload as Record<string, unknown>);
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
  expect(frames.filter((frame) => frame.type === 'finish')).toEqual([
    expect.objectContaining({ finishReason: 'stop' }),
  ]);
  const terminalIndex = frames.findIndex((frame) => frame.type === 'finish');
  // Title generation follows the reply terminal in the Session journal.
  // Only that metadata notification may follow; assistant output must end.
  for (const frame of frames.slice(terminalIndex + 1)) {
    expect(frame).toEqual({ type: 'data-session-changed', transient: true, data: { sessionId } });
  }
  const messages = source.filter((event) => event.type === 'assistant/message');
  const expectedText = messages
    .flatMap((event) => textBlockValues((event.data.message as Record<string, unknown>).content));
  expect(expectedText.join('\n')).toContain(marker);
  const retained = (await api.getMessages(sessionId)).messages.find((message) => message.message_id === answer.message_id)!;
  expect(messageProseBlocks(retained)).toEqual(expectedText);
  const firstDelivery = expectNativeDelivery(sessionId, retained, prompt, clientMessageId, source, nativeSessionId);
  const usage = messages.at(-1)!.data.usage;
  expect(usage).toEqual(expect.any(Object));
  expect(frames.filter((frame) => frame.type === 'data-result')).toEqual([
    expect.objectContaining({ data: expect.objectContaining({ usage }) }),
  ]);
  const completed = sessionEvents(sessionId).filter((event) => event.turn_id === answer.turn_id
    && event.event_type === 'turn.completed');
  expect(completed).toHaveLength(1);
  const completedBlocks = (completed[0]!.payload as { blocks: Record<string, unknown>[] }).blocks;
  expect(completedBlocks.filter((block) => block.type === 'result')).toEqual([
    expect.objectContaining({ usage }),
  ]);
  expect((retained.blocks ?? []).filter((block) => block.type === 'result')).toEqual([
    expect.objectContaining({ usage }),
  ]);

  const nativeBlocks = messages.flatMap((event) =>
    (event.data.message as { content: Record<string, unknown>[] }).content);
  const reasoning = nativeBlocks.filter((block) => block.type === 'reasoning');
  expect(reasoning.length, 'the native thinking route must exercise reasoning custody').toBeGreaterThan(0);
  expect(reasoning.some((block) => typeof block.text === 'string' && block.text.length > 0)).toBe(true);
  await test.info().attach('native-output-coverage', {
    body: JSON.stringify({ sessionId, turnId: answer.turn_id, nativeReasoningBlocks: reasoning.length,
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
    const storedKind = kind === 'reasoning' ? 'thinking' : 'text';
    // Empty native blocks still have stream boundaries, but contribute no
    // prose to completed history. Every non-empty block must survive verbatim.
    expect((retained.blocks ?? []).filter((block) => block.type === storedKind).map((block) => block[storedKind]))
      .toEqual(text.filter((value) => value !== ''));
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
    const reply = page.locator(`[data-message-id="${answer.message_id}"]`);
    await expect(reply).toContainText(marker);
    await expect(reply.getByTestId(/^assistant-(turn-)?process$/).first()).toBeAttached();
    await revealAssistantProcess(page, { within: reply });
    const card = reply.locator(`[data-tool-call-id="${String(call.data.callId)}"]`);
    await expect(card).toBeVisible();
    const trigger = card.locator('button[aria-expanded]').first();
    if (await trigger.getAttribute('aria-expanded') !== 'true') await trigger.click();
    await expect(card).toContainText(marker);
  }

  // The real turn worker stops reading at the native terminal, rather than
  // draining the engine generator. Only a second delivery can prove the
  // adapter released its turn slot at that boundary.
  const nextReceipt = `NEXT_${randomUUID()}`;
  const nextPrompt = `Reply exactly ${nextReceipt}. Do not use tools or delegate.`;
  const nextDelivery = await sendPrompt(page, sessionId, nextPrompt);
  const nextClientMessageId = String(nextDelivery.request().postDataJSON().client_message_id || '');
  expect(nextClientMessageId).not.toBe('');
  const nextAnswer = await api.waitForAssistantMessageMatching(sessionId, 0,
    (message) => messageText(message) === nextReceipt);
  await api.waitForSessionReady(sessionId);
  const laterNative = () => native().filter((event) => event.seq > firstDelivery.terminal.seq);
  await expect.poll(() => laterNative().filter((event) => event.type === 'turn/end')).toHaveLength(1);
  const nextRetained = (await api.getMessages(sessionId)).messages
    .find((message) => message.message_id === nextAnswer.message_id)!;
  const secondDelivery = expectNativeDelivery(sessionId, nextRetained, nextPrompt,
    nextClientMessageId, laterNative(), nativeSessionId);
  expect(secondDelivery.inputId).not.toBe(firstDelivery.inputId);
  expect(nextRetained.turn_id).not.toBe(retained.turn_id);
  expect(nextRetained.message_id).not.toBe(retained.message_id);
  expect(secondDelivery.nativeTurn).not.toBe(firstDelivery.nativeTurn);
  const nextNativeBlocks = laterNative().filter((event) => event.type === 'assistant/message')
    .flatMap((event) => textBlockValues((event.data.message as Record<string, unknown>).content));
  expect(nextNativeBlocks).toEqual([nextReceipt]);
  expect(messageProseBlocks(nextRetained)).toEqual(nextNativeBlocks);
  expect(laterNative().filter((event) => event.type === 'tool/call')).toEqual([]);
  const finalHistory = (await api.getMessages(sessionId)).messages;
  expect(finalHistory.map((message) => ({ role: message.role, id: message.message_id }))).toEqual([
    { role: 'user', id: `${firstDelivery.inputId}:user` },
    { role: 'assistant', id: retained.message_id },
    { role: 'user', id: `${secondDelivery.inputId}:user` },
    { role: 'assistant', id: nextRetained.message_id },
  ]);
  expect(finalHistory.find((message) => message.message_id === retained.message_id)!.blocks).toEqual(retained.blocks);
  for (const reload of [false, true]) {
    if (reload) await page.reload();
    const nextReply = page.locator(`[data-message-id="${nextRetained.message_id}"]`);
    await expect(nextReply.getByTestId('assistant-text')).toHaveText(nextReceipt);
    await expect(page.locator(`[data-message-id="${retained.message_id}"]`)).toContainText(marker);
  }
  await test.info().attach('dsh-native-delivery-scene', {
    body: JSON.stringify({ sessionId, nativeSessionId, clientMessageId, nextClientMessageId,
      firstDelivery, secondDelivery, native: native(), history: finalHistory,
      firstFrames: framesForTurn(retained.turn_id), nextFrames: framesForTurn(nextRetained.turn_id),
      journal: sessionEvents(sessionId) }),
    contentType: 'application/json',
  });
});
