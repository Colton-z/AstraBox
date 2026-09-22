/**
 * E2E: terminal confirmation preserves the reading window and gates reopening,
 * even when the newest durable page is disjoint from the loaded history.
 *
 * All 51 turns, messages, frames and history responses here are real. Twenty-
 * six turns establish a 50-row reading window with an older page. Twenty-five
 * more turns displace that window while the first subscription is withheld.
 * Its real terminal then triggers a held, real history read. That read must
 * complete before the next subscription opens, without replacing the window
 * or skipping unread output by adopting the history response's newer cursor.
 *
 * The replacement stream is held while the retained window
 * and lack of automatic pagination are checked. Only delivery timing changes;
 * status, headers and response bytes are never fabricated.
 */
import { expect, test, type Page, type Route } from '@playwright/test';

import { AstraApi, type MessagePage } from '../fixtures/astraApi';
import { apiPath } from '../fixtures/env';
import { expectComposerEnabled, openSessionView } from '../fixtures/sessionPage';
import { trackSessions } from '../fixtures/sessionCleanup';

// The console's own history page size (useFirstPageMessages'
// AUTHORITATIVE_HISTORY_PAGE_SIZE), which is also what every history request
// this spec observes must carry.
const HISTORY_PAGE_SIZE = 50;
// 52 rows: a full 50-row window, plus an older page behind it. Without that
// older page `has_more` is false, `loadOlder` returns before it asks anything,
// and the "no automatic pagination" assertion would hold vacuously.
const WINDOW_TURNS = 26;
// 50 rows: enough to push every row of the reading window off the newest page.
const DISPLACING_TURNS = 25;
// How long the page is watched for a subscription it must not open. The gate
// before it is a receipt, not a clock: the convergence read is already held, so
// the page has finished the terminal and is blocked on the history read.
// This window only has to outlast the reopen path, which is a
// couple of microtasks once the rehydrate resolves.
const REOPEN_OBSERVATION_MS = 2_000;

/** One session output subscription the BROWSER opened, and its real bytes. */
interface StreamConnection {
  id: number;
  afterSeq: number | null;
  status: number;
  contentType: string;
  /** Every byte read from the backend on this response. */
  raw: string;
  /** The bytes handed on to the application. */
  delivered: string;
  held: boolean;
  upstreamClosed: boolean;
  closed: boolean;
}

type HoldWindow = Window & typeof globalThis & {
  __astraboxStreamHold: {
    connections: StreamConnection[];
    /** Start withholding, and report what had already been delivered. */
    hold: (id: number) => string;
    release: (id: number) => void;
    disarm: () => void;
  };
};

/**
 * Withhold delivery of one session output subscription without changing it.
 *
 * The page's transport calls `globalThis.fetch`, so wrapping `window.fetch`
 * before the app's first script puts this between the backend and the AI SDK
 * parser. The response object the app receives carries the backend's own
 * status, statusText and headers; the body re-emits the backend's own chunks.
 * A held connection keeps READING the backend — its terminal really arrives and
 * really closes the upstream — and only the handover waits.
 *
 * The reading happens INSIDE one `pull`. A `pull` that returns without
 * enqueueing is not called again: the machinery re-pulls after an enqueue (or a
 * BYOB read), so a withhold that returned per chunk would stop draining at its
 * first withheld chunk and never reach the terminal it exists to hold. See
 * https://developer.mozilla.org/en-US/docs/Web/API/ReadableStream/ReadableStream#pullcontroller
 * Unheld reads deliver one chunk per `pull`; release drains the buffered chunks
 * in their original order.
 *
 * `release` also clears the withhold, so a connection released while its
 * upstream is still open resumes with the buffer, in order, on the next chunk.
 */
