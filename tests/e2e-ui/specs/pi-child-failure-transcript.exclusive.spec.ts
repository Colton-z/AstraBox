/** A supplier child failure must not truncate its words or kill the parent relay. */
import { randomUUID } from 'node:crypto';
import { expect, test, type Request } from '@playwright/test';

import { AstraApi, messageText, type ChildRunMessagePage } from '../fixtures/astraApi';
import { engineProfiles } from '../fixtures/engineProfile';
import { apiPath } from '../fixtures/env';
import { nativeRows, type ChildGate } from '../fixtures/nativeChildLifecycle';
import {
  completionNotice, currentPiPipe, directLaunch, finalReplies, gateContents, nativeEntries,
  nativeInspectReplies, nativeSubpath, nativeText, object, obstructCompletionReplay,
  readNativeRun, twoStageChildWorkload, type FailedRun,
} from '../fixtures/piChildFailure';
import { PlatformApi } from '../fixtures/platformApi';
import { requireSandboxHandle } from '../fixtures/sandboxOps';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
let sessionId = '';
let observations: unknown[] = [];
test.beforeEach(() => { sessionId = ''; observations = []; });
test.afterEach(async ({ request, page }, info) => {
  if (!sessionId || !['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  const api = new AstraApi(request);
  const reads = await Promise.allSettled([
    api.getSession(sessionId), api.getMessages(sessionId, 100), aiStreamBodies(page),
    Promise.resolve().then(() => nativeRows(sessionId)),
  ]);
  await info.attach('pi-child-failure-scene', {
    body: JSON.stringify({ sessionId, observations, reads }), contentType: 'application/json',
  });
});

function childText(transcript: ChildRunMessagePage, role: string): string {
  return transcript.messages.filter((message) => message.role === role)
    .map((message) => nativeText(message.content)).join('\n');
}

test('Pi retains a failed direct child transcript and continues its parent after reload', async ({ page, request }) => {
  const profiles = engineProfiles().filter((profile) => profile.engine_kind === 'pi');
  expect(profiles).toHaveLength(1);
  const profile = profiles[0]!;
  expect(profile.contracts.background_subagent).toBe(true);
  const api = new AstraApi(request);
  sessionId = (await api.startConversation(profile.agent_id)).session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  test.info().annotations.push({ type: 'engine_kind', description: 'pi' });
  await api.waitForSessionReady(sessionId);
  if (profile.modes.unattended) await api.setPermissionMode(sessionId, profile.modes.unattended);
  const detail = await api.adminSessionDetail(sessionId);
  const root = String(detail.runtime_identity?.workspace_dir ?? '').replace(/\/+$/, '');
  expect(root).toMatch(/^\//);
  const marker = `RESEARCH_${randomUUID().replaceAll('-', '')}`;
  const receipt = `SOURCE_${randomUUID().replaceAll('-', '')}`;
  const sourceName = `${marker}.txt`;
  await api.uploadFileText(sessionId, root, sourceName,
    `Project receipt: ${receipt}\nA deployment collects agent conversations in a database.\n`, 10_000);
  const task = `For project ${marker}, read ${root}/${sourceName}. Return a brief of at most two plain-text sentences `
    + 'including the actual project receipt and whether the supplied local information can establish the latest release of Pi. '
    + 'Use only available tools. If web tools are unavailable, report that limitation rather than inventing a release. '
    + 'Do not delegate, contact the supervisor, or search other local files.';
  const prompt = `Call subagent exactly once with ${JSON.stringify({ agent: 'researcher', task })}. `
    + 'Use this direct builtin researcher call, not workflowScript. Leave async, tools, model and extensions unspecified. '
    + 'Immediately acknowledge the launch and end your turn without waiting, polling, or reading files yourself. '
    + 'When its automatic completion notification arrives, summarize the actual result in one short sentence, '
    + 'including any failure or limitation it actually reports.';
  await mirrorSseBodies(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  const pendingCatalog = new Set<Request>();
  const catalogRead = (candidate: Request) => candidate.method() === 'GET'
    && !candidate.isNavigationRequest()
    && new URL(candidate.url()).pathname === apiPath(`/sessions/${sessionId}/child-runs`);
  page.on('request', (candidate) => { if (catalogRead(candidate)) pendingCatalog.add(candidate); });
  page.on('requestfinished', (candidate) => pendingCatalog.delete(candidate));
  page.on('requestfailed', (candidate) => pendingCatalog.delete(candidate));
  const firstCatalog = page.waitForResponse((response) => catalogRead(response.request()));
  await openSessionView(page, sessionId);
  await page.getByRole('tab', { name: /^Agents/ }).click();
  const empty = await firstCatalog;
  expect(empty.status()).toBe(200);
  expect(await empty.finished()).toBeNull();
  expect((await empty.json()).data).toMatchObject({ session_id: sessionId, child_runs: [] });
  await expect.poll(() => pendingCatalog.size).toBe(0);
  const panel = page.getByTestId('subagent-agents-panel');
  await expect(panel.getByTestId('subagent-agent-row')).toHaveCount(0);
  await sendPrompt(page, sessionId, prompt);
  await expect.poll(() => directLaunch(sessionId, marker), {
    timeout: 45_000, message: 'the native tool result must identify a real direct background researcher',
  }).not.toBeNull();
  const launch = directLaunch(sessionId, marker)!;
  observations.push({ launch });
  const rows = panel.getByTestId('subagent-agent-row');
  await expect(rows, 'the already-open Agents panel discovers the direct child').toHaveCount(1, { timeout: 15_000 });
  const childId = await rows.getAttribute('data-child-run-id');
  expect(childId).toBeTruthy();
  await rows.click();
  const drawer = page.getByTestId('subagent-transcript-drawer');
  const column = drawer.getByTestId('subagent-transcript-column');
  await expect(drawer).toBeVisible();

  let nativeStatus: FailedRun | undefined;
  await expect.poll(async () => {
    nativeStatus = await readNativeRun(api, sessionId, launch);
    return nativeStatus.state;
  }, { timeout: 60_000, message: 'the real builtin tool allowlist failure must occur' }).toBe('failed');
  expect(nativeStatus!.runId).toBe(launch.runId);
  expect(nativeStatus!.error).toContain("Agent 'researcher' requested unavailable child tools:");
  expect(nativeStatus!.steps).toHaveLength(1);
  expect(nativeStatus!.steps[0]).toMatchObject({ agent: 'researcher', status: 'failed', exitCode: 1 });
  const childSubpath = nativeSubpath(nativeStatus!.steps[0]!.sessionFile);
  expect(nativeSubpath(nativeStatus!.sessionId)).toBe(launch.rootSubpath);
  await expect.poll(() => finalReplies(nativeEntries(sessionId, childSubpath)), {
    timeout: 20_000, message: 'the failed child still has an actual final assistant in database custody',
  }).toHaveLength(1);
  const childEntries = nativeEntries(sessionId, childSubpath);
  const childFinal = finalReplies(childEntries)[0]!;
  expect(childFinal.text).toContain(receipt);
  observations.push({ nativeStatus, childEntries, childFinal });

  await expect(rows.getByText('failed', { exact: true }), 'show the vendor failure, not a stuck running task')
    .toBeVisible({ timeout: 20_000 });
  await expect(rows.locator('svg.animate-spin')).toHaveCount(0);
  await expect(column, 'the already-open drawer receives the final answer even though the task failed')
    .toContainText(receipt, { timeout: 20_000 });
  const children = (await api.listChildRuns(sessionId)).child_runs;
  expect(children).toHaveLength(1);
  expect(children[0]).toMatchObject({ child_run_id: childId, engine_kind: 'pi', engine_status: 'failed',
    task_type: 'subagent', closed: true, active: false });
  const transcript = await api.getChildRunMessages(sessionId, childId!);
  for (const { entry } of childEntries) {
    const message = object(entry.message);
    if (!['user', 'assistant'].includes(String(message.role))) continue;
    const text = nativeText(message.content);
    if (text) expect(childText(transcript, String(message.role)), 'all actual child speech survives its failure').toContain(text);
  }
  expect(childText(transcript, 'assistant')).toContain(childFinal.text);
  await drawer.getByTestId('subagent-close-button').click();

  await expect.poll(() => completionNotice(nativeEntries(sessionId, launch.rootSubpath), nativeStatus!.steps[0]!.sessionFile), {
    timeout: 20_000, message: 'the supplier must actually notify and wake its parent',
  }).toBeTruthy();
  const notice = completionNotice(nativeEntries(sessionId, launch.rootSubpath), nativeStatus!.steps[0]!.sessionFile)!;
  await expect.poll(() => finalReplies(nativeEntries(sessionId, launch.rootSubpath), notice.seq), {
    timeout: 30_000, message: 'native parent completion is separate from the failed child terminal',
  }).toHaveLength(1);
  const parentFinal = finalReplies(nativeEntries(sessionId, launch.rootSubpath), notice.seq)[0]!;
  await expect.poll(async () => (await api.getMessages(sessionId, 100)).messages
    .filter((message) => message.role === 'assistant').map(messageText).join('\n'), {
    timeout: 15_000, message: 'the parent automatic reply must reach the platform, not just the native file',
  }).toContain(parentFinal.text);
  const parentMessage = (await api.getMessages(sessionId, 100)).messages
    .find((message) => message.role === 'assistant' && messageText(message).includes(parentFinal.text));
  expect(parentMessage).toBeDefined();
  const parentRow = page.locator(`[data-message-id="${parentMessage!.message_id}"]`).getByTestId('assistant-text');
  await expect(parentRow, 'the actual automatically awakened parent reply is visible').toBeVisible();
  await expect(parentRow).not.toHaveText('');
  const parentVisible = await parentRow.innerText();
  await api.waitForSession(sessionId, (session) => session.state === 'READY' && !session.current_turn_id, 20_000);

  const followup = `For project ${marker}, explain in one short sentence why a database helps retain conversation history. Do not use tools.`;
  await sendPrompt(page, sessionId, followup);
  await expect.poll(() => nativeEntries(sessionId, launch.rootSubpath).filter(({ entry }) => {
    const message = object(entry.message);
    return message.role === 'user' && nativeText(message.content) === followup;
  }), { timeout: 20_000 }).toHaveLength(1);
  const input = nativeEntries(sessionId, launch.rootSubpath).find(({ entry }) => {
    const message = object(entry.message);
    return message.role === 'user' && nativeText(message.content) === followup;
  })!;
  await expect.poll(() => finalReplies(nativeEntries(sessionId, launch.rootSubpath), input.seq), {
    timeout: 30_000, message: 'the same resident engine must still answer the next user input',
  }).toHaveLength(1);
  const nextFinal = finalReplies(nativeEntries(sessionId, launch.rootSubpath), input.seq)[0]!;
  await expect.poll(async () => (await api.getMessages(sessionId, 100)).messages
    .filter((message) => message.role === 'assistant').map(messageText).join('\n')).toContain(nextFinal.text);
  const nextMessage = (await api.getMessages(sessionId, 100)).messages
    .find((message) => message.role === 'assistant' && messageText(message).includes(nextFinal.text));
  expect(nextMessage).toBeDefined();
  const nextRow = page.locator(`[data-message-id="${nextMessage!.message_id}"]`).getByTestId('assistant-text');
  await expect(nextRow).toBeVisible();
  await expect(nextRow).not.toHaveText('');
  const nextVisible = await nextRow.innerText();
  const settled = await api.waitForSession(sessionId, (session) => session.state === 'READY'
    && session.last_turn_status === 'COMPLETED' && !session.current_turn_id, 20_000);
  expect(settled.background_task_state).toBeFalsy();
  await rows.click();
  await expect(column).toContainText(receipt);
  const visible = await column.innerText();
  observations.push({ transcript, children, notice, parentFinal, nextFinal, parentVisible, nextVisible, settled, visible });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await expect(page.getByTestId('composer-prompt')).toBeEnabled();
  await expect(parentRow).toHaveText(parentVisible, { useInnerText: true });
  await expect(nextRow).toHaveText(nextVisible, { useInnerText: true });
  await page.getByRole('tab', { name: /^Agents/ }).click();
  const coldRow = panel.locator(`[data-child-run-id="${childId}"]`);
  await expect(coldRow.getByText('failed', { exact: true })).toBeVisible();
  await expect(coldRow.locator('svg.animate-spin')).toHaveCount(0);
  await coldRow.click();
  await expect(column).toHaveText(visible, { useInnerText: true });
  expect((await api.getChildRunMessages(sessionId, childId!)).messages).toEqual(transcript.messages);
  expect((await api.listChildRuns(sessionId)).child_runs).toEqual(children);
  const coldHistory = (await api.getMessages(sessionId, 100)).messages;
  for (const reply of [parentFinal, nextFinal]) {
    expect(coldHistory.filter((message) => message.role === 'assistant').map(messageText).join('\n')).toContain(reply.text);
  }
  expect(coldHistory.filter((message) => message.role === 'user' && messageText(message) === followup)).toHaveLength(1);
});

test('Pi survives a real child inspection artifact error and retains subsequent child and parent output', async ({ page, request }) => {
  const profiles = engineProfiles().filter((profile) => profile.engine_kind === 'pi');
  expect(profiles).toHaveLength(1);
  const profile = profiles[0]!;
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  sessionId = (await api.startConversation(profile.agent_id)).session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  test.info().annotations.push({ type: 'engine_kind', description: 'pi' });
  await api.waitForSessionReady(sessionId);
  if (profile.modes.unattended) await api.setPermissionMode(sessionId, profile.modes.unattended);
  const detail = await api.adminSessionDetail(sessionId);
  const identity = object(detail.runtime_identity);
  const root = String(identity.workspace_dir ?? '').replace(/\/+$/, '');
  expect(root).toMatch(/^\//);
  const sandbox = await requireSandboxHandle(api, String(detail.sandbox_id));
  const marker = `INSPECT_${randomUUID().replaceAll('-', '')}`;
  const gates = [1, 2].map((stage) => ({
    marker: `${marker}_${stage}`, started: `${root}/.${marker}.${stage}.started`,
    release: `${root}/.${marker}.${stage}.release`, completed: `${root}/.${marker}.${stage}.completed`,
  })) as [ChildGate, ChildGate];
  const scriptName = `${marker}.py`;
  const workload = twoStageChildWorkload(gates, `${root}/${scriptName}`);
  await api.uploadFileText(sessionId, root, scriptName, workload.script, 10_000);
  const readGate = (index: 0 | 1) => gateContents(api, platform, sessionId, root, gates[index]);
  expect(await readGate(0)).toEqual([null, null, null]);
  expect(await readGate(1)).toEqual([null, null, null]);
  await mirrorSseBodies(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  const pending = new Set<Request>();
  const catalogRead = (candidate: Request) => candidate.method() === 'GET'
    && !candidate.isNavigationRequest()
    && new URL(candidate.url()).pathname === apiPath(`/sessions/${sessionId}/child-runs`);
  page.on('request', (candidate) => { if (catalogRead(candidate)) pending.add(candidate); });
  page.on('requestfinished', (candidate) => pending.delete(candidate));
  page.on('requestfailed', (candidate) => pending.delete(candidate));
  const firstRead = page.waitForResponse((response) => catalogRead(response.request()));
  await openSessionView(page, sessionId);
  await page.getByRole('tab', { name: /^Agents/ }).click();
  const empty = await firstRead;
  expect(empty.status()).toBe(200);
  expect(await empty.finished()).toBeNull();
  expect((await empty.json()).data.child_runs).toEqual([]);
  await expect.poll(() => pending.size).toBe(0);
  await sendPrompt(page, sessionId, workload.prompt);
  // Capture the current process receipt before the parent completes; a later
  // user turn must not manufacture a replacement pipe for this observer.
  await expect.poll(() => currentPiPipe(sessionId), { timeout: 15_000 }).not.toBeNull();
  const pipeId = currentPiPipe(sessionId)!;
  await expect.poll(() => directLaunch(sessionId, gates[0].marker, 'delegate'), { timeout: 30_000 }).not.toBeNull();
  const launch = directLaunch(sessionId, gates[0].marker, 'delegate')!;
  await expect.poll(() => readGate(0), { timeout: 30_000 }).toEqual([gates[0].marker, null, null]);
  const panel = page.getByTestId('subagent-agents-panel');
  const row = panel.getByTestId('subagent-agent-row');
  await expect(row).toHaveCount(1, { timeout: 15_000 });
  const childId = await row.getAttribute('data-child-run-id');
  expect(childId).toBeTruthy();
  await expect(row.locator('svg.animate-spin')).toHaveCount(1);
  await row.click();
  const drawer = page.getByTestId('subagent-transcript-drawer');
  const column = drawer.getByTestId('subagent-transcript-column');
  await expect(column).toContainText(gates[0].marker);
  const before = nativeInspectReplies(sandbox, identity, pipeId, launch.runId);
  expect(before.some((reply) => !reply.error), 'the supplier could inspect this child before the fault').toBe(true);
  const priorRequests = new Set(before.map((reply) => reply.requestId));
  const initialStatus = await readNativeRun(api, sessionId, launch);
  expect(initialStatus.state).toBe('running');
  const childSubpath = nativeSubpath(initialStatus.steps[0]!.sessionFile);
  const fault = await obstructCompletionReplay(api, sessionId, launch);
  observations.push({ launch, pipeId, initialStatus, before, artifactFault: fault.path });
  const firstReceipt = `PHASE_ONE_${randomUUID().replaceAll('-', '')}`;
  let actualError: Record<string, unknown> | undefined;
  try {
    await api.uploadFileText(sessionId, root, gates[0].release.split('/').at(-1)!, firstReceipt, 10_000);
    await expect.poll(() => readGate(1), {
      timeout: 30_000, message: 'a real first tool result must precede the second held tool',
    }).toEqual([gates[1].marker, null, null]);
    expect(await readGate(0)).toEqual([gates[0].marker, firstReceipt, firstReceipt]);
    await expect.poll(() => {
      const replies = nativeInspectReplies(sandbox, identity, pipeId, launch.runId);
      actualError = replies.find((reply) => !priorRequests.has(reply.requestId)
        && object(reply.error).code === 'internal');
      return actualError;
    }, { timeout: 15_000, message: 'the live supplier must emit a correlated internal inspection error' }).toBeTruthy();
    expect(actualError).toMatchObject({ kind: 'pi-subagents.inspect-reply', version: 1, asyncId: launch.runId,
      error: { code: 'internal', message: 'Inspection could not read the async run artifacts.' } });
    expect(actualError!.requestId).toMatch(/^astrabox-[a-z0-9]+$/);
    observations.push({ actualError });
  } finally {
    // Restore only the fixture's empty obstruction, even if the error oracle
    // fails. The scene and genuinely held child remain available for diagnosis.
    await fault.restore();
  }
  expect(await readGate(1)).toEqual([gates[1].marker, null, null]);
  const receipt = `AFTER_INSPECT_${randomUUID().replaceAll('-', '')}`;
  await api.uploadFileText(sessionId, root, gates[1].release.split('/').at(-1)!, receipt, 10_000);
  await expect.poll(() => readGate(1), { timeout: 20_000 }).toEqual([gates[1].marker, receipt, receipt]);
  await expect.poll(() => finalReplies(nativeEntries(sessionId, childSubpath)), { timeout: 30_000 }).toHaveLength(1);
  const childEntries = nativeEntries(sessionId, childSubpath);
  const childFinal = finalReplies(childEntries)[0]!;
  expect(childFinal.text).toContain(firstReceipt);
  expect(childFinal.text).toContain(receipt);
  observations.push({ childEntries, childFinal });
  await expect(row.locator('svg.animate-spin'), 'an inspect read error must not kill the resident output reader')
    .toHaveCount(0, { timeout: 20_000 });
  await expect(column).toContainText(receipt);
  const finalStatus = await readNativeRun(api, sessionId, launch);
  expect(finalStatus.state).toBe('complete');
  const children = (await api.listChildRuns(sessionId)).child_runs;
  expect(children).toHaveLength(1);
  expect(children[0]).toMatchObject({ child_run_id: childId, engine_status: 'complete', closed: true, active: false });
  const transcript = await api.getChildRunMessages(sessionId, childId!);
  for (const { entry } of childEntries) {
    const message = object(entry.message);
    if (!['user', 'assistant'].includes(String(message.role))) continue;
    const text = nativeText(message.content);
    if (text) expect(childText(transcript, String(message.role))).toContain(text);
  }
  const visibleChild = await column.innerText();
  await drawer.getByTestId('subagent-close-button').click();
  await expect.poll(() => completionNotice(nativeEntries(sessionId, launch.rootSubpath), initialStatus.steps[0]!.sessionFile), {
    timeout: 20_000,
  }).toBeTruthy();
  const notice = completionNotice(nativeEntries(sessionId, launch.rootSubpath), initialStatus.steps[0]!.sessionFile)!;
  await expect.poll(() => finalReplies(nativeEntries(sessionId, launch.rootSubpath), notice.seq), { timeout: 30_000 }).toHaveLength(1);
  const parentFinal = finalReplies(nativeEntries(sessionId, launch.rootSubpath), notice.seq)[0]!;
  await expect.poll(async () => (await api.getMessages(sessionId, 100)).messages
    .filter((message) => message.role === 'assistant').map(messageText).join('\n')).toContain(parentFinal.text);
  await api.waitForSession(sessionId, (session) => session.state === 'READY' && !session.current_turn_id, 20_000);
  const followup = `For ${marker}, explain in one short sentence why keeping a conversation history is useful. Do not use tools.`;
  await sendPrompt(page, sessionId, followup);
  await expect.poll(() => nativeEntries(sessionId, launch.rootSubpath).find(({ entry }) => {
    const message = object(entry.message);
    return message.role === 'user' && nativeText(message.content) === followup;
  }), { timeout: 15_000 }).toBeTruthy();
  const input = nativeEntries(sessionId, launch.rootSubpath).find(({ entry }) => {
    const message = object(entry.message);
    return message.role === 'user' && nativeText(message.content) === followup;
  })!;
  await expect.poll(() => finalReplies(nativeEntries(sessionId, launch.rootSubpath), input.seq), { timeout: 30_000 }).toHaveLength(1);
  const nextFinal = finalReplies(nativeEntries(sessionId, launch.rootSubpath), input.seq)[0]!;
  await expect.poll(async () => (await api.getMessages(sessionId, 100)).messages
    .filter((message) => message.role === 'assistant').map(messageText).join('\n')).toContain(nextFinal.text);
  const settled = await api.waitForSession(sessionId, (session) => session.state === 'READY'
    && session.last_turn_status === 'COMPLETED' && !session.current_turn_id, 20_000);
  expect(settled.background_task_state).toBeFalsy();
  observations.push({ transcript, children, parentFinal, nextFinal, finalStatus, settled });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await expect(page.getByTestId('composer-prompt')).toBeEnabled();
  await page.getByRole('tab', { name: /^Agents/ }).click();
  const coldRow = panel.locator(`[data-child-run-id="${childId}"]`);
  await expect(coldRow.locator('svg.animate-spin')).toHaveCount(0);
  await coldRow.click();
  await expect(column).toHaveText(visibleChild, { useInnerText: true });
  expect((await api.getChildRunMessages(sessionId, childId!)).messages).toEqual(transcript.messages);
  expect((await api.listChildRuns(sessionId)).child_runs).toEqual(children);
  const coldHistory = (await api.getMessages(sessionId, 100)).messages;
  for (const reply of [parentFinal, nextFinal]) {
    expect(coldHistory.filter((message) => message.role === 'assistant').map(messageText).join('\n')).toContain(reply.text);
  }
  expect(coldHistory.filter((message) => message.role === 'user' && messageText(message) === followup)).toHaveLength(1);
});
