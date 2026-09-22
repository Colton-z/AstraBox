/**
 * E2E: reconciliation never marks a genuinely active turn FAILED, and stream
 * resume remains valid while that turn is running.
 *
 * A long Bash turn must reach PROCESSING or STREAMING with durable frames and no
 * terminal result. Across multiple reconcile intervals and two browser reloads,
 * the same turn must remain active, the page must stay live without a failure
 * surface, and every browser follow request must avoid HTTP 500. A plain GET
 * ai-stream resume is checked separately because the console uses follow mode.
 *
 * The model may decline to run Bash or finish too quickly. A bounded durable-state
 * probe skips only when no active turn can be observed.
 */
import { test, expect } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { apiPath, appPath, oidcAccessHeaders, parseTimeoutEnv } from '../fixtures/env';
import { framesForTurn, oracleDbPath, snapshotDoc } from '../fixtures/dbOracle';

// One sandbox provision + a long-running turn observed across multiple reconcile
// scans + the interrupt settle sit above the 240s suite default; keep it generous
// and env-tunable.
// Bounded probe: how long to wait for the turn to reach a STREAMING/PROCESSING
// state with durable frames before skipping (the session is already READY, so this
// only covers the model emitting its first tool/text frames).
const PROBE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_STALE_HEARTBEAT_PROBE_TIMEOUT_MS', 60_000);
// The observation window over which the active turn must never be reconciled to
// IDLE+FAILED. It must span at least ~2 reconcile scans (SCAN_INTERVAL_S=10s) so
// the background worker demonstrably runs against the live turn without failing it.
const OBSERVE_WINDOW_MS = parseTimeoutEnv('ASTRABOX_E2E_STALE_HEARTBEAT_OBSERVE_MS', 26_000);
// Cadence of the not-FAILED / resume checks inside the observation window.
const POLL_INTERVAL_MS = parseTimeoutEnv('ASTRABOX_E2E_STALE_HEARTBEAT_POLL_MS', 4_000);
// Upper bound for a single GET resume (headers arrive fast; this only guards
// against a hang if the resume stream never sends its status line).
const RESUME_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_STALE_HEARTBEAT_RESUME_TIMEOUT_MS', 15_000);
// After the observation window, how long to let the interrupted turn settle to READY.
const SETTLE_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_STALE_HEARTBEAT_SETTLE_TIMEOUT_MS', 180_000);
// Sandbox sleep (seconds) the prompt asks for — must outlast probe + observe window.
const SLEEP_SECONDS = parseTimeoutEnv('ASTRABOX_E2E_STALE_HEARTBEAT_SLEEP_S', 60);
// How long a reconnecting browser gets to paint the conversation again, and how
// long the screen may lag the kernel before "the run reads live" is a lie. The
// console polls the session projection on a 1.5–3s cadence, so this is generous
// on purpose: it is only ever spent in full when the screen is genuinely dead.
const PAGE_RENDER_MS = parseTimeoutEnv('ASTRABOX_E2E_STALE_HEARTBEAT_PAGE_RENDER_MS', 60_000);

const ACTIVE_CONVERSATION_STATES = new Set(['STREAMING', 'PROCESSING']);

// What "this turn was failed under me" looks like on screen. A platform-failed
// turn renders INTO the transcript as an assistant message — the TurnFailureCard
// (chat:result.turn_failed / turn_failed_with_detail) — and the header can grow a
// destructive retry banner (chat:retry.turn_failed_summary). The console is
// bilingual and the runner's locale is en-US, so both languages are matched. The
// engine/runtime error strings are the guard every reply assertion in this suite
// carries: a failed turn's error text arrives in the bubble too, so "the screen
// looks fine" must not be satisfied by exactly the failure under test.
// (`this turn failed` is case-insensitive and so covers the English card and the
// English banner's tail alike; Chinese phrases the two surfaces differently, so
// both of its strings are listed.)
const TURN_FAILED_ON_SCREEN =
  /this turn failed|本轮执行失败|这一轮执行失败|API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

/** Read status of GET /sessions/{id}/ai-stream?after_seq=0 without consuming the
 *  (possibly live) SSE body. No fixture helper fits: api.resumeStream buffers the
 *  whole body, which on a still-following stream would block until the turn ends —
 *  so the raw request is inlined here (global fetch; no import needed). The status
 *  line arrives with the response headers; this function reads it and aborts
 *  immediately.
 *
 *  This is the PLAIN resume variant (no `follow`), and it stays an API call by
 *  necessity rather than convenience: the console only ever opens the session
 *  channel (`follow=session`), so no user action drives this route. The browser's
 *  own resumes are asserted separately, off the page's network events. */
