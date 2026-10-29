/**
 * Real credential substitution in the claimed workload, terminal and reattached box.
 * The configuration-transition case requires a task-owned candidate; its caller
 * restores that fixture's configuration after collecting the case result.
 */
import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText, messageToolCalls, sseFrames } from '../fixtures/astraApi';
import { absoluteBaseUrl, apiPath, appPath } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { requireSandboxHandle, restartServerContainer } from '../fixtures/sandboxOps';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();
let agentId = '';
let vaultId = '';
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  if (agentId) {
    if (vaultId) await api.data('PUT', `/admin/agents/${agentId}/credential-vaults`, { vault_ids: [] });
    await api.deleteAgent(agentId);
  }
  if (vaultId) await new PlatformApi(request).deleteVault(vaultId);
  agentId = '';
  vaultId = '';
});

async function workload(request: APIRequestContext, prepared: boolean) {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const probe = String(process.env.ASTRABOX_E2E_CREDENTIAL_PROBE_URL || '').replace(/\/$/, '');
  expect(probe, 'the configured HTTP authorization fixture is required').toMatch(/^http:\/\/[A-Za-z0-9.-]+$/);
  const profile = String((prepared
    ? process.env.ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT
    : process.env.ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT) || '').trim();
  expect(profile, 'a configured conversation-tenancy RuntimeProfile is required').not.toBe('');
  const base = await api.defaultAgent();
  const name = `__e2e_vault_reattach_${randomUUID()}`;
  const secretName = `VAULT_REATTACH_${randomUUID().replaceAll('-', '').toUpperCase()}`;
  const vault = await platform.createVault(name);
  vaultId = vault.vault_id;
  const credential = await platform.createCredential(vaultId, {
    type: 'environment_variable', secret_name: secretName,
    secret_value: 'astrabox-e2e-request-match-secret',
    networking: { type: 'limited', allowed_hosts: [`${new URL(probe).hostname}:80`] },
    injection_location: { header: true, body: false }, allow_insecure_http: true,
    allowed_requests: { methods: ['GET'], paths: ['/allowed'] },
  });
  const config = { name, model: base.model, runtime_profile_name: profile, prepared_capacity: 0, terminal_panel: true };
  const agent = await api.createAgent(config);
  agentId = agent.agent_id;
  await api.data('PUT', `/admin/agents/${agentId}/credential-vaults`, { vault_ids: [vaultId] });
  let preparedBox = '';
  if (prepared) {
    const latest = await api.getAgent(agentId);
    await api.updateAgent(agentId, { ...config, version: latest.version, prepared_capacity: 1 });
    await expect.poll(async () => {
      const capacity = await platform.preparedRuntime(agentId);
      if (capacity.ready === true && Number(capacity.prepared_count) > 0) {
        preparedBox = String(capacity.sandbox_id || '');
      }
      return preparedBox;
    }, { timeout: 60_000, intervals: [500, 1_000] }).not.toBe('');
  }
  const sessionId = (await api.startConversation(agentId)).session_id;
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId, 60_000);
  const box = String(ready.sandbox_id || '');
  expect(box).not.toBe('');
  if (prepared) expect(box, 'claim the exact prepared box').toBe(preparedBox);
  const command = [
    `value="\${${secretName}}"`,
    'case "$value" in ASTRABOX-VAULT-CRED::*) ;; *) exit 91;; esac',
    `status=$(curl --max-time 10 -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $value" '${probe}/allowed')`,
    'digest=$(printf %s "$value" | sha256sum | cut -d " " -f 1)',
    'printf "VAULT_HTTP=%s PLACEHOLDER_SHA256=%s\\n" "$status" "$digest"',
  ].join('; ');
  return { api, platform, sessionId, box, command, credentialId: credential.credential_id };
}

function probeResult(raw: string, status: number): string {
  const result = raw.match(new RegExp(`VAULT_HTTP=${status} PLACEHOLDER_SHA256=([a-f0-9]{64})`));
  expect(result, 'the real request must report its HTTP status and the inert value it used').not.toBeNull();
  expect(raw).not.toContain('astrabox-e2e-request-match-secret');
  return result![1];
}

async function engineProbe(api: AstraApi, sessionId: string, command: string): Promise<string> {
  const raw = await api.streamPrompt(sessionId,
    `Use Bash to run exactly this command once, then report its output: ${command}`, 'bypassPermissions', 45_000);
  const results = sseFrames(raw).filter((frame) => frame.type === 'tool-output-available');
  expect(results.length, 'the real engine must return its tool result').toBeGreaterThan(0);
  return probeResult(JSON.stringify(results.map((frame) => frame.output)), 204);
}

