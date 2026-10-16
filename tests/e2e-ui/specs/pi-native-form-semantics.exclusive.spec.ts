/** Supplier choices and text are data, not platform-authored answer semantics. */
import { spawnSync } from 'node:child_process';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import {
  postgresRows, sessionInputRows, sessionInteractionRows, sessionOutputRows, sessionResponseRows, sqlLiteral,
} from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { currentPiPipe, nativeText, object } from '../fixtures/piChildFailure';
import { piNativeOutput, PiNativeFormScene } from '../fixtures/piNativeFormSemantics';
import { requireSandboxHandle, sandboxExec } from '../fixtures/sandboxOps';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();
let scene: PiNativeFormScene | undefined;
test.beforeEach(() => { scene = undefined; });
onPassOnly(async ({ request }) => {
  if (scene?.agentId) await new AstraApi(request).deleteAgent(scene.agentId);
});
test.afterEach(async ({}, info) => {
  if (scene && ['failed', 'timedOut', 'interrupted'].includes(String(info.status))) {
    await scene.attachFailure(info);
  }
});

test('Pi refuses expired live output without replaying its pending input', async ({ page, request }, info) => {
  const api = new AstraApi(request);
  // A real native select request exceeds execd's 1 MiB retained stdout window.
  // Its handler stays alive awaiting the answer; no synthetic RPC is injected.
  scene = new PiNativeFormScene(api, page, [{ method: 'select', options: ['x'.repeat(1024 * 1024 + 4096)] }]);
  await scene.start(sessions);
  const waiting = await api.waitForSession(scene.sessionId,
    (value) => object(value.pending_interaction?.raw_input).title === scene!.title(0), 15_000);
  const interaction = waiting.pending_interaction!;
  expect(interaction.interaction_id).toBe(object(interaction.raw_input).id);
  expect(object(interaction.raw_input).options).toEqual(scene.steps[0]!.options);
  const inputs = sessionInputRows(scene.sessionId);
  expect(inputs.filter((input) => input.content === '/astrabox-form-semantics')).toEqual([
    expect.objectContaining({ state: 'delivered' }),
  ]);
  const responses = sessionResponseRows(scene.sessionId);
  expect(responses.find((row) => row.response_id === interaction.response_id)?.status).toBe('waiting');
  const [cursor] = postgresRows<{ reader_cursor: unknown }>(
    `SELECT reader_cursor FROM sessions WHERE session_id = ${sqlLiteral(scene.sessionId)}`,
  );
  const native = await api.adminSessionDetail(scene.sessionId);
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const since = new Date().toISOString();
  await api.adminEvictRuntime(scene.sessionId);
  await expect.poll(() => {
    const logs = spawnSync('docker', ['logs', '--since', since, server], {
      encoding: 'utf8', timeout: 10_000, maxBuffer: 8 * 1024 * 1024,
    });
    expect(logs.status).toBe(0);
    const output = `${logs.stdout}${logs.stderr}`;
    return output.includes(`session ${scene!.sessionId} reader failed`) && output.includes('PI_REPLAY_UNAVAILABLE');
  }, { timeout: 20_000, message: 'refuse unsafe observation explicitly instead of replaying or reconciling live Pi' }).toBe(true);
  expect(sessionInputRows(scene.sessionId), 'no delivered input is reclaimed, admitted by guess, or resent').toEqual(inputs);
  expect(sessionResponseRows(scene.sessionId), 'a missing output position must not end live native work').toEqual(responses);
  expect(sessionInteractionRows(scene.sessionId)).toEqual([
    expect.objectContaining({ interaction_id: interaction.interaction_id, status: 'open' }),
  ]);
  expect(postgresRows(`SELECT reader_cursor FROM sessions WHERE session_id = ${sqlLiteral(scene.sessionId)}`))
    .toEqual([cursor]);
  const after = await api.adminSessionDetail(scene.sessionId);
  expect(after.engine_session_key).toBe(native.engine_session_key);
  expect(after.sandbox_id).toBe(native.sandbox_id);
  await expect(page.getByTestId('pending-interaction-panel')).toContainText(scene.title(0));
  await expect(page.getByText(/This turn failed|本轮执行失败/)).toHaveCount(0);
  await info.attach('pi-expired-live-output', {
    body: JSON.stringify({ sessionId: scene.sessionId, cursor, inputs, responses,
      nativeSessionId: native.engine_session_key, interactionId: interaction.interaction_id }),
    contentType: 'application/json',
  });
});

