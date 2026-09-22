/** Real Skill downloads hold startup while the platform schedules spare capacity. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField } from '../fixtures/dbOracle';
import { appPath } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { startupSkillGate } from '../fixtures/startupSkillGate';

const sessions = trackSessions();

interface IsolationRead {
  ids: string[];
  settleSeconds: number;
}

// Read the same supplier inventory as the disabled-Environment lifecycle E2E.
// The deadline is observed from the deployed code, never shortened for this test.
const ISOLATION_PROGRAM = `
import asyncio
import json
import sys
from astrabox.bootstrap import bootstrap
from astrabox.deploy.onebox import ensure_database_wiring, needs_sandbox_server, _export_backend_wiring
from astrabox.seams.sandbox import sandbox_for_name
from astrabox.core.service.orchestrator.agent.prepared_slots import _REFILL_SETTLE_TIMEOUT_S

async def main():
    ensure_database_wiring()
    if needs_sandbox_server():
        _export_backend_wiring()
    bootstrap()
    given = json.loads(sys.argv[1])
    handle = await sandbox_for_name(given['backend']).connect(given['sandboxId'])
    try:
        ids = sorted(item.session_id for item in await handle.sidecar_faces.isolation.list()
                     if item.status.lower() == 'active')
        print('E2E_STARTUP_ISOLATIONS=' + json.dumps({
            'ids': ids, 'settleSeconds': _REFILL_SETTLE_TIMEOUT_S,
        }))
    finally:
        await handle.close()

asyncio.run(main())
`;

function isolations(backend: string, sandboxId: string): IsolationRead {
  const output = execFileSync('docker', [
    'exec', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-c',
    ISOLATION_PROGRAM, JSON.stringify({ backend, sandboxId }),
  ], { encoding: 'utf8', timeout: 12_000, stdio: ['ignore', 'pipe', 'pipe'] });
  const lines = output.split('\n').filter((line) => line.startsWith('E2E_STARTUP_ISOLATIONS='));
  expect(lines).toHaveLength(1);
  return JSON.parse(lines[0].slice('E2E_STARTUP_ISOLATIONS='.length)) as IsolationRead;
}

function row(collection: string, field: string, id: string): Record<string, unknown> {
  const rows = documentsByField(collection, `$.${field}`, id);
  expect(rows).toHaveLength(1);
  return rows[0];
}

async function enablePrewarm(api: AstraApi, agentId: string): Promise<void> {
  const current = await api.getAgent(agentId);
  await api.updateAgent(agentId, {
    name: current.name, model: current.model, environment_name: current.environment_name,
    version: current.version, prewarm_enabled: true,
  });
}

async function readyCapacity(platform: PlatformApi, agentId: string): Promise<Record<string, unknown>> {
  let status: Record<string, unknown> = {};
  await expect.poll(async () => {
    status = await platform.preparedRuntime(agentId);
    expect(status.last_error).toBeNull();
    return status.ready === true && Number(status.prepared_count) > 0;
  }, { timeout: 30_000, intervals: [500, 1_000, 2_000] }).toBe(true);
  return status;
}

for (const boundary of ['foreground-release', 'bounded-wait'] as const) {
  test(`${boundary}: a held foreground startup does not lose prepared capacity`, async ({ page, request }) => {
    const api = new AstraApi(request);
    const platform = new PlatformApi(request);
    const environmentName = String(process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT || '').trim();
    const researchAgent = String(process.env.ASTRABOX_E2E_RESEARCH_AGENT || '').trim();
    expect(environmentName).not.toBe('');
    expect(researchAgent).not.toBe('');
    const model = await api.configuredAgentModel(researchAgent, environmentName);
    const gate = await startupSkillGate();
    const agents: string[] = [];
    const observations: Array<Record<string, unknown>> = [];
    let completed = false;
    let enabledAt = 0;
    let firstExtraAt: number | null = null;
    let sessionId = '';
    try {
      // A fresh Skill repository and disabled prewarm ensure that the first
      // clone belongs to this foreground Session's startup.
      const agent = await api.createAgent({
        name: `__e2e_startup_priority_${randomUUID()}`, model,
        environment_name: environmentName, prewarm_enabled: false, skills: [gate.descriptor],
      });
      agents.push(agent.agent_id);
      sessionId = (await api.startConversation(agent.agent_id)).session_id;
      sessions.push(sessionId);
      await expect.poll(() => {
        expect(gate.errors).toEqual([]);
        return gate.heldAt;
      }, { timeout: 40_000, message: 'a real foreground Git request must reach the release gate' }).not.toBeNull();
      const starting = row('sessions', 'session_id', sessionId);
      expect(starting.state).toBe('CREATING');
      const allocation = starting.startup_allocation as Record<string, unknown>;
      expect(allocation.scope).toBe('isolated_sessions');
      const sandboxId = String(allocation.sandbox_id || '');
      const backend = String(allocation.sandbox_backend || '');
      expect(sandboxId).not.toBe('');
      expect(backend).not.toBe('');
      expect(row('agents', 'agent_id', agent.agent_id).sandbox_id).toBe(sandboxId);
      const before = isolations(backend, sandboxId);
      expect(before.ids).toEqual([...(allocation.isolated_session_ids as string[])].sort());
      expect(before.settleSeconds).toBeGreaterThan(0);

      enabledAt = Date.now();
      await enablePrewarm(api, agent.agent_id);
      const observeHeld = () => {
        expect(gate.releasedAt).toBeNull();
        expect(gate.errors).toEqual([]);
        const current = row('sessions', 'session_id', sessionId);
        expect(current.state).toBe('CREATING');
        expect(current.startup_allocation).toEqual(allocation);
        const observed = isolations(backend, sandboxId);
        expect(observed.ids).toEqual(expect.arrayContaining(before.ids));
        const extra = observed.ids.filter((id) => !before.ids.includes(id));
        const at = Date.now();
        if (extra.length > 0 && firstExtraAt === null) firstExtraAt = at;
        observations.push({ elapsedMs: at - enabledAt, ids: observed.ids, extra });
        return extra;
      };

      if (boundary === 'foreground-release') {
        // B reaching real readiness is the progress barrier; a fixed sleep
        // would not prove that background work was actually able to advance.
        const other = await api.createAgent({
          name: `__e2e_unrelated_refill_${randomUUID()}`, model,
          environment_name: environmentName, prewarm_enabled: true,
        });
        agents.push(other.agent_id);
        let otherStatus: Record<string, unknown> = {};
        await expect.poll(async () => {
          expect(observeHeld(), 'same-Agent refill must yield while another Agent progresses').toEqual([]);
          otherStatus = await platform.preparedRuntime(other.agent_id);
          expect(otherStatus.last_error).toBeNull();
          return otherStatus.ready === true && Number(otherStatus.prepared_count) > 0;
        }, { timeout: 40_000, intervals: [1_000, 2_000] }).toBe(true);
        expect(otherStatus.sandbox_id).not.toBe(sandboxId);
        expect(isolations(backend, String(otherStatus.sandbox_id)).ids.length).toBeGreaterThanOrEqual(2);
        expect(observeHeld()).toEqual([]);
        expect(Date.now() - enabledAt).toBeLessThan(before.settleSeconds * 1_000);
      } else {
        // Wait for the real supplier-side placement after the existing bounded
        // settle window. The Skill cache lock can still delay publication, so
        // counting only a ready manifest would miss the placement boundary.
        await expect.poll(() => {
          const extra = observeHeld();
          if (extra.length > 0) {
            expect(firstExtraAt! - enabledAt, 'refill began before the foreground settle window elapsed')
              .toBeGreaterThanOrEqual(before.settleSeconds * 1_000);
          }
          return extra.length;
        }, {
          timeout: before.settleSeconds * 1_000 + 10_000, intervals: [1_000, 2_000, 4_000],
          message: 'a wedged foreground start must not suppress background placement indefinitely',
        }).toBe(2);
      }

      gate.release();
      await api.waitForSessionReady(sessionId);
      const spare = await readyCapacity(platform, agent.agent_id);
      expect(spare.sandbox_id).toBe(sandboxId);
      if (boundary === 'foreground-release') {
        expect(Date.now() - enabledAt, 'settled startup must release refill without waiting for the hard limit')
          .toBeLessThan(before.settleSeconds * 1_000);
      }
      const live = await api.adminSessionDetail(sessionId);
      expect(live.sandbox_id).toBe(sandboxId);
      expect(live.state).toBe('READY');
      const after = isolations(backend, sandboxId);
      expect(after.ids).toEqual(expect.arrayContaining(before.ids));
      expect(after.ids).toHaveLength(before.ids.length + 2);
      await page.goto(appPath(`/sessions/${sessionId}`));
      await expectComposerEnabled(page);

      // Prove the published spare is usable, not merely a positive count.
      const manifest = row('agents', 'agent_id', agent.agent_id)._prepared_slot as Record<string, unknown>;
      const next = (await api.startConversation(agent.agent_id)).session_id;
      sessions.push(next);
      await api.waitForSessionReady(next);
      const claimed = await api.adminSessionDetail(next);
      expect(claimed.sandbox_id).toBe(sandboxId);
      expect(claimed.runtime_identity?.isolated_session_id).toBe(manifest.isolated_session_id);
      expect(gate.errors).toEqual([]);
      for (const id of [...sessions]) {
        await api.deleteSession(id);
        sessions.splice(sessions.indexOf(id), 1);
      }
      for (const id of agents) await api.deleteAgent(id);
      completed = true;
    } finally {
      gate.release();
      await test.info().attach('foreground-refill-ordering', {
        body: JSON.stringify({ boundary, sessionId, agents, enabledAt, firstExtraAt, observations,
          heldAt: gate.heldAt, releasedAt: gate.releasedAt, requests: gate.requests,
          errors: gate.errors, gitCommit: gate.commit, gitVersion: gate.gitVersion, directory: gate.directory }),
        contentType: 'application/json',
      });
      await gate.close(!completed);
    }
  });
}
