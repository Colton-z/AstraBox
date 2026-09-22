/**
 * E2E: the page admits that it lost its stream, while the turn keeps running.
 *
 * The journey is a route that goes away mid-answer. The user sends a message,
 * the reply starts arriving, and then the connection carrying it breaks — a
 * dropped VPN, a proxy recycled, a captive portal. The platform is untouched:
 * the turn is still running in the sandbox and the reply is still owed. What
 * the user sees is a "Processing" pill, a live run dot, and nothing else, for
 * five minutes — `useSessionChat.ts:765` arms the only notice at
 * `5 * 60 * 1000`, and `:1486-1489` (`isTransientSilent`) actively suppresses
 * the banner that the stream error would otherwise raise at once. Behind that
 * silence `turnStream.ts:42-44` retries up to 100 times at a 5s cap — about
 * eight minutes of invisible work. Nothing keeps the page's view of its own
 * connection fresh, so the staleness is paid by the person watching it: they
 * conclude the product is broken and reload.
 *
 * The claim under test is not "reconnect faster" and not "stop the turn". It is
 * that a page which has lost its only live channel says so within a window a
 * person will wait, and withdraws the notice once the route returns. The budget
 * is the product decision this spec writes down; raising it toward 300s to make
 * the lane green would be weakening the assertion, not tuning it.
 *
 * WHY the outage is stream-only: killing everything also fails the session
 * detail poll, which sets `lifecycleError` (`useSessionLifecycle.ts:167`) and
 * raises a DIFFERENT banner within seconds through `shouldShowSessionErrorBanner`.
 * A spec that watches that banner proves nothing about the silent path, so the
 * session GET, the messages GET and every POST are deliberately left alone —
 * and the spec asserts they stayed healthy.
 *
 * WHY both a route abort and an in-page body cut: `page.route` cannot kill an
 * SSE response that is already established (it was matched and released long
 * ago), and a route added later only ever sees the NEXT attempt. The abort
 * makes every reconnect fail; the body cut breaks the connection the page is
 * reading right now. Neither fabricates success — only failure.
 *
 * WHY the backend barrier: `hold_after_frame` on `text-delta` parks the real
 * turn worker right after the first delta, so "the reply is still owed" is true
 * for a bounded window instead of a race against model speed. Its release
 * timeout is 120s (`astrabox/testing/e2e_faults.py:44`), which is the ceiling
 * on the whole outage.
 *
 * Engine-independent: `text-delta` is the normalized v5 frame type every
 * engine's translated frame carries, the prompt asks for tool-free prose, and
 * no assertion reads engine-specific text. It DOES require the E2E deployment
 * stack (`ASTRABOX_E2E_FAULTS=1` and the shared fault-file path), like the
 * three existing fault-file specs; absent that the barrier is never consumed
 * and the spec fails loudly rather than skipping.
 *
 * NOT COVERED: the flapping stream. `onData` calls `resetTransientState`
 * (`useSessionChat.ts:525`), so a connection that delivers one frame and drops
 * again re-arms a fresh five-minute clock with no ceiling. This spec keeps the
 * route down for the whole window and therefore proves only the sustained
 * outage; the unbounded case needs a spec of its own.
 */
import { expect, test, type Page, type Route } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, parseTimeoutEnv } from '../fixtures/env';
import {
  armFrameHoldFault,
  clearFrameHoldFault,
  frameHoldConsumed,
  frameHoldFaultPath,
  releaseFrameHoldFault,
} from '../fixtures/frameHold';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import {
  expectComposerEnabled,
  expectPromptDelivered,
  openSessionView,
  startPromptDelivery,
} from '../fixtures/sessionPage';

/**
 * How long the page may stay silent about a connection it has lost.
 *
 * THE PRODUCT DECISION. Twenty seconds is roughly the span in which a person
 * decides whether a page is working; the shipped behaviour is 300s. The
 * fallback is the contract, and the env knob exists so a lane can tighten it —
 * loosening it toward 300s makes this spec pass by agreeing with the defect.
 */
