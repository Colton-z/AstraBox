/**
 * E2E for the one question the product can answer about prewarm health.
 *
 * An Agent is answering slowly and the operator asks whether it is warm.
 * `GET /agents/{id}/prepared-runtime` says yes — ready, one prepared unit, a
 * sandbox id — while every conversation that starts on that Agent refuses that
 * exact slot and pays a cold start instead. Both are reading the same
 * `_prepared_slot` document. They disagree about what it is worth, and only one
 * of them is the one a user meets.
 *
 * The readout's rule is `ready = enabled and state == "prepared"`
 * (agent_service.py:254), with `prepared_count = 1 if ready else 0` (:258). It
 * reads neither the manifest's `runtime_generation` nor its `prepared_at`, while
 * printing the ROW's current generation beside it (:261) — the field that
 * decides whether the slot is claimable is the field it does not compare. The
 * claim's rule is the opposite: `claim_prepared_slot` refuses a manifest whose
 * generation is not the Agent's current one (prepared_slots.py:987-993), logs
 * "the prepared unit is a stale generation", and the Session cold-starts.
 *
 * So this spec makes both readers answer about the same document in one run: the
 * operator's GET, and a conversation a user really starts by clicking the
 * Agent's card. The invariant is that they agree — warm capacity reported is
 * capacity the next conversation can take — and the refusal is observed on that
 * live conversation rather than assumed, so the readout's expected answer is
 * derived from what the product did and not from a rule invented here.
 *
 * The readout is therefore READ before the conversation — starting one is itself
 * a refill trigger, and it would move the document out from under the read — and
 * JUDGED after it, against what the conversation did. One run carries both
 * answers, including a failing one.
 *
 * WHERE THIS SPEC EXPIRES. It builds the unclaimable state through the one arm
 * no background sweep repairs. Give the renewal sweep authority over a
 * generation mismatch as well and the scene becomes unreachable: the manifest
 * may be rebuilt between the restamp and the readout. That run fails at the
 * precondition, which says a refill replaced the manifest and this run proves
 * nothing — the signal to re-derive the spec, never a silent pass.
 *
 * EXPECTED RED AGAINST THE CURRENT PRODUCT, at the two operator-seat assertions
 * only. The user-seat half — the claim refusing this slot — already passes,
 * which is what makes the failure a disagreement between two readers of one
 * document rather than an expectation invented here.
 *
 * WHY THE GENERATION ARM AND NOT THE TTL. Backdating `prepared_at` past the
 * 30-minute TTL is racy now that `renew_expiring_prepared_slots` exists: the
 * watcher legitimately rebuilds any slot inside the renewal lead, so the
 * manifest could be replaced between the write and the readout and the spec
 * would go red for a reason nobody wrote it for. Nothing stands over the
 * generation rule on a timer: `prepared_slot_is_due_for_renewal` substitutes the
 * manifest's own generation (prepared_slots.py:263-278), so the sweep cannot see
 * a mismatch, and `reap_slot_manifest_if_stale` — which does compare against the
 * Agent's — runs only at a refill, which follows Session activity or an Agent
 * write. Between the restamp and the readout this spec does neither, so the
 * doctored manifest is exactly as stable as the product leaves its own between a
 * generation bump and the next refill.
 *
 * TENANCY IS THE REQUIREMENT, NOT AN ENGINE. No engine vocabulary is asserted
 * and no turn is taken, so this runs under whichever profile the matrix
 * selected. It does need the Agent-tenancy prewarm Environment, because the
 * defect is in the manifest branch of `_prepared_runtime_status`; a
 * conversation-tenancy Agent takes the pool branch above it, which computes
 * `ready` from the live client pool and is a different, non-defective reader.
 * The baseline's `placement === 'shared_slot'` is where a wrong fixture says so,
 * before anything is doctored.
 *
 * THE CONSOLE HALF OF THIS JOURNEY DOES NOT EXIST YET. Nothing under
 * frontend/src reads prepared-runtime — the only prewarm string in the console
 * is the Agent form's own "Keep a sandbox ready" toggle, which is configuration,
 * not liveness. So the operator's seat is the API read, and the browser is spent
 * on the half that does exist: the click that makes the platform decide whether
 * to claim. If prewarm health is later surfaced on the Agent record, that page
 * is the second reader to add here.
 */
