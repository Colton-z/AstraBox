/**
 * E2E: a background launcher records its task manifest even when the next turn
 * supersedes it immediately.
 *
 * The two API turns are dispatched back-to-back to make the launcher's terminal
 * snapshot compare-and-swap lose to the next turn. That stale-current-turn path
 * must still record the background manifest. After completion, the Session
 * child-run resource must show Done and root history must contain the user-facing
 * background marker without owning child lifecycle blocks.
 *
 * The composer cannot create this overlap because it waits for a polled ready
 * state before dispatching queued input. Background Agent selection is
 * model-dependent, so the test checks for a child-run record after both sends and
 * skips only when no subagent was launched.
 */
import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi, messageText, type ChildRunRecord, type MessageRecord } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';

const SETTLED_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_SUPERSEDE_SETTLED_TIMEOUT_MS', 180_000);

const LABEL = 'supersede';
const BACKGROUND_SLEEP_SECONDS = 25;
const TERMINAL_STATES = new Set(['RECOVERY_REQUIRED', 'TERMINATED', 'DELETED', 'FAILED']);
const FOREGROUND_IDLE_STATES = new Set(['READY', 'BACKGROUND_RUNNING']);

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function messageIdentity(message: MessageRecord): string {
  const messageId = String(message.message_id || '').trim();
  if (!messageId) throw new Error('messages API returned a row without message_id');
  return messageId;
}

/** Launch exactly one run_in_background Agent, reply immediately, never wait. */
function backgroundAgentPrompt(runId: number | string): string {
  const marker = `BG_${LABEL}_${runId}_1`;
  return [
    `E2E background subagent ${LABEL} ${runId}.`,
    'This is a product regression test. Follow the tool instructions literally.',
    'Task 1: launch one Agent tool call with run_in_background=true.',
    "The background Agent task must use Bash to run exactly: python3 - <<'PY'",
    'import time',
    `time.sleep(${BACKGROUND_SLEEP_SECONDS})`,
    `print("${marker}_DONE")`,
    'PY',
    `After the Bash command completes, the background Agent final summary must contain ${marker}_DONE.`,
    'In the parent turn, do not wait for the background Agent to finish.',
    `Immediately after launching the background Agent call, reply with one short sentence containing PARENT_LAUNCHED_${LABEL}_${runId}.`,
    'The parent turn must not use Bash directly.',
  ].join('\n');
}

function backgroundDoneMarker(runId: number | string): string {
  return `BG_${LABEL}_${runId}_1_DONE`;
}

/** A trivial, fast foreground turn — its only job is to become `current_turn_id`
 *  while the launcher is still projecting its terminal (the supersession race). */
function nextTurnPrompt(runId: number | string): string {
  return `E2E supersede next turn ${runId}: reply with one short sentence containing NEXT_${runId}. Do not use any tools.`;
}

