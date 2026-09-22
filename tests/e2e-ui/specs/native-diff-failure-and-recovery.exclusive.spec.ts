/** Native results, not requested arguments, own Diff and survive compute loss. */
import { execFileSync } from 'node:child_process';
import { expect, test, type Page } from '@playwright/test';

import { AstraApi, type MessagePage } from '../fixtures/astraApi';
import { documentsByField } from '../fixtures/dbOracle';
import { engineCases, engineProfileFor, type EngineProfile } from '../fixtures/engineProfile';
import { browserStreamFrames, object } from '../fixtures/nativeMcpServer';
import { killSandbox, requireSandboxHandle, sandboxRunning, waitForSandboxStopped } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
let ownedAgent = '';
let sessionId = '';
test.beforeEach(() => { ownedAgent = ''; sessionId = ''; });
onPassOnly(async ({ request }) => {
  if (ownedAgent) await new AstraApi(request).deleteAgent(ownedAgent);
});
test.afterEach(async ({ request, page }, info) => {
  if (!['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  const api = new AstraApi(request);
  const reads = await Promise.allSettled([
    api.getSession(sessionId), api.getMessages(sessionId, 50), aiStreamBodies(page),
  ]);
  await info.attach('native-diff-scene', {
    body: JSON.stringify({ sessionId, ownedAgent, reads }), contentType: 'application/json',
  });
});

function changes(history: MessagePage): Record<string, unknown>[] {
  return history.messages.flatMap((message) => (message.blocks || [])
    .filter((block) => block.type === 'ui_data' && object(block.part).type === 'data-file-changes')
    .map((block) => object(block.part)));
}

/** Codex rejects invalid patch context before emitting a fileChange item. */
function codexRejectedPatch(path: string, absent: string): Record<string, unknown>[] {
  const payloads = documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .filter((row) => String(row.subpath).startsWith('codex/'))
    .map((row) => object(JSON.parse(String(row.entry_json))))
    .filter((entry) => entry.type === 'response_item')
    .map((entry) => object(entry.payload));
  const calls = payloads.filter((payload) => {
    if (payload.type !== 'function_call' || payload.name !== 'exec_command') return false;
    const args = object(JSON.parse(String(payload.arguments)));
    return typeof args.cmd === 'string' && args.cmd.includes(`*** Update File: ${path}\n`)
      && args.cmd.includes(`\n-${absent}\n`);
  });
  return calls.flatMap((call) => payloads.filter((payload) =>
    payload.type === 'function_call_output' && payload.call_id === call.call_id
    && payload.output === `apply_patch verification failed: Failed to find expected lines in ${path}:\n${absent}`));
}

function edit(engine: string, path: string, before: string, after: string): string {
  if (engine === 'codex') return [
    'Run exactly this command through your shell tool, with no other write mechanism:',
    "apply_patch <<'PATCH'", '*** Begin Patch', `*** Update File: ${path}`,
    '@@', `-${before}`, `+${after}`, '*** End Patch', 'PATCH',
  ].join('\n');
  if (engine === 'pi') return `Use edit with ${JSON.stringify({ path, edits: [{ oldText: before, newText: after }] })}.`;
  return `Use ${engine === 'claude_code' ? 'Edit' : 'edit'} with ${JSON.stringify({ file_path: path, old_string: before, new_string: after })}.`;
}

async function settledTurn(api: AstraApi, page: Page, prompt: string): Promise<void> {
  const previous = (await api.getSession(sessionId)).last_turn_id;
  await sendPrompt(page, sessionId, prompt);
  await api.waitForSession(sessionId, (session) => session.last_turn_id !== previous
    && session.last_turn_status === 'COMPLETED' && session.state === 'READY', 90_000);
}

async function start(api: AstraApi, page: Page, profile: EngineProfile, cold = false): Promise<string> {
  let agentId = profile.agent_id;
  if (cold) {
    const environments = await api.data<Array<Record<string, unknown>>>('GET', '/admin/environments');
    const environment = environments.find((item) => item.enabled === true
      && item.sandbox_tenancy === 'conversation' && item.engine_kind === profile.engine_kind
      && item.runtime_template_name === profile.image);
    expect(environment, `deployment must provide the matching ${profile.engine_kind} cold Environment`).toBeTruthy();
    const base = await api.getAgent(agentId);
    const agent = await api.createAgent({
      name: `__e2e_native_diff_${profile.engine_kind}_${Date.now()}`,
      model: base.model, engine_options: base.engine_options,
      environment_name: environment!.name, prewarm_enabled: false, diff_panel: true,
    });
    ownedAgent = agent.agent_id;
    agentId = ownedAgent;
  }
  sessionId = (await api.startConversation(agentId)).session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  await api.waitForSessionReady(sessionId);
  const detail = await api.adminSessionDetail(sessionId);
  const root = String(detail.runtime_identity?.workspace_dir || '').replace(/\/+$/, '');
  expect(root).toMatch(/^\//);
  expect(object((await api.getSession(sessionId)).workspace_panels).diff).toBe(true);
  await mirrorSseBodies(page);
  await openSessionView(page, sessionId);
  return root;
}

async function noWorkspacePvc(api: AstraApi, sandboxId: string): Promise<void> {
  const handle = await requireSandboxHandle(api, sandboxId);
  expect(handle.runtime, 'this AWS recovery proof inspects the real Kubernetes mounts').toBe('kubernetes');
  if (handle.runtime !== 'kubernetes') throw new Error('Kubernetes sandbox required');
  const pod = JSON.parse(execFileSync('kubectl', [
    'get', 'pod', handle.pod, '-n', handle.namespace, '-o', 'json',
  ], { encoding: 'utf8', timeout: 15_000 }));
  expect(pod.spec.volumes.filter((volume: Record<string, unknown>) => volume.persistentVolumeClaim),
    'native Diff custody must not use a workspace PVC').toEqual([]);
}

for (const declared of engineCases()) {
  const engine = declared.engine_kind;

  test(`${engine} excludes a failed edit from native Diff after real overwrites`, async ({ page, request }) => {
    const profile = engineProfileFor(engine);
    const api = new AstraApi(request);
    const root = await start(api, page, profile);
    const stamp = Date.now();
    const before = `BEFORE-${stamp}`;
    const after = `AFTER-${stamp}`;
    const overwritten = engine === 'codex' ? `${after}\n` : after;
    const names = [`overwrite-a-${stamp}.txt`, `overwrite-b-${stamp}.txt`];
    const guard = `unchanged-${stamp}.txt`;
    for (const name of [...names, guard]) await api.uploadFileText(sessionId, root, name, `${before}\n`);
    const writes = names.map((name) => engine === 'codex'
      ? edit(engine, `${root}/${name}`, before, after)
      : `Use ${engine === 'claude_code' ? 'Write' : 'write'} with ${JSON.stringify(
        engine === 'pi' ? { path: `${root}/${name}`, content: overwritten }
          : { file_path: `${root}/${name}`, content: overwritten },
      )}.`);
    await settledTurn(api, page, [
      'Replace the complete contents of these two files using the following native tools.',
      'Use the supplied arguments exactly. For Write/write, do not add a newline to the content string.',
      ...writes, 'Read the files first if required. Change no other files; then finish.',
    ].join('\n'));
    for (const name of names) expect(await api.downloadFileText(sessionId, `${root}/${name}`)).toBe(overwritten);
    const diffTab = page.getByTestId('run-view').getByRole('tab', { name: /^Diff\b/ });
    await expect(diffTab).toHaveText('Diff (2)');
    await diffTab.click();
    const diffPanel = page.getByRole('tabpanel', { name: /^Diff\b/ });
    for (const name of names) {
      await diffPanel.getByRole('button', { name: new RegExp(name.replaceAll('.', '\\.')) }).click();
      const content = page.getByTestId('file-diff-content');
      if (engine === 'pi') {
        await expect(content).toContainText('The engine did not provide a before-and-after diff.');
        await expect(content.locator('[data-diff-kind]')).toHaveCount(0);
      } else {
        await expect(content.locator('[data-diff-kind="removed"]')).toContainText(before);
        await expect(content.locator('[data-diff-kind="added"]')).toContainText(after);
      }
    }
    const applied = changes(await api.getMessages(sessionId, 50));
    const callsBefore = new Set((await browserStreamFrames(page)).map((frame) => frame.toolCallId));
    await settledTurn(api, page, [
      'Exercise one expected edit failure. The old text below is deliberately absent.',
      edit(engine, `${root}/${guard}`, `ABSENT-${stamp}`, `MUST-NOT-APPEAR-${stamp}`),
      'Call that tool exactly once with those exact arguments. Do not repair the failure, retry, or use another write tool.',
      'You may read the target first if the edit tool requires it. After the tool rejects the edit, report it and finish.',
    ].join('\n'));
    const frames = (await browserStreamFrames(page)).filter((frame) => !callsBefore.has(frame.toolCallId));
    const failures = frames.filter((frame) => {
      if (frame.type === 'tool-output-error') return true;
      const output = frame.output;
      if (frame.type !== 'tool-output-available' || !output
        || typeof output !== 'object' || Array.isArray(output)) return false;
      return object(output).isError === true || object(output).status === 'failed';
    });
    if (engine === 'codex') {
      await expect.poll(() => codexRejectedPatch(`${root}/${guard}`, `ABSENT-${stamp}`).length,
        { message: 'the native Store must pair the real patch call with its exact rejection' }).toBe(1);
    } else {
      expect(failures.length, 'the real native tool must fail, not merely be declined in model prose').toBeGreaterThan(0);
    }
    expect(await api.downloadFileText(sessionId, `${root}/${guard}`)).toBe(`${before}\n`);
    expect(changes(await api.getMessages(sessionId, 50)), 'failed edit must not publish another applied result').toEqual(applied);
    await expect(diffTab).toHaveText('Diff (2)');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(diffTab).toHaveText('Diff (2)');
    await diffTab.click();
    await expect(diffPanel.getByRole('button', { name: new RegExp(guard) })).toHaveCount(0);
  });

  test(`${engine} retains native Diff after volume-free sandbox replacement`, async ({ page, request }) => {
    const profile = engineProfileFor(engine);
    const api = new AstraApi(request);
    const root = await start(api, page, profile, true);
    const stamp = Date.now();
    const name = `lost-workspace-${stamp}.txt`;
    const before = `ORIGINAL-${stamp}`;
    const after = `CHANGED-${stamp}`;
    await api.uploadFileText(sessionId, root, name, `${before}\n`);
    await settledTurn(api, page, [
      edit(engine, `${root}/${name}`, before, after),
      'You may read that file first if required. Change only this file, then finish.',
    ].join('\n'));
    expect(await api.downloadFileText(sessionId, `${root}/${name}`)).toBe(`${after}\n`);
    const diffTab = page.getByTestId('run-view').getByRole('tab', { name: /^Diff\b/ });
    await expect(diffTab).toHaveText('Diff (1)');
    await diffTab.click();
    const content = page.getByTestId('file-diff-content');
    await expect(content.locator('[data-diff-kind="removed"]')).toContainText(before);
    await expect(content.locator('[data-diff-kind="added"]')).toContainText(after);
    const visible = await content.innerText();
    const applied = changes(await api.getMessages(sessionId, 50));
    expect(applied).toHaveLength(1);
    const oldBox = String((await api.getSession(sessionId)).sandbox_id);
    await noWorkspacePvc(api, oldBox);
    const handle = await requireSandboxHandle(api, oldBox);
    await page.goto('about:blank');
    killSandbox(handle);
    await waitForSandboxStopped(handle, 30_000);
    expect(sandboxRunning(handle)).toBe(false);
    await openSessionView(page, sessionId);
    await expect(diffTab).toHaveText('Diff (1)');
    await diffTab.click();
    await expect(content).toHaveText(visible, { useInnerText: true });
    await settledTurn(api, page, 'Explain briefly what a file diff shows. Do not use tools or modify any files.');
    const replacement = String((await api.getSession(sessionId)).sandbox_id);
    expect(replacement).not.toBe(oldBox);
    expect(replacement).not.toBe('');
    await noWorkspacePvc(api, replacement);
    expect(changes(await api.getMessages(sessionId, 50))).toEqual(applied);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(diffTab).toHaveText('Diff (1)');
    await diffTab.click();
    await expect(content).toHaveText(visible, { useInnerText: true });
    await test.info().attach('native-diff-recovery', {
      body: JSON.stringify({ sessionId, oldBox, replacement, applied, visible }), contentType: 'application/json',
    });
  });
}
