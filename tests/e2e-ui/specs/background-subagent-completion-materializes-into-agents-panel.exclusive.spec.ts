/**
 * E2E: completed background Agents materialize in history and the Agents panel.
 *
 * After a launch turn creates background work, the session must return to READY
 * with no background_task_state. Root messages contain the user-facing completion
 * text but no child lifecycle blocks. The independent Session child-run resource
 * must rebuild one row per child with the engine-authored status and open its
 * transcript drawer after reload.
 *
 * Launching a background Agent is model-dependent. A bounded probe skips when no
 * lifecycle evidence appears.
 */
import { test, expect, type Locator, type Page } from '@playwright/test';

import {
  AstraApi,
  messageText,
  type ChildRunRecord,
  type MessageRecord,
  type SessionRecord,
} from '../fixtures/astraApi';
import { insist } from '../fixtures/insist';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';

// Whole-test budget: ready + one real turn + background sleep/settle + reload +
// per-row drawer inspection sit above the 240s suite default; keep it generous
// and env-tunable.
// Bounded probe: how long to wait for the FIRST sign of a background subagent
// (an OPEN background_task_state or any child-run record) before skipping. A
// launched child surfaces in the Session projection promptly; this only covers
// projection lag before concluding the model never used the feature.
const BACKGROUND_PROBE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_BACKGROUND_PROBE_TIMEOUT_MS', 90_000);
// How long to wait for every background completion to materialize + state to clear.
const BACKGROUND_SETTLED_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_BACKGROUND_SETTLED_TIMEOUT_MS', 180_000);

const LABEL = 'materialize';
const TERMINAL_STATES = new Set(['RECOVERY_REQUIRED', 'TERMINATED', 'DELETED', 'FAILED']);

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// A settled drawer shows one of: real transcript content, the no-transcript
// placeholder, or the load-error text. Anything else (the lifecycle-summary
// fallback rendered before the fetch resolves) is still in flight.
const DRAWER_TRANSCRIPT_CONTENT = /Bash|python3|Background task|Agent .* completed|BG_/;
const DRAWER_SETTLED =
  /Bash|python3|Background task|Agent .* completed|BG_|This subagent has no activity to show yet\./;

/**
 * The Agents panel reads the Session child-run index; the drawer fetches the
 * selected child's transcript ON DEMAND (GET
 * /sessions/{id}/child-runs/{childRunId}/messages). Its content arrives
 * one round trip AFTER the drawer becomes visible, and until then the drawer
 * renders the lifecycle summary — which is NOT the loading placeholder, since
 * `loading` is still false on first paint. So poll for a settled state rather
 * than for the placeholder to clear.
 *
 * Times out soft (returns whatever is on screen) so the caller's own assertion
 * reports the real diff instead of this helper masking it.
 */
async function settledDrawerText(
  drawer: Locator,
  page: Page,
  timeoutMs = 20_000,
): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  let text = '';
  for (;;) {
    text = (await drawer.innerText()).replace(/\s+/g, ' ').trim();
    if (DRAWER_SETTLED.test(text) || Date.now() >= deadline) return text;
    await page.waitForTimeout(500);
  }
}

/**
 * The prompt is engineered to be deterministic: launch
 * exactly `count` `run_in_background` Agent tool calls, each running a Bash
 * python3 sleep that prints a unique `BG_<label>_<runId>_<n>_DONE` marker and
 * echoing that marker into the background Agent's final summary, while the
 * PARENT turn replies immediately with a `PARENT_LAUNCHED_<label>_<runId>`
 * sentence and never waits for the background work.
 */