const DEGRADED_NOTICE_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_STREAM_DEGRADED_NOTICE_MS', 20_000);
// Upper bound on the real turn reaching its first text delta at the barrier.
const FIRST_DELTA_MS = parseTimeoutEnv('ASTRABOX_E2E_STREAM_DEGRADED_FIRST_DELTA_MS', 60_000);
// How long the page may take to make its retries visible as failed attempts.
const RETRY_OBSERVED_MS = parseTimeoutEnv('ASTRABOX_E2E_STREAM_DEGRADED_RETRY_MS', 45_000);
// How long the restored route may take to carry a successful subscription again.
const RESTORE_MS = parseTimeoutEnv('ASTRABOX_E2E_STREAM_DEGRADED_RESTORE_MS', 60_000);
// How long the released turn may take to finish and settle durably.
const SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_STREAM_DEGRADED_SETTLE_MS', 90_000);

/**
 * How many reconnects must have failed before the page is judged.
 *
 * This is the anti-vacuity guard for the assertion: without it a page that
 * simply never reconnected — never noticed anything to reconnect from — would
 * satisfy "the stream was down" for the wrong reason. Two is what the page
 * shows inside the window: the reconnect right after the cut, and one more
 * about thirty seconds later; the resume backoff (`turnStream.ts:44`, capped
 * at 5s) is not the cadence the page actually retries on after a failed
 * reopen, which is a question for the product, not for this guard.
 */
const MIN_ABORTED_ATTEMPTS = 2;

// One rendered character proves the first delta reached the browser. The
// barrier, not an amount of prose, is what proves the turn is live.
const LIVE_PREFIX_MIN_CHARS = 1;
// A visible suffix after release, measured from rendered text parts only.
const RESUMED_GROWTH_MIN_CHARS = 40;
// Long enough that prose cannot repeat it by accident, short enough to catch a
// replayed prefix. Compared over normalized text (see normalizeRendered).
const DUPLICATE_WINDOW_CHARS = 48;

/**
 * The acknowledgement, in the product's own words (`chat:banner.backend_unavailable`).
 *
 * Pinned to English by `test.use` plus the persisted `astrabox-lang` below: a
 * spec that accepted two spellings would accept a third locale nobody wrote it
 * against, and this string IS the assertion.
 */
const DEGRADED_NOTICE = 'The backend is temporarily unavailable. Please try again shortly.';

// A FAILED turn renders its error INTO the transcript as an assistant message,
// so "a reply bubble exists" is satisfied by exactly the failure under test.
const TURN_ERROR = /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

test.use({ locale: 'en-US' });

/** One session output subscription the BROWSER opened, as the page saw it. */
interface StreamConnection {
  id: number;
  afterSeq: number | null;
  status: number;
  contentType: string;
  /** Still readable by the application. */
  open: boolean;
  /** This spec broke it, as opposed to the backend ending it. */
  cut: boolean;
}

type CutWindow = Window & typeof globalThis & {
  __astraboxStreamCut: {
    connections: StreamConnection[];
    cut: () => Promise<number>;
    disarm: () => void;
  };
};

/**
 * Give the spec a way to break the connection the page is reading RIGHT NOW.
 *
 * The page's transport calls `globalThis.fetch` (`useSessionChat.ts:393-395`),
 * so wrapping `window.fetch` before the app's first script puts this between
 * the backend and the AI SDK parser. Status, headers and every byte are the
 * backend's own; the only thing this adds is `cut()`, which errors the open
 * body with the same `TypeError('network error')` a dropped route produces and
 * cancels the upstream reader. It fabricates no success.
 */
