/**
 * A turn landing on a process with no resident runtime reattaches the existing
 * live sandbox even when the database lease projection has lapsed. Both
 * provider truth and the durable session projection must advance together.
 */
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { lapseSessionSandboxLease, sessionDoc } from '../fixtures/dbOracle';
import { parseTimeoutEnv } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const TURN_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
const MIN_RENEWAL_ADVANCE_MS = 5_000;

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

function expiryMillis(record: Record<string, unknown>, label: string): number {
  const raw = String(record.expires_at || '').trim();
  expect(raw, `${label} must expose expires_at`).not.toEqual('');
  const parsed = Date.parse(raw);
  expect(Number.isFinite(parsed), `${label} expires_at must be an ISO timestamp`).toBe(true);
  return parsed;
}

test('dispatch renews a transport-attached session sandbox lease before sending', async ({
  request,
}) => {
  const api = new AstraApi(request);
  const base = await api.defaultAgent();
  expect(base.environment_name, 'retain the proven shared Environment').toBeTruthy();
  expect(base.model, 'retain the proven model').toBeTruthy();
  // A dedicated Agent prevents sibling sessions from renewing the measured box
  // between the lease reads.
  const agent = await api.createAgent({
    name: `__e2e_transport_lease_${Date.now()}_${test.info().workerIndex}`,
    model: base.model,
    environment_name: base.environment_name,
    prewarm_enabled: false,
  });
  agentId = agent.agent_id;
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  const ready = await api.waitForSessionReady(sessionId);
  const sandboxId = String(ready.sandbox_id || '').trim();
  expect(sandboxId).not.toEqual('');
  const warmup = await api.sendTurn(
    sessionId,
    'E2E lease baseline: do not use tools; reply briefly.',
    TURN_BUDGET_MS,
  );
  expect(warmup.errorText).toBeNull();
  expect(warmup.text.trim(), 'the baseline must produce an ordinary reply').not.toEqual('');
  expect(warmup.text.trim()).not.toMatch(/^API Error:\s*\d+\b/);
  await api.waitForSessionReady(sessionId);

  const baselineProvider = await api.getSandbox(sandboxId);
  const baselineExpiry = expiryMillis(baselineProvider, 'provider baseline');
  expect(baselineExpiry, 'the baseline lease must still be live').toBeGreaterThan(Date.now());

  // The provider renews to `now + fixed TTL`, so a healthy cold attach that
  // follows the baseline read immediately can advance expiry by only a few
  // milliseconds. Establish the observation window required by the strict
  // >5s assertion before causing the attach; polling afterward cannot make an
  // already-completed renewal advance any further.
  await new Promise((resolve) => setTimeout(resolve, MIN_RENEWAL_ADVANCE_MS + 1_000));

  await api.adminEvictRuntime(sessionId);
  const assistantsBefore = await api.assistantCount(sessionId);
  const expiredAt = new Date(Date.now() - 3_600_000).toISOString();
  const lapse = lapseSessionSandboxLease(sessionId, sandboxId, expiredAt);
  expect(lapse, 'the fault must update exactly the original live binding').toEqual([
    { session_id: sessionId, sandbox_id: sandboxId, expires_at: expiredAt },
  ]);
  const liveProvider = await api.getSandbox(sandboxId);
  expect(liveProvider.sandbox_id).toBe(sandboxId);
  expect(String(liveProvider.state).toLowerCase(), 'only the database lease is expired').toBe('running');
  expect(expiryMillis(liveProvider, 'live provider during fault')).toBeGreaterThan(Date.now());
  const stale = sessionDoc(sessionId);
  expect(stale).toMatchObject({ session_id: sessionId, sandbox_id: sandboxId, expires_at: expiredAt });
  expect(expiryMillis(stale!, 'stale database lease')).toBeLessThan(Date.now());
  await test.info().attach('live-box-stale-lease', {
    body: JSON.stringify({
      lapse,
      provider: { sandbox_id: liveProvider.sandbox_id, state: liveProvider.state, expires_at: liveProvider.expires_at },
    }),
    contentType: 'application/json',
  });

  // No Session GET or explicit recovery between the stale read and this send.
  const attachedTurn = await api.sendTurn(
    sessionId,
    'E2E lease after transport attach: do not use tools; reply briefly.',
    TURN_BUDGET_MS,
  );
  expect(attachedTurn.errorText, 'transport attach must still deliver the input').toBeNull();
  expect(attachedTurn.text.trim(), 'the first post-fault send must produce a reply').not.toEqual('');
  expect(attachedTurn.text.trim()).not.toMatch(/^API Error:\s*\d+\b/);

  let renewedProvider: Record<string, unknown> = {};
  await expect.poll(async () => {
    renewedProvider = await api.getSandbox(sandboxId);
    return expiryMillis(renewedProvider, 'renewed provider lease') - baselineExpiry;
  }, {
    timeout: 60_000,
    intervals: [500, 1_000, 2_000],
    message: 'the provider lease must advance after a cold transport attach',
  }).toBeGreaterThan(MIN_RENEWAL_ADVANCE_MS);

  // Read database custody before a Session GET can repair its projection.
  const realigned = sessionDoc(sessionId);
  expect(realigned).toMatchObject({ session_id: sessionId, sandbox_id: sandboxId });
  const rawExpiry = expiryMillis(realigned!, 'realigned database lease');
  expect(rawExpiry, 'the original database lease must return to the future').toBeGreaterThan(Date.now());
  expect(Math.abs(rawExpiry - expiryMillis(renewedProvider, 'provider renewal'))).toBeLessThan(1_000);
  await test.info().attach('live-box-lease-realignment', {
    body: JSON.stringify({
      session_id: sessionId, sandbox_id: realigned!.sandbox_id,
      database_expires_at: realigned!.expires_at,
      provider_expires_at: renewedProvider.expires_at,
      baseline_expires_at: baselineProvider.expires_at,
    }),
    contentType: 'application/json',
  });

  await api.waitForAssistantMessageCount(sessionId, assistantsBefore, TURN_BUDGET_MS);
  const settled = await api.waitForSessionReady(sessionId);
  expect(String(settled.sandbox_id || '').trim(), 'renewal must reattach the same box').toBe(sandboxId);
  const providerExpiry = expiryMillis(renewedProvider, 'renewed provider lease');
  const durableExpiry = expiryMillis(settled, 'durable session lease');
  expect(
    Math.abs(durableExpiry - providerExpiry),
    'the durable expiry must be the provider renewal result, not a local estimate',
  ).toBeLessThan(1_000);
});