function backgroundAgentPrompt(options: {
  runId: number | string;
  label: string;
  sleepSeconds: number;
  count?: number;
}): string {
  const count = options.count ?? 1;
  const tasks = Array.from({ length: count }, (_, index) => {
    const n = index + 1;
    const marker = `BG_${options.label}_${options.runId}_${n}`;
    return [
      `Task ${n}: launch one Agent tool call with run_in_background=true.`,
      `The background Agent task must use Bash to run exactly: python3 - <<'PY'`,
      'import time',
      `time.sleep(${options.sleepSeconds + index * 4})`,
      `print("${marker}_DONE")`,
      'PY',
      `After the Bash command completes, the background Agent final summary must contain ${marker}_DONE.`,
    ].join('\n');
  }).join('\n\n');
  return [
    `E2E background subagent ${options.label} ${options.runId}.`,
    'This is a product regression test. Follow the tool instructions literally.',
    tasks,
    'In the parent turn, do not wait for any background Agent to finish.',
    `Immediately after launching the background Agent call(s), reply with one short sentence containing PARENT_LAUNCHED_${options.label}_${options.runId}.`,
    'The parent turn must not use Bash directly.',
  ].join('\n');
}

function backgroundDoneMarker(label: string, runId: number | string, index = 1): string {
  return `BG_${label}_${runId}_${index}_DONE`;
}

/**
 * Bounded probe: did the model actually launch a background Agent? True on the
 * first OPEN background_task_state or any child-run record; false only
 * when the budget elapses with the session still healthy (the model answered
 * inline — the skip case). A terminal state here is a genuine anomaly, not a
 * "feature unused" skip, so it throws.
 */
async function probeBackgroundSubagentActivity(
  api: AstraApi,
  sessionId: string,
  timeoutMs: number,
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const session = await api.getSession(sessionId);
    if (session.background_task_state) return true;
    if ((await api.listChildRuns(sessionId)).child_runs.length > 0) return true;
    if (TERMINAL_STATES.has(String(session.state || ''))) {
      throw new Error(`session ${sessionId} reached terminal state ${session.state} during background probe`);
    }
    await sleep(1_500);
  }
  return false;
}

/**
 * Wait for completed children and their user-facing parent replies. A child
 * terminal can precede the parent's subsequent task-notification response.
 */
