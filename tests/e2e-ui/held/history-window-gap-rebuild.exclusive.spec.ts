/**
 * E2E: a page that refreshes onto a newest window it does not overlap rebuilds
 * the records in between from the pinned cursor, keeps the window it already
 * held, and fails on nothing.
 *
 * The console's window read (`useFirstPageMessages.ts::fetchFirstPage`, the
 * committing `window` mode that a manual refresh, a rehydrate and a
 * SessionStore rebuild all go through) merges the newest page onto the records
 * it already holds by the boundary the two share
 * (`historyWindow.ts::mergeLatestDurableRecords`). A busy conversation can
 * write more than a page of records between two of those reads, so the newest
 * page shares nothing with the loaded window. Two answers are wrong there:
 * refusing the merge, which leaves the reader with a load error and a window
 * that never moves; and laying the two windows edge to edge, which hides every
 * record in between without a trace. `joinLatestWindowToLoaded` walks the
 * newest page's own cursor backwards until the windows meet, and only then
 * merges.
 *
 * The scene is built so that the edge-to-edge answer cannot pass: the page
 * holds a 50-row window with an older page behind it, and then MORE than
 * fifty rows are written, so the newest window starts two rows after the held
 * window ends. Those two rows exist only in the page the rebuild fetches. The
 * cursor that page is asked for with is read off the browser's own newest
 * response, not off an earlier API probe: a title write between the two can
 * move the checkpoint and sign a different token for the same records.
 *
 * Nothing here is imported or substituted: fifty-two real turns, the real
 * page, the real refresh command, the raw record order read back from the
 * platform. Exclusive because it lands fifty-two serial real turns inside the
 * budget, which parallel gateway contention turns into a coin flip; it restarts
 * nothing and touches no shared deployment state, so it is not serial.
 */
import { expect, test } from '@playwright/test';

import { AstraApi, type HistoryBlockPage } from '../fixtures/astraApi';
import { apiPath } from '../fixtures/env';
import { expectComposerEnabled, openSessionView } from '../fixtures/sessionPage';
import { trackSessions } from '../fixtures/sessionCleanup';

// The console's own history page size (useFirstPageMessages'
// AUTHORITATIVE_HISTORY_PAGE_SIZE); every history read observed here carries it.
const HISTORY_PAGE_SIZE = 50;
// 52 rows: a full 50-row window plus an older page behind it, so "the page
// behind the held window still loads" is not vacuous.
const WINDOW_TURNS = 26;
// 52 rows: two more than a page, so the newest window and the held one are
// separated by two records that neither of them carries.
const DISPLACING_TURNS = 26;
// Upward wheel arrivals allowed before a loaded input must be in view. The
// whole span is loaded by then, so each arrival only scrolls.
const UPWARD_ARRIVALS = 60;

const sessions = trackSessions();

function ids(page: HistoryBlockPage): string[] {
  return page.messages.map((message) => String(message.message_id));
}