import { randomUUID } from 'node:crypto';

import { expect, test, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField, restampPreparedSlotGeneration } from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled } from '../fixtures/sessionPage';

// Deployment fixtures. Read and asserted present, never created or rewritten:
// the lane resolves the prewarm Environment per engine profile and validates it
// against the running agent image, and the research Agent is where a model route
// this deployment has actually proven comes from.
const SHARED_ENVIRONMENT = String(
  process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT || '',
).trim();
const RESEARCH_AGENT = String(
  process.env.ASTRABOX_E2E_RESEARCH_AGENT || '',
).trim();

// Every per-step wait is tunable so the lane can trade budget against a loaded
// node. The test budget itself is never stated here — the runner owns it.
const POOL_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_AGENT_POOL_TIMEOUT_MS', 60_000);
const CONVERSATION_START_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_AGENT_START_TIMEOUT_MS', 120_000);
const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 60_000);

/** The manifest's PUBLIC coordinates, beside the generation the claim compares. */
interface SlotFacts {
  slotId: string;
  state: string;
  placement: string;
  sandboxId: string;
  isolatedSessionId: string;
  /** The manifest's own generation — what `claim_prepared_slot` checks. */
  slotGeneration: string;
  /** The Agent row's current generation — what the claim checks it against. */
  rowGeneration: string;
}

/**
 * Read one Agent's preparation without letting its secrets into an assertion.
 *
 * `_prepared_slot` also carries the activation token and the model credential,
 * and a failed `expect()` retains and prints both sides of its comparison. Only
 * the named coordinates are lifted out of it.
 */
function preparedSlotFacts(agentId: string): SlotFacts {
  const rows = documentsByField('agents', '$.agent_id', agentId);
  expect(rows, 'the preparation under test must belong to exactly one Agent row').toHaveLength(1);
  const row = rows[0];
  const manifest = (row._prepared_slot || {}) as Record<string, unknown>;
  return {
    slotId: String(manifest.slot_id || '').trim(),
    state: String(manifest.state || '').trim(),
    placement: String(manifest.placement || '').trim(),
    sandboxId: String(manifest.sandbox_id || '').trim(),
    isolatedSessionId: String(manifest.isolated_session_id || '').trim(),
    slotGeneration: String(manifest.runtime_generation || '').trim(),
    rowGeneration: String(row._runtime_generation || '').trim(),
  };
}

/**
 * Start a conversation the way a person does: from the Agent's card.
 *
 * `api.startConversation` reaches the same server path, but the click is what
 * makes this the user's seat — and the picker card carries exactly one button
 * (AgentHome.tsx:182), so the action is addressable without pinning a locale for
 * its label.
 */
