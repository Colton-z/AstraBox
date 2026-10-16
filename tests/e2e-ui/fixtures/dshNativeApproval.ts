/** Supplier approval audits identify the exact held command and its outcome. */
import { execFileSync } from 'node:child_process';
import { expect, type Page } from '@playwright/test';

import { messageToolCalls, type AstraApi, type PendingInteraction } from './astraApi';
import { sessionInputRows, sessionOutputRows, sessionResponseRows } from './dbOracle';
import { events, messageProseBlocks, textBlockValues } from './dshOutput';
import type { NativeEvent } from './dshOutput';
import { nativeRows } from './nativeChildLifecycle';

export interface DshApprovalEvidence {
  asked: NativeEvent;
  call: NativeEvent;
  input: NativeEvent;
  /** The platform's input the held response answers. */
  inputId: string;
  /** The held response, which is also its assistant message's id. */
  responseMessageId: string;
  nativeSessionId: string;
}

export function dshApprovalSource(sessionId: string): NativeEvent[] {
  return nativeRows(sessionId).flatMap((row) => events(JSON.parse(String(row.entry_json))))
    .sort((left, right) => left.seq - right.seq);
}

export async function dshPendingApproval(
  sessionId: string, pending: PendingInteraction, prompt: string, clientMessageId: string,
): Promise<DshApprovalEvidence> {
  const raw = pending.raw_input as Record<string, unknown>;
  await expect.poll(() => dshApprovalSource(sessionId).filter((event) => event.type === 'approval/asked'
    && event.data.callId === pending.tool_call_id)).toHaveLength(1);
  const source = dshApprovalSource(sessionId);
  const asked = source.find((event) => event.type === 'approval/asked' && event.data.callId === pending.tool_call_id)!;
  const calls = source.filter((event) => event.type === 'tool/call' && event.data.callId === asked.data.callId);
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.seq).toBeLessThan(asked.seq);
  expect(pending).toMatchObject({ presentation: 'tool_approval', tool_name: asked.data.toolName,
    tool_call_id: call.data.callId, prompt: asked.data.reason,
  });
  expect(raw).toMatchObject({ toolName: asked.data.toolName, callId: call.data.callId, reason: asked.data.reason });
  expect(call.data.name).toBe(asked.data.toolName);
  expect(String(asked.data.id || '')).not.toBe('');
  expect(String(asked.data.reason || '')).not.toBe('');
  const headers = nativeRows(sessionId).map((row) => JSON.parse(String(row.entry_json)) as Record<string, unknown>)
    .filter((entry) => entry.type === 'session');
  expect(headers).toHaveLength(1);
  const nativeSessionId = String(headers[0]!.id || '');
  expect(nativeSessionId).not.toBe('');
  expect(raw.sessionId).toBe(nativeSessionId);
  expect(source.filter((event) => event.type === 'turn/start')).toEqual([
    expect.objectContaining({ data: expect.objectContaining({ turn: call.data.turn }) }),
  ]);
  expect(source.filter((event) => event.type === 'turn/end')).toEqual([]);
  expect(source.filter((event) => event.type === 'approval/decided' && event.data.id === asked.data.id)).toEqual([]);
  expect(source.filter((event) => event.type === 'tool/result'
    && (event.data.message as Record<string, unknown>).toolCallId === call.data.callId)).toEqual([]);
  const inputs = source.filter((event) => event.type === 'user/message'
    && (event.data.source as Record<string, unknown> | undefined)?.kind === 'user');
  expect(inputs).toHaveLength(1);
  const input = inputs[0]!;
  expect(textBlockValues(input.data.content)).toEqual([prompt]);
  expect(input.seq).toBeLessThan(call.seq);
  expect(String((input.data.source as Record<string, unknown>).rpcId || '')).not.toBe('');
  // The platform's records: the person's one message, taken by the runtime
  // into the response the interaction pauses.
  const responseMessageId = String(pending.response_id || '');
  expect(responseMessageId, 'the interaction names the response it pauses').not.toBe('');
  const accepted = sessionInputRows(sessionId).filter((row) => row.client_message_id === clientMessageId);
  expect(accepted).toEqual([expect.objectContaining({ content: prompt, state: 'admitted', response_id: responseMessageId })]);
  const inputId = accepted[0]!.input_id;
  const responses = sessionResponseRows(sessionId).filter((row) => row.response_id === responseMessageId);
  expect(responses).toEqual([expect.objectContaining({ status: 'waiting', outcome: null, input_ids: [inputId] })]);
  return { asked, call, input, inputId, responseMessageId, nativeSessionId };
}

export async function dshApprovalPanel(page: Page, pending: PendingInteraction): Promise<void> {
  const panel = page.getByTestId('pending-interaction-panel');
  await expect(panel).toBeVisible();
  const disclosure = panel.getByRole('button', { name: /View tool input|查看工具输入/ });
  await disclosure.click();
  await expect(panel.locator('pre')).toBeVisible();
  expect(JSON.parse(await panel.locator('pre').innerText())).toEqual(pending.raw_input);
}

/** Decode saved JSON or Python literals without executing supplier content. */
export function savedOutput(content: unknown): unknown {
  const decoded = execFileSync('python3', ['-c', [
    'import ast, json, sys', 'value = sys.stdin.read()', 'try:', '    decoded = json.loads(value)',
    'except json.JSONDecodeError:', '    decoded = ast.literal_eval(value)',
    'print(json.dumps(decoded))',
  ].join('\n')], { input: String(content), encoding: 'utf8', timeout: 5_000, maxBuffer: 8 * 1024 * 1024 });
  return JSON.parse(decoded);
}

