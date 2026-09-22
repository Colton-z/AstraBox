/**
 * E2E: permission-mode changes persist and affect the next turn.
 *
 * Agent conversations start in bypassPermissions. The page then cycles through
 * default, bypassPermissions, and plan using the permission control. Default
 * must hold a Write behind an approval card; rejecting it clears the pending
 * interaction. Bypass must not raise an approval, and plan must not create the
 * requested file. Browser request inspection confirms that each turn carries
 * the mode shown by the badge.
 *
 * Tool selection and plan confirmation depend on the model. The first gated
 * Write is a bounded capability probe; model-independent mode and file-state
 * assertions remain mandatory. API reads retain exact enum values, interaction
 * identifiers, and file bytes that the console does not expose.
 */
import { test, expect } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi, type PendingInteraction } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';

// One sandbox provision + four turns (probe / reject-settle / bypass / plan) with
// their settles; keep it generous and env-tunable, above the suite default.
// Bounded wait for the "default"-mode Write to surface its permission interaction
// before the probe treats the absence as a model no-op and skips (probe-and-skip rule).
/** What a user says when a model answers instead of acting. */
const INSIST_NUDGE =
  "You answered without using the tool. Do the tool call now, exactly as asked above, before replying again. \u8bf7\u73b0\u5728\u5c31\u6309\u4e0a\u9762\u7684\u8981\u6c42\u8c03\u7528\u5de5\u5177\uff0c\u4e0d\u8981\u53ea\u7528\u6587\u5b57\u56de\u7b54\u3002";

const PROBE_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_PERM_MODES_PROBE_TIMEOUT_MS', 60_000);
// Bounded wait for the plan turn's ExitPlanMode confirmation (annotated if absent).
const PLAN_PENDING_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_PERM_MODES_PLAN_PENDING_TIMEOUT_MS', 30_000);
// Wait for a turn / interrupt to settle the session (READY, mode persisted, no pending).
const SETTLE_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_PERM_MODES_SETTLE_TIMEOUT_MS', 90_000);
// Best-effort window for the bypass Write to land before annotating it a no-op.
const WRITE_SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_PERM_MODES_WRITE_SETTLE_MS', 30_000);

// A probe budget started off a blocking `streamPrompt` return begins AFTER the turn
// has already paused or finished, so PROBE_BUDGET_MS covers projection lag alone and
// never the model's thinking time. The composer click returns immediately and offers
// no such boundary, so this budget spans both windows in sequence: the turn, then
// the probe.
const PAGE_PROBE_BUDGET_MS = SETTLE_BUDGET_MS + PROBE_BUDGET_MS;
const PAGE_PLAN_PROBE_BUDGET_MS = SETTLE_BUDGET_MS + PLAN_PENDING_BUDGET_MS;

// downloadFileText throws `files/download -> 404: {…FILE_NOT_FOUND…path not found…}`
// for a missing file (session_file_service path guard: FILE_NOT_FOUND/404).
const NOT_FOUND = /404|FILE_NOT_FOUND|path not found/;

