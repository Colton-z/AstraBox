import { expect, test, type APIResponse, type Page, type Route } from '@playwright/test';

import {
  AstraApi,
  messageText,
  messageToolCalls,
  type HistoryBlockPage,
  type MessagePage,
  type MessageRecord,
} from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { apiPath, appPath } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { normalizeRendered } from '../fixtures/renderedTranscript';
import { openSessionView } from '../fixtures/sessionPage';
import { trackSessions } from '../fixtures/sessionCleanup';
import { mirrorSseBodies, sessionStreamBodies } from '../fixtures/sseBodies';

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

// Visit overlapping viewports so every history message must actually be
// readable, while allowing the list to unmount messages that have left the
// viewport. A row's `data-message-id` is the message's own id: the input's
// client_message_id for a user message, the response id for a reply.
async function expectCompleteVisibleHistory(page: Page, history: MessageRecord[]) {
  const scroller = page.getByTestId('session-conversation');
  const expected = new Map(history.map((message, index) => [message.id, { message, index }]));
  const seen = new Set<string>();
  const snapshot = () => scroller.evaluate((element) => {
    const viewport = element.getBoundingClientRect();
    return {
      top: element.scrollTop,
      height: element.clientHeight,
      rows: Array.from(element.querySelectorAll<HTMLElement>('[data-message-id]')).map((row) => {
        const rect = row.getBoundingClientRect();
        return {
          id: row.dataset.messageId!,
          visible: rect.bottom > viewport.top && rect.top < viewport.bottom,
          user: row.querySelector('[data-testid="user-message"]')?.textContent ?? null,
          assistant: Array.from(row.querySelectorAll('[data-testid="assistant-text"]'))
            .map((part) => part.textContent || '').join(''),
        };
      }),
    };
  });
  for (let step = 0; step < 80; step += 1) {
    const current = await snapshot();
    const ids = current.rows.map((row) => row.id);
    expect(new Set(ids).size, 'mounted history must not duplicate a message').toBe(ids.length);
    const positions = current.rows.map((row) => {
      expect(expected.has(row.id), `unexpected shared message ${row.id}`).toBe(true);
      return expected.get(row.id)!.index;
    });
    expect(positions, 'mounted shared messages must retain history order')
      .toEqual([...positions].sort((left, right) => left - right));
    for (const row of current.rows.filter((item) => item.visible)) {
      const { message } = expected.get(row.id)!;
      if (message.role === 'user') {
        expect(row.user).not.toBeNull();
        expect(normalizeRendered(row.user!)).toContain(normalizeRendered(messageText(message)));
      } else {
        expect(row.user).toBeNull();
        expect(normalizeRendered(row.assistant), `shared reply ${row.id} must retain its text`)
          .toBe(normalizeRendered(messageText(message)));
        expect(row.assistant.trim()).not.toBe('');
      }
      seen.add(row.id);
    }
    if (seen.size === history.length) break;
    await scroller.hover();
    await page.mouse.wheel(0, -Math.max(1, Math.floor(current.height / 2)));
    await expect.poll(async () => {
      const next = await snapshot();
      return Math.abs(next.top - current.top) > 1
        || next.rows.map((row) => row.id).join(',') !== ids.join(',');
    }, { message: 'the reader must advance through older shared history' }).toBe(true);
  }
  expect(history.filter((message) => !seen.has(message.id)).map((message) => message.id),
    'every real user input and assistant reply must be readable in the share').toEqual([]);
}

