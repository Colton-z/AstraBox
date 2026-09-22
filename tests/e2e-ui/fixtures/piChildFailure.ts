/** Read the supplier's own failed run and SessionStore, without child-API repair. */
import { expect } from '@playwright/test';

import type { AstraApi } from './astraApi';
import { snapshotDoc } from './dbOracle';
import { nativeRows, type ChildGate } from './nativeChildLifecycle';
import { terminalOutput } from './nativeMcpServer';
import { PlatformApi } from './platformApi';
import { sandboxExec, type SandboxHandle } from './sandboxOps';
import { runnerPortFor } from './staleWriterReconnect';

type ObjectRecord = Record<string, unknown>;
export interface NativeEntry { seq: number; subpath: string; entry: ObjectRecord }
export interface DirectLaunch {
  callId: string; rootSubpath: string; input: ObjectRecord; runId: string; asyncDir: string;
}
export interface FailedRun {
  runId: string; state: string; error: string; sessionId: string;
  steps: Array<{ agent: string; status: string; sessionFile: string; error: string; exitCode: number }>;
  [key: string]: unknown;
}

export function object(value: unknown): ObjectRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as ObjectRecord : {};
}

export function nativeEntries(sessionId: string, subpath?: string): NativeEntry[] {
  return nativeRows(sessionId).filter((row) => subpath === undefined || row.subpath === subpath)
    .map((row) => ({ seq: Number(row.seq), subpath: String(row.subpath),
      entry: object(JSON.parse(String(row.entry_json))) })).sort((a, b) => a.seq - b.seq);
}

export function nativeText(content: unknown): string {
  if (typeof content === 'string') return content;
  return Array.isArray(content) ? content.filter((part) => object(part).type === 'text')
    .map((part) => String(object(part).text ?? '')).join('') : '';
}

export function directLaunch(sessionId: string, marker: string, agent = 'researcher'): DirectLaunch | null {
  const entries = nativeEntries(sessionId);
  const calls = entries.flatMap(({ entry, subpath }) => {
    const message = object(entry.message);
    return Array.isArray(message.content) ? message.content.map(object)
      .filter((part) => part.type === 'toolCall' && part.name === 'subagent'
        && object(part.arguments).agent === agent
        && String(object(part.arguments).task ?? '').includes(marker))
      .map((part) => ({ callId: String(part.id), rootSubpath: subpath, input: object(part.arguments) })) : [];
  });
  if (calls.length === 0) return null;
  expect(calls, `one native direct ${agent} launch, not a workflow or duplicate`).toHaveLength(1);
  const call = calls[0]!;
  const result = entries.filter((row) => row.subpath === call.rootSubpath)
    .map((row) => object(row.entry.message))
    .find((message) => message.role === 'toolResult' && message.toolCallId === call.callId);
  if (!result) return null;
  const details = object(result.details);
  expect(result.isError, 'the native launcher must actually admit a background run').not.toBe(true);
  expect(details.asyncId).toEqual(expect.any(String));
  expect(details.asyncDir).toEqual(expect.any(String));
  expect(call.input).not.toHaveProperty('workflowScript');
  expect(call.input).not.toHaveProperty('async');
  return { ...call, runId: String(details.asyncId), asyncDir: String(details.asyncDir) };
}

export async function readNativeRun(api: AstraApi, sessionId: string, launch: DirectLaunch): Promise<FailedRun> {
  // Native artifacts live in the runtime home, outside the workspace file API.
  const path = `${launch.asyncDir}/status.json`;
  // One JSON line avoids one durable terminal event per pretty-printed line.
  return JSON.parse(terminalOutput(await api.runTerminalCommand(sessionId, [
    "python3 - <<'PY'", 'import json',
    `with open(${JSON.stringify(path)}) as source:`,
    '    print(json.dumps(json.load(source), separators=(",", ":")))', 'PY',
  ].join('\n'), undefined, 10_000))) as FailedRun;
}

export function nativeSubpath(sessionFile: string): string {
  const separator = '/.pi/sessions/';
  const position = sessionFile.indexOf(separator);
  expect(position, 'the supplier sessionFile identifies the actual Pi SessionStore file').toBeGreaterThan(0);
  return `pi/${sessionFile.slice(position + separator.length)}`;
}