// ── Page helpers ───────────────────────────────────────────────────────────
// A wide viewport keeps the Agents panel in the active layout.
async function openSessionView(page: Page, sessionId: string): Promise<void> {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto(appPath(`/sessions/${sessionId}`), { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
}

async function showAgentsTab(page: Page): Promise<void> {
  // The tab label is built in code, not i18n (`getRightPanelTabLabel` returns
  // 'Agents' / 'Agents (N)'), so this anchor holds in both locales.
  await page.getByRole('tab', { name: /^Agents/ }).click();
  await expect(page.getByTestId('subagent-agents-panel')).toBeVisible({ timeout: 15_000 });
}

function subagentRows(page: Page) {
  return page.getByTestId('subagent-agents-panel').getByTestId('subagent-agent-row');
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('a background launcher superseded by the next turn still materializes its completion', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = Date.now();

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  try {
    await api.waitForSessionReady(sessionId);
    await api.setPermissionMode(sessionId, 'bypassPermissions');

    // The user is looking at their conversation while all of this happens — the
    // console is attached (session stream + detail polling) for the whole race,
    // which is the real-world condition this materialization has to survive.
    await openSessionView(page, sessionId);

    // ── The race: fire the launcher, then fire the next turn the instant its
    //    SSE closes — no settle poll in between (a poll would insert the very
    //    gap that lets the launcher settle normally and hides the defect).
    //    Injected through the API, not the composer: see WHY THE TWO SENDS STAY
    //    ON THE API in the header — the composer cannot emit a zero gap. ──────
    await api.sendTurn(sessionId, backgroundAgentPrompt(runId));
    await api.sendTurn(sessionId, nextTurnPrompt(runId));

    // ── Wait for the session to settle with the background completion
    //    materialized into the Session child-run projection. This is the WAIT, not the
    //    oracle: it decides when the screen is worth reading, and the screen
    //    below is what decides whether the behaviour is right. ───────────────
    let everSawChildRun = false;
    let childRunCompleted = false;
    let lastMessages: MessageRecord[] = [];
    let lastChildRuns: ChildRunRecord[] = [];
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. A declined ask re-sends the same prompt once, then fails.
    await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) { await api.postTurnInput(sessionId, backgroundAgentPrompt(runId)); await api.postTurnInput(sessionId, nextTurnPrompt(runId)); }
      },
      probe: async () => {
        const deadline = Date.now() + SETTLED_TIMEOUT_MS;
        while (Date.now() < deadline) {
          const session = await api.getSession(sessionId);
          if (TERMINAL_STATES.has(String(session.state || ''))) {
            throw new Error(`session ${sessionId} reached terminal state ${session.state}`);
          }
          // Not `page`: that name is the browser fixture this spec now reads from.
          const [messagePage, childRunPage] = await Promise.all([
            api.getMessages(sessionId, 50),
            api.listChildRuns(sessionId),
          ]);
          lastMessages = messagePage.messages || [];
          lastChildRuns = childRunPage.child_runs;
          if (lastChildRuns.length > 0) everSawChildRun = true;
          childRunCompleted = lastChildRuns.some((childRun) => childRun.closed);

          const idle =
            FOREGROUND_IDLE_STATES.has(String(session.state || '')) &&
            !session.current_turn_id &&
            !session.background_task_state;
          if (idle && childRunCompleted) break;
          await sleep(2_000);
        }

        // The gate stays on the durable child-run resource: it asks whether a
        // child run was spawned (model behaviour), not what the console shows.
        // A child run that exists and a panel that then shows nothing fails
        // loudly below; only "never spawned" earns the second ask.
        return everSawChildRun ? true : null;
      },
      what: 'model answered inline without launching a run_in_background Agent — scenario not exercised',
      budgetMs: SETTLED_TIMEOUT_MS * 2,
      probeMs: SETTLED_TIMEOUT_MS,
    });

    expect(
      lastChildRuns.some(
        (childRun) => childRun.closed
          && childRun.engine_kind === 'claude_code'
          && childRun.engine_status === 'completed',
      ),
      'the Claude Agent child should close with the engine\'s exact completed status',
    ).toBe(true);

    const diag = JSON.stringify({
      child_runs: lastChildRuns,
      assistants: lastMessages.filter((m) => m.role === 'assistant').map((m) => ({
        id: messageIdentity(m),
        text: messageText(m).slice(0, 60),
      })),
    });
    expect(
      lastMessages.flatMap((message) => message.blocks || [])
        .filter((block) => String(block.type || '') === 'subagent'),
      'root messages must remain free of child lifecycle and transcript blocks',
    ).toHaveLength(0);

    // ── The user comes back to the conversation to see whether the background
    //    Agent finished. Reload: this is the console rebuilding its child-run
    //    registry from the Session resource, which is the read path anyone
    //    checking on a background result actually takes. ─────────────────────
    await openSessionView(page, sessionId);

    // Both sends are on screen as the user's own messages — the supersession
    // really happened from the user's side (two turns), and the launcher was not
    // swallowed by the turn that superseded it. Matching uses the launcher prompt
    // text rather than the model's wording.
    await expect(
      page.getByTestId('user-message').filter({ hasText: `E2E background subagent ${LABEL} ${runId}` }),
      'the launcher message the user sent should still be in the transcript',
      // Above the 15s expect default: this is the first read after a reload, so
      // it also waits out the history fetch that backs the whole transcript.
    ).toHaveCount(1, { timeout: 30_000 });
    await expect(
      page.getByTestId('user-message').filter({ hasText: `E2E supersede next turn ${runId}` }),
      'the superseding message the user sent should be in the transcript',
    ).toHaveCount(1, { timeout: 30_000 });

    // (1) The background completion survived even though the launcher lost
    //     current_turn_id to the next turn: the independent Agents projection
    //     shows the background Agent's native completed status.
    await showAgentsTab(page);
    await expect
      .poll(async () => subagentRows(page).count(), {
        timeout: 45_000,
        message: `the launched background Agent should render a row in the Agents panel; durable=${diag}`,
      })
      .toBeGreaterThanOrEqual(1);
    // Exact match on the badge, not `hasText` on the row: the row's subtitle can
    // itself contain the word completed. Engine status is carried verbatim.
    await expect(
      page.getByTestId('subagent-agents-panel').getByText(/^completed$/).first(),
      `the superseded launcher's background Agent must reach a completed status in the Agents panel; durable=${diag}`,
    ).toBeVisible({ timeout: 60_000 });

    // (2) The completion marker surfaces in the visible assistant history — the
    //     materialized background summary is text the user can read, not just a
    //     lifecycle badge. Counted, not matched against model prose: the marker
    //     is fixed by the prompt, everything around it is the model's business.
    await expect
      .poll(
        async () => page.getByTestId('assistant-message').filter({ hasText: backgroundDoneMarker(runId) }).count(),
        {
          timeout: 60_000,
          message: `the background completion marker ${backgroundDoneMarker(runId)} must surface in the rendered transcript; durable=${diag}`,
        },
      )
      .toBeGreaterThan(0);

    // A FAILED turn renders its error INTO the transcript as an assistant
    // message. Neither oracle above can be faked by one (a failure bubble
    // carries no child-run completion and not the completion marker), but a
    // launcher or a superseding turn that died on the way is not this scenario
    // — and "the user sees an error where their background result should be"
    // is the same broken screen from where they sit. Cross-check the whole
    // transcript.
    await expect(
      page.getByTestId('assistant-message').filter({ hasText: /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i }),
      'a superseded launcher must not surface to the user as a failed turn',
    ).toHaveCount(0);

    // And the header settles. A conversation that materialized correctly but is
    // left pulsing tells the user their background Agent is still running.
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute('data-pulse', 'false', {
      timeout: 60_000,
    });
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
