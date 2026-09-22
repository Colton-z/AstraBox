/**
 * E2E: reclaiming a sandbox settles its now-orphaned interaction, and the
 * next browser message re-borrows compute in the same conversation.
 *
 * A default-mode Write request first pauses on the approval panel. The product's
 * sandbox-reclaim action then removes the compute which owned the in-memory
 * PreToolUse wait. That action must close the parked turn and interaction; it
 * must never leave an enabled card whose answer can only 409. A cold page shows
 * the ordinary composer, and its first message completes on a different sandbox
 * without a resend.
 *
 * Tool invocation is model-dependent, so a bounded probe skips only when no
 * permission interaction is raised.
 */
import { test, expect } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi, type PendingInteraction } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import {
  oracleDbPath,
  snapshotDoc,
  waitForTurnSnapshot,
} from '../fixtures/dbOracle';

// Agent create + provision + a paused turn + the reclaim + oracle reads sit above
// the suite default; keep it generous and env-tunable, like the sibling reclaim specs.
// Bounded budget for the lag between the durable write and the projection the
// pending API reads — not the whole probe: `streamPrompt` already runs the
// POST to the interaction's segment `finish` before the first sample, so this
// only needs to cover the lag beyond that. It keeps the name and value tuned
// for that narrower job (and the env name it shares with the five sibling
// interaction specs) rather than being re-tuned to stand in for the model.
/** What a user says when a model answers instead of acting. */
const INSIST_NUDGE =
  "You answered without using the tool. Do the tool call now, exactly as asked above, before replying again. \u8bf7\u73b0\u5728\u5c31\u6309\u4e0a\u9762\u7684\u8981\u6c42\u8c03\u7528\u5de5\u5177\uff0c\u4e0d\u8981\u53ea\u7528\u6587\u5b57\u56de\u7b54\u3002";

const PROBE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_PENDING_PROBE_TIMEOUT_MS', 60_000);
// The window from dispatch to the model actually reaching for the tool and the
// turn parking on it.
const TIME_TO_GATE_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
// Two windows in sequence — the turn, then the probe — under a single budget when
// the PAGE owns the send: the click returns immediately and everything after it is
// a wait. Same two tuned constants, composed rather than re-tuned (the shape the
// tool-input sibling uses); a probe
// that only covered projection lag would report a SKIP — the one outcome that
// proves nothing while looking like nothing is wrong — for a model that simply
// took a few seconds longer to reach for the tool.
const PAGE_PROBE_BUDGET_MS = TIME_TO_GATE_MS + PROBE_TIMEOUT_MS;
// How long the console may take to draw — and, after the reload, to RE-draw — the
// approval card. The reload re-fetches the bundle and rehydrates from the session
// projection, so keep it generous: a slow rehydrate is not a lost approval.
const PANEL_RENDER_MS = parseTimeoutEnv('ASTRABOX_E2E_RECLAIM_WAITING_RENDER_MS', 60_000);
const CONVERSATION_PREWARM_ENVIRONMENT = String(
  process.env.ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT || '',
).trim();

// The affirmative primary button of the tool-permission card
// (misc:composer.allow_continue / apply_suggestion_continue — en/zh; the console is
// bilingual and the runner's default locale is en-US).
const APPROVE_CONTINUE = /Allow and continue|Apply suggestion and continue|允许并继续|应用建议并继续/;

