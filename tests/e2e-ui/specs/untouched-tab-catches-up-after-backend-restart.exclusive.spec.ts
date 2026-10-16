/**
 * E2E: a tab nobody touches catches up on its own after the backend restarts.
 *
 * THE JOURNEY. A person is sitting in front of an open conversation with a
 * response in flight. The backend container goes down under them — a deploy,
 * an OOM kill, a rolling restart — and comes back. They touch NOTHING: no
 * reload, no click, no tab switch, not even a window focus. The promise is that
 * the page they are already looking at heals itself: it reopens its own live
 * channel once the server answers again, the same response ends on screen, the
 * header stops pretending to work, the answer reads as one answer rather than
 * its prefix twice, no banner asks them to intervene, and the stop control
 * goes away.
 *
 * WHY THIS IS NOT THE SIBLING SPEC. `server-restart-reattaches-running-sandbox-
 * turn.exclusive.spec.ts` covers the same outage and then calls `page.reload()`.
 * That reload is the whole difference: it throws away every piece of client
 * state and re-derives the screen from recorded history, which is a claim about
 * the BACKEND. It cannot see a front end that reconnects to nothing, because it
 * never asks the live page to do anything — a tab that stopped reopening its
 * stream forever passes that spec on the strength of its fresh load. Everything
 * below therefore happens on ONE navigation, taken before the outage and never
 * repeated.
 *
 * WHY BOTH HALVES ARE ASSERTED. The page keeps polling the session detail
 * through the outage on its own, and the detail alone can tell it the session
 * is at rest. The reply itself only arrives on the page's stream
 * (`SessionFollower`), so a page whose stream stayed dead can show a settled
 * header over a truncated answer and stream the NEXT message into nothing. The
 * screen assertions and the channel assertions are both taken, and a run where
 * the first set passes and the second fails is not a flaky spec: it is that
 * finding, which is why the recorded counts are attached as evidence rather
 * than left in a variable.
 *
 * HOW TIME ENTERS. It does not have to be simulated. The outage is real
 * (`restartServerContainer` runs `docker restart` on the Node side and returns
 * only once the container runs a new process AND `/readyz` answers), and a
 * restarted container necessarily severs every open SSE, so the cut needs no
 * separate proof and no injected fault. What IS ordered on purpose is
 * platform-before-screen: the response's recorded end is awaited first, and
 * only then does the untouched tab get a short window to agree. A settle
 * assertion taken before the platform recorded the end would pass in the
 * middle of the response; taken after, the short window is the staleness bar
 * itself — the backend was done at T, so a tab nobody touched had to agree by
 * T + SETTLE_MS.
 *
 * ANTI-VACUITY. If the model finishes its essay before the restart lands, the
 * journey never happened. The gate read just before `docker restart` (the
 * response open on the session detail and in `session_responses`, and a live
 * reader lease in `session_readers`) turns that from a silent false green into
 * a loud red. It does not close the window completely — the model can still
 * finish DURING the restart — which is why nothing is asserted about the
 * response being live at the instant the server returns, and why no growth of
 * the answer after the cut is required. Both would be coin flips.
 *
 * BUDGET, stated so a later edit cannot quietly push it past the 180s the
 * runner kills at: setup and first paint ~30s, the restart 15-40s, recovery and
 * settle 30-60s. The new process takes the session's reader lease only once
 * the dead process's lease has expired (`LEASE_TTL_S`, 30s, in
 * `session_runtime/reader.py`), which is inside the recovery budget. It fits
 * only because the response is prose rather than a sleep — never give this
 * spec a long Bash.
 *
 * HOW TO READ A SETTLE_MS RED, because one product mechanism sets its floor. If
 * the dying container leaves the socket half-open rather than resetting it, the
 * page learns nothing until `withStreamLiveness` ends the body on silence, and
 * that limit is 45s (`session/streamLiveness.ts`, STREAM_SILENCE_LIMIT_MS). The
 * window here is measured from the platform's recorded end, not from the cut,
 * so a response that ends quickly after the server returns can leave the tab
 * still inside that silence window. A red on the settle assertions with the
 * channel assertions passing shortly after is that interaction and not a
 * rendering bug — report it as the 45s staleness it is, and raise SETTLE_MS (it
 * is env-tunable) only with that measurement in hand. Raising it on a hunch
 * trades away the only bar this file sets.
 *
 * WHAT WOULD MAKE THIS SPEC LIE. (a) Any stray page interaction — a reload, a
 * goto, a click — remounts the page, which reads history again and opens a new
 * stream, and turns a dead page green; between the send and the final composer
 * read, every call here is a Node-side oracle or a passive poll. (b) Counting
 * stream REQUESTS instead of successful RESPONSES would pass on one doomed
 * socket opened while the server was still down, which is exactly the shape of
 * the failure. (c) An unpinned locale would make the absence assertion match
 * nothing and pass for free. (d) Leaving this file out of the contract's serial
 * list would restart the shared server under four other workers.
 *
 * The test is exclusive, and belongs in `playwright.exclusive.serial_files`:
 * restarting the server disrupts every session on the host.
 */
