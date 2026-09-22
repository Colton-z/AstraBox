/**
 * E2E: a conversation remains sendable while a background Agent is visible in
 * the Agents panel.
 *
 * The launch turn returns while its background task remains open. During that
 * interval the session reports BACKGROUND_RUNNING, the composer accepts a new
 * foreground turn, and the new reply persists without degrading the session.
 * The child-run row and transcript drawer verify the live UI state. The final
 * API projection must return to READY with one completed child run and no open
 * background task.
 *
 * Launching an Agent tool is model-dependent. A bounded probe skips the scenario
 * when no background task or child-run record appears; all assertions after a
 * successful probe remain mandatory.
 */
import { test, expect } from '@playwright/test';
import type { Page, Route } from '@playwright/test';

import {
  AstraApi,
  messageText,
  type ChildRunRecord,
  type MessageRecord,
  type SessionRecord,
} from '../fixtures/astraApi';
import { insist } from '../fixtures/insist';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';

// Sessions created here are deleted only if the test passes; a failure keeps
// the scene and names it in the report tail. This spec's failure IS a
// background-continuation defect whose whole evidence is the session.
const sessions = trackSessions();

// Keep the background child open with a release file until the foreground send
// is observable. A fixed sleep spent most of the suite's 180-second budget
// without proving the overlap; the release gate makes the ordering deterministic.
// Bounded probe: how long to wait for the background task to materialize before
// skipping (the parent turn must launch it AND a child-run/open-state must land).
const PROBE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_BACKGROUND_PROBE_TIMEOUT_MS', 150_000);
// How long to wait for the concurrent foreground reply to persist.
const FOREGROUND_TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
// How long to wait for the background subagent to settle (state READY, no open
// background task, one completed child run).
const BACKGROUND_SETTLED_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_BACKGROUND_SETTLED_TIMEOUT_MS', 180_000);
const BACKGROUND_RELEASE_TIMEOUT_SECONDS = 60;

// A session accepts a foreground turn when it is READY, including the
// BACKGROUND_RUNNING projection used while a background task remains open.
const FOREGROUND_IDLE_STATES = ['READY', 'BACKGROUND_RUNNING'] as const;
// Terminal states derive_ui_state can emit (never expected here).
const TERMINAL_STATES = new Set(['TERMINATED', 'DELETED']);

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** True once the session projects an OPEN background task (BACKGROUND_RUNNING overlay). */
function hasOpenBackgroundTask(session: SessionRecord): boolean {
  const bts = session.background_task_state;
  return Boolean(bts && typeof bts === 'object' && String((bts as Record<string, unknown>).state || '') === 'OPEN');
}

// ── Launch prompt ──────────────────────────────────────────────────────────
function backgroundAgentPrompt(options: { runId: number | string; label: string; releasePath: string }): string {
  const marker = `BG_${options.label}_${options.runId}_1`;
  return [
    `E2E background subagent ${options.label} ${options.runId}.`,
    'This is a product regression test. Follow the tool instructions literally.',
    'Task 1: launch one Agent tool call with run_in_background=true.',
    "The background Agent task must use Bash to run exactly: python3 - <<'PY'",
    'from pathlib import Path',
    'import time',
    `release = Path(${JSON.stringify(options.releasePath)})`,
    `deadline = time.monotonic() + ${BACKGROUND_RELEASE_TIMEOUT_SECONDS}`,
    'while not release.exists():',
    '    if time.monotonic() >= deadline:',
    "        raise TimeoutError('foreground send release did not arrive')",
    '    time.sleep(0.2)',
    'release.unlink(missing_ok=True)',
    `print("${marker}_DONE")`,
    'PY',
    'After the Bash command completes, report whether it succeeded without quoting its stdout.',
    'In the parent turn, do not wait for the background Agent to finish.',
    `Immediately after launching the background Agent call, reply with one short sentence containing PARENT_LAUNCHED_${options.label}_${options.runId}.`,
    'The parent turn must not use Bash directly.',
  ].join('\n');
}