async function getResumeStatus(sessionId: string, timeoutMs: number): Promise<number> {
  const baseUrl = process.env.ASTRABOX_E2E_BASE_URL || 'http://127.0.0.1:8000';
  const url = new URL(apiPath(`/sessions/${sessionId}/ai-stream`), baseUrl);
  url.searchParams.set('after_seq', '0');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url.toString(), {
      method: 'GET',
      headers: {
        ...oidcAccessHeaders(),
        Accept: 'text/event-stream',
        Connection: 'close',
      },
      signal: controller.signal,
    });
    const status = response.status;
    // Only the status line is needed here — release the live stream right away.
    controller.abort();
    return status;
  } finally {
    clearTimeout(timer);
  }
}

/** Poll the read-only snapshot until the turn is genuinely active (STREAMING/
 *  PROCESSING with a current_turn_id) AND has at least one durable, non-terminal
 *  frame — the two conditions that separate a turn the platform is really driving
 *  from one merely dispatched (frames written, no terminal `data-result` yet), and
 *  the same pair reconcile itself weighs. Returns the active turn_id, or null if
 *  none appears in time. */
async function waitForActiveTurnWithDurableFrames(
  sessionId: string,
  timeoutMs: number,
): Promise<string | null> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const snapshot = snapshotDoc(sessionId);
    const conversationState = String(snapshot?.conversation_state || '');
    const turnId = String(snapshot?.current_turn_id || '').trim();
    if (ACTIVE_CONVERSATION_STATES.has(conversationState) && turnId) {
      const frames = framesForTurn(turnId);
      const hasTerminal = frames.some(
        (frame) => String((frame.payload as { type?: unknown } | undefined)?.type ?? '').trim() === 'data-result',
      );
      if (frames.length > 0 && !hasTerminal) {
        return turnId;
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
  return null;
}

// Teardown that changes state runs on the passing path only. The settling
// hook is registered BEFORE `trackSessions()` on purpose: afterEach hooks run
// in registration order, and the session delete cannot succeed until the turn
// has settled — a DELETE against a still-STREAMING session answers
// SESSION_BUSY and leaves the orphan this ordering exists to avoid.
//
// Settle the still-running turn so the session can be deleted cleanly.
// Interrupt is the graceful signal, but a turn interrupted mid tool-call
// can stay STREAMING behind a live, still-heartbeating worker: DELETE then
// 409s SESSION_BUSY and — because worker_heartbeat_at never goes stale —
// the background ReconcileWorker never reclaims it either. Evicting the
// in-memory runtime drops that worker and forces a cold reload that settles
// the turn (to READY + last_turn_status=FAILED), making the session
// deletable; it is a harmless no-op when the turn already settled on its
// own. Ordered interrupt → evict → wait-READY → delete so teardown never
// leaks an orphan session regardless of how the interrupt lands. Stays on
// the API: no user has an evict-runtime button, and the page's own stream
// dies with the browser context, so there is nothing left to drain.
let sessionId = '';
onPassOnly(async ({ request }) => {
  if (!sessionId) return;
  const api = new AstraApi(request);
  await api.interruptSession(sessionId).catch(() => {});
  await api.adminEvictRuntime(sessionId).catch(() => {});
  await api.waitForSessionState(sessionId, 'READY', SETTLE_BUDGET_MS).catch(() => {});
});
const sessions = trackSessions();

test('a live active turn is not reconciled to IDLE+FAILED and GET resume never reports STREAM_PROTOCOL_VIOLATION', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = Date.now();

  // Fail fast (with the descriptive oracle error) if the document store is not
  // reachable — the durable-read half of this spec depends on it.
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  sessionId = created.session_id;
  sessions.push(sessionId);

  // Every GET the BROWSER made on the session's ai-stream channel, with the status
  // it came back with. `page.on('response')` fires on the response HEADERS, so a
  // live SSE body is never touched: this reads the page's own network without
  // perturbing the stream the page is reading. Registered before the first
  // navigation so the console's very first channel open is captured.
  const browserResumes: { url: string; status: number }[] = [];
  page.on('response', (response) => {
    if (response.request().method() !== 'GET') return;
    if (!response.url().includes('/ai-stream')) return;
    browserResumes.push({ url: response.url(), status: response.status() });
  });

  // Scoped to run-view on purpose. Every session row in the sidebar renders a
  // status-pill of its own and the sidebar precedes the content in the DOM, so an
  // unscoped `.first()` reads some OTHER conversation's pill — which is idle by
  // definition and would answer "is this run live?" for free.
  const headerPill = page.getByTestId('run-view').getByTestId('status-pill').first();
  const failureOnScreen = page.getByTestId('run-view').getByText(TURN_FAILED_ON_SCREEN);
  const userMessage = page.getByTestId('user-message').filter({ hasText: String(runId) });

  const marker = `STALE_HEARTBEAT_ACTIVE_TURN_E2E_${runId}`;

  /** Is the turn this spec is about still genuinely running, per the durable
   *  snapshot? Keeps the page-liveness assertion race-free: a turn that
   *  settles on its own mid-check has nothing left to render as live. */
  const turnStillActive = (turnId: string): boolean => {
    const snapshot = snapshotDoc(sessionId);
    return (
      ACTIVE_CONVERSATION_STATES.has(String(snapshot?.conversation_state || ''))
      && String(snapshot?.current_turn_id || '').trim() === turnId
    );
  };

  /**
   * The run must still READ live for as long as it genuinely is one — the
   * dead-screen half of the guarantee, which no API assertion can see.
   *
   * Tolerant by construction: re-reading the snapshot inside the poll means a
   * turn that settles on its own between the two reads resolves the predicate
   * instead of failing it. That tolerance costs nothing, for two reasons. A turn
   * wrongly failed under the user is caught by the durable IDLE+FAILED check and
   * by the failure-surface check, not by this one. And this runs on every pass of
   * an observation window that a 60s sandbox `sleep` spans, so a screen that
   * stops showing a live run while the turn keeps running has nowhere to hide.
   */
  const expectRunReadsLiveWhileActive = async (turnId: string): Promise<void> => {
    await expect
      .poll(
        async () =>
          (await headerPill.getAttribute('data-pulse')) === 'true' || !turnStillActive(turnId),
        {
          timeout: PAGE_RENDER_MS,
          message: 'the header must keep reading live while the turn is genuinely active',
        },
      )
      .toBe(true);
  };

  try {
    await api.waitForSessionReady(sessionId);
    // ARRANGE through the API. The page dispatches under the SESSION's permission
    // mode (useSessionChat sends permissionModeRef.current, seeded from
    // session.permission_mode at load), so the mode has to be armed BEFORE the
    // page opens: a Bash that stops for an approval parks the turn in
    // WAITING_FOR_INTERACTION and there is no live active turn left to protect.
    await api.setPermissionMode(sessionId, 'bypassPermissions');

    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: PAGE_RENDER_MS });

    // Framed as a plausible engineering task (measure runtime responsiveness during
    // a long step) rather than a bare-command injection — a rigid "run exactly this"
    // prompt makes safety-tuned models refuse it as prompt injection and answer in
    // text, settling the turn to IDLE before it can be observed active.
    const prompt = [
      `E2E ${runId}: 我需要测量运行时在一个长耗时步骤中的响应表现。`,
      `请使用 Bash 工具运行一个先等待约 ${SLEEP_SECONDS} 秒、然后打印一个标记的命令，`,
      '以便我确认 shell 在整个过程中保持存活。请执行：',
      '',
      `    sleep ${SLEEP_SECONDS} && echo ${marker}`,
      '',
      '现在就用 Bash 工具运行它，并等待它结束后再回复。',
    ].join('\n');

    // ACT through the page: the user types the long-running instruction and sends
    // it. Nothing is awaited here beyond the send — the composer hands the input
    // over (POST turn-inputs answers with a receipt) and the turn's frames arrive
    // on the session channel the page already holds open, so the turn stays in
    // flight while the snapshot is observed. `.fill()` sets the value without key
    // events, so the multi-line prompt is not submitted early by an Enter newline.
    const composer = page.getByTestId('composer-prompt');
    await expect(composer, 'the composer must be enabled before sending').toBeEnabled({
      timeout: PAGE_RENDER_MS,
    });
    await composer.fill(prompt);
    await page.getByTestId('composer-submit').click();
    await expect(
      page.getByTestId('user-message').last(),
      'the user bubble should render — proof the turn was dispatched from the page',
    ).toContainText(marker, { timeout: 30_000 });

    // The run reads as live to the user: the send button became a stop button.
    // Not a race with the model — `isSubmitted` flips inside the submit handler,
    // before the send receipt (let alone a frame) can come back.
    await expect(page.getByTestId('run-composer-stop')).toBeVisible({ timeout: 30_000 });

    // ── PROBE (deepseek-chat may never hold a long turn) ──────────────────────
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. A declined ask re-sends the same prompt once, then fails.
    const activeTurnId = await insist<string>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(sessionId, prompt);
      },
      probe: () => waitForActiveTurnWithDurableFrames(sessionId, PROBE_TIMEOUT_MS),
      what: `deepseek-chat did not hold a STREAMING/PROCESSING turn with durable frames within ${PROBE_TIMEOUT_MS}ms ` + '(no long-running Bash tool call — the turn settled to IDLE) — there is no live active turn to protect from reconcile',
      budgetMs: PROBE_TIMEOUT_MS * 2,
      probeMs: PROBE_TIMEOUT_MS,
    });
    const turnId = activeTurnId as string;

    // …and the live turn reaches the USER as a live run. Never a skip: the skip
    // above is for the model declining to hold a long turn; a turn the platform IS
    // running that the console does not show as running is the phantom-dead-screen
    // defect, and a browser is the only witness to it. (The race-free half of that
    // claim was already asserted above: the send turned the button into a stop
    // button. This one follows the turn.)
    await expectRunReadsLiveWhileActive(turnId);
    await expect(
      failureOnScreen,
      'the live run must not be showing the user a failed turn before the observation even starts',
    ).toHaveCount(0);

    // ── OBSERVE: across multiple reconcile-scan intervals, the active turn must
    //    never be flipped to IDLE+FAILED, the user's screen must keep showing a
    //    live run rather than a failure, and neither the browser's own resumes nor
    //    the plain GET resume may 500. ─────────────────────────────────────────
    const observeDeadline = Date.now() + OBSERVE_WINDOW_MS;
    const browserResumesBeforeWindow = browserResumes.length;
    let resumeChecks = 0;
    let browserReconnects = 0;
    // The window is a DURATION, but the claim it feeds — "the resume was repeated
    // across reconcile scans" — is a COUNT, and the two assertions after the loop
    // read the count. A pass now costs a reload plus a re-render, several times a
    // bare poll, so on a slow deployment the fixed window can close after one pass
    // and those assertions go red for a harness reason wearing the shape of a
    // product failure. Run the window out AND take the passes the claim needs;
    // extra passes only observe the same turn across more reconcile scans, and
    // every assertion in the body already tolerates a turn that has settled.
    while (Date.now() < observeDeadline || browserReconnects < 2) {
      const snapshot = snapshotDoc(sessionId);
      const conversationState = String(snapshot?.conversation_state || '');
      const lastTurnStatus = String(snapshot?.last_turn_status || '');

      // KEY ASSERTION: the reconcile worker must not settle a still-running turn as
      // IDLE+FAILED (a natural COMPLETED/INTERRUPTED settle is fine — only a FAILED
      // settle is wrong here, because it is what makes the resume 500). No pixel: the
      // console renders a transcript, not `conversation_state`/`last_turn_status`.
      const incorrectlyFailed = conversationState === 'IDLE' && lastTurnStatus === 'FAILED';
      expect(
        incorrectlyFailed,
        `REGRESSION: the active turn was reconciled to IDLE+FAILED (would cause STREAM_PROTOCOL_VIOLATION on resume); ` +
          `turn_id=${turnId} conversation_state=${conversationState} last_turn_status=${lastTurnStatus}`,
      ).toBe(false);

      // The same regression where the user meets it. A turn the platform failed
      // renders its failure INTO the transcript (and can raise the header's retry
      // banner), so this is the pixel form of the assertion above — and it is the
      // one that fails if the failure reaches the screen by some route the two
      // snapshot fields do not name.
      await expect(
        failureOnScreen,
        'a live run must never show the user a failed turn',
      ).toHaveCount(0);

      // And the run keeps reading live to the user while it genuinely is running
      // (see the helper for why this is safe to make tolerant).
      await expectRunReadsLiveWhileActive(turnId);

      // The browser's OWN resumes: every GET the page made on the session channel
      // (follow=session, re-armed with its cursor on each reconnect) must have come
      // back below 500. This is assertion 2 as the user's browser experiences it.
      expect(
        browserResumes.filter((resume) => resume.status >= 500),
        'the console\'s own GET ai-stream resumes must not 500 (STREAM_PROTOCOL_VIOLATION) during the active turn',
      ).toEqual([]);

      // GET resume must not 500 (STREAM_PROTOCOL_VIOLATION); repeating it also must
      // not itself drive the turn to FAILED — verified by the next loop iterations
      // because GET is read-only. The plain (no-`follow`) variant, which no user
      // action reaches, is the one asserted here.
      const resumeStatus = await getResumeStatus(sessionId, RESUME_TIMEOUT_MS);
      resumeChecks += 1;
      expect(
        resumeStatus,
        `GET resume during the active turn must not 500 (STREAM_PROTOCOL_VIOLATION); got ${resumeStatus}`,
      ).toBeLessThan(500);

      // The reconnecting browser, for real: the user refreshes mid-run. Each reload
      // drops the page's follower and opens the session channel again from its
      // cursor — the repeated resume this spec is about — and the conversation must
      // come back with the message that started the turn still in it. Nothing here
      // can come from client memory; the tab that watched the send is gone.
      await page.reload({ waitUntil: 'domcontentloaded' });
      browserReconnects += 1;
      await expect(page.getByTestId('run-view')).toBeVisible({ timeout: PAGE_RENDER_MS });
      await expect(
        userMessage.last(),
        'a reconnecting browser must come back to the conversation that is still running',
      ).toBeVisible({ timeout: PAGE_RENDER_MS });

      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
    }

    // The window must have spanned enough polls to cross ≥2 reconcile-scan
    // intervals (SCAN_INTERVAL_S=10s) — the probe above already guaranteed the
    // window began with a genuinely-active turn (durable frames, no terminal), so
    // the "survives reconcile scans while active" claim is not vacuous.
    expect(
      resumeChecks,
      'the observation window should have exercised at least two GET resumes across reconcile-scan intervals',
    ).toBeGreaterThanOrEqual(2);
    expect(
      browserReconnects,
      'the observation window should have reconnected the browser at least twice — "repeating the resume" is the claim',
    ).toBeGreaterThanOrEqual(2);
    // …and at least one of those refreshes must have actually re-opened the
    // channel, or the browser-side status assertion was reading a list nothing
    // added to. A DELTA rather than one-per-reload on purpose: a console loading
    // an idle session opens no stream at all by design (turnStream.ts
    // `decideAutoResume` → 'no-turn'), so a turn that settles naturally partway
    // through the window legitimately leaves later reloads with nothing to resume.
    expect(
      browserResumes.length - browserResumesBeforeWindow,
      'a refresh during the active turn should have re-opened the session channel (GET ai-stream?follow=session)',
    ).toBeGreaterThanOrEqual(1);
    // The last reconnect's status too — the in-loop check runs before its own
    // reload, so without this the final refresh would go unread.
    expect(
      browserResumes.filter((resume) => resume.status >= 500),
      'no GET ai-stream the console opened may have come back 500 (STREAM_PROTOCOL_VIOLATION)',
    ).toEqual([]);

    // Final durable check: the resume path never left the turn FAILED.
    const finalSnapshot = snapshotDoc(sessionId);
    const finalFailed =
      String(finalSnapshot?.conversation_state || '') === 'IDLE' &&
      String(finalSnapshot?.last_turn_status || '') === 'FAILED';
    expect(
      finalFailed,
      `after repeated read-only resumes the turn must not be IDLE+FAILED; ` +
        `conversation_state=${String(finalSnapshot?.conversation_state || '')} ` +
        `last_turn_status=${String(finalSnapshot?.last_turn_status || '')}`,
    ).toBe(false);

    // And the same, on the screen the user is left looking at: their conversation,
    // their message, and no failed turn.
    await expect(
      failureOnScreen,
      'after repeated reconnects the user must not be looking at a failed turn',
    ).toHaveCount(0);
    await expect(userMessage.last()).toBeVisible();

    console.log('ACTIVE_TURN_NOT_RECONCILED_E2E_EVIDENCE', JSON.stringify({
      session_id: sessionId,
      turn_id: turnId,
      resume_checks: resumeChecks,
      browser_reconnects: browserReconnects,
      browser_resume_statuses: browserResumes.map((resume) => resume.status),
      final_conversation_state: finalSnapshot?.conversation_state,
      final_last_turn_status: finalSnapshot?.last_turn_status,
    }));
  } finally {
    // Nothing is torn down here. `trackSessions()` and `onPassOnly()` decide in
    // afterEach hooks, where the test's real status is known — see that fixture
    // on why a `finally` cannot tell it is unwinding from a failure, and on why
    // the unit is this whole block rather than the delete line.
  }
});
