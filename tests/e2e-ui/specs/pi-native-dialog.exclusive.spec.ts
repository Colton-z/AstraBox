/** Native extension commands can ask a person without owning any model tool call. */
import { randomUUID } from 'node:crypto';
import { expect, test, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { framesForTurn, sessionEvents, snapshotDoc, waitForTurnTerminalProof } from '../fixtures/dbOracle';
import { engineProfiles } from '../fixtures/engineProfile';
import { nativeRows } from '../fixtures/nativeChildLifecycle';
import { object } from '../fixtures/piChildFailure';
import { requireSandboxHandle, sandboxExec, type SandboxHandle } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
let sessionId = '';
let ownedAgent = '';
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
  ]);
  await info.attach('pi-native-dialog-scene', {
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
    String(object(JSON.parse(Buffer.from(anchor.slice('pi-rpc-v1.'.length), 'base64url').toString())).pty_session_id)))];
  if (pipes.length === 0) return null;
  expect(pipes, 'the actual native receipt must name exactly one retained Pi pipe').toHaveLength(1);
  expect(pipes[0]).not.toBe('undefined');
  return pipes[0]!;
}

/** Read supplier stdout without stealing the platform's pipe or writing any RPC. */
function nativeOutput(handle: SandboxHandle, pipeId: string): Record<string, unknown>[] {
  // Conversation tenancy uses the image's ordinary execd, not an isolated-session runner.
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
  const result = object(JSON.parse(sandboxExec(handle, script, 15_000)));
  expect(result.finished_replay).toBe(true);
  expect(Array.isArray(result.records)).toBe(true);
  return (result.records as unknown[]).map(object);
}

async function noFailure(page: Page, phase: 'live' | 'idle' = 'live'): Promise<void> {
  const bodies = await aiStreamBodies(page);
  const frames = bodies.flatMap((body) => body.text.slice(0, body.text.lastIndexOf('\n') + 1)
    .split('\n').flatMap((line) => {
      if (!line.startsWith('data:')) return [];
      const data = line.slice(5).trim();
      return data && data !== '[DONE]' ? [object(JSON.parse(data))] : [];
    }));
  observations.push({ browserBodies: bodies });
  if (phase === 'live') {
    expect(frames.length, 'observe the real browser stream, not just its final DOM').toBeGreaterThan(0);
  }
  expect(frames.filter((frame) => ['error', 'data-turn-failure'].includes(String(frame.type))),
    'a transient error is a failure even when the panel later recovers').toEqual([]);
  expect(frames.filter((frame) => String(frame.type).startsWith('tool-')),
    'a native UI dialog is not a fabricated model tool call').toEqual([]);
  expect(sessionEvents(sessionId).filter((event) => event.event_type === 'turn.failed')).toEqual([]);
  await expect(page.getByTestId('run-view').getByText(
    /本轮执行失败|上一条消息已送达沙箱，但这一轮执行失败了|This turn failed/i,
  )).toHaveCount(0);
}

async function completed(api: AstraApi, previous: string | null | undefined): Promise<string> {
  const session = await api.waitForSession(sessionId, (value) => {
    if (value.last_turn_status === 'FAILED' || value.last_error) {
      throw new Error(`native command failed: ${JSON.stringify(value)}`);
    }
    return Boolean(value.last_turn_id && value.last_turn_id !== previous
      && value.last_turn_status === 'COMPLETED' && value.state === 'READY'
      && !value.current_turn_id && !value.pending_interaction);
  }, 30_000);
  await waitForTurnTerminalProof(sessionId, String(session.last_turn_id), 'COMPLETED', 5_000);
  return String(session.last_turn_id);
}

