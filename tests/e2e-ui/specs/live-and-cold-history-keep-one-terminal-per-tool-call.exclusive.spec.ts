/**
 * E2E: live and cold history agree on one terminal per tool call.
 *
 * The prompt requests two sequential Bash calls. Each executed toolCallId must
 * have one durable `tool-output-*` terminal, and both the live page and a cold
 * reload must show one settled Bash card per call with no failure bubble.
 *
 * The model may combine or omit the requested tools. The test fails when fewer
 * than two distinct Bash calls execute because there is no sequence to verify.
 * Page settle checks run only after the kernel reports the turn complete; the
 * composer's optimistic state is not a terminal oracle.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { trackSessions } from '../fixtures/sessionCleanup';
import { framesForTurn } from '../fixtures/dbOracle';
import { appPath } from '../fixtures/env';

const READY_TIMEOUT_MS = 45_000;
const TURN_BUDGET_MS = 90_000;
// How long the settled transcript gets to finish painting its tool cards.
const RENDER_TIMEOUT_MS = 30_000;

// The tool card header is a button whose accessible name is the tool name plus
// its state badge. `\b` keeps "Bash" from matching the SDK's separate
// "BashOutput" tool, so the page count stays comparable with the durable
// `toolName === 'Bash'` filter below.
const BASH_CARD = /^Bash\b/;
// Non-terminal tool states (chat:tool_state.processing / awaiting_confirmation).
// The console is bilingual and the runner's locale is en-US, so both are matched
// — the same reason the tree-order spec matches en and zh.
const BASH_UNSETTLED = /^Bash\s+(Working|Awaiting confirmation|处理中|等待确认)/;

interface EngineFramePayload {
  type?: unknown;
  toolCallId?: unknown;
  toolName?: unknown;
}

function framePayload(frame: Record<string, unknown>): EngineFramePayload {
  const payload = frame.payload;
  return (payload && typeof payload === 'object' ? payload : {}) as EngineFramePayload;
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('live and cold history keep one terminal per tool call', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const firstMarker = `SEQUENTIAL_TOOLS_FIRST_${runId}`;
  const secondMarker = `SEQUENTIAL_TOOLS_SECOND_${runId}`;
  const doneMarker = `SEQUENTIAL_TOOLS_DONE_${runId}`;

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  try {
    await api.waitForSessionReady(sessionId, READY_TIMEOUT_MS);

    // ARRANGE through the API. The UI dispatches under the SESSION's permission
    // mode (useSessionChat sends `permission_mode: permissionModeRef.current`,
    // seeded from session.permission_mode at load), so the mode has to be armed
    // BEFORE the page opens: a Bash that stops for approval never completes the
    // sequential projection this test verifies.
    const armed = await api.setPermissionMode(sessionId, 'bypassPermissions');
    expect(
      armed.permission_mode,
      'the sequential turn must run its two Bash calls unattended (bypassPermissions)',
    ).toBe('bypassPermissions');

    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });

    const prompt = [
      `E2E sequential tools ${runId}. Follow these instructions literally.`,
      'Run exactly TWO Bash tool calls sequentially and do not combine them.',
      `Bash call 1 must run exactly: printf '${firstMarker}\\n'`,
      `After call 1 completes, Bash call 2 must run exactly: printf '${secondMarker}\\n'`,
      `After both calls complete, reply with exactly: ${doneMarker}`,
    ].join('\n');

    // ACT through the page: the user types the instruction and sends it.
    // `.fill()` sets the value without key events, so the multi-line prompt is
    // not submitted early by an Enter newline.
    const assistantBefore = await page.getByTestId('assistant-message').count();
    const composer = page.getByTestId('composer-prompt');
    await expect(composer, 'the composer must be enabled before sending').toBeEnabled({ timeout: 45_000 });
    await composer.fill(prompt);
    await page.getByTestId('composer-submit').click();
    await expect(
      page.getByTestId('user-message').last(),
      'the user bubble should render — proof the turn was dispatched from the page',
    ).toContainText(`sequential tools ${runId}`, { timeout: 30_000 });

    // The run reads as live to the user: the send button became a stop button.
    // Not a race with the model — `isSubmitted` flips inside the submit handler,
    // before the send receipt (let alone a frame) can come back.
    await expect(page.getByTestId('run-composer-stop')).toBeVisible({ timeout: 30_000 });

    // ── The turn's own liveness is read from the kernel, NOT from the screen,
    //    and that is deliberate. The console derives "busy" from a 1.5s session
    //    poll (useSessionLifecycle), while `isSubmitted` drops the moment the
    //    send receipt returns — so for about a second after the send the composer
    //    and the pill BOTH read settled while the turn is very much alive. A
    //    settle assertion taken there would pass one second into a still-live
    //    turn and prove nothing. `current_turn_id` is the turn's own liveness,
    //    and on a fresh conversation `current_turn_id || last_turn_id` can only
    //    be THIS turn. No pixel: the console shows no turn id anywhere. ─────────
    const running = await api.waitForSession(
      sessionId,
      (s) => Boolean(String(s.current_turn_id || '').trim() || String(s.last_turn_id || '').trim()),
      60_000,
    );
    const turnId =
      String(running.current_turn_id || '').trim() ||
      String(running.last_turn_id || '').trim();
    expect(turnId, 'the dispatched turn should expose a durable turn id').not.toEqual('');

    // ── The live projection window. The browser holds the stream open across
    //    both sequential calls. The turn is over when the kernel clears
    //    current_turn_id and last_turn_id holds this turn.
    await api.waitForSession(
      sessionId,
      (s) =>
        String(s.state || '') === 'READY' &&
        String(s.current_turn_id || '').trim() === '' &&
        String(s.last_turn_id || '').trim() === turnId,
      TURN_BUDGET_MS,
    );

    // ── ASSERT on the page, now that the kernel is done: the screen has to
    //    agree, and within a bounded budget. Reply delivered but the header
    //    still pulsing is the phantom-"generating" failure — a broken screen
    //    over a correct backend, which only a browser can catch.
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute('data-pulse', 'false', {
      timeout: 60_000,
    });
    await expect(
      page.getByTestId('composer-submit'),
      'the run must read as finished to the user: the stop button turns back into send',
    ).toBeVisible({ timeout: 60_000 });

    // The reply lands where the user reads it. Count, not text: the model's
    // wording is its own business.
    await expect
      .poll(() => page.getByTestId('assistant-message').count(), { timeout: RENDER_TIMEOUT_MS })
      .toBeGreaterThan(assistantBefore);
    const reply = page.getByTestId('assistant-message').last();
    await expect(reply).not.toBeEmpty();
    // A bubble that grew the count is not yet a reply: a FAILED turn renders its
    // error INTO the transcript as an assistant message, so "one more non-empty
    // bubble" is satisfied by exactly the outcome under test.
    await expect(reply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);

    const frames = framesForTurn(turnId);

    // ── Precondition: two distinct Bash tool calls durably executed. ─────────
    // Without two distinct calls (deepseek combined them or skipped a tool)
    // there is no sequential projection to exercise.
    const bashToolCallIds = new Set(
      frames
        .map(framePayload)
        .filter((p) => String(p.type ?? '') === 'tool-input-available'
          && String(p.toolName ?? '') === 'Bash')
        .map((p) => String(p.toolCallId ?? '').trim())
        .filter(Boolean),
    );
    expect(
      bashToolCallIds.size,
      `deepseek did not durably execute two distinct Bash tool calls `
        + `(${bashToolCallIds.size}) — no sequential terminal projection to exercise`,
    ).toBeGreaterThanOrEqual(2);

    // ── The user-visible half of "exactly one terminal per tool call": the
    //    live transcript shows ONE card per durably-executed Bash call, and
    //    every one carries a terminal. A duplicate in another assistant message
    //    paints another card; a card left on "Working" means projection lost its
    //    terminal. The durable count below cannot detect either screen defect.
    // The settled turn folds its finished cards behind one header and a closed
    // fold keeps them out of the accessibility tree, so the count below is of
    // what a reader sees after opening it rather than of what survived folding.
    await revealAssistantProcess(page);
    const bashCards = page.getByTestId('assistant-message').getByRole('button', { name: BASH_CARD });
    await expect
      .poll(() => bashCards.count(), {
        timeout: RENDER_TIMEOUT_MS,
        message:
          'the transcript must render exactly one Bash tool card per durably-executed Bash tool call '
          + `(durable distinct toolCallIds=${bashToolCallIds.size})`,
      })
      .toBe(bashToolCallIds.size);
    const unsettledBashCards = page
      .getByTestId('assistant-message')
      .getByRole('button', { name: BASH_UNSETTLED });
    await expect
      .poll(() => unsettledBashCards.count(), {
        timeout: RENDER_TIMEOUT_MS,
        message:
          'no Bash tool card may still read as running/awaiting after the turn settled — '
          + 'a card without its terminal is a terminal the projection dropped',
      })
      .toBe(0);

    // ── Durable half: exactly one tool-output terminal per Bash toolCallId. ──
    // A duplicate terminal can repaint the same live card and merge into the
    // same durable message segment, so only the event ledger carries the count.
    const terminalsByTool = new Map<string, Record<string, unknown>[]>();
    for (const frame of frames) {
      const payload = framePayload(frame);
      const frameType = String(payload.type ?? '');
      const toolCallId = String(payload.toolCallId ?? '').trim();
      if (!frameType.startsWith('tool-output-') || !toolCallId) continue;
      const list = terminalsByTool.get(toolCallId) || [];
      list.push(frame);
      terminalsByTool.set(toolCallId, list);
    }

    const terminalEvidence = Array.from(bashToolCallIds).map((toolCallId) => ({
      tool_call_id: toolCallId,
      frames: (terminalsByTool.get(toolCallId) || []).map((frame) => ({
        event_seq: frame.event_seq,
        type: framePayload(frame).type,
      })),
    }));

    expect(
      terminalEvidence.map((item) => ({
        tool_call_id: item.tool_call_id,
        terminal_count: item.frames.length,
      })),
      `each durably executed Bash call must have exactly one terminal; `
        + `evidence=${JSON.stringify(terminalEvidence)}`,
    ).toEqual(
      Array.from(bashToolCallIds).map((toolCallId) => ({
        tool_call_id: toolCallId,
        terminal_count: 1,
      })),
    );

    // ── Cold projection: rebuild from durable state, not this tab's frames. ──
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
    await expect(
      page.getByTestId('user-message').filter({ hasText: `sequential tools ${runId}` }).last(),
      'the cold transcript must retain the user turn that owns the tool calls',
    ).toBeVisible({ timeout: 45_000 });
    await expect(page.getByTestId('assistant-message').last()).not.toContainText(
      /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i,
    );

    // A cold page carries the folded work as a header alone: the cards are not
    // in the document until the header is opened and its blocks are fetched.
    await revealAssistantProcess(page);
    const coldBashCards = page
      .getByTestId('assistant-message')
      .getByRole('button', { name: BASH_CARD });
    await expect
      .poll(() => coldBashCards.count(), {
        timeout: RENDER_TIMEOUT_MS,
        message: 'cold history must rebuild one Bash card per durable tool call',
      })
      .toBe(bashToolCallIds.size);
    const coldUnsettledBashCards = page
      .getByTestId('assistant-message')
      .getByRole('button', { name: BASH_UNSETTLED });
    await expect
      .poll(() => coldUnsettledBashCards.count(), {
        timeout: RENDER_TIMEOUT_MS,
        message: 'cold history must not lose any Bash terminal',
      })
      .toBe(0);
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
