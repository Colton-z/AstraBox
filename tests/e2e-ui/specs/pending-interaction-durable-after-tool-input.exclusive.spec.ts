/**
 * E2E: a tool-permission interaction becomes durable only after the matching
 * tool input is complete.
 *
 * A default-mode Write request must pause on the approval card. In durable frame
 * order, `tool-input-available` for the tool call must precede the matching
 * `data-interaction`. Approving from the page resumes the same turn, clears the
 * active interaction, and records a later terminal `finish`. The page also proves
 * that the parked interaction remains answerable after the idle window.
 *
 * Exact frame order and identifiers remain persistence assertions because the UI
 * does not render them. Tool invocation is model-dependent, so a bounded probe
 * skips only when no permission interaction is raised.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, type PendingInteraction } from '../fixtures/astraApi';
import { insist } from '../fixtures/insist';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import {
  framesForTurn,
  oracleDbPath,
  waitForTurnSnapshot,
  waitForTurnTerminalProof,
} from '../fixtures/dbOracle';

// One sandbox provision + a paused turn + the answer continuation + the durable
// terminal settle sit above the 240s suite default; keep it generous/tunable.
// How long to wait for the answered turn's terminal proof to become durable.
const TERMINAL_PROOF_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TERMINAL_TIMEOUT_MS', 120_000);
// Bounded budget for the lag between a durable write and the surface that
// exposes it — the projection the pending API reads, and the frame table the
// durable oracle reads. The value is tuned for exactly that projection lag: it
// assumes the interaction's segment `finish` POST has already completed by the
// time the probe starts. With the PAGE owning the send there is no such
// boundary (see PAGE_PROBE_BUDGET_MS), so the constant keeps the job its name
// and value were tuned for rather than also standing in for the model's thinking.
const PROBE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_PENDING_PROBE_TIMEOUT_MS', 60_000);
// The window from dispatch to the model actually reaching for the tool and the
// turn parking on it.
const TIME_TO_GATE_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
// Two windows in sequence — the turn, then the probe — under a single budget: the
// click returns immediately, so everything after it is a wait. Same two tuned
// constants, composed rather than re-tuned; a probe that
// only covered projection lag would report a SKIP (the one outcome that proves
// nothing while looking like nothing is wrong) for a model that simply took a
// few seconds longer to reach for the tool.
const PAGE_PROBE_BUDGET_MS = TIME_TO_GATE_MS + PROBE_TIMEOUT_MS;
// How long the console may take to draw (or re-draw, between approvals) the
// approval card once the platform has raised the gate. Not env-tunable on
// purpose: a card that is late by a minute is a defect, not a deployment.
const PANEL_RENDER_MS = 60_000;
// The click's own POST /interaction-respond round trip.
const ANSWER_ACCEPTED_MS = 60_000;
// The answered continuation is a real model turn (the write, then the reply);
// budget the header settle like one.
const CONTINUATION_SETTLE_MS = 240_000;
// Let the WAITING turn sit before answering: a parked turn's worker exits at the
// interaction boundary BY DESIGN, so its heartbeat goes stale while it waits on a
// human, and reconcile must NOT reclaim it. The window lives between two real
// bounds — it has to EXCEED the reconcile stale threshold
// (ASTRABOX_RECONCILE_HEARTBEAT_STALE_S, default 20s) or it proves nothing, and
// stay well inside the in-box approval budget (DEFAULT_INTERACTION_WAIT_S, 120s)
// or the gate defers and the answer is legitimately refused as expired.
const WAITING_IDLE_MS = parseTimeoutEnv('ASTRABOX_E2E_PENDING_WAITING_IDLE_MS', 30_000);
// A resumed turn may gate another tool; drain the chain rather than assume one
// approval ends it. Bounded so a model that gates forever fails loudly.
const MAX_APPROVALS = 6;
// How long to wait for a resumed turn to gate its NEXT tool before concluding
// it is finishing instead. Short: a resumed turn gates again promptly or not
// at all, and the terminal wait below carries the real budget.
const NEXT_PAUSE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_NEXT_PAUSE_TIMEOUT_MS', 45_000);

// The affirmative primary button of the tool-permission card
// (misc:composer.allow_continue / apply_suggestion_continue — en/zh; the console
// is bilingual and the runner's default locale is en-US).
const APPROVE_CONTINUE = /Allow and continue|Apply suggestion and continue|允许并继续|应用建议并继续/;
// A FAILED turn renders its error INTO the transcript as an assistant message,
// so "there is a reply bubble" is satisfied by exactly a failure.
const TURN_ERROR = /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

/**
 * The next pause the turn has NOT been answered for yet, or null within the budget.
 *
 * Sampling the pending projection right after an approval reads back the one just
 * answered — its engine-side wait is already released, so re-answering it is a
 * legitimate 409 (measured: the platform logged the successful answer and the
 * duplicate one millisecond apart, while the real next pause landed two seconds
 * later). Identity, not presence, is what tells a new pause from a stale read.
 *
 * Driven from the page the same identity check earns a second job: it gates the
 * next CLICK. The console hides an answered card immediately (optimistic
 * suppression) and releases the suppression only once a DIFFERENT interaction is
 * authoritative, so waiting for a genuinely new id here is what makes "the panel
 * is back" mean "it is offering the next gate" rather than "the answer failed".
 */
