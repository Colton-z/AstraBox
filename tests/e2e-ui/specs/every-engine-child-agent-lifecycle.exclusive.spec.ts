/** An already-open Agents panel follows real native children, not only final results. */
import { randomUUID } from 'node:crypto';
import { expect, test, type Page, type Request, type Response } from '@playwright/test';

import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { AstraApi, type ChildRunMessagePage, type ChildRunRecord, type SessionRecord } from '../fixtures/astraApi';
import { expectChildToolCard, expectToolBlocks } from '../fixtures/childToolAssertions';
import { engineCases, engineProfileFor } from '../fixtures/engineProfile';
import { apiPath } from '../fixtures/env';
import { sessionInputRows, sessionResponseRows } from '../fixtures/dbOracle';
import { PlatformApi } from '../fixtures/platformApi';
import {
  childPrompt, childToolEvidence, codexWaits, expectNativeMode, launchEvidence, nativeRows,
  type ChildGate, type ChildMode,
} from '../fixtures/nativeChildLifecycle';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { sessionStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
let sessionId = '';
let observations: unknown[] = [];
test.beforeEach(() => { sessionId = ''; observations = []; });
test.afterEach(async ({ request, page }, info) => {
  if (!sessionId || !['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  const api = new AstraApi(request);
  const reads = await Promise.allSettled([
    api.getSession(sessionId), api.getMessages(sessionId, 50), sessionStreamBodies(page),
    Promise.resolve().then(() => nativeRows(sessionId)),
    Promise.resolve().then(() => sessionInputRows(sessionId)),
    Promise.resolve().then(() => sessionResponseRows(sessionId)),
  ]);
  await info.attach('native-child-lifecycle-scene', {
    body: JSON.stringify({ sessionId, observations, reads }), contentType: 'application/json',
  });
});

function childText(transcript: ChildRunMessagePage, role: string): string {
  return transcript.messages.filter((message) => message.role === role)
    .flatMap((message) => message.content.filter((block) => block.type === 'text')
      .map((block) => String(block.text ?? ''))).join('\n');
}


async function openAgents(page: Page): Promise<void> {
  await page.getByRole('tab', { name: /^Agents/ }).click();
  await expect(page.getByTestId('subagent-agents-panel')).toBeVisible();
}

/** The input id `POST /inputs` answered the page with. */
async function inputIdOf(response: Response): Promise<string> {
  const body = await response.json() as { data?: { input_id?: string } };
  const inputId = String(body.data?.input_id || '');
  expect(inputId, 'the page receives a receipt for its message').not.toEqual('');
  return inputId;
}

const profiles = engineCases().filter((profile) => profile.contracts.background_subagent === true);
const unsupported = engineCases().filter((profile) => profile.contracts.background_subagent !== true)
  .map((profile) => profile.engine_kind);
if (unsupported.length) {
  console.info(`Native child lifecycle: not applicable (capability not declared): ${unsupported.join(', ')}`);
}

for (const declared of profiles) {
  for (const mode of ['foreground', 'background'] as ChildMode[]) {
    test(`${declared.engine_kind} shows a ${mode} child while running and retains its completed transcript after reload`, async ({ page, request }) => {
      const profile = engineProfileFor(declared.engine_kind);
      expect(profile.contracts.background_subagent, `${profile.engine_kind} declares native child execution`).toBe(true);
      const api = new AstraApi(request);
      const platform = new PlatformApi(request);
      sessionId = (await api.startConversation(profile.agent_id)).session_id;
      sessions.push(sessionId);
      test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
      test.info().annotations.push({ type: 'engine_kind', description: profile.engine_kind });
      await api.waitForSessionReady(sessionId);
      if (profile.modes.unattended) await api.setPermissionMode(sessionId, profile.modes.unattended);
      const detail = await api.adminSessionDetail(sessionId);
      const root = String(detail.runtime_identity?.workspace_dir ?? '').replace(/\/+$/, '');
      expect(root, 'use the assigned workspace rather than another Session in the shared box').toMatch(/^\//);
      const marker = `CHILD_${profile.engine_kind}_${randomUUID().replaceAll('-', '')}`;
      const gate: ChildGate = {
        marker, started: `${root}/.${marker}.started`, release: `${root}/.${marker}.release`,
        completed: `${root}/.${marker}.completed`,
      };
      const readGate = async () => {
        // /workspace is Session-local in shared boxes. The file API resolves
        // that namespace; executing at the box root observes a different one.
        const files = await platform.listFiles(sessionId, root, 10_000);
        return await Promise.all([gate.started, gate.release, gate.completed].map((path) =>
          files.entries?.some((entry) => entry.name === path.split('/').at(-1) && entry.kind === 'file')
            ? api.downloadFileText(sessionId, path, 10_000) : null));
      };
      expect(await readGate()).toEqual([null, null, null]);
      await mirrorSseBodies(page);
      await page.setViewportSize({ width: 1440, height: 1000 });
      const browserReads: unknown[] = [];
      const pendingCatalogReads = new Set<Request>();
      const isCatalogRead = (request: Request) => request.method() === 'GET'
        && new URL(request.url()).pathname.endsWith(`/sessions/${sessionId}/child-runs`);
      page.on('request', (request) => {
        if (isCatalogRead(request)) pendingCatalogReads.add(request);
      });
      page.on('requestfinished', (request) => pendingCatalogReads.delete(request));
      page.on('requestfailed', (request) => pendingCatalogReads.delete(request));
      page.on('response', (response) => {
        if (response.request().method() !== 'GET' || !response.url().includes(`/sessions/${sessionId}/child-runs`)) return;
        void response.json().then((body) => browserReads.push({ status: response.status(), body }))
          .catch((error: unknown) => browserReads.push({ error: String(error) }));
      });
      observations.push({ gate, browserReads });
      const initialCatalogRead = page.waitForResponse((response) => isCatalogRead(response.request()));
      await openSessionView(page, sessionId);
      await openAgents(page);
      const initialCatalog = await initialCatalogRead;
      expect(initialCatalog.status(), 'the initial browser child catalog read must succeed').toBe(200);
      expect(await initialCatalog.finished(), 'the empty catalog response must finish before delegation').toBeNull();
      expect((await initialCatalog.json()).data).toMatchObject({ session_id: sessionId, child_runs: [] });
      await expect.poll(() => pendingCatalogReads.size, {
        message: 'no initial browser catalog request may remain in flight when the child launches',
      }).toBe(0);
      const panel = page.getByTestId('subagent-agents-panel');
      const rows = panel.getByTestId('subagent-agent-row');
      await expect(rows).toHaveCount(0);
      const launchInput = await inputIdOf(await sendPrompt(page, sessionId, childPrompt(profile, mode, gate)));

      // File/DB observers do not call child-runs: a test-side catalog read must
      // not repair a missing production discovery notification before the UI.
      await expect.poll(readGate, {
        timeout: 60_000, intervals: [500, 1000],
        message: 'the real child workload must reach its held shell command',
      }).toEqual([marker, null, null]);
      await expect.poll(() => launchEvidence(sessionId, profile, gate), {
        timeout: 15_000, message: 'supplier SessionStore must retain the actual child launcher',
      }).toHaveLength(1);
      const launched = launchEvidence(sessionId, profile, gate);
      observations.push({ launched });
      expectNativeMode(launched, profile, mode);
      await expect.poll(() => rows.count(), {
        timeout: 15_000, message: 'the already-open Agents panel must discover the running child before release',
      }).toBeGreaterThan(0);
      await expect(panel.getByTestId('empty-state')).toHaveCount(0);

      // Pi's native status tree nests the materialized worker as a workflow
      // step. Retain both identities; the step owns the actual child transcript.
      if (profile.engine_kind === 'pi' && mode === 'background') {
        await expect(panel.locator('[data-subagent-task-type="step"]'),
          'the native worker must appear beneath its workflow').toHaveCount(1, { timeout: 15_000 });
        await expect(panel.locator('[data-subagent-task-type="workflow"]'),
          'the native workflow remains visible, not discarded as a duplicate').toHaveCount(1);
      }
      const rowIdentities = await rows.evaluateAll((nodes) => nodes.map((node) => ({
        id: node.getAttribute('data-child-run-id'), depth: Number(node.getAttribute('data-subagent-depth')),
        taskType: node.getAttribute('data-subagent-task-type'),
      })));
      const workers = profile.engine_kind === 'pi' && mode === 'background'
        ? rowIdentities.filter((row) => row.taskType === 'step') : rowIdentities;
      const deepest = Math.max(...workers.map((row) => row.depth));
      const leaves = workers.filter((row) => row.depth === deepest);
      expect(leaves, 'exactly one native worker owns this workload').toHaveLength(1);
      const childId = leaves[0]!.id;
      expect(childId).toBeTruthy();
      const row = panel.locator(`[data-child-run-id="${childId}"]`);
      const transcript = page.getByTestId('run-view').getByTestId('assistant-message');
      const openChild = async () => {
        if (profile.engine_kind === 'claude_code' && mode === 'foreground') {
          // The launch card sits in the turn's process group, which starts
          // closed, and a settled turn also folds that group behind its turn
          // header. The reader opens both to reach the card.
          await expect(transcript.getByTestId(/^assistant-(turn-)?process$/).first()).toBeAttached();
          await revealAssistantProcess(page, { within: transcript });
          const launch = page.getByRole('button', { name: new RegExp(`^${launched[0]!.name} `) });
          await expect(launch).toBeVisible();
          if (await launch.getAttribute('aria-expanded') !== 'true') await launch.click();
          await page.getByRole('button', { name: /^(Open in Agents|在 Agents 中打开)$/ }).click();
        } else {
          await row.click();
        }
      };
      await expect(row.locator('svg.animate-spin'), 'the child must still be running before release').toHaveCount(1);
      const status = page.getByTestId('run-view').locator('header').getByTestId('status-pill');
      if (mode === 'background') {
        // The launch reply has ended while its child is still held: the parent
        // answered, and the person can write again while the child works.
        const launchReply = await api.waitForReplyTo(sessionId, launchInput, { timeoutMs: 20_000 });
        expect(launchReply.metadata?.outcome, 'the launch reply completes without waiting for its child').toBe('completed');
        await expect(page.getByTestId('composer-prompt')).toBeEnabled();
        const background = await api.getSession(sessionId);
        expect(background.current_responses ?? [], 'no response runs while only the child works').toEqual([]);
      } else {
        await expect(status).toHaveAttribute('data-state', 'PROCESSING');
      }
      if (profile.engine_kind === 'codex') {
        if (mode === 'foreground') {
          await expect.poll(() => codexWaits(sessionId, launched[0]!.subpath), {
            timeout: 15_000, message: 'Codex must actually call wait_agent while its child is held',
          }).not.toHaveLength(0);
          const waits = codexWaits(sessionId, launched[0]!.subpath);
          expect(waits.every((wait) => Array.isArray(wait.input.targets) && wait.input.targets.length === 1)).toBe(true);
          observations.push({ waits });
        } else {
          expect(codexWaits(sessionId, launched[0]!.subpath), 'background delegation must not wait on the child').toHaveLength(0);
        }
      }
      expect(await readGate(), 'neither the parent nor the child may release its own work').toEqual([marker, null, null]);
      await openChild();
      const drawer = page.getByTestId('subagent-transcript-drawer');
      await expect(drawer).toBeVisible();
      await expect(drawer.getByTestId('subagent-transcript-column'), 'show the real child task while it is running')
        .toContainText(marker, { timeout: 15_000 });

      // The first test-side child API reads happen only after live UI discovery.
      const running = (await api.listChildRuns(sessionId)).child_runs;
      const liveChild = running.find((child) => child.child_run_id === childId);
      expect(liveChild).toMatchObject({ engine_kind: profile.engine_kind, closed: false, active: true });
      if (profile.engine_kind === 'pi' && mode === 'background') {
        const workflow = running.filter((child) => child.task_type === 'workflow');
        expect(workflow).toHaveLength(1);
        expect(workflow[0]).toMatchObject({ depth: 1, description: 'delegate', operations: ['stop'] });
        expect(liveChild).toMatchObject({
          task_type: 'step', depth: 2, parent_child_run_id: workflow[0]!.child_run_id,
          description: 'child', operations: ['stop'],
        });
      }
      await expect.poll(() => childToolEvidence(sessionId, profile, gate, launched[0]!.subpath), {
        message: 'the child native history must identify the real held tool, independently of its drawer',
      }).toHaveLength(1);
      const liveTool = childToolEvidence(sessionId, profile, gate, launched[0]!.subpath)[0]!;
      observations.push({ liveTool });
      const liveTranscript = await api.getChildRunMessages(sessionId, childId!);
      expect(childText(liveTranscript, 'user'), 'the child transcript must retain its real delegated task').toContain(marker);
      observations.push({ running, liveTranscript, liveStream: await sessionStreamBodies(page) });
      expectToolBlocks(liveTranscript, liveTool);
      await expectChildToolCard(page, liveTool, gate);

      if (mode === 'background') {
        // A cold page must recover background activity from the server's
        // reads — the Session and its child runs — not from the previous
        // tab's streamed child registry or chat state.
        const coldSessionRead = page.waitForRequest((request) => request.method() === 'GET'
          && !request.isNavigationRequest()
          && new URL(request.url()).pathname === apiPath(`/sessions/${sessionId}`));
        const coldCatalogRead = page.waitForResponse((response) => isCatalogRead(response.request()));
        await page.reload({ waitUntil: 'domcontentloaded' });
        const coldResponse = await (await coldSessionRead).response();
        expect(coldResponse, 'the cold page must receive its newly requested Session authority').not.toBeNull();
        expect(coldResponse!.status()).toBe(200);
        expect(await coldResponse!.finished()).toBeNull();
        const coldSession = (await coldResponse!.json()).data as SessionRecord;
        expect(coldSession.current_responses ?? [], 'no response runs while only the child works').toEqual([]);
        expect(coldSession.last_response).toMatchObject({ status: 'ended', outcome: 'completed' });
        await expect(page.getByTestId('run-view')).toBeVisible();
        await expect(page.getByTestId('composer-prompt')).toBeEnabled();
        await openAgents(page);
        const coldCatalog = await coldCatalogRead;
        expect(coldCatalog.status(), 'the cold page must read the child runs from the server').toBe(200);
        expect(await coldCatalog.finished()).toBeNull();
        expect(((await coldCatalog.json()).data.child_runs as ChildRunRecord[])
          .filter((child) => child.child_run_id === childId), 'the cold read reports the held child as working')
          .toEqual([expect.objectContaining({ closed: false, active: true })]);
        await expect(row, 'cold entry must retain the same active child identity').toBeVisible();
        await expect(row.locator('svg.animate-spin')).toHaveCount(1);
        await row.click();
        await expect(drawer.getByTestId('subagent-transcript-column')).toContainText(marker);
        expectToolBlocks(await api.getChildRunMessages(sessionId, childId!), liveTool);
        await expectChildToolCard(page, liveTool, gate);
        expect(await readGate(), 'the child must remain held throughout cold background entry')
          .toEqual([marker, null, null]);
        observations.push({ coldBackgroundSession: coldSession, coldBackgroundStream: await sessionStreamBodies(page) });
      }

      // The receipt is unknown to the model until its actual command reads it.
      const receipt = `RECEIPT_${randomUUID().replaceAll('-', '')}`;
      await api.uploadFileText(sessionId, root, gate.release.split('/').at(-1)!, receipt, 10_000);
      await expect.poll(readGate, { timeout: 20_000, intervals: [500, 1000] })
        .toEqual([marker, receipt, receipt]);
      if (profile.engine_kind === 'deepseek_harness') {
        await expect(row.getByText('inactive', { exact: true }), 'the native child must leave running without a reload')
          .toBeVisible({ timeout: 45_000 });
      }
      await expect(row.locator('svg.animate-spin'), 'the visible child must leave running without a reload')
        .toHaveCount(0, { timeout: 45_000 });
      await expect(drawer.getByTestId('subagent-transcript-column'), 'the already-open child transcript receives its actual result')
        .toContainText(receipt, { timeout: 30_000 });
      if (profile.engine_kind === 'pi' && mode === 'background') {
        await expect.poll(async () => (await api.listChildRuns(sessionId)).child_runs
          .map((child) => ({ kind: child.task_type, active: child.active, closed: child.closed,
            status: child.engine_status, operations: child.operations }))
          .sort((left, right) => String(left.kind).localeCompare(String(right.kind))), {
          timeout: 30_000, message: 'both native workflow and worker must settle before the cold snapshot',
        }).toEqual([
          { kind: 'step', active: false, closed: true, status: 'complete', operations: [] },
          { kind: 'workflow', active: false, closed: true, status: 'complete', operations: [] },
        ]);
      }
      const completed = (await api.listChildRuns(sessionId)).child_runs;
      const finalChild = completed.find((child) => child.child_run_id === childId);
      expect(finalChild?.engine_kind).toBe(profile.engine_kind);
      expect(finalChild?.active, 'native work must settle independently of whether the child can resume').toBe(false);
      if (profile.engine_kind === 'deepseek_harness') {
        expect(['one-shot', 'continuable']).toContain(finalChild!.task_type);
        expect(finalChild!.engine_status).toBe('inactive');
        expect(finalChild!.engine_reason).toBe('completed');
        expect(finalChild!.closed).toBe(finalChild!.task_type === 'one-shot');
      } else {
        expect(finalChild!.closed).toBe(true);
        expect(finalChild!.engine_status).toBe(profile.engine_kind === 'pi' && mode === 'background' ? 'complete' : 'completed');
      }
      const finalTranscript = await api.getChildRunMessages(sessionId, childId!);
      expect(childText(finalTranscript, 'assistant'), 'the receipt belongs to the child answer, not just its input').toContain(receipt);
      await expect.poll(() => childToolEvidence(sessionId, profile, gate, launched[0]!.subpath)
        .filter((tool) => tool.id === liveTool.id && JSON.stringify(tool.result?.output ?? null).includes(receipt)), {
        message: 'the same native child tool must retain the actual released output',
      }).toHaveLength(1);
      const finalTools = childToolEvidence(sessionId, profile, gate, launched[0]!.subpath);
      expect(finalTools).toHaveLength(1);
      const finalTool = finalTools[0]!;
      observations.push({ finalTool });
      expect(finalTool.id).toBe(liveTool.id);
      expect(finalTool.subpath).toBe(liveTool.subpath);
      expectToolBlocks(finalTranscript, finalTool, receipt);
      await expectChildToolCard(page, finalTool, gate, receipt);
      const settled = await api.waitForSession(sessionId, (session) => session.state === 'READY'
        && session.last_response?.outcome === 'completed' && (session.current_responses ?? []).length === 0, 30_000);
      expect((await api.listChildRuns(sessionId)).child_runs.filter((child) => child.active),
        'settled native work must leave no child reported as working').toEqual([]);
      await expect(status, 'the live header must stop reporting work after native activity settles')
        .toHaveAttribute('data-state', 'READY');
      await expect(page.getByTestId('composer-prompt')).toBeEnabled();
      const visible = await drawer.getByTestId('subagent-transcript-column').innerText();
      observations.push({ completed, finalTranscript, settled, visible, stream: await sessionStreamBodies(page) });
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page.getByTestId('run-view')).toBeVisible();
      await expect(status).toHaveAttribute('data-state', 'READY');
      await expect(page.getByTestId('composer-prompt')).toBeEnabled();
      await openAgents(page);
      await expect(row).toBeVisible();
      if (profile.engine_kind === 'deepseek_harness') await expect(row.getByText('inactive', { exact: true })).toBeVisible();
      await expect(row.locator('svg.animate-spin')).toHaveCount(0);
      await openChild();
      await expectChildToolCard(page, finalTool, gate, receipt);
      await expect(drawer.getByTestId('subagent-transcript-column')).toHaveText(visible, { useInnerText: true });
      const coldTranscript = await api.getChildRunMessages(sessionId, childId!);
      expectToolBlocks(coldTranscript, finalTool, receipt);
      expect(coldTranscript.messages).toEqual(finalTranscript.messages);
      const coldChildren = (await api.listChildRuns(sessionId)).child_runs;
      // DSH may dispose the live child after turn/end. That advances its last
      // native event without changing the completed, continuable child or its
      // durable history. Every other field must survive reload unchanged.
      const retainedChildren = completed.map((child) => {
        if (child.engine_kind !== 'deepseek_harness') return child;
        expect(child.engine_event).toMatch(/^(turn\/end|api-session\/removed)$/);
        return { ...child, engine_event: expect.stringMatching(/^(turn\/end|api-session\/removed)$/) };
      });
      expect(coldChildren).toEqual(retainedChildren);
      const coldSettled = await api.getSession(sessionId);
      expect(coldSettled.state).toBe('READY');
      expect(coldSettled.current_responses ?? []).toEqual([]);
      expect(coldChildren.filter((child) => child.active), 'no child is reported as working after reload').toEqual([]);
    });
  }
}
