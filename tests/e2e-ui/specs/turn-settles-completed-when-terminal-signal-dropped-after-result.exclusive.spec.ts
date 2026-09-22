/**
 * E2E: a turn still settles COMPLETED when its bridge loses the terminal signal
 * after receiving the model result.
 *
 * A file-driven fault stops the bridge after it buffers the result. The page must
 * render a normal non-empty reply, return the header to READY, and show neither a
 * failure card nor a retry banner. Durable state must record COMPLETED with no
 * turn or session error, and the fault file must list the session as consumed so
 * the test cannot pass through a normal turn.
 *
 * The server must run with ASTRABOX_E2E_FAULTS=1 and share the configured fault
 * directory with the test host. The prompt forbids tools, so no permission probe
 * is required.
 */
import fs from 'node:fs';
import path from 'node:path';

import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { snapshotDoc } from '../fixtures/dbOracle';

// One sandbox provision + the faulted turn (its buffered result is re-settled
// COMPLETED only after the bounded terminal-settle retry window, ~30s) sits above
// the 240s suite default; keep it generous and env-tunable.
// The faulted turn: model result → up to the 30s terminal-settle retry window
// (service._turn_terminal_settle_retry_window_s) before the buffered result is
// re-settled COMPLETED and the live SSE closes.
const FAULTED_TURN_MS = parseTimeoutEnv('ASTRABOX_E2E_TERMINAL_DROP_TURN_MS', 180_000);

// The community backend reads the terminal-drop fault from this base path (env
// override → the backend's own default), scanning `<base>.d/*.json`. Author and
// backend MUST agree on ASTRABOX_E2E_TURN_TERMINAL_DROP_FAULT_FILE for the shared
// mount; both default to the community default so an unset pair still agrees.
const FAULT_BASE = (
  process.env.ASTRABOX_E2E_TURN_TERMINAL_DROP_FAULT_FILE
  || '/tmp/astrabox-e2e-turn-terminal-drop-faults.json'
).trim();
const FAULT_DIR = `${FAULT_BASE}.d`;

// The two failure surfaces the console renders, matched in both shipped locales
// (i18n picks the browser's language, so pinning one would make the guard inert
// on a differently-configured box rather than red):
//  - the assistant bubble's TurnFailureCard, from a `turn_failure` block;
//  - the header's retry banner for a FAILED post_dispatch turn. Its phrase is
//    matched on the distinctive clause ("已送达沙箱" / "reached the sandbox")
//    rather than the whole sentence, so it cannot be satisfied — or falsely
//    tripped — by whatever prose the model happens to produce.
const TURN_FAILURE_CARD_TEXT = /本轮执行失败|This turn failed/i;
const TURN_FAILED_BANNER_TEXT = /已送达沙箱|reached the sandbox/i;

/** Per-worker, process, and session path that cannot collide with another fault. */
function faultFilePathFor(sessionId: string): string {
  const slug = `${test.info().title} ${sessionId}`
    .replace(/[^a-zA-Z0-9_.-]+/g, '-')
    .replace(/^-|-$/g, '');
  return path.join(FAULT_DIR, `w${test.info().workerIndex}-${process.pid}-${slug}.json`);
}

/** Arm one bridge-terminal drop for this session. */
function armTerminalDropFault(faultPath: string, sessionId: string): void {
  fs.mkdirSync(FAULT_DIR, { recursive: true });
  // The fault dir is a host↔container shared mount and the two sides run as
  // DIFFERENT uids (host playwright vs. the backend server container, e.g.
  // ubuntu:1000 vs. astrabox:999). The backend does not merely READ the fault —
  // it must atomically write `consumed` back (a `<file>.tmp` create + os.replace
  // in this dir), which needs the DIRECTORY group/other-writable. mkdirSync's
  // mode is masked by umask (→ 775, backend uid has no write), so chmod it open
  // explicitly; without this the write-back fails Errno 13 and the drop silently
  // never fires. Harmless on single-uid local runs; no other spec uses this dir.
  fs.chmodSync(FAULT_DIR, 0o777);
  fs.writeFileSync(
    faultPath,
    JSON.stringify({ faults: { drop: 1 }, match: { session_id: sessionId } }),
    'utf8',
  );
  // The controller deliberately runs with a restrictive umask. Make the
  // declaration readable by the backend's different container uid; atomic
  // write-back needs the writable directory above, not a writable file.
  fs.chmodSync(faultPath, 0o644);
}

/** Session ids the backend wrote back to `consumed` (empty if the fault never
 *  fired / the file is gone / malformed). */
function readTerminalDropConsumed(faultPath: string): string[] {
  if (!fs.existsSync(faultPath)) return [];
  try {
    const payload = JSON.parse(fs.readFileSync(faultPath, 'utf8')) as { consumed?: unknown };
    return Array.isArray(payload.consumed) ? payload.consumed.map((v) => String(v)) : [];
  } catch {
    return [];
  }
}