/** The server-issued cursor for the page before `page`; a page with more history must carry one. */
function olderCursor(page: MessagePage): string {
  const cursor = String(page.before ?? '').trim();
  expect(cursor, 'a page with history behind it must issue the cursor for it').not.toBe('');
  return cursor;
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
  prompts[0] += '\n\nProject context: We are preparing release notes for a shared workspace.'
    + '\n\nAudience: The notes will be read by people joining the project for the first time.'
    + '\n\nStyle: Keep the notes clear, with separate paragraphs for each topic.';
  // Real normal inputs, no imported rows or substituted responses. Each
  // response's `finish` on the session stream is the boundary for the next
  // input; no extra per-input polling.
  const inputIds: string[] = [];
  for (const [index, prompt] of prompts.entries()) {
    await test.step(`write real history input ${index + 1} of ${prompts.length}`, async () => {
      const result = await api.sendTurn(sessionId, prompt);
      expect(result.errorText, `input ${index + 1} must complete without an engine error`).toBeNull();
      expect(result.text.trim(), `input ${index + 1} must receive a real reply`).not.toBe('');
      inputIds.push(result.inputId);
    });
  }
  await api.waitForSession(sessionId, (session) => (
    session.state === 'READY' && session.last_response?.outcome === 'completed'
  ));

  const latest = await api.getMessages(sessionId);
  expect(latest.messages).toHaveLength(50);
  expect(latest.has_more).toBe(true);
  const firstCursor = olderCursor(latest);
  const middle = await api.data<MessagePage>('GET', `/sessions/${sessionId}/messages?limit=50&before=${encodeURIComponent(firstCursor)}`);
  expect(middle.messages).toHaveLength(50);
  expect(middle.has_more).toBe(true);
  const secondCursor = olderCursor(middle);
  expect(secondCursor, 'the history cursor must move backwards, not repeat').not.toBe(firstCursor);
  const earliest = await api.data<MessagePage>('GET', `/sessions/${sessionId}/messages?limit=50&before=${encodeURIComponent(secondCursor)}`);
  expect(earliest.messages).toHaveLength(2);
  expect(earliest.has_more).toBe(false);
  const history = [...earliest.messages, ...middle.messages, ...latest.messages];
  expect(new Set(history.map((message) => message.id)).size).toBe(102);
  const users = history.filter((message) => message.role === 'user');
  expect(users.map((message) => messageText(message))).toEqual(prompts);
  // Each user message is the input its send was acknowledged for, in order:
  // the receipts and the history name the same 51 inputs.
  expect(users.map((message) => message.metadata?.input_id)).toEqual(inputIds);
  expect(new Set(users.map((message) => message.id)).size, 'every input keeps its own client_message_id').toBe(51);
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
      .map((message) => message.id),
    'the folded read must carry the same messages, in the same order, as the raw read',
  ).toEqual(history.map((message) => message.id));

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
      expect(body.data.messages.map((message) => message.id)).toEqual(
        (index === 0 ? middle : earliest).messages.map((message) => message.id),
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
  const rowHeights = await scroller.evaluate((element, ids) => ids.map((id) => {
    const row = Array.from(element.querySelectorAll<HTMLElement>('[data-message-id]'))
      .find((item) => item.dataset.messageId === id);
    if (!row) throw new Error(`missing variable-height history row ${id}`);
    return row.getBoundingClientRect().height;
  }), [users[0].id, users[1].id]);
  expect(rowHeights[0] - rowHeights[1],
    'the final partial page must exercise substantially different row heights').toBeGreaterThan(60);
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
    expect(body.data.messages.map((message) => message.id)).toEqual(
      middle.messages.map((message) => message.id),
    );
    const nearestOlderInput = middle.messages.filter((message) => message.role === 'user').at(-1)!;
    await expect(page.getByTestId('user-message').filter({ hasText: messageText(nearestOlderInput) })).toBeVisible();
    await expect(page.getByText(/NETWORK_ERROR|Failed to fetch/)).toHaveCount(0);
    expect(olderPageAttempts).toBe(2);
    expect(olderRequests).toEqual([firstBlockCursor, secondBlockCursor, firstBlockCursor, firstBlockCursor]);
  } finally {
    await page.unroute(retryPageUrl, failFirstOlderPage);
  }

  // The same real history must also be complete for a token-only viewer.
  // Reuse the 51 completed responses instead of generating another long session.
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
    const sharedResponses: Array<Promise<HistoryBlockPage>> = [];
    const sharedAuthentication: Array<Promise<boolean>> = [];
    const sharedHistoryPath = apiPath(`/share/${share.token}/history-blocks`);
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
      sharedResponses.push(response.json().then((body: { data: HistoryBlockPage }) => body.data));
    });
    await sharedPage.goto(appPath(`/share/${share.token}`));
    await expect(sharedPage.getByTestId('user-message').filter({ hasText: prompts[50] })).toBeInViewport();
    expect(sharedRequests.map((url) => url.searchParams.get('before')),
      'opening the share must not eagerly fetch all history').toEqual([null]);
    await expectCompleteVisibleHistory(sharedPage, history);
    expect(sharedRequests.map((url) => url.searchParams.get('before')))
      .toEqual([null, firstBlockCursor, secondBlockCursor]);
    expect(sharedRequests.every((url) => url.searchParams.get('limit') === '50')).toBe(true);
    expect(await Promise.all(sharedAuthentication), 'history must load with the share token alone')
      .toEqual([false, false, false]);
    const sharedPages = await Promise.all(sharedResponses);
    expect(sharedPages.map((part) => part.messages.length)).toEqual([50, 50, 2]);
    expect([...sharedPages].reverse().flatMap((part) => part.messages.map((message) => message.id)))
      .toEqual(history.map((message) => message.id));
    await expect(sharedPage.getByTestId('composer-prompt')).toHaveCount(0);
    await expect(sharedPage.getByTestId('composer-submit')).toHaveCount(0);
  } finally {
    await viewer.close();
  }
});

