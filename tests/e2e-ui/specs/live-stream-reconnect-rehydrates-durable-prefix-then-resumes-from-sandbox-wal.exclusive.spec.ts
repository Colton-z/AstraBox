/**
 * E2E: reloading the browser mid-response restores what the response has
 * written so far and follows the same response to its end.
 *
 * An E2E-only barrier (`response_part_read`, armed through
 * `fixtures/frameHold.ts`) holds the session's reader right after it reads the
 * response's first `text-delta`, once that part is committed. The engine keeps
 * running in its box; nothing it writes after that part reaches the session
 * until the spec releases the hold, so the response is open for as long as the
 * spec needs and model speed cannot finish it before the cut.
 *
 * The reload throws away the page. The new page reads history, which folds the
 * open response through the output position it was read at (`output_seq`),
 * and follows the session's stream from exactly that position. It must show
 * the user's message and the reply text so far, then the rest of the response
 * once the hold is released, without painting anything twice. The rest of a
 * text part history already folded arrives as its continuation, which is the
 * seam this spec watches.
 *
 * The platform's records must show one response answering the message, ended
 * `completed`, with exactly one stored `finish` and no `error` part, and stored
 * parts after the position the reloaded page started from.
 *
 * The second half keeps the same page idle and breaks its stream request three
 * times in a row: each time it must come back on its own, from the same cursor,
 * without a response part arriving and without the page leaving READY.
 */
import { test, expect, type Page } from '@playwright/test';

import { AstraApi, answersInput, messageText, type InputReceipt } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import {
  armFrameHoldFault,
  clearFrameHoldFault,
  frameHoldConsumed,
  frameHoldFaultPath,
  releaseFrameHoldFault,
} from '../fixtures/frameHold';
import {
  oracleDbPath,
  sessionOutputRows,
  sessionResponseRows,
} from '../fixtures/dbOracle';
import {
  assistantTranscript,
  expectNoDuplicateTextBlocks,
  firstRepeatedWindow,
  normalizeRendered,
} from '../fixtures/renderedTranscript';

// Upper bound on the response reaching its first text delta at the barrier.
const HOLD_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_LIVE_RECONNECT_POST_TIMEOUT_MS', 90_000);
// Upper bound on the reconnected page receiving the rest of the answer.
const RESUME_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_LIVE_RECONNECT_RESUME_TIMEOUT_MS', 180_000);
// How long the response's recorded end may take to land.
const TERMINAL_PROOF_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TERMINAL_TIMEOUT_MS', 120_000);

// One rendered character proves the held text delta reached the browser. The
// barrier, rather than an arbitrary amount of prose, proves the response is live.
const LIVE_PREFIX_MIN_CHARS = 1;
// Require a visible suffix of the answer after reconnect, measured from text
// parts only. Reasoning, progress labels and usage are not reply content.
const RESUMED_GROWTH_MIN_CHARS = 40;
// Long enough that prose cannot repeat it by accident, short enough to catch a
// replayed prefix. Compared over normalized text (see normalizeRendered).
const DUPLICATE_WINDOW_CHARS = 48;

// A FAILED response renders its error INTO the transcript as an assistant
// message (`chat:result.turn_failed*`), so "there is a reply bubble" is
// satisfied by exactly the failure under test.
const RESPONSE_ERROR = /This turn failed|本轮执行失败|API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

interface BrowserConnection {
  id: number;
  afterSeq: number | null;
  status: number;
  contentType: string;
  raw: string;
  idleStart: number;
  closed: boolean;
  faulted: boolean;
  readerCancelled: boolean;
}

type ReconnectWindow = Window & typeof globalThis & {
  __idleReconnect: {
    connections: BrowserConnection[];
    cursor: (id: number) => number;
    /** Lines since the idle observation began that carry anything but a cursor or a session notice. */
    businessSinceIdle: (id: number) => string[];
    arm: (id: number, minimumCursor: number) => number;
    fail: (id: number) => Promise<void>;
    disarm: () => void;
  };
};

