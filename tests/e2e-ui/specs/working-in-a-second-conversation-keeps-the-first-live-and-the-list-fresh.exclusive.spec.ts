/**
 * E2E: working in a second conversation while the first is still answering.
 *
 * The journey is the one every user of two conversations performs. A long
 * question is asked in conversation A; while the answer is still arriving the
 * user clicks conversation B in the sidebar, asks something there, and then
 * clicks back to A. What they expect on return is the answer-so-far still on
 * screen, whole and not repeated, still arriving — and the sidebar to have kept
 * up with B in the meantime.
 *
 * WHAT IS NEW HERE. Every neighbouring spec cuts this seam somewhere else:
 * `live-stream-reconnect-...` destroys the document with `page.reload()`;
 * `session-stream-disposal` departs BEFORE any stream opens;
 * `active-task-notification-cold-join` rejoins an active session cold;
 * `backend-restart-rehydrates-...` leaves a PARKED conversation. This is the
 * only case where the document, the un-keyed outer `SessionPage`, its persisted
 * refs and ANOTHER conversation's live stream all survive across the cut — and
 * the only place two turns are proven in flight at once from the browser's seat
 * (`tests/e2e/test_shared_box_cohabitation.py:152` proves it over the API).
 *
 * WHY THE BARRIER. `hold_after_frame` on `text-delta` parks A's real turn worker
 * right after its first delta (`astrabox/testing/e2e_faults.py:340-367`), so "A
 * is still answering while I work in B" is true for a bounded window instead of
 * a race against model speed. Its release timeout is 120s
 * (`e2e_faults.py:44`) and that, not the suite wall, is the ceiling on
 * everything between arming and release: every wait inside the held window is
 * clamped to it by `heldBudget()` and the release runs in a `finally`, so a
 * failing assertion fails as itself instead of as a turn the barrier killed.
 *
 * ENGINE-INDEPENDENT. `text-delta` is the normalized frame type every engine's
 * translated frames carry, the first-turn title is written by the platform's
 * own `session_title_service`, both prompts ask for tool-free prose, and no
 * assertion reads engine-specific text or a localized string. It DOES require
 * the E2E fault stack (`ASTRABOX_E2E_FAULTS=1` and the shared fault-file path)
 * like the other barrier specs; without it the declaration is never consumed
 * and the anti-vacuity poll fails loudly rather than skipping.
 *
 * THE LIST HALF, AND WHY IT IS READ HERE. The conversation rail is a
 * `useSWRInfinite` that refreshes every `RAIL_REFRESH_INTERVAL_MS`
 * (`frontend/src/App.tsx:52`, passed at `:122`), which SWR suspends while the
 * tab is hidden. That timer is the only producer that can reach a row the
 * reader is not standing on: `refreshOverview` (`App.tsx:154-156`) is called by
 * the in-tab conversation create (`:159`), by archive (`:177-183`) and by the
 * SessionPage route (`:401`), and the route fires it only when the CURRENTLY
 * ROUTED session's overview signature moves
 * (`useSessionBootstrapEffects.ts:32-51`). B's title invalidation is published
 * as a `data-session-changed` frame on B's OWN stream, which the console
 * handles only for the session it is streaming and otherwise throws on
 * (`useSessionChat.ts:574-583`), and nobody is reading B's stream. So the row
 * of the conversation the user walked away from has exactly one way to learn
 * its generated title, and this is the seat that isolates it.
 *
 * The reading is taken inside the held window because that is the only moment
 * it isolates anything: once A's turn ends, A's own terminal moves the routed
 * session's signature, `onSessionChanged` refetches the list, and B's row heals
 * for a reason that has nothing to do with the cadence. `LIST_FRESHNESS_MS`
 * clears two whole cadences, so a red is a rail that did not refresh rather
 * than one that had not refreshed yet.
 *
 * It is JUDGED in the last lines of the test rather than where it is read: with
 * `maxFailures: 1` (`playwright.config.ts:57`), asserting at the point of
 * reading would abort the run before the transcript spine — what this file
 * mostly exists for — had been collected. Reading is not asserting;
 * `expect.soft` appears nowhere in this suite and is not introduced here. The
 * claim itself is not softened: "eventually, once the user does something else"
 * would be the defect.
 *
 * WHAT THE SIBLING OWNS, AND WHAT IT DOES NOT.
 * `the-conversation-rail-follows-work-done-elsewhere.parallel.spec.ts` drives
 * the same cadence from `/agents`, where no SessionPage is mounted, and covers
 * arrival, the row's state and removal. It cannot cover this seat: there the
 * rail has no second producer to be confused with, while here `SessionPage` IS
 * mounted and `onSessionChanged` is wired, so "the row caught up" has two
 * possible causes and only the held window separates them. A reader deciding
 * what to fix should start at the sibling, which fails first and with less
 * scaffolding around it.
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

import { AstraApi, messageText, type MessageRecord } from '../fixtures/astraApi';
import {
  framesForTurn,
  oracleDbPath,
  sessionEvents,
  waitForTurnTerminalProof,
} from '../fixtures/dbOracle';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import {
  armFrameHoldFault,
  clearFrameHoldFault,
  frameHoldConsumed,
  frameHoldFaultPath,
  releaseFrameHoldFault,
} from '../fixtures/frameHold';
import {
  assistantTranscript,
  expectNoDuplicateTextBlocks,
  firstRepeatedWindow,
  normalizeRendered,
} from '../fixtures/renderedTranscript';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import {
  expectComposerEnabled,
  expectPromptDelivered,
  openSessionView,
  startPromptDelivery,
} from '../fixtures/sessionPage';

/**
 * The backend's own release ceiling, restated so this spec can respect it.
 *
 * `_FRAME_HOLD_RELEASE_TIMEOUT_SECONDS = 120.0` (`e2e_faults.py:44`): a hold not
 * released inside it raises in the worker and FAILS A's turn, which would paint
 * this spec red for a reason that has nothing to do with the journey. The safety
 * margin covers the gap between the worker consuming the declaration and this
 * process observing that it did — the countdown starts at the former.
 */