test('a claimed environment placeholder still authenticates in its engine and terminal after server restart', async ({ request, page }, info) => {
  const w = await workload(request, true);
  const first = probeResult(await w.api.runTerminalCommand(w.sessionId, w.command), 204);
  expect(await engineProbe(w.api, w.sessionId, w.command)).toBe(first);
  await page.goto(appPath(`/sessions/${w.sessionId}`));
  await expect(page.getByTestId('composer-prompt')).toBeEnabled();
  await restartServerContainer(absoluteBaseUrl(), 60_000);
  const again = await w.api.waitForSessionReady(w.sessionId, 30_000);
  expect(again.sandbox_id).toBe(w.box);
  expect(probeResult(await w.api.runTerminalCommand(w.sessionId, w.command), 204)).toBe(first);
  expect(await engineProbe(w.api, w.sessionId, w.command)).toBe(first);
  await page.reload();
  await expect(page.getByTestId('composer-prompt')).toBeEnabled();
  await info.attach('placeholder-custody.json', { body: JSON.stringify({
    session: w.sessionId, sandbox: w.box, placeholderSha256: first,
  }), contentType: 'application/json' });
});

test('archiving an environment credential removes its live bindings and stops its old placeholder authenticating', async ({ request, page }, info) => {
  const w = await workload(request, false);
  const first = probeResult(await w.api.runTerminalCommand(w.sessionId, w.command), 204);
  const binding = `astrabox-vault-${w.credentialId}`;
  const before = await w.platform.sandboxSecurity(w.box);
  expect(before.credential_names).toContain(binding);
  expect(before.binding_names).toContain(`${binding}-http`);
  await page.goto(appPath(`/manage/sandboxes/${w.box}`));
  await expect(page.getByText(`${binding}-http`, { exact: true })).toBeVisible();
  await w.platform.archiveCredential(vaultId, w.credentialId);
  await restartServerContainer(absoluteBaseUrl());
  // Watching the conversation starts its reader again; a read-only status GET
  // alone does not reconnect an idle engine or refresh its egress bindings.
  await page.goto(appPath(`/sessions/${w.sessionId}`));
  await expect(page.getByTestId('composer-prompt')).toBeEnabled();
  const after = await w.api.waitForSessionReady(w.sessionId, 30_000);
  expect(after.sandbox_id).toBe(w.box);
  await expect.poll(async () => (await w.platform.sandboxSecurity(w.box)).binding_names, {
    timeout: 15_000, intervals: [500, 1_000],
  }).not.toContain(`${binding}-http`);
  const posture = await w.platform.sandboxSecurity(w.box);
  expect(posture.credential_names).not.toContain(binding);
  expect(posture.binding_names).not.toContain(`${binding}-http`);
  expect(probeResult(await w.api.runTerminalCommand(w.sessionId, w.command), 401)).toBe(first);
  await page.goto(appPath(`/manage/sandboxes/${w.box}`));
  await expect(page.getByText(`${binding}-http`, { exact: true })).toHaveCount(0);
  await info.attach('revocation.json', { body: JSON.stringify({
    session: w.sessionId, sandbox: w.box, before, after: posture, placeholderSha256: first,
  }), contentType: 'application/json' });
});

async function setServerVault(enabled: boolean): Promise<void> {
  const path = String(process.env.ASTRABOX_E2E_CANDIDATE_COMPOSE_FILE || '').trim();
  expect(path, 'a task-owned Compose candidate must be declared for the configuration transition').not.toBe('');
  const fixture = JSON.parse(readFileSync(path, 'utf8')) as {
    compose: string[]; project: string; server: string; base_url: string;
  };
  expect(fixture.server).toBe(requireServiceContainer(SERVER_CONTAINER_HANDLE));
  expect(fixture.base_url).toBe(absoluteBaseUrl());
  expect(fixture.project, 'the original astrabox demo project cannot be reconfigured').toMatch(/^astrabox-.+/);
  expect(fixture.compose.slice(0, 2)).toEqual(['docker', 'compose']);
  const inspect = JSON.parse(execFileSync('docker', ['inspect', fixture.server], { encoding: 'utf8' }))[0];
  expect(inspect.Config.Labels['com.docker.compose.project']).toBe(fixture.project);
  const override = join(dirname(path), `vault-${enabled}-${randomUUID()}.json`);
  writeFileSync(override, JSON.stringify({ services: { server: { environment: {
    ASTRABOX_SANDBOX_CREDENTIAL_VAULT: String(enabled),
  } } } }), { mode: 0o600 });
  execFileSync('docker', [...fixture.compose.slice(1), '-f', override,
    'up', '-d', '--no-build', '--no-deps', 'server'], { timeout: 60_000, stdio: 'pipe' });
  const current = JSON.parse(execFileSync('docker', ['inspect', fixture.server], { encoding: 'utf8' }))[0];
  expect(current.Config.Env).toContain(`ASTRABOX_SANDBOX_CREDENTIAL_VAULT=${enabled}`);
  await expect.poll(async () => {
    const response = await fetch(`${absoluteBaseUrl()}/readyz`, { signal: AbortSignal.timeout(2_000) }).catch(() => null);
    return response?.status;
  }, { timeout: 60_000, intervals: [500, 1_000] }).toBe(200);
}

