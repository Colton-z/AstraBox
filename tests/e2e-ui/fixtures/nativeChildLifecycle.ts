/** Native launch evidence and a real, fixture-released child workload. */
import { expect } from '@playwright/test';

import { nativeTranscriptRows, type NativeTranscriptRow } from './dbOracle';
import type { EngineProfile } from './engineProfile';

export type ChildMode = 'foreground' | 'background';

export interface ChildGate {
  marker: string;
  started: string;
  release: string;
  completed: string;
}

/** The session's runtime transcript entries; see `nativeTranscriptRows`. */
export function nativeRows(sessionId: string): NativeTranscriptRow[] {
  return nativeTranscriptRows(sessionId);
}

interface NativeCall {
  id: string;
  name: string;
  input: Record<string, unknown>;
  subpath: unknown;
}

export interface NativeChildTool extends NativeCall {
  result?: { output: unknown; isError: boolean };
  commandArguments?: Record<string, unknown>;
  nativeThreadId?: string;
  nativeSessionCwd?: string;
  completedCommand?: Record<string, unknown>;
  recordedFunctionOutput?: string;
}

function objects(value: unknown): Record<string, unknown>[] {
  if (Array.isArray(value)) return value.flatMap(objects);
  if (!value || typeof value !== 'object') return [];
  const object = value as Record<string, unknown>;
  return [object, ...Object.values(object).flatMap(objects)];
}

/** Supplier history is the oracle, independently of child API normalization. */
export function childToolEvidence(
  sessionId: string, profile: EngineProfile, gate: ChildGate, parentSubpath: unknown,
): NativeChildTool[] {
  const stored = nativeRows(sessionId)
    .map((row) => ({ subpath: row.subpath, entry: JSON.parse(String(row.entry_json)) as Record<string, unknown> }));
  const rows = stored.filter((row) => row.subpath !== parentSubpath)
    // Pi's artifact mirror duplicates native messages and redacts inputs. Read
    // the actual SessionStore, not that inspection artifact's bounded copy.
    .filter((row) => profile.engine_kind !== 'pi' || row.entry.type === 'message');
  if (profile.engine_kind === 'codex') {
    const parent = stored.find((row) => row.subpath === parentSubpath && row.entry.type === 'session_meta')
      ?.entry.payload as Record<string, unknown> | undefined;
    if (!parent) return [];
    const children = new Map(rows.filter((row) => row.entry.type === 'session_meta').flatMap((row) => {
      const meta = row.entry.payload as Record<string, unknown>;
      return meta.parent_thread_id === parent.id ? [[row.subpath, meta] as const] : [];
    }));
    const calls = rows.filter((row) => children.has(row.subpath))
      .flatMap((row) => nativeCalls(row.entry, row.subpath))
      .filter((call) => call.name === 'exec_command' && String(call.input.cmd).includes(gate.started));
    const tools = new Map<string, NativeChildTool>();
    for (const call of calls) {
      const metadata = children.get(call.subpath as string | null)!;
      const nativeThreadId = String(metadata.id);
      tools.set(`${nativeThreadId}:${call.id}`, {
        ...call, name: 'commandExecution', commandArguments: call.input, nativeThreadId,
        nativeSessionCwd: String(metadata.cwd),
      });
    }
    // The rollout retains the same Core CommandExecutionItem that the official
    // app-server converts into ThreadItem, even while the parent is waiting.
    for (const row of rows) {
      const payload = row.entry.payload as Record<string, unknown> | undefined;
      const item = payload?.item as Record<string, unknown> | undefined;
      if (row.entry.type !== 'event_msg' || payload?.type !== 'item_completed' || item?.type !== 'CommandExecution') continue;
      const key = `${String(payload.thread_id)}:${String(item.id)}`;
      const call = tools.get(key);
      if (!call || call.subpath !== row.subpath || item.status === 'in_progress') continue;
      tools.set(key, {
        ...call, completedCommand: item,
        result: { output: item.aggregated_output, isError: item.status !== 'completed' || item.exit_code !== 0 },
      });
    }
    for (const row of rows) {
      if (!children.has(row.subpath) || row.entry.type !== 'response_item') continue;
      const payload = row.entry.payload as Record<string, unknown> | undefined;
      if (payload?.type !== 'function_call_output' || typeof payload.call_id !== 'string'
          || typeof payload.output !== 'string') continue;
      const key = `${String(children.get(row.subpath)!.id)}:${payload.call_id}`;
      const call = tools.get(key);
      if (call) tools.set(key, { ...call, recordedFunctionOutput: payload.output });
    }
    return [...tools.values()];
  }
  const calls = rows.flatMap((row) => nativeCalls(row.entry, row.subpath))
    .filter((call) => profile.tools.command?.includes(call.name) && JSON.stringify(call.input).includes(gate.started));
  return [...new Map(calls.map((call) => [`${String(call.subpath)}:${call.id}`, call])).values()]
    .map((call) => {
      const results = rows.filter((row) => row.subpath === call.subpath).flatMap((row) => objects(row.entry))
        .filter((block) => (block.type === 'tool_result' && block.tool_use_id === call.id)
          || ((block.role === 'tool' || block.role === 'toolResult') && block.toolCallId === call.id));
      const result = results.at(-1);
      return { ...call, ...(result ? {
        result: { output: result.content, isError: result.is_error === true || result.isError === true },
      } : {}) };
    });
}

