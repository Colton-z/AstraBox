/**
 * E2E: replacing a session sandbox preserves conversation identity and history.
 *
 * After a baseline turn, the sandbox compute is killed and the next composer
 * turn must provision a new sandbox. linux_user, home_dir, workspace_dir,
 * template_capability_hash, and engine_session_key must remain stable. The page
 * must retain the earlier transcript, append the new reply, and keep a concurrent
 * conversation's messages isolated.
 *
 * Workspace file readability is outside this scenario and requires a
 * backend-specific storage fixture. Prompts forbid tools.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, type AdminSessionRecord } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import {
  killSandbox,
  requireSandboxHandle,
  sandboxRunning,
  waitForSandboxStopped,
} from '../fixtures/sandboxOps';
import { appPath, parseTimeoutEnv } from '../fixtures/env';

// Three sequential turns and three sandbox provisions require a configurable
// lifecycle-sized budget.
// Abrupt removal converges quickly; this gates the rebuild so it lands on
// genuinely dead compute and the SANDBOX_GONE re-borrow fires.
const KILL_CONVERGE_MS = parseTimeoutEnv('ASTRABOX_E2E_KILL_CONVERGE_MS', 30_000);
// The re-borrow turn (fresh sandbox + resume + reply) is the long pole.
const REBORROW_TURN_MS = parseTimeoutEnv('ASTRABOX_E2E_REBORROW_TURN_TIMEOUT_MS', 240_000);
// The two healthy turns keep the suite-wide turn knob the API path spent
// implicitly (streamPrompt / waitForAssistantMessageCount default) — same
// budget, now waiting for the bubble to render rather than for the SSE body.
const TURN_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);

/** Assert the operator detail carries a runtime_identity with a linux_user; return it. */
function identityOf(detail: AdminSessionRecord, label: string): Record<string, unknown> {
  const identity = detail.runtime_identity;
  expect(identity, `${label} should expose runtime_identity`).toBeTruthy();
  expect(
    String((identity as Record<string, unknown>)?.linux_user || '').trim(),
    `${label} runtime_identity should expose linux_user`,
  ).not.toEqual('');
  return identity as Record<string, unknown>;
}

