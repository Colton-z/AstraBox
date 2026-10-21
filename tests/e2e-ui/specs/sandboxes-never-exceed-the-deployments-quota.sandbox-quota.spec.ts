/**
 * E2E: a backend never runs more sandboxes than the deployment's quota allows.
 *
 * The server runs with ASTRABOX_SANDBOX_QUOTA_BOXES set a few boxes above what
 * it already runs, and the run says so with ASTRABOX_E2E_SANDBOX_QUOTA=1;
 * without it Playwright does not collect this file (playwright.config.ts).
 *
 * Every count is checked against the cluster, not only against what the
 * platform reports: the Pods carrying this installation's label, read with
 * kubectl, are the boxes the backend actually runs.
 *
 * - the quota's ledger knows every Pod the backend runs, after the server
 *   restarted into this configuration;
 * - conversations that each need a box start until the quota is full; the next
 *   one ends at once with an error naming the box ceiling, its use and its
 *   limit, and the cluster never runs more boxes than the quota; a conversation
 *   already running keeps working; when a conversation's box goes, the next
 *   conversation starts; and with one box of room left, two conversations
 *   started together get one box between them.
 *
 * The second case is one journey: each case's conversations are removed when it
 * passes, so no case leaves the quota full for another.
 *
 * Deployment fixtures this reads and never creates: the deployment-proven
 * Agent (`ASTRABOX_E2E_AGENT_NAME`) for its model, an enabled RuntimeProfile of
 * conversation tenancy for its engine, and kubectl access to the cluster
 * (`KUBECONFIG`, `ASTRABOX_E2E_KUBE_NAMESPACE`).
 */

import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly } from '../fixtures/sessionCleanup';

const NAMESPACE = String(process.env.ASTRABOX_E2E_KUBE_NAMESPACE || 'opensandbox');

interface QuotaBackend {
  backend: string;
  quota: { boxes: number | null };
  usage: { boxes: number };
  boxes: Array<{ sandbox_id: string }>;
  reservations: Array<{ sandbox_id: string | null }>;
}

const agentIds: string[] = [];
const sessionIds: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const sessionId of sessionIds.splice(0)) await api.deleteSession(sessionId).catch(() => {});
  for (const agentId of agentIds.splice(0)) await api.deleteAgent(agentId);
});

async function quota(api: AstraApi): Promise<QuotaBackend> {
  const report = await api.data<{ backends: QuotaBackend[] }>('GET', '/admin/sandbox-quota');
  const limited = report.backends.filter((entry) => entry.quota.boxes !== null);
  expect(limited.length, 'this spec needs a deployment that sets ASTRABOX_SANDBOX_QUOTA_BOXES').toBe(1);
  return limited[0];
}

function kubectl(args: string[]): string {
  const result = spawnSync('kubectl', ['-n', NAMESPACE, ...args], { encoding: 'utf8', timeout: 30_000 });
  expect(result.status, `kubectl ${args.join(' ')}: ${result.stderr}`).toBe(0);
  return result.stdout;
}

/** The installation label on one sandbox's Pod. */
function installationOfSandbox(sandboxId: string): string {
  const pods = JSON.parse(kubectl(['get', 'pods', '-l', `opensandbox.io/id=${sandboxId}`, '-o', 'json'])).items;
  expect(pods.length, `a Pod for sandbox ${sandboxId}`).toBe(1);
  return String(pods[0].metadata.labels['astrabox.installation']);
}

/** The sandboxes the cluster runs for this installation, terminating ones included. */
function podSandboxes(installation: string): Set<string> {
  const pods = JSON.parse(kubectl(['get', 'pods', '-l', `astrabox.installation=${installation}`, '-o', 'json'])).items;
  return new Set(pods.map((pod: any) => String(pod.metadata.labels['opensandbox.io/id'])));
}

/** The base Agent's RuntimeProfile with conversation tenancy: same engine and image. */
async function conversationEnvironment(api: AstraApi): Promise<string> {
  const base = await api.defaultAgent();
  const runtime_profiles = await api.data<Array<Record<string, unknown>>>('GET', '/admin/runtime-profiles');
  const own = runtime_profiles.find((env) => env.name === base.runtime_profile_name);
  expect(own, `the base Agent's RuntimeProfile ${base.runtime_profile_name}`).toBeTruthy();
  const names = runtime_profiles
    .filter((env) => env.enabled === true && env.engine_kind === own?.engine_kind
      && env.runtime_template_name === own?.runtime_template_name
      && env.sandbox_tenancy === 'conversation')
    .map((env) => String(env.name))
    .sort();
  expect(names.length, 'an enabled conversation-tenancy RuntimeProfile like the base Agent\'s').toBeGreaterThan(0);
  const counterpart = `${String(own?.name)}-conversation`;
  return names.includes(counterpart) ? counterpart : names[0];
}

