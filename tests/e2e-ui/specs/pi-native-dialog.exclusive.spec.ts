/** Native extension commands can ask a person without owning any model tool call. */
import { randomUUID } from 'node:crypto';
import { expect, test, type Page } from '@playwright/test';

import { AstraApi, messageToolCalls, type SessionRecord } from '../fixtures/astraApi';
import {
  sessionInputRows, sessionInteractionRows, sessionOutputRows, sessionResponseRows, type SessionInputRow,
} from '../fixtures/dbOracle';
import { engineProfiles } from '../fixtures/engineProfile';
import { apiPath } from '../fixtures/env';
import { nativeRows } from '../fixtures/nativeChildLifecycle';
import { currentPiPipe, object } from '../fixtures/piChildFailure';
import { requireSandboxHandle, sandboxExec, type SandboxHandle } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { sessionStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

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
    api.getSession(sessionId), api.getMessages(sessionId, 100), sessionStreamBodies(page),
    Promise.resolve().then(() => sessionInputRows(sessionId)),
    Promise.resolve().then(() => sessionResponseRows(sessionId)),
    Promise.resolve().then(() => sessionInteractionRows(sessionId)),
    Promise.resolve().then(() => nativeRows(sessionId)),
  ]);
  await info.attach('pi-native-dialog-scene', {
    body: JSON.stringify({ sessionId, ownedAgent, observations, reads }), contentType: 'application/json',
  });
});

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
  const bodies = await sessionStreamBodies(page);
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
  expect(frames.filter((frame) => ['error', 'data-input-failed'].includes(String(frame.type))),
    'a transient error is a failure even when the panel later recovers').toEqual([]);
  expect(frames.filter((frame) => String(frame.type).startsWith('tool-')),
    'a native UI dialog is not a fabricated model tool call').toEqual([]);
  expect(sessionResponseRows(sessionId).filter((row) => row.outcome === 'failed')).toEqual([]);
  expect(sessionInputRows(sessionId).filter((row) => row.state === 'failed')).toEqual([]);
  await expect(page.getByTestId('run-view').getByText(/本轮执行失败|This turn failed/i)).toHaveCount(0);
}

/**
 * Wait for Pi to finish an extension command that writes no conversation.
 *
 * Pi answers the command's prompt with a `handled` receipt once its handler
 * returns, and the session's reader admits the input at that receipt
 * (`engine/pi_runtime_client.py`).
 */
async function handledCommand(api: AstraApi, content: string): Promise<SessionInputRow> {
  const command = () => sessionInputRows(sessionId).filter((row) => row.content === content);
  await expect.poll(() => command().map((row) => row.state), {
    timeout: 30_000, message: `Pi must take ${content} with its own receipt`,
  }).toEqual(['admitted']);
  await api.waitForSession(sessionId, (value) => {
    if (value.last_error) throw new Error(`native command failed: ${JSON.stringify(value)}`);
    return value.state === 'READY' && !(value.current_responses ?? []).length && !value.pending_interaction;
  }, 30_000);
  const input = command()[0]!;
  expect(sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(input.input_id)),
    'a command that writes no conversation is answered by no invented response').toEqual([]);
  return input;
}

/** Wait until the response a dialog paused has ended and nothing else runs or waits. */
async function settledOn(api: AstraApi, responseId: string): Promise<SessionRecord> {
  return api.waitForSession(sessionId, (value) => {
    if (value.last_response?.outcome === 'failed' || value.last_error) {
      throw new Error(`native command failed: ${JSON.stringify(value)}`);
    }
    return value.state === 'READY' && !(value.current_responses ?? []).length && !value.pending_interaction
      && value.last_response?.response_id === responseId;
  }, 30_000);
}