async function installSessionStreamHold(page: Page, sessionId: string): Promise<void> {
  await page.addInitScript(({ streamPath }) => {
    const originalFetch = window.fetch.bind(window);
    const gates = new Map<number, () => void>();
    let armed = true;
    const state: HoldWindow['__astraboxStreamHold'] = {
      connections: [],
      hold: (id) => {
        const record = state.connections[id];
        if (!record) throw new Error(`no session output subscription ${id}`);
        if (record.closed) throw new Error(`subscription ${id} is already closed`);
        record.held = true;
        // Read inside the page, after the flag is set: a snapshot taken from
        // the harness could miss a keepalive delivered in between.
        return record.delivered;
      },
      release: (id) => {
        const record = state.connections[id];
        const open = gates.get(id);
        if (!record || !open) throw new Error(`subscription ${id} has no delivery gate`);
        record.held = false;
        open();
      },
      disarm: () => {
        armed = false;
        window.fetch = originalFetch;
        for (const record of state.connections) record.held = false;
        for (const open of gates.values()) open();
      },
    };
    (window as HoldWindow).__astraboxStreamHold = state;
    window.fetch = async (input, init) => {
      const request = input instanceof Request ? input : null;
      const method = String(init?.method ?? request?.method ?? 'GET').toUpperCase();
      const url = new URL(request?.url ?? String(input), window.location.href);
      const response = await originalFetch(input, init);
      if (
        !armed
        || method !== 'GET'
        || url.pathname !== streamPath
        || url.searchParams.get('follow') !== 'session'
      ) {
        return response;
      }
      const rawCursor = url.searchParams.get('after_seq');
      const record: StreamConnection = {
        id: state.connections.length,
        afterSeq: rawCursor === null ? null : Number(rawCursor),
        status: response.status,
        contentType: response.headers.get('content-type') || '',
        raw: '',
        delivered: '',
        // The source holds the replacement stream during its window checks.
        held: state.connections.length > 0,
        upstreamClosed: false,
        closed: false,
      };
      state.connections.push(record);
      if (response.status !== 200 || !record.contentType.includes('text/event-stream') || !response.body) {
        record.upstreamClosed = true;
        record.closed = true;
        return response;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const withheld: Array<{ value: Uint8Array; text: string }> = [];
      let openGate!: () => void;
      const gate = new Promise<void>((resolve) => { openGate = resolve; });
      gates.set(record.id, openGate);
      const deliver = (controller: ReadableStreamDefaultController<Uint8Array>) => {
        for (const pending of withheld.splice(0, withheld.length)) {
          record.delivered += pending.text;
          controller.enqueue(pending.value);
        }
      };
      const body = new ReadableStream<Uint8Array>({
        async pull(controller) {
          try {
            for (;;) {
              const chunk = await reader.read();
              if (chunk.done) {
                record.upstreamClosed = true;
                if (record.held) await gate;
                deliver(controller);
                record.closed = true;
                controller.close();
                return;
              }
              const text = decoder.decode(chunk.value, { stream: true });
              record.raw += text;
              withheld.push({ value: chunk.value, text });
              if (record.held) continue;
              deliver(controller);
              return;
            }
          } catch (error) {
            record.closed = true;
            controller.error(error);
          }
        },
        async cancel(reason) {
          record.closed = true;
          openGate();
          await reader.cancel(reason);
        },
      });
      return new Response(body, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
    };
  }, { streamPath: apiPath(`/sessions/${sessionId}/ai-stream`) });
}

async function streamConnections(page: Page): Promise<StreamConnection[]> {
  return page.evaluate(() => (window as HoldWindow).__astraboxStreamHold.connections);
}

/**
 * Complete `data:` frames in an SSE body, with the unterminated tail kept apart.
 *
 * A body is only split on newlines that actually arrived: whatever follows the
 * last one is an incomplete frame and is returned as such rather than parsed.
 * A complete frame that is not JSON throws — a malformed frame is evidence, and
 * swallowing it would report "no terminal" for a body that had one.
 */
function scanSse(raw: string): {
  frames: Array<Record<string, unknown>>;
  done: boolean;
  incompleteTail: string;
} {
  const boundary = raw.lastIndexOf('\n');
  const complete = boundary < 0 ? '' : raw.slice(0, boundary + 1);
  const incompleteTail = raw.slice(boundary + 1);
  const frames: Array<Record<string, unknown>> = [];
  let done = false;
  for (const line of complete.split('\n')) {
    const field = line.endsWith('\r') ? line.slice(0, -1) : line;
    if (!field.startsWith('data:')) continue;
    const payload = field.slice(5).trim();
    if (!payload) continue;
    if (payload === '[DONE]') {
      done = true;
      continue;
    }
    frames.push(JSON.parse(payload) as Record<string, unknown>);
  }
  return { frames, done, incompleteTail };
}


/** Whether a new output subscription appears within a bounded window. */
async function observeReopen(page: Page, expected: number, windowMs: number): Promise<boolean> {
  const deadline = Date.now() + windowMs;
  while (Date.now() < deadline) {
    if ((await streamConnections(page)).length > expected) return true;
    await page.waitForTimeout(100);
  }
  return false;
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

// Keep the retained failure's test identity during repair. Terminal confirmation
// preserves the live window; the historical title does not require a page union.
test('terminal convergence keeps the reading window and holds the next stream until a disjoint page merges', async ({
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
    `Convergence note ${runId}-${String(index + 1).padStart(3, '0')}. `
    + 'Acknowledge briefly in one word; do not use tools.'
  );
  const windowPrompts = Array.from({ length: WINDOW_TURNS }, (_, index) => note(index));
  const displacingPrompts = Array.from(
    { length: DISPLACING_TURNS },
    (_, index) => note(WINDOW_TURNS + index),
  );
  const newestWindowPrompt = windowPrompts[windowPrompts.length - 1];

  // Real normal inputs, no imported rows and no substituted responses. Each
  // turn's own SSE completion is the boundary for the next one; nothing here
  // waits on a clock for model work.
  const writeTurns = async (label: string, prompts: string[]) => {
    await test.step(label, async () => {
      for (const [index, content] of prompts.entries()) {
        const result = await api.sendTurn(sessionId, content);
        expect(result.errorText, `${label}: input ${index + 1} must complete without an engine error`).toBeNull();
        expect(result.text.trim(), `${label}: input ${index + 1} must receive a real reply`).not.toBe('');
      }
    });
  };

  await writeTurns(`write ${WINDOW_TURNS} real turns behind the reading window`, windowPrompts);
  await api.waitForSession(
    sessionId,
    (session) => session.state === 'READY' && session.last_turn_status === 'COMPLETED',
  );

  // ── The window the reader will be holding. ──────────────────────────────
  const readingWindow = await api.getMessages(sessionId);
  expect(readingWindow.messages, 'the reader must open on a full history page').toHaveLength(HISTORY_PAGE_SIZE);
  expect(
    readingWindow.has_more,
    'an older page must exist behind the window, or "no automatic pagination" asserts nothing',
  ).toBe(true);
  const readingWindowIds = readingWindow.messages.map((message) => message.message_id);
  expect(new Set(readingWindowIds).size).toBe(HISTORY_PAGE_SIZE);

  // ── Instrument, then open the conversation. ─────────────────────────────
  const historyPath = apiPath(`/sessions/${sessionId}/history-blocks`);
  const historyRequests: URL[] = [];
  page.on('request', (sent) => {
    const url = new URL(sent.url());
    if (sent.method() === 'GET' && url.pathname === historyPath) historyRequests.push(url);
  });
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => { pageErrors.push(error.message); });
  const consoleErrors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });

  // Filled as the case proceeds and attached from the outer `finally`, so the
  // first red carries the two windows, the request trace and the subscription
  // state that name its cause. A report only a pass produces is a report for
  // the run that did not need one.
  const report: Record<string, unknown> = { sessionId, readingWindowIds };

  await installSessionStreamHold(page, sessionId);
  const isHistoryRead = (url: URL) => url.pathname === historyPath;
  const firstHistoryResponse = page.waitForResponse((response) => (
    response.request().method() === 'GET' && isHistoryRead(new URL(response.url()))
  ), { timeout: 60_000 });
  try {
    await openSessionView(page, sessionId);
    await expectComposerEnabled(page);

    const openingWindow = (await (await firstHistoryResponse).json()) as { data: MessagePage };
    report.pageWindowIds = openingWindow.data.messages.map((message) => message.message_id);
    expect(
      openingWindow.data.messages.map((message) => message.message_id),
      'the page must open on exactly the durable window the platform serves',
    ).toEqual(readingWindowIds);
    const retainedTailRecord = openingWindow.data.messages.find((message) => (
      message.role === 'user' && String(message.content) === newestWindowPrompt
    ));
    expect(retainedTailRecord, 'the page\'s own window must contain the newest input behind it').toBeTruthy();
    const renderedTail = page.getByTestId('user-message').filter({ hasText: newestWindowPrompt });
    await expect(renderedTail).toBeVisible();

    // ── Arm: hold the one subscription the console owns. ──────────────────
    await expect.poll(async () => (await streamConnections(page)).filter((c) => !c.closed).length, {
      message: 'the console must own exactly one open session output subscription',
      timeout: 60_000,
    }).toBe(1);
    const beforeHold = await streamConnections(page);
    const open = beforeHold.filter((connection) => !connection.closed);
    expect(open, 'exactly one subscription may be open when the window is displaced').toHaveLength(1);
    const armed = open[0];
    const subscriptionsBefore = beforeHold.length;
    const deliveredBeforeHold = await page.evaluate(
      (id) => (window as HoldWindow).__astraboxStreamHold.hold(id),
      armed.id,
    );
    expect(
      (await streamConnections(page))[armed.id].upstreamClosed,
      'the armed subscription must still be following when the displacing turns start',
    ).toBe(false);

    const historyReadsBefore = historyRequests.length;
    report.armedSubscriptionId = armed.id;

    // ── Displace the whole reading window with real turns. ────────────────
    await writeTurns(
      `write ${DISPLACING_TURNS} real turns while the subscription is withheld`,
      displacingPrompts,
    );
    await api.waitForSession(
      sessionId,
      (session) => session.state === 'READY' && session.last_turn_status === 'COMPLETED',
    );

    // The backend closed the held response at a turn terminal; the page has
    // not seen it. This receipt, not a clock, is what orders everything below.
    await expect.poll(async () => (await streamConnections(page))[armed.id].upstreamClosed, {
      message: 'the withheld subscription must receive its real terminal from the backend',
      timeout: 60_000,
    }).toBe(true);

    const withheld = await streamConnections(page);
    expect(withheld, 'a withheld terminal must not make the page open another subscription').toHaveLength(subscriptionsBefore);
    expect(
      withheld[armed.id].delivered,
      'nothing of the withheld response may reach the page yet',
    ).toBe(deliveredBeforeHold);
    expect(historyRequests.length, 'a withheld terminal must not trigger a history read').toBe(historyReadsBefore);
    await expect(renderedTail).toBeVisible();

    const terminal = scanSse(withheld[armed.id].raw);
    report.withheldFrameTypes = terminal.frames.map((frame) => String(frame.type ?? ''));
    report.withheldDone = terminal.done;
    report.withheldIncompleteTail = terminal.incompleteTail;
    expect(terminal.incompleteTail, 'a closed response must end on a complete frame boundary').toBe('');
    const frameTypes = terminal.frames.map((frame) => String(frame.type ?? ''));
    expect(
      frameTypes,
      'the withheld response must carry the durable result the convergence read follows',
    ).toContain('data-result');
    expect(frameTypes, 'the withheld response must end on a turn terminal').toContain('finish');
    expect(frameTypes, 'an errored turn never reaches the clean terminal this case is about').not.toContain('error');
    expect(terminal.done, 'the backend closes an SSE response with its DONE marker').toBe(true);

    // ── The platform's own proof that the two windows are disjoint. ───────
    const displacedWindow = await api.getMessages(sessionId);
    expect(displacedWindow.messages).toHaveLength(HISTORY_PAGE_SIZE);
    const displacedIds = displacedWindow.messages.map((message) => message.message_id);
    report.displacedIds = displacedIds;
    expect(
      displacedIds.filter((id) => readingWindowIds.includes(id)),
      'the case needs two windows the platform itself reports as sharing no message',
    ).toEqual([]);

    // ── Hold the convergence read, then let the terminal through. ─────────
    let releaseHistory!: () => void;
    const historyReleased = new Promise<void>((resolve) => { releaseHistory = resolve; });
    let convergenceReady!: () => void;
    let convergenceFailed!: (reason: unknown) => void;
    const convergenceHeld = new Promise<void>((resolve, reject) => {
      convergenceReady = resolve;
      convergenceFailed = reject;
    });
    let claimed = false;
    let convergenceBody = '';
    const convergenceRoute = async (route: Route) => {
      if (claimed || route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }
      claimed = true;
      try {
        const response = await route.fetch();
        expect(response.status(), 'the convergence read must be a real successful backend response').toBe(200);
        convergenceBody = await response.text();
        convergenceReady();
        // Hold before the headers reach the page's fetch, and release exactly
        // this response afterwards. No successful data is synthesized.
        await historyReleased;
        await route.fulfill({ response });
      } catch (error) {
        convergenceFailed(error);
        throw error;
      }
    };
    await page.route(isHistoryRead, convergenceRoute);
    try {
      await page.evaluate((id) => (window as HoldWindow).__astraboxStreamHold.release(id), armed.id);
      await convergenceHeld;

      // The page read exactly the bytes the backend sent, and read all of them.
      const afterRelease = await streamConnections(page);
      expect(
        afterRelease[armed.id].delivered,
        'the page must consume exactly the withheld real response bytes',
      ).toBe(afterRelease[armed.id].raw);
      expect(afterRelease[armed.id].closed).toBe(true);
      expect(historyRequests.length, 'the terminal must produce exactly one convergence read').toBe(historyReadsBefore + 1);

      const envelope = JSON.parse(convergenceBody) as { code: string; data: MessagePage };
      expect(envelope.code).toBe('OK');
      const convergenceIds = envelope.data.messages.map((message) => message.message_id);
      report.convergenceIds = convergenceIds;
      expect(convergenceIds, 'the held convergence read must be the displaced window').toEqual(displacedIds);
      expect(
        convergenceIds.filter((id) => readingWindowIds.includes(id)),
        'the response released below must share no message with the rendered window',
      ).toEqual([]);

      // Donor property 2: the tail the reader already has stays on screen while
      // convergence is unfinished, and no replacement subscription starts.
      await expect(
        renderedTail,
        'the already rendered tail must stay mounted while convergence reads a disjoint tail',
      ).toBeVisible();
      expect(
        await observeReopen(page, subscriptionsBefore, REOPEN_OBSERVATION_MS),
        'the next output subscription must not overlap terminal durable-history convergence',
      ).toBe(false);
      expect((await streamConnections(page)).length).toBe(subscriptionsBefore);

      // Donor property 3: confirmation completes before the replacement
      // subscription opens. Its real response is held, as in the source, so
      // another turn cannot change the window during the retention checks.
      releaseHistory();
      await expect.poll(async () => (await streamConnections(page)).length, {
        message: 'the replacement output subscription must open once convergence completes',
        timeout: 30_000,
      }).toBe(subscriptionsBefore + 1);
      const replacement = (await streamConnections(page)).at(-1)!;
      expect(replacement.status).toBe(200);
      expect(replacement.held, 'hold the next real stream while checking the retained window').toBe(true);
      expect(replacement.delivered, 'the replacement stream must not alter the reading window yet').toBe('');
      const receivedCursor = terminal.frames
        .filter((frame) => frame.type === 'data-resume-cursor')
        .map((frame) => Number((frame.data as { frameSeq?: unknown }).frameSeq))
        .at(-1);
      expect(Number.isInteger(receivedCursor), 'the terminal must carry its durable stream cursor').toBe(true);
      expect(
        replacement.afterSeq,
        'confirmation must reopen from received output, not skip ahead to the newest history page',
      ).toBe(receivedCursor);
      report.replacementCursor = replacement.afterSeq;

      await expect(
        renderedTail,
        'the original reading-window tail must remain visible after terminal confirmation',
      ).toBeVisible();
      expect(
        await renderedTail.evaluate((node) => (
          node.closest<HTMLElement>('[data-message-id]')?.dataset.messageId ?? ''
        )),
        'the retained tail must keep the exact durable identity from the opening page',
      ).toBe(retainedTailRecord!.message_id);
      await expect(renderedTail).toContainText(String(retainedTailRecord!.content));
      await expect(page.getByText(/SESSION_HISTORY_WINDOWS_DO_NOT_OVERLAP/)).toHaveCount(0);
      expect(pageErrors.filter((message) => message.includes('SESSION_HISTORY_WINDOWS_DO_NOT_OVERLAP'))).toEqual([]);
      expect(
        consoleErrors.filter((message) => message.includes('SESSION_HISTORY_WINDOWS_DO_NOT_OVERLAP')),
        'terminal confirmation must not fail in the history merge path',
      ).toEqual([]);

      // Donor property 4: no older page without user upward intent.
      expect(
        historyRequests.filter((url) => url.searchParams.get('before') !== null).map((url) => url.search),
        'confirmation must not paginate older history without user input',
      ).toEqual([]);
      expect(
        historyRequests.every((url) => url.searchParams.get('limit') === String(HISTORY_PAGE_SIZE)),
        'every history read here is the console\'s own window page',
      ).toBe(true);
      expect(historyRequests.length, 'the terminal produced exactly one history read').toBe(historyReadsBefore + 1);
    } finally {
      releaseHistory();
      await page.unroute(isHistoryRead, convergenceRoute);
    }
  } finally {
    // Read the subscriptions before disarming, and say why they are missing
    // rather than reporting an empty list: "we could not ask" and "there were
    // none" send a reader to different places.
    report.subscriptions = page.isClosed()
      ? 'page closed before the report was taken'
      : await page.evaluate(() => (
        (window as HoldWindow).__astraboxStreamHold?.connections.map((connection) => ({
          id: connection.id,
          afterSeq: connection.afterSeq,
          status: connection.status,
          contentType: connection.contentType,
          rawBytes: connection.raw.length,
          deliveredBytes: connection.delivered.length,
          held: connection.held,
          upstreamClosed: connection.upstreamClosed,
          closed: connection.closed,
        })) ?? 'stream hold was never installed'
      ));
    report.historyRequests = historyRequests.map((url) => url.search);
    report.consoleErrors = consoleErrors;
    report.pageErrors = pageErrors;
    await test.info().attach('session-stream-convergence', {
      body: JSON.stringify(report, null, 2),
      contentType: 'application/json',
    });
    if (!page.isClosed()) {
      await page.evaluate(() => (window as HoldWindow).__astraboxStreamHold?.disarm());
    }
  }
});