test('Pi durably fails a prompt refused after its receipt wait', async ({ page, request }, info) => {
  const api = new AstraApi(request);
  scene = new PiNativeFormScene(api, page, []);
  await scene.start(sessions);
  await api.waitForSessionReady(scene.sessionId);
  const detail = await api.adminSessionDetail(scene.sessionId);
  const identity = object(detail.runtime_identity);
  const root = String(identity.workspace_dir);
  const home = String(identity.home_dir);
  const handle = await requireSandboxHandle(api, String(detail.sandbox_id));
  const path = `${root}/.pi/extensions/late-refusal.ts`;
  const marker = `LATE_REFUSAL_${scene.marker}`;
  const provider = `late-refusal-${scene.marker.toLowerCase()}`;
  // Pi's supported provider and input APIs produce the real negative receipt:
  // preflight outlives the platform wait, then loses its selected provider.
  const extension = [
    'export default function (pi) {',
    `  const provider = ${JSON.stringify(provider)};`,
    "  pi.registerCommand('arm-late-refusal', { handler: async (_args, ctx) => {",
    "    pi.registerProvider(provider, { baseUrl: 'http://127.0.0.1:9/v1',",
    "      api: 'openai-completions', apiKey: 'test-only-not-a-credential', models: [{",
    "        id: 'preflight-only', name: 'Preflight only', input: ['text'], reasoning: false,",
    '        contextWindow: 8192, maxTokens: 1024,',
    '        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },',
    '      }] });',
    '    await ctx.modelRegistry.refresh({ allowNetwork: false });',
    "    const model = ctx.modelRegistry.find(provider, 'preflight-only');",
    "    if (!model || !(await pi.setModel(model))) throw new Error('native provider was not selected');",
    '  } });',
    "  pi.on('input', async (event, ctx) => {",
    `    if (event.text !== ${JSON.stringify(marker)}) return;`,
    '    await new Promise((resolve) => setTimeout(resolve, 35_000));',
    '    pi.unregisterProvider(provider);',
    '    await ctx.modelRegistry.refresh({ allowNetwork: false });',
    "    return { action: 'continue' };", '  });', '}',
  ].join('\n');
  await api.uploadFileText(scene.sessionId, `${root}/.pi/extensions`, 'late-refusal.ts', extension, 10_000);
  sandboxExec(handle, [
    "python3 - <<'PY'", 'import json', 'from pathlib import Path',
    `p = Path(${JSON.stringify(`${home}/.pi/agent/settings.json`)})`,
    's = json.loads(p.read_text())', `s['extensions'].append(${JSON.stringify(path)})`,
    'p.write_text(json.dumps(s))', 'PY',
  ].join('\n'), 10_000);
  for (const command of ['/reload-runtime', '/arm-late-refusal']) {
    await sendPrompt(page, scene.sessionId, command);
    await expect.poll(() => {
      const states = sessionInputRows(scene!.sessionId).filter((row) => row.content === command).map((row) => row.state);
      return states.length > 0 && states.every((state) => state === 'admitted');
    }, { timeout: 15_000 }).toBe(true);
  }
  const responses = sessionResponseRows(scene.sessionId);
  const started = Date.now();
  await sendPrompt(page, scene.sessionId, marker);
  const own = () => sessionInputRows(scene!.sessionId).filter((row) => row.content === marker);
  await expect.poll(() => own().map((row) => row.state), { timeout: 35_000 }).toEqual(['delivered']);
  await expect.poll(() => own().map((row) => row.state), { timeout: 15_000 }).toEqual(['failed']);
  expect(Date.now() - started, 'the real native refusal arrives after the unchanged 30s receipt wait')
    .toBeGreaterThanOrEqual(30_000);
  const input = own()[0]!;
  const wire = piNativeOutput(handle, currentPiPipe(scene.sessionId)!);
  const refusals = wire.filter((row) => row.type === 'response' && row.command === 'prompt' && row.id === input.input_id);
  expect(refusals).toEqual([expect.objectContaining({ success: false, error: expect.any(String) })]);
  expect(input.failure).toMatchObject({ code: 'ENGINE_INPUT_REFUSED', message: refusals[0]!.error });
  expect(wire.filter((row) => row.type === 'message_start' && object(row.message).role === 'user'
    && nativeText(object(row.message).content) === marker)).toEqual([]);
  expect(sessionResponseRows(scene.sessionId), 'Pi never started a response for rejected preflight').toEqual(responses);
  expect(sessionOutputRows(scene.sessionId).map((row) => row.part)
    .filter((part) => part.type === 'data-input-failed' && object(part.data).input_id === input.input_id))
    .toEqual([expect.objectContaining({ data: expect.objectContaining({ message: refusals[0]!.error }) })]);
  await api.waitForSessionReady(scene.sessionId);
  await expect(page.getByTestId('user-message-failure')).toContainText(String(refusals[0]!.error));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('user-message-failure')).toContainText(String(refusals[0]!.error));
  expect(own()).toEqual([input]);
  await info.attach('pi-late-native-refusal', {
    body: JSON.stringify({ sessionId: scene.sessionId, elapsedMs: Date.now() - started, input, refusals, responses }),
    contentType: 'application/json',
  });
});

