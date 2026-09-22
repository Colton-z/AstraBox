/**
 * E2E: an Agent with an unresolvable Environment remains ACTIVE and never owns
 * a sandbox. Configuration resolution belongs to conversation provisioning;
 * wake and hibernate do not resolve the Agent configuration.
 *
 * The test creates a valid Agent, then uses the database fault fixture to model
 * its Environment becoming unavailable after authoring. Public Agent writes
 * correctly reject a missing Environment; the out-of-band mutation exercises the
 * recovery scenario without weakening that write-side gate. The Agent's
 * record page must show the invalid value and let an operator restore a valid
 * Environment. API assertions retain exact state, environment_name, and
 * sandbox_id values that the UI does not expose precisely.
 *
 * The scenario does not run a model turn. It mutates persisted Agent metadata, so it
 * is exclusive. A passing run restores the original row before teardown; a failing
 * run keeps the faulted Agent as diagnostic evidence.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, type AgentRecord } from '../fixtures/astraApi';
import { patchDocs, restoreDoc } from '../fixtures/dbOracle';
import { appPath } from '../fixtures/env';
import { onPassOnly } from '../fixtures/sessionCleanup';

// No sandbox provisioning and no turn happen here — only fast metadata operations
// plus a handful of console page loads. The only slow leg is createAgent (up to its
// own 120s API budget) plus one environment /v1/models read.

// Both surfaces this spec reads are LOCALIZED: the card's state badge
// (agents:state_active → "Active" / "运行中") and the management console's labels
// ("Runtime environment", "Environment", Save, the search placeholder). The console
// detects language as ['localStorage','navigator'] with fallbackLng 'en', so an
// unpinned runner locale decides what they say. Pin it to English — a fixed navigator
// locale plus the persisted 'astrabox-lang' the app's own switch writes (localStorage
// is the highest-priority source) — exactly as the sibling lifecycle spec does.
test.use({ locale: 'en-US' });

/** Assert a returned agent doc is the pure-metadata shape: ACTIVE, holding no sandbox.
 *  The state is asserted here as THAT call's own direct evidence and again on the
 *  card, which is where the user sees it; the sandbox_id has no rendering anywhere in
 *  the console, so this is its only oracle. */
function expectActiveNoSandbox(agent: AgentRecord, label: string): void {
  expect(String(agent.state || ''), `${label}: agent must be ACTIVE (never HIBERNATING)`).toEqual('ACTIVE');
  expect(
    String((agent.sandbox_id as string | null | undefined) || '').trim(),
    `${label}: agent must never hold a sandbox_id`,
  ).toEqual('');
}

let agentId = '';
let agentBeforeFault: Record<string, unknown> | null = null;
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  try {
    if (agentBeforeFault && agentId) {
      restoreDoc('agents', { '$.agent_id': agentId }, agentBeforeFault);
    }
  } finally {
    if (agentId) await api.deleteAgent(agentId).catch(() => {});
  }
});

