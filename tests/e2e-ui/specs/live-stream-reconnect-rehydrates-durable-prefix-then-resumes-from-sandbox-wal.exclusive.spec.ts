/**
 * E2E: reloading the browser mid-turn rehydrates the durable prefix and resumes
 * the same live turn to completion.
 *
 * Before reload, the active-turn overlay must expose a non-terminal durable
 * frame cursor. The page reload closes only the reader; the detached worker
 * continues. The browser must reconnect with after_seq at or beyond that
 * watermark, restore the user message and partial reply, then render additional
 * text without duplication.
 *
 * Durable state must record one normal turn.completed event, no turn.recovered
 * event, exactly one finish, no recovery adapter frame, and a later live source
 * cursor. An E2E-only bridge barrier holds the first text delta until the page
 * has reconnected, so model generation speed cannot turn coverage into a skip.
 */
import fs from 'node:fs';
import path from 'node:path';

import { test, expect, type Page } from '@playwright/test';

import { AstraApi, type MessageRecord } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import {
  framesForTurn,
  sessionEvents,
  oracleDbPath,
  waitForTurnTerminalProof,
} from '../fixtures/dbOracle';

// One sandbox provision + a long streamed turn + the mid-stream disconnect + the
// browser's reconnect + the durable terminal settle sit above the suite default;
// keep the budget generous and env-tunable.
// Upper bound on waiting for the armed bridge barrier to observe a text delta.
const POST_ABORT_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_LIVE_RECONNECT_POST_TIMEOUT_MS', 90_000);
// How long to wait for the active turn's durable overlay cursor to appear.
const OVERLAY_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_LIVE_RECONNECT_OVERLAY_TIMEOUT_MS', 90_000);
// Upper bound on the reconnected page tailing the rest of the answer in.
const RESUME_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_LIVE_RECONNECT_RESUME_TIMEOUT_MS', 180_000);
// How long to wait for the turn's terminal proof to become durable.
const TERMINAL_PROOF_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TERMINAL_TIMEOUT_MS', 120_000);

// One rendered character proves the first text delta reached the browser. The
// barrier, rather than an arbitrary amount of prose, proves the turn is live.
const LIVE_PREFIX_MIN_CHARS = 1;
// Require a visible suffix of the answer after reconnect, measured from text
// parts only. Reasoning, progress labels and usage are not reply content.
const RESUMED_GROWTH_MIN_CHARS = 40;
// Long enough that prose cannot repeat it by accident, short enough to catch a
// replayed prefix. Compared over normalized text (see normalizeRendered).
const DUPLICATE_WINDOW_CHARS = 48;

// A FAILED turn renders its error INTO the transcript as an assistant message,
// so "there is a reply bubble" is satisfied by exactly the failure under test —
// and a failed turn ends on an `error` terminal, which would let the resume
// assertions pass on a turn that never reached a clean `finish`.
const TURN_ERROR = /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

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
    arm: (id: number, minimumCursor: number) => number;
    fail: (id: number) => Promise<void>;
    disarm: () => void;
  };
};