test('Pi answers a new message after stopping a native dialog', async ({ page, request }, info) => {
  const api = new AstraApi(request);
  const current = new PiNativeFormScene(api, page, [{ method: 'confirm' }]);
  scene = current;
  await current.start(sessions);
  const pending = await current.question(0);
  const panel = page.getByTestId('pending-interaction-panel');
  const interrupt = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === apiPath(`/sessions/${current.sessionId}/interrupt`));
  await panel.getByRole('button', { name: /^(Stop generating|Stopping|停止生成|停止中)$/i }).click();
  const interrupted = await interrupt;
  expect(interrupted.ok()).toBe(true);
  expect(object(object(await interrupted.json()).data).status, 'the session\'s live reader takes the stop')
    .toBe('accepted');
  const stopped = await api.waitForSession(current.sessionId, (value) => value.state === 'READY'
    && !(value.current_responses ?? []).length && !value.pending_interaction, 30_000);
  expect(stopped.last_response?.response_id).toBe(pending.response_id);
  await expect.poll(() => postgresRows(
    `SELECT kind, state FROM session_controls WHERE session_id = ${sqlLiteral(current.sessionId)}`,
  ), { message: 'the platform handed the stop to Pi' }).toEqual([{ kind: 'interrupt', state: 'done' }]);
  // Pi dismisses confirm with false. The real handler must return and save
  // that value rather than remain blocked after the stop closes the dialog.
  await current.result(0, false);
  const stoppedWire = current.nativeOutput();
  expect(stoppedWire.filter((row) => row.type === 'response' && row.command === 'abort')).toEqual([
    expect.objectContaining({ success: true }),
  ]);
  const stoppedAcks = stoppedWire.filter((row) => row.type === 'response' && row.command === 'prompt');
  expect(stoppedAcks).toHaveLength(2);
  // The command's handler returned with the dismissal: Pi's receipt for the
  // command says so, the response it paused has ended, and the dialog closed
  // without an answer. How that response ended is the person's stop, asserted
  // after the next message below.
  expect(object(stoppedAcks[1]!.data).disposition).toBe('handled');
  expect(sessionResponseRows(current.sessionId).filter((row) => row.response_id === pending.response_id)).toEqual([
    expect.objectContaining({ status: 'ended' }),
  ]);
  expect(sessionInteractionRows(current.sessionId)).toEqual([
    expect.objectContaining({ interaction_id: pending.interaction_id, status: 'withdrawn', answer: null }),
  ]);
  const stoppedFinishes = sessionOutputRows(current.sessionId, String(pending.response_id)).map((row) => row.part)
    .filter((part) => part.type === 'finish').map((part) => part.finishReason);
  expect(stoppedFinishes, 'the response paused once at the dialog and then ended once').toHaveLength(2);
  expect(stoppedFinishes[0]).toBe('tool-calls');
  const stoppedHistory = await api.getMessages(current.sessionId);
  expect(stoppedHistory.messages.filter((message) => message.id === pending.response_id).map((message) => message.metadata))
    .toEqual([expect.objectContaining({ status: 'ended' })]);
  await expect(panel).toHaveCount(0);
  const answer = `AFTER_STOP_${current.marker}`;
  const prompt = `Reply exactly ${answer}. Do not use tools.`;
  await sendPrompt(page, current.sessionId, prompt);
  const inputs = sessionInputRows(current.sessionId).filter((row) => row.content === prompt);
  expect(inputs, 'the next message is one recorded input').toHaveLength(1);
  const reply = await api.waitForReplyTo(current.sessionId, inputs[0]!.input_id, { timeoutMs: 30_000 });
  const completed = await api.waitForSession(current.sessionId, (value) => value.state === 'READY'
    && !(value.current_responses ?? []).length && value.last_response?.response_id === reply.id, 30_000);
  expect(completed.last_response?.outcome).toBe('completed');
  expect(completed.last_error).toBeFalsy();
  const wire = current.nativeOutput();
  const ownInput = wire.filter((row) => row.type === 'message_start'
    && object(row.message).role === 'user' && nativeText(object(row.message).content) === prompt);
  expect(ownInput).toHaveLength(1);
  const replies = wire.slice(wire.indexOf(ownInput[0]!)).filter((row) => row.type === 'message_end'
    && object(row.message).role === 'assistant');
  expect(replies.map((row) => nativeText(object(row.message).content))).toEqual([answer]);
  const history = await api.getMessages(current.sessionId);
  const visible = history.messages.filter((message) => message.role === 'assistant' && messageText(message) === answer);
  expect(visible).toHaveLength(1);
  expect(visible[0]!.id).toBe(reply.id);
  const responses = sessionResponseRows(current.sessionId);
  expect(responses.filter((row) => row.outcome === 'failed'), 'no response fails').toEqual([]);
  expect(responses.filter((row) => row.response_id !== pending.response_id && row.outcome !== 'completed'),
    'every response the person did not stop completes').toEqual([]);
  await expect(page.getByTestId('assistant-text').filter({ hasText: answer })).toBeVisible();
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(panel).toHaveCount(0);
  await expect(page.getByTestId('assistant-text').filter({ hasText: answer })).toBeVisible();
  await info.attach('pi-stopped-dialog-next-input', {
    body: JSON.stringify({ sessionId: current.sessionId, pending, stopped, stoppedHistory, stoppedWire, completed, wire, history,
      responses }),
    contentType: 'application/json',
  });
  // The person's stop ended the response the dialog paused. A stop completes
  // as `cancelled`, and that outcome is what the console reads to say the
  // response stopped early (`MessageParts.tsx`); a stopped response recorded
  // as completed reads as one that finished on its own.
  expect(responses.find((row) => row.response_id === pending.response_id),
    'the response the person stopped is recorded as cancelled').toMatchObject({ status: 'ended', outcome: 'cancelled' });
  await expect(page.getByText(/^(This response stopped early\.|本次回复提前结束。)$/)).toBeVisible();
});

