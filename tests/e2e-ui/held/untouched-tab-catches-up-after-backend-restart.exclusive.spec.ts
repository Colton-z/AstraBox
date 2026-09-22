/**
 * E2E: a tab nobody touches catches up on its own after the backend restarts.
 *
 * THE JOURNEY. A person is sitting in front of an open conversation with a turn
 * in flight. The backend container goes down under them — a deploy, an OOM kill,
 * a rolling restart — and comes back. They touch NOTHING: no reload, no click,
 * no tab switch, not even a window focus. The promise is that the page they are
 * already looking at heals itself: it reopens its own live channel once the
 * server answers again, the same turn settles on screen, the header stops
 * pretending to work, the answer reads as one answer rather than its prefix
 * twice, no banner asks them to intervene, and the composer comes back.
 *
 * WHY THIS IS NOT THE SIBLING SPEC. `server-restart-reattaches-running-sandbox-
 * turn.exclusive.spec.ts` covers the same outage and then calls `page.reload()`.
 * That reload is the whole difference: it throws away every piece of client
 * state and re-derives the screen from durable records, which is a claim about
 * the BACKEND's rehydrate path. It cannot see a front end that reconnects to
 * nothing, because it never asks the live page to do anything — a tab that
 * stopped reopening its stream forever passes that spec on the strength of its
 * fresh load. The gap is the product defect this file exists for: the reload is
 * the user paying, with an action, for staleness the page should have cleared
 * by itself. Everything below therefore happens on ONE navigation, taken before
 * the outage and never repeated.
 *
 * WHY BOTH HALVES ARE ASSERTED. The console's session-detail poll survives the
 * outage on its own (`useSessionLifecycle.refresh` catches its own errors and
 * reports `detailStale`, and `shouldPollSessionDetail` keeps polling on exactly
 * that), and the history hooks re-fetch on their own interval. That machinery
 * alone can drive the pill to rest and paint the finished reply while the live
 * SSE channel stays dead — a page that looks healed and streams the NEXT message
 * into nothing. So the screen assertions (1-5) and the channel assertions (6-7)
 * are both taken, and a run where the first set passes and the second fails is
 * not a flaky spec: it is that finding, which is why the recorded counts are
 * attached as evidence rather than left in a variable.
 *
 * HOW TIME ENTERS. It does not have to be simulated. The outage is real
 * (`restartServerContainer` runs `docker restart` on the Node side and returns
 * only once the container is healthy AND /healthz answers), and a restarted
 * container necessarily severs every open SSE, so the cut needs no separate
 * proof and no injected fault. What IS ordered on purpose is kernel-before-
 * screen: the durable terminal proof is awaited first, and only then does the
 * untouched tab get a short window to agree. A settle assertion taken before the
 * kernel finished would pass in the middle of the turn; taken after, the short
 * window is the staleness bar itself — the backend was done at T, so a tab
 * nobody touched had to agree by T + SETTLE_MS.
 *
 * ANTI-VACUITY. If the model finishes its essay before the restart lands, the
 * journey never happened. The gate read microseconds before `docker restart`
 * (snapshot on this turn, conversation_state PROCESSING or STREAMING, a fresh
 * worker heartbeat) turns that from a silent false green into a loud red. It
 * does not close the window completely — the model can still finish DURING the
 * restart — which is why nothing is asserted about the turn being live at the
 * instant the server returns, and why no growth of the answer after the cut is
 * required. Both would be coin flips.
 *
 * BUDGET, stated so a later edit cannot quietly push it past the 180s the runner
 * kills at: setup and first paint ~30s, the restart 15-40s, recovery and settle
 * 30-60s. It fits only because the turn is prose rather than a sleep — never
 * give this spec a long Bash. (The sibling's RECOVERY_TIMEOUT_MS of 360_000 is a
 * ceiling it can never reach; it is not headroom and is not copied here.)
 *
 * HOW TO READ A SETTLE_MS RED, because one product mechanism sets its floor. If
 * the dying container leaves the socket half-open rather than resetting it, the
 * page learns nothing until `withStreamLiveness` ends the body on silence, and
 * that limit is 45s (`session/streamLiveness.ts`, STREAM_SILENCE_LIMIT_MS). The
 * window here is measured from the KERNEL's terminal, not from the cut, so a
 * kernel that settles quickly after the server returns can leave the tab still
 * inside that silence window. A red on the settle assertions with the channel
 * assertions passing shortly after is that interaction and not a rendering bug —
 * report it as the 45s staleness it is, and raise SETTLE_MS (it is env-tunable)
 * only with that measurement in hand. Raising it on a hunch trades away the only
 * bar this file sets.
 *
 * WHAT WOULD MAKE THIS SPEC LIE. (a) Any stray page interaction — a reload, a
 * goto, a bringToFront, a click, even an inadvertent focus — runs the console's
 * visibilitychange handler straight into `ensureSessionStream` and turns a dead
 * page green; between the send and the final composer read, every call here is a
 * Node-side oracle or a passive poll. (b) Counting stream REQUESTS instead of
 * successful RESPONSES would pass on one doomed socket opened while the server
 * was still down, which is exactly the shape of the failure. (c) An unpinned
 * locale would make the two absence assertions match nothing and pass for free.
 * (d) Leaving this file out of the contract's serial list would restart the
 * shared server under four other workers.
 *
 * The test is exclusive, and belongs in `playwright.exclusive.serial_files`:
 * restarting the server disrupts every session on the host.
 */
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import {
  oracleDbPath,
  snapshotDoc,
  turnSnapshot,
  waitForTurnTerminalProof,
} from '../fixtures/dbOracle';
import { absoluteBaseUrl, appPath, parseTimeoutEnv } from '../fixtures/env';
import {
  assistantTranscript,
  firstRepeatedWindow,
  normalizeRendered,
} from '../fixtures/renderedTranscript';
import { restartServerContainer } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

