/**
 * E2E: `GET /stream` replays the end of a completed response from a browser
 * resume cursor.
 *
 * Browser instrumentation clones the page's event stream and records a
 * `data-resume-cursor` that arrived before the response's `finish`, without
 * changing the application's response. After the session is at rest, a GET
 * with `after_seq` set to that cursor must replay the rest of the response,
 * `finish` included. The session's output log (`session_output`) confirms the
 * cursor precedes the stored `finish`.
 *
 * The page must render the complete reply both before and after navigation.
 * The explicit GET is an API assertion because the console follows its stream
 * from its own cursor and never asks for this replay. The prompt forbids
 * tools, so the response ends without an interaction.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, sseFrames } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { sessionStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { oracleDbPath, sessionOutputRows, sessionResponseRows } from '../fixtures/dbOracle';

// How long the response's recorded end may take to land after the stream closes.
const TERMINAL_PROOF_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TERMINAL_TIMEOUT_MS', 120_000);

// A response the platform failed renders its error inside the assistant
// message (`chat:result.turn_failed*`), so one more bubble is not yet a reply.
const RESPONSE_ERROR = /This turn failed|本轮执行失败|API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

// Live rendering may split reasoning and answer frames into several assistant
// elements while a fresh transcript projection folds the same frames into one.
// Content identity survives that presentational boundary; element count does not.
const normalizeRendered = (text: string): string => text.replace(/\s+/g, '');

/**
 * Whether an SSE body carries a response's end (`finish` / `error`).
 * Frames are serialized compactly (api.sse.format_sse_event uses
 * `separators=(',', ':')`), so the type marker is a plain substring; the closing
 * quote keeps `finish-step` out.
 */
function carriesTerminalFrame(raw: string): boolean {
  return raw.includes('"type":"finish"') || raw.includes('"type":"error"');
}

/**
 * The browser resume cursors (`data.seq`) an SSE body carries BEFORE the
 * response's end. Walks the `data:` frames in arrival order, stops at the
 * first `finish`/`error`, and collects each `data-resume-cursor`'s seq.
 * Inlined (not a shared fixture) — this is a pure parse over one raw body: it
 * runs over the body the BROWSER pulled rather than one the test opened.
 *
 * Stopping at the end is load-bearing. The stream writes a cursor after every
 * burst, including one after the `finish`, and that last one correctly
 * resumes to the next response. This test asks whether a browser
 * disconnected just before `finish` can still replay the end of the response,
 * so it needs the last cursor that came earlier.
 */