async function waitForNewPause(
  api: AstraApi,
  sessionId: string,
  answered: Set<string>,
  timeoutMs: number,
): Promise<PendingInteraction | null> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const pending = await api.getPendingInteraction(sessionId);
    const id = String(pending?.interaction_id || '').trim();
    if (id && !answered.has(id)) return pending;
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
  return null;
}

/** The canonical engine frame a durable `session_events` row stores under `payload`. */
function framePayload(frame: Record<string, unknown>): Record<string, unknown> {
  const payload = frame.payload;
  return payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {};
}

/** The `data` ride-along of a `data-interaction` frame (the pending interaction). */
function frameData(payload: Record<string, unknown>): Record<string, unknown> {
  const data = payload.data;
  return data && typeof data === 'object' ? (data as Record<string, unknown>) : {};
}

/**
 * The pause's two durable frames, once BOTH are in `session_events`.
 *
 * The API port read the frame table the instant `streamPrompt` returned — a POST
 * that had already run to the interaction's segment `finish` — so "the frames are
 * written" was true before the first read. Driven from the page there is no such
 * boundary, and commit-before-emit puts the PROJECTION (what the pending API
 * exposes) AHEAD of the frame append: sampling the table at the moment the
 * interaction becomes readable races a write that is legitimately still in flight.
 *
 * Waiting does not soften the ordering assertion at the call site. What is
 * asserted there is the order the platform durably ASSIGNED — `event_seq` — not
 * the order this oracle happened to observe; the wait only decides when it is
 * fair to read. A frame that never lands throws with the durable frame types the
 * turn recorded, preserving the evidence needed to diagnose the missing pair.
 */