export async function dshApprovalCompleted(
  api: AstraApi, sessionId: string, pending: PendingInteraction, held: DshApprovalEvidence,
  outcome: 'allowed-once' | 'rejected',
): Promise<Record<string, unknown>> {
  const responseId = String(pending.response_id);
  expect(responseId).toBe(held.responseMessageId);
  await expect.poll(() => dshApprovalSource(sessionId).filter((event) => event.type === 'turn/end')).toHaveLength(1);
  const source = dshApprovalSource(sessionId);
  expect(source.filter((event) => event.type === 'approval/asked')).toEqual([held.asked]);
  const decisions = source.filter((event) => event.type === 'approval/decided');
  expect(decisions).toEqual([expect.objectContaining({ data: { id: held.asked.data.id, outcome } })]);
  expect(decisions[0]!.seq).toBeGreaterThan(held.asked.seq);
  const results = source.filter((event) => event.type === 'tool/result'
    && (event.data.message as Record<string, unknown>).toolCallId === held.call.data.callId);
  expect(results).toHaveLength(1);
  const result = results[0]!;
  const tool = result.data.message as Record<string, unknown>;
  expect(result.seq).toBeGreaterThan(decisions[0]!.seq);
  expect(result.data.turn).toBe(held.call.data.turn);
  expect(tool).toMatchObject({ role: 'tool', toolCallId: held.call.data.callId, isError: outcome === 'rejected' });
  const terminal = source.find((event) => event.type === 'turn/end')!;
  expect(terminal.data).toMatchObject({ turn: held.call.data.turn, reason: { kind: 'completed' } });
  expect(terminal.seq).toBeGreaterThan(result.seq);
  expect(source.filter((event) => event.type === 'turn/start')).toHaveLength(1);
  expect(source.filter((event) => event.type === 'user/message'
    && (event.data.source as Record<string, unknown> | undefined)?.kind === 'user')).toEqual([held.input]);
  // The platform's records of the same response: the input it answered, its
  // ending with the supplier's own last usage, and the parts it relayed.
  const answered = sessionInputRows(sessionId).filter((row) => row.input_id === held.inputId);
  expect(answered).toEqual([expect.objectContaining({ state: 'admitted', response_id: responseId })]);
  const modelMessages = source.filter((event) => event.type === 'assistant/message');
  const usage = modelMessages.at(-1)!.data.usage;
  expect(usage).toEqual(expect.any(Object));
  const responses = sessionResponseRows(sessionId).filter((row) => row.response_id === responseId);
  expect(responses).toEqual([expect.objectContaining({
    status: 'ended', outcome: 'completed', native_reason: 'completed', input_ids: [held.inputId], usage,
  })]);
  const frames = sessionOutputRows(sessionId, responseId).map((row) => row.part);
  const outputs = frames.filter((frame) => frame.type === 'tool-output-available' && frame.toolCallId === held.call.data.callId);
  expect(outputs).toEqual([expect.objectContaining({ output: { content: tool.content, isError: outcome === 'rejected' } })]);
  expect(frames.filter((frame) => frame.type === 'error')).toEqual([]);
  expect(frames.filter((frame) => frame.type === 'finish').at(-1)).toMatchObject({ finishReason: 'stop' });
  const history = await api.getMessages(sessionId, 50);
  const messages = history.messages.filter((message) => message.role === 'assistant' && message.id === responseId);
  expect(messages).toHaveLength(1);
  expect(messageProseBlocks(messages[0]!)).toEqual(modelMessages
    .flatMap((event) => textBlockValues((event.data.message as Record<string, unknown>).content)));
  const toolCalls = messageToolCalls(messages[0]!);
  const calls = source.filter((event) => event.type === 'tool/call');
  expect(toolCalls.map((toolCall) => toolCall.toolCallId)).toEqual(calls.map((event) => event.data.callId));
  for (const nativeCall of calls) {
    const nativeResults = source.filter((event) => event.type === 'tool/result'
      && (event.data.message as Record<string, unknown>).toolCallId === nativeCall.data.callId);
    expect(nativeResults).toHaveLength(1);
    const nativeTool = nativeResults[0]!.data.message as Record<string, unknown>;
    const output = { content: nativeTool.content, isError: nativeTool.isError ?? false };
    expect(frames.filter((frame) => frame.type === 'tool-input-available' && frame.toolCallId === nativeCall.data.callId))
      .toEqual([expect.objectContaining({ toolName: nativeCall.data.name,
        input: JSON.parse(String(nativeCall.data.arguments)) })]);
    expect(frames.filter((frame) => frame.type === 'tool-output-available' && frame.toolCallId === nativeCall.data.callId))
      .toEqual([expect.objectContaining({ output })]);
    // History holds the call once, with the input the supplier recorded and
    // the output it returned.
    expect(toolCalls.filter((toolCall) => toolCall.toolCallId === nativeCall.data.callId)).toEqual([
      expect.objectContaining({
        toolName: nativeCall.data.name,
        input: JSON.parse(String(nativeCall.data.arguments)),
        state: 'output-available',
        output,
      }),
    ]);
  }
  return { source, frames, history, held, result };
}