export function finalReplies(entries: NativeEntry[], afterSeq = -1): Array<{ seq: number; id: string; text: string }> {
  return entries.filter((row) => row.seq > afterSeq).flatMap(({ seq, entry }) => {
    const message = object(entry.message);
    const text = nativeText(message.content);
    return message.role === 'assistant' && message.stopReason === 'stop' && text
      ? [{ seq, id: String(entry.id), text }] : [];
  });
}

export function completionNotice(entries: NativeEntry[], sessionFile: string): NativeEntry | undefined {
  // pi-subagents v0.66.0 notify.ts publishes this native custom message.
  return entries.find(({ entry }) => entry.type === 'custom_message'
    && entry.customType === 'subagent-notify' && nativeText(entry.content).includes(sessionFile));
}

/** Two ordinary Bash calls make new native activity after the fault is armed. */
export function twoStageChildWorkload(gates: [ChildGate, ChildGate], scriptPath: string): { script: string; prompt: string } {
  const script = [
    'from pathlib import Path', 'import json, sys, time',
    `gate = json.loads(${JSON.stringify(JSON.stringify(gates))})[int(sys.argv[1]) - 1]`,
    'Path(gate["started"]).write_text(gate["marker"])',
    'release = Path(gate["release"])',
    'deadline = time.monotonic() + 90',
    'while not release.exists():',
    '    if time.monotonic() >= deadline:',
    "        raise TimeoutError('child inspection observer did not release the workload')",
    '    time.sleep(0.1)',
    'result = release.read_text()',
    'Path(gate["completed"]).write_text(result)',
    'print(result)',
  ].join('\n');
  const task = `Process ${gates[0].marker} using exactly two sequential foreground Bash calls. `
    + 'Execute command 1 and wait for its result before calling command 2. Never combine, detach, or parallelize them.\n'
    + `Command 1: python3 ${scriptPath} 1\nCommand 2: python3 ${scriptPath} 2`
    + '\nAfter both calls return, give a short final answer including both actual stdout receipts. '
    + 'Do not create either release file, delegate, or run other tools.';
  const prompt = `Call subagent exactly once with ${JSON.stringify({ agent: 'delegate', task })}. `
    + 'Use the direct builtin and default background mode, not workflowScript; leave async, tools, model and extensions unspecified. '
    + 'Do not run commands yourself. Immediately acknowledge delegation and end your turn without polling or waiting. '
    + 'When the automatic completion notification arrives, give a short summary of the actual result.';
  return { script, prompt };
}

export async function gateContents(api: AstraApi, platform: PlatformApi, sessionId: string, root: string, gate: ChildGate): Promise<Array<string | null>> {
  const files = await platform.listFiles(sessionId, root, 10_000);
  return Promise.all([gate.started, gate.release, gate.completed].map((path) =>
    files.entries?.some((entry) => entry.name === path.split('/').at(-1) && entry.kind === 'file')
      ? api.downloadFileText(sessionId, path, 10_000) : null));
}

/** EISDIR at the exact replay file makes the real supplier's inspect reader fail. */
export async function obstructCompletionReplay(api: AstraApi, sessionId: string, launch: DirectLaunch): Promise<{ path: string; restore(): Promise<void> }> {
  const suffix = `/async-subagent-runs/${launch.runId}`;
  expect(launch.asyncDir.endsWith(suffix), 'derive the sibling results directory from the actual supplier path').toBe(true);
  const results = `${launch.asyncDir.slice(0, -suffix.length)}/async-subagent-results`;
  const replayDirectory = `${results}/completion-replay`;
  const filename = `${encodeURIComponent(launch.runId)}.json`;
  const path = `${replayDirectory}/${filename}`;
  terminalOutput(await api.runTerminalCommand(sessionId, [
    "python3 - <<'PY'", 'from pathlib import Path',
    `path = Path(${JSON.stringify(path)})`,
    'path.parent.mkdir(exist_ok=True)',
    'assert not path.exists() and not path.is_symlink(), "never overwrite a genuine supplier completion"',
    'path.mkdir()', 'assert path.is_dir()', 'PY',
  ].join('\n'), undefined, 10_000));
  let restored = false;
  return { path, async restore() {
    if (restored) return;
    // rmdir can remove only this empty injected directory, never native artifacts.
    terminalOutput(await api.runTerminalCommand(sessionId, [
      "python3 - <<'PY'", 'from pathlib import Path',
      `path = Path(${JSON.stringify(path)})`,
      'assert path.is_dir() and not path.is_symlink()',
      'assert not list(path.iterdir()), "only remove our empty obstruction"',
      'path.rmdir()', 'assert not path.exists()', 'PY',
    ].join('\n'), undefined, 10_000));
    restored = true;
  } };
}

