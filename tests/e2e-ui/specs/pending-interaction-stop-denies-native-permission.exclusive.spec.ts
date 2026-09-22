/**
 * E2E: stopping a turn at a tool-permission prompt rejects the tool and lets the
 * conversation settle normally.
 *
 * The page sends a default-mode Write request, waits for the approval panel, and
 * uses that panel's stop control. The panel must clear, the composer must return,
 * and the header must move from WAITING_INPUT to READY. Durable state must record
 * a denied tool result, a terminal `finish`, COMPLETED status, and no active
 * interaction or error.
 *
 * Tool invocation is model-dependent. A bounded probe skips only when no
 * permission interaction appears; a raised interaction that the page cannot show
 * remains a failure.
 */
import { test, expect, type Page } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi, visibleMessages, type PendingInteraction } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { documentsByField, framesForTurn, oracleDbPath, snapshotDoc, waitForTurnTerminalProof } from '../fixtures/dbOracle';
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

// One sandbox provision + a paused turn + the interrupt-deny continuation + the
// durable terminal settle sit above the 240s suite default; keep it generous
// and env-tunable.
// Bounded probe: how long to wait for a pending interaction before skipping.
// Sending from the composer returns as soon as the turn is accepted (it is not a
// blocking POST that comes back only once the turn parks), so this budget spans
// the model's run up to the park, not just projection lag. Left at the suite-wide
// 60s the whole pending-interaction family shares — a model too slow to ask inside
// it skips, and the skip reason says which of the two happened.
/** What a user says when a model answers instead of acting. */
const INSIST_NUDGE =
  "You answered without using the tool. Do the tool call now, exactly as asked above, before replying again. \u8bf7\u73b0\u5728\u5c31\u6309\u4e0a\u9762\u7684\u8981\u6c42\u8c03\u7528\u5de5\u5177\uff0c\u4e0d\u8981\u53ea\u7528\u6587\u5b57\u56de\u7b54\u3002";

const PROBE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_PENDING_PROBE_TIMEOUT_MS', 60_000);
// After the interrupt-deny, how long to let the resumed turn settle back to READY.
const SETTLE_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_PENDING_STOP_SETTLE_TIMEOUT_MS', 180_000);
// How long to wait for the denied turn's terminal proof to become durable.
const TERMINAL_PROOF_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TERMINAL_TIMEOUT_MS', 120_000);

// The pending card's own stop control. SessionPendingInteractionFooter hands
// PromptInputSubmit to the card as `stopControl`; PromptInputSubmit localizes its
// accessible name, so name both states in both locales the console ships. Keep
// this role-and-name locator: it proves the icon-only control is reachable to a
// reader, not merely present in the DOM.
// Deliberately NOT the card's "Reject this action" button: rejecting ANSWERS the
// interaction through interaction-respond, while stop goes through POST
// /interrupt — and the interrupt path is the one under test.
const STOP_CONTROL = /^(Stop generating|Stopping|停止生成|停止中)$/i;
// A FAILED turn renders its error INTO the transcript as an assistant message,
// so "there is an assistant bubble" is satisfied by exactly a failure.
const TURN_ERROR = /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;
// Tearing the turn down as an interrupt surfaces as a FAILED turn carrying this
// exact backend string (frontend/src/session/turnFailureIntent.ts
// USER_INTERRUPTED_TURN_ERROR), which TurnFailureCard renders verbatim. Its
// absence is the user-visible half of "stop denied the permission, it did not
// interrupt the turn"; the durable last_turn_status below stays the authority,
// because this card only renders when the projection carries a turn_failure
// block. The lookahead excludes the unrelated Claude wire literal "[Request
// interrupted by user for tool use]", which is a legitimate transcript string
// (normally replaced for display by utils/format DISPLAY_TEXT_REPLACEMENTS, but
// not on every surface) — matching it would be a red about nothing.
const INTERRUPTED_FAILURE = /Request interrupted by user(?! for tool use)/i;