// How long the first assistant prose gets to paint. This is the model's first
// token plus the sandbox already being READY, so it is short.
const FIRST_TEXT_MS = parseTimeoutEnv('ASTRABOX_E2E_RESTART_CATCHUP_FIRST_TEXT_MS', 60_000);
// How long the interrupted turn gets to reach a durable terminal proof after the
// new instance comes up. The turn is prose on a live sandbox, not a sleep.
const RECOVERY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_RESTART_CATCHUP_RECOVERY_MS', 90_000);
// Taken only AFTER the kernel reports the turn terminal, so this is the screen's
// lag behind a settled backend on a page nobody touched — short on purpose. This
// constant IS the staleness bar; widening it weakens the only claim here.
const SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_RESTART_CATCHUP_SETTLE_MS', 45_000);
// How stale the driving worker's heartbeat may be and still count as "a live
// worker owns this turn". Generous: the point is that SOMEONE is driving it.
const WORKER_HEARTBEAT_FRESH_MS = 120_000;

// Long enough that prose cannot repeat it by accident, short enough to catch a
// replayed prefix. Compared over normalized text (see normalizeRendered).
const DUPLICATE_WINDOW_CHARS = 48;
// The pre-restart prose must be long enough to cut a window out of its MIDDLE,
// or the duplicate check below would compare nothing and pass for free.
const PRE_RESTART_MIN_CHARS = DUPLICATE_WINDOW_CHARS * 2;

// A FAILED turn renders its error INTO the transcript as an assistant message,
// so "there is a reply bubble" is satisfied by exactly the failure under test.
const TURN_ERROR = /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

// The two ways the console asks the user to do the healing itself. Both are
// asserted ABSENT, and only at the END: `banner.backend_unavailable` DURING the
// outage is a promise the product deliberately makes ("retrying automatically"),
// so the bar is that it is gone once the backend is done, not that it never
// appeared. `retry.budget_exhausted_summary` is the auto-resume budget giving up
// — the page stops reopening and waits for a click, which is the seed family
// exactly. Both strings are pinned to en-US by the locale seeding below.
const BUDGET_EXHAUSTED_EN = 'Auto-resume reached its limit';
const BACKEND_UNAVAILABLE_EN = 'The backend is temporarily unavailable';

// One GET the BROWSER made on the session stream, and one response it got back.
// Requests say the page TRIED; responses say a stream was actually ESTABLISHED.
// "Opened one doomed socket while the server was down and stayed dead" and
// "recovered" are indistinguishable from requests alone.
interface StreamOpen { at: number; afterSeq: number | null }
interface StreamResponse { at: number; ok: boolean; status: number }

// The console reads its language from localStorage first and navigator second,
// and the two absence assertions above are English strings. An unpinned runner
// locale decides which spelling the product uses, and a spec that accepted both
// would also accept a third nobody wrote it against.
test.use({ locale: 'en-US' });

// Both halves of the verdict are kept legible on a red: assertion 6 failing
// while 1-5 passed is the "healed by polling, channel still dead" finding, and
// it is unreadable without the counts.
const evidence: Record<string, unknown> = {};
let sessionId = '';

test.afterEach(async (_args, info) => {
  if (!['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  await info.attach('untouched-tab-restart-catchup-evidence', {
    body: JSON.stringify({ session_id: sessionId, ...evidence }, null, 2),
    contentType: 'application/json',
  });
});