const HOLD_CEILING_MS = 120_000;
const HOLD_SAFETY_MS = 15_000;

// Before the hold starts: A's prompt reaching its first text delta at the barrier.
const FIRST_DELTA_MS = parseTimeoutEnv('ASTRABOX_E2E_SECOND_CONVERSATION_FIRST_DELTA_MS', 60_000);
// Inside the held window. Each is additionally clamped to what the ceiling
// leaves, so the fallbacks are ceilings on ONE step, not a budget to spend.
const OVERLAY_MS = parseTimeoutEnv('ASTRABOX_E2E_SECOND_CONVERSATION_OVERLAY_MS', 20_000);
const DEPARTURE_MS = parseTimeoutEnv('ASTRABOX_E2E_SECOND_CONVERSATION_DEPARTURE_MS', 20_000);
const SECOND_TURN_MS = parseTimeoutEnv('ASTRABOX_E2E_SECOND_CONVERSATION_TURN_MS', 45_000);
const LIST_FRESHNESS_MS = parseTimeoutEnv('ASTRABOX_E2E_SECOND_CONVERSATION_LIST_MS', 20_000);
// After release: A tailing the rest of its answer in, and settling durably.
const RESUME_MS = parseTimeoutEnv('ASTRABOX_E2E_SECOND_CONVERSATION_RESUME_MS', 60_000);
const TERMINAL_PROOF_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TERMINAL_TIMEOUT_MS', 90_000);

// One rendered character proves the first delta reached the browser; the
// barrier, not an amount of prose, is what proves the turn is still owed.
const LIVE_PREFIX_MIN_CHARS = 1;
// A visible suffix after release, measured from rendered text parts only.
const RESUMED_GROWTH_MIN_CHARS = 40;
// Long enough that prose cannot repeat it by accident, short enough to catch a
// replayed prefix. Compared over normalized text (see normalizeRendered).
const DUPLICATE_WINDOW_CHARS = 48;

// A FAILED turn renders its error INTO the transcript as an assistant message,
// so "a reply bubble came back with content in it" is satisfied by exactly the
// failure this spec must not pass on.
const TURN_ERROR = /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

interface StreamChannel {
  id: number;
  sessionId: string;
  afterSeq: number | null;
  status: number;
  openedAt: number;
  closedAt: number;
  closed: boolean;
  readerCancelled: boolean;
  bytes: number;
}

type ChannelWindow = Window & typeof globalThis & {
  __sessionChannels: StreamChannel[];
};

/**
 * Record every session channel the PAGE opens, and when each one ends.
 *
 * The console opens one `follow=session` subscription per conversation
 * (`buildSessionStreamUrl`, `frontend/src/session/utils/chatHelpers.ts:12-19`),
 * and it opens one even for an idle conversation
 * (`shouldOpenSessionSubscription`, `session/turnStream.ts:65-71`). Whether the
 * one belonging to the conversation the user LEFT ends is not observable from
 * the outside: the server keeps its side of a session stream open, so only the
 * browser's own reader can say. Nothing here fabricates a response — the real
 * body is re-streamed byte for byte and only its lifetime is recorded.
 */