function resumeCursorSeqsBeforeTerminal(raw: string): number[] {
  const cursors: number[] = [];
  for (const rawLine of raw.split('\n')) {
    const line = rawLine.trim();
    if (!line.startsWith('data:')) continue;
    const body = line.slice(5).trim();
    if (!body || body === '[DONE]') continue;
    let frame: Record<string, unknown>;
    try {
      frame = JSON.parse(body) as Record<string, unknown>;
    } catch {
      continue;
    }
    const type = String(frame.type ?? '').trim();
    if (type === 'finish' || type === 'error') break;
    if (type !== 'data-resume-cursor') continue;
    const data = frame.data && typeof frame.data === 'object' ? (frame.data as { seq?: unknown }) : null;
    const seq = Number(data?.seq);
    if (Number.isInteger(seq) && seq >= 0) cursors.push(seq);
  }
  return cursors;
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('GET ai-stream replays terminal tail from browser cursor after session is ready', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();

  // Fail fast (with the descriptive oracle error) if the database is not
  // reachable — the durable half of this spec depends on it.
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  /** Send from the composer and wait for one more rendered assistant bubble. */
  const sendAndReadReply = async (prompt: string, budgetMs: number) => {
    const before = await page.getByTestId('assistant-message').count();
    await page.getByTestId('composer-prompt').fill(prompt);
    await page.getByTestId('composer-submit').click();
    await expect(page.getByTestId('user-message').last()).toContainText(prompt.slice(0, 8), {
      timeout: 30_000,
    });
    // Count, not text: the model's wording is its own business, and a spec that
    // pins wording fails on a model that is behaving correctly.
    await expect
      .poll(() => page.getByTestId('assistant-message').count(), { timeout: budgetMs })
      .toBeGreaterThan(before);
    const reply = page.getByTestId('assistant-message').last();
    await expect(reply).not.toBeEmpty();
    // A bubble that grew the count is not yet a reply: a failed response
    // renders its error INTO the transcript, so "one more non-empty bubble" is
    // satisfied by exactly the outcome under test — and a failed response ends
    // on an `error` part, which would let the replay assertions below pass on a
    // response that never reached a clean `finish`.
    await expect(reply).not.toContainText(RESPONSE_ERROR);
    return reply;
  };

  try {
    await api.waitForSessionReady(sessionId);

    // ── The user sends one message from the composer. ────────────────────────
    // Mirror the browser's SSE bodies before the app's first script runs, so the
    // session stream is captured from its first byte — the cursors in the
    // middle of the response included. No tools, one short sentence, so the
    // response ends on a clean `finish` without an interaction.
    await mirrorSseBodies(page);
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible();

    const prompt = `E2E terminal tail replay ${runId}: 不要使用工具。请简短回复一句话。`;
    const liveReply = await sendAndReadReply(prompt, 240_000);

    // The header settles. A response whose text arrived but whose header still
    // pulses is a broken screen — and it is also the user-visible reading of
    // "this response is over", the state the replay assertions are about.
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute('data-pulse', 'false', {
      timeout: 60_000,
    });
    const liveReplyText = normalizeRendered(
      (await liveReply.getByTestId('assistant-text').allInnerTexts()).join('\n'),
    );
    expect(liveReplyText, 'the completed live response should have readable reply content').not.toEqual('');

    // The response's end has to have REACHED the browser before its cursors
    // are read: the settled header comes off the session detail, which can win
    // the race against the last parts landing in the page. Poll for the end
    // rather than sampling once, so a slow tail is not read as a missing one —
    // and so "the cursors before the end" is over a complete body.
    await expect
      .poll(async () => (await sessionStreamBodies(page)).some((body) => carriesTerminalFrame(body.text)), {
        timeout: 60_000,
      })
      .toBe(true);

    // ── The browser cursor: the LAST resume cursor the PAGE applied before the
    // response's end. ────────────────────────────────────────────────────────
    // A disconnecting browser holds the newest cursor it applied; resuming from
    // it must still surface the end it never saw. An empty set means the
    // session stream never handed the browser a resume cursor, so no browser
    // could ever reconnect from one — that failure is what this assertion
    // catches.
    const browserStreams = await sessionStreamBodies(page);
    expect(
      browserStreams.length,
      'the console should have opened the session stream (GET /sessions/{id}/stream)',
    ).toBeGreaterThan(0);
    // Only the body that delivered the end says where it fell. One stream
    // request ends after a response's `finish` and the console reopens from its
    // cursor, so a later body holds only what came after the response (the
    // generated title's transient data-session-changed) and has no end to stop at.
    const endingBodies = browserStreams.filter((body) => carriesTerminalFrame(body.text));
    const cursorSeqs = endingBodies.flatMap((body) => resumeCursorSeqsBeforeTerminal(body.text));
    const replayAfterSeq = cursorSeqs.length ? Math.max(...cursorSeqs) : Number.NaN;
    expect(
      Number.isFinite(replayAfterSeq) && replayAfterSeq >= 0,
      'the session stream should hand the browser a resume cursor (data-resume-cursor.seq) before the response ends',
    ).toBe(true);
    // The response the browser watched, named by its own `start`.
    const browserResponseIds = endingBodies
      .flatMap((body) => sseFrames(body.text))
      .filter((frame) => frame.type === 'start')
      .map((frame) => String(frame.messageId ?? ''))
      .filter(Boolean);
    expect(browserResponseIds, 'the body that ended the response must also have started it').toHaveLength(1);
    const responseId = browserResponseIds[0];

    // ── The session is at rest. ──────────────────────────────────────────────
    // This is the crux of "after session is ready" — the replay must serve a
    // finished response from the output log, not a live tail. No pixel for
    // either field: the console renders a transcript, not the session detail.
    const ready = await api.waitForSessionReady(sessionId);
    expect(ready.state, 'session should be READY before the replay').toBe('READY');
    expect(
      ready.current_responses ?? [],
      'the replay covers a response that has ended, with nothing else running',
    ).toEqual([]);
    expect(ready.last_response?.response_id, 'the last response is the one the browser watched').toBe(responseId);
    expect(ready.last_response?.outcome, 'and it completed').toBe('completed');

    // ── Core property: GET replays the end of the response from the cursor. ──
    // resumeStream is GET /sessions/{id}/stream?after_seq=N: the parts stored
    // after the cursor, through the end of one response. The console never
    // asks for this replay — it follows from its own cursor — so this stays an
    // API call by necessity, not by convenience.
    const replay = await api.resumeStream(sessionId, replayAfterSeq);
    expect(replay.status, 'GET /stream?after_seq must answer the replay').toBe(200);
    expect(
      replay.frameTypes,
      'GET /stream should replay the `finish` after the browser cursor',
    ).toContain('finish');
    expect(replay.errorText, 'the replayed end must not be an error').toBeNull();

    // ── Durable half: the response's `finish` is stored after the browser
    // cursor, so the replayed end is recorded state. A response id has no
    // pixel either. ───────────────────────────────────────────────────────────
    await expect.poll(
      () => sessionResponseRows(sessionId).find((row) => row.response_id === responseId)?.outcome ?? null,
      { timeout: TERMINAL_PROOF_BUDGET_MS, message: 'the platform must record the response as completed' },
    ).toBe('completed');
    const storedEnd = sessionOutputRows(sessionId, responseId)
      .filter((row) => String(row.part.type ?? '') === 'finish');
    expect(storedEnd, 'the response must store exactly one finish').toHaveLength(1);
    expect(
      storedEnd[0].seq,
      'the stored finish must come after the cursor the browser held before it',
    ).toBeGreaterThan(replayAfterSeq);

    // ── The user-facing reading of the guarantee. ────────────────────────────
    // A browser that was not connected when the answer landed comes back to the
    // conversation: reload, and the finished response must be on screen — the
    // user's own message, a complete non-error reply, and a header that is not
    // still pretending to work. This is why the ending has to survive past the
    // live stream at all, and it is the one part of the guarantee that has
    // pixels: a backend that replays the end perfectly to a page that renders a
    // truncated transcript afterwards has still lost the answer as far as the
    // user is concerned.
    await page.reload();
    await expect(page.getByTestId('run-view')).toBeVisible();
    await expect(page.getByTestId('user-message').last()).toContainText(String(runId), {
      timeout: 60_000,
    });
    await expect
      .poll(async () => {
        const reloaded = normalizeRendered(
          (await page.getByTestId('assistant-text').allInnerTexts()).join('\n'),
        );
        return reloaded.includes(liveReplyText);
      }, {
        timeout: 60_000,
        message: 'the reloaded transcript must still contain the reply content rendered live',
      })
      .toBe(true);
    const reloadedReply = page.getByTestId('assistant-message').last();
    await expect(reloadedReply).not.toBeEmpty();
    await expect(reloadedReply).not.toContainText(RESPONSE_ERROR);
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute('data-pulse', 'false', {
      timeout: 60_000,
    });
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