async function installStreamCut(page: Page, sessionId: string): Promise<void> {
  await page.addInitScript(({ streamPath }) => {
    const originalFetch = window.fetch.bind(window);
    const cuts = new Map<number, () => Promise<void>>();
    let armed = true;
    const state: CutWindow['__astraboxStreamCut'] = {
      connections: [],
      cut: async () => {
        const open = state.connections.filter((connection) => connection.open);
        if (open.length === 0) {
          throw new Error('no open session output subscription to cut');
        }
        // The newest one is the channel the page is reading; an older open
        // record would mean the page holds two, which is its own defect.
        const target = open[open.length - 1];
        const fault = cuts.get(target.id);
        if (!fault) throw new Error(`no live response body for connection ${target.id}`);
        await fault();
        return target.id;
      },
      disarm: () => {
        armed = false;
        window.fetch = originalFetch;
        cuts.clear();
      },
    };
    (window as CutWindow).__astraboxStreamCut = state;
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
        open: false,
        cut: false,
      };
      state.connections.push(record);
      if (
        response.status !== 200
        || !record.contentType.includes('text/event-stream')
        || !response.body
      ) {
        return response;
      }
      record.open = true;
      const reader = response.body.getReader();
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          cuts.set(record.id, async () => {
            if (!record.open) throw new Error(`connection ${record.id} is not open`);
            record.open = false;
            record.cut = true;
            cuts.delete(record.id);
            controller.error(new TypeError('network error'));
            await reader.cancel('E2E route loss');
          });
        },
        async pull(controller) {
          try {
            const chunk = await reader.read();
            // The cut already errored this controller; enqueueing now throws.
            if (!record.open) return;
            if (chunk.done) {
              record.open = false;
              cuts.delete(record.id);
              controller.close();
              return;
            }
            controller.enqueue(chunk.value);
          } catch (error) {
            if (!record.open) return;
            record.open = false;
            cuts.delete(record.id);
            controller.error(error);
          }
        },
        async cancel(reason) {
          record.open = false;
          cuts.delete(record.id);
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
  return page.evaluate(() => (window as CutWindow).__astraboxStreamCut.connections);
}

/** Rendered reply parts only; reasoning and progress labels are not the answer. */
async function assistantTranscript(page: Page): Promise<string> {
  return (await page.getByTestId('assistant-text').allInnerTexts()).join('\n');
}

/**
 * Rendered prose with layout and markdown decoration removed.
 *
 * The same text is not the same string across the stream/settled boundary: a
 * half-arrived `**` is literal mid-stream and gone once the emphasis closes,
 * and line wrapping moves with the panel. Normalizing keeps the comparison
 * about content.
 */
function normalizeRendered(text: string): string {
  return text.replace(/\s+/g, '').replace(/[*_`#>|~\-–—·•]/g, '');
}

/**
 * The first stretch of *text* that appears twice, with both neighbourhoods.
 *
 * A reconnect that repaints the prefix it already showed and then tails the
 * same frames again puts the same sentences on screen twice. A window this long
 * cannot recur in prose by accident, so a hit is a repaint defect and not a
 * wordy model — and the surroundings are reported because the window alone
 * sends the reader on a scavenger hunt.
 */
function firstRepeatedWindow(text: string, size: number): string | null {
  if (text.length < size * 2) return null;
  const seen = new Map<string, number>();
  for (let i = 0; i + size <= text.length; i += 1) {
    const chunk = text.slice(i, i + size);
    const first = seen.get(chunk);
    if (first !== undefined) {
      return [
        `window=${JSON.stringify(chunk)}`,
        `first@${first}: ${JSON.stringify(text.slice(Math.max(0, first - 120), first + size + 120))}`,
        `second@${i}: ${JSON.stringify(text.slice(Math.max(0, i - 120), i + size + 120))}`,
      ].join('\n');
    }
    seen.set(chunk, i);
  }
  return null;
}

// Teardown that changes state runs on the passing path only, and afterEach
// hooks run in registration order — so the interrupt that settles a still-held
// turn goes BEFORE the tracker's delete, which would otherwise answer
// SESSION_BUSY and leave the orphan this ordering exists to avoid.
let heldSessionId = '';
onPassOnly(async ({ request }) => {
  if (heldSessionId) await new AstraApi(request).interruptSession(heldSessionId);
});
const sessions = trackSessions();

test('a stream lost mid-reply is admitted on the page while the turn keeps running, and the admission clears when the route returns', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();

  // Setup is API-only. The seeded Agent claims a prepared slot, so the 180s
  // wall is spent on the outage rather than on a cold sandbox build.
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  heldSessionId = sessionId;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);

  // Exact pathnames, never a glob: `/sessions/{id}` is a prefix of
  // `/sessions/{id}/messages` and of the stream itself, and a matcher that
  // caught those would break the half of the API this outage must leave alone.
  const aiStreamPath = apiPath(`/sessions/${sessionId}/ai-stream`);
  const sessionDetailPath = apiPath(`/sessions/${sessionId}`);
  const isAiStream = (url: URL) => url.pathname === aiStreamPath;

  // Every ai-stream attempt this spec refused, counted on the Node side where
  // the refusal actually happens.
  let abortedStreamAttempts = 0;
  const abortAiStream = async (route: Route) => {
    abortedStreamAttempts += 1;
    await route.abort('connectionreset');
  };

  // The page's own session-detail reads. These must keep succeeding: if they
  // fail, a different banner rises and this spec would be watching the wrong
  // one. `page.on` observes without touching the response.
  const detailResponses: { at: number; status: number }[] = [];
  page.on('response', (response) => {
    if (response.request().method() !== 'GET') return;
    if (new URL(response.url()).pathname !== sessionDetailPath) return;
    detailResponses.push({ at: Date.now(), status: response.status() });
  });

  // Which of the two silences this is, if the assertion goes red: the timer
  // armed and the page waited five minutes, or `onError` never fired and the
  // page will never say anything at all. Identical failure text, different
  // defect — so the console is captured and reported either way.
  const streamErrorLogs: string[] = [];
  page.on('console', (message) => {
    const text = message.text();
    if (text.includes('[useSessionChat] stream error:')
      || text.includes('[useSessionChat] session stream failed:')) {
      streamErrorLogs.push(`${message.type()}: ${text.slice(0, 300)}`);
    }
  });

  // Pin the console language before the FIRST navigation; the acknowledgement
  // asserted below is a localized string (see test.use above).
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });
  // Installed BEFORE the first navigation, so the console's first channel open
  // is wrapped too.
  await installStreamCut(page, sessionId);

  const frameHoldPath = frameHoldFaultPath(sessionId);
  armFrameHoldFault(frameHoldPath, sessionId, 'text-delta');
  test.info().annotations.push({ type: 'e2e_turn_frame_hold_file', description: frameHoldPath });

  const statusPill = page.getByTestId('run-view').getByTestId('status-pill').first();
  const degradedNotice = page
    .getByTestId('run-view')
    .getByText(DEGRADED_NOTICE, { exact: true });

  try {
    await openSessionView(page, sessionId);
    await expectComposerEnabled(page);

    // ── The user sends one long turn from the composer. ──────────────────────
    const prompt = [
      `E2E lost route ${runId}. Answer directly and do not use any tool.`,
      'Write four to six paragraphs of plain prose explaining, for a beginner,',
      'what a heartbeat is in a networked application, why a connection can look',
      'healthy while delivering nothing, and how a reader can tell the two apart.',
      'Output only the prose.',
    ].join('\n');
    await expectPromptDelivered(await startPromptDelivery(page, sessionId, prompt));
    await expect(
      page.getByTestId('user-message').last(),
      'the user bubble must render — proof the turn was dispatched from the page',
    ).toContainText(String(runId), { timeout: 30_000 });

    // ── Prove the stream was live and delivering before it is cut. ───────────
    // Anti-vacuity first: the backend must have consumed THIS session's own
    // declaration. Without it every assertion below could pass on an ordinary
    // settled turn — or on a deployment where the fault hooks are not armed.
    await expect
      .poll(() => frameHoldConsumed(frameHoldPath, sessionId, 'text-delta'), {
        timeout: FIRST_DELTA_MS,
        message:
          'the real turn worker must consume this session\'s text-delta frame hold. '
          + 'If it never does, the deployment is not running with ASTRABOX_E2E_FAULTS=1 '
          + `or ${frameHoldPath} is not the path the backend reads.`,
      })
      .toBe(true);
    await expect
      .poll(async () => (await assistantTranscript(page)).trim().length, {
        timeout: FIRST_DELTA_MS,
        message: 'the held text delta must render before the connection is cut',
      })
      .toBeGreaterThanOrEqual(LIVE_PREFIX_MIN_CHARS);

    const preCutText = (await assistantTranscript(page)).trim();
    const preCutRendered = normalizeRendered(preCutText);
    await expect(
      statusPill,
      'the header must read PROCESSING before the cut — the turn is live',
    ).toHaveAttribute('data-state', 'PROCESSING');
    const detailsBeforeCut = detailResponses.length;

    // ── THE CUT: the route to the stream disappears. ─────────────────────────
    // Order matters. The abort is armed first so the reconnect the page makes
    // in response to the cut is refused too; the cut then breaks the connection
    // already established, which no route could reach. Everything else — the
    // session GET, the messages GET, every POST — is left alone on purpose.
    await page.route(isAiStream, abortAiStream);
    const cutConnectionId = await page.evaluate(
      () => (window as CutWindow).__astraboxStreamCut.cut(),
    );
    const cutAt = Date.now();
    test.info().annotations.push({
      type: 'e2e_cut_connection',
      description: String(cutConnectionId),
    });

    // The page really lost it and really tried to come back. A page that never
    // reconnected would satisfy "the stream is down" for the wrong reason.
    await expect
      .poll(() => abortedStreamAttempts, {
        timeout: RETRY_OBSERVED_MS,
        message:
          `the page must retry its lost subscription at least ${MIN_ABORTED_ATTEMPTS} times `
          + '— without that, the silence below is not a silence about anything',
      })
      .toBeGreaterThanOrEqual(MIN_ABORTED_ATTEMPTS);
    const cutConnection = (await streamConnections(page))[cutConnectionId];
    expect(cutConnection.cut, 'the cut connection must be the one this spec broke').toBe(true);
    expect(cutConnection.open, 'the cut connection must be unreadable after the cut').toBe(false);

    // And the outage is stream-only: the rest of the API kept answering, so the
    // banner asserted below can only be the transient one.
    const detailsAfterCut = detailResponses.slice(detailsBeforeCut);
    expect(
      detailsAfterCut.length,
      'the page must keep reading session detail through the outage',
    ).toBeGreaterThan(0);
    expect(
      detailsAfterCut.filter((entry) => entry.status !== 200),
      'the session detail route must stay healthy — a failing one raises a different banner',
    ).toEqual([]);

    // ── THE ASSERTION ────────────────────────────────────────────────────────
    // The page has no live channel, has failed to get one back several times,
    // and the reply it is showing is not finished. It must say so.
    const remainingMs = Math.max(1_000, DEGRADED_NOTICE_BUDGET_MS - (Date.now() - cutAt));
    await expect(
      degradedNotice,
      `the page must acknowledge the lost connection within ${DEGRADED_NOTICE_BUDGET_MS}ms of `
        + 'losing it. The shipped page arms its only notice at 5 * 60 * 1000 '
        + '(useSessionChat.ts:765) and the banner that would otherwise fire is '
        + 'suppressed by isTransientSilent (useSessionChat.ts:1486-1489), so the '
        + 'user watches a Processing pill for five minutes and reloads.',
    ).toBeVisible({ timeout: remainingMs });

    // ── The anti-cheat companion, read at the same moment. ───────────────────
    // The acknowledgement must be about the CONNECTION. A fix that quiets the
    // page by pretending the turn ended would pass the line above and lose the
    // user's answer, so the turn is required to still be running right here.
    const pillAtAdmission = await statusPill.getAttribute('data-state');
    const pulseAtAdmission = await statusPill.getAttribute('data-pulse');
    const detailAtAdmission = await api.getSession(sessionId);
    expect(
      pillAtAdmission,
      'the header must still read PROCESSING — the admission is about the connection',
    ).toBe('PROCESSING');
    expect(pulseAtAdmission, 'the header run dot must still be live').toBe('true');
    expect(
      String(detailAtAdmission.current_turn_id || '').trim(),
      'the platform must still report the turn active — nothing was cancelled',
    ).not.toEqual('');

    // ── RESTORE: the route comes back. ───────────────────────────────────────
    // A product that admits an outage must also take the admission back, or the
    // next thing it teaches the user is to ignore it.
    const restored = page.waitForResponse(
      (response) => response.request().method() === 'GET'
        && isAiStream(new URL(response.url()))
        && response.status() === 200,
      { timeout: RESTORE_MS },
    );
    await page.unroute(isAiStream, abortAiStream);
    await restored;
    await expect(
      degradedNotice,
      'the acknowledgement must clear once the page holds a live channel again',
    ).toBeHidden({ timeout: RESTORE_MS });

    // ── RELEASE: the turn the user was waiting for finishes on that page. ────
    releaseFrameHoldFault(frameHoldPath);
    await expect
      .poll(async () => normalizeRendered(await assistantTranscript(page)).length, {
        timeout: SETTLE_MS,
        message: 'the reconnected page must receive the rest of the answer',
      })
      .toBeGreaterThan(preCutRendered.length + RESUMED_GROWTH_MIN_CHARS);

    const settledTranscript = normalizeRendered(await assistantTranscript(page));
    expect(
      firstRepeatedWindow(settledTranscript, DUPLICATE_WINDOW_CHARS),
      'the reconnect must not render the same stretch of the answer twice',
    ).toBeNull();

    // The durable half: the turn the outage interrupted is recorded COMPLETED,
    // not FAILED and not recovered into a second one.
    await api.waitForSession(
      sessionId,
      (record) => String(record.state || '') === 'READY'
        && String(record.last_turn_status || '') === 'COMPLETED',
      SETTLE_MS,
    );
    await expect(
      statusPill,
      'the header must return to READY once the resumed turn settles',
    ).toHaveAttribute('data-state', 'READY', { timeout: RESTORE_MS });

    const reply = page.getByTestId('assistant-message').last();
    await expect(reply, 'the resumed turn should have rendered assistant content').not.toBeEmpty();
    await expect(reply).not.toContainText(TURN_ERROR);
  } finally {
    // The declaration goes first and unconditionally: a hold nobody releases
    // raises on the backend after 120s and fails the turn for an unrelated
    // reason, and a file left behind would park the next turn in this
    // deployment. `trackSessions()` still decides whether the session survives.
    releaseFrameHoldFault(frameHoldPath);
    clearFrameHoldFault(frameHoldPath);
    await page.unrouteAll({ behavior: 'ignoreErrors' }).catch(() => undefined);
    await page.evaluate(() => (window as CutWindow).__astraboxStreamCut.disarm())
      .catch(() => undefined);
    test.info().annotations.push({
      type: 'e2e_stream_error_console',
      description: streamErrorLogs.length
        ? streamErrorLogs.slice(0, 5).join(' | ')
        : 'none — the page logged NO stream error, so the five-minute timer never armed '
          + 'and the page would have stayed silent forever, not for five minutes',
    });
    test.info().annotations.push({
      type: 'e2e_aborted_stream_attempts',
      description: String(abortedStreamAttempts),
    });
  }
});
