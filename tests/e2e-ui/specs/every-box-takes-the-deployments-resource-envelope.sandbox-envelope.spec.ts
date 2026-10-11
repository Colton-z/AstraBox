/**
 * E2E: every box a deployment creates takes the size the deployment set for its kind.
 *
 * The server runs with ASTRABOX_SANDBOX_* (a conversation's own box) and
 * ASTRABOX_SHARED_SANDBOX_* (an Agent's shared box) set to sizes other than the
 * defaults, and the run names the same sizes in ASTRABOX_E2E_SANDBOX_ENVELOPE, as
 * JSON: `{"own": {"limits": {...}, "requests": {...}}, "shared": {...}}`.
 * Without it Playwright does not collect this file (playwright.config.ts).
 *
 * The platform creates a box in three places, and each is checked on the box
 * the cluster actually runs, not on what the platform says it asked for: the
 * Pod's own requests and limits as the Kubernetes API reports them, and the
 * CPU and memory ceilings the box's processes run under (cgroup v2).
 *
 * - a conversation's own box, created when the conversation starts;
 * - an Agent's shared box, created when its first conversation starts;
 * - a prepared box from an Agent's pool, which a conversation then claims.
 *
 * Deployment fixtures this reads and never creates: the deployment-proven
 * Agent (`ASTRABOX_E2E_AGENT_NAME`) for its model, and one enabled Environment
 * of each sandbox tenancy for that Agent's engine.
 */

import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { requireSandboxHandle, sandboxExec } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

interface Envelope {
  limits: { cpu: string; memory: string };
  requests: { cpu: string; memory: string };
}

interface Envelopes {
  own: Envelope;
  shared: Envelope;
}

const sessions = trackSessions();
const agentIds: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const agentId of agentIds.splice(0)) await api.deleteAgent(agentId);
});

function declaredEnvelopes(): Envelopes {
  const raw = String(process.env.ASTRABOX_E2E_SANDBOX_ENVELOPE || '').trim();
  const value = JSON.parse(raw) as Envelopes;
  for (const box of ['own', 'shared'] as const) {
    for (const side of ['limits', 'requests'] as const) {
      for (const kind of ['cpu', 'memory'] as const) {
        expect(String(value?.[box]?.[side]?.[kind] || ''),
          `ASTRABOX_E2E_SANDBOX_ENVELOPE names ${box}.${side}.${kind}`).not.toBe('');
      }
    }
  }
  return value;
}

/** Cores in a Kubernetes CPU quantity: `2`, `0.5` or `500m`. */
function cores(quantity: string): number {
  const match = /^(\d+(?:\.\d+)?)(m?)$/.exec(quantity);
  if (!match) throw new Error(`${JSON.stringify(quantity)} is not a CPU quantity`);
  return match[2] ? Number(match[1]) / 1000 : Number(match[1]);
}

/** Bytes in a Kubernetes memory quantity: `2Gi`, `768Mi` or a plain count. */
function bytes(quantity: string): number {
  const units: Record<string, number> = {
    '': 1, k: 1e3, M: 1e6, G: 1e9, T: 1e12, Ki: 2 ** 10, Mi: 2 ** 20, Gi: 2 ** 30, Ti: 2 ** 40,
  };
  const match = /^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|k|M|G|T)?$/.exec(quantity);
  if (!match) throw new Error(`${JSON.stringify(quantity)} is not a memory quantity`);
  return Number(match[1]) * units[match[2] || ''];
}

/** An enabled Environment of this tenancy for the engine the base Agent runs. */
async function environmentWithTenancy(
  api: AstraApi,
  engineKind: string,
  tenancy: 'conversation' | 'agent',
): Promise<string> {
  const environments = await api.data<Array<Record<string, unknown>>>('GET', '/admin/environments');
  const names = environments
    .filter((env) => env.enabled === true && env.engine_kind === engineKind && env.sandbox_tenancy === tenancy)
    .map((env) => String(env.name));
  expect(names.length, `an enabled ${engineKind} Environment with ${tenancy} tenancy`).toBeGreaterThan(0);
  return names.sort()[0];
}

async function createAgent(
  api: AstraApi,
  label: string,
  environmentName: string,
  preparedCapacity: number,
): Promise<string> {
  const base = await api.defaultAgent();
  const agent = await api.createAgent({
    name: `__e2e_envelope_${label}_${randomUUID().slice(0, 8)}`,
    model: base.model,
    environment_name: environmentName,
    prepared_capacity: preparedCapacity,
  });
  agentIds.push(agent.agent_id);
  return agent.agent_id;
}