test('a delayed history read keeps the completed live reply and its tools', async ({ page, request }) => {
  await mirrorSseBodies(page);
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const { session_id: sessionId } = await api.startConversation(agent.agent_id);
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  await openSessionView(page, sessionId);
  const run = Date.now();
  const started = `history-${run}.started`;
  const releaseFile = `history-${run}.release`;
  const marker = `HISTORY_FINISHED_${run}`;
  const historyPath = apiPath(`/sessions/${sessionId}/history-blocks`);
  const matchesHistory = (url: URL) => url.pathname === historyPath;
  let allowCapture!: () => void;
  const captureAllowed = new Promise<void>((resolve) => { allowCapture = resolve; });
  let releaseRead!: () => void;
  const delivery = new Promise<void>((resolve) => { releaseRead = resolve; });
  let snapshot: HistoryBlockPage | null = null;
  let captured = false;
  let delivered = false;
  await page.route(matchesHistory, async (route) => {
    if (captured) return route.continue();
    captured = true;
    await captureAllowed;
    const response = await route.fetch();
    snapshot = (await response.json()).data;
    await delivery;
    await route.fulfill({ response });
    delivered = true;
  });
  const command = [
    "python3 - <<'PY'", 'import time', 'from pathlib import Path',
    `Path('/workspace/${started}').write_text('started')`,
    'deadline = time.monotonic() + 180',
    `while not Path('/workspace/${releaseFile}').exists():`,
    "    if time.monotonic() >= deadline: raise RuntimeError('history acceptance release timed out')",
    '    time.sleep(0.1)', `print('${marker}')`, 'PY',
  ].join('\n');
  // Submitted outside this page: the real response start must reconcile its input.
  const turn = api.streamPrompt(sessionId,
    `Run this command verbatim with Bash, wait for it, then reply with ${marker}.\n\n${command}`,
    'bypassPermissions');
  void turn.catch(() => undefined);
  try {
    await expect.poll(async () => {
      try { return await api.downloadFileText(sessionId, `/workspace/${started}`); }
      catch (error) {
        if (/404|FILE_NOT_FOUND|path not found/.test(String(error))) return '';
        throw error;
      }
    }, { timeout: 90_000 }).toBe('started');
    allowCapture();
    await expect.poll(() => snapshot).not.toBeNull();
    const partial = snapshot!.messages.filter((message) => message.role === 'assistant');
    expect(partial).toHaveLength(1);
    expect(partial[0].metadata?.status, 'retained server snapshot precedes real completion').toBe('running');
    await api.uploadFileText(sessionId, '/workspace', releaseFile, 'release');
    await turn;
    const detail = await api.waitForSession(sessionId, (value) => value.last_response?.status === 'ended');
    expect(detail.last_response?.outcome).toBe('completed');
    const canonical = (await api.getMessages(sessionId)).messages;
    const response = canonical.find((message) => message.id === detail.last_response?.response_id)!;
    const row = page.locator(`[data-message-id="${response.id}"]`);
    await expect(row.getByTestId('assistant-text').last()).toContainText(marker);
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute('data-pulse', 'false');
    await expect(row.getByTestId('assistant-turn-process-trigger')).toBeVisible();
    await revealAssistantProcess(page, { within: row });
    expect(normalizeRendered((await row.getByTestId('assistant-text').allTextContents()).join('')))
      .toBe(normalizeRendered(messageText(response)));
    const tools = messageToolCalls(response);
    expect(tools.length).toBeGreaterThan(0);
    for (const tool of tools) {
      expect(tool.state).toBe('output-available');
      await expect(row.locator(`[data-tool-call-id="${tool.toolCallId}"]`)).toBeVisible();
      await expect(row.locator(`[data-tool-call-id="${tool.toolCallId}"]`).getByText(/^(Done|已完成)$/)).toBeVisible();
    }
    releaseRead();
    await expect.poll(() => delivered).toBe(true);
    // The merged history must include the user input missing from the initial page.
    await expect(page.locator(`[data-message-id="${canonical[0].id}"]`)).toHaveCount(1);
    await expect.poll(async () => normalizeRendered((await row.getByTestId('assistant-text').allTextContents()).join('')))
      .toBe(normalizeRendered(messageText(response)));
    for (const tool of tools) {
      await expect(row.locator(`[data-tool-call-id="${tool.toolCallId}"]`).getByText(/^(Done|已完成)$/)).toBeVisible();
    }
    expect(await row.locator('[data-tool-call-id]').evaluateAll(
      (cards) => cards.map((element) => element.getAttribute('data-tool-call-id')),
    )).toEqual(tools.map((tool) => tool.toolCallId));
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute('data-pulse', 'false');
    expect(await page.getByTestId('session-conversation').locator('[data-message-id]').evaluateAll(
      (rows) => rows.map((element) => element.getAttribute('data-message-id')),
    )).toEqual(canonical.map((message) => message.id));
  } finally {
    allowCapture();
    releaseRead();
    await test.info().attach('late-history-snapshot', {
      body: JSON.stringify({ sessionId, captured, delivered, snapshot, stream: await sessionStreamBodies(page) }),
      contentType: 'application/json',
    });
    await page.unroute(matchesHistory);
  }
});