function backgroundReleasePath(runId: number | string): string {
  // Claude Bash and the platform terminal deliberately have separate /tmp
  // views. The conversation workspace is their shared filesystem contract.
  return `/workspace/.astrabox-e2e-sendable-${runId}.release`;
}

function childContentText(block: Record<string, unknown>): string {
  const content = block.content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .map((item) => (typeof item === 'string' ? item : JSON.stringify(item)))
    .join('\n');
}

// ── Page helpers ───────────────────────────────────────────────────────────
async function openSessionView(page: Page, sessionId: string): Promise<void> {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto(appPath(`/sessions/${sessionId}`), { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
}

// Filling avoids key events that could submit a multiline prompt before it is
// complete. Until the engine consumes the input, the platform queue owns it;
// after consumption, the transcript owns it. Either surface confirms that the
// page kept the submission visible.
async function sendPromptFromComposer(page: Page, prompt: string, echoSubstring: string): Promise<void> {
  const composer = page.getByTestId('composer-prompt');
  await expect(composer, 'the composer must be enabled before sending').toBeEnabled({ timeout: 45_000 });
  // .fill() sets the value without key events, so the multi-line launch prompt
  // is not submitted early by an Enter newline.
  await composer.fill(prompt);
  const submit = page.getByTestId('composer-submit');
  await expect(submit).toBeEnabled({ timeout: 15_000 });
  await submit.click();
  const queuedPrompt = page.getByTestId('composer-queue').filter({ hasText: echoSubstring });
  const consumedPrompt = page.getByTestId('user-message').filter({ hasText: echoSubstring }).last();
  await expect(
    queuedPrompt.or(consumedPrompt).first(),
    'the submitted prompt should remain visible in the platform queue or consumed transcript',
  ).toBeVisible({ timeout: 30_000 });
}

async function closePageAfterAssertions(page: Page): Promise<void> {
  await page.goto('about:blank', { waitUntil: 'domcontentloaded', timeout: 5_000 }).catch(() => undefined);
  await page.close({ runBeforeUnload: false }).catch(() => undefined);
}

async function showAgentsTab(page: Page): Promise<void> {
  await page.getByRole('tab', { name: /^Agents/ }).click();
  await expect(page.getByTestId('subagent-agents-panel')).toBeVisible({ timeout: 15_000 });
}

async function expectColdBackgroundStatus(page: Page, sessionId: string, childRunId: string): Promise<void> {
  const detailPath = apiPath(`/sessions/${sessionId}`);
  const childrenPath = apiPath(`/sessions/${sessionId}/child-runs`);
  const matchesRead = (url: URL) => url.pathname === detailPath || url.pathname === childrenPath;
  let releaseDetail!: () => void;
  let releaseChildren!: () => void;
  const detailReleased = new Promise<void>((resolve) => { releaseDetail = resolve; });
  const childrenReleased = new Promise<void>((resolve) => { releaseChildren = resolve; });
  const details: Array<{ status: number; data: SessionRecord }> = [];
  const children: Array<{ status: number; data: { child_runs: ChildRunRecord[] } }> = [];
  const pendingReads: Promise<void>[] = [];
  const readErrors: string[] = [];
  const holdRead = (route: Route) => {
    const pending = (async () => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }
      const response = await route.fetch({ maxRetries: 0, maxRedirects: 0, timeout: 10_000 });
      const body = await response.json();
      if (new URL(route.request().url()).pathname === detailPath) {
        details.push({ status: response.status(), data: body.data as SessionRecord });
        await detailReleased;
      } else {
        children.push({ status: response.status(), data: body.data as { child_runs: ChildRunRecord[] } });
        await childrenReleased;
      }
      await route.fulfill({ response });
    })().catch(async (error: unknown) => {
      readErrors.push(String(error));
      await route.abort('failed').catch(() => undefined);
    });
    pendingReads.push(pending);
    return pending;
  };
  await page.addInitScript(() => {
    const states: string[] = [];
    const capture = () => {
      const state = document.querySelector('[data-testid="run-view"] header [data-testid="status-pill"]')
        ?.getAttribute('data-state');
      if (state && states.at(-1) !== state) states.push(state);
    };
    const observer = new MutationObserver(capture);
    observer.observe(document, { subtree: true, childList: true, attributes: true });
    (window as Window & { __e2eColdBackgroundStates?: () => string[] }).__e2eColdBackgroundStates = () => {
      capture();
      observer.disconnect();
      return states;
    };
  });
  await page.route(matchesRead, holdRead);
  try {
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect.poll(() => {
      expect(readErrors).toEqual([]);
      return details.length;
    }, { message: 'the cold page must request its real session authority' }).toBeGreaterThan(0);
    expect(details[0].status).toBe(200);
    expect(details[0].data.state).toBe('BACKGROUND_RUNNING');
    expect(hasOpenBackgroundTask(details[0].data)).toBe(true);

    // Until session authority arrives, the page cannot claim that work is ready.
    await expect(page.getByText(/Loading session…|正在加载会话/)).toBeVisible();
    await expect(page.getByTestId('run-view')).toHaveCount(0);
    releaseDetail();

    await expect.poll(() => {
      expect(readErrors).toEqual([]);
      return children.length;
    }, { message: 'the cold page must also request its real durable child tree' }).toBeGreaterThan(0);
    expect(children[0].status).toBe(200);
    expect(children[0].data.child_runs.filter((child) => child.child_run_id === childRunId))
      .toEqual([expect.objectContaining({ child_run_id: childRunId, closed: false })]);
    const status = page.getByTestId('run-view').locator('header').getByTestId('status-pill');
    // The tree may arrive later: the session detail already owns background status.
    await expect(status).toHaveText(/Running in background|后台任务运行中/);
    await expect(status).not.toHaveAttribute('data-state', 'READY');
    releaseChildren();
    await Promise.all(pendingReads);
    expect(readErrors).toEqual([]);
    await showAgentsTab(page);
    await expect(page.locator(
      `[data-testid="subagent-agent-row"][data-child-run-id="${childRunId}"]`,
    )).toBeVisible();
    await expect(status).toHaveText(/Running in background|后台任务运行中/);
    const states = await page.evaluate(() => (
      (window as Window & { __e2eColdBackgroundStates?: () => string[] }).__e2eColdBackgroundStates?.()
    ));
    expect(states, 'cold loading and late child delivery must never flash READY').toBeDefined();
    expect(states!.length).toBeGreaterThan(0);
    expect(states).not.toContain('READY');
  } finally {
    releaseDetail();
    releaseChildren();
    await page.unroute(matchesRead, holdRead);
    await Promise.all(pendingReads);
  }
}

