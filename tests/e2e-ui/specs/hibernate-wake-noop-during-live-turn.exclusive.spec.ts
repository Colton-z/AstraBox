/**
 * E2E: Agent hibernate and wake calls do not disturb a running response or its
 * per-session sandbox.
 *
 * A message sent from the composer starts a Bash command that waits on a
 * workspace release file. Before the lifecycle calls, the started marker and
 * the platform's records (`session_responses`, the parts relayed for the
 * response in `session_output`) must identify a running response that has
 * announced its Bash call. After both calls, the same response must still be
 * running on the same sandbox. Releasing the command must then deliver its real
 * result to the page without a failure card. The records carry response and
 * sandbox identity because the page does not render those identifiers.
 *
 * A bounded probe must observe the requested Bash command starting; otherwise
 * the test fails because the running-response scenario was not exercised.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, type InputReceipt } from '../fixtures/astraApi';
import { openLiveProcessGroup, revealAssistantProcess } from '../fixtures/assistantProcess';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { oracleDbPath, sessionOutputRows, sessionResponseRows } from '../fixtures/dbOracle';
import { expectPromptDelivered, startPromptDelivery } from '../fixtures/sessionPage';

// Bounded probe: how long to wait for the response to be running with a relayed
// Bash call and its workspace started marker before failing. The session is
// already READY, so this only covers the model entering the command.
const PROBE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_HIBERNATE_LIVE_PROBE_TIMEOUT_MS', 60_000);
// After the pokes, let the released response settle to READY so the session
// deletes cleanly.
const SETTLE_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_HIBERNATE_LIVE_SETTLE_TIMEOUT_MS', 180_000);
// Fail loud if a retained command is never released. The normal path releases it
// immediately after the lifecycle assertions, so this is not test latency.
const GATE_TIMEOUT_SECONDS = parseTimeoutEnv('ASTRABOX_E2E_HIBERNATE_LIVE_GATE_TIMEOUT_S', 120);
// How long the screen gets to keep up with the platform before "the run still
// reads live / the command result arrived" counts as a lie. Generous on purpose:
// it is only ever spent in full when the screen is genuinely dead.
const PAGE_RENDER_MS = parseTimeoutEnv('ASTRABOX_E2E_HIBERNATE_LIVE_PAGE_RENDER_MS', 60_000);

const FILE_NOT_FOUND = /404|FILE_NOT_FOUND|path not found/i;
const BASH_CARD = /^Bash\b/;

// What "this turn was killed under me" looks like on screen. A platform-failed
// turn renders INTO the transcript as an assistant message — the TurnFailureCard
// (chat:result.turn_failed / turn_failed_with_detail). The console is bilingual and
// the runner's locale is unpinned, so both languages are matched, together with the
// engine/runtime error strings every reply assertion in this suite carries: a
// failed turn's error text arrives in a bubble too, so "the transcript still looks
// fine" must not be satisfied by exactly the failure under test.
const TURN_FAILED_ON_SCREEN =
  /this turn failed|本轮执行失败|这一轮执行失败|API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

/** The response answering one input, as the platform recorded it. */
interface LiveResponseState {
  responseId: string;
  /** `running`, `waiting` or `ended` (`session_responses.status`). */
  status: string;
  outcome: string | null;
  /** How many parts the reader relayed for the response. */
  partCount: number;
  /** Whether one of those parts announced a Bash call. */
  bashAnnounced: boolean;
}

/**
 * Read the response answering `inputId` from the platform's own records: its
 * row in `session_responses` and the parts relayed for it in `session_output`.
 * `null` until the runtime has started a response for the input.
 */
function readLiveResponse(sessionId: string, inputId: string): LiveResponseState | null {
  const response = sessionResponseRows(sessionId).find((row) => row.input_ids.includes(inputId));
  if (!response) return null;
  const parts = sessionOutputRows(sessionId, response.response_id).map((row) => row.part);
  return {
    responseId: response.response_id,
    status: response.status,
    outcome: response.outcome,
    partCount: parts.length,
    bashAnnounced: parts.some((part) => (
      String(part.type ?? '') === 'tool-input-available'
      && String(part.toolName ?? '') === 'Bash'
    )),
  };
}

/**
 * Poll until the requested Bash command has entered its held workspace gate;
 * the running response's id, or null when the response ended without it.
 */
