/**
 * E2E: the top of a long transcript answers the keyboard, not only the wheel.
 *
 * A reader who moves through a conversation with Home or PageUp instead of a
 * wheel must still reach the start of what they said. The transcript requests
 * its older page through a single gate: `requestOlder` returns without asking
 * unless `userRequestedOlderRef` is set
 * (`frontend/src/session/components/VirtualizedMessageList.tsx:56-61`). Three
 * gestures arm that ref — an upward `wheel` (`:75-82`), a downward-dragging
 * `touchmove` (`:86-97`) and ArrowUp / PageUp / Home on the focused scroller
 * (`:104-111`) — and the opposite direction of each disarms it. Every arrival
 * at the boundary reaches the same gate: the scroller's own `scroll` handler
 * (`:112-114`) and Virtuoso's `startReached` (`:175`) ask, and are refused
 * unless one of those three armed the intent. An idle transcript announces
 * nothing above it either: the overlay renders only while a page is in flight
 * or after one failed (`:197-217`), so there is no button a reader could aim
 * at instead — the arrival IS the request, and for a reader with no pointing
 * device the keyboard is the only way to make one.
 *
 * The arming and the asking are two different events, which is why the
 * keypress alone is the whole gesture. `keydown` runs before the browser has
 * scrolled, so at that instant `scrollTop` is not yet at the boundary; the
 * handler arms the intent, Chromium scrolls the focused container, and the
 * `scroll` that follows finds the intent set and asks.
 *
 * The journey is therefore keyboard-only once the page is reloaded: focus the
 * transcript, press Home, and the older page must be fetched and rendered.
 *
 * Two conditions are asserted separately on purpose. `scrollTop <= 1` says the
 * keypress moved the scroller; the older-page request says the arrival was
 * heard. Folded into one assertion, a run that never reached the boundary
 * would read as a missing load and send a reader to the wrong file.
 *
 * Scope, stated rather than implied:
 *  - 26 completed turns leave 52 durable rows, so exactly one older page
 *    exists. That proves an arrival loads a page; it cannot prove an arrival
 *    loads only ONE page. `session-history-window.exclusive.spec.ts` owns that
 *    invariant, with three pages.
 *  - It does not prove that a SECOND keyboard arrival loads a SECOND page. The
 *    intent flag is consumed per page (`VirtualizedMessageList.tsx:59`) and
 *    cleared whenever `loadingMore` flips (`VirtualizedMessageList.tsx:135-137`),
 *    so an implementation that arms the gate exactly once satisfies this file.
 *    That case belongs in the three-page sibling.
 *  - The scrollbar-drag arrival shares the same gate and is not driven here:
 *    headless Chromium hides scrollbars, so the thumb has no geometry to drag.
 *    The half proven here is the one with the accessibility consequence.
 *
 * Two dependencies outside this repository each fail with their own message
 * rather than blaming the product: react-virtuoso 4.18.13 puts the scroller in
 * the tab order (`tabIndex: 0` in `react-virtuoso/dist/index.mjs:2630`, spread
 * ahead of the props this component passes, so the transcript inherits it),
 * and Chromium scrolls the focused scroll container on Home. If either stops
 * holding, the focus or the scrollTop assertion goes red first and names
 * itself.
 *
 * Exclusive because it lands 26 serial real turns and two page loads inside
 * the suite budget, which parallel model-gateway and prepared-slot contention
 * turns into a coin flip. The cost is bounded by precedent rather than by
 * estimate: `session-history-window.exclusive.spec.ts:159-171` writes 51 real
 * turns plus a browser walk and a share-link walk inside the same 180s cap, so
 * 26 turns is about half of a walk the lane already carries. It restarts
 * nothing and mutates no shared deployment state, so it is not a member of the
 * serial group.
 */
import { expect, test } from '@playwright/test';

import { AstraApi, type MessagePage, type MessageRecord } from '../fixtures/astraApi';
import { apiPath, parseTimeoutEnv } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

const sessions = trackSessions();

const OLDER_PAGE_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_HISTORY_OLDER_PAGE_TIMEOUT_MS', 15_000);
const TOP_REACH_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_HISTORY_TOP_REACH_TIMEOUT_MS', 10_000);
// An exhausted boundary emits nothing, so the closing assertion needs a window
// to be non-vacuous: keep pressing against the top for this long and count.
const EXHAUSTED_OBSERVE_MS = parseTimeoutEnv('ASTRABOX_E2E_HISTORY_EXHAUSTED_OBSERVE_MS', 2_000);

/**
 * The `before` cursor the raw message read pages on, or a refusal.
 *
 * That read pages on the oldest `created_at` of the page it holds, which is
 * safe only while that timestamp identifies one row. A tie would make the page
 * it returns ambiguous and the precondition below meaningless, so it is
 * checked here rather than absorbed.
 */
function olderCursor(records: MessageRecord[]): string {
  const values = records.map((record) => String(record.created_at || '').trim()).sort();
  expect(values.every(Boolean), 'every real history row needs its pagination timestamp').toBe(true);
  expect(values[0], 'the oldest timestamp must identify one boundary row').not.toBe(values[1]);
  return values[0];
}