/** The canonical engine frame a durable `session_events` row stores under `payload`. */
function framePayload(frame: Record<string, unknown>): Record<string, unknown> {
  const payload = frame.payload;
  return payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {};
}

/** Parse complete SSE data lines already received by the browser. */
function streamPayloads(text: string): Record<string, unknown>[] {
  return text.split(/\r?\n/).slice(0, -1)
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trim())
    .filter((raw) => raw && raw !== '[DONE]')
    .map((raw) => JSON.parse(raw) as Record<string, unknown>);
}

/** Open the session the way the user does, and wait for the run view to mount. */
async function openSessionView(page: Page, sessionId: string): Promise<void> {
  await page.goto(appPath(`/sessions/${sessionId}`), { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
}

/**
 * Fill the real composer and confirm dispatch by the user's own bubble. `.fill()`
 * sets the value without key events, so nothing is submitted early by an Enter.
 */
async function sendFromComposer(page: Page, prompt: string, echo: string): Promise<void> {
  const composer = page.getByTestId('composer-prompt');
  await expect(composer, 'the composer must be enabled before sending').toBeEnabled({ timeout: 45_000 });
  await composer.fill(prompt);
  const submit = page.getByTestId('composer-submit');
  await expect(submit).toBeEnabled({ timeout: 15_000 });
  await submit.click();
  await expect(
    page.getByTestId('user-message').filter({ hasText: echo }).last(),
    'the user bubble should render — proof the turn was dispatched from the page',
  ).toBeVisible({ timeout: 30_000 });
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('pending interaction stop denies native permission and settles turn', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const targetPath = `e2e-pending-stop-${runId}.txt`;

  // Fail fast (with the descriptive oracle error) if the document store is not
  // reachable — the durable half of this spec depends on it.
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  try {
    await api.waitForSessionReady(sessionId);

    // ARRANGE (API — this is a precondition, not the behaviour under test): the
    // page dispatches under the session's permission_mode (useSessionChat sends
    // `permission_mode: permissionModeRef.current`, seeded from
    // session.permission_mode at load), and community conversations open in
    // bypassPermissions, so the switch has to happen BEFORE the page opens or no
    // permission is ever raised to stop.
    await api.setPermissionMode(sessionId, 'default');
    const armed = await api.getSession(sessionId);
    expect(
      armed.permission_mode,
      'stopping a native permission needs tool approvals on (permission_mode=default)',
    ).toBe('default');

    await mirrorSseBodies(page);
    await openSessionView(page, sessionId);

    // ── The user asks for one Write; the turn parks on the tool permission. ───
    await sendFromComposer(
      page,
      `E2E pending stop ${runId}: 请使用 Write 工具创建相对路径 ${targetPath}，` +
        `内容写入 "pending stop e2e"。只创建这一个文件，然后等待我确认，不要继续其它操作。`,
      `E2E pending stop ${runId}`,
    );

    // ── PROBE: deepseek-chat may never raise a permission (the turn just
    //    finishes with stop). Bounded-wait for a pending interaction; skip — do
    //    not fake-pass — if the platform never surfaces one. Kept on the API on
    //    purpose: the skip is for the MODEL declining, so a raised interaction
    //    that never reaches the screen must go red, which is what the page
    //    assertion below does. ────────────────────────────────────────────────
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. The probe returns the moment the turn settles ungated,
    // so a declined ask costs that turn rather than the whole budget.
    const pending = await insist<PendingInteraction>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(sessionId, INSIST_NUDGE);
      },
      probe: () =>
        api.waitForPendingInteractionOrSettledTurn(sessionId, PROBE_TIMEOUT_MS),
      what: "deepseek-chat raised no tool-permission interaction for a Write under permission_mode 'default' " + '(the turn finished with stop, or never reached the tool) — no pending interaction to stop/deny; ',
      budgetMs: PROBE_TIMEOUT_MS * 2,
      probeMs: PROBE_TIMEOUT_MS,
    });
    const interaction = pending as PendingInteraction;

    // ── The pending interaction (API projection): a tool permission bound to the
    //    turn this spec is about to stop. No pixel — no testid renders an
    //    interaction id or a turn id, and the durable assertions below need both. ──
    expect(interaction.presentation, 'pending interaction should be a tool approval').toBe('tool_approval');
    const turnId = String(interaction.turn_id || '').trim();
    expect(turnId, 'pending interaction should bind to a turn').not.toEqual('');
    const interactionId = String(interaction.interaction_id || '').trim();
    expect(interactionId, 'pending interaction should carry an interaction_id').not.toEqual('');
    const toolCallId = String(interaction.tool_call_id || '').trim();
    expect(toolCallId, 'the pending permission must expose its native tool identity').not.toBe('');
    const toolName = String(interaction.tool_name || '').trim();
    expect(['Write', 'Bash'], 'the pending permission must gate a filesystem write').toContain(toolName);

    await expect.poll(async () => {
      const pendingMessages = visibleMessages(await api.getMessages(sessionId, 50));
      return pendingMessages.filter((message) => message.turn_id === turnId)
        .flatMap((message) => message.blocks || [])
        .filter((block) => block.type === 'tool_use' && block.id === toolCallId);
    }, {
      message: 'the active transcript must retain exactly the tool whose permission is pending',
    }).toHaveLength(1);
    const pendingMessages = visibleMessages(await api.getMessages(sessionId, 50));
    const pendingTool = pendingMessages.filter((message) => message.turn_id === turnId)
      .flatMap((message) => message.blocks || [])
      .find((block) => block.type === 'tool_use' && block.id === toolCallId)!;
    expect(pendingTool.name).toBe(toolName);
    expect(JSON.stringify(pendingTool.input), 'the gated tool must target the requested file').toContain(targetPath);
    await expect(api.downloadFileText(sessionId, targetPath),
      'the file must not exist while its write is waiting for permission',
    ).rejects.toThrow(/files\/download -> 404:/);

    // ── It reaches the USER, and it BLOCKS them: the pending panel replaces the
    //    composer, so there is nothing to type into — the only ways forward are
    //    the card's own buttons. This is the state the stop has to resolve. ────
    const panel = page.getByTestId('pending-interaction-panel');
    await expect(
      panel,
      'the raised tool permission must reach the user as the composer pending panel',
    ).toBeVisible({ timeout: 60_000 });
    await expect(panel).toContainText(toolName);
    await expect(page.getByTestId('session-conversation-shell'),
      'the pending panel and transcript must refer to the same native tool',
    ).toHaveAttribute('data-pending-tool-call-id', toolCallId);
    await expect(
      page.getByTestId('composer-prompt'),
      'a blocking permission takes the composer away — the user cannot type past it',
    ).toHaveCount(0);
    const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
    await expect(
      pill,
      'the header must say the conversation is waiting on the user, not still working',
    ).toHaveAttribute('data-state', 'WAITING_INPUT', { timeout: 30_000 });

    // ── THE action: the user presses STOP on the blocking card (not "reject").
    //    The platform answers the pending native permission with a DENY (reject,
    //    interrupt) rather than tearing the turn down. ─────────────────────────
    const stopControl = panel.getByRole('button', { name: STOP_CONTROL });
    await expect(stopControl, 'the blocking card should offer a stop control').toBeEnabled({ timeout: 30_000 });
    const beforeStopFrameCounts = (await aiStreamBodies(page))
      .map(({ text }) => streamPayloads(text).length);
    await stopControl.click();

    // ── What the user gets back. The card clears because the interaction was
    //    ANSWERED (a dangling pending interaction would leave it standing), and
    //    the header settles. Both attributes are asserted: `data-pulse` alone
    //    proves nothing here, because a turn parked on an interaction already
    //    reads pulse=false — leaving WAITING_INPUT for READY is the half that
    //    says the denied turn actually ended. ──────────────────────────────────
    await expect(
      panel,
      'stopping must clear the blocking card — a card left standing is a session no user can unblock',
    ).toBeHidden({ timeout: SETTLE_BUDGET_MS });
    await expect(
      pill,
      'the denied turn must settle: the header leaves WAITING_INPUT for READY (the pixel of conversation_state IDLE)',
    ).toHaveAttribute('data-state', 'READY', { timeout: SETTLE_BUDGET_MS });
    await expect(pill, 'a settled turn stops pulsing').toHaveAttribute('data-pulse', 'false', {
      timeout: SETTLE_BUDGET_MS,
    });

    // The conversation is usable again — the user-visible face of "the turn
    // settled and nothing is holding the session". (An EMPTY composer keeps
    // submit disabled by design — type first, then assert.)
    const composer = page.getByTestId('composer-prompt');
    await expect(composer, 'the composer must come back once the permission is answered').toBeEnabled({
      timeout: 30_000,
    });
    await composer.fill('follow-up');
    await expect(page.getByTestId('composer-submit')).toBeEnabled({ timeout: 15_000 });

    // The stopped turn is still in the transcript, and it did not land there as
    // a failure. Asserted after the pill settled, so the transcript is final —
    // a negative text assertion on a still-streaming page proves nothing.
    const assistantMessages = page.getByTestId('assistant-message');
    // The AI SDK can retain a zero-height assistant placeholder from stream
    // setup; the current turn is the last assistant message, not the first.
    await expect(
      assistantMessages.last(),
      'the stopped turn still renders an assistant message — the denial is not a silent drop',
    ).toBeVisible({ timeout: 30_000 });
    await expect(assistantMessages.last()).not.toContainText(TURN_ERROR);
    await expect(
      assistantMessages.filter({ hasText: INTERRUPTED_FAILURE }),
      'stop must deny the permission, never surface to the user as an interrupted/failed turn',
    ).toHaveCount(0);

    // ── Durable settle: the denied turn ends. The terminal frame is written
    //    before the snapshot that points at it, so observing the proof makes
    //    the whole settled state durable and race-free. ─────────────────────────
    const committed = await waitForTurnTerminalProof(sessionId, turnId, 'COMPLETED', TERMINAL_PROOF_BUDGET_MS);
    expect(
      Boolean(committed.active_interaction_id),
      'settling the denied turn clears the active-interaction pointer',
    ).toBe(false);

    // The turn settled on a real terminal `finish` — a denied native permission
    // still ends the turn cleanly (COMPLETED), it is not left hanging.
    const terminalProof =
      committed.last_turn_terminal_frame && typeof committed.last_turn_terminal_frame === 'object'
        ? (committed.last_turn_terminal_frame as Record<string, unknown>)
        : {};
    expect(
      String(terminalProof.type || ''),
      'the denied turn settles on a durable terminal finish frame',
    ).toBe('finish');

    // ── Durable snapshot: the conversation is back to IDLE with no active
    //    interaction, and the turn settled COMPLETED with no error — the stop
    //    denied the permission and settled the turn, it did NOT fail it. The
    //    settled pill above is the user-visible face of the same state; these
    //    are the parts of it with no pixel — which turn settled, under what
    //    status, and what a restart would rehydrate. ───────────────────────────
    const snapshot = snapshotDoc(sessionId);
    expect(snapshot, 'a session snapshot should exist after the denied turn settles').not.toBeNull();
    expect(
      String(snapshot?.last_turn_id || ''),
      'the settled snapshot reflects the stopped turn',
    ).toBe(turnId);
    expect(
      String(snapshot?.conversation_state || ''),
      'the conversation returns to IDLE after the denied turn settles',
    ).toBe('IDLE');
    expect(
      Boolean(snapshot?.active_interaction_id),
      'the settled snapshot clears the active-interaction pointer',
    ).toBe(false);
    expect(
      String(snapshot?.last_turn_status || ''),
      'a denied-then-settled turn is COMPLETED, never INTERRUPTED/FAILED',
    ).toBe('COMPLETED');
    expect(snapshot?.last_turn_error, 'a denied-then-settled turn carries no turn error').toBeFalsy();

    // The settled history must retain both sides of this exact native tool call.
    const frames = framesForTurn(turnId).map(framePayload);
    const frameTypes = frames.map((frame) => String(frame.type || ''));
    const messagePage = await api.getMessages(sessionId, 50);
    const stoppedBlocks = messagePage.messages.filter((message) => message.turn_id === turnId)
      .flatMap((message) => message.blocks || []);
    const matchingUses = stoppedBlocks.filter((block) => block.type === 'tool_use' && block.id === toolCallId);
    const matchingResults = stoppedBlocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === toolCallId);
    expect(matchingUses, 'history must retain the exact interrupted tool identity once').toHaveLength(1);
    expect(matchingUses[0]).toEqual(pendingTool);
    expect(matchingResults, 'Stop must reject the same tool once instead of executing it').toHaveLength(1);
    expect(matchingResults[0].is_error).toBe(true);
    expect(matchingResults[0].tool_result_state).toBe('output-denied');
    await expect.poll(() => {
      const rows = documentsByField('transcript_entries', '$.platform_session_id', sessionId);
      const nativeBlocks = rows.flatMap((row) => {
        expect(typeof row.entry_json).toBe('string');
        const entry = JSON.parse(String(row.entry_json)) as Record<string, unknown>;
        const message = entry.message as Record<string, unknown> | undefined;
        if (!Array.isArray(message?.content)) return [];
        return (message.content as Record<string, unknown>[])
          .map((block) => ({ role: message.role, block }));
      });
      return {
        uses: nativeBlocks.filter(({ block }) => block.type === 'tool_use' && block.id === toolCallId),
        results: nativeBlocks.filter(({ block }) => block.type === 'tool_result' && block.tool_use_id === toolCallId),
      };
    }, {
      message: 'the database-backed native Store must contain the same real tool use and its rejection once',
    }).toEqual({
      uses: [{ role: 'assistant', block: expect.objectContaining({
        type: 'tool_use', id: toolCallId, name: toolName, input: pendingTool.input,
      }) }],
      results: [{ role: 'user', block: expect.objectContaining({
        type: 'tool_result', tool_use_id: toolCallId, is_error: true,
      }) }],
    });
    expect(frames.some((frame) => frame.toolCallId === toolCallId
      && (frame.type === 'tool-output-denied' || frame.type === 'tool-output-error')),
    'the durable stream must carry the stopped tool result, not an unrelated denial').toBe(true);
    expect(frameTypes, 'Stop must not persist a fatal stream error').not.toContain('error');
    await expect(api.downloadFileText(sessionId, targetPath),
      'the denied write must still be absent after the turn settles',
    ).rejects.toThrow(/files\/download -> 404:/);

    await expect.poll(async () => {
      const bodies = await aiStreamBodies(page);
      return bodies.some(({ text }, index) => streamPayloads(text)
        .slice(beforeStopFrameCounts[index] ?? 0)
        .some((frame) => frame.type === 'finish' && frame.finishReason === 'stop'));
    }, {
      message: 'the browser must receive a new clean finish after the user presses Stop',
    }).toBe(true);
    const browserFrames = (await aiStreamBodies(page)).flatMap(({ text }) => streamPayloads(text));
    expect(browserFrames.filter((frame) => frame.type === 'error'),
      'a user Stop must not poison the browser stream with a fatal error',
    ).toEqual([]);

    // The denied turn still records a terminal `finish` in the durable frame
    // stream — the settle is real, not a projection-only fabrication.
    expect(
      frameTypes,
      'the denied turn still emits a terminal finish in the durable frames',
    ).toContain('finish');
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