test('Pi native command dialog survives reload without inventing a tool call', async ({ page, request }) => {
  const profiles = engineProfiles().filter((profile) => profile.engine_kind === 'pi');
  expect(profiles).toHaveLength(1);
  const profile = profiles[0]!;
  const api = new AstraApi(request);
  const environments = await api.data<Array<Record<string, unknown>>>('GET', '/admin/environments');
  const environment = environments.find((item) => item.enabled === true
    && item.sandbox_tenancy === 'conversation' && item.engine_kind === 'pi'
    && item.runtime_template_name === profile.image);
  expect(environment, 'use the existing deployment-proven cold Pi Environment').toBeTruthy();
  const base = await api.getAgent(profile.agent_id);
  const options = object(base.engine_options);
  const settings = object(options.settings);
  const originalExtensions = settings.extensions ?? [];
  expect(Array.isArray(originalExtensions)).toBe(true);
  const reloadExtension = '/opt/pi/node_modules/@earendil-works/pi-coding-agent/examples/extensions/reload-runtime.ts';
  const agent = await api.createAgent({
    name: `__e2e_pi_dialog_${Date.now()}`, model: base.model,
    environment_name: environment!.name, prewarm_enabled: false,
    engine_options: { ...options, settings: {
      ...settings, extensions: [...originalExtensions as unknown[], reloadExtension],
    } },
  });
  ownedAgent = agent.agent_id;
  sessionId = (await api.startConversation(ownedAgent)).session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  test.info().annotations.push({ type: 'engine_kind', description: 'pi' });
  await api.waitForSessionReady(sessionId);
  const detail = await api.adminSessionDetail(sessionId);
  // Publishing Pi's runtime already binds the conversation and awaits native get_state.
  // The launcher must have written settings before this test edits them; no model warm-up.
  const nativeSessionId = String(detail.engine_session_key ?? '').trim();
  expect(nativeSessionId, 'READY must expose the initialized native Pi conversation').not.toBe('');
  const identity = object(detail.runtime_identity);
  expect(identity.sandbox_tenancy).toBe('conversation');
  const root = String(identity.workspace_dir ?? '').replace(/\/+$/, '');
  const home = String(identity.home_dir ?? '').replace(/\/+$/, '');
  expect(root).toMatch(/^\//);
  expect(home).toMatch(/^\//);
  const directory = `${root}/.pi/extensions`;
  const extensionPath = `${directory}/astrabox-dialog.ts`;
  const marker = `PI_DIALOG_${randomUUID().replaceAll('-', '')}`;
  const resultPath = `${root}/${marker}.json`;
  await api.data('POST', `/sessions/${sessionId}/files/mkdir`, { path: directory });
  await api.uploadFileText(sessionId, root, `${marker}.json`, 'NOT_ANSWERED', 10_000);
  // Pi 0.85.1's documented registerCommand + ctx.ui.confirm API; no registered tool.
  const extension = [
    "import { writeFile } from 'node:fs/promises';",
    "export default function (pi) {",
    "  pi.registerCommand('astrabox-dialog', {",
    "    description: 'Confirm the test-owned workspace action',",
    "    handler: async (_args, ctx) => {",
    `      const confirmed = await ctx.ui.confirm(${JSON.stringify(marker)}, 'Apply this workspace action?');`,
    `      const result = { marker: ${JSON.stringify(marker)}, confirmed };`,
    `      await writeFile(${JSON.stringify(resultPath)}, JSON.stringify(result));`,
    "      pi.appendEntry('astrabox-dialog-result', result);",
    "    },",
    "  });",
    "}",
  ].join('\n');
  await api.uploadFileText(sessionId, directory, 'astrabox-dialog.ts', extension, 10_000);
  expect(await api.downloadFileText(sessionId, extensionPath, 10_000)).toBe(extension);
  // Explicit user/global registration avoids implicitly trusting arbitrary project extensions.
  // Installation is fixture preparation, not the terminal product under test. The real
  // browser command below still performs Pi's own reload and all human interaction.
  expect(String(detail.sandbox_id ?? '')).not.toBe('');
  const handle = await requireSandboxHandle(api, String(detail.sandbox_id));
  sandboxExec(handle, [
    "python3 - <<'PY'", 'import json', 'from pathlib import Path',
    `path = Path(${JSON.stringify(`${home}/.pi/agent/settings.json`)})`,
    'settings = json.loads(path.read_text())',
    `assert ${JSON.stringify(reloadExtension)} in settings.get("extensions", []), "initial Pi settings were not installed"`,
    `extension = ${JSON.stringify(extensionPath)}`,
    'extensions = settings.setdefault("extensions", [])',
    'assert isinstance(extensions, list)',
    'assert extension not in extensions', 'extensions.append(extension)',
    'path.write_text(json.dumps(settings))',
    'assert extension in json.loads(path.read_text())["extensions"]', 'PY',
  ].join('\n'), 10_000);

  await mirrorSseBodies(page);
  await openSessionView(page, sessionId);
  const beforeReload = (await api.getSession(sessionId)).last_turn_id;
  await sendPrompt(page, sessionId, '/reload-runtime');
  // A supplier slash command ends by RPC response, not by a made-up agent_end.
  const reloading = await api.waitForSession(sessionId, (value) => Boolean(value.current_turn_id
    || (value.last_turn_id && value.last_turn_id !== beforeReload)), 15_000);
  const reloadTurn = String(reloading.current_turn_id || reloading.last_turn_id);
  await expect.poll(() => pipeFor(reloadTurn), { timeout: 15_000 }).not.toBeNull();
  const pipe = pipeFor(reloadTurn)!;
  const initialNative = nativeOutput(handle, pipe);
  expect(initialNative.filter((row) => row.type === 'response' && row.command === 'get_state')
    .some((row) => row.success === true && object(row.data).sessionId === nativeSessionId),
  'reload must run on the already initialized native conversation').toBe(true);
  const initialAcks = initialNative.filter((row) => row.type === 'response' && row.command === 'prompt');
  expect(initialAcks).toHaveLength(1);
  expect(initialAcks[0]).toMatchObject({ success: true });
  observations.push({ reloadTurn, initialNative });
  expect(await completed(api, beforeReload)).toBe(reloadTurn);
  await noFailure(page);

  await sendPrompt(page, sessionId, '/astrabox-dialog');
  const waiting = await api.waitForSession(sessionId, (value) => {
    if (value.last_turn_status === 'FAILED' || value.last_error) {
      throw new Error(`native dialog failed: ${JSON.stringify(value)}`);
    }
    return Boolean(value.pending_interaction);
  }, 30_000);
  const pending = waiting.pending_interaction!;
  const raw = object(pending.raw_input);
  expect(pending).toMatchObject({ tool_name: 'pi.extension.confirm', presentation: 'form' });
  expect(String(pending.tool_call_id ?? '')).toBe('');
  expect(raw).toMatchObject({ type: 'extension_ui_request', method: 'confirm', title: marker });
  expect(raw.id).toEqual(expect.any(String));
  expect(String(raw.id).trim()).not.toBe('');
  expect(pending.interaction_id).toBe(raw.id);
  expect(pending.turn_id).toEqual(expect.any(String));
  expect(String(pending.turn_id).trim()).not.toBe('');
  const dialogTurn = String(pending.turn_id);
  const pausedNative = nativeOutput(handle, pipe);
  const requests = pausedNative.filter((row) => row.type === 'extension_ui_request'
    && row.method === 'confirm' && row.title === marker);
  expect(requests).toHaveLength(1);
  expect(requests[0]).toEqual(raw);
  expect(pausedNative.filter((row) => row.type === 'response' && row.command === 'prompt')).toEqual(initialAcks);
  observations.push({ pending, pausedNative });
  expect(await api.downloadFileText(sessionId, resultPath, 10_000)).toBe('NOT_ANSWERED');
  const panel = page.getByTestId('pending-interaction-panel');
  await expect(panel).toContainText(marker);
  await noFailure(page);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(panel).toContainText(marker);
  expect((await api.getPendingInteraction(sessionId))?.interaction_id).toBe(raw.id);
  const historyPending = object((await api.getMessages(sessionId, 100)).pending_interaction);
  expect(historyPending).toMatchObject({ interaction_id: raw.id, turn_id: dialogTurn });
  expect(String(historyPending.tool_call_id ?? '')).toBe('');
  const yes = panel.getByRole('radio', { name: /^1\.\s+Yes$/ });
  await yes.click();
  await expect(yes).toBeChecked();
  const answerResponse = page.waitForResponse((response) => response.request().method() === 'POST'
    && response.url().includes(`/sessions/${sessionId}/interaction-respond`));
  await panel.getByRole('button', { name: /Submit answer|提交回答/ }).click();
  const answered = await answerResponse;
  expect(answered.status()).toBe(200);
  expect(object(answered.request().postDataJSON()).interaction_id).toBe(raw.id);
  expect(await completed(api, reloadTurn)).toBe(dialogTurn);
  expect(JSON.parse(await api.downloadFileText(sessionId, resultPath, 10_000)))
    .toEqual({ marker, confirmed: true });
  const finalNative = nativeOutput(handle, pipe);
  const acks = finalNative.filter((row) => row.type === 'response' && row.command === 'prompt');
  expect(acks).toHaveLength(2);
  expect(acks[1]).toMatchObject({ success: true });
  expect(acks[1]!.id).not.toBe(acks[0]!.id);
  expect(finalNative.filter((row) => ['agent_start', 'agent_end', 'agent_settled',
    'tool_execution_start', 'tool_execution_end', 'extension_error'].includes(String(row.type)))).toEqual([]);
  observations.push({ finalNative });
  // Pi 0.85.1 SessionManager._persist defers a new file until its first assistant
  // message. This zero-model command still emits a real entry; retain that event
  // in platform custody. The post-model command case checks the native file mirror.
  const appended = finalNative.filter((row) => row.type === 'entry_appended'
    && object(row.entry).customType === 'astrabox-dialog-result');
  expect(appended).toHaveLength(1);
  expect(object(appended[0]!.entry)).toMatchObject({
    type: 'custom', id: expect.any(String), data: { marker, confirmed: true },
  });
  expect(String(object(appended[0]!.entry).id).trim()).not.toBe('');
  const persistedEntries = sessionEvents(sessionId).filter((event) => {
    const payload = object(event.payload);
    return event.turn_id === dialogTurn && event.event_type === 'engine.diagnostic'
      && payload.event_type === 'pi.rpc' && payload.subtype === 'entry_appended';
  }).map((event) => object(event.payload).raw);
  expect(persistedEntries, 'persist the exact supplier entry event, without inventing a SessionStore file')
    .toEqual(appended);
  observations.push({ appended, persistedEntries });
  const history = await api.getMessages(sessionId, 100);
  expect(history.messages.flatMap((message) => message.blocks ?? [])
    .filter((block) => ['tool_use', 'tool_result'].includes(String(block.type)))).toEqual([]);
  await expect(panel).toHaveCount(0);
  await noFailure(page);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await expect(panel).toHaveCount(0);
  expect((await api.getSession(sessionId)).last_turn_status).toBe('COMPLETED');
  expect(JSON.parse(await api.downloadFileText(sessionId, resultPath, 10_000)))
    .toEqual({ marker, confirmed: true });
  // Cold READY history does not require replaying an already completed live turn.
  await noFailure(page, 'idle');
});