// Teardown that changes state runs on the passing path only, and hook
// registration order is the old `finally` order: afterEach hooks run in the
// order they are registered, so the session goes before the agent it ran on.
// A kept session pointing at a deleted agent is half a scene.
let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('sandbox reclaim settles an orphaned interaction and the next browser message re-borrows', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();

  // Fail fast (with the descriptive oracle error) if the document store is not
  // reachable — the durable half of this spec depends on it.
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });

  let sessionId = '';
  try {
    // ── Conversation-tenancy Agent + one conversation. Agent preparation removes
    //    Kubernetes cold start from the user-visible recovery path; the
    //    conversation still owns the physical box reclaimed below rather than
    //    sharing an Agent box. ─────────────────────────────────────────────────
    expect(
      CONVERSATION_PREWARM_ENVIRONMENT,
      'the reclaim SLO requires the deployment-proven conversation-prewarm Environment',
    ).not.toEqual('');
    const model = await api.configuredAgentModel(
      process.env.ASTRABOX_E2E_AGENT_NAME || 'Claude Code',
      CONVERSATION_PREWARM_ENVIRONMENT,
    );
    const agent = await api.createAgent({
      name: `__e2e_reclaim_waiting_${runId}`,
      model,
      environment_name: CONVERSATION_PREWARM_ENVIRONMENT,
      prewarm_enabled: false,
    });
    agentId = String(agent.agent_id || '');
    expect(agentId, 'created agent must have an id').not.toEqual('');

    const created = await api.startConversation(agentId);
    sessionId = created.session_id;
    sessions.push(sessionId);
    const ready = await api.waitForSessionReady(sessionId);
    const oldSandbox = String(ready.sandbox_id || '').trim();
    expect(oldSandbox, 'fresh conversation should hold a sandbox').not.toEqual('');
    // No pixel: the console renders no agent-runtime surface on the session screen.
    expect(
      String((ready.agent_runtime as { state?: string } | undefined)?.state || ''),
      'agent is ACTIVE while its conversation is live',
    ).toBe('ACTIVE');
    // Conversation tenancy: an ACTIVE Agent owns no sandbox; the conversation
    // holds the physical box that the reclaim below must destroy.
    expect(
      (ready.agent_runtime as { sandbox_id?: string | null } | undefined)?.sandbox_id ?? null,
      'per-session model: the ACTIVE agent holds no sandbox of its own',
    ).toBeFalsy();

    // ── ARRANGE the permission mode, then hand the conversation to the user. The
    //    console dispatches under the mode it read at load, so this must land
    //    BEFORE the navigation or the composer would send in bypass and no
    //    approval would ever be raised. ────────────────────────────────────────
    await api.setPermissionMode(sessionId, 'default');

    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });

    // ── The user asks for the file, from the real composer. `.fill()` sets the
    //    value without key events, so the prompt is not submitted early. ────────
    const marker = `E2E reclaim waiting interaction ${runId}`;
    const composer = page.getByTestId('composer-prompt');
    await expect(composer, 'the composer must be enabled before sending').toBeEnabled({ timeout: 45_000 });
    await composer.fill(
      `${marker}: 请使用 Write 工具创建相对路径 ` +
        `e2e-reclaim-waiting-${runId}.txt，内容写入 "reclaim while waiting e2e"。` +
        `只创建这一个文件，然后等待我确认，不要继续其它操作。`,
    );
    const submit = page.getByTestId('composer-submit');
    await expect(submit).toBeEnabled({ timeout: 15_000 });
    await submit.click();
    await expect(
      page.getByTestId('user-message').filter({ hasText: marker }).last(),
      'the user bubble should render — proof the turn was dispatched from the page',
    ).toBeVisible({ timeout: 30_000 });

    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. The probe returns the moment the turn settles ungated,
    // so a declined ask costs that turn rather than the whole budget.
    const interaction = await insist<PendingInteraction>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(sessionId, INSIST_NUDGE);
      },
      probe: () =>
        api.waitForPendingInteractionOrSettledTurn(sessionId, PAGE_PROBE_BUDGET_MS),
      what: "deepseek-chat raised no tool-permission interaction for a Write under permission_mode 'default' " + '(the turn finished with stop) — no waiting interaction to reclaim under',
      budgetMs: PAGE_PROBE_BUDGET_MS * 2,
      probeMs: PAGE_PROBE_BUDGET_MS,
    });

    // ── The pending interaction (API projection): a tool permission bound to a
    //    turn, exposing the real tool_call_id. ─────────────────────────────────────
    expect(interaction.presentation, 'pending interaction should be a tool approval').toBe('tool_approval');
    const turnId = String(interaction.turn_id || '').trim();
    expect(turnId, 'pending interaction should bind to a turn').not.toEqual('');
    const toolCallId = String(interaction.tool_call_id || '').trim();
    expect(toolCallId, 'pending interaction should expose the real tool_call_id').not.toEqual('');
    const interactionId = String(interaction.interaction_id || '').trim();
    expect(interactionId, 'pending interaction should carry an interaction_id').not.toEqual('');
    // The page's identity anchor, taken from the SERVER rather than from what the
    // prompt asked for: whatever tool it says is gated must be the tool the card
    // names, before and after the reclaim.
    const toolName = String(interaction.tool_name || '').trim();
    expect(toolName, 'a tool permission should name the tool it is gating').not.toEqual('');

    // ── It reaches the USER: the composer is REPLACED by the approval card
    //    (SessionPage renders one or the other). This is not a state the reclaim is allowed
    //    to leave intact — a question somebody is looking at. ────────────────────
    const panel = page.getByTestId('pending-interaction-panel');
    await expect(
      panel,
      'the raised approval must reach the user as the composer pending panel before the reclaim',
    ).toBeVisible({ timeout: PANEL_RENDER_MS });
    await expect(panel, 'the card should name the tool the server says is gated').toContainText(toolName);
    await expect(
      panel.getByRole('button', { name: APPROVE_CONTINUE }).last(),
      'the card should offer the affirmative button — the approval is answerable',
    ).toBeEnabled({ timeout: PANEL_RENDER_MS });

    // ── Precondition: the turn's durable snapshot records WAITING_FOR_INTERACTION
    //    pointing at THIS interaction — this is the durable "waiting for approval"
    //    the reclaim must settle. Awaited, not sampled: the interaction
    //    becomes readable over HTTP as soon as the kernel registers it, while the
    //    snapshot naming the parked turn is a separate durable write — and because
    //    the page owns the send, no completed POST absorbs that interval, so a
    //    single sample would race the two writes rather than test anything this
    //    spec is about. ─────────────────────────────────────────────────────────
    const pausedBefore = await waitForTurnSnapshot(sessionId, turnId);
    expect(
      String(pausedBefore.conversation_state || ''),
      'the paused turn records WAITING_FOR_INTERACTION',
    ).toBe('WAITING_FOR_INTERACTION');
    expect(
      String(pausedBefore.active_interaction_id || ''),
      'the paused turn points at the pending interaction',
    ).toBe(interactionId);

    // ── Reclaim through the product lifecycle. This command owns settlement
    //    because the in-box approval slot disappears with the sandbox; retaining
    //    the card would leave a button which can never succeed.
    const reclaim = await api.terminateSandbox(sessionId);
    expect(reclaim.session_id, 'the reclaim must target this conversation').toBe(sessionId);
    expect(reclaim.status, 'the lifecycle action must report a compute reclaim')
      .toBe('sandbox-reclaimed');
    expect(
      String(reclaim.sandbox_id || ''),
      'the lifecycle action must name the removed sandbox',
    ).toBe(oldSandbox);
    test.info().annotations.push({ type: 'e2e_reclaimed_sandbox_id', description: oldSandbox });

    // ── Durable close: READY without compute is the wakeable defer boundary;
    //    the original turn is FAILED and no active interaction remains.
    const reclaimed = await api.waitForSession(sessionId, (session) => (
      session.state === 'READY'
      && !session.sandbox_id
      && !session.pending_interaction
      && String(session.last_turn_status || '') === 'FAILED'
    ), 60_000);
    expect(
      reclaimed.runtime_unavailable,
      'a reclaimed conversation advertises recoverable compute loss',
    ).toBe(true);

    await expect.poll(() => {
      const snapshot = snapshotDoc(sessionId);
      return {
        lastTurnId: String(snapshot?.last_turn_id || ''),
        lastTurnStatus: String(snapshot?.last_turn_status || ''),
        activeInteractionId: String(snapshot?.active_interaction_id || ''),
      };
    }, {
      timeout: 30_000,
      message: 'reclaim must durably settle the parked turn and deactivate its interaction',
    }).toEqual({
      lastTurnId: turnId,
      lastTurnStatus: 'FAILED',
      activeInteractionId: '',
    });

    const orphaned = await request.fetch(apiPath(`/sessions/${sessionId}/interaction-respond`), {
      method: 'POST',
      data: { interaction_id: interactionId, answer: { decision: 'approve' } },
      timeout: 30_000,
    });
    const orphanedBody = await orphaned.text();
    expect(
      [400, 409],
      `an orphaned interaction answer must be refused; body=${orphanedBody.slice(0, 300)}`,
    ).toContain(orphaned.status());

    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: PANEL_RENDER_MS });
    await expect(
      panel,
      'a cold page must not resurrect the interaction whose in-box wait was reclaimed',
    ).toHaveCount(0);
    await expect(composer, 'the settled conversation must be immediately sendable').toBeEnabled({
      timeout: PANEL_RENDER_MS,
    });

    // ── The SAME next send rebuilds and completes. Stopping at an enabled
    //    composer would miss a recovery that exists only on paper while the first
    //    real delivery still targets the dead box.
    const followupMarker = `E2E_POST_RECLAIM_${runId}`;
    const assistantsBefore = await page.getByTestId('assistant-message').count();
    await composer.fill(`${followupMarker}: do not use tools; reply briefly.`);
    await page.getByTestId('composer-submit').click();
    await expect(
      page.getByTestId('user-message').filter({ hasText: followupMarker }),
      'the one post-reclaim input must reach the transcript',
    ).toHaveCount(1, { timeout: 30_000 });
    await expect.poll(
      () => page.getByTestId('assistant-message').count(),
      {
        timeout: TIME_TO_GATE_MS,
        message: 'the post-reclaim input must produce an ordinary assistant turn',
      },
    ).toBeGreaterThan(assistantsBefore);

    const rebuilt = await api.waitForSession(sessionId, (session) => (
      session.state === 'READY'
      && Boolean(String(session.sandbox_id || '').trim())
      && String(session.sandbox_id || '').trim() !== oldSandbox
      && String(session.last_turn_status || '') === 'COMPLETED'
      && !session.pending_interaction
    ), TIME_TO_GATE_MS);
    expect(
      String(rebuilt.sandbox_id || ''),
      'the next input must borrow fresh compute',
    ).not.toBe(oldSandbox);
    expect(
      rebuilt.last_error ?? null,
      'the rebuilt turn must not settle through an error bubble',
    ).toBeNull();

    // The agent record stays active while only its conversation replaces compute.
    // A session-level reclaim never terminates the owning agent.
    expect(
      String((rebuilt.agent_runtime as { state?: string } | undefined)?.state || ''),
      'the agent remains ACTIVE while its conversation replaces compute',
    ).toBe('ACTIVE');
    test.info().annotations.push({
      type: 'e2e_reborrowed_sandbox_id',
      description: String(rebuilt.sandbox_id || ''),
    });
  } finally {
    // The session and the agent are not released here. `trackSessions()` and
    // `onPassOnly()` decide in afterEach hooks, where the test's real status is
    // known — see that fixture on why a `finally` cannot tell it is unwinding
    // from a failure, and on why the unit is the whole block.
    // Delete the conversation first (releases its replacement sandbox), then the
    // agent. The lifecycle action already reclaimed the historical compute.
  }
});