// The engine enum is carried verbatim in data-permission-mode. A tone class is
// presentation, not protocol identity: the SDK may add a mode whose neutral tone
// resembles another one, and a third-party engine may declare a name this test has
// never seen. The label is checked separately over both shipped languages.
const MODE_LABEL = {
  default: /Default mode|默认模式/,
  acceptEdits: /Accept edits|接受编辑/,
  plan: /Plan mode|计划模式/,
  bypassPermissions: /Skip confirmations|跳过确认/,
} as const;
type ModeName = keyof typeof MODE_LABEL;
// The roster trigger has one shipped label in each locale. Mode identities stay
// engine-authored and are selected through each radio item's data attribute.
const MODE_PICKER = /Choose permission mode|选择权限模式/;
// The tool-permission card's negative button (misc:composer.reject_operation, and
// its in-flight label misc:composer.rejecting).
const REJECT_ACTION = /Reject this action|拒绝此次操作|Rejecting|拒绝中/;
// PromptInputSubmit's aria-label while a turn is live ("Stop" / "Stopping").
const STOP_TURN = /^Stop/;
// A FAILED turn renders its error INTO the transcript as an assistant message, so
// "one more non-empty bubble" is satisfied by exactly a failure.
const TURN_ERROR = /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('chat permission modes take effect on create and later turn changes', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = Date.now();

  const pathDefault = `e2e-perm-default-${runId}.txt`;
  const pathBypass = `e2e-perm-bypass-${runId}.txt`;
  const pathPlan = `e2e-perm-plan-${runId}.txt`;
  const bypassMarker = `bypass switch e2e ${runId}`;

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  const panel = page.getByTestId('pending-interaction-panel');
  const pill = page.getByTestId('run-view').getByTestId('status-pill').first();

  // The mode each turn actually left the browser under, in send order. The console
  // sends on POST /sessions/{id}/turn-inputs (a receipt — the frames come back on the
  // session stream), carrying `permission_mode` from the strip. Reading the request
  // the page issues is the only oracle that binds the badge the user was looking at
  // to the mode the turn ran under, and it perturbs nothing.
  const sentModes: string[] = [];
  page.on('request', (req) => {
    if (req.method() !== 'POST' || !req.url().includes('/turn-inputs')) return;
    try {
      const body = JSON.parse(req.postData() || '{}') as { permission_mode?: unknown };
      sentModes.push(String(body.permission_mode ?? ''));
    } catch {
      // Not a JSON turn dispatch; nothing to read.
    }
  });

  /** Which mode the composer's badge is showing right now. */
  const visibleMode = async (): Promise<string> => {
    const badge = page.getByTestId('permission-mode-badge');
    await expect(badge, 'exactly one permission mode may be shown').toHaveCount(1);
    return String(await badge.getAttribute('data-permission-mode') || 'none');
  };

  /**
   * Change the mode the way a user does: open the engine's complete mode roster and
   * pick the target. The trigger is absent while a switch is in flight or the session
   * is not idle, so Playwright's actionability wait keeps the selection from being
   * dropped; because the badge only moves after setSessionPermissionMode resolves, a
   * badge that moved IS the server's acknowledgement of the switch.
   */
  const selectModeFromPicker = async (target: ModeName) => {
    const detail = await api.getSession(sessionId);
    const availableModes = detail.engine_capabilities?.permission_modes ?? [];
    expect(
      availableModes,
      `the engine manifest must declare the mode this scenario needs (${target})`,
    ).toContain(target);
    if ((await visibleMode()) !== target) {
      const picker = page.getByRole('button', { name: MODE_PICKER });
      await expect(picker, 'the permission roster must be available while the session is idle')
        .toBeEnabled({ timeout: 30_000 });
      await picker.click();
      const option = page.locator(
        `[role="menuitemradio"][data-permission-option="${target}"]`,
      );
      await expect(option, `the permission roster should include ${target}`)
        .toBeVisible({ timeout: 15_000 });
      await option.click();
      await expect
        .poll(visibleMode, {
          timeout: 30_000,
          message: `the permission badge should move to ${target}`,
        })
        .toBe(target);
    }
    const badge = page.getByTestId('permission-mode-badge');
    await expect(
      badge,
      `the composer should show the conversation switched to ${target}`,
    ).toHaveAttribute('data-permission-mode', target);
    await expect(badge, 'the badge tone and the label the user reads must agree').toHaveText(MODE_LABEL[target]);
  };

  /**
   * Send from the real composer and confirm the console dispatched the turn under
   * the mode the badge is showing. `.fill()` sets the value without key events, so a
   * multi-line prompt is not submitted early by an Enter newline.
   */
  const sendFromComposer = async (prompt: string, echo: string, expectedMode: ModeName) => {
    const before = sentModes.length;
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
    await expect.poll(() => sentModes.length, { timeout: 30_000 }).toBeGreaterThan(before);
    expect(
      sentModes[sentModes.length - 1],
      `the console must dispatch this turn under the mode it is showing (${expectedMode})`,
    ).toBe(expectedMode);
  };

  /**
   * End whatever the turn is still doing, from the page, and wait until the session
   * is genuinely idle. The action is the user's own stop button — the card's while an
   * approval is up, the composer's otherwise — and not the card's reject button:
   * a rejected model often re-asks straight away, so answering the gate does not end
   * the turn while an interrupt does.
   *
   * The exit condition is read off the projection, not the header: the console's idle
   * flag follows a 1.5–3s poll, so for a beat after a send the pill still shows the
   * PREVIOUS settled state and a helper that trusted it would hand the next step a
   * session that is not idle at all — and the mode cycle is only offered while it is.
   * This is synchronisation for the next user action, not an oracle; the panel and
   * pill assertions at the call sites are.
   */
  const stopAndSettle = async (budgetMs: number): Promise<boolean> => {
    const deadline = Date.now() + budgetMs;
    let settledSamples = 0;
    while (Date.now() < deadline) {
      const session = await api.getSession(sessionId);
      if (String(session.state || '') === 'READY' && !session.pending_interaction) {
        // Two samples ~2s apart, because the projection reads READY for a beat
        // between an answered interaction and the continuation the model starts on
        // the same turn. Handing that beat to the next step would find the mode
        // cycle disabled and burn an actionability timeout on it.
        settledSamples += 1;
        if (settledSamples >= 2) return true;
      } else {
        settledSamples = 0;
        const carded = (await panel.count()) > 0;
        const stop = carded
          ? panel.getByRole('button', { name: STOP_TURN })
          : page.getByTestId('run-composer-stop');
        if (await stop.count()) {
          await stop.first().click({ timeout: 10_000 }).catch(() => {});
        }
      }
      await page.waitForTimeout(2_000);
    }
    return false;
  };

  try {
    // ── ON CREATE: the conversation opens in bypassPermissions. ───────────────
    // The exact enum has no pixel (see the header), so the create route's hardcoded
    // default is read off the projection here and confirmed as the user meets it —
    // the composer badge — the moment the page is open.
    const ready = await api.waitForSessionReady(sessionId);
    expect(
      ready.permission_mode,
      'a fresh agent conversation should open in bypassPermissions',
    ).toBe('bypassPermissions');

    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
    const bypassBadge = page.getByTestId('permission-mode-badge');
    await expect(
      bypassBadge,
      'a fresh conversation should tell the user it skips confirmations',
    ).toHaveAttribute('data-permission-mode', 'bypassPermissions', { timeout: 30_000 });
    await expect(bypassBadge).toHaveText(MODE_LABEL.bypassPermissions);

    // ── PROBE + core property: a switch to "default" GATES the Write. ─────────
    // The user flips the mode on the strip and then asks for a file. Under "default"
    // the console must stop them for approval before anything is written.
    await selectModeFromPicker('default');
    const defaultEcho = `E2E 权限门控探针 ${runId}`;
    await sendFromComposer(
      `${defaultEcho}：请立即使用 Write 工具，在当前工作目录下创建相对路径文件 ${pathDefault}，写入文本 default gated e2e。只创建这一个文件，创建后请停下等待我确认，不要做别的事。`,
      defaultEcho,
      'default',
    );

    // Kept on the API deliberately (see the header): the skip is for the MODEL
    // declining to drive the tool. A gate that IS raised but never reaches the
    // screen fails at the page assertion below — it is never skipped.
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. The probe returns the moment the turn settles ungated,
    // so a declined ask costs that turn rather than the whole budget.
    const probePending = await insist<PendingInteraction>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(sessionId, INSIST_NUDGE);
      },
      probe: () =>
        api.waitForPendingInteractionOrSettledTurn(sessionId, PAGE_PROBE_BUDGET_MS),
      what: `permission gating unverifiable: a "default"-mode Write raised no tool_permission ` + `interaction within ${PAGE_PROBE_BUDGET_MS}ms. deepseek-chat behind the gateway can finish ` + `with stop without driving the Write tool, so there is nothing to gate — not fake-passed.`,
      budgetMs: PAGE_PROBE_BUDGET_MS * 2,
      probeMs: PAGE_PROBE_BUDGET_MS,
    });

    // The switch to "default" took effect: it gated the edit tool — and the gate
    // reached the USER, as the approval card that replaces the composer, with the
    // header saying the turn is waiting on them rather than still generating.
    await expect(
      panel,
      'default mode should hold the edit behind an approval card the user can answer',
    ).toBeVisible({ timeout: SETTLE_BUDGET_MS });
    await expect(pill, 'the header should read as waiting on the user, not generating').toHaveAttribute(
      'data-state',
      'WAITING_INPUT',
      { timeout: 30_000 },
    );

    expect(probePending.presentation, 'default mode should hold the edit behind a tool approval').toBe('tool_approval');
    // The gated tool name reflects the model's actual choice (deepseek may reach
    // for another edit tool despite the explicit "use Write" prompt), so the
    // hard check is "a tool was gated" — the
    // pathDefault-absence assertion below carries "the requested write specifically
    // did not happen".
    expect(probePending.tool_name, 'default mode should gate the tool the model reached for').toBeTruthy();
    expect(probePending.turn_id, 'the pending interaction should bind to a turn').toBeTruthy();
    expect(probePending.interaction_id, 'the pending interaction should expose a stable id').toBeTruthy();
    // The ids have no pixel, but the tool name does — asserting it ties the card on
    // screen to the interaction the platform actually raised, rather than checking
    // the two independently and hoping they are the same one.
    await expect(
      panel,
      'the card on screen must be about the tool the platform gated',
    ).toContainText(String(probePending.tool_name));
    if (String(probePending.tool_name) !== 'Write') {
      test.info().annotations.push({
        type: 'e2e_gated_tool_not_write',
        description: `default-mode gate fired on tool_name=${String(probePending.tool_name)} (expected Write)`,
      });
    }

    const gated = await api.getSession(sessionId);
    expect(gated.permission_mode, 'the turn should run under the switched-to default').toBe('default');
    // No pixel: the console renders no file bytes anywhere.
    await expect(
      api.downloadFileText(sessionId, pathDefault),
      'a gated Write must not create the file while the permission is pending',
    ).rejects.toThrow(NOT_FOUND);

    // ── Rejecting the interaction CLEARS the gate. ────────────────────────────
    // Pressed on the card, where the user meets it: the whole point of a gating mode
    // is that saying no is reachable and that it un-blocks the screen.
    const reject = panel.getByRole('button', { name: REJECT_ACTION });
    await expect(reject, 'the approval card must offer the user a way to say no').toBeEnabled({ timeout: 30_000 });
    await reject.click();
    await expect(
      panel,
      'rejecting the Write permission should clear the approval card off the composer',
    ).toBeHidden({ timeout: SETTLE_BUDGET_MS });
    expect(
      await stopAndSettle(SETTLE_BUDGET_MS),
      'the rejected turn should end and leave the user a usable composer',
    ).toBe(true);
    // The half of that sentence the projection cannot say. A session that reads
    // READY behind a composer still disabled is a conversation the user cannot
    // continue — saying no has to hand the screen back, not just clear a row.
    await expect(
      page.getByTestId('composer-prompt'),
      'rejecting should return the composer to the user',
    ).toBeEnabled({ timeout: 30_000 });

    // ── LATER TURN CHANGE → bypassPermissions: the identical Write is NOT gated.
    await selectModeFromPicker('bypassPermissions');
    const bypassEcho = `E2E 权限直通切换 ${runId}`;
    const assistantsBefore = await page.getByTestId('assistant-message').count();
    await sendFromComposer(
      `${bypassEcho}：请立即使用 Write 工具，在当前工作目录下创建相对路径文件 ${pathBypass}，文件内容必须精确等于：${bypassMarker}。只创建这一个文件。`,
      bypassEcho,
      'bypassPermissions',
    );

    // Re-checking for the card on EVERY tick, rather than once at the end, is what
    // makes this the contrast it claims to be: a turn parked on a permission emits
    // nothing further, so a reply-only wait would sit to the full budget and then
    // report a missing reply instead of the gate that caused it.
    const replyDeadline = Date.now() + PAGE_PROBE_BUDGET_MS;
    let replied = false;
    while (Date.now() < replyDeadline) {
      expect(
        await panel.count(),
        'bypassPermissions must run the same Write that "default" held, without stopping the user',
      ).toBe(0);
      // Count, not text: the model's wording is its own business, and a spec that
      // pins wording fails on a model that is behaving correctly.
      if ((await page.getByTestId('assistant-message').count()) > assistantsBefore) {
        replied = true;
        break;
      }
      await page.waitForTimeout(1_000);
    }
    expect(replied, `no assistant reply rendered within ${PAGE_PROBE_BUDGET_MS}ms`).toBe(true);
    const bypassReply = page.getByTestId('assistant-message').last();
    await expect(bypassReply).not.toBeEmpty();
    // A bubble that grew the count is not yet a reply: a failed turn renders its
    // error INTO the transcript as an assistant message, so "one more non-empty
    // bubble" is satisfied by exactly a failure.
    await expect(bypassReply).not.toContainText(TURN_ERROR);
    await expect(pill, 'the bypass turn must settle the header, not leave it live').toHaveAttribute(
      'data-pulse',
      'false',
      { timeout: SETTLE_BUDGET_MS },
    );
    // The loop above stops watching at the first delta of the reply, and a tool runs
    // after that: re-check the tail of the turn, where a Write permission would
    // actually be raised.
    await expect(
      panel,
      'no approval card may appear in the tail of the bypass turn either',
    ).toHaveCount(0);

    // The projection behind the settled screen — the exact enum has no pixel, and
    // this is also what makes the session idle enough to switch modes again.
    const bypassSettled = await api.waitForSession(
      sessionId,
      (s) =>
        String(s.state || '') === 'READY' &&
        s.permission_mode === 'bypassPermissions' &&
        !s.pending_interaction,
      SETTLE_BUDGET_MS,
    );
    expect(
      bypassSettled.permission_mode,
      'a per-turn switch to bypassPermissions should persist',
    ).toBe('bypassPermissions');
    expect(
      bypassSettled.pending_interaction,
      'bypassPermissions should not gate the same Write that "default" held — the switch took effect',
    ).toBeFalsy();

    // Best-effort teeth (model-dependent): if the model drove the Write, the file
    // exists under bypass — proof the tool ran unblocked. No pixel: the console shows
    // no file content. A missing file is annotated (after re-confirming nothing is
    // pending, on the page and in the projection), not failed.
    const bypassDeadline = Date.now() + WRITE_SETTLE_MS;
    let bypassText: string | null = null;
    let lastBypassError = '';
    while (Date.now() < bypassDeadline) {
      try {
        bypassText = await api.downloadFileText(sessionId, pathBypass);
        break;
      } catch (error) {
        lastBypassError = String((error as Error)?.message ?? error);
        await new Promise((r) => setTimeout(r, 2_000));
      }
    }
    if (bypassText !== null) {
      expect(
        bypassText,
        'a Write under bypassPermissions should land the requested content',
      ).toContain(bypassMarker);
    } else {
      const stillPending = await api.getPendingInteraction(sessionId);
      expect(
        stillPending,
        'a missing bypass file must be a model no-op, never a suppressed permission prompt',
      ).toBeFalsy();
      await expect(
        panel,
        'a missing bypass file must not be a permission the console is holding on screen',
      ).toHaveCount(0);
      test.info().annotations.push({
        type: 'e2e_bypass_write_not_exercised',
        description:
          `model did not create ${pathBypass} within ${WRITE_SETTLE_MS}ms; ` +
          `bypass no-gate invariant asserted. last=${lastBypassError}`,
      });
    }

    // ── LATER TURN CHANGE → plan: writes are gated; ExitPlanMode is expected. ──
    await selectModeFromPicker('plan');
    const planEcho = `E2E 权限计划切换 ${runId}`;
    await sendFromComposer(
      `${planEcho}：你现在处于 plan mode。请先调用 ExitPlanMode 工具提交一个简短计划以请求退出 plan mode，不要直接创建任何文件。只有在计划获批之后，才能创建相对路径文件 ${pathPlan}。`,
      planEcho,
      'plan',
    );

    // Model-dependent shape, so the same API probe as above: the wait bounds how long
    // the plan turn gets to raise its confirmation, and doubles as "the turn has had
    // its chance" before the no-write assertion.
    const planPending: PendingInteraction | null = await api
      .waitForPendingInteraction(sessionId, PAGE_PLAN_PROBE_BUDGET_MS)
      .catch(() => null);

    // Model-independent invariant: no write slipped through while in plan mode. No
    // pixel — the console renders no file content.
    await expect(
      api.downloadFileText(sessionId, pathPlan),
      'plan mode must not let the target file be written',
    ).rejects.toThrow(NOT_FOUND);

    // Assert the plan_confirmation only when it surfaces — and assert it where the
    // user has to act on it. The exit-plan card is the only interaction card that
    // renders its choices as a radiogroup, which identifies it on screen without
    // pinning a translation.
    if (planPending && planPending.presentation === 'decision') {
      await expect(
        panel,
        'a plan-mode confirmation should reach the user as the composer approval card',
      ).toBeVisible({ timeout: 30_000 });
      await expect(
        panel.getByRole('radiogroup'),
        'the card should offer the exit-plan choices, not a bare tool permission',
      ).toBeVisible({ timeout: 30_000 });
      expect(
        planPending.tool_name,
        'a plan-mode confirmation should come from ExitPlanMode',
      ).toBe('ExitPlanMode');
    } else {
      test.info().annotations.push({
        type: 'e2e_plan_confirmation_not_raised',
        description:
          `plan mode did not surface an ExitPlanMode plan_confirmation within ` +
          `${PAGE_PLAN_PROBE_BUDGET_MS}ms (pending=${planPending ? String(planPending.presentation) : 'none'}); ` +
          `mode-persist + no-write invariants asserted.`,
      });
    }

    // The switch to plan PERSISTED — proven the way a user would find out. The badge
    // on its own cannot say it: usePermissionModeControl only re-seeds from the
    // session when the server's value CHANGES, so a mode that never persisted would
    // leave the local badge standing. A reloaded page has no client memory at all, so
    // the badge it draws can only have come from the session the server rehydrated.
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
    const planBadge = page.getByTestId('permission-mode-badge');
    await expect(
      planBadge,
      'a reloaded console should still show the conversation in plan mode — the switch persisted',
    ).toHaveAttribute('data-permission-mode', 'plan', { timeout: 45_000 });
    await expect(planBadge).toHaveText(MODE_LABEL.plan);

  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
