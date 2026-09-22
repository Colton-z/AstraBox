/**
 * E2E: an unanswered tool-permission interaction survives Agent hibernate and
 * wake calls.
 *
 * A default-mode Write request pauses on the approval panel. After both no-op
 * lifecycle calls, a page reload must restore the same blocking card and show
 * WAITING_INPUT. The API and durable snapshot must retain the same interaction
 * id, turn id, and WAITING_FOR_INTERACTION state while the Agent remains ACTIVE
 * without a sandbox.
 *
 * Reloading is essential because it proves that the panel was reconstructed from
 * persisted state rather than retained React state. Tool invocation is
 * model-dependent, so a bounded probe skips only when no permission interaction
 * is raised.
 */
import { test, expect, type Page } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi, type PendingInteraction } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { oracleDbPath, snapshotDoc, turnSnapshot, waitForTurnSnapshot } from '../fixtures/dbOracle';

// Agent create + provision + a paused turn + the two lifecycle no-ops + a reload +
// oracle reads sit above the 240s suite default; keep it generous and env-tunable,
// like the sibling pending-interaction specs.
// Bounded probe: how long to wait for a pending interaction before skipping.
// Sending from the composer returns as soon as the turn is accepted (it is not a
// blocking POST that comes back at the interaction's segment `finish`), so this
// budget spans the model's run up to the park, not just projection lag.
// Left at the suite-wide 60s the whole pending-interaction family
// shares — a model too slow to ask inside it skips, and the skip reason says which
// of the two happened.
/** What a user says when a model answers instead of acting. */
const INSIST_NUDGE =
  "You answered without using the tool. Do the tool call now, exactly as asked above, before replying again. \u8bf7\u73b0\u5728\u5c31\u6309\u4e0a\u9762\u7684\u8981\u6c42\u8c03\u7528\u5de5\u5177\uff0c\u4e0d\u8981\u53ea\u7528\u6587\u5b57\u56de\u7b54\u3002";

const PROBE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_PENDING_PROBE_TIMEOUT_MS', 60_000);

// The affirmative primary button of the tool-permission card
// (misc:composer.allow_continue with the default allow_once choice selected,
// misc:composer.apply_suggestion_continue once a suggestion is picked). Matched in
// either language so the runner's locale stays unpinned.
const APPROVE_CONTINUE = /Allow and continue|Apply suggestion and continue|允许并继续|应用建议并继续/;
// A FAILED turn renders its error INTO the transcript as an assistant message, so
// "the transcript still looks fine" is satisfied by exactly a failure. If the
// lifecycle pokes tore the paused turn down, this is where the user would see it.
const TURN_ERROR = /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

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