// Teardown that changes state runs on the passing path only, and hook
// registration order is the old `finally` order: the interrupt is registered
// BEFORE `trackSessions()` because a DELETE against a still-STREAMING session
// answers SESSION_BUSY and leaves the orphan this ordering exists to avoid. On
// the passing path the turn has settled and the interrupt is a harmless no-op.
onPassOnly(async ({ request }) => {
  if (sessionId) await new AstraApi(request).interruptSession(sessionId);
});
const sessions = trackSessions();

test('a tab left open mid-turn catches up by itself after the backend restarts', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const marker = `E2E_RESTART_CATCHUP_${runId}`;

  // Fail fast (with the descriptive oracle error) if the document store is not
  // reachable — the kernel half of this spec depends on it.
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });

  // ARRANGE through the API: a user picks a conversation that exists rather than
  // authoring one to have a chat. No Agent is created, so no Agent teardown is
  // owed. The engine is whichever profile the matrix selected — the prompt below
  // forbids tools and asks only for prose, so nothing here is engine-specific.
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  sessionId = created.session_id;
  sessions.push(sessionId);

  // Every GET the BROWSER makes on the session stream, both halves, registered
  // BEFORE the first navigation so the console's first channel open is captured
  // too. `page.on()` reads the page's own network without touching the SSE body
  // the page is reading — the zero-perturbation oracle, and the only one that
  // says whether this tab came back by itself.
  const streamOpens: StreamOpen[] = [];
  const streamResponses: StreamResponse[] = [];
  page.on('request', (req) => {
    if (req.method() !== 'GET' || !req.url().includes('/ai-stream')) return;
    const raw = new URL(req.url()).searchParams.get('after_seq');
    const parsed = raw === null ? Number.NaN : Number(raw);
    streamOpens.push({ at: Date.now(), afterSeq: Number.isFinite(parsed) ? parsed : null });
  });
  page.on('response', (resp) => {
    if (resp.request().method() !== 'GET' || !resp.url().includes('/ai-stream')) return;
    streamResponses.push({ at: Date.now(), ok: resp.ok(), status: resp.status() });
  });

  await api.waitForSessionReady(sessionId);

  // Pin the console language before the FIRST navigation, or the two absence
  // assertions at the end read whatever the runner's locale happens to be.
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });

  // The ONLY navigation in this test. Everything after it happens on this tab.
  await page.goto(appPath(`/sessions/${sessionId}`));
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });

  // ── ACT through the page: the user types and sends. ──────────────────────
  // Driving it through the composer keeps THIS browser holding the stream —
  // that browser is the tab nobody touches once the server dies, and it is the
  // state a real redeploy interrupts. `.fill()` sets the value without key
  // events, so the multi-line prompt is not submitted early by an Enter newline.
  const prompt = [
    `Reference ${marker}. Please answer in prose only and do not use any tools.`,
    'Write roughly 500 words explaining, for a general reader, how a public library',
    'decides which books to keep on its shelves and which to move into storage:',
    'the space constraint, how borrowing history is read, what happens to donations,',
    'and why a rarely borrowed book is sometimes kept anyway.',
    'Use four to six paragraphs and output only the article itself.',
  ].join('\n');
  const composer = page.getByTestId('composer-prompt');
  await expect(composer, 'the composer must be enabled before sending').toBeEnabled({
    timeout: 60_000,
  });
  await composer.fill(prompt);
  await page.getByTestId('composer-submit').click();
  await expect(
    page.getByTestId('user-message').last(),
    'the user bubble should render — proof the turn was dispatched from the page',
  ).toContainText(marker, { timeout: 30_000 });

  // The run reads as live to the user: the send button became a stop button. Not
  // a race with the model — `isSubmitted` flips inside the submit handler,
  // before the send receipt (let alone a frame) can come back.
  await expect(page.getByTestId('run-composer-stop')).toBeVisible({ timeout: 30_000 });

  // ── The prefix this tab actually has on screen. ──────────────────────────
  // Sampled here rather than at the send: "the answer came back whole" is only a
  // claim about what the tab kept if it is measured against what the tab really
  // had. Long enough to cut a middle window out of, so the duplicate check below
  // is comparing something.
  await expect
    .poll(async () => normalizeRendered(await assistantTranscript(page)).length, {
      timeout: FIRST_TEXT_MS,
      message: 'the reply must start painting before the backend is pulled out from under it',
    })
    .toBeGreaterThanOrEqual(PRE_RESTART_MIN_CHARS);
  const preRestartRendered = normalizeRendered(await assistantTranscript(page));
  const windowStart = Math.floor((preRestartRendered.length - DUPLICATE_WINDOW_CHARS) / 2);
  const preRestartWindow = preRestartRendered.slice(
    windowStart,
    windowStart + DUPLICATE_WINDOW_CHARS,
  );
  evidence.pre_restart_rendered_chars = preRestartRendered.length;

  // ── ANTI-VACUITY GATE, immediately before pulling the plug. ──────────────
  // No pixel: the screen shows that something is running, never WHO is driving
  // it. The snapshot's own turn pointer plus a fresh worker heartbeat is the
  // surviving evidence that a live worker owns THIS turn — and if the model has
  // already finished, this is where the run goes red instead of passing on a
  // journey that never happened.
  await expect
    .poll(() => String(snapshotDoc(sessionId)?.current_turn_id ?? '').trim(), {
      timeout: 30_000,
      message: 'the durable snapshot must name the turn before the restart',
    })
    .not.toBe('');
  const turnId = String(snapshotDoc(sessionId)?.current_turn_id ?? '').trim();
  evidence.turn_id = turnId;
  const before = turnSnapshot(sessionId, turnId);
  expect(
    before,
    `the snapshot must still be on this turn before restart; before=${JSON.stringify(before)}`,
  ).not.toBeNull();
  expect(
    ['PROCESSING', 'STREAMING'].includes(String(before?.conversation_state ?? '')),
    `conversation must be actively running before restart; before=${JSON.stringify(before)}`,
  ).toBe(true);
  const heartbeatAt = Date.parse(String(before?.worker_heartbeat_at ?? ''));
  expect(
    Number.isFinite(heartbeatAt),
    `a worker must be driving the turn before restart; before=${JSON.stringify(before)}`,
  ).toBe(true);
  expect(
    Date.now() - heartbeatAt,
    'the driving worker heartbeat must be fresh before restart',
  ).toBeLessThan(WORKER_HEARTBEAT_FRESH_MS);

  // ── The outage, under an open browser. ───────────────────────────────────
  // `restartServerContainer` runs on the Node side, so the page is never
  // touched. The timestamp is taken BEFORE the call: the page owns exactly one
  // session subscription at a time and already has a live one here, so it cannot
  // open another before the container goes down — every GET counted after this
  // instant is the untouched tab reaching for a server that left.
  const restartRequestedAt = Date.now();
  const streamOpensBefore = streamOpens.length;
  const streamResponsesBefore = streamResponses.filter((entry) => entry.ok).length;
  evidence.stream_opens_before = streamOpensBefore;
  evidence.stream_responses_ok_before = streamResponsesBefore;
  await restartServerContainer(absoluteBaseUrl());
  evidence.restart_seconds = (Date.now() - restartRequestedAt) / 1_000;

  // ── FROM HERE THE PAGE IS NOT TOUCHED. ──────────────────────────────────
  // No reload, no goto, no bringToFront, no click, no keyboard event: each of
  // those runs the console's visibilitychange/mount path into
  // `ensureSessionStream` and heals the exact defect under test. Until the
  // composer read at the very end, every call below is a Node-side oracle or a
  // passive poll.

  // Kernel first. The console renders no turn id, so THIS turn settling — rather
  // than some later turn, or a repaint over an unresolved one — is only bindable
  // in the durable record.
  await waitForTurnTerminalProof(sessionId, turnId, 'COMPLETED', RECOVERY_TIMEOUT_MS);
  expect(
    String(snapshotDoc(sessionId)?.last_turn_status ?? ''),
    'the interrupted turn should complete once the new instance takes it over',
  ).toBe('COMPLETED');
  evidence.kernel_terminal_at_ms_after_restart = Date.now() - restartRequestedAt;

  // ── Screen second, and quickly. ──────────────────────────────────────────
  // Scoped to run-view ON PURPOSE. The app shell's sidebar renders a
  // `status-pill` for EVERY conversation in the list and precedes the route
  // outlet in the DOM, so a bare `status-pill.first()` binds to some OTHER
  // conversation's row, whose `data-pulse` is false for free — the assertion
  // would pass on a header still spinning forever, which is the failure this
  // line exists for.
  await expect(
    page.getByTestId('run-view').getByTestId('status-pill').first(),
    'the untouched tab must stop pretending to work once the recovered turn ended',
  ).toHaveAttribute('data-pulse', 'false', { timeout: SETTLE_MS });

  // A reply, not a rendered failure. Existence FIRST, then content: a negated
  // Playwright matcher passes on an element that was never found, so
  // `not.toBeEmpty()` alone would be green on a transcript with no answer in it.
  const reply = page.getByTestId('assistant-message').last();
  await expect(
    reply,
    'the untouched tab must still be showing the answer, not an empty transcript',
  ).toBeVisible({ timeout: SETTLE_MS });
  await expect(reply).not.toBeEmpty();
  await expect(reply).not.toContainText(TURN_ERROR);

  // ── It reads as ONE answer. ──────────────────────────────────────────────
  // Two halves, because either alone is passable by the other's failure. The
  // stretch the user watched arrive must still be there (a cursorless reopen
  // that rebuilt the transcript from the head, or a recovery that regenerated
  // the reply, loses it) and it must be there ONCE (a reopen that replayed from
  // a stale cursor shows the same sentences twice).
  const settled = normalizeRendered(await assistantTranscript(page));
  evidence.settled_rendered_chars = settled.length;
  expect(
    settled.includes(preRestartWindow),
    `the prose this tab had already painted must survive the restart; `
      + `window=${JSON.stringify(preRestartWindow)}`,
  ).toBe(true);
  expect(
    firstRepeatedWindow(settled, DUPLICATE_WINDOW_CHARS),
    'catching up must not render the same stretch of the answer twice',
  ).toBeNull();

  // ── The page never ended up asking the user for help. ────────────────────
  // Taken only at the END. A transient "the backend is temporarily unavailable,
  // retrying automatically" DURING the outage is a promise the product makes on
  // purpose; still being there once the backend is done is the promise broken.
  // "Auto-resume reached its limit" is the harder failure: the page has stopped
  // reopening its stream and is waiting for a click it should not need.
  await expect(
    page.getByText(BUDGET_EXHAUSTED_EN),
    'a tab that healed itself must not be sitting on the auto-resume budget banner',
  ).toHaveCount(0);
  await expect(
    page.getByText(BACKEND_UNAVAILABLE_EN),
    'the backend-unavailable banner must be gone once the backend is back',
  ).toHaveCount(0);

  // ── The mechanism half: this tab reopened its OWN live channel. ──────────
  // Counted on RESPONSES, not requests. A page that opened one doomed socket
  // while the server was down and then gave up produces the same request count
  // as a page that recovered — and with CLEAN_RESUME_ATTEMPTS=1 against
  // DROPPED_RESUME_ATTEMPTS=100, "gave up after one" is the realistic shape of
  // the failure, not a contrived one.
  const establishedAfterRestart = () =>
    streamResponses.filter((entry) => entry.ok && entry.at >= restartRequestedAt);
  await expect
    .poll(() => establishedAfterRestart().length, {
      timeout: SETTLE_MS,
      message:
        'the untouched tab must re-establish its own live channel after the backend came back — '
        + 'a transcript healed by the detail poll while the stream stays dead means the NEXT '
        + 'message streams into nothing',
    })
    .toBeGreaterThan(0);

  const resumedOpens = streamOpens.filter((entry) => entry.at >= restartRequestedAt);
  evidence.stream_opens_after = resumedOpens.length;
  evidence.stream_responses_ok_after = establishedAfterRestart().length;
  evidence.resumed_after_seq = resumedOpens.map((entry) => entry.afterSeq);

  // And that reopen carried a cursor rather than restarting from the head.
  // Deliberately NOT compared to a specific durable watermark: this spec arms no
  // frame-hold fault, so it has no pinned cursor to compare to. The correctness
  // half a wrong cursor would break is carried by the duplicate check above.
  expect(
    resumedOpens.some((entry) => typeof entry.afterSeq === 'number'),
    `the reopened channel must resume from a cursor, not from the head; `
      + `after_seq=${JSON.stringify(resumedOpens.map((entry) => entry.afterSeq))}`,
  ).toBe(true);

  // ── LAST: the turn lock released, in the only form a user can see it. ────
  // Taken after every no-touch assertion, and the submit button is never
  // clicked: sending dispatches `send-started`, which would reopen the stream
  // and retroactively mask the failure this spec exists for. An EMPTY composer
  // keeps submit disabled by design, so typing is the only way to read the lock,
  // and typing changes no stream state.
  await expect(
    page.getByTestId('composer-prompt'),
    'the composer must come back on its own once the recovered turn settled',
  ).toBeEnabled({ timeout: SETTLE_MS });
  await page.getByTestId('composer-prompt').fill('follow-up');
  await expect(page.getByTestId('composer-submit')).toBeEnabled({ timeout: 15_000 });

  // eslint-disable-next-line no-console -- the report tail is where an operator looks
  console.log(
    'UNTOUCHED_TAB_RESTART_CATCHUP_E2E_EVIDENCE',
    JSON.stringify({ session_id: sessionId, ...evidence }),
  );
});