test('a refresh onto a disjoint newest window rebuilds the records between and keeps the held window', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);

  const runId = Date.now();
  const note = (index: number) => (
    `Gap note ${runId}-${String(index + 1).padStart(3, '0')}. `
    + 'Acknowledge briefly in one word; do not use tools.'
  );
  const windowPrompts = Array.from({ length: WINDOW_TURNS }, (_, index) => note(index));
  const displacingPrompts = Array.from(
    { length: DISPLACING_TURNS },
    (_, index) => note(WINDOW_TURNS + index),
  );

  // Real normal inputs; each turn's own completion is the boundary for the
  // next one, and nothing waits on a clock for model work.
  const writeTurns = async (label: string, prompts: string[]) => {
    await test.step(label, async () => {
      for (const [index, content] of prompts.entries()) {
        const result = await api.sendTurn(sessionId, content);
        expect(result.errorText, `${label}: input ${index + 1} must complete without an engine error`).toBeNull();
        expect(result.text.trim(), `${label}: input ${index + 1} must receive a real reply`).not.toBe('');
      }
    });
  };

  await writeTurns(`write ${WINDOW_TURNS} real turns behind the held window`, windowPrompts);
  await api.waitForSession(
    sessionId,
    (session) => session.state === 'READY' && session.last_turn_status === 'COMPLETED',
  );

  // ── The window the reader will hold, from the read the console makes. ────
  const heldWindow = await api.getHistoryBlocks(sessionId);
  expect(heldWindow.messages, 'the reader must open on a full history page').toHaveLength(HISTORY_PAGE_SIZE);
  expect(heldWindow.has_more, 'an older page must exist behind the held window').toBe(true);
  const heldIds = ids(heldWindow);
  const oldestHeldPrompt = windowPrompts[1];
  expect(
    heldWindow.messages.some((message) => message.role === 'user' && String(message.content) === oldestHeldPrompt),
    'the held window starts at the second input; the first sits on the page behind it',
  ).toBe(true);

  // ── Instrument, then open. ───────────────────────────────────────────────
  const historyPath = apiPath(`/sessions/${sessionId}/history-blocks`);
  const historyRequests: URL[] = [];
  page.on('request', (sent) => {
    const url = new URL(sent.url());
    if (sent.method() === 'GET' && url.pathname === historyPath) historyRequests.push(url);
  });
  const olderReads = () => historyRequests
    .filter((url) => url.searchParams.get('before') !== null)
    .map((url) => String(url.searchParams.get('before')));
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => { pageErrors.push(error.message); });
  const consoleErrors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });

  await openSessionView(page, sessionId);
  await expectComposerEnabled(page);
  await expect(page.getByTestId('user-message').filter({ hasText: windowPrompts[WINDOW_TURNS - 1] })).toBeVisible();
  expect(olderReads(), 'opening the conversation must not page backwards on its own').toEqual([]);

  // ── Write more than a page past the held window while the page is open. ──
  await writeTurns(`write ${DISPLACING_TURNS} real turns past the held window`, displacingPrompts);
  await api.waitForSession(
    sessionId,
    (session) => session.state === 'READY' && session.last_turn_status === 'COMPLETED',
  );

  // The platform's own account of the whole transcript, in order, through the
  // read the console makes: the newest page, then every page its cursors
  // name. This is what every window below is checked against.
  const walked: HistoryBlockPage[] = [];
  let cursor: string | null = null;
  for (let guard = 0; guard < 8; guard += 1) {
    const walkedPage: HistoryBlockPage = await api.getHistoryBlocks(sessionId, HISTORY_PAGE_SIZE, cursor ?? undefined);
    walked.push(walkedPage);
    if (!walkedPage.has_more) { cursor = null; break; }
    cursor = String(walkedPage.next_cursor || '');
    expect(cursor, 'a page with history behind it must issue the cursor for it').not.toBe('');
  }
  expect(cursor, 'the walk must reach the start of the session').toBeNull();
  const allIds = walked.slice().reverse().flatMap(ids);
  expect(new Set(allIds).size, 'the platform must hand out every record exactly once').toBe(allIds.length);
  expect(allIds, 'fifty-two turns are one hundred and four records').toHaveLength(2 * (WINDOW_TURNS + DISPLACING_TURNS));
  const displaced = walked[0];
  expect(displaced.messages).toHaveLength(HISTORY_PAGE_SIZE);
  const displacedIds = ids(displaced);
  expect(
    displacedIds.filter((id) => heldIds.includes(id)),
    'the case needs two windows the platform itself reports as sharing no record',
  ).toEqual([]);
  const between = allIds.filter((id) => !heldIds.includes(id) && !displacedIds.includes(id) && allIds.indexOf(id) > allIds.indexOf(heldIds[heldIds.length - 1]));
  expect(
    between,
    'the records between the two windows are the ones an edge-to-edge join would lose',
  ).toHaveLength(allIds.length - heldIds.length - displacedIds.length - allIds.indexOf(heldIds[0]));
  expect(between.length, 'more than a page was written, so the two windows do not touch').toBeGreaterThan(0);
  const olderReadsBeforeRefresh = olderReads().length;

  // ── The refresh, and the reads it must make. ──────────────────────────────
  const newestRead = page.waitForResponse((response) => (
    response.request().method() === 'GET'
    && new URL(response.url()).pathname === historyPath
    && new URL(response.url()).searchParams.get('before') === null
  ));
  await page.evaluate(() => { window.dispatchEvent(new Event('astrabox:manual-refresh')); });
  const newestResponse = await newestRead;
  expect(newestResponse.status()).toBe(200);
  const newestBody = (await newestResponse.json()) as { data: HistoryBlockPage };
  const newestIds = ids(newestBody.data);
  expect(
    newestIds,
    'the refresh must read the displaced newest page, not the one the reader held',
  ).toEqual(displacedIds);
  // The cursor the rebuild must use is the one THIS response issued. A probe
  // made a moment earlier can carry a different checkpoint for the same rows.
  const bridgeCursor = String(newestBody.data.next_cursor || '');
  expect(bridgeCursor, 'a displaced newest page must issue the cursor for the page behind it').not.toBe('');
  const bridgeResponse = await page.waitForResponse((response) => (
    response.request().method() === 'GET'
    && new URL(response.url()).pathname === historyPath
    && new URL(response.url()).searchParams.get('before') === bridgeCursor
  ));
  expect(bridgeResponse.status(), 'the records between are read from the cursor the newest page issued').toBe(200);
  const bridgeBody = (await bridgeResponse.json()) as { data: HistoryBlockPage };
  expect(bridgeBody.data.paging_mode).toBe('blocks');
  const bridgeIds = ids(bridgeBody.data);
  const newestStart = allIds.indexOf(newestIds[0]);
  expect(
    bridgeIds,
    'the page behind the newest one is the fifty records immediately before it, in the platform\'s own order',
  ).toEqual(allIds.slice(newestStart - HISTORY_PAGE_SIZE, newestStart));
  expect(bridgeIds, 'that page is not the held window itself — the transcript grew past it').not.toEqual(heldIds);
  for (const id of between) {
    expect(bridgeIds, `record ${id} sits between the windows and must be on the page the rebuild fetched`).toContain(id);
  }
  expect(
    heldIds.some((id) => bridgeIds.includes(id)),
    'the fetched page shares a boundary with the held window, which is what lets the merge join them',
  ).toBe(true);

  await expect(
    page.getByTestId('user-message').filter({ hasText: displacingPrompts[DISPLACING_TURNS - 1] }),
    'the newest input reaches the page after the refresh',
  ).toBeVisible();
  await expect(page.getByText(/SESSION_HISTORY_WINDOWS_DO_NOT_OVERLAP/)).toHaveCount(0);
  await expect(
    page.getByText(/Failed to load messages|消息加载失败/),
    'a disjoint newest window is not a load failure',
  ).toHaveCount(0);
  expect(pageErrors.filter((message) => message.includes('SESSION_HISTORY_WINDOWS_DO_NOT_OVERLAP'))).toEqual([]);
  expect(
    consoleErrors.filter((message) => message.includes('SESSION_HISTORY_WINDOWS_DO_NOT_OVERLAP')),
    'the refresh must not fail in the history merge path',
  ).toEqual([]);
  expect(
    olderReads().slice(olderReadsBeforeRefresh),
    'closing a one-page gap is exactly one older read, at the cursor the newest page issued',
  ).toEqual([bridgeCursor]);

  // ── The span is continuous and the held window is still loaded: the input
  //    that sits between the windows and the oldest held input are both
  //    reached by scrolling alone, and the page behind the held window loads
  //    on the arrival after that. ──────────────────────────────────────────
  const scroller = page.getByTestId('session-conversation');
  const betweenInput = page.getByTestId('user-message').filter({ hasText: displacingPrompts[0] });
  const oldestHeldInput = page.getByTestId('user-message').filter({ hasText: oldestHeldPrompt });
  const olderReadsAfterRefresh = olderReads().length;
  await scroller.hover();
  const scrollUntilVisible = async (target: typeof betweenInput, what: string) => {
    for (let arrival = 0; arrival < UPWARD_ARRIVALS; arrival += 1) {
      if (await target.isVisible()) return;
      await page.mouse.wheel(0, -100_000);
    }
    await expect(target, what).toBeVisible();
  };
  await scrollUntilVisible(betweenInput, 'the input between the two windows must be reachable after the refresh');
  await expect(betweenInput).toBeInViewport();
  expect(
    olderReads().length,
    'reaching the records between the windows must not read again: the rebuild already fetched them',
  ).toBe(olderReadsAfterRefresh);
  await scrollUntilVisible(oldestHeldInput, 'the oldest input of the held window must be reachable after the refresh');
  await expect(oldestHeldInput).toBeInViewport();
  expect(
    olderReads().length,
    'reaching the held window must not read again either: it was kept, not replaced',
  ).toBe(olderReadsAfterRefresh);

  const pageBehind = page.waitForResponse((response) => (
    response.request().method() === 'GET'
    && new URL(response.url()).pathname === historyPath
    && new URL(response.url()).searchParams.get('before') !== null
    && new URL(response.url()).searchParams.get('before') !== bridgeCursor
  ));
  await scroller.hover();
  await page.mouse.wheel(0, -100_000);
  const behind = await pageBehind;
  expect(behind.status(), 'the page behind the held window still loads on an upward arrival').toBe(200);
  expect(
    String(new URL(behind.url()).searchParams.get('before')),
    'and it is asked for with the cursor the rebuild\'s page issued, not with a stale one',
  ).toBe(String(bridgeBody.data.next_cursor || ''));
  await expect(
    page.getByTestId('user-message').filter({ hasText: windowPrompts[0] }),
    'the very first input is the last thing to arrive, from the page behind the held window',
  ).toBeInViewport();
  expect(pageErrors).toEqual([]);
});
