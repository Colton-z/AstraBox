/** A supplier command can continue writing native history after its model settles. */
import { randomUUID } from 'node:crypto';
import { expect, test, type Page } from '@playwright/test';

import { AstraApi, messageText, type SessionRecord } from '../fixtures/astraApi';
import { sessionInputRows, sessionOutputRows, sessionResponseRows, type SessionInputRow } from '../fixtures/dbOracle';
import { engineProfiles } from '../fixtures/engineProfile';
import { nativeCalls, nativeRows } from '../fixtures/nativeChildLifecycle';
import { currentPiPipe, finalReplies, nativeEntries, object } from '../fixtures/piChildFailure';
import { requireSandboxHandle, sandboxExec, type SandboxHandle } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { sessionStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

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
    api.getSession(sessionId), api.getMessages(sessionId, 100), sessionStreamBodies(page),
    Promise.resolve().then(() => sessionInputRows(sessionId)),
    Promise.resolve().then(() => sessionResponseRows(sessionId)),
    Promise.resolve().then(() => nativeRows(sessionId)),
    Promise.resolve().then(() => handle && pipe ? nativeOutput(handle, pipe) : null),
  ]);
  await info.attach('pi-post-model-command-scene', {
    body: JSON.stringify({ sessionId, ownedAgent, observations, reads }), contentType: 'application/json',
  });
});

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
  const bodies = await sessionStreamBodies(page);
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
  expect(frames.filter((frame) => ['error', 'data-input-failed'].includes(String(frame.type)))).toEqual([]);
  expect(sessionResponseRows(sessionId).filter((row) => row.outcome === 'failed')).toEqual([]);
  expect(sessionInputRows(sessionId).filter((row) => row.state === 'failed')).toEqual([]);
  await expect(page.getByTestId('run-view').getByText(/本轮执行失败|This turn failed/i)).toHaveCount(0);
}

/** The one recorded input that carried `content`, if the session has accepted it. */
function commandInput(content: string): SessionInputRow | undefined {
  const rows = sessionInputRows(sessionId).filter((row) => row.content === content);
  expect(rows.length, `${content} is accepted at most once`).toBeLessThanOrEqual(1);
  return rows[0];
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
    environment_name: environment!.name, prepared_capacity: 0,
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
  await sendPrompt(page, sessionId, '/reload-runtime');
  // Pi answers an extension command with a `handled` receipt once its handler
  // returns; the session's reader admits the input at that receipt.
  await expect.poll(() => commandInput('/reload-runtime')?.state, { timeout: 30_000 }).toBe('admitted');
  await api.waitForSession(sessionId, (value) => {
    if (value.last_error) throw new Error(JSON.stringify(value));
    return value.state === 'READY' && !(value.current_responses ?? []).length;
  }, 30_000);
  const reload = commandInput('/reload-runtime')!;
  expect(sessionResponseRows(sessionId), 'a command that writes no conversation is answered by no invented response')
    .toEqual([]);
  await expect.poll(() => currentPiPipe(sessionId), { timeout: 15_000 }).not.toBeNull();
  pipe = currentPiPipe(sessionId)!;
  const initialNative = nativeOutput(handle, pipe);
  expect(initialNative.filter((row) => row.type === 'response' && row.command === 'get_state')
    .some((row) => row.success === true && object(row.data).sessionId === nativeSessionId)).toBe(true);
  const initialAcks = initialNative.filter((row) => row.type === 'response' && row.command === 'prompt');
  expect(initialAcks, 'the receipt Pi wrote for the reload names its platform input').toEqual([expect.objectContaining({
    id: reload.input_id, success: true, data: expect.objectContaining({ disposition: 'handled' }),
  })]);
  observations.push({ reload, initialNative });
  // The reload writes no conversation, so the page's stream has nothing to carry yet.
  await noFailure(page, 'idle');

  await sendPrompt(page, sessionId, '/astrabox-post-model');
  // Retain the supplier's completed result even when the platform fails at entry_appended.
  let ended: SessionRecord | undefined;
  await expect.poll(async () => {
    ended = await api.getSession(sessionId);
    const input = commandInput('/astrabox-post-model');
    if (input?.state === 'failed' || ended.last_response?.outcome === 'failed' || ended.last_error) return 'failed';
    return input?.state === 'admitted' && !(ended.current_responses ?? []).length ? 'settled' : 'running';
  }, { timeout: 90_000, message: 'the command must settle, or fail, before its evidence is read' }).not.toBe('running');
  let wire: Record<string, unknown>[] = [];
  await expect.poll(() => {
    wire = nativeOutput(handle!, pipe!);
    expect(wire.filter((row) => row.type === 'extension_error')).toEqual([]);
    return wire.filter((row) => row.type === 'response' && row.command === 'prompt').length;
  }, { timeout: 15_000, message: 'read the actual supplier command ACK, including on platform failure' }).toBe(2);
  observations.push({ ended, wire });
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
  // The supplier command finished after its model; the platform must hold the
  // command until Pi's own receipt for it, not end it with the model's run.
  const command = commandInput('/astrabox-post-model');
  expect(command, 'Pi took the command with the receipt it wrote after the post-model entry')
    .toMatchObject({ input_id: acks[1]!.row.id, state: 'admitted' });
  expect(object(acks[1]!.row.data).disposition).toBe('handled');
  expect(ended!.state).toBe('READY');
  expect(ended!.last_error).toBeFalsy();
  expect(ended!.current_responses ?? []).toEqual([]);
  observations.push({ command });
  await expect.poll(() => nativeEntries(sessionId).filter(({ entry }) => entry.type === 'custom'
    && entry.customType === 'astrabox-post-model-result').map(({ entry }) => entry.data)).toEqual([result]);
  const entries = nativeEntries(sessionId);
  expect(finalReplies(entries).map((reply) => reply.text)).toEqual([result.answer]);
  expect(entries.flatMap(({ entry, subpath }) => nativeCalls(entry, subpath))).toEqual([]);
  const history = await api.getMessages(sessionId, 100);
  const replies = history.messages.filter((message) => message.role === 'assistant'
    && messageText(message) === result.answer);
  expect(replies).toHaveLength(1);
  const reply = replies[0]!;
  // The model's run is the conversation's one response, and it ended once with
  // the `stop` Pi recorded on its final message (`finalReplies` reads only those).
  expect(sessionResponseRows(sessionId)).toEqual([expect.objectContaining({
    response_id: reply.id, status: 'ended', outcome: 'completed', native_reason: 'stop',
  })]);
  expect(ended!.last_response).toMatchObject({ response_id: reply.id, outcome: 'completed' });
  const finishes = sessionOutputRows(sessionId, reply.id).filter((row) => row.part.type === 'finish');
  expect(finishes, 'one terminal ends the model run').toHaveLength(1);
  const answer = page.locator(`[data-message-id="${reply.id}"]`).getByTestId('assistant-text');
  await expect(answer).toContainText(marker);
  const rendered = await answer.innerText();
  await noFailure(page);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(answer).toHaveText(rendered);
  expect((await api.getSession(sessionId)).last_response).toMatchObject({ response_id: reply.id, outcome: 'completed' });
  await noFailure(page, 'idle');
});