async function waitForBackgroundSettledWithCompletedSubagents(
  api: AstraApi,
  sessionId: string,
  expectedCompletionMarkers: string[],
  timeoutMs: number,
): Promise<{ session: SessionRecord; messages: MessageRecord[]; childRuns: ChildRunRecord[] }> {
  const deadline = Date.now() + timeoutMs;
  let lastSession: SessionRecord | null = null;
  let lastChildRuns: ChildRunRecord[] = [];
  let missingCompletionMarkers = expectedCompletionMarkers;
  while (Date.now() < deadline) {
    lastSession = await api.getSession(sessionId);
    const [page, childRunPage] = await Promise.all([
      api.getMessages(sessionId, 50),
      api.listChildRuns(sessionId),
    ]);
    lastChildRuns = childRunPage.child_runs;
    const completed = lastChildRuns.filter((item) => item.closed);
    missingCompletionMarkers = expectedCompletionMarkers.filter(
      (marker) => !(page.messages || []).some(
        (message) => message.role === 'assistant' && messageText(message).includes(marker),
      ),
    );
    if (
      String(lastSession.state || '') === 'READY' &&
      !lastSession.background_task_state &&
      completed.length >= expectedCompletionMarkers.length &&
      missingCompletionMarkers.length === 0
    ) {
      return { session: lastSession, messages: page.messages || [], childRuns: lastChildRuns };
    }
    if (TERMINAL_STATES.has(String(lastSession.state || ''))) {
      throw new Error(`session ${sessionId} failed before background settled; last=${JSON.stringify(lastSession)}`);
    }
    await sleep(2_000);
  }
  throw new Error(
    `session ${sessionId} did not settle background subagents; ` +
      `last_state=${lastSession?.state} last_child_runs=${JSON.stringify(lastChildRuns)} ` +
      `missing_parent_markers=${JSON.stringify(missingCompletionMarkers)}`,
  );
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('background subagent completion materializes into the Agents panel and clears background state', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  try {
    await api.waitForSessionReady(sessionId);
    // Set bypassPermissions explicitly so each background Agent's Bash call
    // runs unattended instead of relying on the conversation default.
    await api.setPermissionMode(sessionId, 'bypassPermissions');

    // ── Send the background-subagent prompt through the real composer. ──────
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });

    const parentMarker = `PARENT_LAUNCHED_${LABEL}_${runId}`;
    const prompt = backgroundAgentPrompt({ runId, label: LABEL, sleepSeconds: 8, count: 2 });
    await page.locator('textarea').fill(prompt);
    await page.getByTestId('composer-submit').click();
    const visibleSubmission = page
      .getByTestId('composer-queue')
      .filter({ hasText: parentMarker })
      .or(page.getByTestId('user-message').filter({ hasText: parentMarker }));
    await expect(
      visibleSubmission,
      'a submitted prompt stays visible in either the FIFO or the transcript',
    ).toHaveCount(1, { timeout: 15_000 });

    // The parent turn must return its own short reply without waiting on the
    // background work. Wait on the prompt's PARENT_LAUNCHED marker, not on the
    // assistant-message count: the model may split the turn into a
    // tool_use-only assistant message followed by the text reply, and a
    // count-based wait can observe the empty tool_use message first.
    const parentDeadline = Date.now() + parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
    let parentAssistant: MessageRecord | undefined;
    while (Date.now() < parentDeadline) {
      const messagePage = await api.getMessages(sessionId, 50);
      parentAssistant = (messagePage.messages || []).find(
        (m) => m.role === 'assistant' && messageText(m).includes(parentMarker),
      );
      if (parentAssistant) break;
      await sleep(1_500);
    }
    expect(
      parentAssistant && messageText(parentAssistant).trim(),
      `parent reply should contain ${parentMarker}`,
    ).toBeTruthy();

    // ── PROBE (deepseek): skip if the model launched no background Agent. ────
    // A skip here means exactly one thing now: the model answered inline
    // without spawning a background Agent (model behaviour, not a platform
    // defect). The feature path remains observable here: the engine client
    // emits a neutral background-task manifest, derived from the CLI's
    // `async_launched` answer, and `turn.background_tasks_opened` is written
    // to the journal.
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. A declined ask re-sends the same prompt once, then fails.
    const launched = await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(sessionId, prompt);
      },
      probe: async () =>
        (await probeBackgroundSubagentActivity(api, sessionId, BACKGROUND_PROBE_TIMEOUT_MS)) ? true : null,
      what: `no background subagent activity within ${BACKGROUND_PROBE_TIMEOUT_MS}ms ` + '(no OPEN background_task_state and no child-run record): the model ' + 'answered inline without launching a run_in_background Agent this run.',
      budgetMs: BACKGROUND_PROBE_TIMEOUT_MS * 2,
      probeMs: BACKGROUND_PROBE_TIMEOUT_MS,
    });

    // ── Core invariant: every completion materializes + background clears. ───
    const settled = await waitForBackgroundSettledWithCompletedSubagents(
      api,
      sessionId,
      [backgroundDoneMarker(LABEL, runId, 1), backgroundDoneMarker(LABEL, runId, 2)],
      BACKGROUND_SETTLED_TIMEOUT_MS,
    );
    expect(settled.session.state, 'background completion should return the session to READY').toBe('READY');
    expect(
      settled.session.background_task_state,
      'background task state should clear after materialization',
    ).toBeFalsy();
    expect(
      settled.messages.some(
        (message) =>
          message.role === 'assistant' && messageText(message).includes(backgroundDoneMarker(LABEL, runId, 1)),
      ),
      'assistant history should include background task 1 completion marker',
    ).toBe(true);
    expect(
      settled.messages.some(
        (message) =>
          message.role === 'assistant' && messageText(message).includes(backgroundDoneMarker(LABEL, runId, 2)),
      ),
      'assistant history should include background task 2 completion marker',
    ).toBe(true);
    const completedChildRuns = settled.childRuns.filter((item) => item.closed);
    expect(
      completedChildRuns.length,
      'the Session child-run resource should include every background completion',
    ).toBeGreaterThanOrEqual(2);
    expect(
      completedChildRuns.every(
        (item) => item.engine_kind === 'claude_code' && item.engine_status === 'completed',
      ),
      'Claude Agent children should retain the engine\'s exact completed status',
    ).toBe(true);
    expect(
      settled.messages.flatMap((message) => message.blocks || [])
        .filter((block) => String(block.type || '') === 'subagent'),
      'root message history must not own child-run lifecycle or transcript blocks',
    ).toHaveLength(0);

    // ── UI: reload and assert the completions materialized into the Agents ──
    // panel. A fresh navigation exercises the child-runs API → registry rebuild,
    // so materialization must survive reload, not just a transient invalidation.
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
    await page.getByRole('tab', { name: /^Agents/ }).click();
    const panel = page.getByTestId('subagent-agents-panel');
    await expect(panel).toBeVisible({ timeout: 15_000 });

    const rows = panel.getByTestId('subagent-agent-row');
    await expect
      .poll(async () => rows.count(), {
        timeout: 45_000,
        message: 'completed background Agents should render subagent rows in the Agents panel',
      })
      .toBeGreaterThanOrEqual(2);
    // No empty-state, every completed row retains the engine's exact status,
    // and the background-running header indicator is gone (state cleared).
    await expect(panel.getByTestId('empty-state')).toHaveCount(0);
    for (const childRun of completedChildRuns) {
      const status = childRun.engine_status ?? childRun.engine_reason ?? childRun.engine_event;
      const row = panel.locator(
        `[data-testid="subagent-agent-row"][data-child-run-id="${childRun.child_run_id}"]`,
      );
      await expect(row).toBeVisible({ timeout: 45_000 });
      await expect(row.getByText(status, { exact: true })).toBeVisible();
    }
    await expect(page.getByText('Running in background')).toHaveCount(0);

    // ── Each completed background Agent exposes a real transcript drawer. ────
    const rowCount = await rows.count();
    let meaningfulDrawerCount = 0;
    const inspected: Array<{ row: string; drawer: string }> = [];
    for (let index = 0; index < rowCount; index += 1) {
      const row = rows.nth(index);
      const rowText = (await row.innerText()).replace(/\s+/g, ' ').trim();
      await row.click();
      const drawer = page.getByTestId('subagent-transcript-drawer');
      await expect(drawer, `subagent row ${index + 1} should open the transcript drawer`).toBeVisible({
        timeout: 15_000,
      });
      const drawerText = await settledDrawerText(drawer, page);
      inspected.push({ row: rowText, drawer: drawerText });
      // The column lays its blocks out with a gap; a block that carries a
      // margin utility of its own must not be able to cancel it (Tailwind v4
      // emits `space-y` under `:where()`, which any margin utility outranks —
      // that is how thinking and tool cards came to touch here).
      const touching = await drawer.evaluate((root) => {
        const column = root.querySelector('[data-testid="subagent-transcript-column"]');
        if (!column) return ['no transcript column'];
        const kids = [...column.children].filter((k) => k.getBoundingClientRect().height > 0);
        const found: string[] = [];
        for (let i = 1; i < kids.length; i += 1) {
          const gap = kids[i].getBoundingClientRect().top - kids[i - 1].getBoundingClientRect().bottom;
          if (gap < 4) found.push(`blocks ${i} and ${i + 1} are ${Math.round(gap)}px apart`);
        }
        return found;
      });
      expect(touching, `transcript blocks in drawer ${index + 1} must be spaced apart`).toEqual([]);
      if (
        !drawerText.includes('This subagent has no activity to show yet.') &&
        DRAWER_TRANSCRIPT_CONTENT.test(drawerText)
      ) {
        meaningfulDrawerCount += 1;
      }
      await drawer.getByTestId('subagent-close-button').click();
      await expect(drawer).toHaveCount(0, { timeout: 10_000 });
    }
    expect(
      meaningfulDrawerCount,
      `each completed background Agent should expose a real transcript drawer; inspected=${JSON.stringify(inspected)}`,
    ).toBeGreaterThanOrEqual(2);
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