test('Pi select preserves Other and Another option as literal choices', async ({ page, request }) => {
  const options = ['Other', 'Another option'];
  scene = new PiNativeFormScene(new AstraApi(request), page, [
    { method: 'select', options }, { method: 'select', options },
  ]);
  await scene.start(sessions);
  for (const [index, label] of options.entries()) {
    const pending = await scene.question(index);
    const panel = page.getByTestId('pending-interaction-panel');
    const option = panel.getByRole('radio', { name: new RegExp(`^${index + 1}\\.\\s+${label}$`) });
    await option.click();
    await expect(option, 'select the literal supplier choice instead of switching to a custom answer').toBeChecked();
    await expect(panel.getByRole('textbox')).toHaveCount(0);
    await scene.submit(pending, 'option_label', label);
    await scene.result(index, label);
  }
  await scene.finish();
});

test('Pi confirm offers only native boolean answers', async ({ page, request }) => {
  scene = new PiNativeFormScene(new AstraApi(request), page, [{ method: 'confirm' }, { method: 'confirm' }]);
  await scene.start(sessions);
  for (const [index, value] of [true, false].entries()) {
    const pending = await scene.question(index);
    const panel = page.getByTestId('pending-interaction-panel');
    await expect(panel.getByRole('radio'), 'a boolean dialog must not acquire a custom-text answer').toHaveCount(2);
    await expect(panel.getByRole('textbox')).toHaveCount(0);
    const label = value ? 'Yes' : 'No';
    const option = panel.getByRole('radio', { name: new RegExp(`^${value ? 1 : 2}\\.\\s+${label}$`) });
    await option.click();
    await expect(option).toBeChecked();
    await scene.submit(pending, 'option_label', label);
    await scene.result(index, value);
  }
  await scene.finish();
});

test('Pi input preserves whitespace and empty text without cancellation', async ({ page, request }) => {
  scene = new PiNativeFormScene(new AstraApi(request), page, [{ method: 'input' }, { method: 'input' }]);
  await scene.start(sessions);
  for (const [index, value] of ['  exact input value  ', ''].entries()) {
    const pending = await scene.question(index);
    const field = page.getByTestId('pending-interaction-panel').getByRole('textbox');
    await field.fill(value);
    await expect(field).toHaveValue(value);
    await scene.submit(pending, 'free_text', value);
    await scene.result(index, value);
  }
  await scene.finish();
});

test('Pi editor preserves whitespace and empty text without cancellation', async ({ page, request }) => {
  scene = new PiNativeFormScene(new AstraApi(request), page, [{ method: 'editor' }, { method: 'editor' }]);
  await scene.start(sessions);
  for (const [index, value] of ['\n  indented first line\n\tsecond line  \n', ''].entries()) {
    const pending = await scene.question(index);
    const field = page.getByTestId('pending-interaction-panel').getByRole('textbox');
    await field.fill(value);
    await expect(field).toHaveValue(value);
    await scene.submit(pending, 'free_text', value);
    await scene.result(index, value);
  }
  await scene.finish();
});
