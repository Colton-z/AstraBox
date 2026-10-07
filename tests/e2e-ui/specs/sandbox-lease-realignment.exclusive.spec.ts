/** A delayed provider observation cannot overwrite a newer live-box lease. */
import { execFile, execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import { promisify } from 'node:util';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { repoRoot } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();
const agents: string[] = [];
const assistants: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const id of agents.splice(0)) await api.deleteAgent(id);
  for (const id of assistants.splice(0)) await api.deleteAssistant(id);
});

for (const owner of ['agent', 'assistant'] as const) {
  test(`${owner} lease realignment preserves a concurrent renewal and the live conversations`, async ({ request }, info) => {
    const api = new AstraApi(request);
    const name = `__e2e_lease_realign_${randomUUID()}`;
    const ownSessions: string[] = [];
    if (owner === 'agent') {
      const base = await api.defaultAgent();
      expect(base.environment_name).toBeTruthy();
      expect(base.model).toBeTruthy();
      const agent = await api.createAgent({
        name, environment_name: base.environment_name, model: base.model, prepared_capacity: 0,
      });
      agents.push(agent.agent_id);
      for (let index = 0; index < 2; index += 1) {
        const created = await api.startConversation(agent.agent_id);
        sessions.push(created.session_id);
        ownSessions.push(created.session_id);
        await api.waitForSessionReady(created.session_id);
      }
    } else {
      const environment = await api.assistantEnvironmentName();
      const assistant = await api.createAssistant({
        display_name: name, environment_name: environment,
        model_config_override: { model_name: await api.assistantModelName(environment) },
      });
      const assistantId = String(assistant.assistant_id);
      assistants.push(assistantId);
      await api.waitForWorkspaceReady(assistantId);
      const created = await api.startAssistantConversation(assistantId);
      sessions.push(created.session_id);
      ownSessions.push(created.session_id);
      await api.waitForSessionReady(created.session_id);
    }
    const before = await Promise.all(ownSessions.map((id) => api.getSession(id)));
    const sandboxId = String(before[0]!.sandbox_id);
    expect(sandboxId).not.toBe('');
    expect(before.every((session) => session.sandbox_id === sandboxId)).toBe(true);
    const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
    const script = `/tmp/astrabox-lease-${ownSessions[0]}.py`;
    execFileSync('docker', ['cp', path.join(repoRoot, 'tests/e2e-ui/fixtures/lease_realignment_replica.py'), `${server}:${script}`], {
      timeout: 20_000,
    });
    const { stdout } = await promisify(execFile)('docker', [
      'exec', server, 'python', script, '--session-id', ownSessions[0]!, '--owner-kind', owner,
    ], { timeout: 45_000, maxBuffer: 1024 * 1024 });
    const receipts = stdout.split('\n').filter((line) => line.startsWith('ASTRABOX_LEASE_REALIGNMENT='));
    expect(receipts).toHaveLength(1);
    const evidence = JSON.parse(receipts[0]!.slice('ASTRABOX_LEASE_REALIGNMENT='.length));
    expect(evidence.sandbox_id).toBe(sandboxId);
    expect(evidence.retained.filter((item: Record<string, unknown>) => item.collection === 'sessions')
      .map((item: Record<string, unknown>) => item.id)).toEqual(expect.arrayContaining(ownSessions));
    expect(evidence.retained.some((item: Record<string, unknown>) => item.collection === (
      owner === 'agent' ? 'agents' : 'assistant_workspace'
    ))).toBe(true);
    await info.attach('sandbox-lease-realignment', { body: JSON.stringify(evidence), contentType: 'application/json' });
    const provider = await api.getSandbox(sandboxId);
    expect(String(provider.state).toLowerCase()).toBe('running');
    expect(Date.parse(String(provider.expires_at))).toBe(Date.parse(evidence.renewed_expires_at));
    for (const id of ownSessions) {
      const receipt = `LEASE_OK_${randomUUID()}`;
      const reply = await api.sendTurn(id, `Reply exactly ${receipt}. Do not use tools.`, 60_000);
      expect(reply.errorText).toBeNull();
      expect(reply.text.trim()).toBe(receipt);
      const settled = await api.waitForSessionReady(id);
      expect(settled.sandbox_id).toBe(sandboxId);
      expect(settled.runtime_unavailable).not.toBe(true);
    }
  });
}
