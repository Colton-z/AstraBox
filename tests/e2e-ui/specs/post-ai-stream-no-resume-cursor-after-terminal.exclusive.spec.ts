/**
 * E2E: the session subscription advances through a terminal frame.
 *
 * Browser instrumentation clones the session event stream without changing the
 * response consumed by the page. Its terminal-bounded GET response must carry a
 * durable cursor for the terminal `finish`, allowing the next GET to start after
 * that turn. The coupled POST path still ends at its terminal without a cursor.
 * Durable state must contain exactly one `finish` on both transport paths.
 *
 * The composer prompt forbids tools and the page must render a reply with a
 * settled header.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { framesForTurn, oracleDbPath, waitForTurnTerminalProof } from '../fixtures/dbOracle';

// One sandbox provision + one full turn + the durable terminal settle; keep the
// budget generous and env-tunable, above the suite default.
// How long to wait for the turn's terminal proof to become durable after the stream closes.
const TERMINAL_PROOF_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TERMINAL_TIMEOUT_MS', 120_000);

/**
 * Byte offset of the LAST terminal frame (`finish`/`error`) in an SSE body, or
 * -1 when it carries none. Frames are serialized compactly
 * (api.sse.format_sse_event uses `separators=(',', ':')`), so the
 * type marker is a plain substring on both channels.
 */
function terminalFrameIndex(raw: string): number {
  return Math.max(raw.lastIndexOf('"type":"finish"'), raw.lastIndexOf('"type":"error"'));
}

/**
 * Every durable frame_seq the stream handed the browser as a resume cursor.
 * Walks the SSE `data:` frames and collects `data.frameSeq` off each
 * `data-resume-cursor` (_helpers._build_resume_cursor_payload). Inlined rather
 * than shared: it is a pure parse over one raw body, like the sibling
 * terminal-tail spec's `resumeCursorSeqsBeforeTerminal`.
 */
