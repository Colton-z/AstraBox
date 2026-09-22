/**
 * E2E: GET ai-stream replays a completed turn's terminal tail from a browser
 * resume cursor.
 *
 * Browser instrumentation clones the page's event stream and records a
 * pre-terminal data-resume-cursor without changing the application's response.
 * After the turn returns to READY, a GET with after_seq set to that cursor must
 * replay later durable frames including `finish`. The frame store confirms that
 * the cursor precedes the terminal frame.
 *
 * The page must render the complete reply both before and after navigation. The
 * explicit GET remains an API assertion because the console follows a session
 * stream rather than issuing per-turn resume requests. The prompt forbids tools,
 * so the turn should finish without a pending interaction.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { framesForTurn, oracleDbPath, waitForTurnTerminalProof } from '../fixtures/dbOracle';

// One sandbox provision + one full turn + a GET replay + the durable terminal
// settle; keep the budget generous and env-tunable, above the suite default.
// How long to wait for the turn's terminal proof to become durable after the stream closes.
const TERMINAL_PROOF_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TERMINAL_TIMEOUT_MS', 120_000);

// Live rendering may split reasoning and answer frames into several assistant
// elements while a fresh transcript projection folds the same frames into one.
// Content identity survives that presentational boundary; element count does not.
const normalizeRendered = (text: string): string => text.replace(/\s+/g, '');

/**
 * Whether an SSE body carries a turn-terminal frame (`finish` / `error`).
 * Frames are serialized compactly (api.sse.format_sse_event uses
 * `separators=(',', ':')`), so the type marker is a plain substring; the closing
 * quote keeps `finish-step` out.
 */
function carriesTerminalFrame(raw: string): boolean {
  return raw.includes('"type":"finish"') || raw.includes('"type":"error"');
}