async function readyConversation(api: AstraApi, agentId: string): Promise<string> {
  const created = await api.startConversation(agentId);
  sessions.push(created.session_id);
  const ready = await api.waitForSessionReady(created.session_id);
  return String(ready.sandbox_id);
}

/** The box as the cluster runs it: the Pod's resources and the cgroup ceilings inside it. */
async function assertBoxTakesTheEnvelope(api: AstraApi, sandboxId: string, envelope: Envelope): Promise<void> {
  const handle = await requireSandboxHandle(api, sandboxId);
  expect(handle.runtime, 'this spec reads a Pod; run it against a Kubernetes deployment').toBe('kubernetes');
  if (handle.runtime !== 'kubernetes') return;

  const pod = spawnSync('kubectl', ['get', 'pod', '-n', handle.namespace, handle.pod, '-o', 'json'], {
    encoding: 'utf8',
    timeout: 30_000,
  });
  expect(pod.status, `kubectl get pod ${handle.pod}: ${pod.stderr}`).toBe(0);
  const containers = (JSON.parse(pod.stdout).spec?.containers || []) as Array<Record<string, any>>;
  const sandbox = containers.find((container) => container.name === 'sandbox');
  expect(sandbox, `Pod ${handle.pod} has a sandbox container`).toBeTruthy();
  const resources = sandbox!.resources || {};
  const seen = {
    limits: { cpu: cores(String(resources.limits?.cpu)), memory: bytes(String(resources.limits?.memory)) },
    requests: { cpu: cores(String(resources.requests?.cpu)), memory: bytes(String(resources.requests?.memory)) },
  };
  expect(seen, `Pod ${handle.pod} of sandbox ${sandboxId}`).toEqual({
    limits: { cpu: cores(envelope.limits.cpu), memory: bytes(envelope.limits.memory) },
    requests: { cpu: cores(envelope.requests.cpu), memory: bytes(envelope.requests.memory) },
  });

  const [cpuMax, memoryMax] = sandboxExec(handle, 'cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/memory.max')
    .trim()
    .split('\n');
  const [quota, period] = cpuMax.trim().split(/\s+/);
  expect(Number(quota) / Number(period), `cpu.max ${cpuMax} inside sandbox ${sandboxId}`)
    .toBeCloseTo(cores(envelope.limits.cpu), 3);
  expect(Number(memoryMax), `memory.max inside sandbox ${sandboxId}`).toBe(bytes(envelope.limits.memory));
}

async function engineKind(api: AstraApi): Promise<string> {
  const base = await api.defaultAgent();
  const environments = await api.data<Array<Record<string, unknown>>>('GET', '/admin/environments');
  const own = environments.find((env) => env.name === base.environment_name);
  expect(own, `the base Agent's Environment ${base.environment_name}`).toBeTruthy();
  return String(own!.engine_kind);
}

async function preparedBox(request: APIRequestContext, agentId: string): Promise<string> {
  const platform = new PlatformApi(request);
  let prepared: Record<string, unknown> = {};
  await expect.poll(async () => {
    prepared = await platform.preparedRuntime(agentId);
    return prepared.ready === true && Number(prepared.prepared_count || 0) > 0 && Boolean(prepared.sandbox_id);
  }, {
    message: 'the Agent prepares a box',
    timeout: parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 120_000),
  }).toBe(true);
  return String(prepared.sandbox_id);
}

test('a conversation\'s own box takes the size set for own boxes', async ({ request }) => {
  const envelope = declaredEnvelopes().own;
  const api = new AstraApi(request);
  const environment = await environmentWithTenancy(api, await engineKind(api), 'conversation');
  const agentId = await createAgent(api, 'own', environment, 0);

  await assertBoxTakesTheEnvelope(api, await readyConversation(api, agentId), envelope);
});

test('an Agent\'s shared box takes the size set for shared boxes', async ({ request }) => {
  const envelope = declaredEnvelopes().shared;
  const api = new AstraApi(request);
  const environment = await environmentWithTenancy(api, await engineKind(api), 'agent');
  const agentId = await createAgent(api, 'shared', environment, 0);

  await assertBoxTakesTheEnvelope(api, await readyConversation(api, agentId), envelope);
});

test('a prepared shared box takes the size set for shared boxes, and a conversation claims it', async ({
  request,
}) => {
  const envelope = declaredEnvelopes().shared;
  const api = new AstraApi(request);
  const environment = await environmentWithTenancy(api, await engineKind(api), 'agent');
  const agentId = await createAgent(api, 'prepared', environment, 1);

  const box = await preparedBox(request, agentId);
  await assertBoxTakesTheEnvelope(api, box, envelope);
  expect(await readyConversation(api, agentId), 'the conversation claims the prepared box').toBe(box);
});