test('a keyboard reader reaching the top of a reloaded long transcript loads its older history', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);

  // Real inputs and real replies, no imported rows. 26 completed turns leave 52
  // durable rows, the arithmetic minimum above the console's 50-row page
  // (`useFirstPageMessages.ts:25`) that makes `has_more` true.
  const runId = Date.now();
  const prompts = Array.from({ length: 26 }, (_, index) => (
    `History note ${runId}-${String(index + 1).padStart(3, '0')}. Acknowledge briefly in one word; do not use tools.`
  ));
  for (const [index, prompt] of prompts.entries()) {
    await test.step(`write real history input ${index + 1} of ${prompts.length}`, async () => {
      const result = await api.sendTurn(sessionId, prompt);
      expect(result.errorText, `input ${index + 1} must complete without an engine error`).toBeNull();
      expect(result.text.trim(), `input ${index + 1} must receive a real reply`).not.toBe('');
    });
  }
  await api.waitForSession(sessionId, (session) => (
    session.state === 'READY' && session.last_turn_status === 'COMPLETED'
  ));

  // The precondition is asserted over the API before the browser is involved:
  // on a short transcript every assertion below would hold for the wrong reason.
  const newest = await api.getMessages(sessionId);
  expect(
    newest.messages.length,
    `${prompts.length} completed turns must fill the console's first history page; it carried ${newest.messages.length} rows`,
  ).toBe(50);
  expect(newest.has_more, 'an older page is the precondition of this journey').toBe(true);
  const cursor = olderCursor(newest.messages);
  const older = await api.data<MessagePage>(
    'GET',
    `/sessions/${sessionId}/messages?limit=50&before=${encodeURIComponent(cursor)}`,
  );
  expect(older.has_more, 'one older page must carry the remainder of this transcript').toBe(false);
  const firstPromptIndex = older.messages.findIndex((message) => (
    message.role === 'user' && String(message.content ?? '') === prompts[0]
  ));
  expect(
    firstPromptIndex,
    `the older page must carry the first thing the reader said; it holds ${older.messages.length} rows`,
  ).toBeGreaterThanOrEqual(0);
  const oldestReply = older.messages
    .slice(firstPromptIndex + 1)
    .find((message) => message.role === 'assistant');
  expect(oldestReply, 'the first prompt must be followed by the reply that answered it').toBeTruthy();
  const oldestReplyId = String(oldestReply?.message_id ?? '');

  // The console asks for its older page with the token the page it holds
  // carried, not with a timestamp it derived, so the expected request can only
  // be named by reading that token off the same read the console makes.
  const newestBlocks = await api.getHistoryBlocks(sessionId);
  expect(newestBlocks.messages).toHaveLength(50);
  expect(newestBlocks.has_more, 'an older page is the precondition of this journey').toBe(true);
  const blockCursor = String(newestBlocks.next_cursor || '');
  expect(blockCursor, 'a page with history behind it must issue the cursor for it').not.toEqual('');

  const historyPath = apiPath(`/sessions/${sessionId}/history-blocks`);
  const olderRequests: string[] = [];
  page.on('request', (observed) => {
    if (observed.method() !== 'GET') return;
    const url = new URL(observed.url());
    if (url.pathname !== historyPath) return;
    const before = url.searchParams.get('before');
    if (before) olderRequests.push(before);
  });

  const newestInput = page.getByTestId('user-message').filter({ hasText: prompts[prompts.length - 1] });
  await openSessionView(page, sessionId);
  const scroller = page.getByTestId('session-conversation');
  await expect(scroller).toBeVisible();
  await expect(newestInput).toBeVisible();

  // The reader's own reload: it discards the loaded window and leaves the
  // durable history to be reached again.
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(newestInput).toBeVisible();
  expect(
    olderRequests,
    'opening and reloading must not fetch older pages before the reader asks',
  ).toEqual([]);

  await expect(
    scroller,
    'the transcript must be in the tab order for a keyboard reader',
  ).toHaveJSProperty('tabIndex', 0);
  await scroller.focus();
  await expect(
    scroller,
    'a scroll container the reader cannot focus cannot be driven from the keyboard',
  ).toBeFocused();

  // Keyboard only from here: no wheel, no touch, no programmatic scroll.
  await page.keyboard.press('Home');
  await expect.poll(() => scroller.evaluate((element) => element.scrollTop), {
    message: 'the keyboard alone must carry the reader to the top of the transcript',
    timeout: TOP_REACH_BUDGET_MS,
  }).toBeLessThanOrEqual(1);

  await expect.poll(() => olderRequests, {
    message: 'reaching the top of the transcript with the keyboard must load the older page',
    timeout: OLDER_PAGE_BUDGET_MS,
  }).toEqual([blockCursor]);

  // The prepended page lands above the reading window, which is where the
  // reader was: reaching what arrived is another keyboard arrival.
  await scroller.focus();
  await expect(scroller).toBeFocused();
  await page.keyboard.press('Home');
  await expect.poll(() => scroller.evaluate((element) => element.scrollTop), {
    message: 'the keyboard must also reach the top of the page that was prepended',
    timeout: TOP_REACH_BUDGET_MS,
  }).toBeLessThanOrEqual(1);

  await expect(
    page.getByTestId('user-message').filter({ hasText: prompts[0] }),
    'the reader must see the first thing they said in this conversation',
  ).toBeInViewport();
  await expect(
    scroller.locator(`[data-message-id="${oldestReplyId}"]`).getByTestId('assistant-message'),
    'the oldest reply must render, not merely arrive on the wire',
  ).toBeVisible();

  // An exhausted boundary must settle, not turn into a request loop: the
  // reader keeps pressing against a top that has nothing left above it.
  await scroller.focus();
  const observationDeadline = Date.now() + EXHAUSTED_OBSERVE_MS;
  while (Date.now() < observationDeadline) {
    await page.keyboard.press('Home');
    await page.waitForTimeout(250);
  }
  await expect(page.getByText(/NETWORK_ERROR|Failed to fetch/)).toHaveCount(0);
  expect(
    olderRequests,
    'a transcript whose history is exhausted must stop asking for older pages',
  ).toEqual([blockCursor]);
});