export function nativeCalls(value: unknown, subpath: unknown): NativeCall[] {
  if (Array.isArray(value)) return value.flatMap((item) => nativeCalls(item, subpath));
  if (!value || typeof value !== 'object') return [];
  const block = value as Record<string, unknown>;
  if (block.type === 'assistant/message') {
    const data = block.data as Record<string, unknown> | undefined;
    const message = data?.message as Record<string, unknown> | undefined;
    if (!Array.isArray(message?.content)) {
      throw new Error('Native DSH assistant message has no content blocks');
    }
    // The supplier's replayState and attempt stream are provider-private
    // metadata; only committed message content contains these tool calls.
    return nativeCalls(message.content, subpath);
  }
  if (block.type === 'tool/call') {
    const data = block.data as Record<string, unknown>;
    return [{ id: String(data.callId), name: String(data.name),
      input: JSON.parse(String(data.arguments)) as Record<string, unknown>, subpath }];
  }
  if (['tool_use', 'tool-call', 'toolCall', 'function_call'].includes(String(block.type))) {
    const raw = block.input ?? block.arguments;
    const input: unknown = typeof raw === 'string' ? JSON.parse(raw) : raw;
    if (!input || typeof input !== 'object' || Array.isArray(input)) {
      throw new Error(`Native ${String(block.name)} call has non-object arguments`);
    }
    return [{
      id: String(block.call_id ?? block.id ?? ''), name: String(block.name),
      input: input as Record<string, unknown>, subpath,
    }];
  }
  return Object.values(block).flatMap((item) => nativeCalls(item, subpath));
}

/** Reads committed supplier records, never the reconciling child-runs API. */
export function launchEvidence(sessionId: string, profile: EngineProfile, gate: ChildGate): NativeCall[] {
  const names = profile.engine_kind === 'claude_code' ? ['Agent', 'Task']
    : profile.engine_kind === 'codex' ? ['spawn_agent'] : ['subagent'];
  const calls = nativeRows(sessionId).flatMap((row) => {
    const entry = JSON.parse(String(row.entry_json));
    // Pi's workflow:true consumes the fenced script in the same assistant
    // message. The tool arguments carry the mode, not the child task.
    const hasPiScript = profile.engine_kind === 'pi' && objects(entry).some((block) =>
      block.type === 'text' && typeof block.text === 'string'
      && block.text.includes('```js workflow') && block.text.includes(gate.marker));
    return nativeCalls(entry, row.subpath).filter((call) => names.includes(call.name)
      && (JSON.stringify(call.input).includes(gate.marker) || (hasPiScript && call.input.workflow === true)));
  });
  return [...new Map(calls.map((call) => [call.id, call])).values()];
}