import { expect, test } from '@playwright/test';

import { AstraApi, type InputReceipt } from '../fixtures/astraApi';
import {
  oracleDbPath,
  postgresRows,
  sessionResponseRows,
  sqlLiteral,
} from '../fixtures/dbOracle';
import { absoluteBaseUrl, apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
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
// How long the interrupted response gets to be recorded as ended after the new
// instance comes up. The response is prose on a live sandbox, not a sleep.
const RECOVERY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_RESTART_CATCHUP_RECOVERY_MS', 90_000);
// Taken only AFTER the platform recorded the response's end, so this is the
// screen's lag behind a settled backend on a page nobody touched — short on
// purpose. This constant IS the staleness bar; widening it weakens the only
// claim here.
const SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_RESTART_CATCHUP_SETTLE_MS', 45_000);

// Long enough that prose cannot repeat it by accident, short enough to catch a
// replayed prefix. Compared over normalized text (see normalizeRendered).
const DUPLICATE_WINDOW_CHARS = 48;
// The pre-restart prose must be long enough to cut a window out of its MIDDLE,
// or the duplicate check below would compare nothing and pass for free.
const PRE_RESTART_MIN_CHARS = DUPLICATE_WINDOW_CHARS * 2;

// A FAILED response renders its error INTO the transcript as an assistant
// message (`chat:result.turn_failed*`), so "there is a reply bubble" is
// satisfied by exactly the failure under test.
const RESPONSE_ERROR = /This turn failed|本轮执行失败|API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

// The way the console asks the user to wait out a lost stream
// (`chat:banner.backend_unavailable`). Asserted ABSENT, and only at the END:
// the notice DURING the outage is a promise the product deliberately makes,
// so the bar is that it is gone once the backend is done, not that it never
// appeared. Pinned to en-US by the locale seeding below.
const BACKEND_UNAVAILABLE_EN = 'The backend is temporarily unavailable';

// One GET the BROWSER made on the session stream, and one response it got back.
// Requests say the page TRIED; responses say a stream was actually ESTABLISHED.
// "Opened one doomed socket while the server was down and stayed dead" and
// "recovered" are indistinguishable from requests alone.
interface StreamOpen { at: number; afterSeq: number | null }
interface StreamResponse { at: number; ok: boolean; status: number }

/** The session's reader lease: which process records its runtime, and whether that lease is live. */
interface ReaderLeaseRow {
  owner: string;
  generation: number;
  live: boolean;
}

function readerLease(sessionId: string): ReaderLeaseRow | null {
  return postgresRows<ReaderLeaseRow>(
    `SELECT owner, generation, expires_at > now() AS live FROM session_readers `
      + `WHERE session_id = ${sqlLiteral(sessionId)}`,
  )[0] ?? null;
}

// The console reads its language from localStorage first and navigator second,
// and the absence assertion above is an English string. An unpinned runner
// locale decides which spelling the product uses, and a spec that accepted both
// would also accept a third nobody wrote it against.
test.use({ locale: 'en-US' });

// Both halves of the verdict are kept legible on a red: the channel assertions
// failing while the screen ones passed is the "settled header over a dead
// stream" finding, and it is unreadable without the counts.
const evidence: Record<string, unknown> = {};
let sessionId = '';

test.afterEach(async ({}, info) => {
  if (!['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  await info.attach('untouched-tab-restart-catchup-evidence', {
    body: JSON.stringify({ session_id: sessionId, ...evidence }, null, 2),
    contentType: 'application/json',
  });
});

// Teardown that changes state runs on the passing path only, and hook
// registration order is the order they run in: the interrupt is registered
// BEFORE `trackSessions()` so a response still running cannot keep the delete
// from cleaning up. On the passing path the response has ended and the
// interrupt is a harmless no-op.
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

  // Fail fast (with the descriptive oracle error) if the database is not
  // reachable — the platform half of this spec depends on it.
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
  const streamPath = apiPath(`/sessions/${sessionId}/stream`);
  const streamOpens: StreamOpen[] = [];
  const streamResponses: StreamResponse[] = [];
  page.on('request', (req) => {
    if (req.method() !== 'GET' || new URL(req.url()).pathname !== streamPath) return;
    const raw = new URL(req.url()).searchParams.get('after_seq');
    const parsed = raw === null ? Number.NaN : Number(raw);
    streamOpens.push({ at: Date.now(), afterSeq: Number.isFinite(parsed) ? parsed : null });
  });
  page.on('response', (resp) => {
    if (resp.request().method() !== 'GET' || new URL(resp.url()).pathname !== streamPath) return;
    streamResponses.push({ at: Date.now(), ok: resp.ok(), status: resp.status() });
  });

  await api.waitForSessionReady(sessionId);

  // Pin the console language before the FIRST navigation, or the absence
  // assertion at the end reads whatever the runner's locale happens to be.
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
  const inputPath = apiPath(`/sessions/${sessionId}/inputs`);
  const sent = page.waitForResponse((response) => (
    response.request().method() === 'POST' && new URL(response.url()).pathname === inputPath
  ), { timeout: 30_000 });
  await composer.fill(prompt);
  await page.getByTestId('composer-submit').click();
  const receiptResponse = await sent;
  expect(receiptResponse.status(), 'the platform must accept the message').toBe(200);
  const receipt = ((await receiptResponse.json()) as { data: InputReceipt }).data;
  evidence.input_id = receipt.input_id;
  await expect(
    page.getByTestId('user-message').last(),
    'the user bubble should render — proof the message was sent from the page',
  ).toContainText(marker, { timeout: 30_000 });

  // The run reads as live to the user: the stop control appears as soon as the
  // page sends, before any part of the response can come back.
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
  // No pixel: the screen shows that something is running, never WHO is
  // recording it. The response open in the platform's own records plus a live
  // reader lease is the evidence that a reader is recording THIS response — and
  // if the model has already finished, this is where the run goes red instead
  // of passing on a journey that never happened.
  const responseRow = sessionResponseRows(sessionId)
    .find((row) => row.input_ids.includes(receipt.input_id));
  expect(
    responseRow,
    `the platform must have recorded the response to this message before the restart; `
      + `responses=${JSON.stringify(sessionResponseRows(sessionId))}`,
  ).toBeTruthy();
  const responseId = responseRow!.response_id;
  evidence.response_id = responseId;
  expect(
    responseRow!.status,
    `the response must still be running before the restart; row=${JSON.stringify(responseRow)}`,
  ).toBe('running');
  const before = await api.getSession(sessionId);
  expect(
    (before.current_responses ?? []).map((response) => response.response_id),
    'the session detail must report the same response running',
  ).toContain(responseId);
  const leaseBefore = readerLease(sessionId);
  evidence.reader_lease_before = leaseBefore;
  expect(leaseBefore?.live, `a reader must hold a live lease on the session; lease=${JSON.stringify(leaseBefore)}`)
    .toBe(true);

  // ── The outage, under an open browser. ───────────────────────────────────
  // `restartServerContainer` runs on the Node side, so the page is never
  // touched. The timestamp is taken BEFORE the call: the page holds exactly one
  // stream request at a time and already has a live one here, so it cannot open
  // another before the container goes down — every GET counted after this
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
  // those can remount the page or start a request that heals the exact defect
  // under test. Until the composer read at the very end, every call below is a
  // Node-side oracle or a passive poll.

  // Platform first. The console renders no response id, so THIS response
  // ending — rather than some later one, or a repaint over an unresolved one —
  // is only bindable in the platform's records.
  await expect
    .poll(() => {
      const row = sessionResponseRows(sessionId).find((item) => item.response_id === responseId);
      return row ? `${row.status}/${row.outcome ?? ''}` : 'missing';
    }, {
      timeout: RECOVERY_TIMEOUT_MS,
      message: 'the interrupted response must complete once the new instance reads the runtime again',
    })
    .toBe('ended/completed');
  const recovered = await api.getSession(sessionId);
  expect(recovered.last_response?.response_id, 'the last response is the one the restart interrupted')
    .toBe(responseId);
  expect(
    sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(receipt.input_id)),
    'the message is answered by exactly one response, not a second one started after the restart',
  ).toHaveLength(1);
  evidence.reader_lease_after = readerLease(sessionId);
  evidence.platform_end_at_ms_after_restart = Date.now() - restartRequestedAt;

  // ── Screen second, and quickly. ──────────────────────────────────────────
  // Scoped to run-view ON PURPOSE. The app shell's sidebar renders a
  // `status-pill` for EVERY conversation in the list and precedes the route
  // outlet in the DOM, so a bare `status-pill.first()` binds to some OTHER
  // conversation's row, whose `data-pulse` is false for free — the assertion
  // would pass on a header still spinning forever, which is the failure this
  // line exists for.
  await expect(
    page.getByTestId('run-view').getByTestId('status-pill').first(),
    'the untouched tab must stop pretending to work once the recovered response ended',
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
  await expect(reply).not.toContainText(RESPONSE_ERROR);

  // ── It reads as ONE answer. ──────────────────────────────────────────────
  // Two halves, because either alone is passable by the other's failure. The
  // stretch the user watched arrive must still be there (a reopen that rebuilt
  // the transcript from the head, or a recovery that regenerated the reply,
  // loses it) and it must be there ONCE (a reopen that replayed from a stale
  // cursor shows the same sentences twice).
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

  // ── The page never ended up asking the user to wait for nothing. ─────────
  // Taken only at the END. "The backend is temporarily unavailable" DURING the
  // outage is a promise the product makes on purpose; still being there once
  // the backend is done is the promise broken.
  await expect(
    page.getByText(BACKEND_UNAVAILABLE_EN),
    'the backend-unavailable banner must be gone once the backend is back',
  ).toHaveCount(0);

  // ── The mechanism half: this tab reopened its OWN live channel. ──────────
  // Counted on RESPONSES, not requests. A page that opened one doomed socket
  // while the server was down and then gave up produces the same request count
  // as a page that recovered.
  const establishedAfterRestart = () =>
    streamResponses.filter((entry) => entry.ok && entry.at >= restartRequestedAt);
  await expect
    .poll(() => establishedAfterRestart().length, {
      timeout: SETTLE_MS,
      message:
        'the untouched tab must re-establish its own live channel after the backend came back — '
        + 'a page settled by the detail poll while the stream stays dead means the NEXT '
        + 'message streams into nothing',
    })
    .toBeGreaterThan(0);

  const resumedOpens = streamOpens.filter((entry) => entry.at >= restartRequestedAt);
  evidence.stream_opens_after = resumedOpens.length;
  evidence.stream_responses_ok_after = establishedAfterRestart().length;
  evidence.resumed_after_seq = resumedOpens.map((entry) => entry.afterSeq);

  // And every reopen carried the page's cursor rather than restarting from the
  // head: the page had received parts of this response before the restart, so
  // its cursor is past zero. Deliberately NOT compared to a specific position —
  // nothing here pins one; the correctness half a wrong cursor would break is
  // carried by the duplicate check above.
  expect(
    resumedOpens.length > 0 && resumedOpens.every((entry) => typeof entry.afterSeq === 'number' && entry.afterSeq > 0),
    `the reopened channel must resume from a cursor, not from the head; `
      + `after_seq=${JSON.stringify(resumedOpens.map((entry) => entry.afterSeq))}`,
  ).toBe(true);

  // ── LAST: the page reads at rest, in the only form a user can see it. ────
  // Taken after every no-touch assertion, and the submit button is never
  // clicked: a send would start a new response and mask what this spec reads.
  // The stop control is shown only while the page believes a response runs.
  await expect(
    page.getByTestId('run-composer-stop'),
    'the stop control must go away on its own once the recovered response ended',
  ).toHaveCount(0, { timeout: SETTLE_MS });
  await expect(page.getByTestId('composer-prompt')).toBeEnabled({ timeout: SETTLE_MS });
  await page.getByTestId('composer-prompt').fill('follow-up');
  await expect(page.getByTestId('composer-submit')).toBeEnabled({ timeout: 15_000 });

  // eslint-disable-next-line no-console -- the report tail is where an operator looks
  console.log(
    'UNTOUCHED_TAB_RESTART_CATCHUP_E2E_EVIDENCE',
    JSON.stringify({ session_id: sessionId, ...evidence }),
  );
});