async function startConversationFromCard(page: Page, agentName: string): Promise<string> {
  await page.goto(appPath('/agents'));
  const card = page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`);
  await expect(card, `${agentName} must appear in the Agent picker`).toBeVisible({
    timeout: READY_TIMEOUT_MS,
  });
  await card.getByRole('button').click();
  await page.waitForURL((url) => /\/sessions\/[^/]+$/.test(url.pathname), {
    timeout: CONVERSATION_START_TIMEOUT_MS,
  });
  const sessionId = new URL(page.url()).pathname.split('/').filter(Boolean).pop() || '';
  expect(sessionId, 'the Agent card must open a concrete conversation').not.toEqual('');
  return sessionId;
}

const sessions = trackSessions();
let agentId = '';
// Registered after the tracker, because afterEach hooks run in registration
// order: the session goes before the Agent it ran on. A failure keeps both —
// including the Agent's box and its doctored manifest, which IS the scene.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('a prepared slot the next conversation refuses is not reported as ready capacity', async ({
  page,
  request,
}) => {
  expect(
    SHARED_ENVIRONMENT,
    'ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT must name the deployed Agent-tenancy prewarm Environment',
  ).not.toEqual('');
  expect(
    RESEARCH_AGENT,
    'ASTRABOX_E2E_RESEARCH_AGENT must name the deployed Agent whose model route was proven',
  ).not.toEqual('');

  const api = new AstraApi(request);
  const platform = new PlatformApi(request);

  // ── An Agent with real warm capacity ─────────────────────────────────────
  // Authored over the API: a user picks an Agent that already exists rather than
  // creating one in order to have a conversation, so authoring is setup here and
  // not part of the journey.
  const agentName = `__e2e_prewarm_readout_${Date.now()}_${test.info().workerIndex}`;
  const created = await api.createAgent({
    name: agentName,
    model: await api.configuredAgentModel(RESEARCH_AGENT, SHARED_ENVIRONMENT),
    environment_name: SHARED_ENVIRONMENT,
    prewarm_enabled: true,
  });
  agentId = String(created.agent_id || '').trim();
  expect(agentId, 'the probe Agent must have an id').not.toEqual('');

  // The healthy readout an operator would see — and the premise of everything
  // below. An engine whose adapter has no input-free prepare seam publishes no
  // manifest at all; that is a deployment fact, and it belongs here rather than
  // read back later as a defect.
  let status: Record<string, unknown> = {};
  await expect.poll(async () => {
    status = await platform.preparedRuntime(agentId);
    return Boolean(
      status.ready === true
      && Number(status.prepared_count || 0) > 0
      && String(status.sandbox_id || '').trim()
      && String(status.runtime_generation || '').trim(),
    );
  }, {
    timeout: POOL_TIMEOUT_MS,
    intervals: [2_000],
    message:
      `Agent ${agentId} must prepare claimable capacity on ${SHARED_ENVIRONMENT} before the two `
      + 'readers can be asked to disagree about it',
  }).toBe(true);

  // ── The two readers start in agreement ───────────────────────────────────
  // Whatever they say later is therefore something this spec did, not something
  // the fixture arrived with.
  const before = preparedSlotFacts(agentId);
  expect(before.state, 'the manifest must be claimable before anything is doctored').toBe('prepared');
  expect(
    before.placement,
    'this journey needs Agent tenancy: a conversation-tenancy Agent is read by the pool branch '
    + 'of _prepared_runtime_status, which computes ready from the live pool and is a different reader',
  ).toBe('shared_slot');
  expect(before.slotId, 'the manifest must name its slot').not.toEqual('');
  expect(before.isolatedSessionId, 'the manifest must name its prepared isolation session').not.toEqual('');
  expect(before.sandboxId, 'the manifest must name the box prepared-runtime reports').toBe(
    String(status.sandbox_id || '').trim(),
  );
  expect(before.rowGeneration, 'the Agent must carry a runtime generation').not.toEqual('');
  expect(
    before.slotGeneration,
    'a freshly prepared slot is built under the Agent\'s current generation — this is the agreement '
    + 'the restamp below breaks',
  ).toBe(before.rowGeneration);
  expect(
    String(status.runtime_generation || '').trim(),
    'the readout prints the ROW\'s generation, which is the one the claim compares against',
  ).toBe(before.rowGeneration);

  // ── Make the slot exactly what a claim refuses ───────────────────────────
  // Using the product's own rule, not a fault of this spec's invention: a
  // manifest under a generation the Agent has moved past is what the row really
  // holds between a configuration change and the refill that rebuilds it, and
  // `claim_prepared_slot` skips it by name ("the prepared unit is a stale
  // generation"). One statement guarded down to the slot id, so a refill landing
  // underneath cannot be silently overwritten by a read-modify-write.
  const foreignGeneration = `stale-${randomUUID()}`;
  expect(
    restampPreparedSlotGeneration(agentId, before.slotId, foreignGeneration),
    'the restamp must land on exactly the manifest this spec measured, and on nothing else',
  ).toEqual([{
    agent_id: agentId,
    slot_id: before.slotId,
    state: 'prepared',
    runtime_generation: foreignGeneration,
    agent_runtime_generation: before.rowGeneration,
  }]);

  // Still the document on the record, immediately before the readout is taken.
  // A refill between the two would leave this run proving nothing, and it must
  // say so rather than pass quietly.
  const doctored = preparedSlotFacts(agentId);
  expect(
    doctored.slotId,
    'a refill replaced the manifest before the readout — this run proves nothing about either reader',
  ).toBe(before.slotId);
  expect(doctored.state, 'the restamp must leave the manifest claiming to be prepared').toBe('prepared');
  expect(doctored.slotGeneration, 'the manifest must carry the foreign generation').toBe(foreignGeneration);
  expect(
    doctored.rowGeneration,
    'the restamp must not touch the Agent\'s own generation — that is the value the claim compares against',
  ).toBe(before.rowGeneration);
  expect(
    doctored.isolatedSessionId,
    'the restamp must not have moved the placement this slot holds',
  ).toBe(before.isolatedSessionId);

  // ── The operator's seat ──────────────────────────────────────────────────
  // No page has been opened yet, on purpose: a conversation and an Agent write
  // are each a refill trigger, and either would rebuild the slot before the
  // question could be asked.
  const after = await platform.preparedRuntime(agentId);
  await test.info().attach('prepared-runtime-over-an-unclaimable-slot', {
    body: JSON.stringify({
      agentId,
      slotId: before.slotId,
      slotGeneration: foreignGeneration,
      agentGeneration: before.rowGeneration,
      readout: {
        ready: after.ready,
        prepared_count: after.prepared_count,
        state: after.state,
        sandbox_id: after.sandbox_id,
        runtime_generation: after.runtime_generation,
        last_error: after.last_error,
      },
    }),
    contentType: 'application/json',
  });
  // Read now, judged at the end. The comparison needs the other reader's answer,
  // and asking the other reader means starting a conversation — which is itself
  // a refill trigger and would move the document out from under this read.

  // ── The user's seat, and the action that makes the platform decide ───────
  const startedAt = Date.now();
  const sessionId = await startConversationFromCard(page, agentName);
  sessions.push(sessionId);
  // Routed is not the same as usable. The cold path works — it is only slow,
  // which is precisely why the readout is the operator's whole diagnosis.
  await expectComposerEnabled(page);
  test.info().annotations.push({
    type: 'picker_to_composer_ms',
    // Diagnostic, never an assertion: it spans the picker navigation, and a
    // budget asserted on it would fail on a loaded node rather than on the
    // disagreement this spec is about.
    description: JSON.stringify({ ms: Date.now() - startedAt, sessionId }),
  });

  // ── What the other reader did with the same document ─────────────────────
  await api.waitForSessionReady(sessionId, READY_TIMEOUT_MS);
  const detail = await api.adminSessionDetail(sessionId);
  const claimedIsolation = String(detail.runtime_identity?.isolated_session_id || '').trim();
  expect(
    claimedIsolation,
    'Agent tenancy places every conversation in its own isolation session inside the shared box',
  ).not.toEqual('');
  // The product's own rule, stated where the scene depends on it: a unit under a
  // generation the Agent has moved past is skipped by claim_prepared_slot, so
  // this conversation must have built its own child rather than adopting the
  // one on the row. Should that rule ever change, this is where a reader learns
  // that the comparison below rests on a premise the product has dropped.
  expect(
    claimedIsolation,
    'the conversation must refuse a prepared unit under a generation the Agent has moved past '
    + '(prepared_slots.py:987-993), and build its own isolation session instead',
  ).not.toBe(before.isolatedSessionId);

  // ── The two readers, compared ────────────────────────────────────────────
  // The values below are not a rule this spec invented. The refusal was just
  // observed on a live conversation, and these say the readout had to describe
  // the same document the same way. Which answer a correction gives is still
  // open: `ready` may go false over an unclaimable unit, or the unit may be
  // rebuilt so that a conversation can take it and `ready` stays true — that
  // rebuild simply has to happen before the readout, not after the next user.
  const disagreement =
    `slot ${before.slotId} carries generation ${foreignGeneration} while Agent ${agentId} is on `
    + `${before.rowGeneration}; the conversation refused it and ran on isolation session `
    + `${claimedIsolation}, while prepared-runtime answered ready=${String(after.ready)} `
    + `prepared_count=${String(after.prepared_count)} over that same manifest`;
  expect(
    after.ready,
    `PREWARM READOUT: warm capacity reported must be capacity a conversation can take — ${disagreement}`,
  ).toBe(false);
  expect(
    Number(after.prepared_count || 0),
    `PREWARM READOUT: a unit the next conversation refuses must not be counted as prepared — ${disagreement}`,
  ).toBe(0);
  // Deliberately not asserted: `sandbox_id` and `state`. What a correct readout
  // should say in those two fields is unspecified, and pinning them would freeze
  // one implementation of a correction rather than the agreement being tested.
});