function subagentRows(page: Page) {
  return page.getByTestId('subagent-agents-panel').getByTestId('subagent-agent-row');
}

async function expectAgentsPanelRows(page: Page, minCount: number, label: string): Promise<void> {
  await expect
    .poll(async () => subagentRows(page).count(), {
      timeout: 45_000,
      message: `${label} should render subagent rows in the Agents panel`,
    })
    .toBeGreaterThanOrEqual(minCount);
  // The empty-state note must be gone once real rows render. Located by handle
  // rather than by copy: this is an assertion of ABSENCE, which a matcher that
  // has stopped matching anything satisfies for free — a rename would leave it
  // green and meaningless. AgentsListPanel.test.tsx renders both states and
  // proves the handle fires in the one it names, so a nothing-matches handle
  // fails there instead of passing silently here.
  await expect(
    page.getByTestId('subagent-agents-panel').getByTestId('empty-state'),
  ).toHaveCount(0);
}

async function expectLiveSubagentDrawer(page: Page, childRunId: string, label: string) {
  // The API's open child is the lifecycle fact. Engine statuses are deliberately
  // vendor-authored (for example Claude's `task_started`), so matching translated
  // platform status words would make this test reject a correctly projected row.
  const row = page.locator(
    `[data-testid="subagent-agent-row"][data-child-run-id="${childRunId}"]`,
  );
  await expect(row, `${label} should render a live subagent row`).toBeVisible({ timeout: 45_000 });
  await row.click();
  const drawer = page.getByTestId('subagent-transcript-drawer');
  await expect(drawer, `${label} should open the subagent transcript drawer`).toBeVisible({ timeout: 15_000 });
  // The drawer body must render a RECOGNIZED subagent surface, but WHICH one is
  // non-deterministic for a BACKGROUND subagent: its child-run invalidation
  // frames materialize into this already-open page's registry on a cadence that
  // races the UI, so at drawer-open the body may be the live waiting placeholder,
  // the no-transcript placeholder, a streamed text block (SubagentTranscriptDrawer
  // renders it inside the sole `.markdown` wrapper), a thinking block (`p.italic`),
  // or a Bash/python3 tool card (the `.not-prose` Tool collapsible). Match the
  // whole recognized set (both locales) — a blank or broken body matches none. The
  // load-bearing "lifecycle is visible live" fact is the Agents-panel row asserted
  // above; this only guards the drawer body against rendering nothing.
  await expect
    .poll(
      async () => {
        const placeholders = await drawer
          .getByText(/Waiting for subagent output|等待子 Agent 输出|no activity to show yet|暂无可显示的活动/)
          .count();
        const textBlocks = await drawer.locator('.markdown').count();
        const thinkingBlocks = await drawer.locator('p.italic').count();
        const toolCards = await drawer.locator('.not-prose').count();
        return placeholders + textBlocks + thinkingBlocks + toolCards;
      },
      { timeout: 30_000, message: `${label} should render a recognized subagent transcript surface` },
    )
    .toBeGreaterThan(0);
  return drawer;
}