async function volumeFreeBox(api: AstraApi, box: string) {
  const handle = await requireSandboxHandle(api, box);
  let mounts: unknown[];
  if (handle.runtime === 'docker') {
    const native: Array<{ Type: string; Destination: string; Name?: string }> = JSON.parse(execFileSync('docker', [
      'inspect', handle.container, '--format', '{{json .Mounts}}',
    ], { encoding: 'utf8', timeout: 10_000 }));
    mounts = native.filter((mount) => !(mount.Type === 'volume'
      && mount.Destination === '/opt/opensandbox' && mount.Name === `opensandbox-runtime-${box}`));
  } else {
    const pod: { spec: {
      volumes?: Array<{ name: string; persistentVolumeClaim?: unknown }>;
      containers: Array<{ name: string; volumeMounts?: Array<{ name: string }> }>;
    } } = JSON.parse(execFileSync('kubectl', [
      'get', 'pod', handle.pod, '-n', handle.namespace, '-o', 'json',
    ], { encoding: 'utf8', timeout: 10_000 }));
    const claims = new Set((pod.spec.volumes || []).filter((volume) => volume.persistentVolumeClaim).map((volume) => volume.name));
    const workload = pod.spec.containers.find((container) => container.name === 'sandbox');
    expect(workload, 'the live Pod must expose its native sandbox container').toBeTruthy();
    mounts = (workload!.volumeMounts || []).filter((mount) => claims.has(mount.name));
  }
  expect(mounts, 'native recovery must run without a persistent workspace volume').toEqual([]);
  return { box, runtime: handle.runtime, mounts };
}

test('disabling the vault refuses the old bound workload and terminal until explicit volume-free replacement', async ({ request, page }, info) => {
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(`__e2e_vault_disabled_${randomUUID()}`);
  agentId = agent.agent_id;
  const sessionId = (await api.startConversation(agentId)).session_id;
  sessions.push(sessionId);
  const first = await api.waitForSessionReady(sessionId, 60_000);
  const oldVolumes = await volumeFreeBox(api, String(first.sandbox_id));
  const reference = `CUSTODY-${randomUUID()}`;
  const taught = await api.sendTurn(sessionId, `Remember ${reference}. Acknowledge briefly without tools.`, 30_000);
  expect(taught.errorText).toBeNull();
  const history = (await api.getMessages(sessionId)).messages;
  await setServerVault(false);
  await page.goto(appPath(`/sessions/${sessionId}`));
  await expect(page.getByText(/ASTRABOX_SANDBOX_CREDENTIAL_VAULT/).first()).toBeVisible();
  const refused = await request.post(`${apiPath(`/sessions/${sessionId}/terminal/stream`)}`, {
    data: { command: 'printf UNEXPECTED_EXECUTION' },
  });
  expect(refused.status(), await refused.text()).toBe(400);
  expect((await refused.json()).code).toBe('SANDBOX_CREDENTIAL_VAULT_DISABLED');
  const held = await api.getSession(sessionId);
  expect(held.sandbox_id).toBe(first.sandbox_id);
  expect(held.current_responses || []).toEqual([]);
  await api.terminateSandbox(sessionId);
  const recalled = await api.sendTurn(sessionId,
    'What reference did I ask you to remember? Reply with only that reference and use no tools.', 60_000);
  expect(recalled.errorText).toBeNull();
  const reply = await api.waitForReplyTo(sessionId, recalled.inputId, { timeoutMs: 10_000 });
  expect(reply.metadata?.outcome).toBe('completed');
  expect(messageText(reply).trim()).toBe(reference);
  expect(messageToolCalls(reply)).toEqual([]);
  const restored = await api.getSession(sessionId);
  expect(restored.sandbox_id).not.toBe(first.sandbox_id);
  const newVolumes = await volumeFreeBox(api, String(restored.sandbox_id));
  expect((await api.getMessages(sessionId)).messages.slice(0, history.length)).toEqual(history);
  await info.attach('vault-disabled-replacement.json', { body: JSON.stringify({
    session: sessionId, oldBox: first.sandbox_id, newBox: restored.sandbox_id,
    refused: 'SANDBOX_CREDENTIAL_VAULT_DISABLED', reference, oldVolumes, newVolumes,
  }), contentType: 'application/json' });
});