export function currentPiPipe(sessionId: string): string | null {
  const anchor = object(snapshotDoc(sessionId)?.current_turn_engine_anchor);
  if (!anchor.engine_turn_id) return null;
  expect(anchor.engine_kind).toBe('pi');
  const value = String(anchor.engine_turn_id);
  expect(value.startsWith('pi-rpc-v1.')).toBe(true);
  const decoded = object(JSON.parse(Buffer.from(value.slice('pi-rpc-v1.'.length), 'base64url').toString('utf8')));
  expect(decoded.pty_session_id).toEqual(expect.any(String));
  return String(decoded.pty_session_id);
}

/** Replay the real execd pipe as a viewer; never attach as its input/output owner. */
export function nativeInspectReplies(handle: SandboxHandle, identity: ObjectRecord, pipeId: string, runId: string): ObjectRecord[] {
  expect(identity.sandbox_tenancy, 'this fixture observes the configured Agent-shared Pi process').toBe('agent');
  expect(identity.isolated_session_id).toBeTruthy();
  const port = runnerPortFor(identity);
  const home = String(identity.home_dir ?? '');
  expect(home).toMatch(/^\/home\/conversations\//);
  const script = [
    "python3 - <<'PY'", 'import asyncio, json', 'from pathlib import Path',
    'from websockets.asyncio.client import connect',
    `credential = Path(${JSON.stringify(`${home}/.astrabox-service-credential`)}).read_text().strip()`,
    `url = ${JSON.stringify(`ws://127.0.0.1:${port}/pty/${pipeId}/ws?mode=viewer&since=0`)}`,
    `target = ${JSON.stringify(runId)}`,
    'async def main():',
    '    buffer = b""', '    offsets = []', '    replies = []',
    '    async with connect(url, additional_headers={"X-EXECD-ACCESS-TOKEN": credential}, max_size=64*1024*1024) as ws:',
    '        while True:', '            frame = await asyncio.wait_for(ws.recv(), 10)',
    '            if isinstance(frame, str):', '                control = json.loads(frame)',
    '                if control.get("type") == "connected":',
    '                    assert offsets and offsets[0] == 0, "native replay did not begin at zero"',
    '                    print(json.dumps({"finished_replay": True, "replies": replies}))',
    '                    return', '                continue',
    '            if frame[0] == 3:',
    '                offsets.append(int.from_bytes(frame[1:9], "big"))',
    '                buffer += frame[9:]',
    '            elif frame[0] == 1:', '                buffer += frame[1:]',
    '            else:', '                continue',
    '            while b"\\n" in buffer:',
    '                line, buffer = buffer.split(b"\\n", 1)',
    '                if not line.strip(): continue',
    '                record = json.loads(line)',
    '                for text in record.get("widgetLines", []):',
    '                    if text.startswith("PI_SUBAGENT_INSPECT_JSON:"):',
    '                        reply = json.loads(text.split(":", 1)[1])',
    '                        if reply.get("asyncId") == target: replies.append(reply)',
    'asyncio.run(main())', 'PY',
  ].join('\n');
  const result = object(JSON.parse(sandboxExec(handle, script, 15_000)));
  expect(result.finished_replay).toBe(true);
  expect(Array.isArray(result.replies)).toBe(true);
  return (result.replies as unknown[]).map(object);
}