async function waitForOpenChildRun(
  api: AstraApi,
  sessionId: string,
  timeoutMs = 45_000,
): Promise<ChildRunRecord> {
  const deadline = Date.now() + timeoutMs;
  let last: ChildRunRecord[] = [];
  while (Date.now() < deadline) {
    last = (await api.listChildRuns(sessionId)).child_runs;
    const open = last.find((childRun) => !childRun.closed);
    if (open) return open;
    await sleep(1_000);
  }
  throw new Error(
    `Session ${sessionId} did not project an open child run within ${timeoutMs}ms; ` +
      `last=${JSON.stringify(last)}`,
  );
}

// ── Session waiters ────────────────────────────────────────────────────────
async function waitForForegroundIdleOrBackground(
  api: AstraApi,
  sessionId: string,
  timeoutMs = FOREGROUND_TURN_TIMEOUT_MS,
): Promise<SessionRecord> {
  const deadline = Date.now() + timeoutMs;
  let last: SessionRecord | null = null;
  while (Date.now() < deadline) {
    last = await api.getSession(sessionId);
    const state = String(last.state || '');
    if (
      (FOREGROUND_IDLE_STATES as readonly string[]).includes(state) &&
      !last.current_turn_id &&
      !last.pending_interaction
    ) {
      return last;
    }
    if (TERMINAL_STATES.has(state)) {
      throw new Error(`Session ${sessionId} reached terminal ${state} while waiting for foreground-idle; last=${JSON.stringify(last)}`);
    }
    await sleep(1_500);
  }
  throw new Error(`Session ${sessionId} did not return to a foreground-idle state within ${timeoutMs}ms; last=${JSON.stringify(last)}`);
}

async function waitForAssistantTextMatching(
  api: AstraApi,
  sessionId: string,
  pattern: RegExp,
  timeoutMs: number,
): Promise<MessageRecord> {
  const deadline = Date.now() + timeoutMs;
  let lastAssistant = 0;
  while (Date.now() < deadline) {
    const page = await api.getMessages(sessionId, 50);
    const messages = page.messages || [];
    lastAssistant = messages.filter((m) => m.role === 'assistant').length;
    const hit = messages.find((m) => m.role === 'assistant' && pattern.test(messageText(m)));
    if (hit) return hit;
    await sleep(1_500);
  }
  throw new Error(`Session ${sessionId} did not receive assistant text matching ${pattern} within ${timeoutMs}ms (assistants=${lastAssistant})`);
}