async function recordSessionChannels(page: Page): Promise<void> {
  await page.addInitScript(({ sessionsPrefix, streamSuffix }) => {
    const originalFetch = window.fetch.bind(window);
    const channels: StreamChannel[] = [];
    (window as ChannelWindow).__sessionChannels = channels;
    window.fetch = async (input, init) => {
      const request = input instanceof Request ? input : null;
      const method = String(init?.method ?? request?.method ?? 'GET').toUpperCase();
      const url = new URL(request?.url ?? String(input), window.location.href);
      const inStreamPath = url.pathname.startsWith(`${sessionsPrefix}/`)
        && url.pathname.endsWith(streamSuffix);
      const owner = inStreamPath
        ? url.pathname.slice(sessionsPrefix.length + 1, -streamSuffix.length)
        : '';
      if (method !== 'GET' || !owner || url.searchParams.get('follow') !== 'session') {
        return originalFetch(input, init);
      }
      const response = await originalFetch(input, init);
      const rawCursor = url.searchParams.get('after_seq');
      const record: StreamChannel = {
        id: channels.length,
        sessionId: owner,
        afterSeq: rawCursor === null ? null : Number(rawCursor),
        status: response.status,
        openedAt: performance.now(),
        closedAt: 0,
        closed: false,
        readerCancelled: false,
        bytes: 0,
      };
      channels.push(record);
      const close = () => {
        if (record.closed) return;
        record.closed = true;
        record.closedAt = performance.now();
      };
      if (response.status !== 200 || !response.body) {
        close();
        return response;
      }
      const reader = response.body.getReader();
      const body = new ReadableStream<Uint8Array>({
        async pull(controller) {
          try {
            const chunk = await reader.read();
            if (chunk.done) {
              close();
              controller.close();
              return;
            }
            record.bytes += chunk.value.byteLength;
            controller.enqueue(chunk.value);
          } catch (error) {
            close();
            controller.error(error);
          }
        },
        async cancel(reason) {
          close();
          await reader.cancel(reason);
          record.readerCancelled = true;
        },
      });
      return new Response(body, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
    };
  }, { sessionsPrefix: apiPath('/sessions'), streamSuffix: '/ai-stream' });
}

async function sessionChannels(page: Page): Promise<StreamChannel[]> {
  return page.evaluate(() => (window as ChannelWindow).__sessionChannels);
}

function describeChannels(channels: StreamChannel[]): string {
  return channels
    .map((channel) => (
      `#${channel.id} session=${channel.sessionId} after_seq=${channel.afterSeq} `
      + `status=${channel.status} bytes=${channel.bytes} closed=${channel.closed} `
      + `readerCancelled=${channel.readerCancelled}`
    ))
    .join('\n') || '<no session channel was opened at all>';
}

interface DurableOverlayCursor {
  turnId: string;
  frameSeq: number;
}

/**
 * The cursor the console itself reads on mount, taken while the turn is held.
 *
 * Returning null rather than throwing lets the caller name this as an unmet
 * precondition of the journey instead of reporting it as the journey failing.
 */
async function waitForDurableOverlayCursor(
  api: AstraApi,
  sessionId: string,
  timeoutMs: number,
): Promise<DurableOverlayCursor | null> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const overlay = (await api.getMessages(sessionId, 50)).active_turn_overlay;
    const turnId = String(overlay?.turn_id ?? overlay?.resume_cursor?.turn_id ?? '').trim();
    const frameSeq = overlay?.resume_cursor?.frame_seq;
    if (turnId && typeof frameSeq === 'number' && Number.isInteger(frameSeq) && frameSeq >= 0) {
      return { turnId, frameSeq };
    }
    if (Date.now() >= deadline) return null;
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
}

/** Durable engine frames of one turn, reduced to (sequence, vocabulary type). */
function turnFrameTypes(turnId: string): { seq: number; type: string }[] {
  return framesForTurn(turnId).map((frame) => {
    const payload = frame.payload && typeof frame.payload === 'object'
      ? (frame.payload as { type?: unknown })
      : {};
    return { seq: Number(frame.event_seq), type: String(payload.type ?? '').trim() };
  });
}

function journalEventCount(sessionId: string, turnId: string, eventType: string): number {
  return sessionEvents(sessionId).filter((event) => (
    String(event.turn_id ?? '').trim() === turnId
    && String(event.event_type ?? '').trim() === eventType
  )).length;
}

function persistedText(messages: MessageRecord[]): string {
  return messages.map((message) => messageText(message)).join('\n');
}

/**
 * READ whether the sidebar row has caught up. Deliberately not an assertion.
 *
 * The observation can only be made inside the held window — once A's turn ends,
 * A's own title lands, the routed session's overview signature changes and the
 * list refetches, which heals B's row through `onSessionChanged` rather than
 * through the rail's own cadence. The VERDICT belongs at the end, after the rest
 * of the journey has been collected: with `maxFailures: 1`, asserting at the
 * point of reading would abort the run before anything is learned about the
 * transcript spine. This is the same separation
 * `an-expired-oauth-credential-...:41-52` sets out, and for the same reason it
 * does not reach for `expect.soft`, which appears nowhere in this suite.
 */