function clearTerminalDropFault(faultPath: string): void {
  try {
    if (fs.existsSync(faultPath)) fs.rmSync(faultPath, { force: true });
  } catch {
    // best-effort teardown
  }
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('a turn whose bridge terminal signal is dropped after the result still settles COMPLETED (reconnect race)', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const faultPath = faultFilePathFor(sessionId);
  test.info().annotations.push({ type: 'e2e_terminal_drop_fault_file', description: faultPath });

  /** Send from the composer and wait for one more rendered assistant bubble. */
  const sendAndReadReply = async (prompt: string, budgetMs: number) => {
    const before = await page.getByTestId('assistant-message').count();
    await page.getByTestId('composer-prompt').fill(prompt);
    await page.getByTestId('composer-submit').click();
    await expect(page.getByTestId('user-message').last()).toContainText(prompt.slice(0, 8), {
      timeout: 30_000,
    });
    // Count, not text: the model's wording is its own business, and a spec that
    // pins wording fails on a model that is behaving correctly.
    await expect
      .poll(() => page.getByTestId('assistant-message').count(), { timeout: budgetMs })
      .toBeGreaterThan(before);
    const reply = page.getByTestId('assistant-message').last();
    await expect(reply).not.toBeEmpty();
    // A bubble that grew the count is not yet a reply: a failed turn renders
    // its error INTO the transcript as an assistant message, so "one more
    // non-empty bubble" is satisfied by exactly the outcome under test.
    await expect(reply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
    // And here that guard needs a second clause. The failure this spec
    // targets does NOT produce any of those words: bridge_loop's other branch
    // settles the terminal error text "turn ended without terminal event",
    // which reaches the transcript as a `turn_failure` block — i.e. a
    // TurnFailureCard whose only distinctive text is the localized "this turn
    // failed". Matching the detail string alone would let the exact bug under
    // test through the oracle.
    await expect(reply, 'the reply must be the real answer, not a turn-failure card').not.toContainText(
      TURN_FAILURE_CARD_TEXT,
    );
    return reply;
  };

  try {
    await api.waitForSessionReady(sessionId);

    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible();

    // Arm the REAL backend turn worker to drop the bridge terminal signal right
    // after the model result frame for THIS session — the exact production
    // reconnect race. The worker must then settle a successful finish from the
    // buffered result, not a turn_failure that discards the real answer.
    // Armed AFTER the page is up so the only turn on this session is the one the
    // user is about to send from the composer.
    armTerminalDropFault(faultPath, sessionId);

    // A plain text turn (explicitly no tools) so a terminal result frame is
    // reliably emitted — that is what the fault drops the signal AFTER.
    const prompt = `E2E terminal-drop ${runId}: 不要使用任何工具，直接用一句话回答“你好，这是一次终止信号丢失后的恢复测试”。`;
    // The drop kills the turn's bridge before it can announce that the turn is
    // over, so nothing the browser is already holding open will say so either:
    // the ONLY way this reply and this header ever settle on screen is the
    // re-derived durable terminal. That is the claim, read off the screen.
    await sendAndReadReply(prompt, FAULTED_TURN_MS);

    // The header settles, and settles to READY. `data-pulse=false` is "no
    // phantom generating tail"; `data-state` is the session's own state
    // (sessionRunStatus.headerRunState), so the bridge branch that gives up and
    // projects RECOVERY_REQUIRED reads RECOVERY_REQUIRED here and fails this.
    // Budget: the terminal re-settle is bounded by the 30s retry window, and the
    // console then has to poll the projection — well inside this.
    // Scoped to run-view ON PURPOSE. The app shell's sidebar renders a
    // `status-pill` for EVERY conversation in the list (App.tsx SidebarMenu),
    // and the sidebar precedes the route outlet in the DOM — so a bare
    // `status-pill.first()` binds to some OTHER conversation's row, whose
    // `data-state` is that row's raw `session.state` and whose `data-pulse` is
    // false for free. Both assertions below would then be vacuous, and the
    // `data-state` one would silently degrade into the API-level
    // `session.state` check it is supposed to replace. SessionHeader's pill is
    // the only `status-pill` inside `run-view`, and it is the one carrying
    // headerRunState.
    const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
    await expect(pill, 'a dropped terminal must not leave the header generating forever').toHaveAttribute(
      'data-pulse',
      'false',
      { timeout: 120_000 },
    );
    await expect(pill, 'the recovered turn must leave the conversation READY').toHaveAttribute(
      'data-state',
      'READY',
      { timeout: 120_000 },
    );

    // Re-read the failure card on the SETTLED transcript. The check inside
    // sendAndReadReply fires as soon as one more non-empty bubble exists — i.e.
    // on the first text delta, which is BEFORE the result frame the fault hangs
    // off, so at that moment no turn_failure could have been rendered yet and
    // the guard is necessarily satisfied. It is the same guard read after the
    // header settled that can actually catch a failure card appended to the
    // live turn.
    await expect(
      page.getByTestId('assistant-message').last(),
      'the settled reply must still be the real answer, not a turn-failure card',
    ).not.toContainText(TURN_FAILURE_CARD_TEXT);
    await expect(
      page.getByTestId('assistant-message').last(),
    ).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);

    // The FAILED shape has a screen of its own: a post_dispatch turn failure
    // raises the console's retry banner. It must not be there.
    await expect(
      page.getByText(TURN_FAILED_BANNER_TEXT),
      'a recovered turn must not raise the "this turn failed" retry banner',
    ).toHaveCount(0);

    // ANTI-VACUITY: the fault must have actually fired against the real backend
    // turn worker (session id recorded in `consumed`). A miss means the seam was
    // not armed/shared — not that the code regressed — so the message is
    // actionable. Without this guard the assertions here only prove a NORMAL turn
    // completes rather than exercising the buffered-result settlement path.
    // Read only now, and the ordering is not incidental: the bubble appears on the
    // first text delta, which is BEFORE the result frame the fault hangs off — but
    // the settled header above cannot happen until after the drop, and
    // e2e_faults writes `consumed` atomically at consumption time, ahead of the
    // bridge break. So a settled header means this file has already been written.
    const consumed = readTerminalDropConsumed(faultPath);
    expect(
      consumed,
      'terminal-drop fault must be consumed by the real backend turn worker — start the server with ' +
        `ASTRABOX_E2E_FAULTS=1 and make ${FAULT_DIR} the same path the backend reads (a shared host↔container ` +
        'mount, like the sqlite state mount dbOracle relies on)',
    ).toContain(sessionId);

    // Truth owner: the snapshot settled COMPLETED, not FAILED. No pixel — the
    // console renders a transcript, not turn rows — and the banner absence above
    // is only a conditional witness of the same fact, so this stays.
    const snapshot = snapshotDoc(sessionId);
    expect(snapshot, 'terminal-drop turn should have a session snapshot').toBeTruthy();
    expect(
      String(snapshot?.last_turn_id || '').trim(),
      'terminal-drop turn should persist a last_turn_id',
    ).not.toEqual('');
    expect(
      snapshot?.last_turn_status,
      `terminal-drop snapshot must settle COMPLETED, not FAILED; snapshot=${JSON.stringify(snapshot)}`,
    ).toBe('COMPLETED');
    expect(
      String(snapshot?.last_turn_error || '').trim(),
      'terminal-drop must not record a turn error',
    ).toEqual('');

    // The header error line has no stable selector, so exact last_error state is
    // read from the session projection.
    const ready = await api.waitForSessionReady(sessionId);
    expect(String(ready.last_error || '').trim(), 'terminal-drop turn must not leak last_error').toBe('');

    // ── The answer survives a reload, which is where the persisted projection
    //    becomes a pixel. useInitialMessages replays the stored blocks, and a
    //    `turn_failure` block among them would render as the same TurnFailureCard
    //    — so this is the block-shape assertion, read off the screen a returning
    //    user actually gets rather than off the messages payload.
    await page.reload();
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 30_000 });
    const rehydrated = page.getByTestId('assistant-message').last();
    // Presence first, then content: the transcript rehydrates from a fetch, so
    // for a moment after reload there is no bubble at all — and an assertion
    // about the CONTENT of an element that does not exist yet reports the wrong
    // failure (a missing element, not a lost answer).
    await expect(rehydrated, 'the recovered answer must still be there on reload').toBeVisible({
      timeout: 30_000,
    });
    await expect(rehydrated, 'the reloaded answer must not be an empty bubble').not.toBeEmpty();
    await expect(rehydrated).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
    await expect(
      rehydrated,
      'the persisted turn must not carry a turn_failure block (it renders as the failure card)',
    ).not.toContainText(TURN_FAILURE_CARD_TEXT);
    await expect(
      page.getByText(TURN_FAILED_BANNER_TEXT),
      'a reloaded recovered turn must not raise the "this turn failed" retry banner',
    ).toHaveCount(0);
    // Reload re-derives the header from the durable projection alone — the live
    // stream that carried this turn is long gone — so a settled READY here is
    // the durable half of the same claim.
    // Same run-view scoping as above: the sidebar's per-conversation pills
    // precede the outlet, so an unscoped `.first()` would read another row.
    await expect(
      page.getByTestId('run-view').getByTestId('status-pill').first(),
      'the rehydrated header must read READY, not RECOVERY_REQUIRED',
    ).toHaveAttribute('data-state', 'READY', { timeout: 60_000 });
  } finally {
    clearTerminalDropFault(faultPath);
    // The session is not deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known.
  }
});
