/**
 * E2E: reconciliation drives an unresolved turn for a deleted session to a
 * terminal state without restoring the session.
 *
 * A real composer turn first produces visible and durable history. The test then
 * marks the session deleted and parks its last turn in TRANSCRIPT_PENDING through
 * the document-store fault helpers. Reconciliation must clear that recovery phase
 * within the bounded window. The session must remain absent from both the store's
 * active set and the browser sidebar.
 *
 * The fault has no product UI, so setup and terminal-state checks use the document
 * store. Every changed document is restored in finally before normal cleanup.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { apiPath, appPath } from '../fixtures/env';
import {
  patchSessionDoc,
  patchSnapshotDoc,
  restoreDoc,
  snapshotDoc,
  waitForTurnTerminalProof,
} from '../fixtures/dbOracle';

// The reconcile worker runs on a ~10s cadence and the coordinator needs a pass
// of its own; allow several rounds before calling the orphan livelocked.
const TERMINAL_BUDGET_MS = 180_000;
// A heartbeat old enough that the stuck-session scan cannot mistake the orphan
// for a live worker's turn.
const STALE_HEARTBEAT_AT = '2020-01-01T00:00:00+00:00';

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// Teardown that changes state runs on the passing path only, restore before
// delete as it always was — the session row has to exist for the delete to see
// it. Registration order is execution order for afterEach hooks.
//
// The restores are not exempt from the rule because they put rows BACK. This
// test removes the session and snapshot rows to manufacture an orphan and the
// reconciler acts on that absence; writing the pre-test snapshot back over what
// the reconciler wrote is precisely what a failing run must not do. The cost is
// that a kept session's row stays deleted, so the report tail resolves its
// sandbox as `<unresolved>` — an honest answer, with the oracle DB holding the
// rest.
let sessionId = '';
let sessionBefore: Record<string, unknown>[] = [];
let snapshotBefore: Record<string, unknown>[] = [];
onPassOnly(async () => {
  if (sessionBefore[0]) restoreDoc('sessions', { '$.session_id': sessionId }, sessionBefore[0]);
  if (snapshotBefore[0]) restoreDoc('snapshots', { '$.session_id': sessionId }, snapshotBefore[0]);
});
const sessions = trackSessions();

test('reconcile drives an orphaned turn of a deleted session terminal', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = Date.now();

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  sessionId = created.session_id;
  sessions.push(sessionId);

  // THIS conversation's row in the console's session list. The row carries its
  // own session id (App.tsx: data-testid="session-row" data-session-id=…), so
  // the locator is bound to this conversation rather than to "some row" — a
  // count over the bare testid would be satisfied by any other session on the
  // deployment and would prove nothing about deletion.
  const sidebarRow = page
    .getByTestId('session-row')
    .and(page.locator(`[data-session-id="${sessionId}"]`));

  /**
   * Load a console route and return the session ids the CONSOLE ITSELF was
   * handed for its list.
   *
   * The absence assertions below have to be non-vacuous, and `toHaveCount(0)`
   * on its own is not: it passes the instant the list has not painted yet, so
   * it would go green on a console that never loaded at all. Waiting for the
   * browser's own `GET /sessions?page=1` fixes the moment — the list HAS
   * answered, and these are the ids it answered with — after which the DOM
   * assertion means "and the console renders that answer".
   *
   * Page 1 is the page to read: the list is ordered updated_at DESC
   * (session_repository.list_user_sessions_page) and this conversation was
   * touched seconds earlier, so if it is listed at all it is at the top.
   */
  const listedSessionIds = async (navigate: () => Promise<unknown>): Promise<string[]> => {
    const listed = page.waitForResponse(
      (response) =>
        response.request().method() === 'GET' &&
        response.url().includes(`${apiPath('/sessions')}?`) &&
        response.ok(),
      { timeout: 60_000 },
    );
    await navigate();
    // The page response is only the synchronization point. Its BODY is read
    // out-of-band through the API context: a whole-page navigation floods
    // Chromium's inspector network cache, and a body evicted from it fails
    // with a protocol error on a list that loaded fine. The server list is
    // the same authority either way.
    await listed;
    const body = (await api.data('GET', '/sessions?page=1&limit=50')) as {
      sessions?: Array<{ session_id?: string }>;
    };
    const sessions = body.sessions ?? [];
    return sessions.map((session) => String(session.session_id ?? ''));
  };

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
    // non-empty bubble" is satisfied by exactly the outcome under test. Here it
    // would be worse than a false green — a turn that failed on its own would
    // enter the recovery phase independently, so the simulation could not prove
    // that it parked a settled turn.
    await expect(reply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
    return reply;
  };

  try {
    await api.waitForSessionReady(sessionId);
    await api.setPermissionMode(sessionId, 'bypassPermissions');

    // Positive control for the deletion oracle below: while the conversation is
    // alive, the console's list carries it and the sidebar renders its row.
    // Without this pair, the later "the row is gone" assertions are
    // indistinguishable from a list that never had it.
    const listedWhileLive = await listedSessionIds(() =>
      page.goto(appPath(`/sessions/${sessionId}`)),
    );
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
    expect(
      listedWhileLive,
      'a live conversation should be on the first page the console lists',
    ).toContain(sessionId);
    await expect(
      sidebarRow,
      'a live conversation should be listed in the console sidebar',
    ).toHaveCount(1, { timeout: 45_000 });

    // ── One real turn, sent the way a user sends one. This is the turn the
    //    simulation then parks unresolved. ────────────────────────────────────
    await sendAndReadReply(
      `E2E orphaned deleted turn ${runId}: 不要使用工具,请只回复一句话:orphan ${runId} done。`,
      240_000,
    );
    // The header settles: the turn the spec is about is one that genuinely
    // reached its end on screen, not one still in flight when the simulation parks it.
    // Scoped to run-view on purpose. Every session row in the sidebar renders a
    // status-pill of its own, and the sidebar precedes the content in the DOM —
    // an unscoped `.first()` reads some OTHER conversation's pill, which is
    // idle by definition and would settle this assertion for free.
    await expect(
      page.getByTestId('run-view').getByTestId('status-pill').first(),
    ).toHaveAttribute('data-pulse', 'false', { timeout: 60_000 });

    // The turn id and its durable terminal proof have no pixel — the console
    // renders a transcript, not turn ids — so they are read off the snapshot.
    const settled = snapshotDoc(sessionId);
    const turnId = String(settled?.last_turn_id ?? settled?.current_turn_id ?? '').trim();
    expect(turnId, 'the settled turn id should be visible before the simulation').toBeTruthy();
    // Establish a successful baseline before simulating the unresolved deletion.
    await waitForTurnTerminalProof(sessionId, turnId, 'COMPLETED');
    expect(
      String(snapshotDoc(sessionId)?.session_lifecycle_state ?? ''),
      'the baseline session must not start deleted',
    ).not.toBe('DELETED');

    // ── Simulate: the user deleted the session, and the turn is parked
    //    unresolved. Clearing the terminal proof is what makes it an ORPHAN
    //    rather than a settled turn wearing a recovery phase. ────────────────
    sessionBefore = patchSessionDoc(sessionId, { deleted: true });
    snapshotBefore = patchSnapshotDoc(sessionId, {
      session_lifecycle_state: 'DELETED',
      last_turn_status: 'FAILED',
      turn_recovery_phase: 'TRANSCRIPT_PENDING',
      last_turn_terminal_frame: null,
      worker_heartbeat_at: STALE_HEARTBEAT_AT,
    });
    expect(sessionBefore.length, 'the simulation must find the session row').toBeGreaterThan(0);
    expect(snapshotBefore.length, 'the simulation must find the snapshot row').toBeGreaterThan(0);

    const simulated = snapshotDoc(sessionId);
    expect(
      String(simulated?.turn_recovery_phase ?? ''),
      'the simulation must park the turn unresolved',
    ).toBe('TRANSCRIPT_PENDING');
    expect(
      String(simulated?.session_lifecycle_state ?? ''),
      'the simulation must delete the session',
    ).toBe('DELETED');

    // ── What deletion means to the user: the conversation leaves their list.
    //    A full load rather than an in-page wait — the sidebar's list is SWR
    //    with no polling interval, so it revalidates on mount, and waiting on a
    //    page that will never refetch would time out on a correct console.
    //    Leaving the conversation also drops the browser's session follower, so
    //    the orphan is resolved below with nobody tailing it — which is the
    //    state a deleted session is actually in. ────────────────────────────
    const listedAfterDelete = await listedSessionIds(() => page.goto(appPath('/')));
    expect(
      listedAfterDelete,
      'a deleted conversation must leave the list the console fetches',
    ).not.toContain(sessionId);
    await expect(page.getByTestId('sessions-page')).toBeVisible({ timeout: 45_000 });
    await expect(
      sidebarRow,
      'a deleted conversation must not be listed in the console sidebar',
    ).toHaveCount(0, { timeout: 30_000 });

    // ── The orphan must reach a terminal outcome. Which one is the
    //    coordinator's call — recovered from the mirror, or settled
    //    unrecoverable — but the phase must not still be pending. No pixel:
    //    turn_recovery_phase is kernel state with no surface in the console. ─
    const deadline = Date.now() + TERMINAL_BUDGET_MS;
    let phase = 'TRANSCRIPT_PENDING';
    while (Date.now() < deadline) {
      phase = String(snapshotDoc(sessionId)?.turn_recovery_phase ?? '');
      if (phase !== 'TRANSCRIPT_PENDING') break;
      await sleep(5_000);
    }
    expect(
      phase,
      `the orphaned turn of a deleted session must reach a terminal outcome within ` +
        `${TERMINAL_BUDGET_MS}ms instead of being retried forever`,
    ).not.toBe('TRANSCRIPT_PENDING');

    expect(
      String(snapshotDoc(sessionId)?.session_lifecycle_state ?? ''),
      'driving the orphan terminal must preserve the deleted boundary',
    ).toBe('DELETED');

    // The same boundary, where the user would notice it being broken: driving
    // the orphan terminal touches this session's rows, and none of that may put
    // the conversation back in the sidebar. This second load is also the second
    // plain GET served while the orphan lane was active.
    // A fresh `goto` rather than `reload()`: the home route redirects to
    // /agents client-side, so a reload would depend on the SPA fallback serving
    // that path — an unrelated thing to go red on.
    const listedAfterTerminal = await listedSessionIds(() => page.goto(appPath('/')));
    expect(
      listedAfterTerminal,
      'driving the orphan terminal must not resurrect the conversation into the user list',
    ).not.toContain(sessionId);
    await expect(page.getByTestId('sessions-page')).toBeVisible({ timeout: 45_000 });
    await expect(
      sidebarRow,
      'the console must not render a row for a conversation the user deleted',
    ).toHaveCount(0, { timeout: 30_000 });

    console.log('RECONCILE_ORPHAN_DELETED_SESSION_E2E_EVIDENCE', JSON.stringify({
      session_id: sessionId,
      turn_id: turnId,
      final_recovery_phase: phase,
      final_last_turn_status: snapshotDoc(sessionId)?.last_turn_status,
      final_session_lifecycle_state: snapshotDoc(sessionId)?.session_lifecycle_state,
    }));
  } finally {
    // Nothing is undone here: the restores and the delete both run from
    // afterEach hooks, on the passing path only.
  }
});