/**
 * The browser resume cursors (`data.frameSeq`) an SSE body emits BEFORE its
 * terminal frame. Walks the `data:` frames in arrival order, stops at the first
 * `finish`/`error`, and collects each `data-resume-cursor`'s frameSeq. Inlined
 * (not a shared fixture) — this is a pure parse over one raw body: it runs over
 * the body the BROWSER pulled rather than one the test opened.
 *
 * Stopping at the terminal is load-bearing on THIS channel, not decoration.
 * The POST tail this parse was written for ends at the terminal frame, so
 * "every cursor in the body" and "every cursor before the terminal" were the
 * same set. The session subscription now emits its terminal cursor after the
 * `finish`; stopping here deliberately selects the last earlier boundary. That
 * is the precondition the replay below needs: the terminal cursor correctly
 * resumes to the next turn, while this test asks whether a browser disconnected
 * just before `finish` can still replay the terminal tail.
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
    const data = frame.data && typeof frame.data === 'object' ? (frame.data as { frameSeq?: unknown }) : null;
    const frameSeq = Number(data?.frameSeq);
    if (Number.isFinite(frameSeq) && frameSeq >= 0) cursors.push(frameSeq);
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

  // Fail fast (with the descriptive oracle error) if the document store is not
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
    // A bubble that grew the count is not yet a reply: a failed turn renders
    // its error INTO the transcript as an assistant message, so "one more
    // non-empty bubble" is satisfied by exactly the outcome under test — and a
    // failed turn ends on an `error` terminal, which would let the tail-replay
    // assertions below pass on a turn that never reached a clean `finish`.
    await expect(reply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
    return reply;
  };

  try {
    await api.waitForSessionReady(sessionId);

    // ── The user sends one turn from the composer. ───────────────────────────
    // Mirror the browser's SSE bodies before the app's first script runs, so the
    // session channel is captured from its first byte — the mid-turn cursors
    // included. Same prompt the API-driven version used: no tools, one short
    // sentence, so the turn ends on a clean terminal `finish` with no park.
    await mirrorSseBodies(page);
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible();

    const prompt = `E2E terminal tail replay ${runId}: 不要使用工具。请简短回复一句话。`;
    const liveReply = await sendAndReadReply(prompt, 240_000);

    // The header settles. A turn whose text arrived but whose header still
    // pulses is a broken screen — and it is also the user-visible reading of
    // "this turn is over", the state the replay assertions are about.
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute('data-pulse', 'false', {
      timeout: 60_000,
    });
    const liveReplyText = normalizeRendered(
      (await liveReply.getByTestId('assistant-text').allInnerTexts()).join('\n'),
    );
    expect(liveReplyText, 'the completed live turn should have readable reply content').not.toEqual('');

    // The turn's end has to have REACHED the browser before its cursors are
    // read: the settled header comes off the session projection, which can win
    // the race against the last frames landing in the page. Poll for the
    // terminal rather than sampling once, so a slow tail is not read as a
    // missing one — and so "the cursors before terminal" is over a complete
    // body.
    await expect
      .poll(async () => (await aiStreamBodies(page)).some((body) => carriesTerminalFrame(body.text)), {
        timeout: 60_000,
      })
      .toBe(true);

    // ── The browser cursor: the LAST resume cursor the PAGE applied before the
    // terminal frame. ────────────────────────────────────────────────────────
    // A disconnecting browser holds the newest cursor it applied; resuming from
    // it must still surface the terminal tail it never saw. An empty set means
    // the session channel never handed the browser a resume cursor, so no
    // browser could ever reconnect from one — that failure is what this
    // assertion catches, so assert >= 0.
    const browserStreams = await aiStreamBodies(page);
    expect(
      browserStreams.length,
      'the console should have opened the session output channel (GET ai-stream?follow=session)',
    ).toBeGreaterThan(0);
    const cursorSeqs = browserStreams.flatMap((body) => resumeCursorSeqsBeforeTerminal(body.text));
    const replayAfterSeq = cursorSeqs.length ? Math.max(...cursorSeqs) : Number.NaN;
    expect(
      Number.isFinite(replayAfterSeq) && replayAfterSeq >= 0,
      'the session stream should hand the browser a resume cursor (data-resume-cursor.frameSeq) before the terminal frame',
    ).toBe(true);

    // ── The turn is COMPLETE: session READY, current_turn_id cleared. ────────
    // This is the crux of "after session is ready" — the resume must serve a
    // finished turn from durable storage, not a still-active live tail. No
    // pixel for either field: the console renders a transcript, not the
    // session projection.
    const ready = await api.waitForSessionReady(sessionId);
    expect(ready.state, 'session should be READY before the terminal-tail resume').toBe('READY');
    expect(
      ready.current_turn_id,
      'terminal-tail resume covers a completed turn after current_turn_id is cleared',
    ).toBeFalsy();

    // ── Core property: GET resume replays the terminal tail from the cursor. ──
    // resumeStream is GET /sessions/{id}/ai-stream?after_seq=N (no `follow`); it
    // 204s (replayed false) when there is nothing resumable, else replays the
    // durable frames past the cursor. The completed turn's replay must reach the
    // durable `finish`. The console never asks for this variant — it follows the
    // session — so this stays an API call by necessity, not by convenience.
    const replay = await api.resumeStream(sessionId, replayAfterSeq);
    expect(
      replay.replayed,
      'GET ai-stream?after_seq should replay (200, not 204) the completed turn from the browser cursor',
    ).toBe(true);
    expect(
      replay.frameTypes,
      'GET resume should replay the terminal `finish` after the browser cursor',
    ).toContain('finish');

    // ── Durable half: the completed turn commits with a `finish` whose frame_seq
    // is strictly after the browser cursor (the replayed tail is real state). ──
    // Once the turn is done current_turn_id is cleared and last_turn_id holds the
    // completed turn. A turn id has no pixel either.
    const turnId =
      String(ready.last_turn_id || '').trim() ||
      String(ready.current_turn_id || '').trim();
    expect(
      turnId,
      'completed turn should expose a durable turn id (session projection)',
    ).not.toEqual('');

    // Replay follows a successful terminal on this same turn.
    await waitForTurnTerminalProof(sessionId, turnId, 'COMPLETED', TERMINAL_PROOF_BUDGET_MS);

    // Community stores each canonical engine payload under `payload`; event_seq
    // orders the durable tail and is exposed as frameSeq on the wire.
    const hasFinishAfterCursor = framesForTurn(turnId).some((frame) => {
      const seq = Number(frame.event_seq);
      const type = String((frame.payload as { type?: unknown } | undefined)?.type ?? '').trim();
      return Number.isFinite(seq) && seq > replayAfterSeq && type === 'finish';
    });
    expect(
      hasFinishAfterCursor,
      'durable events should contain a `finish` with event_seq after the browser cursor',
    ).toBe(true);

    // ── The user-facing reading of the guarantee. ────────────────────────────
    // A browser that was not connected when the answer landed comes back to the
    // conversation: reload, and the finished turn must be on screen — the user's
    // own message, a complete non-error reply, and a header that is not still
    // pretending to work. This is why the ending has to survive past the live
    // stream at all, and it is the one part of the guarantee that has pixels: a
    // backend that replays the tail perfectly to a page that renders a truncated
    // transcript afterwards has still lost the answer as far as the user is
    // concerned.
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
    await expect(reloadedReply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute('data-pulse', 'false', {
      timeout: 60_000,
    });
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