async function waitForHeldBashResponse(
  api: AstraApi,
  sessionId: string,
  inputId: string,
  startedPath: string,
  timeoutMs: number,
): Promise<string | null> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const state = readLiveResponse(sessionId, inputId);
    if (state?.status === 'ended') return null;
    if (state && state.status === 'running' && state.bashAnnounced) {
      try {
        const marker = await api.downloadFileText(sessionId, startedPath, 15_000);
        if (marker.trim() === 'started') {
          const confirmed = readLiveResponse(sessionId, inputId);
          if (confirmed?.responseId === state.responseId && confirmed.status === 'running') {
            return state.responseId;
          }
        }
      } catch (error) {
        if (!FILE_NOT_FOUND.test(String((error as Error)?.message ?? error))) throw error;
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
  return null;
}

/** Real command-side barrier: the turn cannot finish until the test releases it. */
function heldBashPrompt(
  runId: string,
  startedPath: string,
  releasePath: string,
  resultMarker: string,
): string {
  const command = [
    "python3 - <<'PY'",
    'import time',
    'from pathlib import Path',
    `started = Path(${JSON.stringify(startedPath)})`,
    `release = Path(${JSON.stringify(releasePath)})`,
    "started.write_text('started', encoding='utf-8')",
    `deadline = time.monotonic() + ${GATE_TIMEOUT_SECONDS}`,
    'while not release.is_file():',
    '    if time.monotonic() >= deadline:',
    "        raise RuntimeError('E2E hibernate/wake release marker timed out')",
    '    time.sleep(0.1)',
    // OpenSandbox execd exposes an uploaded file before applying its requested
    // owner and mode. Leave the marker in place until sandbox teardown: deleting
    // it here can release the command but make the still-finishing upload return
    // 500 from its permission step.
    `print(${JSON.stringify(resultMarker)})`,
    'PY',
  ].join('\n');
  return [
    `E2E hibernate live no-op ${runId}.`,
    'Use the Bash tool exactly once to run this command verbatim:',
    '```bash',
    command,
    '```',
    'Do not use any other tool. Wait for Bash before replying.',
    `After it finishes, reply with exactly ${resultMarker}.`,
  ].join('\n');
}

// Teardown that changes state runs on the passing path only, in three hooks
// whose registration order IS the old `finally` order: afterEach hooks run in
// the order they are registered. Settle first — a DELETE while the runtime
// reports it is working answers SESSION_BUSY — then the session, then the agent
// it ran on.
//
// Settle a still-running response so the session deletes cleanly. Interrupt is
// the graceful signal; evicting the in-memory runtime makes the session's reader
// reconnect and read the runtime's own state again (harmless no-op if the
// response already settled). Ordered
// interrupt → evict → wait-READY → delete session → delete agent. Stays on the
// API: no user has an evict-runtime button, and the page's own stream dies with
// the browser context, so there is nothing left to drain.
let sessionId = '';
let agentId = '';
onPassOnly(async ({ request }) => {
  if (!sessionId) return;
  const api = new AstraApi(request);
  await api.interruptSession(sessionId).catch(() => {});
  await api.adminEvictRuntime(sessionId).catch(() => {});
  await api.waitForSessionState(sessionId, 'READY', SETTLE_BUDGET_MS).catch(() => {});
});
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('agent hibernate/wake are no-ops that do not disturb a live durable turn', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  const startedName = `.astrabox-e2e-hibernate-live-${runId}.started`;
  const releaseName = `.astrabox-e2e-hibernate-live-${runId}.release`;
  const startedPath = `/workspace/${startedName}`;
  const releasePath = `/workspace/${releaseName}`;
  const resultMarker = `HIBERNATE_LIVE_CONTINUED_${runId}`;

  // Fail fast (with the descriptive oracle error) if the database is not
  // reachable — the running-response observation depends on it.
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });

  // Scoped to run-view on purpose. Every session row in the sidebar renders a
  // status-pill of its own and the sidebar precedes the content in the DOM, so an
  // unscoped `.first()` reads some OTHER conversation's pill — which is idle by
  // definition and would answer "is this run live?" for free.
  const headerPill = page.getByTestId('run-view').getByTestId('status-pill').first();
  const stopControl = page.getByTestId('run-composer-stop');
  const failureOnScreen = page.getByTestId('run-view').getByText(TURN_FAILED_ON_SCREEN);
  const bashCard = page.getByTestId('assistant-message').getByRole('button', { name: BASH_CARD }).first();

  /** Is the response this spec is about still genuinely running, per the
   *  platform's records? Keeps the page assertions race-free: a response that
   *  finishes on its own mid-check has nothing left to render as live. */
  const responseStillRunning = (responseId: string): boolean => (
    sessionResponseRows(sessionId).find((row) => row.response_id === responseId)?.status === 'running'
  );

  /**
   * The run must keep READING live for as long as it genuinely is one — the
   * dead-screen half of the guarantee, which no API assertion can see. Both
   * controls are read: the header pill (the session detail reaching the screen)
   * and the composer's stop button (the client still holding a response).
   *
   * Tolerant by construction: the recorded state is re-read AFTER the page reads,
   * so a response that settles between the two resolves the predicate instead of
   * failing it. That tolerance costs nothing — a response wrongly torn down by
   * the pokes is caught by the recorded checks and by the failure-surface check,
   * not by this one.
   */
  const expectRunReadsLiveWhileActive = async (responseId: string): Promise<void> => {
    await expect
      .poll(
        async () => {
          const pulsing = (await headerPill.getAttribute('data-pulse')) === 'true';
          const stoppable = await stopControl.isVisible();
          return (pulsing && stoppable) || !responseStillRunning(responseId);
        },
        {
          timeout: PAGE_RENDER_MS,
          message: 'the header must keep reading live and the composer must keep offering stop '
            + 'while the response is genuinely running',
        },
      )
      .toBe(true);
  };

  try {
    // ── Create an isolated agent — pure metadata, no sandbox, ACTIVE at birth. ──
    const agent = await api.createColdTestAgent(`__e2e_hibernate_live_${runId}`);
    agentId = String(agent.agent_id || '');
    expect(agentId, 'created agent must have an id').not.toEqual('');
    expect(String(agent.state || ''), 'created agent is ACTIVE immediately').toEqual('ACTIVE');
    expect(
      String(agent.sandbox_id || '').trim(),
      'per-session agent must not hold a sandbox_id',
    ).toEqual('');

    // ── Start a conversation and let it provision its own sandbox. ─────────────
    const created = await api.startConversation(agentId);
    sessionId = created.session_id;
    sessions.push(sessionId);
    const ready = await api.waitForSessionReady(sessionId);
    const sandboxBefore = String(ready.sandbox_id || '').trim();
    expect(sandboxBefore, 'conversation must own a sandbox once READY').not.toEqual('');
    test.info().annotations.push({ type: 'e2e_session_sandbox_id', description: sandboxBefore });
    await api.setPermissionMode(sessionId, 'bypassPermissions');

    // ── The user opens their conversation. ────────────────────────────────────
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: PAGE_RENDER_MS });

    // ── ACT: the user starts a Bash command held on a workspace file and sends it from the
    //    composer. Nothing is awaited beyond the send — the composer hands the
    //    input over (POST inputs answers with a receipt) and the response's parts
    //    arrive on the session stream the page already follows, so the response
    //    keeps advancing while the pokes land. `.fill()` sets the value without key
    //    events, so the multi-line prompt is not submitted early by an Enter
    //    newline. bypassPermissions means no permission interaction can park it. ──
    const prompt = heldBashPrompt(runId, startedPath, releasePath, resultMarker);
    const delivery = await startPromptDelivery(page, sessionId, prompt);
    const userMessage = page.getByTestId('user-message').filter({ hasText: runId });
    await expect(
      userMessage.last(),
      'the user bubble should render — proof the turn was dispatched from the page',
    ).toBeVisible({ timeout: 30_000 });

    // The run reads live to the user straight away: the send button became a stop
    // button. Not a race with the model — `isSubmitted` flips inside the submit
    // handler, before the send receipt (let alone a frame) can come back — which is
    // why this is the pre-poke page evidence: it costs the live turn no observation
    // time, unlike a header pill that waits on the next projection poll.
    await expect(
      stopControl,
      'the user must be able to see their run is going (the composer offers stop)',
    ).toBeVisible({ timeout: 30_000 });
    const receipt = ((await (await expectPromptDelivered(delivery)).json()) as { data: InputReceipt }).data;
    const inputId = String(receipt.input_id || '').trim();
    expect(inputId, 'the platform must accept the message with an input id').not.toEqual('');

    // ── PROBE: a relayed Bash call plus the command's own started marker. ─────
    const heldResponseId = await waitForHeldBashResponse(
      api, sessionId, inputId, startedPath, PROBE_TIMEOUT_MS,
    );
    expect(
      heldResponseId,
      `the configured model did not enter the requested held Bash command within ${PROBE_TIMEOUT_MS}ms ` +
        '— there is no running response to poke hibernate/wake against',
    ).not.toBeNull();
    const responseId = heldResponseId as string;

    // A running turn keeps its tool cards inside a process group that starts
    // closed, so the card reaches the user through the reader's own press.
    await openLiveProcessGroup(page);
    await expect(
      bashCard,
      'the held Bash command must reach the user before hibernate/wake',
    ).toBeVisible({ timeout: PAGE_RENDER_MS });
    await expect(
      bashCard,
      'the command-side gate must still read as running before hibernate/wake',
    ).toHaveText(/Working|处理中/, { timeout: 30_000 });

    // The lifecycle calls must land on the running response identified by the probe.
    const before = readLiveResponse(sessionId, inputId);
    expect(before?.responseId, 'the recorded response must be the one the probe observed').toBe(responseId);
    expect(before?.status, 'the response must be running before the pokes').toBe('running');

    // ── hibernate is a no-op even with a live turn: agent stays ACTIVE, never
    //    acquires a sandbox. The shared hibernateAgent helper wraps the public POST
    //    route (returns the sanitized agent under the {data} envelope). No console
    //    control hibernates an AGENT, so this is an operator poke — the same class
    //    of out-of-band action as the sibling specs' sandbox kill. ────────────────
    const afterHibernate = await api.hibernateAgent(agentId);
    expect(
      String(afterHibernate.state || ''),
      'per-session hibernate must be a no-op returning ACTIVE',
    ).toEqual('ACTIVE');
    expect(
      String(afterHibernate.sandbox_id || '').trim(),
      'per-session agent must never hold a sandbox_id (hibernate)',
    ).toEqual('');

    // ── wake is also a no-op. ──────────────────────────────────────────────────
    const afterWake = await api.wakeAgent(agentId);
    expect(
      String(afterWake.state || ''),
      'per-session wake must be a no-op returning ACTIVE',
    ).toEqual('ACTIVE');
    expect(
      String(afterWake.sandbox_id || '').trim(),
      'per-session agent must never hold a sandbox_id (wake)',
    ).toEqual('');

    // ── THE titular property, recorded half first because it is the tightest race:
    //    the pokes did not disturb the running response. The workspace gate cannot
    //    finish naturally before this read, so an ending here is attributable to
    //    the lifecycle path rather than model timing. The platform's records
    //    supply response identity because the page does not render a response id.
    const after = readLiveResponse(sessionId, inputId);
    expect(after, 'the response should still be recorded after hibernate/wake').not.toBeNull();
    // The same response is still running and no lifecycle call ended it.
    expect(
      after?.responseId,
      'the SAME response should still answer the message after hibernate/wake',
    ).toBe(responseId);
    expect(
      after?.status,
      'the response should still be running after no-op hibernate/wake',
    ).toBe('running');
    // Non-flaky backbone: agent-level hibernate did NOT tear down the conversation's
    // own sandbox, and did NOT end the response as failed.
    expect(
      after?.outcome ?? null,
      'no-op hibernate/wake must not have ended the response',
    ).toBeNull();
    const sessionAfter = await api.getSession(sessionId);
    expect(
      String(sessionAfter.sandbox_id || '').trim(),
      'the session must still own the SAME sandbox after agent hibernate/wake',
    ).toEqual(sandboxBefore);

    // The run still READS live while it genuinely is one (header pill + stop
    // button). The held command makes this a non-vacuous post-poke assertion.
    await expectRunReadsLiveWhileActive(responseId);

    // Release the exact command observed above. A completed Bash card carrying
    // its stdout proves both halves that a recorded state alone cannot: the runner
    // kept executing after the lifecycle pokes, and the page kept consuming its
    // frames. Uploading a marker is deterministic platform I/O, not another model
    // decision.
    await api.uploadFileText(sessionId, '/workspace', releaseName, 'release', 30_000);
    const settled = await api.waitForSessionReady(sessionId, SETTLE_BUDGET_MS);
    expect(
      settled.last_response?.response_id,
      'the last response must be the one the pokes landed on',
    ).toBe(responseId);
    expect(
      settled.last_response?.outcome,
      'the held response should complete normally after its release',
    ).toBe('completed');
    // The settled turn folds its finished work behind one header, and folding
    // rebuilds the group opened above as closed. This prompt runs one tool and
    // then replies, which is the shape that folds, so the header is waited for
    // and opened before the card is read again.
    await expect(
      page.getByTestId('assistant-turn-process'),
      'a settled tool response must fold into one header the reader can open',
    ).toHaveCount(1, { timeout: PAGE_RENDER_MS });
    await revealAssistantProcess(page);
    await expect(
      bashCard,
      'the released Bash call must stop reading as running on screen',
    ).toHaveText(/Done|已完成/, { timeout: PAGE_RENDER_MS });
    await bashCard.click();
    await expect(
      bashCard.locator('xpath=..'),
      'the post-poke Bash result must reach the user with its real stdout',
    ).toContainText(resultMarker, { timeout: PAGE_RENDER_MS });

    // Nothing on screen says the turn was failed under them. A platform-failed turn
    // renders its error INTO the transcript as an assistant message, so without this
    // guard "there is more text than before" would be satisfied by exactly the
    // outcome the pokes must not cause.
    await expect(
      failureOnScreen,
      'an agent-level lifecycle poke must never surface to the user as a failed turn',
    ).toHaveCount(0);
    // …and it is still their conversation: the message that started the run is
    // still on screen, not swapped out from under them by the agent-record poke.
    await expect(userMessage.last()).toBeVisible();
  } finally {
    // Nothing is torn down here. `trackSessions()` and `onPassOnly()` decide in
    // afterEach hooks, where the test's real status is known — see that fixture
    // on why a `finally` cannot tell it is unwinding from a failure, and on why
    // the unit is this whole block rather than the delete line.
  }
});