// Teardown that changes state runs on the passing path only, in three hooks
// whose registration order IS the old `finally` order: afterEach hooks run in
// the order they are registered. Settle first — a DELETE against a still
// STREAMING session answers SESSION_BUSY — then the session, then the agent it
// ran on.
//
// Interrupt the paused turn first (best-effort — releases the outstanding
// interaction so teardown is clean), then delete the conversation (releases
// its own sandbox), then the agent (delete_agent owns nothing else).
let sessionId = '';
let agentId = '';
onPassOnly(async ({ request }) => {
  if (sessionId) await new AstraApi(request).interruptSession(sessionId);
});
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('pending-interaction residue survives the hibernate/wake no-ops', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = Date.now();

  // Fail fast (with the descriptive oracle error) if the document store is not
  // reachable — the durable half of this spec depends on it.
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });

  try {
    // ── Isolated agent (pure metadata, always ACTIVE, no sandbox of its own) +
    //    one conversation. The isolated agent makes the lifecycle separation
    //    observable: hibernate/wake act on the agent record only, never on the
    //    conversation's compute or its durable turn state. Creating it is
    //    ARRANGE — the console's create flow is a management-drawer form, not the
    //    subject of this invariant. ──────────────────────────────────────────────
    const agent = await api.createColdTestAgent(`__e2e_pending_hibernate_${runId}`);
    agentId = String(agent.agent_id || '');
    expect(agentId, 'created agent must have an id').not.toEqual('');
    expect(String(agent.state || ''), 'created agent is ACTIVE immediately').toEqual('ACTIVE');
    expect(
      String(agent.sandbox_id || '').trim(),
      'per-session agent must not hold a sandbox_id',
    ).toEqual('');

    const created = await api.startConversation(agentId);
    sessionId = created.session_id;
    sessions.push(sessionId);
    await api.waitForSessionReady(sessionId);

    // ARRANGE (API — a precondition, not the behaviour under test): community
    // conversations open in bypassPermissions, and the page dispatches under the
    // session's permission_mode as it was at load, so the switch has to happen
    // BEFORE the page opens or no permission is ever raised to preserve.
    await api.setPermissionMode(sessionId, 'default');
    const armed = await api.getSession(sessionId);
    expect(
      armed.permission_mode,
      'parking a turn on a tool permission needs tool approvals on (permission_mode=default)',
    ).toBe('default');

    await openSessionView(page, sessionId);

    // ── The user asks for one Write; the turn parks on the tool permission. ────
    // The echo is also the transcript anchor after the reload: it is the one
    // bubble that must come back, so it is what says the transcript rehydrated
    // before anything is concluded from what the transcript does NOT contain.
    const echo = `E2E pending hibernate ${runId}`;
    await sendFromComposer(
      page,
      `${echo}: 请使用 Write 工具创建相对路径 ` +
        `e2e-pending-hibernate-${runId}.txt，内容写入 "pending hibernate per-session e2e"。` +
        `只创建这一个文件，然后等待我确认，不要继续其它操作。`,
      echo,
    );

    // ── PROBE: deepseek-chat may never raise a permission (the turn just finishes
    //    with stop). Bounded-wait for a pending interaction; skip — do not
    //    fake-pass — if the platform never surfaces one. Kept on the API on
    //    purpose: the skip is for the MODEL declining, so a raised interaction
    //    that never reaches the screen must go red, which is what the page
    //    assertion below does. ───────────────────────────────────────────────────
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
      what: "deepseek-chat raised no tool-permission interaction for a Write under permission_mode 'default' " + '(the turn finished with stop, or never reached the tool) — no pending-interaction residue to ',
      budgetMs: PROBE_TIMEOUT_MS * 2,
      probeMs: PROBE_TIMEOUT_MS,
    });
    const interaction = pending as PendingInteraction;

    // ── The pending interaction (API projection): a tool approval bound to a
    //    turn. The panel renders neither id, and the durable assertions below
    //    need both. ────────────────────────────────────────────────────────────
    expect(interaction.presentation, 'the setup turn should create a tool-approval interaction').toBe(
      'tool_approval',
    );
    const turnId = String(interaction.turn_id || '').trim();
    expect(turnId, 'the pending interaction should bind to a turn').not.toEqual('');
    const interactionId = String(interaction.interaction_id || '').trim();
    expect(interactionId, 'the pending interaction should carry an interaction_id').not.toEqual('');
    // The tool name is the one piece of the interaction's identity the card DOES
    // render (ToolPermissionInteractionCard draws `tool_name` verbatim, unlocalized),
    // so it is what the page can hold the surviving card to.
    const toolName = String(interaction.tool_name || '').trim();
    expect(toolName, 'a tool-permission interaction should name its tool').not.toEqual('');

    // ── BEFORE: the residue as the USER holds it. The pending panel replaces the
    //    composer, so there is nothing to type into — the only ways forward are the
    //    card's own buttons. This is the state the no-ops must not disturb. ──────
    const panel = page.getByTestId('pending-interaction-panel');
    await expect(
      panel,
      'the raised tool permission must reach the user as the composer pending panel',
    ).toBeVisible({ timeout: 60_000 });
    await expect(
      page.getByTestId('composer-prompt'),
      'a blocking permission takes the composer away — the user cannot type past it',
    ).toHaveCount(0);
    await expect(panel, 'the card is the permission request for the tool that parked the turn')
      .toContainText(toolName);
    // Scoped to the run view: the sidebar renders a status pill per conversation
    // row, and this assertion is about the header of the one on screen.
    const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
    await expect(
      pill,
      'a session paused on an interaction is not actively-running turn work: the header says it is waiting on the user',
    ).toHaveAttribute('data-state', 'WAITING_INPUT', { timeout: 30_000 });

    // ── Capture the durable paused state. The panel proves only what this browser
    //    received; the snapshot is the state a restart would rehydrate. It must
    //    point WAITING_FOR_INTERACTION at this interaction.
    //    Awaited, not sampled: the interaction becomes readable over HTTP as soon
    //    as the kernel registers it, while the snapshot naming the turn it parked
    //    on is a separate durable write, and a composer send returns long before
    //    either — a blocking POST route hides that gap by holding its response
    //    open until the turn settles; this composer's send does not. ──────────
    const pausedBefore = await waitForTurnSnapshot(sessionId, turnId);
    expect(
      String(pausedBefore.conversation_state || ''),
      'the paused turn records WAITING_FOR_INTERACTION',
    ).toBe('WAITING_FOR_INTERACTION');
    expect(
      String(pausedBefore.active_interaction_id || ''),
      'the paused turn points at the pending interaction',
    ).toBe(interactionId);

    //    …and the API session surfaces the SAME unanswered interaction as residue.
    const sessionBefore = await api.getSession(sessionId);
    expect(
      String(sessionBefore.pending_interaction?.interaction_id || ''),
      'the session surfaces the pending interaction as residue before the no-ops',
    ).toBe(interactionId);
    // The session records the paused turn (retained while an interaction is
    // active — session_mirror clears current_turn_id only once no active
    // interaction remains). Soft corroboration: assert only if projected.
    const currentTurnBefore = String(sessionBefore.current_turn_id || '').trim();
    if (currentTurnBefore) {
      expect(
        currentTurnBefore,
        'the session records the paused turn while the interaction is pending',
      ).toBe(turnId);
    }

    //    …and (no pixel, and NOT what the header pill says) the server's own
    //    rendered session state is the waiting/wakeable projection, never an
    //    actively-running one. The pill cannot stand in for this: `headerRunState`
    //    answers WAITING_INPUT on hasPendingInteraction ahead of its live branch,
    //    so it renders WAITING_INPUT no matter what the server thinks, while
    //    `derive_ui_state` answers PROCESSING on terminal_state == 'RUNNING' AHEAD
    //    of its pending branch. A platform still accounting the paused turn as
    //    running work is therefore invisible on screen and visible only here — and
    //    so that incorrect accounting is visible only here. READY is included to
    //    absorb a cross-GET projection race; an
    //    actively-running render (PROCESSING/INTERRUPTING/BACKGROUND_RUNNING) fails it.
    expect(
      ['READY', 'WAITING_INPUT'],
      `a session paused on an interaction is not actively-running turn work; state=${sessionBefore.state}`,
    ).toContain(String(sessionBefore.state || ''));

    //    Snapshot corroboration: the session_snapshots projection exists and is
    //    keyed to this session while the interaction is pending.
    const snapshotBefore = snapshotDoc(sessionId);
    expect(snapshotBefore, 'a session snapshot should exist while the interaction is pending').not.toBeNull();
    expect(
      String(snapshotBefore?.session_id || ''),
      'the session snapshot is keyed to this session',
    ).toBe(sessionId);

    // ── The two lifecycle no-ops. Per-session hibernate/wake act on the agent
    //    record only (pure metadata) — the agent stays ACTIVE and never acquires a
    //    sandbox. They stay on the API because no console control hibernates an
    //    AGENT (only assistants expose Wake/Hibernate): this is an operator action,
    //    like the sibling specs' out-of-band sandbox kill. Both shared fixture
    //    helpers wrap the public POST routes and return the sanitized agent under
    //    the {data} envelope; the state half is the direct evidence of THAT poke
    //    and the sandbox_id half has no rendering anywhere in the console. ───────
    const afterHibernate = await api.hibernateAgent(agentId);
    expect(
      String(afterHibernate.state || ''),
      'per-session hibernate must be a no-op returning ACTIVE',
    ).toEqual('ACTIVE');
    expect(
      String(afterHibernate.sandbox_id || '').trim(),
      'per-session agent must never hold a sandbox_id (hibernate)',
    ).toEqual('');

    const afterWake = await api.wakeAgent(agentId);
    expect(
      String(afterWake.state || ''),
      'per-session wake must be a no-op returning ACTIVE',
    ).toEqual('ACTIVE');
    expect(
      String(afterWake.sandbox_id || '').trim(),
      'per-session agent must never hold a sandbox_id (wake)',
    ).toEqual('');

    // ── AFTER: the question is still the user's to answer. The reload is what
    //    makes this a real oracle — nothing pushes to the browser when an agent
    //    record is poked, so a panel that merely stayed on screen would prove only
    //    that the client did not re-render. A freshly loaded console has no memory
    //    of the paused turn, so the card it draws now came from durable state the
    //    server still holds. ─────────────────────────────────────────────────────
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
    await expect(
      panel,
      'after the no-ops a freshly loaded page must still show the pending permission — ' +
        'rehydrated from durable state, or the user is left with an unanswerable session',
    ).toBeVisible({ timeout: 60_000 });
    await expect(
      panel,
      'the surviving card is still the permission request for the tool that parked the turn, ' +
        'not a blank or a different one',
    ).toContainText(toolName);
    await expect(
      page.getByTestId('composer-prompt'),
      'the turn is still blocked on the user — the no-ops did not release it',
    ).toHaveCount(0);
    // The header still says "waiting on you", not TERMINATED: isTerminated /
    // isAgentRuntimeDeleted override the run state AHEAD of the pending-interaction
    // branch, so this is the assertion that catches an agent-level poke leaving the
    // conversation reading as dead under an unanswered question.
    await expect(
      pill,
      'the conversation is still paused on the user after the no-ops, not terminated and not running',
    ).toHaveAttribute('data-state', 'WAITING_INPUT', { timeout: 60_000 });
    // …and still ANSWERABLE. A card that came back as a husk with dead controls is
    // a session no user can unblock, which is the same outcome as losing it.
    await expect(
      panel.getByRole('button', { name: APPROVE_CONTINUE }).last(),
      'the surviving card must still offer a live affirmative control',
    ).toBeEnabled({ timeout: 30_000 });
    // The paused turn was not torn down behind the card: a failed turn renders its
    // error INTO the transcript as an assistant message, which is where the user
    // would meet a lifecycle poke that killed the turn it was not supposed to touch.
    // Sound as a NEGATIVE text assertion because the turn is parked, not streaming —
    // the transcript is not moving while the card is up.
    //
    // Anchored first: the panel is drawn from the session detail, the transcript
    // from a separate history fetch, so "no error bubble" would pass vacuously on a
    // transcript that has not rehydrated yet. The user's own message is the bubble
    // that must be back before absence means anything.
    await expect(
      page.getByTestId('user-message').filter({ hasText: echo }).last(),
      'the reloaded transcript must have rehydrated before its contents are judged',
    ).toBeVisible({ timeout: 60_000 });
    await expect(
      page.getByTestId('assistant-message').filter({ hasText: TURN_ERROR }),
      'the no-ops must not surface to the user as a failed turn',
    ).toHaveCount(0);

    // ── AFTER (no pixel): the durable residue is intact. The no-ops never reached
    //    into the conversation's paused turn — it still records
    //    WAITING_FOR_INTERACTION pointing at the SAME interaction, and the API still
    //    surfaces the SAME unanswered pending interaction. The panel has no
    //    interaction-id attribute, so identity stays on the API. Sampled, not awaited: after a no-op
    //    nothing should have moved, so it must ALREADY be there. ─────────────────
    const pausedAfter = turnSnapshot(sessionId, turnId);
    expect(pausedAfter, 'the no-ops must leave the durable paused turn in place').not.toBeNull();
    expect(
      String(pausedAfter?.conversation_state || ''),
      'the turn still records WAITING_FOR_INTERACTION after the no-ops',
    ).toBe('WAITING_FOR_INTERACTION');
    expect(
      String(pausedAfter?.active_interaction_id || ''),
      'the turn still points at the unanswered interaction after the no-ops',
    ).toBe(interactionId);

    const pendingAfter = await api.getPendingInteraction(sessionId);
    expect(pendingAfter, 'the pending interaction must survive the hibernate/wake no-ops').not.toBeNull();
    expect(
      String(pendingAfter?.interaction_id || ''),
      'the SAME interaction is still pending after the no-ops (residue not disturbed)',
    ).toBe(interactionId);

    // ── The agent record itself is untouched — still ACTIVE, still holds no
    //    sandbox (the no-ops are pure metadata; per-session lifecycle separation).
    const agentAfter = await api.getAgent(agentId);
    expect(String(agentAfter.state || ''), 'the agent stays ACTIVE through the no-ops').toEqual('ACTIVE');
    expect(
      String(agentAfter.sandbox_id || '').trim(),
      'the agent still holds no sandbox after the no-ops',
    ).toEqual('');
  } finally {
    // Nothing is torn down here. `trackSessions()` and `onPassOnly()` decide in
    // afterEach hooks, where the test's real status is known — see that fixture
    // on why a `finally` cannot tell it is unwinding from a failure, and on why
    // the unit is this whole block rather than the delete line.
  }
});
