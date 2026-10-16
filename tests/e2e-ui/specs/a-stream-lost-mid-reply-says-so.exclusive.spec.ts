/**
 * E2E: the page admits that it lost its stream, while the response keeps
 * running.
 *
 * The journey is a route that goes away mid-answer. The user sends a message,
 * the response starts, and then the connection carrying it breaks — a dropped
 * VPN, a proxy recycled, a captive portal. The platform is untouched: the
 * response is still running in the sandbox and the reply is still owed. What a
 * page that says nothing shows is a "Processing" pill and a live run dot, for
 * as long as the outage lasts; the person watching it concludes the product is
 * broken and reloads.
 *
 * The claim under test is not "reconnect faster" and not "stop the response".
 * It is that a page which has lost its only live channel says so within a
 * window a person will wait, and withdraws the notice once the route returns.
 * The console's notice is `chat:banner.backend_unavailable`, raised when the
 * stream has failed to connect for `STREAM_TROUBLE_NOTICE_DELAY_MS` (10s, in
 * `useSessionChat.ts`) and withdrawn when the stream is connected again. The
 * budget below is the product decision this spec writes down; raising it to
 * make the lane green would be weakening the assertion, not tuning it.
 *
 * WHY the outage is stream-only: killing everything also fails the session
 * detail poll, which raises a DIFFERENT banner (the lifecycle error) within
 * seconds. A spec that watches that banner proves nothing about the stream, so
 * the session GET, the history GETs and every POST are deliberately left
 * alone — and the spec asserts they stayed healthy.
 *
 * WHY both a route abort and an in-page body cut: `page.route` cannot kill an
 * SSE response that is already established (it was matched and released long
 * ago), and a route added later only ever sees the NEXT attempt. The abort
 * makes every reconnect fail; the body cut breaks the connection the page is
 * reading right now. Neither fabricates success — only failure.
 *
 * WHY the reader hold: an E2E-only barrier (`response_part_read`, armed
 * through `fixtures/frameHold.ts`) holds the session's reader right after it
 * reads the response's first `text-delta`, once that part is committed, so
 * the page has it. Nothing the engine writes after it reaches the session until
 * the spec releases the hold, so "the reply is still owed" is true for a
 * bounded window instead of a race against model speed. The backend gives the
 * hold up after 120s (`_HOLD_RELEASE_TIMEOUT_SECONDS` in
 * `astrabox/testing/e2e_faults.py`), which is the ceiling on the whole outage.
 * It needs the E2E deployment stack (`ASTRABOX_E2E_FAULTS=1` and the shared
 * fault-file path); absent that the barrier is never consumed and the spec
 * fails loudly rather than skipping.
 *
 * Engine-independent: `text-delta` is the part type every engine's translated
 * output carries, the prompt asks for tool-free prose, and no assertion reads
 * engine-specific text.
 *
 * NOT COVERED: the flapping stream. The notice is withdrawn whenever a
 * request delivers a part, so a connection that delivers one part and drops
 * again re-arms a fresh notice delay with no ceiling. This spec keeps the
 * route down for the whole window and therefore proves only the sustained
 * outage; the unbounded case needs a spec of its own.
 */
import { expect, test, type Page, type Route } from '@playwright/test';

import { AstraApi, type InputReceipt } from '../fixtures/astraApi';
import { sessionResponseRows } from '../fixtures/dbOracle';
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
 * decides whether a page is working. The fallback is the contract, and the env
 * knob exists so a lane can tighten it — loosening it makes this spec pass by
 * agreeing with a silent page.
 */
const DEGRADED_NOTICE_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_STREAM_DEGRADED_NOTICE_MS', 20_000);
// Upper bound on the response reaching its first text delta at the barrier.
const FIRST_DELTA_MS = parseTimeoutEnv('ASTRABOX_E2E_STREAM_DEGRADED_FIRST_DELTA_MS', 60_000);
// How long the page may take to make its retries visible as failed attempts.
const RETRY_OBSERVED_MS = parseTimeoutEnv('ASTRABOX_E2E_STREAM_DEGRADED_RETRY_MS', 45_000);
// How long the restored route may take to carry a successful request again.
const RESTORE_MS = parseTimeoutEnv('ASTRABOX_E2E_STREAM_DEGRADED_RESTORE_MS', 60_000);
// How long the released response may take to finish and be recorded.
const SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_STREAM_DEGRADED_SETTLE_MS', 90_000);

/**
 * How many reconnects must have failed before the page is judged.
 *
 * This is the anti-vacuity guard for the assertion: without it a page that
 * simply never reconnected — never noticed anything to reconnect from — would
 * satisfy "the stream was down" for the wrong reason. The page's follower
 * retries after 250ms, 1s, 2s and then every 5s, so two refused attempts come
 * within seconds of the cut.
 */
const MIN_ABORTED_ATTEMPTS = 2;