export function expectNativeMode(calls: NativeCall[], profile: EngineProfile, mode: ChildMode): void {
  expect(calls, 'one real native delegation must own the held child workload').toHaveLength(1);
  expect(calls[0]!.id, 'the supplier launch must carry a native call identity').not.toBe('');
  if (profile.engine_kind !== 'codex') {
    const field = profile.engine_kind === 'pi' ? 'async' : 'run_in_background';
    expect(calls[0]!.input[field], `the actual ${field} must select ${mode}`).toBe(mode === 'background');
  }
}

/** Codex rust-v0.153.4 waits through its native wait_agent targets argument. */
export function codexWaits(sessionId: string, launchSubpath: unknown): NativeCall[] {
  return nativeRows(sessionId).filter((row) => row.subpath === launchSubpath)
    .flatMap((row) => nativeCalls(JSON.parse(String(row.entry_json)), row.subpath))
    .filter((call) => call.name === 'wait_agent');
}

export function childPrompt(profile: EngineProfile, mode: ChildMode, gate: ChildGate): string {
  const command = [
    "python3 - <<'PY'", 'from pathlib import Path', 'import time',
    `Path(${JSON.stringify(gate.started)}).write_text(${JSON.stringify(gate.marker)})`,
    `release = Path(${JSON.stringify(gate.release)})`,
    'deadline = time.monotonic() + 90',
    'while not release.exists():',
    '    if time.monotonic() >= deadline:',
    "        raise TimeoutError('child lifecycle observer did not release the workload')",
    '    time.sleep(0.1)',
    'result = release.read_text()',
    `Path(${JSON.stringify(gate.completed)}).write_text(result)`,
    'print(result)', 'PY',
  ].join('\n');
  const task = [
    `Process work item ${gate.marker}. Use your shell tool once to execute this exact command.`,
    'Keep the command in the foreground and wait for it to finish; do not detach it.',
    profile.engine_kind === 'claude_code' ? 'Set Bash.run_in_background=false and timeout=120000.' : '',
    command,
    'After the command finishes, include the actual stdout receipt in your final answer.',
    'Do not fabricate the receipt, create the release file, start another child, or run other tools.',
  ].filter(Boolean).join('\n');
  let launch: string;
  if (profile.engine_kind === 'pi') {
    launch = 'In the same reply as your one subagent call, emit this exact workflow block:\n'
      + '```js workflow\n'
      + `return runs.run("child", { agent: "delegate", task: ${JSON.stringify(task)} })\n`
      + '```\n'
      + `Call subagent exactly once with ${JSON.stringify({
        workflow: true, async: mode === 'background', isolation: 'none', mission: false,
      })}. Do not pass script text as the workflow argument.`;
  } else if (profile.engine_kind === 'codex') {
    launch = `Call spawn_agent exactly once with message=${JSON.stringify(task)}. `
      + (mode === 'foreground'
        ? 'Then call wait_agent for that Agent and wait for its completed result before answering.'
        : 'Do not call wait_agent, send_input, close_agent, or any other tool after spawning.');
  } else {
    launch = `Call ${profile.engine_kind === 'claude_code' ? 'Agent' : 'subagent'} exactly once with `
      + `run_in_background=${mode === 'background'}, description=${JSON.stringify(gate.marker)}, `
      + `prompt=${JSON.stringify(task)}.`
      + (profile.engine_kind === 'claude_code' ? ' Use subagent_type="general-purpose".' : '');
  }
  return [
    `Delegate work item ${gate.marker} to exactly one native child Agent.`, launch,
    'Do not run shell commands yourself. The child owns the complete workload.',
    mode === 'background'
      ? 'Immediately acknowledge the launch and end your parent turn while the child continues. Do not collect, poll, or wait for its result.'
      : 'Keep the parent turn waiting for this child; after its result arrives, give a short completion acknowledgement.',
  ].join('\n');
}
