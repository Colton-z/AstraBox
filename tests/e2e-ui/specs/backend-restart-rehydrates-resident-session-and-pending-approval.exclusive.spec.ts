/**
 * E2E: a server-container restart restores both a warm conversation and a
 * pending tool approval from durable state.
 *
 * Restarting the server drops in-memory runtimes while the PostgreSQL database
 * and separate sandbox containers survive. After the API is ready, a full page
 * navigation must restore the warm transcript on the same sandbox and accept a
 * new turn. A second session must restore its approval card; allowing the action
 * must clear the interaction and commit a terminal finish on the same turn.
 *
 * The test resolves the server through the shared deployment-service handle. It
 * is exclusive because the restart affects every session on the host. The
 * approval half uses a bounded model capability probe before restarting.
 */
import { execFileSync } from 'node:child_process';

import { test, expect } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi, type PendingInteraction } from '../fixtures/astraApi';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import {
  oracleDbPath,
  turnSnapshot,
  waitForTurnSnapshot,
  waitForTurnTerminalProof,
} from '../fixtures/dbOracle';
import {
  requireServiceContainer,
  SERVER_CONTAINER_HANDLE,
} from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

// A failing restart-rehydration run must keep its sessions: the pending
// interaction and the turn snapshot ARE the evidence.
/** What a user says when a model answers instead of acting. */
const INSIST_NUDGE =
  "You answered without using the tool. Do the tool call now, exactly as asked above, before replying again. \u8bf7\u73b0\u5728\u5c31\u6309\u4e0a\u9762\u7684\u8981\u6c42\u8c03\u7528\u5de5\u5177\uff0c\u4e0d\u8981\u53ea\u7528\u6587\u5b57\u56de\u7b54\u3002";

const sessions = trackSessions();

// The agent is released on the passing path too. Hooks register at file
// scope, so the id lives here and the test assigns it.
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

// Two provisions + a warm turn + a paused turn + a full server restart + rehydration
// + the answer continuation sit well above the 240s suite default; keep it generous
// and env-tunable. A backend restart adds real wall-clock over the sibling specs.
// How long to poll GET /sessions/{id} for the server to resume serving after the
// restart (connection refused / 502 during boot is expected and retried).
const SERVER_BACK_MS = parseTimeoutEnv('ASTRABOX_E2E_SERVER_RESTART_BACK_MS', 180_000);
// One model turn dispatched from the composer: the composer click returns
// immediately, so this budget bounds the wall-clock the spec waits through
// rather than the duration of a blocking `streamPrompt` call.
const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
// Bounded probe: the lag between the durable write and the projection the pending
// API reads (shared env name with the pending siblings).
const PROBE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_PENDING_PROBE_TIMEOUT_MS', 60_000);
// Two windows in sequence — the turn, then the probe — under a single budget: the
// composer click returns immediately, so everything after it is a wait. Same two
// tuned constants, composed rather than re-tuned: a probe that
// only covered projection lag would report a SKIP (the one outcome that proves
// nothing while looking like nothing is wrong) for a model that simply took a few
// seconds longer to reach for the tool.
const PAGE_PROBE_BUDGET_MS = TURN_TIMEOUT_MS + PROBE_TIMEOUT_MS;
// How long to wait for the durable pending interaction to re-surface after the
// restart (it is durable, so this only covers rehydration + projection lag) — on
// the screen as well as on the API.
const PENDING_RESURFACE_MS = parseTimeoutEnv('ASTRABOX_E2E_PENDING_RESURFACE_TIMEOUT_MS', 90_000);
// How long to wait for the answered-post-restart turn to reach a durable terminal.
const TERMINAL_PROOF_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TERMINAL_TIMEOUT_MS', 120_000);
// `docker restart` grace: SIGTERM → (drain) → SIGKILL then start again.
const RESTART_CMD_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_SERVER_RESTART_CMD_TIMEOUT_MS', 120_000);
// How long to wait for a resumed turn to gate its NEXT tool before concluding it is
// finishing instead (shared env name with the pending-durable sibling).
const NEXT_PAUSE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_NEXT_PAUSE_TIMEOUT_MS', 45_000);
// A resumed turn may gate another tool; drain the chain rather than assume one
// approval ends it. Bounded so a model that gates forever fails loudly.
const MAX_APPROVALS = 6;

