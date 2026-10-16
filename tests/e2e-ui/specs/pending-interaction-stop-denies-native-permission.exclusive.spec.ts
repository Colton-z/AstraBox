/**
 * E2E: stopping a response at a tool-permission prompt rejects the tool and lets
 * the conversation settle normally.
 *
 * The page sends a default-mode Write request, waits for the approval panel, and
 * uses that panel's stop control. The panel must clear, the composer must return,
 * and the header must move from WAITING_INPUT to READY. The platform's records
 * must show the response ended as a user stop (`cancelled`, not `failed`) with
 * no error, the tool's rejection relayed once, and the interaction closed without
 * an answer; the native transcript must hold the tool use and its rejection once.
 *
 * Tool invocation is model-dependent. A bounded probe skips only when no
 * permission interaction appears; a raised interaction that the page cannot show
 * remains a failure.
 */
import { test, expect, type Page } from '@playwright/test';

import { insist } from '../fixtures/insist';
import {
  AstraApi,
  messageToolCalls,
  visibleMessages,
  type PendingInteraction,
  type ToolCallPart,
} from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import {
  oracleDbPath,
  sessionInteractionRows,
  sessionOutputRows,
  sessionResponseRows,
  type SessionResponseRow,
} from '../fixtures/dbOracle';
import { sessionStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

// One sandbox provision + a paused response + the interrupt-deny continuation +
// the recorded end sit above the 240s suite default; keep it generous and
// env-tunable.
// Bounded probe: how long to wait for a pending interaction before skipping.
// Sending from the composer returns with the receipt, so this budget spans the
// model's run up to the pause. Left at the suite-wide 60s the whole
// pending-interaction family shares — a model too slow to ask inside it skips,
// and the skip reason says which of the two happened.
/** What a user says when a model answers instead of acting. */
const INSIST_NUDGE =
  "You answered without using the tool. Do the tool call now, exactly as asked above, before replying again. \u8bf7\u73b0\u5728\u5c31\u6309\u4e0a\u9762\u7684\u8981\u6c42\u8c03\u7528\u5de5\u5177\uff0c\u4e0d\u8981\u53ea\u7528\u6587\u5b57\u56de\u7b54\u3002";

const PROBE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_PENDING_PROBE_TIMEOUT_MS', 60_000);
// After the interrupt-deny, how long to let the resumed response settle back to READY.
const SETTLE_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_PENDING_STOP_SETTLE_TIMEOUT_MS', 180_000);
// How long to wait for the stopped response's end to be recorded.
const TERMINAL_PROOF_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TERMINAL_TIMEOUT_MS', 120_000);

// The pending card's own stop control. SessionPendingInteractionFooter hands
// PromptInputSubmit to the card as `stopControl`; PromptInputSubmit localizes its
// accessible name, so name both states in both locales the console ships. Keep
// this role-and-name locator: it proves the icon-only control is reachable to a
// reader, not merely present in the DOM.
// Deliberately NOT the card's "Reject this action" button: rejecting ANSWERS the
// interaction through POST /interactions/{id}/answer, while stop goes through
// POST /interrupt — and the interrupt path is the one under test.
const STOP_CONTROL = /^(Stop generating|Stopping|停止生成|停止中)$/i;
// A FAILED response renders its error INTO the transcript as an assistant
// message, so "there is an assistant bubble" is satisfied by exactly a failure.
const TURN_ERROR = /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;
// The line a stopped response carries (chat:process.stopped), in both locales.
// A user stop ends the response with the outcome `cancelled`, and this line is
// how the transcript says so, on the open page and after a reload.
const STOP_LINE = /^(This response stopped early\.|本次回复提前结束。)$/;
// A failed response renders a ResponseFailureCard (ResultCards.tsx), an
// ErrorNote with role=alert, when its outcome is `failed`. A user stop ends the
// response as `cancelled` instead, so no alert in the stopped response's bubble
// is the user-visible half of "stop denied the permission, it did not fail the
// response"; the recorded outcome below stays the authority.

/**
 * The content blocks of the session's native transcript, as the admin route
 * serves the SessionStore copy: one JSONL file for a session without
 * sub-agent transcripts, which a single Write never has.
 */
async function nativeTranscriptBlocks(
  api: AstraApi,
  sessionId: string,
): Promise<Array<{ role: unknown; block: Record<string, unknown> }>> {
  const raw = await api.adminSessionTranscript(sessionId);
  // A gzip archive starts with byte 0x1f; the main transcript alone is JSONL.
  expect(raw.charCodeAt(0), 'a single Write leaves one main transcript, not an archive').not.toBe(0x1f);
  return raw.split('\n').filter((line) => line.trim()).flatMap((line) => {
    const entry = JSON.parse(line) as Record<string, unknown>;
    const message = entry.message as Record<string, unknown> | undefined;
    if (!Array.isArray(message?.content)) return [];
    return (message.content as Record<string, unknown>[]).map((block) => ({ role: message.role, block }));
  });
}

/** The recorded response, once it has ended. */
async function waitForEndedResponse(
  sessionId: string,
  responseId: string,
  timeoutMs: number,
): Promise<SessionResponseRow> {
  const deadline = Date.now() + timeoutMs;
  let last: SessionResponseRow | undefined;
  while (Date.now() < deadline) {
    last = sessionResponseRows(sessionId).find((row) => row.response_id === responseId);
    if (last?.status === 'ended') return last;
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(
    `response ${sessionId}/${responseId} was not recorded as ended within ${timeoutMs}ms; ` +
      `last row=${JSON.stringify(last ?? null)}`,
  );
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

  // Fail fast (with the descriptive oracle error) if the database is not
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

    // ── The user asks for one Write; the response pauses on the tool permission.
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
    //    response this spec is about to stop. No pixel — no testid renders an
    //    interaction id or a response id, and the durable assertions below need
    //    both. ──────────────────────────────────────────────────────────────────
    expect(interaction.presentation, 'pending interaction should be a tool approval').toBe('tool_approval');
    const responseId = String(interaction.response_id || '').trim();
    expect(responseId, 'pending interaction should name the response it pauses').not.toEqual('');
    const interactionId = String(interaction.interaction_id || '').trim();
    expect(interactionId, 'pending interaction should carry an interaction_id').not.toEqual('');
    const toolCallId = String(interaction.tool_call_id || '').trim();
    expect(toolCallId, 'the pending permission must expose its native tool identity').not.toBe('');
    const toolName = String(interaction.tool_name || '').trim();
    expect(['Write', 'Bash'], 'the pending permission must gate a filesystem write').toContain(toolName);

    // The response's history message (its id is the response id) carries the
    // gated call as a tool part.
    const gatedCalls = async (): Promise<ToolCallPart[]> => visibleMessages(await api.getMessages(sessionId, 50))
      .filter((message) => message.id === responseId)
      .flatMap((message) => messageToolCalls(message))
      .filter((call) => call.toolCallId === toolCallId);
    await expect.poll(gatedCalls, {
      message: 'the paused response must retain exactly the tool whose permission is pending',
    }).toHaveLength(1);
    const pendingTool = (await gatedCalls())[0];
    expect(pendingTool.toolName).toBe(toolName);
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
    //    The interrupt reaches the runtime, which denies the pending native
    //    permission and stops the response rather than tearing it down. ────────
    const stopControl = panel.getByRole('button', { name: STOP_CONTROL });
    await expect(stopControl, 'the blocking card should offer a stop control').toBeEnabled({ timeout: 30_000 });
    const beforeStopFrameCounts = (await sessionStreamBodies(page))
      .map(({ text }) => streamPayloads(text).length);
    await stopControl.click();

    // ── What the user gets back. The card clears because the interaction
    //    closed with the response (one left open would leave it standing), and
    //    the header settles. Both attributes are asserted: `data-pulse` alone
    //    proves nothing here, because a response paused on an interaction
    //    already reads pulse=false — leaving WAITING_INPUT for READY is the half
    //    that says the stopped response actually ended. ───────────────────────
    await expect(
      panel,
      'stopping must clear the blocking card — a card left standing is a session no user can unblock',
    ).toBeHidden({ timeout: SETTLE_BUDGET_MS });
    await expect(
      pill,
      'the stopped response must settle: the header leaves WAITING_INPUT for READY',
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

    // The stopped response is still in the transcript, and it did not land
    // there as a failure. Asserted after the pill settled, so the transcript is
    // final — a negative text assertion on a still-streaming page proves nothing.
    const stopped = page.locator(`[data-message-id="${responseId}"]`).getByTestId('assistant-message');
    await expect(
      stopped,
      'the stopped response still renders as an assistant message — the denial is not a silent drop',
    ).toBeVisible({ timeout: 30_000 });
    await expect(stopped).not.toContainText(TURN_ERROR);
    await expect(
      stopped.getByRole('alert'),
      'stop must deny the permission, never surface to the user as a failed response',
    ).toHaveCount(0);
    await expect(
      stopped.getByText(STOP_LINE),
      'the stopped response must say it stopped on the open page',
    ).toBeVisible({ timeout: 30_000 });

    // ── Recorded end: the response ended as a user stop. `cancelled` is the
    //    stop's own outcome; `failed` would mean the stop broke the response
    //    instead of ending it, and an error would be that failure's report. ───
    const ended = await waitForEndedResponse(sessionId, responseId, TERMINAL_PROOF_BUDGET_MS);
    expect(ended.outcome, 'a user stop ends the response as cancelled, never failed').toBe('cancelled');
    expect(ended.error, 'a stopped response carries no error').toBeNull();

    // Stop is not an answer: the interaction closes with the response and
    // nobody's decision is recorded on it.
    const closed = sessionInteractionRows(sessionId).find((row) => row.interaction_id === interactionId);
    expect(closed, 'the interaction the response waited on is recorded').toBeTruthy();
    expect(closed?.status, 'the stopped response leaves no interaction open').not.toBe('open');
    expect(closed?.answer ?? null, 'a stop records no answer on the interaction').toBeNull();

    // The session detail is the same state as the settled header: nothing
    // waiting, nothing running, and the stopped response as the last one.
    const settled = await api.getSession(sessionId);
    expect(settled.pending_interaction ?? null, 'no interaction is left waiting on the owner').toBeNull();
    expect(settled.current_responses ?? [], 'no response is left running').toEqual([]);
    expect(settled.last_response?.response_id, 'the stopped response is the last one that ended').toBe(responseId);
    expect(settled.last_response?.outcome).toBe('cancelled');

    // The settled history must retain both sides of this exact native tool call:
    // one tool part, rejected rather than run.
    const stoppedMessage = visibleMessages(await api.getMessages(sessionId, 50))
      .find((message) => message.id === responseId);
    expect(stoppedMessage?.metadata?.status, 'history records the stopped response as ended').toBe('ended');
    expect(stoppedMessage?.metadata?.outcome).toBe('cancelled');
    const stoppedCalls = stoppedMessage ? messageToolCalls(stoppedMessage).filter((call) => call.toolCallId === toolCallId) : [];
    expect(stoppedCalls, 'history must retain the exact interrupted tool identity once').toHaveLength(1);
    expect(stoppedCalls[0].toolName).toBe(toolName);
    expect(stoppedCalls[0].input).toEqual(pendingTool.input);
    expect(
      ['output-denied', 'output-error'],
      'Stop must reject the same tool once instead of executing it',
    ).toContain(stoppedCalls[0].state);
    await expect.poll(async () => {
      const nativeBlocks = await nativeTranscriptBlocks(api, sessionId);
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

    // The parts the reader relayed for the response: the rejection, no error,
    // and the response's own `finish` as its last part.
    const relayed = sessionOutputRows(sessionId, responseId).map((row) => row.part);
    const relayedTypes = relayed.map((part) => String(part.type || ''));
    expect(relayed.some((part) => part.toolCallId === toolCallId
      && (part.type === 'tool-output-denied' || part.type === 'tool-output-error')),
    'the recorded output must carry the stopped tool result, not an unrelated denial').toBe(true);
    expect(relayedTypes, 'Stop must not record a fatal stream error').not.toContain('error');
    expect(relayedTypes.at(-1), 'the stopped response ends on its own finish part').toBe('finish');
    await expect(api.downloadFileText(sessionId, targetPath),
      'the denied write must still be absent after the response settles',
    ).rejects.toThrow(/files\/download -> 404:/);

    // The browser hears the response end after the stop: a `finish` that is
    // not another pause, on the stream the page already follows.
    await expect.poll(async () => {
      const bodies = await sessionStreamBodies(page);
      return bodies.some(({ text }, index) => streamPayloads(text)
        .slice(beforeStopFrameCounts[index] ?? 0)
        .some((frame) => frame.type === 'finish' && frame.finishReason !== 'tool-calls'));
    }, {
      message: 'the browser must receive the stopped response\'s finish after the user presses Stop',
    }).toBe(true);
    const browserFrames = (await sessionStreamBodies(page)).flatMap(({ text }) => streamPayloads(text));
    expect(browserFrames.filter((frame) => frame.type === 'error'),
      'a user Stop must not poison the browser stream with a fatal error',
    ).toEqual([]);

    // A reload reads only the durable record, so the stop line there proves
    // the stop was recorded, not merely rendered from the live stream.
    await openSessionView(page, sessionId);
    await expect(
      page.locator(`[data-message-id="${responseId}"]`).getByText(STOP_LINE),
      'the reloaded page must still say the stopped response stopped',
    ).toBeVisible({ timeout: 45_000 });
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