// Teardown that changes state runs on the passing path only, and hook
// registration order is the old `finally` order: afterEach hooks run in the
// order they are registered.
//
// `sessionIds` IS the tracker's array, so the existing pushes are unchanged.
const sessionIds = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('session sandbox replacement preserves the conversation identity and thread', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  const agentName = `__e2e_uidgid_${runId}`;
  // One marker per message, carried in the user's own bubble. On the page these
  // ARE the identity of a transcript: the historical marker must still be in
  // place after the replacement, the rebuild marker must land after it in the
  // SAME thread, and the concurrent occupant's must never appear there at all.
  const historicalMarker = `OLD-${runId}`;
  const occupantMarker = `OCC-${runId}`;
  const rebuildMarker = `NEW-${runId}`;

  // Source a concrete model + environment from the seeded default agent — the
  // create validator (agent_schema) requires a non-blank model — skipping the
  // litellm wildcard routing keys, exactly as the sibling sandbox specs do.
  const base = await api.defaultAgent();
  const environmentName = String(base.environment_name || '').trim();
  expect(environmentName, 'seeded default agent must name an environment').not.toEqual('');
  const models = await api.listEnvironmentModels(environmentName);
  const model = models.find((m) => m && !m.includes('*')) || 'deepseek-chat';

  try {
    // ── Isolated agent (pure metadata, its own monotonic uid sequence). ────
    // Creating the agent stays on the API: it is arrangement, and a user picks
    // an agent that already exists rather than authoring one to have a chat.
    const agent = await api.createAgent({
      name: agentName,
      model,
      environment_name: environmentName,
    });
    agentId = String(agent.agent_id || '');
    expect(agentId, 'created agent must have an id').not.toEqual('');

    const card = page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`);

    /** Start a conversation the user's way — the agent picker — and return its id. */
    const startConversationFromCard = async (): Promise<string> => {
      await page.goto(appPath('/agents'));
      await expect(card, 'the agent must be offered on the picker').toBeVisible({ timeout: 30_000 });
      // One button per card — the Badge, title and model line are all spans.
      await card.getByRole('button').click();
      // The console navigates to the conversation it just created, and that
      // route IS the user-visible identity of the conversation.
      await page.waitForURL((url) => /\/sessions\/[^/]+$/.test(url.pathname), { timeout: 120_000 });
      const sessionId = new URL(page.url()).pathname.split('/').filter(Boolean).pop() || '';
      expect(sessionId, 'starting a conversation must open its own /sessions/<id> route').not.toEqual('');
      sessionIds.push(sessionId);
      await expect(page.getByTestId('run-view')).toBeVisible();
      return sessionId;
    };

    /** Send from the composer and wait for one more rendered assistant bubble. */
    const sendAndReadReply = async (prompt: string, marker: string, budgetMs: number) => {
      const before = await page.getByTestId('assistant-message').count();
      await page.getByTestId('composer-prompt').fill(prompt);
      await page.getByTestId('composer-submit').click();
      const queuedPrompt = page.getByTestId('composer-queue').filter({ hasText: marker });
      const userMessage = page.getByTestId('user-message').filter({ hasText: marker });
      await expect(
        queuedPrompt.or(userMessage).first(),
        'the accepted input remains visible in the queue or transcript',
      ).toBeVisible({ timeout: 30_000 });
      await expect(
        userMessage,
        'engine consumption hands the input from the queue to one transcript row',
      ).toHaveCount(1, { timeout: budgetMs });
      await expect(
        queuedPrompt,
        'the queue stops owning the input after the transcript receives it',
      ).toHaveCount(0, { timeout: budgetMs });
      // Count, not text: the model's wording is its own business, and a spec that
      // pins wording fails on a model that is behaving correctly.
      await expect
        .poll(() => page.getByTestId('assistant-message').count(), { timeout: budgetMs })
        .toBeGreaterThan(before);
      const reply = page.getByTestId('assistant-message').last();
      await expect(reply).not.toBeEmpty();
      // A bubble that grew the count is not yet a settled reply: while it carries
      // data-streaming=true, a provider/runtime error can still be appended after
      // a negative text assertion has already passed. Wait for both the bubble
      // and the scoped header to settle before ruling out the failure surface.
      await expect(reply).not.toHaveAttribute('data-streaming', 'true', { timeout: budgetMs });
      await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute(
        'data-pulse',
        'false',
        { timeout: 60_000 },
      );
      await expect(reply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
      return reply;
    };

    // ── Historical conversation: started from the card; its first turn
    //    provisions the sandbox whose numeric owner gets captured below. ────
    const historicalId = await startConversationFromCard();
    // The composer is disabled until the conversation has its box, so waiting
    // for READY is a precondition of typing, not an oracle of its own.
    const historicalStartReady = await api.waitForSessionReady(historicalId);

    await sendAndReadReply(
      `E2E uidgid old sandbox ${historicalMarker}: 请简短回复一句话，不要使用工具。`,
      historicalMarker,
      TURN_BUDGET_MS,
    );

    const historicalReady = await api.waitForSessionReady(historicalId);
    const oldSandbox = String(historicalReady.sandbox_id || '').trim();
    expect(oldSandbox, 'historical conversation should hold a sandbox_id when READY').not.toEqual('');
    test.info().annotations.push({ type: 'e2e_uidgid_old_sandbox_id', description: oldSandbox });

    // Runtime identity is operator-only: uid, gid, linux_user, home_dir,
    // workspace_dir and template_capability_hash are not part of the owner
    // Session DTO or any console screen.
    const oldIdentity = identityOf(
      await api.adminSessionDetail(historicalId),
      'historical session before replacement',
    );
    expect(
      String(oldIdentity.sandbox_id || '').trim(),
      'runtime_identity.sandbox_id should be projected from the session sandbox',
    ).toBe(oldSandbox);
    const linuxUser = String(oldIdentity.linux_user || '').trim();
    const homeDir = String(oldIdentity.home_dir || '').trim();
    expect(homeDir, 'historical runtime_identity should expose home_dir').not.toEqual('');
    const workspaceDir = String(oldIdentity.workspace_dir || '').trim();
    expect(workspaceDir, 'historical runtime_identity should expose workspace_dir').not.toEqual('');
    const oldCapabilityHash = String(oldIdentity.template_capability_hash || '').trim();
    expect(
      oldCapabilityHash,
      'historical runtime_identity should carry a template_capability_hash (deterministic per agent/template)',
    ).not.toEqual('');

    // The engine's opaque native conversation key is read from the owner/admin
    // detail projection — it must survive the sandbox replacement unchanged. No
    // pixel, and the transcript is not a stand-in for it: the console renders
    // the platform's mirror, not the sandbox's resumed Claude history.
    const historicalDetail = await api.adminSessionDetail(historicalId);
    const engineSessionKey = String(historicalDetail.engine_session_key || '').trim();
    expect(engineSessionKey, 'historical session should persist an engine_session_key before replacement').not.toEqual('');

    // ── A concurrent conversation in the same Agent scope, started and driven
    //    from the page like the first one. Its job here is the isolation half:
    //    its messages must never appear in the historical thread, before or
    //    after the replacement.
    const occupantId = await startConversationFromCard();
    expect(occupantId, 'the second start must open a DIFFERENT conversation').not.toEqual(historicalId);
    await api.waitForSessionReady(occupantId);
    await sendAndReadReply(
      `E2E uidgid occupant ${occupantMarker}: 请简短回复一句话，不要使用工具。`,
      occupantMarker,
      TURN_BUDGET_MS,
    );
    await api.waitForSessionReady(occupantId);
    identityOf(await api.adminSessionDetail(occupantId), 'occupant session');

    // ── Reclaim the original sandbox out of band. Compute disappears while the
    //    session row still points at oldSandbox, so the next message must rebuild.
    const oldSandboxHandle = await requireSandboxHandle(api, oldSandbox);
    expect(
      sandboxRunning(oldSandboxHandle),
      'historical sandbox should be running before the reclaim',
    ).toBe(true);
    killSandbox(oldSandboxHandle);
    await waitForSandboxStopped(oldSandboxHandle, KILL_CONVERGE_MS);
    expect(sandboxRunning(oldSandboxHandle), 'the reclaimed sandbox should be gone').toBe(false);
    test.info().annotations.push({ type: 'e2e_uidgid_reclaimed_sandbox_id', description: oldSandbox });

    // ── Back to the historical conversation the way a user goes back: its row
    //    in the sessions sidebar. Nothing on screen says the box died — the row
    //    still reads healthy because the platform never observed the death —
    //    which is exactly the state the rebuild has to survive. The transcript
    //    the console re-fetches here is the durable history, and it must already
    //    hold the pre-replacement message and only that conversation's messages.
    const historicalRow = page.locator(`[data-testid="session-row"][data-session-id="${historicalId}"]`);
    await expect(historicalRow, 'the historical conversation must still be listed').toBeVisible({ timeout: 30_000 });
    await historicalRow.getByRole('link').click();
    await page.waitForURL((url) => url.pathname.endsWith(`/sessions/${historicalId}`), { timeout: 60_000 });
    await expect(page.getByTestId('run-view')).toBeVisible();
    const userBubbles = page.getByTestId('user-message');
    await expect(
      userBubbles.filter({ hasText: historicalMarker }),
      'the pre-replacement message must still be on screen',
    ).toHaveCount(1);
    await expect(
      userBubbles.filter({ hasText: occupantMarker }),
      "the occupant conversation's message must not bleed into this transcript",
    ).toHaveCount(0);

    // The rebuild's oracle is "one MORE assistant bubble than there were", so
    // the baseline has to be taken against the fully re-rendered history. Taken
    // while the transcript was still arriving it would read zero, and the
    // historical reply — already persisted, just not painted yet — would satisfy
    // the poll on its own, passing the rebuild without the rebuild ever landing.
    await expect(
      page.getByTestId('assistant-message').first(),
      "the historical turn's reply must be back on screen before the rebuild baseline is taken",
    ).toBeVisible();

    // ── Rebuild via the next message, typed into the composer of a conversation
    //    whose compute is already dead. Same send, no resend: the re-borrow
    //    RESTARTS the stream, so the reply arrives on a second connection — the
    //    page is the one oracle that does not care which connection carried it
    //    (the API form of this spec had to swallow the first stream's error and
    //    fall back to polling persisted messages to see the same delivery).
    await sendAndReadReply(
      `E2E uidgid rebuild ${rebuildMarker}: 请再简短回复一句话，不要使用工具。`,
      rebuildMarker,
      REBORROW_TURN_MS,
    );

    // And the header settles. A replacement that delivers the text but leaves
    // the turn looking live is still a broken screen. Scoped to the run-view
    // because the sidebar renders one status-pill PER conversation row, and an
    // unscoped .first() would read some other conversation's pill.
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute(
      'data-pulse',
      'false',
      { timeout: 60_000 },
    );

    // The user-visible shape of "the replacement is a non-event": ONE continuous
    // thread — the pre-replacement message still in its place, the rebuild after
    // it, and the concurrent conversation still absent. A rebuild that quietly
    // rehomed the conversation onto a new/empty thread would pass every row-level
    // assertion below and be obvious here.
    // Asserted by POSITION and by the older message's own count, not by a total
    // bubble count: whether the re-borrow's re-dispatch persists the user's
    // message once or twice has never been established on this stack, and a
    // strict total here would go red on that unrelated question. What must hold
    // either way is that the message from BEFORE the replacement was not
    // duplicated, moved, or dropped by it.
    await expect(
      userBubbles.filter({ hasText: historicalMarker }),
      'the pre-replacement message survives the replacement exactly once',
    ).toHaveCount(1);
    await expect(userBubbles.first(), 'the pre-replacement message keeps its place').toContainText(historicalMarker);
    await expect(userBubbles.last(), 'the rebuild message is the newest one').toContainText(rebuildMarker);
    await expect(
      userBubbles.filter({ hasText: occupantMarker }),
      "the occupant conversation's message must not appear after the replacement either",
    ).toHaveCount(0);

    // ── THE core properties, on the rebound session. Everything from here down
    //    is row/identity state with no pixel — see the header for what the
    //    console would have to expose for any of it to be provable on screen.
    const rebound = await api.waitForSessionReady(historicalId);
    const newSandbox = String(rebound.sandbox_id || '').trim();
    expect(newSandbox, 'rebuilt conversation should hold a sandbox_id').not.toEqual('');
    expect(newSandbox, `rebuilt sandbox must differ from the reclaimed one (old=${oldSandbox})`).not.toBe(oldSandbox);
    test.info().annotations.push({ type: 'e2e_uidgid_new_sandbox_id', description: newSandbox });

    // Calm terminal state: the replacement is a non-event for the row too. The
    // page proved the user-facing half (a clean reply, a settled header); these
    // three are the durable fields behind it — last_error only ever renders on a
    // terminated / runtime-unavailable session and on an untagged `<p>`, so a
    // healthy row's cleanliness cannot be read off the screen.
    expect(String(rebound.state || ''), 'rebuilt session should read READY').toBe('READY');
    expect(Boolean(rebound.runtime_unavailable), 'rebuilt session should not be runtime-unavailable').toBe(false);
    expect(String(rebound.last_error || '').trim(), 'rebuilt session should retain no last_error').toBe('');

    const newIdentity = identityOf(
      await api.adminSessionDetail(historicalId),
      'historical session after replacement',
    );
    expect(String(newIdentity.sandbox_id || '').trim(), 'runtime_identity.sandbox_id should track the new sandbox').toBe(
      newSandbox,
    );

    // Identity STABILITY — everything that identifies the conversation (not its
    // numeric sandbox-local owner) survives the replacement.
    expect(String(newIdentity.linux_user || '').trim(), 'linux_user should be stable across replacement').toBe(linuxUser);
    expect(String(newIdentity.home_dir || '').trim(), 'home_dir should be stable across replacement').toBe(homeDir);
    expect(String(newIdentity.workspace_dir || '').trim(), 'workspace_dir should be stable across replacement').toBe(
      workspaceDir,
    );

    // template_capability_hash CURRENT, not dropped: the agent/template is
    // unchanged and the hash is a deterministic sha256 of it, so whatever the
    // rebuild's identity carries must equal the pre-replacement value. This
    // proves the rebuilt sandbox did not lose its capability hash.
    expect(
      String(newIdentity.template_capability_hash || '').trim(),
      'rebuild should carry the current template_capability_hash, not drop or blank it',
    ).toBe(oldCapabilityHash);

    // NAS history handle survives the sandbox replacement. The transcript above
    // does NOT prove this — it is served from the platform's mirror, so the old
    // bubbles would still render even if the rebuilt box started a fresh Claude
    // session. This is the only place that distinction is observable.
    const reboundDetail = await api.adminSessionDetail(historicalId);
    expect(
      String(reboundDetail.engine_session_key || '').trim(),
      'the conversation NAS history handle (engine_session_key) should survive the replacement',
    ).toBe(engineSessionKey);
  } finally {
    // Delete each conversation first (releases its CURRENT sandbox), then the
    // agent. The out-of-band fault has already made the historical compute
    // unreachable; only the current replacement remains bound to the session.
    // Nothing here is released on a failing run. `trackSessions()` and
    // `onPassOnly()` decide in afterEach hooks, where the test's real status is
    // known — see that fixture on why the unit is the whole block.
  }
});