async function ownBoxAgent(api: AstraApi): Promise<string> {
  const base = await api.defaultAgent();
  const agent = await api.createAgent({
    name: `__e2e_quota_${randomUUID().slice(0, 8)}`,
    model: base.model,
    runtime_profile_name: await conversationEnvironment(api),
    prepared_capacity: 0,
  });
  agentIds.push(agent.agent_id);
  return agent.agent_id;
}

type Started = { sessionId: string; sandboxId: string } | { sessionId: string; refused: string };

/** Start one conversation and wait until it has its box or has failed. */
async function start(api: AstraApi, agentId: string): Promise<Started> {
  const created = await api.startConversation(agentId);
  sessionIds.push(created.session_id);
  try {
    const ready = await api.waitForSessionReady(created.session_id);
    return { sessionId: created.session_id, sandboxId: String(ready.sandbox_id) };
  } catch (error) {
    return { sessionId: created.session_id, refused: String((error as Error).message) };
  }
}

async function removeConversation(api: AstraApi, started: { sessionId: string; sandboxId: string }, installation: string) {
  await api.deleteSession(started.sessionId);
  await expect.poll(() => podSandboxes(installation).has(started.sandboxId), {
    message: `sandbox ${started.sandboxId} is gone from the cluster`,
    timeout: 120_000,
  }).toBe(false);
}

/** The installation the ledger's boxes are labelled with. */
function installationOf(entry: QuotaBackend): string {
  const known = [
    ...entry.boxes.map((box) => box.sandbox_id),
    ...entry.reservations.map((reservation) => String(reservation.sandbox_id || '')).filter(Boolean),
  ];
  expect(known.length, 'the deployment already runs boxes the ledger inventoried').toBeGreaterThan(0);
  return installationOfSandbox(known[0]);
}

test('the quota ledger knows every box the backend runs', async ({ request }) => {
  const api = new AstraApi(request);
  const entry = await quota(api);
  const known = new Set([
    ...entry.boxes.map((box) => box.sandbox_id),
    ...entry.reservations.map((reservation) => String(reservation.sandbox_id || '')).filter(Boolean),
  ]);
  const pods = podSandboxes(installationOf(entry));
  const unknown = [...pods].filter((sandboxId) => !known.has(sandboxId));
  expect(unknown, 'every Pod of this installation is charged in the ledger').toEqual([]);
  expect(entry.usage.boxes, 'usage never counts fewer boxes than the cluster runs').toBeGreaterThanOrEqual(pods.size);
});

test('boxes past the quota are refused, and room that comes back goes to one start', async ({ request }) => {
  test.setTimeout(360_000);
  const api = new AstraApi(request);
  const entry = await quota(api);
  const limit = Number(entry.quota.boxes);
  const installation = installationOf(entry);
  expect(limit - entry.usage.boxes, 'the run leaves room for at least one box').toBeGreaterThan(0);
  const agentId = await ownBoxAgent(api);

  const running: Array<{ sessionId: string; sandboxId: string }> = [];
  let refusal = '';
  for (let attempt = 0; attempt <= limit && !refusal; attempt += 1) {
    const started = await start(api, agentId);
    if ('refused' in started) refusal = started.refused;
    else running.push(started);
  }
  expect(running.length, 'conversations start until the quota is full').toBeGreaterThan(0);
  expect(refusal, 'the next conversation is refused for the box ceiling')
    .toContain(`has no room for another box: boxes ${limit} of ${limit} in use`);
  expect(podSandboxes(installation).size, 'the cluster never runs more boxes than the quota').toBeLessThanOrEqual(limit);

  const output = await api.runTerminalCommand(running[0].sessionId, 'echo STILL_RUNNING');
  expect(output, 'a conversation already running keeps its box').toContain('STILL_RUNNING');

  await removeConversation(api, running.pop()!, installation);
  const next = await start(api, agentId);
  expect('sandboxId' in next ? '' : next.refused, 'a box that went frees its room').toBe('');

  await removeConversation(api, running.pop() || (next as { sessionId: string; sandboxId: string }), installation);
  const results = await Promise.all([start(api, agentId), start(api, agentId)]);
  const started = results.filter((result) => 'sandboxId' in result);
  const refused = results.filter((result) => 'refused' in result) as Array<{ refused: string }>;
  expect(started.length, 'exactly one of two starts takes the last box').toBe(1);
  expect(refused.map((result) => result.refused.includes('has no room for another box'))).toEqual([true]);
  expect(podSandboxes(installation).size, 'the cluster never runs more boxes than the quota').toBeLessThanOrEqual(limit);
});