async function waitForUserMessageConsumedWhileBackgroundOpen(
  api: AstraApi,
  sessionId: string,
  marker: string,
  timeoutMs = 45_000,
): Promise<SessionRecord> {
  const deadline = Date.now() + timeoutMs;
  let lastSession: SessionRecord | null = null;
  let lastMatches = 0;
  while (Date.now() < deadline) {
    const [session, history] = await Promise.all([
      api.getSession(sessionId),
      api.getMessages(sessionId, 50),
    ]);
    lastSession = session;
    const matches = (history.messages || []).filter((message) => (
      message.role === 'user' && messageText(message).includes(marker)
    ));
    lastMatches = matches.length;
    if (matches.length > 1) {
      throw new Error(`Session ${sessionId} consumed foreground marker ${marker} more than once`);
    }
    if (matches.length === 1 && hasOpenBackgroundTask(session)) {
      return session;
    }
    if (!hasOpenBackgroundTask(session)) {
      throw new Error(
        `Session ${sessionId} closed its background task before consuming foreground marker ${marker}; ` +
          `matches=${matches.length} state=${session.state}`,
      );
    }
    if (TERMINAL_STATES.has(String(session.state || ''))) {
      throw new Error(`Session ${sessionId} reached terminal state while consuming foreground input; last=${JSON.stringify(session)}`);
    }
    await sleep(1_000);
  }
  throw new Error(
    `Session ${sessionId} did not consume foreground marker ${marker} while its background task was open; ` +
      `matches=${lastMatches} last=${JSON.stringify(lastSession)}`,
  );
}

async function waitForBackgroundSettledWithCompletedSubagents(
  api: AstraApi,
  sessionId: string,
  expectedCompletedCount: number,
  timeoutMs = BACKGROUND_SETTLED_TIMEOUT_MS,
): Promise<ChildRunRecord[]> {
  const deadline = Date.now() + timeoutMs;
  let lastSession: SessionRecord | null = null;
  let lastCompleted = 0;
  while (Date.now() < deadline) {
    const [session, childRunPage] = await Promise.all([
      api.getSession(sessionId),
      api.listChildRuns(sessionId),
    ]);
    lastSession = session;
    const closed = childRunPage.child_runs.filter((item) => item.closed);
    lastCompleted = closed.length;
    if (String(lastSession.state || '') === 'READY' && !hasOpenBackgroundTask(lastSession) && lastCompleted >= expectedCompletedCount) {
      return closed;
    }
    if (TERMINAL_STATES.has(String(lastSession.state || ''))) {
      throw new Error(`Session ${sessionId} reached terminal before background settled; last=${JSON.stringify(lastSession)}`);
    }
    await sleep(2_000);
  }
  throw new Error(
    `Session ${sessionId} did not settle background subagents within ${timeoutMs}ms; ` +
      `expected>=${expectedCompletedCount} completed, saw ${lastCompleted}; last_state=${lastSession?.state} bg=${JSON.stringify(lastSession?.background_task_state)}`,
  );
}

/** Bounded deepseek probe: did the launch turn actually produce a background subagent? */
async function probeBackgroundSubagentLaunched(api: AstraApi, sessionId: string, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const session = await api.getSession(sessionId);
    if (hasOpenBackgroundTask(session)) return true;
    if ((await api.listChildRuns(sessionId)).child_runs.length > 0) return true;
    if (TERMINAL_STATES.has(String(session.state || ''))) return false;
    await sleep(2_000);
  }
  return false;
}