test('a history prefix resumes one intact Markdown code block', async ({ page, request }) => {
  await mirrorSseBodies(page);
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const { session_id: sessionId } = await api.startConversation(agent.agent_id);
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  const lines = Array.from({ length: 400 }, (_, index) => String(index + 1).padStart(4, '0'));
  const turn = api.streamPrompt(sessionId,
    'Without tools or commentary, return one fenced text code block containing the numbers 0001 through 0400, one per line in order.');
  void turn.catch(() => undefined);
  let retained: APIResponse | null = null;
  let snapshot: HistoryBlockPage | null = null;
  try {
    await expect.poll(async () => {
      const response = await request.get(apiPath(`/sessions/${sessionId}/history-blocks?limit=50`));
      expect(response.ok()).toBe(true);
      const history = (await response.json()).data as HistoryBlockPage;
      const reply = history.messages.find((message) => message.role === 'assistant');
      if (!reply) return false;
      const text = messageText(reply);
      if (reply.metadata?.status === 'running' && text.includes('```') && text.split('```').length === 2) {
        retained = response;
        snapshot = history;
        return true;
      }
      if (reply.metadata?.status === 'ended') {
        throw new Error('Fault-observation precondition unavailable: no unfinished Markdown history prefix was captured');
      }
      return false;
    }, { timeout: 90_000, intervals: [50, 100, 250] }).toBe(true);
    await turn;
    const detail = await api.waitForSession(sessionId, (value) => value.last_response?.status === 'ended');
    expect(detail.last_response?.outcome).toBe('completed');
    const canonical = (await api.getMessages(sessionId)).messages.find(
      (message) => message.id === detail.last_response?.response_id,
    )!;
    expect(messageText(canonical).trim(), 'native answer must supply the requested Markdown scene')
      .toBe(`\`\`\`text\n${lines.join('\n')}\n\`\`\``);
    let supplied = false;
    await page.route((url) => url.pathname === apiPath(`/sessions/${sessionId}/history-blocks`), async (route) => {
      if (supplied) return route.continue();
      supplied = true;
      // The real partial response is delayed unchanged, never fabricated.
      await route.fulfill({ response: retained! });
    });
    await openSessionView(page, sessionId);
    expect(supplied, 'the browser must bootstrap from the retained partial history').toBe(true);
    const row = page.locator(`[data-message-id="${canonical.id}"]`);
    const code = row.locator('[data-testid="assistant-text"] pre code');
    await expect(code).toHaveCount(1);
    await expect(code).toHaveText(`${lines.join('\n')}\n`);
    await expect(row.getByTestId('assistant-text')).toHaveCount(1);
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute('data-pulse', 'false');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(code).toHaveCount(1);
    await expect(code).toHaveText(`${lines.join('\n')}\n`);
  } finally {
    await test.info().attach('Markdown-history-continuation', {
      body: JSON.stringify({ sessionId, snapshot, stream: await sessionStreamBodies(page) }),
      contentType: 'application/json',
    });
  }
});
