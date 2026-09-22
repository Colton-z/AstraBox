import { expect, test, type Page, type Route } from '@playwright/test';

import { AstraApi, type MessagePage, type MessageRecord } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { openSessionView } from '../fixtures/sessionPage';
import { trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();

interface VisibleAnchor {
  id: string;
  offsetTop: number;
}

async function firstVisibleAnchor(page: Page): Promise<VisibleAnchor> {
  return page.getByTestId('session-conversation').evaluate((scroller) => {
    const viewport = scroller.getBoundingClientRect();
    const visible = Array.from(scroller.querySelectorAll<HTMLElement>('[data-message-id]'))
      .map((element) => ({ element, rect: element.getBoundingClientRect() }))
      .filter(({ rect }) => rect.bottom > viewport.top && rect.top < viewport.bottom)
      .sort((left, right) => left.rect.top - right.rect.top)[0];
    if (!visible) throw new Error('no visible history anchor');
    return {
      id: String(visible.element.dataset.messageId || ''),
      offsetTop: visible.rect.top - viewport.top,
    };
  });
}

// Sample every paint, not just the final scroll position: a prepend that jumps
// away and then returns still interrupts the reader.
async function observeAnchorStability(page: Page, anchor: VisibleAnchor) {
  return page.getByTestId('session-conversation').evaluate(async (scroller, observed) => (
    new Promise<{
      finalOffsetTop: number | null;
      maxMovement: number;
      missingFrames: number;
      measurements: unknown[];
    }>((resolve) => {
      const startedAt = performance.now();
      let finalOffsetTop: number | null = observed.offsetTop;
      let maxMovement = 0;
      let missingFrames = 0;
      const measurements: unknown[] = [];
      const sample = () => {
        const viewport = scroller.getBoundingClientRect();
        const match = Array.from(scroller.querySelectorAll<HTMLElement>('[data-message-id]'))
          .find((element) => element.dataset.messageId === observed.id);
        if (match) {
          finalOffsetTop = match.getBoundingClientRect().top - viewport.top;
          const movement = Math.abs(finalOffsetTop - observed.offsetTop);
          if (measurements.length === 0 || movement > maxMovement) {
            const list = scroller.querySelector<HTMLElement>('[data-testid="virtuoso-item-list"]');
            measurements.push({
              elapsedMs: performance.now() - startedAt,
              offsetTop: finalOffsetTop,
              scrollTop: scroller.scrollTop,
              scrollHeight: scroller.scrollHeight,
              listStyle: list?.getAttribute('style'),
              rows: Array.from(scroller.querySelectorAll<HTMLElement>('[data-known-size]'))
                .map((element) => ({
                  index: element.dataset.itemIndex,
                  knownSize: element.dataset.knownSize,
                  height: element.getBoundingClientRect().height,
                  offsetTop: element.getBoundingClientRect().top - viewport.top,
                  messageId: element.querySelector<HTMLElement>('[data-message-id]')?.dataset.messageId,
                })),
            });
          }
          maxMovement = Math.max(maxMovement, movement);
        } else {
          finalOffsetTop = null;
          missingFrames += 1;
        }
        if (performance.now() - startedAt >= 5_000) {
          scroller.removeAttribute('data-history-anchor-sampling');
          resolve({ finalOffsetTop, maxMovement, missingFrames, measurements });
        } else {
          scroller.setAttribute('data-history-anchor-sampling', observed.id);
          requestAnimationFrame(sample);
        }
      };
      requestAnimationFrame(sample);
    })
  ), anchor);
}

// Delay delivery, not production: the backend serves the real page unchanged,
// and the browser must acknowledge its first sample before seeing that page.
async function observeHistoryUpdate(
  page: Page,
  historyPath: string,
  before: string | null,
  trigger: () => Promise<void>,
) {
  const matches = (url: URL) => url.pathname === historyPath && url.searchParams.get('before') === before;
  let release!: () => void;
  const delivery = new Promise<void>((resolve) => { release = resolve; });
  let ready!: () => void;
  let failed!: (error: unknown) => void;
  const held = new Promise<void>((resolve, reject) => { ready = resolve; failed = reject; });
  let claimed = false;
  const handler = async (route: Route) => {
    if (claimed || route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    claimed = true;
    try {
      const response = await route.fetch();
      ready();
      await delivery;
      await route.fulfill({ response });
    } catch (error) {
      failed(error);
      throw error;
    }
  };
  await page.route(matches, handler);
  try {
    await Promise.all([held, trigger()]);
    const anchor = await firstVisibleAnchor(page);
    const observation = observeAnchorStability(page, anchor);
    await expect(page.getByTestId('session-conversation')).toHaveAttribute('data-history-anchor-sampling', anchor.id);
    const response = page.waitForResponse((candidate) => (
      candidate.request().method() === 'GET' && matches(new URL(candidate.url()))
    ));
    release();
    const [reply, stability] = await Promise.all([response, observation]);
    await test.info().attach('history-anchor-measurements', {
      body: JSON.stringify({ before, anchor, stability }),
      contentType: 'application/json',
    });
    return { reply, stability, anchor };
  } finally {
    release();
    await page.unroute(matches, handler);
  }
}

function olderCursor(records: MessageRecord[]): string {
  const values = records.map((record) => String(record.created_at || '').trim()).sort();
  expect(values.every(Boolean), 'every real history row needs its pagination timestamp').toBe(true);
  expect(values[0], 'the oldest timestamp must identify one boundary row').not.toBe(values[1]);
  return values[0];
}

test('long session history loads one page per upward arrival and preserves the reader on refresh', async ({ page, request, browser }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);

  const runId = Date.now();
  const prompts = Array.from({ length: 51 }, (_, index) => (
    `History note ${runId}-${String(index + 1).padStart(3, '0')}. Acknowledge briefly in one word; do not use tools.`
  ));
  // Real normal inputs, no imported rows or substituted responses. Each SSE
  // completion is the boundary for the next input; no extra per-turn polling.
  for (const [index, prompt] of prompts.entries()) {
    await test.step(`write real history input ${index + 1} of ${prompts.length}`, async () => {
      const result = await api.sendTurn(sessionId, prompt);
      expect(result.errorText, `input ${index + 1} must complete without an engine error`).toBeNull();
      expect(result.text.trim(), `input ${index + 1} must receive a real reply`).not.toBe('');
    });
  }
  await api.waitForSession(sessionId, (session) => session.state === 'READY' && session.last_turn_status === 'COMPLETED');

  const latest = await api.getMessages(sessionId);
  expect(latest.messages).toHaveLength(50);
  expect(latest.has_more).toBe(true);
  const firstCursor = olderCursor(latest.messages);
  const middle = await api.data<MessagePage>('GET', `/sessions/${sessionId}/messages?limit=50&before=${encodeURIComponent(firstCursor)}`);
  expect(middle.messages).toHaveLength(50);
  expect(middle.has_more).toBe(true);
  const secondCursor = olderCursor(middle.messages);
  expect(secondCursor < firstCursor).toBe(true);
  const earliest = await api.data<MessagePage>('GET', `/sessions/${sessionId}/messages?limit=50&before=${encodeURIComponent(secondCursor)}`);
  expect(earliest.messages).toHaveLength(2);
  expect(earliest.has_more).toBe(false);
  const history = [...earliest.messages, ...middle.messages, ...latest.messages];
  expect(new Set(history.map((message) => message.message_id)).size).toBe(102);
  const users = history.filter((message) => message.role === 'user');
  expect(users.map((message) => message.content)).toEqual(prompts);
  expect(users.every((message) => Boolean(message.client_message_id))).toBe(true);
  expect(new Set(users.map((message) => message.client_message_id)).size).toBe(51);
  expect(history.filter((message) => message.role === 'assistant')).toHaveLength(51);

  // The same durable history through the read the console actually makes. Its
  // cursors are server-issued and opaque, so the page a spec expects the
  // browser to ask for can only be named by the token its predecessor carried.
  const newestBlocks = await api.getHistoryBlocks(sessionId);
  expect(newestBlocks.messages).toHaveLength(50);
  expect(newestBlocks.has_more).toBe(true);
  const firstBlockCursor = String(newestBlocks.next_cursor || '');
  expect(firstBlockCursor, 'a page with history behind it must issue the cursor for it').not.toEqual('');
  const middleBlocks = await api.getHistoryBlocks(sessionId, 50, firstBlockCursor);
  expect(middleBlocks.messages).toHaveLength(50);
  expect(middleBlocks.has_more).toBe(true);
  const secondBlockCursor = String(middleBlocks.next_cursor || '');
  expect(secondBlockCursor).not.toEqual('');
  expect(secondBlockCursor).not.toEqual(firstBlockCursor);
  const earliestBlocks = await api.getHistoryBlocks(sessionId, 50, secondBlockCursor);
  expect(earliestBlocks.messages).toHaveLength(2);
  expect(earliestBlocks.has_more).toBe(false);
  expect(
    [...earliestBlocks.messages, ...middleBlocks.messages, ...newestBlocks.messages]
      .map((message) => message.message_id),
    'the folded read must carry the same records, in the same order, as the raw read',
  ).toEqual(history.map((message) => message.message_id));

  const historyPath = apiPath(`/sessions/${sessionId}/history-blocks`);
  const olderRequests: string[] = [];
  const historyRequests: URL[] = [];
  page.on('request', (observed) => {
    const url = new URL(observed.url());
    if (observed.method() !== 'GET' || url.pathname !== historyPath) return;
    historyRequests.push(url);
    const before = url.searchParams.get('before');
    if (before) olderRequests.push(before);
  });
  await openSessionView(page, sessionId);
  const scroller = page.getByTestId('session-conversation');
  await expect(scroller).toBeVisible();
  await expect(page.getByTestId('user-message').filter({ hasText: prompts[50] })).toBeVisible();
  expect(olderRequests, 'opening long history must not fetch all older pages').toEqual([]);
  expect(historyRequests.length, 'initial history must not repeatedly reload its first page').toBeLessThanOrEqual(2);
  expect(historyRequests.length).toBeGreaterThan(0);
  expect(historyRequests.every((url) => url.searchParams.get('limit') === '50')).toBe(true);
  const firstWindowRows = scroller.locator('[data-message-virtual-index]');
  expect(await firstWindowRows.count()).toBeGreaterThan(0);
  expect(await firstWindowRows.count(), 'the initial history window must not mount all 50 rows').toBeLessThan(50);

  for (const [index, cursor] of [firstBlockCursor, secondBlockCursor].entries()) {
    await test.step(`physical upward arrival ${index + 1}`, async () => {
      const { reply, stability, anchor } = await observeHistoryUpdate(page, historyPath, cursor, async () => {
        await scroller.hover();
        await page.mouse.wheel(0, -100_000);
        await expect.poll(() => olderRequests.length).toBe(index + 1);
        await expect.poll(() => scroller.evaluate((element) => element.scrollTop), {
          message: 'the physical upward wheel must reach the boundary before the held page is delivered',
        }).toBeLessThanOrEqual(1);
      });
      expect(reply.status()).toBe(200);
      const body = await reply.json() as { data: MessagePage };
      expect(body.data.messages.map((message) => message.message_id)).toEqual(
        (index === 0 ? middle : earliest).messages.map((message) => message.message_id),
      );
      expect(olderRequests, 'one upward arrival must not chase the next page').toEqual([firstBlockCursor, secondBlockCursor].slice(0, index + 1));
      expect(stability.missingFrames, `anchor ${anchor.id} must remain mounted`).toBe(0);
      expect(stability.finalOffsetTop).not.toBeNull();
      expect(stability.maxMovement).toBeLessThanOrEqual(4);
    });
  }

  await scroller.hover();
  await page.mouse.wheel(0, -100_000);
  const oldestInput = page.getByTestId('user-message').filter({ hasText: prompts[0] });
  await expect(oldestInput).toBeInViewport();
  expect(await scroller.locator('[data-message-id]').count(), 'offscreen loaded history must not all remain in the DOM').toBeLessThan(history.length);
  expect(olderRequests).toEqual([firstBlockCursor, secondBlockCursor]);
  await page.getByRole('button', { name: /Scroll to latest messages|回到最新消息/ }).click();
  await expect(page.getByTestId('user-message').filter({ hasText: prompts[50] })).toBeVisible();
  await scroller.hover();
  await page.mouse.wheel(0, -100_000);
  await expect(oldestInput).toBeInViewport();
  expect(olderRequests, 'both ends of the loaded window must remain locally readable').toEqual([firstBlockCursor, secondBlockCursor]);

  // This is the application's existing refresh command, not a page reload:
  // refreshing current truth must keep the older history the reader loaded.
  const { reply: refreshed, stability } = await observeHistoryUpdate(page, historyPath, null, async () => {
    await page.evaluate(() => { window.dispatchEvent(new Event('astrabox:manual-refresh')); });
  });
  expect(refreshed.status()).toBe(200);
  expect(stability.missingFrames).toBe(0);
  expect(stability.maxMovement).toBeLessThanOrEqual(4);
  expect(olderRequests, 'refreshing a loaded window must not reload older pages').toEqual([firstBlockCursor, secondBlockCursor]);
  await expect(oldestInput).toBeInViewport();

  // A reload discards the loaded window, but reuses the same durable history.
  // Only the first older-page request fails; its retry reaches the real API.
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('user-message').filter({ hasText: prompts[50] })).toBeVisible();
  expect(olderRequests).toEqual([firstBlockCursor, secondBlockCursor]);
  let olderPageAttempts = 0;
  const retryPageUrl = (url: URL) => (
    url.pathname === historyPath && url.searchParams.get('before') === firstBlockCursor
  );
  const failFirstOlderPage = async (route: Route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    olderPageAttempts += 1;
    if (olderPageAttempts === 1) {
      await route.abort('failed');
      return;
    }
    await route.continue();
  };
  await page.route(retryPageUrl, failFirstOlderPage);
  try {
    const retriedResponse = page.waitForResponse((candidate) => (
      candidate.request().method() === 'GET' && retryPageUrl(new URL(candidate.url()))
    ));
    await scroller.hover();
    await page.mouse.wheel(0, -100_000);
    await expect.poll(() => olderPageAttempts, {
      message: 'the failed older-history cursor must be retried automatically',
      timeout: 10_000,
    }).toBe(2);
    const reply = await retriedResponse;
    expect(reply.status()).toBe(200);
    const body = await reply.json() as { data: MessagePage };
    expect(body.data.messages.map((message) => message.message_id)).toEqual(
      middle.messages.map((message) => message.message_id),
    );
    const nearestOlderInput = middle.messages.filter((message) => message.role === 'user').at(-1)!;
    await expect(page.getByTestId('user-message').filter({ hasText: String(nearestOlderInput.content) })).toBeVisible();
    await expect(page.getByText(/NETWORK_ERROR|Failed to fetch/)).toHaveCount(0);
    expect(olderPageAttempts).toBe(2);
    expect(olderRequests).toEqual([firstBlockCursor, secondBlockCursor, firstBlockCursor, firstBlockCursor]);
  } finally {
    await page.unroute(retryPageUrl, failFirstOlderPage);
  }

  // The same real history must also be complete for a token-only viewer.
  // Reuse the 51 completed turns instead of generating another long session.
  const share = await new PlatformApi(request).createShare(sessionId);
  expect(share.token).toBeTruthy();
  const viewer = await browser.newContext({
    storageState: { cookies: [], origins: [] },
    extraHTTPHeaders: {},
  });
  try {
    expect((await viewer.cookies()).length, 'the viewer must not inherit the owner login').toBe(0);
    const sharedPage = await viewer.newPage();
    const sharedRequests: URL[] = [];
    const sharedResponses: Array<Promise<MessagePage>> = [];
    const sharedAuthentication: Array<Promise<boolean>> = [];
    const sharedHistoryPath = apiPath(`/share/${share.token}/messages`);
    sharedPage.on('request', (observed) => {
      const url = new URL(observed.url());
      if (observed.method() === 'GET' && url.pathname === sharedHistoryPath) {
        sharedRequests.push(url);
        sharedAuthentication.push(observed.allHeaders().then((headers) => (
          Boolean(headers.cookie || headers.authorization)
        )));
      }
    });
    sharedPage.on('response', (response) => {
      if (response.request().method() !== 'GET' || new URL(response.url()).pathname !== sharedHistoryPath) return;
      sharedResponses.push(response.json().then((body: { data: MessagePage }) => body.data));
    });
    await sharedPage.goto(appPath(`/share/${share.token}`));
    await expect(sharedPage.getByTestId('user-message')).toHaveCount(51);
    await expect(sharedPage.getByTestId('user-message')).toContainText(prompts);
    await expect(sharedPage.getByTestId('assistant-message')).toHaveCount(51);
    const replies = await sharedPage.getByTestId('assistant-message').evaluateAll((elements) => (
      elements.map((element) => Array.from(element.querySelectorAll('[data-testid="assistant-text"]'))
        .map((part) => part.textContent || '').join('').trim())
    ));
    expect(replies.every(Boolean), 'every shared turn must retain its actual reply').toBe(true);
    expect(sharedRequests.map((url) => url.searchParams.get('before'))).toEqual([null, firstCursor, secondCursor]);
    expect(sharedRequests.every((url) => url.searchParams.get('limit') === '50')).toBe(true);
    expect(await Promise.all(sharedAuthentication), 'history must load with the share token alone')
      .toEqual([false, false, false]);
    const sharedPages = await Promise.all(sharedResponses);
    expect(sharedPages.map((part) => part.messages.length)).toEqual([50, 50, 2]);
    expect([...sharedPages].reverse().flatMap((part) => part.messages.map((message) => message.message_id)))
      .toEqual(history.map((message) => message.message_id));
    await expect(sharedPage.getByTestId('composer-prompt')).toHaveCount(0);
    await expect(sharedPage.getByTestId('composer-submit')).toHaveCount(0);
  } finally {
    await viewer.close();
  }
});