test('agent stays ACTIVE on broken config with no HIBERNATING fallback; wake/hibernate stay no-ops', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  agentId = '';
  agentBeforeFault = null;

  // Source a real model + environment from the seeded default agent. Seeded agents
  // carry model:"" (resolved at run time), but the create validator requires a
  // NON-BLANK model, so pick a concrete id from the environment catalogue (skipping
  // litellm wildcard routing keys), falling back to the documented stack model only
  // if the catalogue is empty. Mirrors the sibling per-session specs.
  const base = await api.defaultAgent();
  const originalEnvironmentName = String(base.environment_name || '').trim();
  expect(originalEnvironmentName, 'seeded default agent must name an environment').not.toEqual('');
  const models = await api.listEnvironmentModels(originalEnvironmentName);
  const model = models.find((m) => m && !m.includes('*')) || 'deepseek-chat';

  // A guaranteed-unresolvable environment reference for fault injection. Public
  // Agent writes correctly refuse it. Mutating the already-valid row models its
  // Environment becoming unavailable after authoring; _require_environment then
  // raises AGENT_ENVIRONMENT_MISSING only if conversation provisioning resolves it.
  // Agent wake / hibernate / get / list must not resolve runtime configuration.
  const brokenEnvironmentName = `__e2e_missing_env_${runId}__`;
  const agentName = `__e2e_broken_config_${runId}`;

  /** environment_name rides the sanitized doc via the index signature (typed unknown). */
  const envRefOf = (agent: AgentRecord): string =>
    String((agent.environment_name as string | null | undefined) || '').trim();

  // The agent's card on the console home, and the state badge inside it.
  const card = page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`);
  const stateBadge = card.locator('[data-slot="badge"]');

  // The record page's own surfaces. The fact rail is the page's only <aside>, and it
  // renders environment_name RAW (`record.environment_name || '—'`), which is what
  // makes a missing environment visible rather than resolved away. The select is the
  // same value as an editable field: ConsoleSelect adds an unknown current value as a
  // real option, so a value no environment matches is offered back instead of being
  // silently dropped to blank.
  const environmentFact = page.locator('aside').filter({ hasText: 'Environment' });
  const environmentSelect = page.getByLabel('Runtime environment');

  /**
   * Re-open the console home and read the card. A fresh load is not a test
   * convenience: AgentHome fetches the agent list once on mount, so re-opening the
   * page IS how a user sees the state after an out-of-band config edit or lifecycle
   * poke. "Active" is the whole user-visible content of "no HIBERNATING fallback" —
   * the badge is what would read Hibernating if a bad config could flip the state.
   */
  const expectPickerOffersActiveAgent = async (label: string) => {
    await page.goto(appPath('/agents'));
    await expect(card, `${label}: the console must still offer the agent`).toBeVisible({
      timeout: 30_000,
    });
    await expect(
      stateBadge,
      `${label}: the card must read Active — a broken config must never surface as Hibernating`,
    ).toHaveText('Active');
  };

  /**
   * Open the operator console's record page for this agent the way an operator
   * reaches it: the agents list → click its unique row. Reaching it also
   * proves the broken-config agent is still LISTED and still editable, which is what
   * "the lifecycle never resolved the config" buys the operator who has to fix it.
   *
   * The row carries role="row" and its click handler navigates to the record; the
   * heading is the agent's display name, so waiting on it asserts both that the
   * navigation happened and that the row behind it was the right agent.
   */
  const openAgentDetail = async (label: string) => {
    await page.goto(appPath('/manage/agents'));
    await expect(page.getByTestId('console-table')).toBeVisible({ timeout: 30_000 });
    // ConsoleSearch is intentionally absent for 12 or fewer rows; this isolated
    // deployment starts below that threshold. The run-scoped name is unique, so
    // locating the row directly proves the same listed-and-editable contract at
    // every list size without depending on an optional convenience control.
    const row = page.getByTestId('console-table').getByRole('row').filter({ hasText: agentName });
    await expect(row, `${label}: the management console must still list the agent`).toBeVisible({
      timeout: 30_000,
    });
    await row.click();
    await expect(
      page.getByRole('heading', { name: agentName }),
      `${label}: the agent's record page must open`,
    ).toBeVisible({ timeout: 15_000 });
  };

  try {
    // ── 1. Create a HEALTHY agent → ACTIVE immediately, no sandbox. ─────────
    const agent = await api.createAgent({
      name: agentName,
      model,
      environment_name: originalEnvironmentName,
    });
    agentId = String(agent.agent_id || '');
    expect(agentId, 'created agent must have an id').not.toEqual('');
    expectActiveNoSandbox(agent, 'on creation (healthy config)');
    // Arrangement verification, not an invariant about a screen: the agent this
    // spec is about to BREAK must genuinely start out resolvable, or the
    // corrupt→repair story below proves nothing. The same field gets its pixel on
    // the record page at steps 2 and 5, where it is the subject.
    expect(
      envRefOf(agent),
      'healthy agent must carry its resolvable environment reference',
    ).toEqual(originalEnvironmentName);

    // Pin the console language before the first navigation so the badge and the
    // management labels read what this spec was written against (see test.use above).
    await page.addInitScript(() => {
      try {
        window.localStorage.setItem('astrabox-lang', 'en');
      } catch {
        /* localStorage unavailable — the en-US navigator locale still applies */
      }
    });
    // A freshly created healthy agent is offered to the user straight away, Active.
    await expectPickerOffersActiveAgent('on creation (healthy config)');

    // ── 2. Make the persisted Environment reference unresolvable. ────────
    // This storage-level fault models an Environment disappearing or being disabled
    // after the Agent was valid,
    // without weakening the public write contract or changing a shared Environment
    // that parallel E2Es may be using.
    const beforeFault = patchDocs(
      'agents',
      { '$.agent_id': agentId },
      { environment_name: brokenEnvironmentName },
    );
    agentBeforeFault = beforeFault[0] ?? null;
    const corrupted = await api.getAgent(agentId);
    // The external fault never trips a lifecycle-state transition: still ACTIVE,
    // and the agent still holds no sandbox (the no-pixel half).
    expectActiveNoSandbox(corrupted, 'after environment becomes unavailable');

    // …and the user's screen agrees: the agent is still offered, still Active. This
    // is the assertion a startup-failure → HIBERNATING fallback would fail.
    await expectPickerOffersActiveAgent('after corrupting config');

    // The corruption is real and OBSERVABLE where an operator would look. Two
    // readings of one field, and they answer different questions: the fact rail is
    // what the deployment REPORTS about the record, and the select is what an
    // operator would CHANGE. (The name is run-scoped and unique, so containment is
    // unambiguous.) A fault injection that missed the stored Agent, or landed on an
    // inert field, cannot produce either.
    await openAgentDetail('after corrupting config');
    await expect(
      environmentFact,
      'the record page must report the agent pointing at the missing environment',
    ).toContainText(brokenEnvironmentName);
    await expect(
      environmentSelect,
      'the edit field must show the agent still pointing at the missing environment',
    ).toHaveValue(brokenEnvironmentName);

    // ── 3. wake on the BROKEN-config agent → no-op: ACTIVE, no sandbox. ─────
    // If wake resolved the Agent configuration it would fail on the missing env;
    // it returns ACTIVE, proving resolution is absent from the Agent lifecycle. The poke
    // stays on the API: no console control wakes an AGENT (only assistants expose
    // Wake/Hibernate), so there is no pixel to click here.
    const afterWake = await api.wakeAgent(agentId);
    expectActiveNoSandbox(afterWake, 'after wake (broken config)');
    await expectPickerOffersActiveAgent('after wake (broken config)');

    // ── 4. hibernate on the BROKEN-config agent → no-op: ACTIVE, no sandbox. ─
    const afterHibernate = await api.hibernateAgent(agentId);
    expectActiveNoSandbox(afterHibernate, 'after hibernate (broken config)');
    await expectPickerOffersActiveAgent('after hibernate (broken config)');

    // ── 5. RESTORE the config THROUGH THE CONSOLE; still ACTIVE, no sandbox. ─
    // The repair is a real operator action, so it goes through the record page's own
    // editor: pick the healthy environment, then save that section (AgentDetailPage
    // .saveSection → PUT /agents/{id} carrying the version CAS fence).
    await openAgentDetail('before repairing the config');
    await expect(
      environmentSelect.locator(`option[value="${originalEnvironmentName}"]`),
      'the console must offer the healthy environment to repair the agent with',
    ).toHaveCount(1);
    await environmentSelect.selectOption({ value: originalEnvironmentName });
    // A section's Save exists only while that section differs from the saved record,
    // and this run has changed exactly one field, so the page carries exactly one.
    // ("Save MCP servers and Skills" on the extensions card is a different name, which the
    // exact match excludes even when it is present.)
    const saveEnvironment = page.getByRole('button', { name: 'Save', exact: true });
    // Enabled, asserted before the click: a section whose card is blocked renders
    // Save disabled, and clicking a disabled button is a no-op that would surface
    // three assertions later as an unexplained timeout on the rail.
    await expect(
      saveEnvironment,
      'the console must offer an enabled Save for the repaired environment',
    ).toBeEnabled({ timeout: 15_000 });
    await saveEnvironment.click();

    // A successful save re-reads the record from the server before repainting, so the
    // rail below is persisted truth rather than the local draft. The missing
    // environment is gone from it, and the healthy one is what the agent reads as.
    // (A failed save leaves the record untouched and the rail still reading the
    // missing environment — which this same assertion catches.)
    await expect(
      environmentFact,
      'the repaired agent must no longer report the missing environment',
    ).not.toContainText(brokenEnvironmentName, { timeout: 30_000 });
    await expect(
      environmentFact,
      'the record page must report the restored environment back',
    ).toContainText(originalEnvironmentName, { timeout: 15_000 });

    // And the picker still simply offers the agent, Active — the config went broken
    // and back with the lifecycle never noticing, which is the whole claim.
    await expectPickerOffersActiveAgent('after restoring config in the console');

    // The no-pixel half, re-read from the API: ACTIVE with no sandbox_id. The console
    // renders a sandbox id nowhere, so this fact has no screen to be read off.
    const reread = await api.getAgent(agentId);
    expectActiveNoSandbox(reread, 'after restore (GET)');
    // BOTH, deliberately. The rail above proves the operator SEES the repair, but it
    // proves it as text in a panel; the original assertion was an equality on the
    // field itself, and the console gives that field no testid to pin it to. So the
    // exact persisted value is re-asserted here on the GET already being made —
    // keeping the page oracle and the precise one, rather than trading one for the
    // other (see rule: do not weaken an assertion to make a conversion easier).
    expect(
      envRefOf(reread),
      'the console repair must have persisted the original environment reference',
    ).toEqual(originalEnvironmentName);
  } finally {
    // Nothing is restored or deleted here. `onPassOnly()` decides in afterEach,
    // when Playwright knows whether this scene is evidence for a failure. A passing
    // run restores the exact pre-fault row before it soft-deletes the Agent.
  }
});
