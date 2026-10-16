/**
 * A conversation whose host connection to its runtime dropped is reattached to
 * its live box even when the database's record of that box's lease has lapsed,
 * and the lease the provider renews is the one the database ends up holding.
 *
 * The session's reader holds the runtime's only connection and reconnects as
 * soon as that connection ends. So the fault lapses the database lease FIRST
 * and evicts the host runtime SECOND: the reconnect then attaches through a
 * lapsed lease. The same box must answer the next message, the provider's
 * lease must advance, and the database lease must return to the future at the
 * provider's renewed expiry — written by the attach or by the expiration
 * watcher's realignment of a lapsed lease over a live box, whichever lands
 * first.
 */
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, postgresTextRows, sqlLiteral } from '../fixtures/dbOracle';
import { parseTimeoutEnv } from '../fixtures/env';
import { POSTGRES_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const TURN_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
const MIN_RENEWAL_ADVANCE_MS = 5_000;
// How long the database lease may stay behind the provider's renewal. Covers
// the expiration watcher's realignment tick, which runs on a short interval;
// an attach that writes the renewal itself lands far sooner.
const DATABASE_FOLLOWS_MS = 60_000;

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

function expiryMillis(record: { expires_at?: unknown }, label: string): number {
  const raw = String(record.expires_at || '').trim();
  expect(raw, `${label} must expose expires_at`).not.toEqual('');
  const parsed = Date.parse(raw);
  expect(Number.isFinite(parsed), `${label} expires_at must be an ISO timestamp`).toBe(true);
  return parsed;
}

/** The session's binding and lease as the `sessions` row holds them. */
interface LeaseRow {
  session_id: string;
  sandbox_id: string | null;
  /** `expires_at` in epoch milliseconds, or null when the row holds no lease. */
  expires_at_ms: number | null;
}

const LEASE_COLUMNS = `session_id, sandbox_id, (extract(epoch FROM expires_at) * 1000)::bigint AS expires_at_ms`;

function leaseRow(sessionId: string): LeaseRow | null {
  return postgresRows<LeaseRow>(
    `SELECT ${LEASE_COLUMNS} FROM sessions WHERE session_id = ${sqlLiteral(sessionId)}`,
  )[0] ?? null;
}

/**
 * Lapse the database lease of exactly this binding, and read the fault back
 * from the statement that wrote it. The watcher realigns a lapsed lease over a
 * live box on its next tick, so a second read could observe that repair
 * instead of the fault.
 */
function lapseLease(sessionId: string, sandboxId: string, expiredAt: string): LeaseRow[] {
  return postgresTextRows(
    requireServiceContainer(POSTGRES_CONTAINER_HANDLE),
    process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox',
    `UPDATE sessions SET expires_at = TIMESTAMPTZ ${sqlLiteral(expiredAt)} `
      + `WHERE session_id = ${sqlLiteral(sessionId)} AND sandbox_id = ${sqlLiteral(sandboxId)} `
      + `RETURNING json_build_object('session_id', session_id, 'sandbox_id', sandbox_id, `
      + `'expires_at_ms', (extract(epoch FROM expires_at) * 1000)::bigint)::text`,
  ).map((line) => JSON.parse(line) as LeaseRow);
}

test('a reattached runtime renews its live box lease and the lapsed database lease follows it', async ({
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
    prepared_capacity: 0,
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

  // ── The fault: lapsed database lease, then a dropped host connection ─────
  const expiredAt = new Date(Date.now() - 3_600_000).toISOString();
  const lapse = lapseLease(sessionId, sandboxId, expiredAt);
  expect(lapse, 'the fault must update exactly the original live binding').toEqual([
    { session_id: sessionId, sandbox_id: sandboxId, expires_at_ms: Date.parse(expiredAt) },
  ]);
  const evicted = await api.adminEvictRuntime(sessionId);
  expect(evicted.evicted, 'evict-runtime should report the evicted session').toBe(sessionId);
  // Recorded, not asserted: whether the watcher had already realigned the lease
  // when the reader started reconnecting is a race this spec does not judge.
  const leaseAfterEvict = leaseRow(sessionId);
  const liveProvider = await api.getSandbox(sandboxId);
  expect(liveProvider.sandbox_id).toBe(sandboxId);
  expect(String(liveProvider.state).toLowerCase(), 'only the database lease is expired').toBe('running');
  expect(expiryMillis(liveProvider, 'live provider during fault')).toBeGreaterThan(Date.now());
  await test.info().attach('live-box-stale-lease', {
    body: JSON.stringify({
      lapse,
      lease_after_evict: leaseAfterEvict,
      provider: { sandbox_id: liveProvider.sandbox_id, state: liveProvider.state, expires_at: liveProvider.expires_at },
    }),
    contentType: 'application/json',
  });

  // No explicit recovery between the fault and this send: the reader that
  // reconnects on its own is the only thing that may bring the runtime back.
  const attachedTurn = await api.sendTurn(
    sessionId,
    'E2E lease after transport attach: do not use tools; reply briefly.',
    TURN_BUDGET_MS,
  );
  expect(attachedTurn.errorText, 'the reattached runtime must still answer the input').toBeNull();
  expect(attachedTurn.text.trim(), 'the first post-fault send must produce a reply').not.toEqual('');
  expect(attachedTurn.text.trim()).not.toMatch(/^API Error:\s*\d+\b/);

  let renewedProvider: Record<string, unknown> = {};
  await expect.poll(async () => {
    renewedProvider = await api.getSandbox(sandboxId);
    return expiryMillis(renewedProvider, 'renewed provider lease') - baselineExpiry;
  }, {
    timeout: 60_000,
    intervals: [500, 1_000, 2_000],
    message: 'the provider lease must advance after the runtime is reattached',
  }).toBeGreaterThan(MIN_RENEWAL_ADVANCE_MS);

  // Database custody, read from the row itself: the lease returns to the
  // future at the provider's renewed expiry.
  let realigned: LeaseRow | null = null;
  let providerAtRealign = 0;
  await expect.poll(async () => {
    realigned = leaseRow(sessionId);
    providerAtRealign = expiryMillis(await api.getSandbox(sandboxId), 'provider lease');
    const databaseMs = realigned?.expires_at_ms ?? null;
    return databaseMs !== null
      && Number(databaseMs) > Date.now()
      && Math.abs(Number(databaseMs) - providerAtRealign) < 1_000;
  }, {
    timeout: DATABASE_FOLLOWS_MS,
    intervals: [500, 1_000, 2_000],
    message: 'the lapsed database lease must return to the future at the provider renewal',
  }).toBe(true);
  expect(realigned, 'the session row must still name the original box').toMatchObject({
    session_id: sessionId,
    sandbox_id: sandboxId,
  });
  await test.info().attach('live-box-lease-realignment', {
    body: JSON.stringify({
      session_id: sessionId,
      database_lease: realigned,
      provider_expires_at_ms: providerAtRealign,
      renewed_provider_expires_at: renewedProvider.expires_at,
      baseline_expires_at: baselineProvider.expires_at,
    }),
    contentType: 'application/json',
  });

  const settled = await api.waitForSessionReady(sessionId);
  expect(String(settled.sandbox_id || '').trim(), 'the reattached runtime must be on the same box').toBe(sandboxId);
  expect(settled.last_response?.outcome, 'the post-fault response must complete').toBe('completed');
  const providerExpiry = expiryMillis(await api.getSandbox(sandboxId), 'provider lease');
  const durableExpiry = expiryMillis(settled, 'durable session lease');
  expect(
    Math.abs(durableExpiry - providerExpiry),
    'the durable expiry must be the provider renewal result, not a local estimate',
  ).toBeLessThan(1_000);
});