test('Pi native command dialog survives reload without inventing a tool call', async ({ page, request }) => {
  const profiles = engineProfiles().filter((profile) => profile.engine_kind === 'pi');
  expect(profiles).toHaveLength(1);
  const profile = profiles[0]!;
  const api = new AstraApi(request);
  const runtime_profiles = await api.data<Array<Record<string, unknown>>>('GET', '/admin/runtime-profiles');
  const environment = runtime_profiles.find((item) => item.enabled === true
    && item.sandbox_tenancy === 'conversation' && item.engine_kind === 'pi'
    && item.runtime_template_name === profile.image);
  expect(environment, 'use the existing deployment-proven cold Pi RuntimeProfile').toBeTruthy();
  const base = await api.getAgent(profile.agent_id);
  const options = object(base.engine_options);
  const settings = object(options.settings);
  const originalExtensions = settings.extensions ?? [];
  expect(Array.isArray(originalExtensions)).toBe(true);
  const reloadExtension = '/opt/pi/node_modules/@earendil-works/pi-coding-agent/examples/extensions/reload-runtime.ts';
  const agent = await api.createAgent({
    name: `__e2e_pi_dialog_${Date.now()}`, model: base.model,
    runtime_profile_name: environment!.name, prepared_capacity: 0,
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
  await sendPrompt(page, sessionId, '/reload-runtime');
  // A supplier slash command ends by its RPC receipt, not by a made-up agent_end.
  const reload = await handledCommand(api, '/reload-runtime');
  await expect.poll(() => currentPiPipe(sessionId), { timeout: 15_000 }).not.toBeNull();
  const pipe = currentPiPipe(sessionId)!;
  const initialNative = nativeOutput(handle, pipe);
  expect(initialNative.filter((row) => row.type === 'response' && row.command === 'get_state')
    .some((row) => row.success === true && object(row.data).sessionId === nativeSessionId),
  'reload must run on the already initialized native conversation').toBe(true);
  const initialAcks = initialNative.filter((row) => row.type === 'response' && row.command === 'prompt');
  expect(initialAcks, 'the receipt Pi wrote for the reload names its platform input').toEqual([expect.objectContaining({
    id: reload.input_id, success: true, data: expect.objectContaining({ disposition: 'handled' }),
  })]);
  observations.push({ reload, initialNative });
  // The reload writes no conversation, so the page's stream has nothing to carry yet.
  await noFailure(page, 'idle');

  await sendPrompt(page, sessionId, '/astrabox-dialog');
  const waiting = await api.waitForSession(sessionId, (value) => {
    if (value.last_response?.outcome === 'failed' || value.last_error) {
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
  const dialogResponse = String(pending.response_id ?? '').trim();
  expect(dialogResponse, 'the dialog pauses a response').not.toBe('');
  expect(sessionInteractionRows(sessionId), 'the platform keeps the dialog open on that response').toEqual([
    expect.objectContaining({ interaction_id: raw.id, response_id: dialogResponse, status: 'open', answer: null }),
  ]);
  expect(object(sessionInteractionRows(sessionId)[0]!.contract).raw_input).toEqual(raw);
  expect(sessionResponseRows(sessionId).filter((row) => row.response_id === dialogResponse))
    .toEqual([expect.objectContaining({ status: 'waiting', outcome: null })]);
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
  expect(historyPending).toMatchObject({ interaction_id: raw.id, response_id: dialogResponse });
  expect(String(historyPending.tool_call_id ?? '')).toBe('');
  const yes = panel.getByRole('radio', { name: /^1\.\s+Yes$/ });
  await yes.click();
  await expect(yes).toBeChecked();
  const answerPath = apiPath(`/sessions/${sessionId}/interactions/${encodeURIComponent(String(raw.id))}/answer`);
  const answerResponse = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === answerPath);
  await panel.getByRole('button', { name: /Submit answer|提交回答/ }).click();
  const answered = await answerResponse;
  expect(answered.status()).toBe(200);
  const answer = object(object(answered.request().postDataJSON()).answer);
  expect(answer.answers, 'the person chose the native Yes').toEqual([{ question_id: raw.id, option_label: 'Yes' }]);
  const settled = await settledOn(api, dialogResponse);
  expect(settled.last_response).toMatchObject({ status: 'ended', outcome: 'completed' });
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
  const appended = finalNative.filter((row) => row.type === 'entry_appended'
    && object(row.entry).customType === 'astrabox-dialog-result');
  expect(appended).toHaveLength(1);
  expect(object(appended[0]!.entry)).toMatchObject({
    type: 'custom', id: expect.any(String), data: { marker, confirmed: true },
  });
  expect(String(object(appended[0]!.entry).id).trim()).not.toBe('');
  observations.push({ appended });
  // The platform's records of the same command: Pi took the person's input with
  // its own receipt, the response the dialog paused ended with Pi's own word for
  // that receipt, and the dialog closed with the answer the person gave.
  const command = sessionInputRows(sessionId).filter((row) => row.content === '/astrabox-dialog');
  expect(command).toEqual([expect.objectContaining({ input_id: acks[1]!.id, state: 'admitted' })]);
  expect(object(acks[1]!.data).disposition).toBe('handled');
  expect(sessionResponseRows(sessionId).filter((row) => row.response_id === dialogResponse)).toEqual([
    expect.objectContaining({ status: 'ended', outcome: 'completed', native_reason: object(acks[1]!.data).disposition }),
  ]);
  expect(sessionInteractionRows(sessionId)).toEqual([
    expect.objectContaining({ interaction_id: raw.id, response_id: dialogResponse, status: 'answered', answer }),
  ]);
  const parts = sessionOutputRows(sessionId, dialogResponse).map((row) => row.part);
  expect(parts.filter((part) => part.type === 'data-interaction').map((part) => object(part.data).interaction_id))
    .toEqual([raw.id]);
  expect(parts.filter((part) => part.type === 'finish').map((part) => part.finishReason),
    'the response pauses at the dialog and ends once, when the command returns').toEqual(['tool-calls', 'other']);
  expect(parts.filter((part) => part.type === 'error' || String(part.type).startsWith('tool-'))).toEqual([]);
  const history = await api.getMessages(sessionId, 100);
  expect(history.messages.flatMap(messageToolCalls), 'a native dialog is not shown as a tool call').toEqual([]);
  await expect(panel).toHaveCount(0);
  await noFailure(page);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await expect(panel).toHaveCount(0);
  expect((await api.getSession(sessionId)).last_response)
    .toMatchObject({ response_id: dialogResponse, outcome: 'completed' });
  expect(JSON.parse(await api.downloadFileText(sessionId, resultPath, 10_000)))
    .toEqual({ marker, confirmed: true });
  // Cold READY history does not require replaying an already completed live response.
  await noFailure(page, 'idle');
});