function resumeCursorSeqs(raw: string): number[] {
  const seqs: number[] = [];
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
    if (String(frame.type ?? '').trim() !== 'data-resume-cursor') continue;
    const data = frame.data && typeof frame.data === 'object' ? (frame.data as { frameSeq?: unknown }) : null;
    const frameSeq = Number(data?.frameSeq);
    if (Number.isFinite(frameSeq) && frameSeq >= 0) seqs.push(frameSeq);
  }
  return seqs;
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('session GET advances past its terminal while POST ends at the terminal', async ({
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
    // failed turn's terminal is an `error` frame, which would let the cursor
    // assertions below pass on a turn that never reached a clean `finish`.
    await expect(reply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
    return reply;
  };

  try {
    await api.waitForSessionReady(sessionId);

    // Mirror the browser's SSE bodies before the app's first script runs.
    await mirrorSseBodies(page);
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible();

    // ── The user sends one turn from the composer. ───────────────────────────
    // Same prompt the API-driven version used: no tools, one short sentence, so
    // the turn ends on a clean terminal `finish` with no interaction park.
    await sendAndReadReply(`E2E terminal cursor ${runId}: 不要使用工具。请简短回复一句话。`, 240_000);

    // The header settles. A turn whose text arrived but whose header still
    // pulses is a broken screen — and it is also the user-visible reading of
    // "this turn is over", the state the cursor assertions are about.
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute('data-pulse', 'false', {
      timeout: 60_000,
    });

    // The turn's end has to have REACHED the page before its cursors are read.
    // The settled header comes off the session projection, which can win the
    // race against the last frames landing in the browser; poll for the terminal
    // rather than sampling once, so a slow tail is not read as a missing one.
    await expect
      .poll(
        async () => (await aiStreamBodies(page)).some((body) => terminalFrameIndex(body.text) >= 0),
        { timeout: 60_000 },
      )
      .toBe(true);

    // ── Durable property: exactly one committed `finish` survives suppression.
    // No pixel for any of this: the console renders a transcript, not turn ids
    // and not durable frame rows.
    const ready = await api.waitForSessionReady(sessionId);
    // Once the turn is done, current_turn_id is cleared and last_turn_id holds the
    // completed turn.
    const turnId =
      String(ready.last_turn_id || '').trim() ||
      String(ready.current_turn_id || '').trim();
    expect(
      turnId,
      'completed turn should expose a durable turn id (session projection)',
    ).not.toEqual('');

    // 'COMPLETED' is the oracle's kept-for-compatibility `expectedState` slot
    // (it is `void`ed there); the budget is the FOURTH argument. Passing the
    // budget third — what every call site in this suite does — silently leaves
    // the oracle on its 90s default and makes the constant above inert.
    await waitForTurnTerminalProof(sessionId, turnId, 'COMPLETED', TERMINAL_PROOF_BUDGET_MS);

    // Community stores each canonical engine payload under `payload`; the
    // event's terminal type lives at `payload.type`, and its shared event_seq
    // is the value exposed as frameSeq at the streaming protocol boundary.
    const durableFrames = framesForTurn(turnId);
    const frameTypes = durableFrames.map((frame) =>
      String((frame.payload as { type?: unknown } | undefined)?.type ?? '').trim(),
    );
    expect(frameTypes, 'durable turn should include a terminal finish frame').toContain('finish');
    expect(
      frameTypes.filter((type) => type === 'finish'),
      'the response boundary must still leave exactly one durable finish',
    ).toHaveLength(1);

    // ── THE property, on the response the BROWSER pulled. ────────────────────
    // The response must name its durable terminal coordinate exactly once. A
    // missing coordinate replays the same finish forever; a cursor from the next
    // turn would put two assistant messages into one AI SDK parser.
    const browserStreams = await aiStreamBodies(page);
    expect(
      browserStreams.length,
      'the console should have opened the session output channel (GET ai-stream?follow=session)',
    ).toBeGreaterThan(0);
    const browserCursorSeqs = browserStreams.flatMap((body) => resumeCursorSeqs(body.text));
    expect(
      browserCursorSeqs.length,
      'the session response should hand the browser durable resume cursors',
    ).toBeGreaterThan(0);

    const terminalFrameSeqs = new Set(
      durableFrames
        .filter((frame) => {
          const type = String((frame.payload as { type?: unknown } | undefined)?.type ?? '').trim();
          return type === 'finish' || type === 'error';
        })
        .map((frame) => Number(frame.event_seq))
        .filter((seq) => Number.isFinite(seq)),
    );
    expect(
      terminalFrameSeqs.size,
      'the durable turn should carry a terminal frame to compare cursors against',
    ).toBeGreaterThan(0);
    const terminalCursorSeqs = browserCursorSeqs.filter((seq) => terminalFrameSeqs.has(seq));
    expect(
      terminalCursorSeqs,
      'the session response must advance through each terminal exactly once '
        + `(terminal event_seqs=${JSON.stringify([...terminalFrameSeqs])})`,
    ).toHaveLength(terminalFrameSeqs.size);
    expect(new Set(terminalCursorSeqs)).toEqual(terminalFrameSeqs);

    // ── The POST endpoint's own gate, which no user can drive. ───────────────
    // streamPrompt buffers the full stream (the server closes after the terminal
    // frame), so the raw body carries every `data:` line — including any resume
    // cursor that the server should NOT have emitted after terminal.
    const beforeAssistantCount = await api.assistantCount(sessionId);
    const raw = await api.streamPrompt(
      sessionId,
      `E2E terminal cursor ${runId} (POST endpoint): 不要使用工具。请简短回复一句话。`,
    );
    const assistant = await api.waitForAssistantMessageMatching(
      sessionId,
      beforeAssistantCount,
      (message) => messageText(message).trim() !== '',
    );
    expect(messageText(assistant).trim(), 'assistant response should not be empty').not.toEqual('');

    const terminalIndex = terminalFrameIndex(raw);
    expect(
      terminalIndex,
      'POST ai-stream body should include a terminal frame (finish/error)',
    ).toBeGreaterThanOrEqual(0);
    expect(
      raw.indexOf('"type":"data-resume-cursor"', terminalIndex),
      'POST ai-stream must not emit a resume cursor at or after the terminal frame',
    ).toBe(-1);
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
