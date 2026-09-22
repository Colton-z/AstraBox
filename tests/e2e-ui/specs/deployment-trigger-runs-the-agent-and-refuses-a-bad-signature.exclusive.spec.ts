/**
 * E2E: a signed deployment webhook starts its bound Agent, while invalid requests
 * and inactive deployments are refused.
 *
 * A valid signature over the exact timestamp and request bytes must create a real
 * session and persist the triggered turn. Reusing that signature with another
 * body, omitting headers, or sending a stale timestamp must return 401. Disabling
 * or deleting the deployment must close the trigger. The management page also
 * confirms that the created binding is visible to an operator.
 *
 * The spec is exclusive because the accepted trigger provisions a sandbox.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { PlatformApi } from '../fixtures/platformApi';
import { appPath } from '../fixtures/env';

const RUN_ID = new Date().toISOString().replace(/[:.]/g, '-');
// The triggered conversation provisions its own sandbox and then runs a turn,
// all after the HTTP call has already returned. This is one shared deadline:
// two independent waits cannot both fit under the spec's 180-second watchdog.
const TRIGGERED_TURN_BUDGET_MS = 120_000;

// Well outside ASTRABOX_WEBHOOK_HMAC_WINDOW_SECONDS (300s default), so the
// freshness check fires regardless of clock skew between runner and server.
const STALE_SECONDS = 3_600;

// Teardown that changes state runs on the passing path only, and hook
// registration order is the old `finally` order: afterEach hooks run in the
// order they are registered.
//
// `triggeredSessions` IS the tracker's array. `deploymentId` is cleared by the
// test when it deletes the binding itself, so the guard below skips it then.
const triggeredSessions = trackSessions();
let agentId = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (deploymentId && agentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('a signed deployment trigger runs the agent, and a bad signature is refused', async ({ page, request }) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);

  try {
    // ── Arrange: an isolated agent to bind the trigger to ───────────────────
    const base = await api.defaultAgent();
    const environmentName = String(base.environment_name || '').trim();
    expect(environmentName, 'seeded default agent must name an environment').not.toEqual('');
    let model = String(base.model || '').trim();
    if (!model || model.includes('*')) {
      const models = await api.listEnvironmentModels(environmentName);
      model = models.find((m) => m && !m.includes('*')) || 'deepseek-chat';
    }
    const agentName = `__e2e_deployment_${RUN_ID}`;
    const agent = await api.createAgent({
      name: agentName,
      model,
      environment_name: environmentName,
      engine_options: { sdk_options: { max_turns: 6 } },
    });
    agentId = String(agent.agent_id || '');
    expect(agentId, 'created agent must have an id').not.toEqual('');

    // ── Bind an hmac trigger ────────────────────────────────────────────────
    const promptPrefix = 'Reply once, in one short sentence, with the webhook marker. Do not use tools. Webhook says:';
    const deployment = await platform.createDeployment(agentId, {
      scene: 'hmac',
      prompt_prefix: promptPrefix,
    });
    deploymentId = String(deployment.deployment_id || '');
    expect(deploymentId, 'creating a deployment must return its id').not.toEqual('');
    expect(deployment.scene, 'the binding should record the scene it was created with').toBe('hmac');
    expect(deployment.enabled, 'a fresh binding should be enabled').toBe(true);
    expect(
      String(deployment.secret || '').trim(),
      'the owner must receive the signing secret — a trigger that cannot be signed is unreachable',
    ).not.toEqual('');
    const secret = String(deployment.secret);

    const listed = await platform.listDeployments(agentId);
    expect(
      listed.map((item) => item.deployment_id),
      'the binding should appear in the agent listing',
    ).toContain(deploymentId);

    // The operator listing must expose this run's binding. Waiting on the table
    // alone could pass while it contains only an unrelated, pre-existing row.
    await page.goto(appPath('/manage/deployments'));
    const deploymentTable = page.getByTestId('console-table');
    const deploymentRow = deploymentTable.getByRole('row').filter({ hasText: deploymentId });
    const deploymentIdCell = deploymentRow.getByText(deploymentId, { exact: true });
    await expect(
      deploymentRow,
      'the operator listing should contain exactly the deployment created by this run',
    ).toHaveCount(1, { timeout: 30_000 });
    await expect(
      deploymentIdCell,
      'the created deployment id should be visible in its operator row',
    ).toBeVisible();

    // ── Refusals first: a request the platform must not run ─────────────────
    const payload = { source: 'e2e', marker: RUN_ID };

    const unsigned = await platform.refusal('POST', `/deployments/${deploymentId}/trigger`, payload);
    expect(unsigned.status, 'a trigger with no signature headers must be refused').toBe(401);
    expect(unsigned.code, 'an unsigned trigger should be refused as unauthorized').toBe('DEPLOYMENT_UNAUTHORIZED');

    const misSigned = await platform.triggerHmac(deploymentId, secret, payload, {
      signature: 'not-the-signature',
    });
    expect(misSigned.status, 'a wrong signature must be refused').toBe(401);
    expect(misSigned.body, 'a wrong signature should say so').toContain('invalid webhook signature');

    const stale = await platform.triggerHmac(deploymentId, secret, payload, {
      timestamp: String(Math.floor(Date.now() / 1000) - STALE_SECONDS),
    });
    expect(stale.status, 'a stale timestamp must be refused even when correctly signed').toBe(401);
    expect(
      stale.body,
      'a stale trigger should be refused on freshness, naming the window',
    ).toContain('freshness window');

    // The signature covers the body, so a captured pair cannot carry a NEW
    // prompt: a signature valid for `payload`, posted with different bytes, is
    // the exact shape of a webhook capture turned into prompt injection.
    // Deliberately costs no sandbox — the platform must refuse before it starts
    // anything.
    const swappedBody = await platform.triggerHmac(
      deploymentId,
      secret,
      { source: 'e2e', marker: 'SWAPPED-BY-A-CAPTOR' },
      { signOverPayload: payload },
    );
    expect(
      swappedBody.status,
      'a signature captured for one body must not authorize a different body',
    ).toBe(401);
    expect(swappedBody.body, 'the swapped body should be refused on the signature').toContain(
      'invalid webhook signature',
    );

    // ── Accept: a properly signed trigger runs the agent ────────────────────
    const receipt = await platform.triggerHmacAccepted(deploymentId, secret, payload);
    expect(receipt.status, 'a signed trigger should be accepted').toBe('accepted');
    const sessionId = String(receipt.session_id || '').trim();
    expect(sessionId, 'an accepted trigger must name the conversation it started').not.toEqual('');
    triggeredSessions.push(sessionId);

    // The conversation belongs to the bound agent — not to whoever called.
    const started = await api.getSession(sessionId);
    expect(String(started.agent_id || ''), 'the triggered conversation should belong to the bound agent').toBe(agentId);

    // And the trigger really RAN. The platform fires the turn fire-and-forget
    // after answering, so "accepted" is only a promise until a transcript
    // carries the webhook's own payload back.
    const turnDeadline = Date.now() + TRIGGERED_TURN_BUDGET_MS;
    const remainingTurnBudget = () => Math.max(1, turnDeadline - Date.now());
    await api.waitForSessionReady(sessionId, remainingTurnBudget());
    const answered = await api.waitForAssistantMessageMatching(
      sessionId,
      0,
      (message) => messageText(message).includes(RUN_ID),
      remainingTurnBudget(),
    );
    expect(answered, 'the triggered conversation should hold an assistant reply').toBeTruthy();
    expect(
      messageText(answered),
      'the assistant reply should prove that it received the webhook marker',
    ).toContain(RUN_ID);
    const page1 = await api.getMessages(sessionId, 50);
    const userTexts = (page1.messages || [])
      .filter((message) => message.role === 'user')
      .map(messageText);
    expect(
      userTexts,
      'only the configured prefix may precede the exact sender-authored JSON; no platform prose or reformatting',
    ).toEqual([`${promptPrefix}\n\n${JSON.stringify(payload)}`]);

    // ── Disabling closes the endpoint ───────────────────────────────────────
    const disabled = await platform.updateDeployment(agentId, deploymentId, { enabled: false });
    expect(disabled.enabled, 'the patch should disable the binding').toBe(false);
    const whileDisabled = await platform.triggerHmac(deploymentId, secret, payload);
    expect(
      whileDisabled.status,
      'a disabled binding must not accept a trigger, however well signed',
    ).toBeGreaterThanOrEqual(400);

    // ── …and deleting it removes it ─────────────────────────────────────────
    const deadDeploymentId = deploymentId;
    const deleted = await platform.deleteDeployment(agentId, deploymentId);
    expect(deleted.deleted, 'deleting the binding should report it deleted').toBe(true);
    deploymentId = '';
    const afterDelete = await platform.triggerHmac(deadDeploymentId, secret, payload);
    expect(afterDelete.status, 'a deleted binding must not answer').toBeGreaterThanOrEqual(400);
    expect(
      (await platform.listDeployments(agentId)).length,
      'the agent should hold no bindings after the delete',
    ).toBe(0);
  } finally {
    // Nothing here is released on a failing run. `trackSessions()` and
    // `onPassOnly()` decide in afterEach hooks, where the test's real status is
    // known — see that fixture on why the unit is the whole block.
  }
});
