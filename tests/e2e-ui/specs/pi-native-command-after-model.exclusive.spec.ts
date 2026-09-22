/** A supplier command can continue writing native history after its model settles. */
import { randomUUID } from 'node:crypto';
import { expect, test, type Page } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { framesForTurn, sessionEvents, snapshotDoc, waitForTurnTerminalProof } from '../fixtures/dbOracle';
import { engineProfiles } from '../fixtures/engineProfile';
import { nativeCalls, nativeRows } from '../fixtures/nativeChildLifecycle';
import { finalReplies, nativeEntries, object } from '../fixtures/piChildFailure';
import { requireSandboxHandle, sandboxExec, type SandboxHandle } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
let sessionId = '';
let ownedAgent = '';
let handle: SandboxHandle | undefined;
let pipe: string | undefined;
const observations: unknown[] = [];
onPassOnly(async ({ request }) => {
  if (ownedAgent) await new AstraApi(request).deleteAgent(ownedAgent);
});
test.afterEach(async ({ request, page }, info) => {
  if (!sessionId || !['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  const api = new AstraApi(request);
  const reads = await Promise.allSettled([
    api.getSession(sessionId), api.getMessages(sessionId, 100), aiStreamBodies(page),
    Promise.resolve().then(() => sessionEvents(sessionId)),
    Promise.resolve().then(() => nativeRows(sessionId)),
    Promise.resolve().then(() => handle && pipe ? nativeOutput(handle, pipe) : null),
  ]);
  await info.attach('pi-post-model-command-scene', {
    body: JSON.stringify({ sessionId, ownedAgent, observations, reads }), contentType: 'application/json',
  });
});

function pipeFor(turnId: string): string | null {
  const anchors = [
    object(snapshotDoc(sessionId)?.current_turn_engine_anchor).engine_turn_id,
    ...framesForTurn(turnId).map((frame) => frame.engine_turn_id),
    ...sessionEvents(sessionId).filter((event) => event.turn_id === turnId)
      .map((event) => object(event.payload).engine_turn_id),
  ].filter((value): value is string => typeof value === 'string' && value.startsWith('pi-rpc-v1.'));
  const pipes = [...new Set(anchors.map((anchor) =>
    object(JSON.parse(Buffer.from(anchor.slice('pi-rpc-v1.'.length), 'base64url').toString())).pty_session_id))];
  if (pipes.length === 0) return null;
  expect(pipes).toHaveLength(1);
  expect(pipes[0]).toEqual(expect.any(String));
  expect(String(pipes[0]).trim()).not.toBe('');
  return String(pipes[0]);
}

/** Cold tenancy's read-only execd viewer retains wire order without writing RPC. */
function nativeOutput(sandbox: SandboxHandle, pipeId: string): Record<string, unknown>[] {
  const script = [
    "python3 - <<'PY'", 'import asyncio, json', 'from websockets.asyncio.client import connect',
    `url = ${JSON.stringify(`ws://127.0.0.1:44772/pty/${pipeId}/ws?mode=viewer&since=0`)}`,
    'async def main():', '    buffer = b""', '    offsets = []', '    records = []',
    '    async with connect(url, max_size=64*1024*1024) as ws:',
    '        while True:', '            frame = await asyncio.wait_for(ws.recv(), 10)',
    '            if isinstance(frame, str):', '                control = json.loads(frame)',
    '                if control.get("type") == "connected":',
    '                    assert offsets and offsets[0] == 0, "native replay did not begin at zero"',
    '                    print(json.dumps({"finished_replay": True, "records": records}))',
    '                    return', '                continue',
    '            if frame[0] == 3:', '                offsets.append(int.from_bytes(frame[1:9], "big"))',
    '                buffer += frame[9:]', '            elif frame[0] == 1:', '                buffer += frame[1:]',
    '            else:', '                continue',
    '            while b"\\n" in buffer:', '                line, buffer = buffer.split(b"\\n", 1)',
    '                if line.strip(): records.append(json.loads(line))',
    'asyncio.run(main())', 'PY',
  ].join('\n');
  const result = object(JSON.parse(sandboxExec(sandbox, script, 15_000)));
  expect(result.finished_replay).toBe(true);
  expect(Array.isArray(result.records)).toBe(true);
  return (result.records as unknown[]).map(object);
}

async function noFailure(page: Page, phase: 'live' | 'idle' = 'live'): Promise<void> {
  const bodies = await aiStreamBodies(page);
  observations.push({ browserBodies: bodies });
  const frames = bodies.flatMap((body) => body.text.slice(0, body.text.lastIndexOf('\n') + 1)
    .split('\n').flatMap((line) => {
      if (!line.startsWith('data:')) return [];
      const data = line.slice(5).trim();
      return data && data !== '[DONE]' ? [object(JSON.parse(data))] : [];
    }));
  if (phase === 'live') {
    expect(frames.length, 'capture the browser stream including transient failures').toBeGreaterThan(0);
  }
  expect(frames.filter((frame) => ['error', 'data-turn-failure'].includes(String(frame.type)))).toEqual([]);
  expect(sessionEvents(sessionId).filter((event) => event.event_type === 'turn.failed')).toEqual([]);
  await expect(page.getByTestId('run-view').getByText(
    /本轮执行失败|上一条消息已送达沙箱，但这一轮执行失败了|This turn failed/i,
  )).toHaveCount(0);
}

test('Pi native command retains post-model entries until its prompt acknowledgement', async ({ page, request }) => {
  const profiles = engineProfiles().filter((profile) => profile.engine_kind === 'pi');
  expect(profiles).toHaveLength(1);
  const profile = profiles[0]!;
  const api = new AstraApi(request);
  const environments = await api.data<Array<Record<string, unknown>>>('GET', '/admin/environments');
  const environment = environments.find((item) => item.enabled === true
    && item.sandbox_tenancy === 'conversation' && item.engine_kind === 'pi'
    && item.runtime_template_name === profile.image);
  expect(environment, 'use the deployment-proven cold Pi Environment').toBeTruthy();
  const base = await api.getAgent(profile.agent_id);
  const options = object(base.engine_options);
  const settings = object(options.settings);
  const extensions = settings.extensions ?? [];
  expect(Array.isArray(extensions)).toBe(true);
  const reloadExtension = '/opt/pi/node_modules/@earendil-works/pi-coding-agent/examples/extensions/reload-runtime.ts';
  const agent = await api.createAgent({
    name: `__e2e_pi_post_model_${Date.now()}`, model: base.model,
    environment_name: environment!.name, prewarm_enabled: false,
    engine_options: { ...options, settings: {
      ...settings, extensions: [...extensions as unknown[], reloadExtension],
    } },
  });
  ownedAgent = agent.agent_id;
  sessionId = (await api.startConversation(ownedAgent)).session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  test.info().annotations.push({ type: 'engine_kind', description: 'pi' });
  await api.waitForSessionReady(sessionId);
  const detail = await api.adminSessionDetail(sessionId);
  const identity = object(detail.runtime_identity);
  expect(identity.sandbox_tenancy).toBe('conversation');
  const root = String(identity.workspace_dir ?? '').replace(/\/+$/, '');
  const home = String(identity.home_dir ?? '').replace(/\/+$/, '');
  const nativeSessionId = String(detail.engine_session_key ?? '').trim();
  expect(root).toMatch(/^\//);
  expect(home).toMatch(/^\//);
  expect(nativeSessionId).not.toBe('');
  expect(String(detail.sandbox_id ?? '')).not.toBe('');
  handle = await requireSandboxHandle(api, String(detail.sandbox_id));
  const directory = `${root}/.pi/extensions`;
  const extensionPath = `${directory}/astrabox-post-model.ts`;
  const marker = `PI_POST_MODEL_${randomUUID().replaceAll('-', '')}`;
  const resultPath = `${root}/${marker}.json`;
  const prompt = `Reply with exactly ${marker}. Do not call tools or delegate; this only needs a plain text reply.`;
  await api.data('POST', `/sessions/${sessionId}/files/mkdir`, { path: directory });
  await api.uploadFileText(sessionId, root, `${marker}.json`, 'NOT_COMPLETED', 10_000);
  // Pi 0.85.1 agent-session.ts _emitAgentSettled emits before resolving waitForIdle;
  // appendEntry emits entry_appended, and prompt preflight ACK follows handler return.
  // sendUserMessage is void: wait for the real agent_start before testing native idle.
  const extension = [
    "import { writeFile } from 'node:fs/promises';",
    'export default function (pi) {',
    '  let started;', '  let answer;',
    "  pi.on('agent_start', () => { if (started) started(); });",
    "  pi.on('message_end', (event) => {",
    "    if (event.message.role === 'assistant' && event.message.stopReason === 'stop') {",
    "      answer = event.message.content.filter(p => p.type === 'text').map(p => p.text).join('');",
    '    }', '  });',
    "  pi.registerCommand('astrabox-post-model', {",
    "    description: 'Persist a native command result after its model settles',",
    '    handler: async (_args, ctx) => {',
    '      const hasStarted = new Promise(resolve => { started = resolve; });',
    `      pi.sendUserMessage(${JSON.stringify(prompt)});`,
    '      await hasStarted;', '      await ctx.waitForIdle();',
    "      if (!answer) throw new Error('native model did not produce a successful answer');",
    `      const result = { marker: ${JSON.stringify(marker)}, answer };`,
    "      pi.appendEntry('astrabox-post-model-result', result);",
    `      await writeFile(${JSON.stringify(resultPath)}, JSON.stringify(result));`,
    '    },', '  });', '}',
  ].join('\n');
  await api.uploadFileText(sessionId, directory, 'astrabox-post-model.ts', extension, 10_000);
  expect(await api.downloadFileText(sessionId, extensionPath, 10_000)).toBe(extension);
  sandboxExec(handle, [
    "python3 - <<'PY'", 'import json', 'from pathlib import Path',
    `path = Path(${JSON.stringify(`${home}/.pi/agent/settings.json`)})`,
    'settings = json.loads(path.read_text())',
    `assert ${JSON.stringify(reloadExtension)} in settings.get("extensions", [])`,
    `extension = ${JSON.stringify(extensionPath)}`,
    'extensions = settings.setdefault("extensions", [])', 'assert isinstance(extensions, list)',
    'assert extension not in extensions', 'extensions.append(extension)',
    'path.write_text(json.dumps(settings))',
    'assert extension in json.loads(path.read_text())["extensions"]', 'PY',
  ].join('\n'), 10_000);

  await mirrorSseBodies(page);
  await openSessionView(page, sessionId);
  const previous = (await api.getSession(sessionId)).last_turn_id;
  await sendPrompt(page, sessionId, '/reload-runtime');
  const reloaded = await api.waitForSession(sessionId, (value) => {
    if (value.last_error || value.last_turn_status === 'FAILED') throw new Error(JSON.stringify(value));
    return Boolean(value.last_turn_id && value.last_turn_id !== previous
      && value.last_turn_status === 'COMPLETED' && value.state === 'READY' && !value.current_turn_id);
  }, 30_000);
  const reloadTurn = String(reloaded.last_turn_id);
  await waitForTurnTerminalProof(sessionId, reloadTurn, 'COMPLETED', 5_000);
  await expect.poll(() => pipeFor(reloadTurn), { timeout: 15_000 }).not.toBeNull();
  pipe = pipeFor(reloadTurn)!;
  const initialNative = nativeOutput(handle, pipe);
  expect(initialNative.filter((row) => row.type === 'response' && row.command === 'get_state')
    .some((row) => row.success === true && object(row.data).sessionId === nativeSessionId)).toBe(true);
  const initialAcks = initialNative.filter((row) => row.type === 'response' && row.command === 'prompt');
  expect(initialAcks).toHaveLength(1);
  expect(initialAcks[0]).toMatchObject({ success: true });
  observations.push({ reloadTurn, initialNative });
  await noFailure(page);

  await sendPrompt(page, sessionId, '/astrabox-post-model');
  // Retain the supplier's completed result even when the platform fails at entry_appended.
  const ended = await api.waitForSession(sessionId, (value) => Boolean(value.last_turn_id
    && value.last_turn_id !== reloadTurn && ['COMPLETED', 'FAILED'].includes(String(value.last_turn_status))), 90_000);
  const turnId = String(ended.last_turn_id);
  let wire: Record<string, unknown>[] = [];
  await expect.poll(() => {
    wire = nativeOutput(handle!, pipe!);
    expect(wire.filter((row) => row.type === 'extension_error')).toEqual([]);
    return wire.filter((row) => row.type === 'response' && row.command === 'prompt').length;
  }, { timeout: 15_000, message: 'read the actual supplier command ACK, including on platform failure' }).toBe(2);
  observations.push({ turnId, ended, wire });
  const settled = wire.map((row, index) => ({ row, index })).filter(({ row }) => row.type === 'agent_settled');
  expect(settled).toHaveLength(1);
  const appended = wire.map((row, index) => ({ row, index })).filter(({ row }) => row.type === 'entry_appended'
    && object(row.entry).customType === 'astrabox-post-model-result');
  expect(appended).toHaveLength(1);
  const acks = wire.map((row, index) => ({ row, index })).filter(({ row }) => row.type === 'response'
    && row.command === 'prompt');
  expect(acks).toHaveLength(2);
  expect(acks[1]!.row).toMatchObject({ success: true });
  expect(acks[1]!.row.id).not.toBe(acks[0]!.row.id);
  expect(appended[0]!.index).toBeGreaterThan(settled[0]!.index);
  expect(acks[1]!.index).toBeGreaterThan(appended[0]!.index);
  const result = object(object(appended[0]!.row.entry).data);
  expect(result.marker).toBe(marker);
  expect(result.answer).toEqual(expect.any(String));
  expect(String(result.answer)).toContain(marker);
  expect(JSON.parse(await api.downloadFileText(sessionId, resultPath, 10_000))).toEqual(result);
  expect(ended.last_turn_status, 'the supplier command finished after its model; both boundaries must survive').toBe('COMPLETED');
  expect(ended.state).toBe('READY');
  expect(ended.last_error).toBeFalsy();
  expect(ended.current_turn_id).toBeFalsy();
  await waitForTurnTerminalProof(sessionId, turnId, 'COMPLETED', 5_000);
  const durableFrames = framesForTurn(turnId);
  const durableAcks = sessionEvents(sessionId).filter((event) => {
    const payload = object(event.payload);
    return event.turn_id === turnId && event.event_type === 'engine.diagnostic'
      && payload.event_type === 'pi.rpc' && payload.subtype === 'prompt'
      && object(payload.raw).id === acks[1]!.row.id;
  });
  expect(durableAcks, 'the actual command ACK must belong to this platform turn').toHaveLength(1);
  expect(object(durableAcks[0]!.payload).raw).toEqual(acks[1]!.row);
  const finishes = durableFrames.filter((frame) => object(frame.payload).type === 'finish');
  expect(finishes, 'one durable terminal must follow the complete command, not only its model').toHaveLength(1);
  expect(Number(durableAcks[0]!.event_seq)).toBeGreaterThan(0);
  expect(Number(finishes[0]!.event_seq), 'platform completion must follow its persisted native ACK')
    .toBeGreaterThan(Number(durableAcks[0]!.event_seq));
  observations.push({ durableAcks, finishes });
  await expect.poll(() => nativeEntries(sessionId).filter(({ entry }) => entry.type === 'custom'
    && entry.customType === 'astrabox-post-model-result').map(({ entry }) => entry.data)).toEqual([result]);
  const entries = nativeEntries(sessionId);
  expect(finalReplies(entries).map((reply) => reply.text)).toEqual([result.answer]);
  expect(entries.flatMap(({ entry, subpath }) => nativeCalls(entry, subpath))).toEqual([]);
  const history = await api.getMessages(sessionId, 100);
  const replies = history.messages.filter((message) => message.role === 'assistant'
    && messageText(message) === result.answer);
  expect(replies).toHaveLength(1);
  const answer = page.locator(`[data-message-id="${replies[0]!.message_id}"]`).getByTestId('assistant-text');
  await expect(answer).toContainText(marker);
  const rendered = await answer.innerText();
  await noFailure(page);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(answer).toHaveText(rendered);
  expect((await api.getSession(sessionId)).last_turn_status).toBe('COMPLETED');
  await noFailure(page, 'idle');
});