async function waitForPauseFrames(
  turnId: string,
  toolCallId: string,
  interactionId: string,
  timeoutMs: number,
): Promise<{ toolInput: Record<string, unknown>; dataInteraction: Record<string, unknown> }> {
  const deadline = Date.now() + timeoutMs;
  let seenTypes: string[] = [];
  for (;;) {
    const frames = framesForTurn(turnId);
    const toolInput = frames.find((frame) => {
      const payload = framePayload(frame);
      return (
        String(payload.type || '') === 'tool-input-available' &&
        String(payload.toolCallId || '') === toolCallId
      );
    });
    const dataInteraction = frames.find((frame) => {
      const payload = framePayload(frame);
      return (
        String(payload.type || '') === 'data-interaction' &&
        String(frameData(payload).interaction_id || '') === interactionId
      );
    });
    if (toolInput && dataInteraction) return { toolInput, dataInteraction };
    seenTypes = frames.map((frame) => String(framePayload(frame).type || ''));
    if (Date.now() >= deadline) break;
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(
    `turn ${turnId} did not durably record BOTH the closed tool input ` +
      `(tool-input-available toolCallId=${toolCallId}) and the pending interaction ` +
      `(data-interaction interaction_id=${interactionId}) within ${timeoutMs}ms; ` +
      `durable frame types were [${seenTypes.join(', ')}]`,
  );
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('pending interaction is durable only after tool input is available', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = Date.now();

  // Fail fast (with the descriptive oracle error) if the document store is not
  // reachable — the durable half of this spec depends on it.
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  const panel = page.getByTestId('pending-interaction-panel');
  // Scoped to `run-view`, not a bare `status-pill.first()`: the sidebar renders a
  // `status-pill` for EVERY conversation in the list (App.tsx SidebarMenu) and it
  // precedes the route outlet in the DOM, so `.first()` binds to some OTHER
  // conversation's row — whose `data-state` is the session's lifecycle state and
  // is therefore NEVER WAITING_INPUT. That mis-binding fails as "the header never
  // said the turn was waiting", i.e. it reads as the product defect this spec
  // exists to catch. SessionHeader's pill is the only one inside `run-view`.
  const pill = page.getByTestId('run-view').getByTestId('status-pill').first();

  /**
   * Approve the gate the way a user does: press the card's affirmative button.
   *
   * The acceptance proof is read off the console's OWN POST rather than off the
   * card disappearing, because the card disappears optimistically at the click
   * (see the header). The response also carries the interaction_id, which is the
   * only place the button the user pressed and the id the durable oracle follows
   * can be tied together — nothing renders an interaction id.
   */
  const approveFromPanel = async (expectedInteractionId: string) => {
    const approve = panel.getByRole('button', { name: APPROVE_CONTINUE }).last();
    await expect(
      approve,
      'the approval card must offer the user an enabled affirmative button',
    ).toBeEnabled({ timeout: PANEL_RENDER_MS });
    const accepted = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' && response.url().includes('/interaction-respond'),
      { timeout: ANSWER_ACCEPTED_MS },
    );
    await approve.click();
    const response = await accepted;
    expect(
      response.status(),
      'the approval the user pressed must be accepted by the platform',
    ).toBe(200);
    const body = (await response.json()) as Record<string, unknown>;
    const result = (body.data ?? body) as { answered?: unknown; interaction_id?: unknown };
    expect(
      Boolean(result.answered),
      'approving the interaction should be accepted',
    ).toBe(true);
    expect(
      String(result.interaction_id || ''),
      'the console must answer the interaction the card was showing, not a stale one',
    ).toBe(expectedInteractionId);
  };

  try {
    await api.waitForSessionReady(sessionId);
    // ARRANGE the permission mode before the page opens: the console dispatches
    // under the mode it read at LOAD, so a page opened first would send in
    // bypass and nothing would ever gate.
    await api.setPermissionMode(sessionId, 'default');

    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });

    // ── ACT: the user asks for one Write, from the real composer. `.fill()` sets
    //    the value without key events, so the prompt is not submitted early.
    //
    //    Asked again if the model answers without reaching for Write. This is
    //    SETUP, not a test retry: the subject below is what the platform does
    //    with a pending interaction, and it cannot be observed without one.
    //    Skipping when the model declines Write would leave that behaviour
    //    unverified. ─────────────────────────────────────────────────────────
    const marker = `E2E pending interaction ${runId}`;
    const askForOneWrite = async (): Promise<void> => {
      const composer = page.getByTestId('composer-prompt');
      await expect(composer, 'the composer must be enabled before sending').toBeEnabled({ timeout: 45_000 });
      await composer.fill(
        `${marker}: 请使用 Write 工具创建相对路径 e2e-pending-${runId}.txt，` +
          `内容写入 "pending interaction e2e"。只创建这一个文件，然后等待我确认，不要继续其它操作。`,
      );
      const submit = page.getByTestId('composer-submit');
      await expect(submit).toBeEnabled({ timeout: 15_000 });
      await submit.click();
      await expect(
        page.getByTestId('user-message').filter({ hasText: marker }).last(),
        'the user bubble should render — proof the turn was dispatched from the page',
      ).toBeVisible({ timeout: 30_000 });
    };

    await askForOneWrite();

    // Kept on the API: what is being insisted on is the MODEL reaching for a
    // gated tool. An interaction that IS raised and never reaches the screen
    // must go red on the page assertion below, not be re-asked here.
    // The probe returns as soon as the turn settles ungated, so a declined ask
    // costs the seconds that turn took rather than the whole probe budget --
    // which is what makes a second ask fit inside the lane's per-test wall.
    const interaction = await insist<PendingInteraction>({
      ask: async (attempt) => {
        if (attempt > 1) await askForOneWrite();
      },
      probe: () =>
        api.waitForPendingInteractionOrSettledTurn(sessionId, PROBE_TIMEOUT_MS),
      what:
        "no tool-permission interaction was raised for a Write under " +
        "permission_mode 'default'",
      budgetMs: PROBE_TIMEOUT_MS * 2,
      probeMs: PROBE_TIMEOUT_MS,
    });

    // ── The pending interaction (API projection): a tool permission bound to a
    //    turn, exposing the real tool_call_id. ─────────────────────────────────
    expect(interaction.presentation, 'pending interaction should be a tool approval').toBe('tool_approval');
    const turnId = String(interaction.turn_id || '').trim();
    expect(turnId, 'pending interaction should bind to a turn').not.toEqual('');
    const toolCallId = String(interaction.tool_call_id || '').trim();
    expect(toolCallId, 'pending interaction should expose the real tool_call_id').not.toEqual('');
    const interactionId = String(interaction.interaction_id || '').trim();
    expect(interactionId, 'pending interaction should carry an interaction_id').not.toEqual('');
    // The page's identity anchor, taken from the SERVER rather than from what the
    // prompt asked for: whatever tool it says is gated must be the tool the card
    // names.
    const toolName = String(interaction.tool_name || '').trim();
    expect(toolName, 'a tool permission should name the tool it is gating').not.toEqual('');

    // ── It reaches the USER: the composer is REPLACED by the approval card, and
    //    the header says the turn is waiting on THEM rather than still
    //    generating. A gate that never gets this far is a session nobody can
    //    unblock, however well ordered its frames are. ──────────────────────────
    await expect(
      panel,
      'the raised permission must reach the user as the composer approval card',
    ).toBeVisible({ timeout: PANEL_RENDER_MS });
    await expect(panel, 'the card must be about the tool the platform gated').toContainText(toolName);
    await expect(page.getByTestId('session-conversation-shell'),
      'the transcript must bind to the same authoritative pending tool as the answer panel',
    ).toHaveAttribute('data-pending-tool-call-id', toolCallId);
    // File-change headers name the file, while generic tool headers name the tool.
    const rawInput = interaction.raw_input as Record<string, unknown>;
    const headerLabel = toolName === 'Write' || toolName === 'Edit'
      ? String(rawInput.file_path || '').split('/').at(-1) || ''
      : toolName;
    expect(headerLabel, 'the pending tool must expose the label rendered by its card').not.toBe('');
    const pendingToolHeader = page.getByRole('log').getByRole('button').filter({ hasText: headerLabel });
    await expect(pendingToolHeader, 'the pending invocation must identify exactly one tool header').toHaveCount(1);
    await expect(pendingToolHeader,
      'the pending tool must tell the reader it is waiting for confirmation',
    ).toHaveText(/Awaiting confirmation|等待确认/);
    // `data-state` rather than the label, because the console is bilingual and
    // the label is a translation; sessionRunStatus maps the two 1:1.
    await expect(
      pill,
      'a turn parked on a permission must read as waiting on the user, not generating',
    ).toHaveAttribute('data-state', 'WAITING_INPUT', { timeout: PANEL_RENDER_MS });

    // ── Durable state while pending: WAITING_FOR_INTERACTION + the active
    //    pointer at THIS interaction. Awaited, not sampled: the interaction
    //    becomes readable over HTTP as soon as the kernel registers it, while the
    //    snapshot naming the parked turn is a separate durable write — and with
    //    the page owning the send there is no completed POST absorbing that
    //    interval any more, so a single sample would race the two writes rather
    //    than test anything this spec is about. ─────────────────────────────────
    const paused = await waitForTurnSnapshot(sessionId, turnId);
    expect(
      String(paused.conversation_state || ''),
      'the paused turn records WAITING_FOR_INTERACTION',
    ).toBe('WAITING_FOR_INTERACTION');
    expect(
      String(paused.active_interaction_id || ''),
      'the paused turn points at the pending interaction',
    ).toBe(interactionId);

    // ── CORE: durable frame ordering. The closed tool input
    //    (`tool-input-available` for the interaction's tool_call_id) must be
    //    durable BEFORE the interaction lands (`data-interaction` for its
    //    interaction_id) — the pending interaction is durable only after the
    //    tool input is available. ───────────────────────────────────────────────
    const { toolInput: toolInputFrame, dataInteraction: dataInteractionFrame } =
      await waitForPauseFrames(turnId, toolCallId, interactionId, PROBE_TIMEOUT_MS);

    // The durable data-interaction rides the same tool call as the closed input.
    expect(
      String(frameData(framePayload(dataInteractionFrame)).tool_call_id || ''),
      'the durable pending-interaction frame references the same tool_call_id as the closed tool input',
    ).toBe(toolCallId);

    const toolInputSeq = Number(toolInputFrame.event_seq);
    const dataInteractionSeq = Number(dataInteractionFrame.event_seq);
    expect(
      Number.isFinite(toolInputSeq) && Number.isFinite(dataInteractionSeq),
      'durable engine events should carry numeric event_seq',
    ).toBe(true);
    expect(
      toolInputSeq < dataInteractionSeq,
      'the pending interaction must be durable only AFTER the closed tool input ' +
        '(tool-input-available event_seq must precede the data-interaction event_seq)',
    ).toBe(true);

    // ── Let the released WAITING turn sit (stale heartbeat), then approve.
    //    A stale-lease waiting turn must still resume and commit cleanly — and,
    //    where the user is concerned, must still be ANSWERABLE when they come
    //    back to it: a card whose button has gone dead, or a header that has
    //    quietly moved on, is a reclaimed turn however the answer is then made. ─
    await page.waitForTimeout(WAITING_IDLE_MS);
    await expect(
      panel,
      'a turn parked on a human must still be on screen after the reconcile stale window',
    ).toBeVisible();
    await expect(
      pill,
      'a turn waiting on the user must not be reclaimed out from under them while they think',
    ).toHaveAttribute('data-state', 'WAITING_INPUT');
    await approveFromPanel(interactionId);

    // ── The answer continuation resumes the SAME turn and settles it. ─────────
    //    How MANY permissions the model asks for is the model's business — with
    //    deepseek behind the gateway a resumed turn regularly gates a second
    //    tool, and the session legitimately parks again. So drain the pause
    //    chain instead of assuming one approval ends the turn, and assert the
    //    load-bearing part at EVERY pause: the continuation stays on the SAME
    //    turn rather than starting a fresh one, and each next gate reaches the
    //    user as its own card (the console keeps the conversation answerable
    //    across the whole chain, not just on the first stop).
    const answered = new Set<string>([interactionId]);
    let drained = false;
    for (let approvals = 1; approvals <= MAX_APPROVALS; approvals += 1) {
      const next = await waitForNewPause(api, sessionId, answered, NEXT_PAUSE_TIMEOUT_MS);
      if (next === null) {
        drained = true;
        break;
      }
      expect(
        String(next.turn_id || ''),
        'the answered continuation must stay on the SAME turn, not start a new one',
      ).toBe(turnId);
      const nextId = String(next.interaction_id || '');
      await expect(
        panel,
        'each further gate must reach the user as an approval card too',
      ).toBeVisible({ timeout: PANEL_RENDER_MS });
      // A tool-permission chain is what this scenario produces, and the API port
      // assumed the same (it posted `decision: approve` at every pause whatever
      // the kind). A differently shaped card — a questionnaire, an exit-plan
      // confirmation — has no affirmative button, so it fails inside
      // approveFromPanel naming the missing control rather than being
      // mis-clicked. Answering those shapes belongs to the sibling
      // pending-interaction-survives-server-restart spec, which owns them.
      await approveFromPanel(nextId);
      answered.add(nextId);
    }
    expect(drained, `the turn kept gating tools past ${MAX_APPROVALS} approvals`).toBe(true);

    // ── The turn settles where the user watches it: the card is gone, the header
    //    stops pulsing, the transcript ends on a reply rather than a rendered
    //    failure, and the composer is usable again (the user-visible face of the
    //    turn releasing). ───────────────────────────────────────────────────────
    await expect(
      panel,
      'the answered approval should clear off the composer',
    ).toBeHidden({ timeout: PANEL_RENDER_MS });
    await expect(
      pill,
      'the header must settle once the resumed turn ends',
    ).toHaveAttribute('data-pulse', 'false', { timeout: CONTINUATION_SETTLE_MS });

    // Wording is never pinned — the backend is deepseek and non-deterministic.
    // A bubble on its own is not a reply either: a failed turn renders its error
    // INTO the transcript as an assistant message, so the error guard is what
    // makes "the continuation delivered" mean anything.
    const reply = page.getByTestId('assistant-message').last();
    await expect(reply, 'the resumed turn should have rendered assistant content').not.toBeEmpty();
    await expect(reply).not.toContainText(TURN_ERROR);

    // An EMPTY composer keeps submit disabled by design — type first, then assert.
    await page.getByTestId('composer-prompt').fill('follow-up');
    await expect(page.getByTestId('composer-submit')).toBeEnabled({ timeout: 15_000 });

    // ── The durable half behind the settled screen. ──────────────────────────
    await api.waitForSessionReady(sessionId);
    const committed = await waitForTurnTerminalProof(sessionId, turnId, 'COMPLETED', TERMINAL_PROOF_BUDGET_MS);
    expect(
      Boolean(committed.active_interaction_id),
      'committing the answered turn clears the active-interaction pointer',
    ).toBe(false);

    // The answered interaction is not active/pending on the session. The
    // cleared card above is the pixel, but the console suppresses it at the click
    // (see the header), so the projection is asserted as well rather than instead.
    const afterPending = await api.getPendingInteraction(sessionId);
    expect(afterPending, 'the answered interaction is no longer active/pending').toBeNull();

    // The commit records a durable terminal `finish` whose frame_seq is strictly
    // AFTER the pending-interaction frame — the answer resumed and settled the
    // same turn cleanly, with no premature/duplicated terminal at the pause.
    const terminalProof =
      committed.last_turn_terminal_frame && typeof committed.last_turn_terminal_frame === 'object'
        ? (committed.last_turn_terminal_frame as Record<string, unknown>)
        : {};
    expect(
      String(terminalProof.type || ''),
      'the committed turn ends on a durable finish frame',
    ).toBe('finish');
    const committedTerminalSeq = Number(terminalProof.frame_seq);
    expect(
      Number.isFinite(committedTerminalSeq) && committedTerminalSeq > dataInteractionSeq,
      'the durable terminal advances past the pending-interaction frame (the answer committed the same turn)',
    ).toBe(true);
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