// The console is served by the container that just restarted: a reloaded page
// re-fetches the bundle and its first API calls land on a freshly-booted backend.
// Generous, so a cold start is not reported as lost state. Not env-tunable on
// purpose — a console still blank after a minute and a half is a defect.
const REHYDRATE_RENDER_MS = 90_000;
// How long the console may take to draw (or re-draw, between approvals) the
// approval card once the platform has raised the gate.
const PANEL_RENDER_MS = 60_000;
// The click's own POST /interaction-respond round trip.
const ANSWER_ACCEPTED_MS = 60_000;
// Taken only AFTER the reply is rendered, so this is the screen's lag behind a
// finished turn — the phantom-"generating" tail a browser is the only witness to.
const SETTLE_MS = 60_000;
// The answered continuation is a real model turn (the write, then the reply);
// budget the header settle like one.
const CONTINUATION_SETTLE_MS = 240_000;

// The affirmative primary button of the tool-permission card
// (misc:composer.allow_continue / apply_suggestion_continue — en/zh; the console is
// bilingual and the runner's default locale is en-US).
const APPROVE_CONTINUE = /Allow and continue|Apply suggestion and continue|允许并继续|应用建议并继续/;
// A FAILED turn renders its error INTO the transcript as an assistant message, so
// "there is one more non-empty bubble" is satisfied by exactly the failure a lost
// restart leaves behind.
const TURN_ERROR = /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// Live rendering may split reasoning and answer frames into several assistant
// elements while a fresh transcript projection folds the same frames into one.
// Content identity survives that presentational boundary; element count does not.
const normalizeRendered = (text: string): string => text.replace(/\s+/g, '');