// The donor's body error is retained; only actual backend responses can establish
// a connection. No business frames or successful HTTP responses are fabricated.
async function installIdleDisconnects(page: Page, sessionId: string): Promise<void> {
  await page.addInitScript(({ streamPath }) => {
    const originalFetch = window.fetch.bind(window);
    const faults = new Map<number, () => Promise<void>>();
    let faultsEnabled = true;
    let idleArmed = false;
    const state: ReconnectWindow['__idleReconnect'] = {
      connections: [],
      cursor: (id) => {
        const record = state.connections[id];
        let cursor = record.afterSeq ?? 0;
        // Only complete SSE events establish a delivered cursor.
        for (const block of record.raw.split(/\r?\n\r?\n/).slice(0, -1)) {
          const data = block.split(/\r?\n/).filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).replace(/^ /, '')).join('\n');
          if (!data || data === '[DONE]') continue;
          const frame = JSON.parse(data);
          if (frame.type === 'data-resume-cursor') {
            const next = frame.data?.frameSeq;
            if (!Number.isInteger(next) || next < cursor) throw new Error('invalid delivered cursor');
            cursor = next;
          }
        }
        return cursor;
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
      if (method !== 'GET' || url.pathname !== streamPath || url.searchParams.get('follow') !== 'session') {
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
            if (record.raw.slice(record.idleStart).split(/\r?\n/).some((line) => line.trim() && !line.startsWith(':'))) {
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
  }, { streamPath: apiPath(`/sessions/${sessionId}/ai-stream`) });
}

async function browserConnections(page: Page): Promise<BrowserConnection[]> {
  return page.evaluate(() => (window as ReconnectWindow).__idleReconnect.connections);
}

const FAULT_BASE = (
  process.env.ASTRABOX_E2E_TURN_TERMINAL_DROP_FAULT_FILE
  || '/tmp/astrabox-e2e-turn-terminal-drop-faults.json'
).trim();
const FAULT_DIR = `${FAULT_BASE}.d`;

interface FrameHoldFault {
  faults: { hold_after_frame: number };
  match: { session_id: string; frame_type: string };
  release: boolean;
  consumed: unknown[];
}

function frameHoldFaultPath(sessionId: string): string {
  const slug = `${test.info().title} ${sessionId}`
    .replace(/[^a-zA-Z0-9_.-]+/g, '-')
    .replace(/^-|-$/g, '');
  return path.join(FAULT_DIR, `w${test.info().workerIndex}-${process.pid}-${slug}.json`);
}

function writeFrameHoldFault(faultPath: string, payload: FrameHoldFault): void {
  const temporary = `${faultPath}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(payload), 'utf8');
  fs.chmodSync(temporary, 0o644);
  fs.renameSync(temporary, faultPath);
}

function armFrameHoldFault(faultPath: string, sessionId: string): void {
  fs.mkdirSync(FAULT_DIR, { recursive: true });
  fs.chmodSync(FAULT_DIR, 0o777);
  writeFrameHoldFault(faultPath, {
    faults: { hold_after_frame: 1 },
    match: { session_id: sessionId, frame_type: 'text-delta' },
    release: false,
    consumed: [],
  });
}

function readFrameHoldFault(faultPath: string): FrameHoldFault | null {
  if (!fs.existsSync(faultPath)) return null;
  try {
    return JSON.parse(fs.readFileSync(faultPath, 'utf8')) as FrameHoldFault;
  } catch {
    return null;
  }
}

function releaseFrameHoldFault(faultPath: string): void {
  const payload = readFrameHoldFault(faultPath);
  if (!payload || payload.release) return;
  writeFrameHoldFault(faultPath, { ...payload, release: true });
}

function clearFrameHoldFault(faultPath: string): void {
  if (faultPath && fs.existsSync(faultPath)) fs.rmSync(faultPath, { force: true });
}

/** Rendered reply parts only; reasoning can collapse across reload or settlement. */
async function assistantTranscript(page: Page): Promise<string> {
  return (await page.getByTestId('assistant-text').allInnerTexts()).join('\n');
}

/**
 * Rendered prose with layout and markdown decoration removed.
 *
 * Comparisons here run across a page reload and across the stream/settled
 * boundary, where the SAME text is not the same string: a half-arrived `**` is
 * literal mid-stream and gone once the emphasis closes, and line wrapping moves
 * with the panel. Normalizing keeps the assertions about content.
 */
function normalizeRendered(text: string): string {
  return text.replace(/\s+/g, '').replace(/[*_`#>|~\-–—·•]/g, '');
}

/**
 * The first stretch of `text` that appears twice, or null.
 *
 * The page-side reading of "no duplicated assistant text": a reconnect that
 * rehydrates the prefix and then replays it AGAIN off the resumed stream shows
 * the user the same sentences twice. A window this long cannot recur in prose by
 * accident, so a hit is a repaint defect and not a wordy model.
 */
function firstRepeatedWindow(text: string, size: number): string | null {
  if (text.length < size * 2) return null;
  const seen = new Map<string, number>();
  for (let i = 0; i + size <= text.length; i += 1) {
    const chunk = text.slice(i, i + size);
    const first = seen.get(chunk);
    if (first !== undefined) {
      // The window alone cannot be diagnosed — say WHERE both occurrences sit
      // and what surrounds them, or the failure is a scavenger hunt through a
      // transcript nobody kept.
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

interface DurableOverlayCursor {
  turnId: string;
  frameSeq: number;
}

// The messages overlay is the cursor source the console reads on mount. Returning
// null lets the caller classify an early model completion as an unmet precondition.
async function waitForDurableOverlayCursor(
  api: AstraApi,
  sessionId: string,
  timeoutMs: number,
): Promise<DurableOverlayCursor | null> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const page = await api.getMessages(sessionId, 50);
    const overlay = page.active_turn_overlay;
    const turnId = String(overlay?.turn_id ?? overlay?.resume_cursor?.turn_id ?? '').trim();
    const frameSeq = overlay?.resume_cursor?.frame_seq;
    if (turnId && typeof frameSeq === 'number' && Number.isInteger(frameSeq) && frameSeq >= 0) {
      return { turnId, frameSeq };
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  return null;
}

interface MinimalFrame {
  frame_seq: number;
  type: string;
  live_seq?: number;
  source_kind?: string;
}

// Keep only fields needed to compare durable replay and live-source progress.
function minimalFramesForTurn(turnId: string): MinimalFrame[] {
  return framesForTurn(turnId).map((frame) => {
    const payload =
      frame.payload && typeof frame.payload === 'object'
        ? (frame.payload as { type?: unknown })
        : {};
    const rawLiveSeq = Number(frame.live_seq);
    const rawSourceKind = frame.source_kind;
    return {
      frame_seq: Number(frame.event_seq),
      type: String(payload.type ?? '').trim(),
      live_seq:
        frame.live_seq === undefined ||
        frame.live_seq === null ||
        !Number.isFinite(rawLiveSeq)
          ? undefined
          : rawLiveSeq,
      source_kind:
        rawSourceKind === undefined || rawSourceKind === null
          ? undefined
          : String(rawSourceKind),
    };
  });
}

// A null result means no live-source cursor was durable at the reconnect watermark.
function maxLiveSourceSeqAtOrBefore(frames: MinimalFrame[], frameSeq: number): number | null {
  const seqs = frames
    .filter(
      (frame) =>
        Number.isFinite(frame.frame_seq) &&
        frame.frame_seq <= frameSeq &&
        frame.live_seq !== undefined,
    )
    .map((frame) => Number(frame.live_seq))
    .filter((seq) => Number.isFinite(seq));
  return seqs.length > 0 ? Math.max(...seqs) : null;
}

// Event counts distinguish normal live completion from recovery settlement.
function journalTerminalCounts(sessionId: string, turnId: string): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const event of sessionEvents(sessionId)) {
    if (String(event.turn_id ?? '').trim() !== turnId) continue;
    const eventType = String(event.event_type ?? '').trim();
    if (!eventType) continue;
    counts[eventType] = (counts[eventType] || 0) + 1;
  }
  return counts;
}

// The page and persisted duplicate checks cover separate writers. The page can
// replay a prefix twice; the worker can materialize the same text block twice.
// Short duplicate blocks also fall below the rendered sliding-window threshold.
function expectNoDuplicateTextBlocks(message: MessageRecord, label: string): void {
  const textBlocks = (message.blocks || [])
    .filter((block) => String(block.type || '') === 'text')
    .map((block) => String(block.text || block.content || '').trim())
    .filter((text) => text.length > 20);
  const duplicates = textBlocks.filter((text, index) => textBlocks.indexOf(text) !== index);
  expect(duplicates, `${label} should not duplicate persisted assistant text blocks`).toEqual([]);
}

// Teardown that changes state runs on the passing path only. The settling
// hook is registered BEFORE `trackSessions()` on purpose: afterEach hooks run
// in registration order, and the session delete cannot succeed until the turn
// has settled — a DELETE against a still-STREAMING session answers
// SESSION_BUSY and leaves the orphan this ordering exists to avoid.
//
// A probe-skip can leave the detached worker still streaming (DELETE would
// then 409 SESSION_BUSY); interrupt first to settle it, then delete. Both are
// best-effort — the happy path is already READY and interrupt is a harmless
// no-op there.
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

  // Fail fast (with the descriptive oracle error) if the document store is not
  // reachable — the durable half of this spec depends on it.
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  sessionId = created.session_id;
  sessions.push(sessionId);

  // Every GET the BROWSER makes on the session's stream, with the cursor it asked
  // from. Registered before the first navigation so the console's first channel
  // open is captured too. `page.on('request')` reads the page's own network
  // without touching the SSE body the page is reading — the only oracle that says
  // whether the console came back FROM A CURSOR or restarted from the head.
  const streamOpens: { url: string; afterSeq: number | null }[] = [];
  page.on('request', (req) => {
    if (req.method() !== 'GET' || !req.url().includes('/ai-stream')) return;
    const raw = new URL(req.url()).searchParams.get('after_seq');
    const parsed = raw === null ? Number.NaN : Number(raw);
    streamOpens.push({ url: req.url(), afterSeq: Number.isFinite(parsed) ? parsed : null });
  });

  try {
    await api.waitForSessionReady(sessionId);

    await installIdleDisconnects(page, sessionId);
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });

    frameHoldPath = frameHoldFaultPath(sessionId);
    armFrameHoldFault(frameHoldPath, sessionId);
    test.info().annotations.push({ type: 'e2e_turn_frame_hold_file', description: frameHoldPath });

    // ── The user sends one long turn from the composer. ──────────────────────
    // The bridge pauses immediately after its first text delta. This keeps a real
    // vendor turn and real sandbox transport in flight while making the cut point
    // independent of model speed. `.fill()` sets the value without key events, so
    // the multi-line prompt is not submitted early by an Enter newline.
    const prompt = [
      `E2E live reconnect ${runId}：请直接用中文回答，不要使用任何工具。`,
      '请写一篇约 500 字的科普短文，介绍中国茶文化的起源、主要茶类（绿茶、红茶、乌龙茶、普洱）以及基本的冲泡与品饮礼仪。',
      '分成 4 到 6 个自然段，语言通顺，只输出文章正文。',
    ].join('\n');
    const composer = page.getByTestId('composer-prompt');
    await expect(composer, 'the composer must be enabled before sending').toBeEnabled({
      timeout: 45_000,
    });
    await composer.fill(prompt);
    await page.getByTestId('composer-submit').click();
    await expect(
      page.getByTestId('user-message').last(),
      'the user bubble should render — proof the turn was dispatched from the page',
    ).toContainText(String(runId), { timeout: 30_000 });

    // Anti-vacuity: the server must consume this session's exact fault. Otherwise
    // every assertion below could pass on an ordinary, already-settled turn.
    await expect
      .poll(
        () => {
          const consumed = readFrameHoldFault(frameHoldPath)?.consumed;
          return Array.isArray(consumed)
            ? consumed.some((entry) => {
                const record = entry as { session_id?: unknown; frame_type?: unknown };
                return record.session_id === sessionId && record.frame_type === 'text-delta';
              })
            : false;
        },
        {
          timeout: POST_ABORT_TIMEOUT_MS,
          message: 'the real turn worker must consume the text-delta frame hold',
        },
      )
      .toBe(true);

    // An unfinished Markdown delimiter is visible text while the bridge is held.
    // Strip decoration only when comparing prose across stream settlement.
    await expect
      .poll(async () => (await assistantTranscript(page)).trim().length, {
        timeout: POST_ABORT_TIMEOUT_MS,
        message: 'the held text delta must render before the browser disconnects',
      })
      .toBeGreaterThanOrEqual(LIVE_PREFIX_MIN_CHARS);

    // ── The durable reconnect cursor: the active turn's overlay watermark. ───
    // The barrier keeps the turn active while the asynchronous durable writer
    // commits the first delta. This is the same first-page read the console makes
    // on mount, taken here because the cursor itself has no pixel.
    const cursor = await waitForDurableOverlayCursor(api, sessionId, OVERLAY_BUDGET_MS);
    expect(cursor, 'the held turn must expose a durable active_turn_overlay cursor').not.toBeNull();
    const durableCursor = cursor as DurableOverlayCursor;

    // ── Rehydrate the durable prefix and prove its live WAL provenance. ───────
    // Frames at/before the cursor are the durable prefix. They must (1) carry a
    // server-side live source position (`live_seq`) and (2) NOT yet contain the
    // terminal finish (the cut lands mid-turn). Filtering by frame_seq <= cursor
    // keeps this stable even if the worker races ahead and writes the finish
    // before this read; one read feeds both derivations so they see the same
    // frame set.
    const prefixArtifactFrames = minimalFramesForTurn(durableCursor.turnId);
    const prefixFrames = prefixArtifactFrames.filter(
      (frame) => frame.frame_seq <= durableCursor.frameSeq,
    );

    expect(
      prefixFrames.some((frame) => frame.type === 'finish'),
      'the barrier must hold the bridge before the durable terminal finish',
    ).toBe(false);

    // `live_seq` is the server-side WAL source position persisted from the agent
    // stream. It remains an internal recovery oracle; the browser resumes from
    // the public durable `resume_cursor.frame_seq` asserted below.
    const prefixSourceSeq = maxLiveSourceSeqAtOrBefore(
      prefixArtifactFrames,
      durableCursor.frameSeq,
    );
    expect(
      prefixSourceSeq,
      'durable prefix must prove a live WAL source cursor (live_seq)',
    ).not.toBeNull();

    // ── THE CUT: the browser drops mid-answer and comes back. ────────────────
    // A reload is the real thing, not a simulation of it: it kills the channel
    // the console was reading and throws away every byte the tab had in memory.
    // Nothing the page draws from here can come from client state.
    //
    // Sampled HERE, not at the probe above: the answer kept streaming while the
    // cursor was read, and "the rest of it arrived after the cut" is only a claim
    // about new content if it is measured against what the dropped tab actually
    // had on screen.
    const preCutText = (await assistantTranscript(page)).trim();
    expect(preCutText.length, 'the cut must retain a nonempty displayed reply prefix')
      .toBeGreaterThanOrEqual(LIVE_PREFIX_MIN_CHARS);
    const preCutRendered = normalizeRendered(preCutText);
    const streamOpensBeforeCut = streamOpens.length;
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });

    // Claim 1, with pixels: the conversation the user was in is intact and the
    // answer-so-far is back on screen. The tab that watched it live is gone, so
    // this is durable state the console re-fetched — the messages first page plus
    // the active turn's overlay — which is what "rehydrate the durable prefix"
    // means to the person reading it.
    await expect(
      page.getByTestId('user-message').filter({ hasText: String(runId) }).last(),
      'the reconnected page must still hold the message that started the live turn',
    ).toBeVisible({ timeout: 60_000 });
    // Existence FIRST, then content: a negated Playwright matcher passes on an
    // element that was never found, so `not.toBeEmpty()` alone would be green on
    // the exact failure this line is here for — a transcript that came back with
    // no answer in it at all.
    const rehydratedReply = page.getByTestId('assistant-message').last();
    await expect(
      rehydratedReply,
      'the reconnected page must rehydrate the answer-so-far, not an empty transcript',
    ).toBeVisible({ timeout: 60_000 });
    await expect(rehydratedReply).not.toBeEmpty();
    // The held prefix has not advanced, so its literal rendering must survive
    // reload even when it consists entirely of unfinished Markdown decoration.
    await expect
      .poll(async () => (await assistantTranscript(page)).trim(), {
        timeout: 60_000,
        message: 'the reconnected page must restore the displayed reply prefix before release',
      })
      .toBe(preCutText);
    // Same guard as the settled reply below, one step earlier: a turn that FAILED
    // renders its error INTO the transcript as an assistant bubble, so "an
    // assistant bubble came back with content in it" is satisfied by exactly the
    // failure this spec must not pass on — and catching it here fails in seconds
    // instead of after the whole RESUME_TIMEOUT_MS growth poll expires.
    await expect(rehydratedReply).not.toContainText(TURN_ERROR);

    // And it reconnected FROM THE CURSOR. The console reopens its channel as GET
    // ai-stream?follow=session&after_seq=<overlay frame_seq>; a reconnect without
    // a cursor would replay the whole turn on top of the prefix it just painted,
    // which is the duplication asserted against below.
    await expect
      .poll(() => streamOpens.length - streamOpensBeforeCut, {
        timeout: 60_000,
        message: 'the reconnected console must reopen the session stream',
      })
      .toBeGreaterThan(0);
    const reconnectCursors = streamOpens
      .slice(streamOpensBeforeCut)
      .map((open) => open.afterSeq)
      .filter((seq): seq is number => seq !== null && seq >= 0);
    expect(
      reconnectCursors.length,
      'the reconnect must carry a durable cursor (after_seq), not open the channel cursorless',
    ).toBeGreaterThan(0);
    const browserReconnectCursor = Math.max(...reconnectCursors);
    // Not merely "some after_seq": `after_seq=0` IS a restart from the head, so a
    // presence check cannot say what this line has to say. The floor is the
    // watermark captured mid-turn, because the console reads the SAME overlay on
    // mount (useSessionChat cursorFromOverlay ← active_turn_overlay.resume_cursor)
    // and that watermark — source_frame_seq_applied, the highest durable frame
    // folded into the overlay message — only advances within a turn. So the
    // browser's own cursor is necessarily at or past that watermark, and anything below it
    // means the console went back for frames it had already painted.
    expect(
      browserReconnectCursor,
      'the reconnect must resume AT OR PAST the durable watermark the turn had mid-flight, ' +
        'not restart the stream from the head',
    ).toBeGreaterThanOrEqual(durableCursor.frameSeq);
    test.info().annotations.push({
      type: 'e2e_browser_reconnect_after_seq',
      description: String(browserReconnectCursor),
    });

    // The reconnected browser has proven it opened from the durable watermark.
    // Release the real worker now; every remaining frame is the suffix this new
    // channel must tail without repainting the prefix.
    releaseFrameHoldFault(frameHoldPath);

    // ── Claim 2, with pixels: the SAME turn finishes on the reconnected page. ─
    // The answer grows past what the dropped tab had seen — content that can only
    // have arrived on the connection this page opened after the cut — and then
    // the header stops pretending to work.
    await expect
      .poll(async () => normalizeRendered(await assistantTranscript(page)).length, {
        timeout: RESUME_TIMEOUT_MS,
        message: 'the resumed stream must deliver the rest of the answer to the reconnected page',
      })
      .toBeGreaterThan(preCutRendered.length + RESUMED_GROWTH_MIN_CHARS);
    await expect(
      page.getByTestId('run-view').getByTestId('status-pill').first(),
      'the header must settle once the resumed turn reaches its terminal frame',
    ).toHaveAttribute('data-pulse', 'false', { timeout: 60_000 });

    // A reply, not a rendered failure. Wording is never pinned — the backend is
    // deepseek and non-deterministic — so what is asserted is that the last bubble
    // carries content and is not the failure bubble a lost turn leaves.
    const reply = page.getByTestId('assistant-message').last();
    await expect(reply, 'the resumed turn should have rendered assistant content').not.toBeEmpty();
    await expect(reply).not.toContainText(TURN_ERROR);

    // No stretch of the answer twice. This is the rehydrate/resume seam: the page
    // painted the durable prefix and then tailed from the cursor, so anything the
    // resume replayed over the prefix shows up here as repeated prose.
    const settledTranscript = normalizeRendered(await assistantTranscript(page));
    expect(
      firstRepeatedWindow(settledTranscript, DUPLICATE_WINDOW_CHARS),
      'rehydrate + resume must not render the same stretch of the answer twice',
    ).toBeNull();

    // ── Durable half: the committed turn advanced past the reconnect cursor,
    //    finished exactly once, completed LIVE (not recovered), and did not
    //    append a synthetic recovery finish. None of this has a pixel: a turn the
    //    live worker completed and one a recovery path rebuilt render the same
    //    finished answer, and that difference is the spec. ────────────────────
    // The same turn must record COMPLETED as well as a durable terminal frame.
    await waitForTurnTerminalProof(
      sessionId,
      durableCursor.turnId,
      'COMPLETED',
      TERMINAL_PROOF_BUDGET_MS,
    );
    const finalFrames = minimalFramesForTurn(durableCursor.turnId);
    const finalFrameSeqs = finalFrames
      .map((frame) => frame.frame_seq)
      .filter((seq) => Number.isFinite(seq));
    const finalMaxFrameSeq = finalFrameSeqs.length > 0 ? Math.max(...finalFrameSeqs) : durableCursor.frameSeq;

    expect(
      finalMaxFrameSeq,
      'durable frames should advance after the WAL-suffix resume',
    ).toBeGreaterThan(durableCursor.frameSeq);
    const finalTypes = finalFrames.map((frame) => frame.type);
    expect(finalTypes, 'final durable frames should include terminal finish').toContain('finish');
    expect(
      finalFrames.filter((frame) => frame.type === 'finish'),
      'live/WAL resume should commit exactly one durable finish frame',
    ).toHaveLength(1);

    const finalCounts = journalTerminalCounts(sessionId, durableCursor.turnId);
    expect(
      finalCounts['turn.completed'] || 0,
      'live/WAL resume should write exactly one normal completed terminal event',
    ).toBe(1);
    expect(
      finalCounts['turn.recovered'] || 0,
      'the browser reconnect must not write turn.recovered while the live worker completes',
    ).toBe(0);

    // The cursor the BROWSER reconnected on must itself land inside the durable
    // WAL — the original spec asserted this of the cursor the test chose; now that
    // the console picks its own, it is asserted of that one.
    expect(
      maxLiveSourceSeqAtOrBefore(finalFrames, browserReconnectCursor),
      "the browser's reconnect cursor must map to a live WAL source cursor (live_seq)",
    ).not.toBeNull();

    expect(
      finalFrames.map((frame) => frame.source_kind),
      'live/WAL resume should not append a synthetic turn-recovery finish',
    ).not.toContain('turn_recovery');

    // Materialization, which has no pixel of its own: the settled transcript on
    // screen is the console's merge of durable records and the frames it tailed,
    // so a rendered answer does NOT prove the worker committed an assistant
    // message. This asserts the durable row exists and does not carry the same
    // text twice — see expectNoDuplicateTextBlocks for why it stands beside the
    // page-side duplicate scan rather than being replaced by it.
    const finalMessages = await api.getMessages(sessionId, 50);
    const finalAssistantMessages = (finalMessages.messages || []).filter(
      (message) => message.role === 'assistant',
    );
    const finalAssistant = finalAssistantMessages[finalAssistantMessages.length - 1];
    expect(finalAssistant, 'live/WAL resume should persist a final assistant message').toBeTruthy();
    expectNoDuplicateTextBlocks(finalAssistant as MessageRecord, 'live/WAL resume final assistant');

    const finalSourceSeq = maxLiveSourceSeqAtOrBefore(finalFrames, finalMaxFrameSeq);
    expect(
      finalSourceSeq,
      'final durable live source cursor should advance past the prefix',
    ).toBeGreaterThan(prefixSourceSeq as number);

    // The source case is idle: two failures in the SAME mounted page, without
    // receiving another frame that could reset its cursor or reconnect budget.
    const idle = await api.getSession(sessionId);
    expect(idle.state).toBe('READY');
    expect(idle.current_turn_id).toBeFalsy();
    const statusPill = page.getByTestId('run-view').getByTestId('status-pill').first();
    await expect(statusPill).toHaveAttribute('data-state', 'READY');
    await expect(composer).toBeEnabled();
    const pageInstance = await page.evaluate(() => performance.timeOrigin);
    const userMessages = await page.getByTestId('user-message').count();
    const idleTranscript = normalizeRendered(await assistantTranscript(page));
    // First-turn title publication follows READY. It must finish before the
    // no-business-frame window, not before this subscription was established.
    await expect.poll(() => minimalFramesForTurn(durableCursor.turnId)
      .some((frame) => frame.type === 'data-session-changed'), {
      timeout: 10_000, message: 'the first-turn title notification must be published before idle faults',
    }).toBe(true);
    const idleFrameSeq = Math.max(...minimalFramesForTurn(durableCursor.turnId).map((frame) => frame.frame_seq));
    await expect.poll(async () => (await browserConnections(page)).filter(
      (connection) => !connection.closed,
    ).length, { timeout: 10_000, message: 'a real idle subscription must open beyond the completed reply' }).toBe(1);
    const initialIdle = (await browserConnections(page)).find((connection) => !connection.closed)!;
    await expect.poll(() => page.evaluate((id) => (window as ReconnectWindow).__idleReconnect.cursor(id), initialIdle.id), {
      timeout: 10_000, message: 'the real idle stream must receive the post-turn durable cursor',
    }).toBeGreaterThanOrEqual(idleFrameSeq);
    const idleCursor = await page.evaluate(({ id, minimum }) =>
      (window as ReconnectWindow).__idleReconnect.arm(id, minimum), { id: initialIdle.id, minimum: idleFrameSeq });
    expect(idleCursor).toBeGreaterThanOrEqual(finalMaxFrameSeq);

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
        expect(connection.afterSeq, 'the application must resume at the same delivered idle cursor after each fault')
          .toBe(idleCursor);
      }
      expect(connection.raw.slice(connection.idleStart).split(/\r?\n/).filter((line) => line.trim() && !line.startsWith(':')),
        'an idle connection may receive keepalive comments but no business frame').toEqual([]);
      await expect(page.getByText('network error', { exact: true })).not.toBeVisible();
      await expect(statusPill).toHaveAttribute('data-state', 'READY');
      await expect(composer).toBeEnabled();
      if (index === 2) break;
      await page.evaluate((id) => (window as ReconnectWindow).__idleReconnect.fail(id), connectionId);
      expect((await browserConnections(page))[connectionId]).toMatchObject({
        closed: true, faulted: true, readerCancelled: true,
      });
    }
    expect(await page.evaluate(() => performance.timeOrigin), 'no reload may reset the reconnect budget').toBe(pageInstance);
    await expect(page.getByTestId('user-message')).toHaveCount(userMessages);
    expect(normalizeRendered(await assistantTranscript(page))).toBe(idleTranscript);
    const stillIdle = await api.getSession(sessionId);
    expect(stillIdle.state).toBe('READY');
    expect(stillIdle.current_turn_id).toBeFalsy();
    expect(journalTerminalCounts(sessionId, durableCursor.turnId)).toEqual(finalCounts);
  } finally {
    // A failed assertion must not strand the real worker at the E2E barrier. The
    // declaration remains on disk on failure for diagnosis; passing cleanup is
    // handled by onPassOnly above.
    releaseFrameHoldFault(frameHoldPath);
    if (!page.isClosed()) {
      await page.evaluate(() => (window as ReconnectWindow).__idleReconnect?.disarm());
    }
  }
});