// Only real backend responses establish a connection; a fault only ever errors
// the body the page is reading. No part or successful response is fabricated.
async function installIdleDisconnects(page: Page, sessionId: string): Promise<void> {
  await page.addInitScript(({ streamPath }) => {
    const originalFetch = window.fetch.bind(window);
    const faults = new Map<number, () => Promise<void>>();
    let faultsEnabled = true;
    let idleArmed = false;
    // A cursor follows every burst, and a change to the session record (its
    // generated title) arrives as a transient notice between responses. Both
    // belong to no response, so an idle stream may carry them.
    const idleTypes = new Set(['data-resume-cursor', 'data-session-changed']);
    const completeEvents = (raw: string): string[] => raw.split(/\r?\n\r?\n/).slice(0, -1);
    const state: ReconnectWindow['__idleReconnect'] = {
      connections: [],
      cursor: (id) => {
        const record = state.connections[id];
        let cursor = record.afterSeq ?? 0;
        // Only complete SSE events establish a delivered cursor.
        for (const block of completeEvents(record.raw)) {
          const data = block.split(/\r?\n/).filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).replace(/^ /, '')).join('\n');
          if (!data || data === '[DONE]') continue;
          const frame = JSON.parse(data);
          if (frame.type === 'data-resume-cursor') {
            const next = frame.data?.seq;
            if (!Number.isInteger(next) || next < cursor) throw new Error('invalid delivered cursor');
            cursor = next;
          }
        }
        return cursor;
      },
      businessSinceIdle: (id) => {
        const record = state.connections[id];
        const business: string[] = [];
        for (const line of record.raw.slice(record.idleStart).split(/\r?\n/)) {
          if (!line.trim() || line.startsWith(':')) continue;
          if (!line.startsWith('data:')) {
            business.push(line);
            continue;
          }
          let type = '';
          try {
            type = String((JSON.parse(line.slice(5).trim()) as { type?: unknown }).type ?? '');
          } catch {
            business.push(line);
            continue;
          }
          if (!idleTypes.has(type)) business.push(line);
        }
        return business;
      },
      arm: (id, minimumCursor) => {
        if (idleArmed) throw new Error('idle observation is already armed');
        const record = state.connections[id];
        const cursor = state.cursor(id);
        if (record.closed || cursor < minimumCursor) throw new Error('idle stream has not caught up');
        const tail = record.raw.split(/\r?\n\r?\n/).at(-1) ?? '';
        if (tail.trim()) throw new Error('cannot arm within an incomplete SSE event');
        record.idleStart = record.raw.length;
        idleArmed = true;
        return cursor;
      },
      fail: async (id) => {
        if (!idleArmed) throw new Error('idle observation is not armed');
        const fault = faults.get(id);
        if (!fault) throw new Error(`no live response body for connection ${id}`);
        await fault();
      },
      disarm: () => {
        faultsEnabled = false;
        faults.clear();
      },
    };
    (window as ReconnectWindow).__idleReconnect = state;
    window.fetch = async (input, init) => {
      const request = input instanceof Request ? input : null;
      const method = String(init?.method ?? request?.method ?? 'GET').toUpperCase();
      const url = new URL(request?.url ?? String(input), window.location.href);
      const response = await originalFetch(input, init);
      if (method !== 'GET' || url.pathname !== streamPath) {
        return response;
      }
      const rawCursor = url.searchParams.get('after_seq');
      const record: BrowserConnection = {
        id: state.connections.length,
        afterSeq: rawCursor === null ? null : Number(rawCursor),
        status: response.status,
        contentType: response.headers.get('content-type') || '',
        raw: '', idleStart: 0, closed: false, faulted: false, readerCancelled: false,
      };
      state.connections.push(record);
      if (response.status !== 200 || !record.contentType.includes('text/event-stream') || !response.body) {
        record.closed = true;
        return response;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          if (!faultsEnabled) return;
          faults.set(record.id, async () => {
            if (record.closed || record.faulted) throw new Error(`connection ${record.id} is not open`);
            if (state.businessSinceIdle(record.id).length > 0) {
              throw new Error(`connection ${record.id} received data before the idle disconnect`);
            }
            record.faulted = true;
            record.closed = true;
            faults.delete(record.id);
            controller.error(new TypeError('network error'));
            await reader.cancel('E2E idle network disconnect');
            record.readerCancelled = true;
          });
        },
        async pull(controller) {
          try {
            const chunk = await reader.read();
            if (record.closed) return;
            if (chunk.done) {
              record.closed = true;
              faults.delete(record.id);
              controller.close();
              return;
            }
            record.raw += decoder.decode(chunk.value, { stream: true });
            controller.enqueue(chunk.value);
          } catch (error) {
            if (record.closed) return;
            record.closed = true;
            faults.delete(record.id);
            controller.error(error);
          }
        },
        async cancel(reason) {
          record.closed = true;
          faults.delete(record.id);
          await reader.cancel(reason);
          record.readerCancelled = true;
        },
      });
      return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
    };
  }, { streamPath: apiPath(`/sessions/${sessionId}/stream`) });
}