/** Restart the backend server container — the fault this whole scenario rests on. */
function restartServerContainer(container: string): void {
  try {
    execFileSync('docker', ['restart', container], {
      encoding: 'utf8',
      timeout: RESTART_CMD_TIMEOUT_MS,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (error) {
    const err = error as { code?: string; stderr?: Buffer | string; message?: string };
    if (err.code === 'ENOENT') {
      throw new Error(
        'backend-restart spec: the `docker` CLI was not found — this spec must run ON the box ' +
          'where the Docker daemon owns the server container (the same host posture as the sandboxOps fixture).',
      );
    }
    const stderr = typeof err.stderr === 'string' ? err.stderr : err.stderr?.toString() ?? '';
    throw new Error(`backend-restart spec: \`docker restart ${container}\` failed: ${stderr.trim() || err.message}`);
  }
}

/**
 * Poll GET /sessions/{id} until the restarted server serves it again. uvicorn does
 * not accept requests until lifespan startup finishes, so the first successful GET
 * proves the process is back AND the business routes are mounted.
 */
async function waitForServerBack(api: AstraApi, sessionId: string, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown = null;
  while (Date.now() < deadline) {
    try {
      await api.getSession(sessionId);
      return;
    } catch (error) {
      lastError = error;
    }
    await sleep(2_000);
  }
  throw new Error(
    `backend-restart spec: server did not resume serving GET /sessions/${sessionId} within ${timeoutMs}ms: ${String(lastError)}`,
  );
}

/**
 * The next pause the turn has NOT been answered for yet, or null within the budget.
 *
 * Sampling the pending projection right after an approval reads back the one just
 * answered — its engine-side wait is already released — so identity, not presence,
 * is what tells a new gate from a stale read. Driven from the page that identity
 * check earns a second job: it gates the next CLICK, because the console hides an
 * answered card optimistically and only releases the suppression once a DIFFERENT
 * interaction is authoritative.
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
    await sleep(2_000);
  }
  return null;
}

test('backend restart rehydrates a resident warm conversation and a resident pending approval', async ({
  page,
  request,
}) => {
  // Restarting the shared server disrupts every neighboring spec. run-round.mjs
  // therefore schedules restart specs in their own serial pass.
  const api = new AstraApi(request);
  const runId = Date.now();

  // Fail fast (with the descriptive oracle error) if the document store is not
  // reachable — the durable half of this spec depends on it.
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });

  const panel = page.getByTestId('pending-interaction-panel');
  const pill = page.getByTestId('run-view').getByTestId('status-pill').first();

  /**
   * Open one conversation the way a user does — by loading its page. Used again
   * after the restart on the URL the tab is already on: `goto` navigates either
   * way, so the console comes back with no client memory of the pre-restart turn.
   */
  const openConversation = async (sessionId: string, timeoutMs = 45_000) => {
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view'), 'the conversation must load for the user').toBeVisible({
      timeout: timeoutMs,
    });
  };

  /** Fill the real composer and confirm dispatch by the user's own bubble. `.fill()`
   * sets the value without key events, so a long prompt is not submitted early by
   * an Enter newline. */
  const sendFromComposer = async (prompt: string, echo: string) => {
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
  };

  /**
   * Send from the composer and wait for one more rendered assistant bubble that is
   * a REPLY. Counting bubbles, never matching prose: the backend is deepseek behind
   * a gateway and its wording is its own business. The error guard is what makes
   * the count mean anything — a failed turn renders its error into the transcript
   * as an assistant message and would satisfy the count on its own.
   */
  const sendAndReadReply = async (prompt: string, echo: string, budgetMs: number) => {
    const before = await page.getByTestId('assistant-message').count();
    await sendFromComposer(prompt, echo);
    await expect
      .poll(() => page.getByTestId('assistant-message').count(), { timeout: budgetMs })
      .toBeGreaterThan(before);
    const reply = page.getByTestId('assistant-message').last();
    await expect(reply, 'the turn should have rendered assistant content').not.toBeEmpty();
    await expect(reply).not.toContainText(TURN_ERROR);
    return reply;
  };

  /**
   * Approve the gate the way a user does: press the card's affirmative button.
   *
   * Acceptance is read off the console's OWN POST rather than off the card
   * disappearing, because the card disappears optimistically at the click. The
   * response body also carries `answered` and the interaction_id — the only place
   * the button the user pressed and the id the durable oracle follows can be tied
   * together, since nothing on screen renders an interaction id. The check is
   * strict: the POST's status, its `answered` flag and the interaction_id it
   * answers are all asserted — every fact a direct `approvePendingInteraction`
   * call asserts, read here off the button the user pressed.
   */
  const approveFromPanel = async (expectedInteractionId: string) => {
    const approve = panel.getByRole('button', { name: APPROVE_CONTINUE }).last();
    await expect(
      approve,
      'the approval card must offer the user an enabled affirmative button',
    ).toBeEnabled({ timeout: PANEL_RENDER_MS });
    const accepted = page.waitForResponse(
      (response) => response.request().method() === 'POST' && response.url().includes('/interaction-respond'),
      { timeout: ANSWER_ACCEPTED_MS },
    );
    await approve.click();
    const response = await accepted;
    expect(response.status(), 'the approval the user pressed must be accepted by the platform').toBe(200);
    const body = (await response.json()) as Record<string, unknown>;
    const result = (body.data ?? body) as { answered?: unknown; interaction_id?: unknown };
    expect(Boolean(result.answered), 'approving the survived interaction should be accepted').toBe(true);
    expect(
      String(result.interaction_id || ''),
      'the console must answer the interaction the card was showing, not a stale one',
    ).toBe(expectedInteractionId);
  };

  // Source a concrete model + environment from the seeded default agent — the create
  // validator (agent_schema) requires a non-blank model; skip any litellm wildcard
  // routing key, exactly as the reclaim / evict siblings do.
  const base = await api.defaultAgent();
  const environmentName = String(base.environment_name || '').trim();
  expect(environmentName, 'seeded default agent must name an environment').not.toEqual('');
  const models = await api.listEnvironmentModels(environmentName);
  const model = models.find((m) => m && !m.includes('*')) || 'deepseek-chat';

  let warmSessionId = '';
  let pendingSessionId = '';
  try {
    // ── One isolated agent (pure metadata, always ACTIVE) hosting two conversations.
    //    The per-session sandbox lives on each conversation, so both resident states
    //    are genuinely independent compute that one server restart must rehydrate. ──
    const agent = await api.createAgent({
      name: `__e2e_restart_rehydrate_${runId}`,
      model,
      environment_name: environmentName,
    });
    agentId = String(agent.agent_id || '');
    expect(agentId, 'created agent must have an id').not.toEqual('');

    // ── Resident state A — a paused pending approval (armed BEFORE the restart so the
    //    deepseek probe-and-skip runs before this spec touches the shared stack). ───
    const pendingConversation = await api.startConversation(agentId);
    pendingSessionId = pendingConversation.session_id;
    sessions.push(pendingSessionId);
    await api.waitForSessionReady(pendingSessionId);
    // ARRANGE the permission mode through the API, and BEFORE the page opens: the
    // console dispatches under the mode it read at LOAD (useSessionChat sends
    // permissionModeRef.current, seeded from session.permission_mode), so a page
    // opened first would send in bypassPermissions and nothing would ever gate.
    // The API port passed the mode per-turn to `streamPrompt`; the page has no such
    // per-send knob, which is why the session-level mode is the equivalent arrange.
    await api.setPermissionMode(pendingSessionId, 'default');

    await openConversation(pendingSessionId);
    const pendingMarker = `E2E restart pending ${runId}`;
    await sendFromComposer(
      `${pendingMarker}: 请使用 Write 工具创建相对路径 e2e-restart-pending-${runId}.txt，` +
        `内容写入 "restart pending approval e2e"。只创建这一个文件，然后等待我确认，不要继续其它操作。`,
      pendingMarker,
    );

    // deepseek-chat may never raise a permission (the turn just finishes with stop).
    // Probe with a bounded wait; skip — do not fake-pass, and do not restart the stack
    // — if the platform never surfaces one.
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. The probe returns the moment the turn settles ungated,
    // so a declined ask costs that turn rather than the whole budget.
    const interaction = await insist<PendingInteraction>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(pendingSessionId, INSIST_NUDGE);
      },
      probe: () =>
        api.waitForPendingInteractionOrSettledTurn(pendingSessionId, PAGE_PROBE_BUDGET_MS),
      what: "deepseek-chat raised no tool-permission interaction for a Write under permission_mode 'default' " + '(the turn finished with stop) — no resident pending approval to carry across a restart',
      budgetMs: PAGE_PROBE_BUDGET_MS * 2,
      probeMs: PAGE_PROBE_BUDGET_MS,
    });
    expect(interaction.presentation, 'pending interaction should be a tool approval').toBe('tool_approval');
    const pendingTurnId = String(interaction.turn_id || '').trim();
    expect(pendingTurnId, 'pending interaction should bind to a turn').not.toEqual('');
    const interactionId = String(interaction.interaction_id || '').trim();
    expect(interactionId, 'pending interaction should carry an interaction_id').not.toEqual('');
    // The page's identity anchor, taken from the SERVER rather than from what the
    // prompt asked for: no testid renders an interaction id, so the tool the gate
    // names is how the screen and the durable record are tied together.
    const toolName = String(interaction.tool_name || '').trim();
    expect(toolName, 'a tool permission should name the tool it is gating').not.toEqual('');

    // The resident state this restart has to preserve is not a row — it is a person
    // looking at a question. Assert it reached them: the composer is REPLACED by the
    // approval card, naming the gated tool, and the header says the turn is waiting
    // on THEM. Never a skip — the skip above is for the model declining to ask.
    await expect(
      panel,
      'the raised permission must reach the user as the composer approval card before the restart',
    ).toBeVisible({ timeout: PANEL_RENDER_MS });
    await expect(panel, 'the card must be about the tool the platform gated').toContainText(toolName);
    // `data-state` rather than the label, because the console is bilingual and the
    // label is a translation; sessionRunStatus maps the two 1:1.
    await expect(
      pill,
      'a turn parked on a permission must read as waiting on the user, not generating',
    ).toHaveAttribute('data-state', 'WAITING_INPUT', { timeout: PANEL_RENDER_MS });

    // Durable precondition: the paused turn records WAITING_FOR_INTERACTION
    // pointing at THIS interaction — this is the resident state the restart must not lose.
    // No pixel: the console renders neither a turn id nor an interaction id, so which
    // turn is parked on which gate is only bindable in the durable record.
    // Awaited, not sampled: the interaction is readable over HTTP the moment the
    // kernel registers it, and the snapshot naming its turn is a separate write.
    // The post-restart read below stays a single sample — THAT one is the
    // behaviour under test, and durable state that survived a restart is
    // already there or it is lost.
    const waitingBefore = await waitForTurnSnapshot(pendingSessionId, pendingTurnId);
    expect(
      String(waitingBefore?.conversation_state || ''),
      'the paused turn records WAITING_FOR_INTERACTION before the restart',
    ).toBe('WAITING_FOR_INTERACTION');
    expect(
      String(waitingBefore?.active_interaction_id || ''),
      'the paused turn points at the pending interaction before the restart',
    ).toBe(interactionId);

    // ── Resident state B — an idle warm conversation with a settled turn on a known
    //    sandbox. The warm turn is what makes the sandbox genuinely resident: a
    //    conversation that never ran holds no runtime for the restart to rebuild,
    //    and the restart must reattach the SAME sandbox. ────────────────────────────
    const warmConversation = await api.startConversation(agentId);
    warmSessionId = warmConversation.session_id;
    sessions.push(warmSessionId);
    await api.waitForSessionReady(warmSessionId);

    // The user switches to their other conversation and works in it — the same tab,
    // the same console. Leaving the pending one is not a problem for it: a parked
    // turn is durable, which is the whole claim being carried across the restart.
    await openConversation(warmSessionId);
    const warmMarker = `E2E restart warm ${runId}`;
    const warmReply = await sendAndReadReply(
      `${warmMarker}: 不要使用工具。请简短回复一句话。`,
      warmMarker,
      TURN_TIMEOUT_MS,
    );
    await expect(pill, 'the warm turn must settle before the conversation counts as idle-warm')
      .toHaveAttribute('data-pulse', 'false', { timeout: SETTLE_MS });
    // Record the actual reply the user can read. The live stream may render its
    // reasoning and answer as several elements while a fresh transcript load folds
    // the same frames into one, so element count is not durable identity.
    const warmReplyText = normalizeRendered(
      (await warmReply.getByTestId('assistant-text').allInnerTexts()).join('\n'),
    );
    expect(warmReplyText, 'the warm turn should have left readable reply content').not.toEqual('');

    const warmSettled = await api.waitForSessionReady(warmSessionId);
    const warmSandboxId = String(warmSettled.sandbox_id || '').trim();
    expect(warmSandboxId, 'warm conversation should hold a sandbox before the restart').not.toEqual('');
    test.info().annotations.push({ type: 'e2e_warm_sandbox_id', description: warmSandboxId });

    // ── THE fault: restart the backend server container. Durable PostgreSQL state
    //    and the per-session sandboxes (separate containers)
    //    survive; every in-memory runtime is dropped and must be rehydrated. ──────────
    const serverContainer = requireServiceContainer(SERVER_CONTAINER_HANDLE);
    test.info().annotations.push({ type: 'e2e_restarted_server_container', description: serverContainer });
    restartServerContainer(serverContainer);
    await waitForServerBack(api, warmSessionId, SERVER_BACK_MS);

    // ── Verify resident warm conversation rehydrated onto the SAME sandbox and is
    //    still usable — the next message lands a real reply without a re-borrow. ───────
    //    The page does not render sandbox identity, and re-borrowing is deliberately
    //    invisible, so exact reattachment is read from the session record.
    const warmReady = await api.waitForSessionReady(warmSessionId);
    expect(
      String(warmReady.sandbox_id || '').trim(),
      'restart rehydration should reattach the warm conversation to its surviving sandbox',
    ).toBe(warmSandboxId);
    expect(
      String(warmReady.last_error || '').trim(),
      'restart rehydration should not leave a user-visible error on the warm conversation',
    ).toBe('');

    // The user comes back to the conversation. This load is what makes the
    // transcript evidence of REHYDRATION: the tab that watched the restart is gone,
    // so nothing the console draws now can come from client memory.
    await openConversation(warmSessionId, REHYDRATE_RENDER_MS);
    await expect(
      page.getByTestId('user-message').filter({ hasText: warmMarker }).last(),
      'after the restart a freshly loaded page must still hold the warm conversation the user left',
    ).toBeVisible({ timeout: REHYDRATE_RENDER_MS });
    await expect
      .poll(async () => {
        const rehydrated = normalizeRendered(
          (await page.getByTestId('assistant-text').allInnerTexts()).join('\n'),
        );
        return rehydrated.includes(warmReplyText);
      }, {
        timeout: REHYDRATE_RENDER_MS,
        message: 'the rehydrated transcript must still carry the reply content it had before the restart',
      })
      .toBe(true);

    // …and it is not just readable, it is USABLE: the next message goes through the
    // real composer and comes back a reply rather than a rendered failure.
    const warmFollowUpMarker = `E2E restart warm rehydrated ${runId}`;
    await sendAndReadReply(
      `${warmFollowUpMarker}: 不要使用工具。请简短回复一句话。`,
      warmFollowUpMarker,
      TURN_TIMEOUT_MS,
    );
    await expect(pill, 'the header must settle after the post-restart warm turn')
      .toHaveAttribute('data-pulse', 'false', { timeout: SETTLE_MS });

    const warmAfterTurn = await api.waitForSessionReady(warmSessionId);
    expect(
      String(warmAfterTurn.sandbox_id || '').trim(),
      'the post-restart turn should stay on the original warm sandbox',
    ).toBe(warmSandboxId);

    // ── Verify resident pending approval SURVIVED the restart and still points at the
    //    same durable interaction (rehydrated from storage, not resurrected anew). ─────
    //    Read where it matters first: a freshly loaded console must put the question
    //    back in front of the user. An interaction that is perfect in the document
    //    store and absent from the screen is a session nobody can unblock — which is
    //    the failure "the pending approval survived" is supposed to rule out.
    await openConversation(pendingSessionId, REHYDRATE_RENDER_MS);
    await expect(
      page.getByTestId('user-message').filter({ hasText: pendingMarker }).last(),
      'after the restart a freshly loaded page must still hold the message that opened the gate',
    ).toBeVisible({ timeout: REHYDRATE_RENDER_MS });
    await expect(
      panel,
      'after the restart a freshly loaded page must still show the approval the user was asked for',
    ).toBeVisible({ timeout: PENDING_RESURFACE_MS });
    await expect(panel, 'the rehydrated card must still be about the tool the platform gated').toContainText(toolName);
    await expect(
      pill,
      'the rehydrated turn must still read as waiting on the user, not as generating or idle',
    ).toHaveAttribute('data-state', 'WAITING_INPUT', { timeout: PENDING_RESURFACE_MS });

    // Same interaction, not a new one raised on the way back up. No pixel — no
    // testid renders an interaction id, which is exactly why the card above is
    // matched on the tool name it carries.
    const survivedPending = await api.waitForPendingInteraction(pendingSessionId, PENDING_RESURFACE_MS);
    expect(
      String(survivedPending.interaction_id || ''),
      'the SAME pending approval must survive the backend restart',
    ).toBe(interactionId);

    const waitingAfter = turnSnapshot(pendingSessionId, pendingTurnId);
    expect(waitingAfter, 'the paused turn must be rehydrated from durable state after the restart').not.toBeNull();
    expect(
      String(waitingAfter?.conversation_state || ''),
      'the turn still records WAITING_FOR_INTERACTION after the restart',
    ).toBe('WAITING_FOR_INTERACTION');
    expect(
      String(waitingAfter?.active_interaction_id || ''),
      'the turn still points at the unanswered interaction after the restart',
    ).toBe(interactionId);

    // ── The answered continuation commits cleanly across the restart: the user
    //    presses the card's own button, which resumes the SAME turn
    //    (answer_pending_interaction reuses pending.turn_id) and settles it. ──────────
    await approveFromPanel(interactionId);

    // Whether the engine asks AGAIN before it finishes is the model's business,
    // not the restart's: deepseek-v4-flash follows the approved Write with a
    // Bash, and the session parks on that second gate exactly as it should.
    // Waiting for a settled header without draining measures the model's tool plan
    // and reports it as a restart regression.
    //
    // Each gate answered must be a NEW one: identity, not presence, tells a fresh
    // gate from a read-back of the one just answered. An answer that did NOT take
    // therefore never re-arms this loop — it shows up right after it as the card
    // refusing to clear and the header refusing to settle, which is the same
    // failure caught one assertion later and with a better message.
    //
    // "No new gate" is only the end after a WINDOW, never after one sample: the
    // answered gate clears BEFORE the engine opens the next, so a momentary null
    // is a gap in the chain, not the end of it. NEXT_PAUSE_TIMEOUT_MS is that
    // window — a single read would end the drain inside the gap and hand the
    // still-gated turn to the settle assertions below as if it had finished.
    const answered = new Set<string>([interactionId]);
    let drained = false;
    for (let approvals = 1; approvals <= MAX_APPROVALS; approvals += 1) {
      const next = await waitForNewPause(api, pendingSessionId, answered, NEXT_PAUSE_TIMEOUT_MS);
      if (next === null) {
        drained = true;
        break;
      }
      expect(
        String(next.turn_id || ''),
        'the answered continuation must stay on the SAME turn across the restart, not start a new one',
      ).toBe(pendingTurnId);
      const nextId = String(next.interaction_id || '');
      await expect(
        panel,
        'each further gate must reach the user as an approval card too',
      ).toBeVisible({ timeout: PANEL_RENDER_MS });
      // A tool-permission chain is what this scenario produces, and the API port
      // assumed the same (it posted `decision: approve` at every pause whatever the
      // kind). A differently shaped card — a questionnaire, an exit-plan
      // confirmation — has no affirmative button, so it fails inside
      // approveFromPanel naming the missing control rather than being mis-clicked.
      await approveFromPanel(nextId);
      answered.add(nextId);
    }
    expect(drained, `the turn kept gating tools past ${MAX_APPROVALS} approvals`).toBe(true);

    // ── The turn settles where the user watches it: the card is gone, the header
    //    stops pulsing, the transcript ends on a reply rather than a rendered
    //    failure, and the composer is usable again (the user-visible face of the
    //    turn lock releasing across the restart). ────────────────────────────────────
    await expect(panel, 'the answered approval should clear off the composer').toBeHidden({
      timeout: PANEL_RENDER_MS,
    });
    await expect(pill, 'the header must settle once the resumed turn ends')
      .toHaveAttribute('data-pulse', 'false', { timeout: CONTINUATION_SETTLE_MS });

    const reply = page.getByTestId('assistant-message').last();
    await expect(reply, 'the resumed turn should have rendered assistant content').not.toBeEmpty();
    await expect(reply).not.toContainText(TURN_ERROR);

    // An EMPTY composer keeps submit disabled by design — type first, then assert.
    await page.getByTestId('composer-prompt').fill('follow-up');
    await expect(page.getByTestId('composer-submit')).toBeEnabled({ timeout: 15_000 });

    // ── The durable half behind the settled screen. The cleared card above is the
    //    pixel, but the console suppresses it optimistically at the click, so the
    //    projection is asserted as well rather than instead. ────────────────────────
    const pendingReady = await api.waitForSessionReady(pendingSessionId);
    expect(pendingReady.pending_interaction, 'the answered interaction should clear off the session').toBeFalsy();
    const afterPending = await api.getPendingInteraction(pendingSessionId);
    expect(afterPending, 'the answered interaction is no longer active/pending after the restart').toBeNull();

    const committed = await waitForTurnTerminalProof(
      pendingSessionId,
      pendingTurnId,
      'COMPLETED',
      TERMINAL_PROOF_BUDGET_MS,
    );
    expect(
      Boolean(committed.active_interaction_id),
      'committing the answered turn clears the active-interaction pointer after the restart',
    ).toBe(false);
    const terminalProof =
      committed.last_turn_terminal_frame && typeof committed.last_turn_terminal_frame === 'object'
        ? (committed.last_turn_terminal_frame as Record<string, unknown>)
        : {};
    expect(
      String(terminalProof.type || ''),
      'the answer continuation ends on a durable finish frame after the restart',
    ).toBe('finish');
    expect(
      Number.isFinite(Number(terminalProof.frame_seq)),
      'the settled turn carries a durable terminal frame_seq after the restart',
    ).toBe(true);
  } finally {
    // Nothing state-changing happens here. Both conversations AND the agent
    // they ran on are released on the passing path only — see the cleanup
    // fixture on why a `finally` cannot tell it is unwinding from a failure,
    // and on why the unit is the whole block rather than the delete line. A
    // kept session pointing at a deleted agent is a poor scene for this spec
    // in particular: a rehydration failure is diagnosed against the agent's
    // environment and permission mode, which the deletion would take with it.
  }
});