// One rendered character proves the first delta reached the browser. The
// barrier, not an amount of prose, is what proves the response is live.
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

// A FAILED response renders its error INTO the transcript as an assistant
// message (`chat:result.turn_failed`), so "a reply bubble exists" is satisfied
// by exactly the failure under test.
const RESPONSE_ERROR = /This turn failed|API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

test.use({ locale: 'en-US' });

/** One session stream request the BROWSER opened, as the page saw it. */
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
 * The page's follower calls `globalThis.fetch` for every stream request, so
 * wrapping `window.fetch` before the app's first script puts this between the
 * backend and the AI SDK parser. Status, headers and every byte are the
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
          throw new Error('no open session stream request to cut');
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
      if (!armed || method !== 'GET' || url.pathname !== streamPath) {
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
  }, { streamPath: apiPath(`/sessions/${sessionId}/stream`) });
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
 * same parts again puts the same sentences on screen twice. A window this long
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
// response goes BEFORE the tracker's delete.
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

  // Exact pathnames, never a glob: `/sessions/{id}` is a prefix of every
  // session route, and a matcher that caught the others would break the half
  // of the API this outage must leave alone.
  const streamPath = apiPath(`/sessions/${sessionId}/stream`);
  const sessionDetailPath = apiPath(`/sessions/${sessionId}`);
  const isStream = (url: URL) => url.pathname === streamPath;

  // Every stream attempt this spec refused, counted on the Node side where the
  // refusal actually happens.
  let abortedStreamAttempts = 0;
  const abortStream = async (route: Route) => {
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

  // Whether the page noticed the loss at all, if the assertion goes red: a page
  // that logged its lost requests and stayed silent is a different defect from
  // one that never saw a failure. Captured and reported either way.
  const streamErrorLogs: string[] = [];
  page.on('console', (message) => {
    const text = message.text();
    if (text.includes('[useSessionChat] session stream lost:')) {
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
  // Installed BEFORE the first navigation, so the console's first stream
  // request is wrapped too.
  await installStreamCut(page, sessionId);

  const frameHoldPath = frameHoldFaultPath(sessionId);
  armFrameHoldFault(frameHoldPath, sessionId, 'text-delta');
  test.info().annotations.push({ type: 'e2e_reader_part_hold_file', description: frameHoldPath });

  const statusPill = page.getByTestId('run-view').getByTestId('status-pill').first();
  const degradedNotice = page
    .getByTestId('run-view')
    .getByText(DEGRADED_NOTICE, { exact: true });

  try {
    await openSessionView(page, sessionId);
    await expectComposerEnabled(page);

    // ── The user sends one long message from the composer. ───────────────────
    const prompt = [
      `E2E lost route ${runId}. Answer directly and do not use any tool.`,
      'Write four to six paragraphs of plain prose explaining, for a beginner,',
      'what a heartbeat is in a networked application, why a connection can look',
      'healthy while delivering nothing, and how a reader can tell the two apart.',
      'Output only the prose.',
    ].join('\n');
    const delivery = await startPromptDelivery(page, sessionId, prompt);
    const receipt = ((await (await expectPromptDelivered(delivery)).json()) as { data: InputReceipt }).data;
    await expect(
      page.getByTestId('user-message').last(),
      'the user bubble must render — proof the message was sent from the page',
    ).toContainText(String(runId), { timeout: 30_000 });

    // ── Prove the stream was live and delivering before it is cut. ───────────
    // Anti-vacuity first: the reader must have consumed THIS session's own
    // hold. Without it every assertion below could pass on an ordinary finished
    // response — or on a deployment where the barrier is not armed.
    await expect
      .poll(() => frameHoldConsumed(frameHoldPath, sessionId, 'text-delta'), {
        timeout: FIRST_DELTA_MS,
        message:
          'the session reader must consume this session\'s text-delta hold. If it never does, '
          + 'the deployment is not running with ASTRABOX_E2E_FAULTS=1 '
          + `or ${frameHoldPath} is not the path the backend reads.`,
      })
      .toBe(true);
    await expect
      .poll(async () => (await assistantTranscript(page)).trim().length, {
        timeout: FIRST_DELTA_MS,
        message: 'the held text delta must render before the connection is cut',
      })
      .toBeGreaterThanOrEqual(LIVE_PREFIX_MIN_CHARS);
    const heldResponse = sessionResponseRows(sessionId).find((row) => row.input_ids.includes(receipt.input_id));
    expect(heldResponse?.status, 'the response answering the message must be running').toBe('running');
    const responseId = heldResponse!.response_id;
    await expect(
      page.locator(`[data-message-id="${responseId}"]`).getByTestId('assistant-message'),
      'the running response must be on the page, streaming, before the connection is cut',
    ).toHaveAttribute('data-streaming', 'true', { timeout: FIRST_DELTA_MS });

    const preCutRendered = normalizeRendered(await assistantTranscript(page));
    await expect(
      statusPill,
      'the header must read PROCESSING before the cut — the response is live',
    ).toHaveAttribute('data-state', 'PROCESSING');
    const detailsBeforeCut = detailResponses.length;

    // ── THE CUT: the route to the stream disappears. ─────────────────────────
    // Order matters. The abort is armed first so the reconnect the page makes
    // in response to the cut is refused too; the cut then breaks the connection
    // already established, which no route could reach. Everything else — the
    // session GET, the history GETs, every POST — is left alone on purpose.
    await page.route(isStream, abortStream);
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
          `the page must retry its lost stream at least ${MIN_ABORTED_ATTEMPTS} times `
          + '— without that, the silence below is not a silence about anything',
      })
      .toBeGreaterThanOrEqual(MIN_ABORTED_ATTEMPTS);
    const cutConnection = (await streamConnections(page))[cutConnectionId];
    expect(cutConnection.cut, 'the cut connection must be the one this spec broke').toBe(true);
    expect(cutConnection.open, 'the cut connection must be unreadable after the cut').toBe(false);

    // And the outage is stream-only: the rest of the API kept answering, so the
    // banner asserted below can only be the stream one.
    await expect.poll(() => detailResponses.slice(detailsBeforeCut).length, {
      timeout: Math.max(1_000, DEGRADED_NOTICE_BUDGET_MS - (Date.now() - cutAt)),
      message: 'the page must keep reading session detail through the outage',
    }).toBeGreaterThan(0);
    const detailsAfterCut = detailResponses.slice(detailsBeforeCut);
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
        + 'losing it, or the user watches a Processing pill over a stream that is not '
        + 'delivering and reloads',
    ).toBeVisible({ timeout: remainingMs });

    // ── The anti-cheat companion, read at the same moment. ───────────────────
    // The acknowledgement must be about the CONNECTION. A page that quiets
    // itself by pretending the response ended would pass the line above and
    // lose the user's answer, so the response is required to still be running
    // right here.
    const pillAtAdmission = await statusPill.getAttribute('data-state');
    const pulseAtAdmission = await statusPill.getAttribute('data-pulse');
    const detailAtAdmission = await api.getSession(sessionId);
    expect(
      pillAtAdmission,
      'the header must still read PROCESSING — the admission is about the connection',
    ).toBe('PROCESSING');
    expect(pulseAtAdmission, 'the header run dot must still be live').toBe('true');
    expect(
      (detailAtAdmission.current_responses ?? []).map((response) => response.response_id),
      'the platform must still report the response running — nothing was cancelled',
    ).toContain(responseId);

    // ── RESTORE: the route comes back. ───────────────────────────────────────
    // A product that admits an outage must also take the admission back, or the
    // next thing it teaches the user is to ignore it. The response is still
    // held here, so the restored request has nothing new to deliver: the
    // notice has to go because the channel is back, not because parts arrived.
    const restored = page.waitForResponse(
      (response) => response.request().method() === 'GET'
        && isStream(new URL(response.url()))
        && response.status() === 200,
      { timeout: RESTORE_MS },
    );
    await page.unroute(isStream, abortStream);
    await restored;
    await expect(
      degradedNotice,
      'the acknowledgement must clear once the page holds a live channel again',
    ).toBeHidden({ timeout: RESTORE_MS });

    // ── RELEASE: the response the user was waiting for finishes on that page.
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

    // The recorded half: the response the outage interrupted is recorded
    // completed, and it is the only one answering the message — not failed,
    // and not followed by a second one.
    await expect
      .poll(() => {
        const row = sessionResponseRows(sessionId).find((item) => item.response_id === responseId);
        return row ? `${row.status}/${row.outcome ?? ''}` : 'missing';
      }, { timeout: SETTLE_MS, message: 'the platform must record the response as completed' })
      .toBe('ended/completed');
    expect(
      sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(receipt.input_id)),
      'exactly one response answers the message',
    ).toHaveLength(1);
    await api.waitForSessionReady(sessionId, SETTLE_MS);
    await expect(
      statusPill,
      'the header must return to READY once the response ends',
    ).toHaveAttribute('data-state', 'READY', { timeout: RESTORE_MS });

    const reply = page.locator(`[data-message-id="${responseId}"]`).getByTestId('assistant-message');
    await expect(reply, 'the response should have rendered assistant content').not.toBeEmpty();
    await expect(reply).not.toContainText(RESPONSE_ERROR);
  } finally {
    // The declaration goes first and unconditionally: a hold nobody releases
    // stops the reader on the backend after 120s for a reason unrelated to this
    // spec, and a file left behind would hold the next response in this
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
        : 'none — the page logged NO lost stream request, so it never knew to say anything',
    });
    test.info().annotations.push({
      type: 'e2e_aborted_stream_attempts',
      description: String(abortedStreamAttempts),
    });
  }
});