async function readRowCaughtUp(row: Locator, state: string, budgetMs: number): Promise<boolean> {
  const deadline = Date.now() + budgetMs;
  for (;;) {
    // The state rides on the row's status pill (AstraConsole.tsx:122), not on
    // the row element.
    const pill = row.getByTestId('status-pill').first();
    if ((await pill.getAttribute('data-state').catch(() => null)) === state) return true;
    if (Date.now() >= deadline) return false;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
}

// Teardown that changes state runs on the passing path only, and afterEach hooks
// run in registration order — which is the old `finally` order. The interrupt
// and the fault-file removal are registered BEFORE `trackSessions()` because a
// DELETE against a still-STREAMING session answers SESSION_BUSY: without this
// ordering a failure partway through the held window would leave two orphans.
// Removing the declaration is itself a release (`_wait_for_turn_frame_release`
// returns on FileNotFoundError), so it must never outlive this test.
let heldSessionId = '';
let secondSessionId = '';
let frameHoldPath = '';
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  if (heldSessionId) await api.interruptSession(heldSessionId);
  if (secondSessionId) await api.interruptSession(secondSessionId);
  clearFrameHoldFault(frameHoldPath);
});
const sessions = trackSessions();

test('working in a second conversation keeps the first turn live and the conversation list fresh', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const markerA = `CONV_A_${runId}`;
  const markerB = `CONV_B_${runId}`;

  // Fail fast with the oracle's own descriptive error if the document store is
  // unreachable — the durable half of this spec depends on it.
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });

  // ── Arrange over the API. Two conversations of the same Agent is scene, not
  //    journey: what is under test starts at the first composer. ─────────────
  const agent = await api.defaultAgent();
  const [createdA, createdB] = await Promise.all([
    api.startConversation(agent.agent_id),
    api.startConversation(agent.agent_id),
  ]);
  heldSessionId = createdA.session_id;
  secondSessionId = createdB.session_id;
  sessions.push(heldSessionId, secondSessionId);
  await Promise.all([
    api.waitForSessionReady(heldSessionId),
    api.waitForSessionReady(secondSessionId),
  ]);

  // ── Instrument BEFORE the first navigation. ──────────────────────────────
  // Two oracles, deliberately separate. `page.on('request')` reads the page's
  // own network without touching the body the page is reading, and is the only
  // thing that can say whether the console came back FROM A CURSOR. The init
  // script wraps fetch and is the only thing that can say whether the channel
  // of the conversation the user LEFT actually ended.
  const streamOpens: { sessionId: string; afterSeq: number | null }[] = [];
  let sessionListFetches = 0;
  const sessionListPath = apiPath('/sessions');
  page.on('request', (sent) => {
    if (sent.method() !== 'GET') return;
    const url = new URL(sent.url());
    if (url.pathname === sessionListPath) {
      sessionListFetches += 1;
      return;
    }
    if (!url.pathname.startsWith(`${sessionListPath}/`) || !url.pathname.endsWith('/ai-stream')) return;
    const raw = url.searchParams.get('after_seq');
    const parsed = raw === null ? Number.NaN : Number(raw);
    streamOpens.push({
      sessionId: url.pathname.slice(sessionListPath.length + 1, -'/ai-stream'.length),
      afterSeq: Number.isFinite(parsed) ? parsed : null,
    });
  });
  await recordSessionChannels(page);

  // ── The one entry navigation. Everything after this is SPA clicks. ────────
  await openSessionView(page, heldSessionId);
  await expectComposerEnabled(page);
  // The document identity. A `goto` or a reload anywhere below would reset this
  // and turn the journey into the reload case `live-stream-reconnect` already
  // covers — where a duplicated prefix or a lost tail can hide behind a fresh
  // document. Asserting it unchanged is what makes every later claim a claim
  // about LIVE client state.
  const documentStartedAt = await page.evaluate(() => performance.timeOrigin);

  frameHoldPath = frameHoldFaultPath(heldSessionId);
  armFrameHoldFault(frameHoldPath, heldSessionId, 'text-delta');
  test.info().annotations.push({ type: 'e2e_turn_frame_hold_file', description: frameHoldPath });

  // Established inside the held window and read again after the release, so both
  // live outside the `try`. The definite-assignment assertions are safe because
  // a throw before either is set unwinds through the `finally` and never reaches
  // the post-release half.
  let durableCursor!: DurableOverlayCursor;
  let preReleaseRendered!: string;
  let listFreshness!: {
    caughtUp: boolean;
    state: string;
    rowText: string;
    listFetches: number;
  };

  let holdDeadline = Number.POSITIVE_INFINITY;
  /** What a wait inside the held window may spend without reaching the ceiling. */
  const heldBudget = (wanted: number, what: string): number => {
    const remaining = holdDeadline - Date.now();
    if (remaining <= 0) {
      throw new Error(
        `the ${HOLD_CEILING_MS / 1_000}s frame-hold ceiling was reached before ${what}. `
        + 'The held window is the budget for the whole middle of this journey — B\'s send, '
        + 'the return to A, B\'s terminal, B\'s generated title and the sidebar poll. Tune the '
        + 'ASTRABOX_E2E_SECOND_CONVERSATION_* budgets down, or raise the backend\'s '
        + '_FRAME_HOLD_RELEASE_TIMEOUT_SECONDS (astrabox/testing/e2e_faults.py:44) — do not '
        + 'let the hold expire, which fails A\'s turn for a reason this spec is not about.',
      );
    }
    return Math.min(wanted, remaining);
  };

  try {
    // ── A: one long question, sent from the composer like a person sends it. ─
    // The barrier parks the real worker after its first delta, so the rest of
    // the answer stays owed for a bounded window. Tool-free prose keeps this
    // engine-independent.
    await expectPromptDelivered(await startPromptDelivery(page, heldSessionId, [
      `E2E 第一会话 ${markerA}：请直接用中文回答，不要使用任何工具。`,
      '请写一篇约 500 字的短文，介绍中国大运河的开凿背景、主要河段以及它对南北经济交流的影响。',
      '分成 4 到 6 个自然段，语言通顺，只输出文章正文。',
    ].join('\n')));
    await expect(
      page.getByTestId('user-message').last(),
      'A\'s user bubble must render — proof the turn was dispatched from the page',
    ).toContainText(markerA, { timeout: 30_000 });

    // Anti-vacuity, and the moment the ceiling starts counting. Without this the
    // whole spec could pass on an ordinary settled turn the barrier never
    // touched — including on a deployment where ASTRABOX_E2E_FAULTS is not armed
    // and the hook is not installed at all.
    await expect
      .poll(() => frameHoldConsumed(frameHoldPath, heldSessionId, 'text-delta'), {
        timeout: FIRST_DELTA_MS,
        message: 'A\'s real turn worker must consume the text-delta frame hold',
      })
      .toBe(true);
    // The ceiling is counted from HERE. The backend writes `consumed` in
    // `_consume_turn_frame_hold` at the instant it decides to hold, immediately
    // before starting its own countdown, so this observation trails the real
    // start by one poll interval plus the shared file's write latency — which is
    // what HOLD_SAFETY_MS covers, with an order of magnitude to spare.
    holdDeadline = Date.now() + HOLD_CEILING_MS - HOLD_SAFETY_MS;

    await expect
      .poll(async () => (await assistantTranscript(page)).trim().length, {
        timeout: heldBudget(FIRST_DELTA_MS, 'A rendered its first delta'),
        message: 'the held text delta must render before the user walks away',
      })
      .toBeGreaterThanOrEqual(LIVE_PREFIX_MIN_CHARS);

    // What the user has on screen at the instant they leave. "The answer-so-far
    // came back" is only a claim about content if it is measured against this.
    const preSwitchText = (await assistantTranscript(page)).trim();

    // ── A's durable watermark, read while the turn is genuinely mid-flight. ──
    const cursor = await waitForDurableOverlayCursor(
      api,
      heldSessionId,
      heldBudget(OVERLAY_MS, 'A exposed a durable active-turn cursor'),
    );
    expect(
      cursor,
      'the held turn must expose a durable active_turn_overlay cursor before the user leaves',
    ).not.toBeNull();
    durableCursor = cursor as DurableOverlayCursor;
    expect(
      turnFrameTypes(durableCursor.turnId)
        .filter((frame) => frame.seq <= durableCursor.frameSeq)
        .some((frame) => frame.type === 'finish'),
      'the barrier must hold A before its durable terminal finish — the cut lands mid-turn',
    ).toBe(false);

    // ── LEAVING, THE WAY A USER LEAVES: the sidebar row, inside the SPA. ─────
    const rowB = page.locator(`[data-testid="session-row"][data-session-id="${secondSessionId}"]`);
    await expect(rowB, 'the second conversation must be listed').toBeVisible({
      timeout: heldBudget(DEPARTURE_MS, 'B\'s row was listed in the sidebar'),
    });
    await rowB.getByRole('link').click();
    await page.waitForURL(
      (url) => url.pathname.endsWith(`/sessions/${secondSessionId}`),
      { timeout: heldBudget(DEPARTURE_MS, 'the click on B\'s row landed') },
    );
    await expect(page.getByTestId('run-view')).toBeVisible();

    // Standing in B, before typing anything: this is B's conversation, not a
    // repaint of A's.
    //
    // `expectComposerEnabled` is not used inside the held window on purpose. Its
    // fallback is 180s (sessionPage.ts:6-9), sized for a cold sandbox, and a
    // single 180s wait inside a 120s ceiling would expire the barrier and fail
    // A's turn instead of reporting whatever it was really waiting for. B is
    // already READY here, so the clamped wait costs nothing it should not.
    const composer = page.getByTestId('composer-prompt');
    await expect(composer, 'B\'s composer must be usable').toBeEnabled({
      timeout: heldBudget(DEPARTURE_MS, 'B\'s composer became usable'),
    });
    await expect(
      composer,
      'B\'s composer must be empty — A\'s prompt does not follow the user across',
    ).toHaveValue('');
    await expect(
      page.getByTestId('user-message').filter({ hasText: markerA }),
      'A\'s message must not bleed into B\'s transcript',
    ).toHaveCount(0);
    await expect(
      page.getByTestId('assistant-message').filter({ hasText: markerA }),
      'A\'s reply must not bleed into B\'s transcript',
    ).toHaveCount(0);

    // And the channel the user walked away from is not still being read. The
    // server holds its side of a session stream open, so an abandoned reader is
    // invisible from outside the browser — and one leaked per conversation
    // switch is exactly the kind of debt that later reads as "the product feels
    // slow". This is the weakest-stated contract in the spec (one channel per
    // conversation is implied by `follow=session`, not written down), so it is
    // bounded rather than instantaneous: the handoff may overlap, it may not
    // persist.
    await expect
      .poll(async () => (await sessionChannels(page))
        .filter((channel) => channel.sessionId === heldSessionId && !channel.closed).length, {
        timeout: heldBudget(DEPARTURE_MS, 'A\'s stream channel closed behind the user'),
        message: 'the departed conversation\'s session channel must not stay open in the browser',
      })
      .toBe(0);
    // One read, two claims: a table sampled twice could describe a different
    // instant than the one being judged.
    const channelsInB = await sessionChannels(page);
    const openInB = channelsInB.filter((channel) => !channel.closed);
    expect(
      openInB.length,
      `standing in B, B must own a live session channel:\n${describeChannels(channelsInB)}`,
    ).toBeGreaterThan(0);
    expect(
      openInB.filter((channel) => channel.sessionId !== secondSessionId).map((channel) => channel.sessionId),
      `standing in B, no other conversation may hold an open channel:\n${describeChannels(channelsInB)}`,
    ).toEqual([]);

    // ── B: one short, topic-bearing question, so B settles fast and its
    //    first-turn title actually generates. ──────────────────────────────────
    await expectPromptDelivered(await startPromptDelivery(page, secondSessionId, [
      `E2E 第二会话 ${markerB}：请直接用中文回答，不要使用任何工具。`,
      '请用两三句话说明 PostgreSQL 的 B-tree 索引为什么能加速范围查询。',
    ].join('\n')));
    await expect(
      page.getByTestId('user-message').filter({ hasText: markerB }),
      'B\'s own message must render in B',
    ).toHaveCount(1, { timeout: heldBudget(DEPARTURE_MS, 'B rendered its own message') });

    // ── TWO TURNS IN FLIGHT AT ONCE, from the browser's seat. ────────────────
    // The browser-side statement of what tests/e2e/test_shared_box_cohabitation.py:152
    // establishes over the API. B's acceptance is polled because the turn id
    // appears a moment after the input is taken; A's is read at that same instant
    // and must still be the turn the barrier is holding.
    await expect
      .poll(async () => String((await api.getSession(secondSessionId)).current_turn_id || '').trim(), {
        timeout: heldBudget(SECOND_TURN_MS, 'B\'s turn started'),
        message: 'B must take a turn of its own while A is still answering',
      })
      .not.toEqual('');
    expect(
      String((await api.getSession(heldSessionId)).current_turn_id || '').trim(),
      'A must still be on the held turn at the moment B starts — that overlap IS the journey',
    ).toEqual(durableCursor.turnId);

    // ── RETURNING TO A, by the same SPA click, while B keeps running. ────────
    const rowA = page.locator(`[data-testid="session-row"][data-session-id="${heldSessionId}"]`);
    const streamOpensBeforeReturn = streamOpens.length;
    await rowA.getByRole('link').click();
    await page.waitForURL(
      (url) => url.pathname.endsWith(`/sessions/${heldSessionId}`),
      { timeout: heldBudget(DEPARTURE_MS, 'the click back to A landed') },
    );
    await expect(page.getByTestId('run-view')).toBeVisible();
    expect(
      await page.evaluate(() => performance.timeOrigin),
      'the whole journey must happen inside ONE document — a reload would launder a lost tail',
    ).toBe(documentStartedAt);

    // The prefix is back, exactly, and exactly once. The worker is held, so
    // nothing has been added: anything other than equality here is the seam
    // repainting, truncating or replaying what the user already read.
    await expect(
      page.getByTestId('user-message').filter({ hasText: markerA }),
      'A\'s own message must come back exactly once',
    ).toHaveCount(1, { timeout: heldBudget(DEPARTURE_MS, 'A\'s message came back') });
    // Existence FIRST, then content: Playwright passes `not.toBeEmpty()` on an
    // element it never found, so the check would be green on exactly the failure
    // it exists for — a transcript that came back with no answer in it.
    const rehydratedReply = page.getByTestId('assistant-message').last();
    await expect(
      rehydratedReply,
      'returning to A must restore the answer-so-far, not an empty transcript',
    ).toBeVisible({ timeout: heldBudget(DEPARTURE_MS, 'A\'s reply came back') });
    await expect(rehydratedReply).not.toBeEmpty();
    await expect(rehydratedReply).not.toContainText(TURN_ERROR);
    await expect
      .poll(async () => (await assistantTranscript(page)).trim(), {
        timeout: heldBudget(DEPARTURE_MS, 'A\'s displayed prefix was restored'),
        message: 'returning to A must restore the displayed reply prefix unchanged',
      })
      .toBe(preSwitchText);
    await expect(
      page.getByTestId('user-message').filter({ hasText: markerB }),
      'B\'s message must not bleed into A\'s transcript',
    ).toHaveCount(0);

    // And it came back FROM THE CURSOR. `after_seq=0`, or an open with no
    // cursor at all, IS a restart from the head — which would replay the whole
    // turn on top of the prefix just painted. The floor is the watermark taken
    // mid-turn, because the console reads that same overlay on mount
    // (`bootstrapStreamCursor` ← `active_turn_overlay.resume_cursor`) and it
    // only advances within a turn.
    await expect
      .poll(
        () => streamOpens.slice(streamOpensBeforeReturn)
          .filter((open) => open.sessionId === heldSessionId).length,
        {
          timeout: heldBudget(DEPARTURE_MS, 'A\'s channel was reopened'),
          message: 'returning to A must reopen A\'s session stream',
        },
      )
      .toBeGreaterThan(0);
    const returnCursors = streamOpens
      .slice(streamOpensBeforeReturn)
      .filter((open) => open.sessionId === heldSessionId)
      .map((open) => open.afterSeq)
      .filter((seq): seq is number => seq !== null && seq >= 0);
    expect(
      returnCursors.length,
      'the return must carry a durable cursor (after_seq), not open A\'s channel cursorless',
    ).toBeGreaterThan(0);
    const returnCursor = Math.max(...returnCursors);
    expect(
      returnCursor,
      'returning to A must resume AT OR PAST the watermark its turn had when the user left, '
      + 'not restart the stream from the head',
    ).toBeGreaterThanOrEqual(durableCursor.frameSeq);
    test.info().annotations.push({
      type: 'e2e_return_after_seq',
      description: String(returnCursor),
    });

    // ── THE LIST, WHILE THE USER SITS IN A. ─────────────────────────────────
    // B finishes and earns its generated title with nobody watching it. Both
    // waits are preconditions of the freshness verdict, so each fails with its
    // own message rather than folding into it — "the row is stale" and "B never
    // got a title" send a reader to different places.
    await api.waitForSession(
      secondSessionId,
      (session) => String(session.last_turn_status || '') === 'COMPLETED',
      heldBudget(SECOND_TURN_MS, 'B\'s turn completed'),
    );
    // THE FRESHNESS OBSERVATION. No reload, no click, and A's turn has not
    // ended: the only thing that changed is a conversation the user is not
    // looking at. Read here because this is the only moment it means anything;
    // judged at the very end. See the header for the traced chain.
    const listFetchesBefore = sessionListFetches;
    listFreshness = {
      caughtUp: await readRowCaughtUp(
        rowB,
        'READY',
        heldBudget(LIST_FRESHNESS_MS, 'the list was given its chance to catch up with B'),
      ),
      state: 'READY',
      rowText: (await rowB.innerText().catch(() => '<B\'s row is not rendered>'))
        .replace(/\s+/g, ' ')
        .trim(),
      listFetches: sessionListFetches - listFetchesBefore,
    };
    test.info().annotations.push({
      type: 'e2e_left_row_freshness',
      description: JSON.stringify(listFreshness),
    });

    preReleaseRendered = normalizeRendered(await assistantTranscript(page));
  } finally {
    // Unconditional, and before the ceiling: a failed assertion above must fail
    // as itself, not as a turn the barrier killed 120s after it was armed.
    releaseFrameHoldFault(frameHoldPath);
  }

  // ── AND IT KEEPS ARRIVING, WITHOUT THE USER DOING ANYTHING. ───────────────
  // No reload, no click, no second send: the released worker's remaining frames
  // reach the page over the channel it reopened on return.
  await expect
    .poll(async () => normalizeRendered(await assistantTranscript(page)).length, {
      timeout: RESUME_MS,
      message: 'the resumed stream must deliver the rest of A\'s answer to the page the user came back to',
    })
    .toBeGreaterThan(preReleaseRendered.length + RESUMED_GROWTH_MIN_CHARS);
  await expect(
    page.getByTestId('run-view').getByTestId('status-pill').first(),
    'A\'s header must settle once the resumed turn reaches its terminal frame',
  ).toHaveAttribute('data-pulse', 'false', { timeout: RESUME_MS });

  const settledReply = page.getByTestId('assistant-message').last();
  await expect(settledReply, 'A\'s resumed turn should have rendered assistant content').not.toBeEmpty();
  await expect(settledReply).not.toContainText(TURN_ERROR);

  // Not rendered twice. This is the seam: the page painted a durable prefix on
  // return and then tailed from a cursor, so anything the resume replayed over
  // that prefix shows up here as repeated prose.
  expect(
    firstRepeatedWindow(normalizeRendered(await assistantTranscript(page)), DUPLICATE_WINDOW_CHARS),
    'leaving and returning must not render the same stretch of A\'s answer twice',
  ).toBeNull();

  // ── Durable half. A turn the live worker completed and one a recovery path
  //    rebuilt render the same finished answer; that difference has no pixel,
  //    and it is the difference between "the user's conversation survived them
  //    walking away" and "the platform rebuilt it afterwards". ───────────────
  await waitForTurnTerminalProof(heldSessionId, durableCursor.turnId, 'COMPLETED', TERMINAL_PROOF_MS);
  const finalTypes = turnFrameTypes(durableCursor.turnId).map((frame) => frame.type);
  expect(
    finalTypes.filter((type) => type === 'finish'),
    'A\'s turn must commit exactly one durable finish frame',
  ).toHaveLength(1);
  expect(
    journalEventCount(heldSessionId, durableCursor.turnId, 'turn.completed'),
    'A\'s turn must write exactly one normal completed terminal event',
  ).toBe(1);
  expect(
    journalEventCount(heldSessionId, durableCursor.turnId, 'turn.recovered'),
    'working in another conversation must not push A\'s turn through a recovery path',
  ).toBe(0);

  // ── Durable isolation, both directions. The rendered transcripts already
  //    said this; these are the rows that outlive the tab. ──────────────────
  const finalA = await api.getMessages(heldSessionId, 50);
  const finalB = await api.getMessages(secondSessionId, 50);
  const textA = persistedText(finalA.messages || []);
  const textB = persistedText(finalB.messages || []);
  expect(textA, 'A\'s persisted transcript must carry its own message').toContain(markerA);
  expect(textA, 'B\'s message must not be persisted into A').not.toContain(markerB);
  expect(textB, 'B\'s persisted transcript must carry its own message').toContain(markerB);
  expect(textB, 'A\'s message must not be persisted into B').not.toContain(markerA);

  const assistantA = (finalA.messages || []).filter((message) => message.role === 'assistant');
  const lastAssistantA = assistantA[assistantA.length - 1];
  expect(lastAssistantA, 'A must persist a final assistant message').toBeTruthy();
  expectNoDuplicateTextBlocks(lastAssistantA as MessageRecord, 'A\'s settled reply');

  const assistantB = (finalB.messages || []).filter((message) => message.role === 'assistant');
  const lastAssistantB = assistantB[assistantB.length - 1];
  expect(lastAssistantB, 'B must persist a final assistant message').toBeTruthy();
  expectNoDuplicateTextBlocks(lastAssistantB as MessageRecord, 'B\'s settled reply');

  // ── THE VERDICT ON THE LIST, on the reading taken while A was still held. ──
  // It is judged last and not where it was read, for the reason set out on
  // readRowCaughtUp: the observation is only meaningful inside the held window,
  // and with `maxFailures: 1` asserting it there would abort the run before any
  // of the evidence above was collected. The claim itself is not softened —
  // "eventually, once the user does something else" would be the defect.
  expect(
    listFreshness,
    'the sidebar row for the conversation the user LEFT must read READY once its turn is '
    + 'done, while the user sits in another conversation whose turn has not ended — no '
    + 'reload, no click. '
    + `It was given ${LIST_FRESHNESS_MS}ms and read ${JSON.stringify(listFreshness.rowText)}; `
    + `the list was fetched ${listFreshness.listFetches} time(s) during that wait. A count of 0 `
    + 'means the tab never re-read the list at all, so the rail\'s cadence is gone — the '
    + 'RAIL_REFRESH_INTERVAL_MS on frontend/src/App.tsx:52, passed at :122, is the only '
    + 'producer that reaches a row the reader is not standing on. A non-zero count with a stale '
    + 'row means the rail asked and the answer did not carry B\'s state. B\'s invalidation is '
    + 'published on B\'s OWN stream (useSessionChat.ts:574-583), which nobody is reading, and '
    + 'refreshOverview (App.tsx:154-156) fires from the SessionPage route only when the '
    + 'CURRENTLY ROUTED session\'s signature moves (useSessionBootstrapEffects.ts:32-51) — A is '
    + 'held, so it did not. Fix the refresh, do not relax this to "eventually".',
  ).toMatchObject({ caughtUp: true });
});