async function browserConnections(page: Page): Promise<BrowserConnection[]> {
  return page.evaluate(() => (window as ReconnectWindow).__idleReconnect.connections);
}

// Teardown that changes state runs on the passing path only. The interrupt is
// registered BEFORE `trackSessions()` because afterEach hooks run in
// registration order and a response still running cannot be cleaned up after.
// The declaration is removed only on a pass, so a failed run leaves it on disk
// for diagnosis.
let sessionId = '';
let frameHoldPath = '';
onPassOnly(async ({ request }) => {
  if (sessionId) await new AstraApi(request).interruptSession(sessionId);
  clearFrameHoldFault(frameHoldPath);
});
const sessions = trackSessions();

test('live stream reconnect rehydrates durable prefix then resumes from sandbox WAL', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();

  // Fail fast (with the descriptive oracle error) if the database is not
  // reachable — the platform half of this spec depends on it.
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  sessionId = created.session_id;
  sessions.push(sessionId);

  // Every GET the BROWSER makes on the session's stream, with the cursor it
  // asked from, and every newest-history read with the position it carried.
  // Registered before the first navigation. `page.on` reads the page's own
  // network without touching the bodies the page reads — the only oracle that
  // says whether the console came back FROM A POSITION or restarted from the head.
  const streamPath = apiPath(`/sessions/${sessionId}/stream`);
  const historyPath = apiPath(`/sessions/${sessionId}/history-blocks`);
  const streamOpens: { at: number; afterSeq: number | null }[] = [];
  const historyPositions: { at: number; outputSeq: number | null }[] = [];
  page.on('request', (req) => {
    if (req.method() !== 'GET' || new URL(req.url()).pathname !== streamPath) return;
    const raw = new URL(req.url()).searchParams.get('after_seq');
    const parsed = raw === null ? Number.NaN : Number(raw);
    streamOpens.push({ at: Date.now(), afterSeq: Number.isFinite(parsed) ? parsed : null });
  });
  page.on('response', async (resp) => {
    const url = new URL(resp.url());
    if (resp.request().method() !== 'GET' || url.pathname !== historyPath || url.searchParams.has('before')) return;
    const at = Date.now();
    try {
      const body = (await resp.json()) as { data?: { output_seq?: unknown } };
      const seq = body.data?.output_seq;
      historyPositions.push({ at, outputSeq: typeof seq === 'number' ? seq : null });
    } catch {
      historyPositions.push({ at, outputSeq: null });
    }
  });

  try {
    await api.waitForSessionReady(sessionId);

    await installIdleDisconnects(page, sessionId);
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });

    frameHoldPath = frameHoldFaultPath(sessionId);
    armFrameHoldFault(frameHoldPath, sessionId, 'text-delta');
    test.info().annotations.push({ type: 'e2e_reader_part_hold_file', description: frameHoldPath });

    // ── The user sends one long message from the composer. ───────────────────
    // `.fill()` sets the value without key events, so the multi-line prompt is
    // not submitted early by an Enter newline.
    const prompt = [
      `E2E live reconnect ${runId}：请直接用中文回答，不要使用任何工具。`,
      '请写一篇约 500 字的科普短文，介绍中国茶文化的起源、主要茶类（绿茶、红茶、乌龙茶、普洱）以及基本的冲泡与品饮礼仪。',
      '分成 4 到 6 个自然段，语言通顺，只输出文章正文。',
    ].join('\n');
    const composer = page.getByTestId('composer-prompt');
    await expect(composer, 'the composer must be enabled before sending').toBeEnabled({
      timeout: 45_000,
    });
    const inputPath = apiPath(`/sessions/${sessionId}/inputs`);
    const sent = page.waitForResponse((response) => (
      response.request().method() === 'POST' && new URL(response.url()).pathname === inputPath
    ), { timeout: 30_000 });
    await composer.fill(prompt);
    await page.getByTestId('composer-submit').click();
    const receiptResponse = await sent;
    expect(receiptResponse.status(), 'the platform must accept the message').toBe(200);
    const receipt = ((await receiptResponse.json()) as { data: InputReceipt }).data;
    await expect(
      page.getByTestId('user-message').last(),
      'the user bubble should render — proof the message was sent from the page',
    ).toContainText(String(runId), { timeout: 30_000 });

    // Anti-vacuity: the reader must have consumed this session's exact hold.
    // Otherwise every assertion below could pass on an ordinary response that
    // had already ended — or on a deployment where the barrier is not armed.
    await expect
      .poll(() => frameHoldConsumed(frameHoldPath, sessionId, 'text-delta'), {
        timeout: HOLD_TIMEOUT_MS,
        message:
          'the session reader must consume the text-delta hold; if it never does, the deployment '
          + `is not running with ASTRABOX_E2E_FAULTS=1 or ${frameHoldPath} is not the path the backend reads`,
      })
      .toBe(true);
    // An unfinished Markdown delimiter is visible text while the reader is
    // held. Strip decoration only when comparing prose across settlement.
    await expect
      .poll(async () => (await assistantTranscript(page)).trim().length, {
        timeout: HOLD_TIMEOUT_MS,
        message: 'the held text delta must render before the browser reloads',
      })
      .toBeGreaterThanOrEqual(LIVE_PREFIX_MIN_CHARS);
    const heldResponse = sessionResponseRows(sessionId).find((row) => row.input_ids.includes(receipt.input_id));
    expect(heldResponse?.status, 'the response answering the message must be running while the reader is held')
      .toBe('running');
    const responseId = heldResponse!.response_id;
    test.info().annotations.push({ type: 'e2e_response_id', description: responseId });

    // ── THE CUT: the browser drops mid-answer and comes back. ────────────────
    // A reload is the real thing, not a simulation of it: it kills the stream
    // the console was reading and throws away every byte the tab had in memory.
    // Nothing the page draws from here can come from client state.
    //
    // The output position read just before the reload is the floor the new
    // page's history read cannot be below: positions only grow.
    const preCutText = (await assistantTranscript(page)).trim();
    const preCutRendered = normalizeRendered(preCutText);
    const outputSeqBeforeReload = Number((await api.getSession(sessionId)).output_seq ?? 0);
    expect(outputSeqBeforeReload, 'the open response must have stored parts before the reload').toBeGreaterThan(0);
    expect(
      sessionOutputRows(sessionId, responseId).some((row) => row.part.type === 'finish'),
      'the hold must keep the response open: no finish may be stored before the reload',
    ).toBe(false);
    const reloadAt = Date.now();
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });

    // Claim 1, with pixels: the conversation the user was in is intact and the
    // answer so far is back on screen, read from history — the response is one
    // message, and it is still the one the reader holds open.
    await expect(
      page.getByTestId('user-message').filter({ hasText: String(runId) }).last(),
      'the reconnected page must still hold the message that started the response',
    ).toBeVisible({ timeout: 60_000 });
    const responseOnPage = page.locator(`[data-message-id="${responseId}"]`);
    await expect(
      responseOnPage,
      'the reconnected page must show the open response, from history, once',
    ).toHaveCount(1, { timeout: 60_000 });
    // The held prefix has not advanced, so its literal rendering must survive
    // the reload even when it consists entirely of unfinished Markdown.
    await expect
      .poll(async () => (await assistantTranscript(page)).trim(), {
        timeout: 60_000,
        message: 'the reconnected page must restore the reply text it had shown before the reload',
      })
      .toBe(preCutText);
    await expect(responseOnPage).not.toContainText(RESPONSE_ERROR);

    // And it reconnected FROM THE POSITION its history was read at. The page
    // follows its stream from the history page's `output_seq`; anything lower
    // would replay parts that history already folded into the message.
    await expect
      .poll(() => historyPositions.filter((entry) => entry.at >= reloadAt).length, {
        timeout: 60_000,
        message: 'the reloaded page must read its newest history page',
      })
      .toBeGreaterThan(0);
    const historyAfterReload = historyPositions.filter((entry) => entry.at >= reloadAt)[0];
    expect(
      historyAfterReload.outputSeq,
      'the newest history page must carry the position it was read at',
    ).not.toBeNull();
    expect(historyAfterReload.outputSeq!).toBeGreaterThanOrEqual(outputSeqBeforeReload);
    // Counted from the history read on: the reloaded page opens its stream only
    // once it has the history window, so an earlier request can only be the
    // unloading page's.
    await expect
      .poll(() => streamOpens.filter((entry) => entry.at >= historyAfterReload.at).length, {
        timeout: 60_000,
        message: 'the reconnected console must reopen the session stream',
      })
      .toBeGreaterThan(0);
    const reconnects = streamOpens.filter((entry) => entry.at >= historyAfterReload.at);
    expect(
      reconnects[0].afterSeq,
      'the first stream request after the reload must start where the history page was read',
    ).toBe(historyAfterReload.outputSeq);
    test.info().annotations.push({
      type: 'e2e_browser_reconnect_after_seq',
      description: String(reconnects[0].afterSeq),
    });

    // The reloaded page is following from the position history was read at.
    // Release the reader now; every remaining part is the rest this new page
    // must receive without repainting what it already shows.
    releaseFrameHoldFault(frameHoldPath);

    // ── Claim 2, with pixels: the SAME response finishes on the reloaded page.
    // The answer grows past what the dropped tab had seen — content that can
    // only have arrived on the stream this page opened after the cut — and then
    // the header stops pretending to work.
    await expect
      .poll(async () => normalizeRendered(await assistantTranscript(page)).length, {
        timeout: RESUME_TIMEOUT_MS,
        message: 'the followed stream must deliver the rest of the answer to the reloaded page',
      })
      .toBeGreaterThan(preCutRendered.length + RESUMED_GROWTH_MIN_CHARS);
    const statusPill = page.getByTestId('run-view').getByTestId('status-pill').first();
    await expect(
      statusPill,
      'the header must settle once the response ends',
    ).toHaveAttribute('data-pulse', 'false', { timeout: 60_000 });

    // A reply, not a rendered failure, and still one message for the response.
    // Wording is never pinned — the model is its own business.
    await expect(responseOnPage).toHaveCount(1);
    await expect(responseOnPage, 'the response should have rendered assistant content').not.toBeEmpty();
    await expect(responseOnPage).not.toContainText(RESPONSE_ERROR);

    // No stretch of the answer twice. This is the history/stream seam: the page
    // painted the folded prefix and then followed from the position, so
    // anything the stream replayed over the prefix shows up here as repeated
    // prose.
    const settledTranscript = normalizeRendered(await assistantTranscript(page));
    expect(
      firstRepeatedWindow(settledTranscript, DUPLICATE_WINDOW_CHARS),
      'history + stream must not render the same stretch of the answer twice',
    ).toBeNull();
    expect(
      settledTranscript.startsWith(preCutRendered),
      'the answer on the reloaded page must continue the prefix it restored, not replace it',
    ).toBe(true);

    // ── Platform half: one response answered the message, it completed, it
    //    stored one `finish` and no error, and it stored parts after the
    //    position the reloaded page started from. None of this has a pixel. ──
    await expect
      .poll(() => sessionResponseRows(sessionId).find((row) => row.response_id === responseId)?.status ?? 'missing', {
        timeout: TERMINAL_PROOF_BUDGET_MS,
        message: 'the platform must record the response as ended',
      })
      .toBe('ended');
    const responses = sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(receipt.input_id));
    expect(responses, 'exactly one response answers the message').toHaveLength(1);
    expect(responses[0].outcome, 'the response must complete, not fail').toBe('completed');
    const output = sessionOutputRows(sessionId, responseId);
    const outputTypes = output.map((row) => String(row.part.type ?? ''));
    expect(outputTypes.filter((type) => type === 'finish'), 'the response stores exactly one finish').toHaveLength(1);
    expect(outputTypes, 'the response stores no error part').not.toContain('error');
    const finalMaxSeq = Math.max(...output.map((row) => row.seq));
    expect(
      finalMaxSeq,
      'the response must have stored parts after the position the reloaded page followed from',
    ).toBeGreaterThan(historyAfterReload.outputSeq!);

    // History: the message the response is, with no text part written twice.
    // The page merges history and the stream, so a rendered answer alone does
    // not prove what was recorded.
    const recorded = await api.waitForReplyTo(sessionId, receipt.input_id, { timeoutMs: TERMINAL_PROOF_BUDGET_MS });
    expect(recorded.id).toBe(responseId);
    expect(answersInput(recorded, receipt.input_id)).toBe(true);
    expect(recorded.metadata?.outcome).toBe('completed');
    expectNoDuplicateTextBlocks(recorded, 'the reloaded response');
    expect(messageText(recorded).trim(), 'the response must have recorded its answer').not.toEqual('');

    // ── The idle half: the same mounted page, its stream broken while idle. ──
    // Two failures in a row on the SAME page, with nothing arriving in between
    // that could move its cursor.
    const idle = await api.getSession(sessionId);
    expect(idle.state).toBe('READY');
    expect(idle.current_responses ?? []).toEqual([]);
    await expect(statusPill).toHaveAttribute('data-state', 'READY');
    await expect(composer).toBeEnabled();
    const pageInstance = await page.evaluate(() => performance.timeOrigin);
    const userMessages = await page.getByTestId('user-message').count();
    const idleTranscript = normalizeRendered(await assistantTranscript(page));
    const storedBeforeIdle = sessionOutputRows(sessionId).length;
    const idleFloor = Math.max(...sessionOutputRows(sessionId).map((row) => row.seq));
    await expect.poll(async () => (await browserConnections(page)).filter(
      (connection) => !connection.closed,
    ).length, { timeout: 10_000, message: 'an idle stream request must open past the completed reply' }).toBe(1);
    const initialIdle = (await browserConnections(page)).find((connection) => !connection.closed)!;
    await expect.poll(() => page.evaluate((id) => (window as ReconnectWindow).__idleReconnect.cursor(id), initialIdle.id), {
      timeout: 10_000, message: 'the idle stream must start at or past the last stored part',
    }).toBeGreaterThanOrEqual(idleFloor);
    const idleCursor = await page.evaluate(({ id, minimum }) =>
      (window as ReconnectWindow).__idleReconnect.arm(id, minimum), { id: initialIdle.id, minimum: idleFloor });
    expect(idleCursor).toBeGreaterThanOrEqual(finalMaxSeq);

    for (let index = 0; index < 3; index += 1) {
      const connectionId = initialIdle.id + index;
      await expect.poll(async () => (await browserConnections(page)).length, {
        timeout: 10_000,
        message: `idle connection ${index + 1} must establish automatically after the previous body failure`,
      }).toBe(connectionId + 1);
      const connection = (await browserConnections(page))[connectionId];
      expect(connection.status, 'the browser must receive real successful response headers').toBe(200);
      expect(connection.contentType).toContain('text/event-stream');
      expect(connection.closed).toBe(false);
      if (index > 0) {
        expect(connection.afterSeq, 'the page must resume at the same cursor after each fault')
          .toBe(idleCursor);
      }
      expect(
        await page.evaluate((id) => (window as ReconnectWindow).__idleReconnect.businessSinceIdle(id), connectionId),
        'an idle stream request may receive keepalives, cursors and session notices, but no response part',
      ).toEqual([]);
      await expect(page.getByText('network error', { exact: true })).not.toBeVisible();
      await expect(statusPill).toHaveAttribute('data-state', 'READY');
      await expect(composer).toBeEnabled();
      if (index === 2) break;
      await page.evaluate((id) => (window as ReconnectWindow).__idleReconnect.fail(id), connectionId);
      expect((await browserConnections(page))[connectionId]).toMatchObject({
        closed: true, faulted: true, readerCancelled: true,
      });
    }
    expect(await page.evaluate(() => performance.timeOrigin), 'no reload may stand in for the reconnects').toBe(pageInstance);
    await expect(page.getByTestId('user-message')).toHaveCount(userMessages);
    expect(normalizeRendered(await assistantTranscript(page))).toBe(idleTranscript);
    const stillIdle = await api.getSession(sessionId);
    expect(stillIdle.state).toBe('READY');
    expect(stillIdle.current_responses ?? []).toEqual([]);
    expect(sessionOutputRows(sessionId), 'the idle reconnects must not record anything').toHaveLength(storedBeforeIdle);
  } finally {
    // A failed assertion must not strand the reader at the barrier. The
    // declaration stays on disk on failure for diagnosis; a pass removes it in
    // onPassOnly above.
    releaseFrameHoldFault(frameHoldPath);
    if (!page.isClosed()) {
      await page.evaluate(() => (window as ReconnectWindow).__idleReconnect?.disarm());
    }
  }
});