test('background Agent keeps the page sendable while the background lifecycle is visible', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const releasePath = backgroundReleasePath(runId);

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  try {
    await api.waitForSessionReady(sessionId);
    // The background Agent's Bash must run unattended — pin bypassPermissions so
    // no tool-permission interaction stalls the background task. (Community
    // conversations already default to bypassPermissions; this makes it explicit.)
    await api.setPermissionMode(sessionId, 'bypassPermissions');

    // ── Launch the background subagent through the real composer. ────────────
    await openSessionView(page, sessionId);
    const launchPrompt = backgroundAgentPrompt({ runId, label: 'sendable', releasePath });
    await sendPromptFromComposer(
      page,
      launchPrompt,
      `E2E background subagent sendable ${runId}`,
    );

    // ── deepseek probe: skip (do not fake-pass) if no background task materializes. ──
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. A declined ask re-sends the same prompt once, then fails.
    const launched = await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(sessionId, launchPrompt);
      },
      probe: async () =>
        (await probeBackgroundSubagentLaunched(api, sessionId, PROBE_TIMEOUT_MS)) ? true : null,
      what: 'deepseek-chat did not launch a background Agent/Task subagent (no background_task_state OPEN and ' + 'no durable child-run record) — there is no visible child run to assert page-sendability against',
      budgetMs: PROBE_TIMEOUT_MS * 2,
      probeMs: PROBE_TIMEOUT_MS,
    });

    // ── The background lifecycle is VISIBLE in the Agents panel (live). ──────
    const liveChild = await waitForOpenChildRun(api, sessionId);
    await showAgentsTab(page);
    await expectAgentsPanelRows(page, 1, 'running background Agent');
    const liveDrawer = await expectLiveSubagentDrawer(
      page,
      liveChild.child_run_id,
      'running background Agent',
    );

    await waitForForegroundIdleOrBackground(api, sessionId);
    await expectColdBackgroundStatus(page, sessionId, liveChild.child_run_id);
    await expectLiveSubagentDrawer(page, liveChild.child_run_id, 'cold running background Agent');

    // ── The session is foreground-idle (READY | BACKGROUND_RUNNING) — the
    //    parent turn returned; the composer is sendable. ──────────────────────
    const backgroundOpen = await waitForForegroundIdleOrBackground(api, sessionId);
    expect(
      (FOREGROUND_IDLE_STATES as readonly string[]).includes(String(backgroundOpen.state || '')),
      `session should be foreground-idle before accepting a foreground message; detail=${JSON.stringify(backgroundOpen)}`,
    ).toBe(true);
    await expect(
      page.getByTestId('composer-prompt'),
      'the page must stay sendable while the background Agent runs',
    ).toBeEnabled({ timeout: 45_000 });

    // ── CORE: a foreground turn dispatched WHILE the background task is OPEN
    //    completes normally. Ask the model NOT to use tools and just echo a
    //    marker (deepseek-reliable), then assert the reply persisted. ─────────
    const foregroundMarker = `FOREGROUND_SENDABLE_${runId}_DONE`;
    const foregroundPromptEcho = `E2E foreground while background ${runId}`;
    await sendPromptFromComposer(
      page,
      `E2E foreground while background ${runId}: 不要使用工具。请回复 ${foregroundMarker}。`,
      foregroundPromptEcho,
    );
    const duringForeground = await waitForUserMessageConsumedWhileBackgroundOpen(
      api,
      sessionId,
      foregroundPromptEcho,
    );
    expect(
      hasOpenBackgroundTask(duringForeground),
      'the engine must consume the foreground message while the background child is still open',
    ).toBe(true);

    // The engine consumed the foreground send while the child was OPEN. Release
    // the child now so its settlement can overlap the foreground model turn.
    await api.runTerminalCommand(sessionId, `touch ${releasePath}`, '/tmp', 30_000);

    const foregroundUser = page.getByTestId('user-message').filter({ hasText: foregroundPromptEcho });
    const [foregroundAssistant] = await Promise.all([
      waitForAssistantTextMatching(
        api,
        sessionId,
        new RegExp(foregroundMarker),
        FOREGROUND_TURN_TIMEOUT_MS,
      ),
      expect(
        foregroundUser,
        'the consumed foreground input must hand over from the queue to one transcript bubble',
      ).toHaveCount(1, { timeout: 45_000 }),
    ]);
    await expect(
      page.getByTestId('composer-queue').filter({ hasText: foregroundPromptEcho }),
      'the platform queue row must disappear after the transcript owns the input',
    ).toHaveCount(0, { timeout: 15_000 });
    expect(messageText(foregroundAssistant).trim(), 'foreground assistant response should not be empty').not.toEqual('');

    // ── The concurrent send must not degrade the session. ───────────────────
    const afterForeground = await waitForForegroundIdleOrBackground(api, sessionId);
    expect(
      (FOREGROUND_IDLE_STATES as readonly string[]).includes(String(afterForeground.state || '')),
      `foreground turn should not leave the session in an abnormal state while background work exists; detail=${JSON.stringify(afterForeground)}`,
    ).toBe(true);
    expect(
      Boolean(afterForeground.runtime_unavailable),
      'foreground send during background should not mark runtime unavailable',
    ).toBe(false);
    expect(
      String(afterForeground.last_error || '').trim(),
      'foreground send during background should not leave a last_error',
    ).toBe('');

    // ── The background subagent eventually settles cleanly (one completed). ──
    const completedChildren = await waitForBackgroundSettledWithCompletedSubagents(api, sessionId, 1);
    expect(
      completedChildren.every(
        (childRun) => childRun.engine_kind === 'claude_code' && childRun.engine_status === 'completed',
      ),
      'the Claude Agent child should close with the engine\'s exact completed status',
    ).toBe(true);
    const backgroundMarker = `BG_sendable_${runId}_1_DONE`;
    const childTranscript = await api.getChildRunMessages(sessionId, liveChild.child_run_id);
    const childBlocks = childTranscript.messages.flatMap((message) => message.content || []);
    const releaseBash = childBlocks.find((block) => {
      const input = block.input;
      return (
        String(block.type || '') === 'tool_use'
        && String(block.name || '') === 'Bash'
        && input != null
        && typeof input === 'object'
        && String((input as Record<string, unknown>).command || '').includes(backgroundMarker)
      );
    });
    expect(
      releaseBash,
      'the child transcript must contain the instructed release-gate Bash call',
    ).toBeTruthy();
    const releaseBashResult = childBlocks.find((block) => (
      String(block.type || '') === 'tool_result'
      && String(block.tool_use_id || '') === String(releaseBash?.id || '')
    ));
    expect(
      releaseBashResult,
      'the release-gate Bash call must have a durable tool result',
    ).toBeTruthy();
    expect(
      releaseBashResult?.is_error,
      `the release-gate Bash call must succeed; result=${JSON.stringify(releaseBashResult)}`,
    ).not.toBe(true);
    expect(
      childContentText(releaseBashResult || {}),
      'the completion marker must come from Bash stdout, not from the Agent summary',
    ).toContain(backgroundMarker);

    const completedBash = liveDrawer.getByRole('button', {
      name: /^Bash\s+(Done|完成)$/,
    }).first();
    await expect(
      completedBash,
      'the drawer opened after parent end_turn must receive the completed Bash round-trip',
    ).toBeVisible({ timeout: 45_000 });
    await completedBash.click();
    await expect(
      completedBash.locator('xpath=..'),
      'the later Bash card must render the stdout completion marker',
    ).toContainText(backgroundMarker, { timeout: 15_000 });
    // Close only after the later marker arrived. Closing immediately after the
    // initial live row would prove that a drawer can open, not that its durable
    // child transcript keeps advancing after the parent turn ends.
    await liveDrawer.getByTestId('subagent-close-button').click();
    await expect(liveDrawer).toHaveCount(0, { timeout: 10_000 });
  } finally {
    await closePageAfterAssertions(page);
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
