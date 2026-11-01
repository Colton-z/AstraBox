/** Agent management authorizes a channel policy, without granting a sender's account. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { agentSessionRows, channelInboundRows, channelMessageInputs } from '../fixtures/ingressRecords';
import { PlatformApi } from '../fixtures/platformApi';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

type Identity = { user_id: string; roles: string[]; token: string };

test('an Agent creator and its administrators configure its channel identity while foreign managers cannot', async ({
  playwright, page,
}, info) => {
  // The installed identity fixture authenticates these opaque tokens through
  // the public identity seam; none is a machine token with an admin API scope.
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const identities: Identity[] = JSON.parse(execFileSync('docker', [
    'exec', server, 'cat', '/data/session-mcp-fixture/identities.private.json',
  ], { encoding: 'utf8', timeout: 10_000 }));
  const [admin, owner, manager, ordinary] = identities;
  expect(identities).toHaveLength(4);
  expect(admin.roles).toContain('admin');
  for (const identity of [owner, manager, ordinary]) expect(identity.roles).toEqual([]);
  const clients: APIRequestContext[] = [];
  const agents: string[] = [];
  const bindings: Array<{ agent: string; deployment: string }> = [];
  let passed = false;
  try {
    for (const identity of identities) clients.push(await playwright.request.newContext({
      storageState: { cookies: [], origins: [] },
      extraHTTPHeaders: { Authorization: `Bearer ${identity.token}` },
    }));
    const [adminClient, ownerClient, managerClient, ordinaryClient] = clients;
    const ownApi = new AstraApi(ownerClient);
    const ownPlatform = new PlatformApi(ownerClient);
    const adminApi = new AstraApi(adminClient);
    const id = randomUUID();
    const base = await adminApi.defaultAgent();
    const profile = process.env.ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT;
    expect(profile, 'the configured cold RuntimeProfile is required').toBeTruthy();
    const own = await ownApi.createAgent({
      name: `__e2e_agent_identity_${id}`, model: base.model,
      runtime_profile_name: profile, prepared_capacity: 0,
    });
    agents.push(own.agent_id);
    await ownApi.setAgentAccess(own.agent_id, { visibility: 'public', admins: [manager.user_id], allowed_user_ids: [] });
    const foreign = await adminApi.createColdTestAgent(`__e2e_foreign_identity_${id}`);
    agents.push(foreign.agent_id);
    const targetPath = (agent: string, deployment: string) => `/admin/agents/${agent}/deployments/${deployment}`;
    const binding = await ownPlatform.createDeployment(own.agent_id, { scene: 'channel:generic_json', execution_mode: 'linked' });
    bindings.push({ agent: own.agent_id, deployment: binding.deployment_id });
    const foreignBinding = await new PlatformApi(adminClient).createDeployment(foreign.agent_id, { scene: 'channel:generic_json' });
    bindings.push({ agent: foreign.agent_id, deployment: foreignBinding.deployment_id });

    for (const [client, identity] of [[ownerClient, owner], [managerClient, manager], [adminClient, owner]] as const) {
      const catalog = await client.get(apiPath(`/channel-providers?agent_id=${own.agent_id}`));
      expect(catalog.status(), await catalog.text()).toBe(200);
      const provider = (await catalog.json()).data.find((row: { name: string }) => row.name === 'generic_json');
      expect(provider.can_configure_execution_user).toBe(true);
      const changed = await client.put(apiPath(targetPath(own.agent_id, binding.deployment_id)), {
        data: { execution_mode: 'account', execution_user_id: identity.user_id },
      });
      expect(changed.status(), await changed.text()).toBe(200);
      expect((await changed.json()).data).toMatchObject({ execution_mode: 'account', execution_user_id: identity.user_id });
      const linked = await client.put(apiPath(targetPath(own.agent_id, binding.deployment_id)), { data: { execution_mode: 'linked' } });
      expect(linked.status(), await linked.text()).toBe(200);
    }
    // Managing an Agent does not delegate another employee's identity.
    for (const client of [ownerClient, managerClient]) {
      const forbiddenAccount = await client.put(apiPath(targetPath(own.agent_id, binding.deployment_id)), {
        data: { execution_mode: 'account', execution_user_id: ordinary.user_id },
      });
      expect(forbiddenAccount.status(), await forbiddenAccount.text()).toBe(403);
      expect((await ownPlatform.listDeployments(own.agent_id))
        .find((row) => row.deployment_id === binding.deployment_id)).toMatchObject({
        execution_mode: 'linked', execution_user_id: '',
      });
    }
    for (const [client, agent, deployment] of [
      [managerClient, foreign.agent_id, foreignBinding.deployment_id],
      [ordinaryClient, own.agent_id, binding.deployment_id],
    ] as const) {
      const deniedCatalog = await client.get(apiPath(`/channel-providers?agent_id=${agent}`));
      expect(deniedCatalog.status()).toBe(404);
      const deniedUpdate = await client.put(apiPath(targetPath(agent, deployment)), { data: { execution_mode: 'owner' } });
      expect(deniedUpdate.status()).toBe(404);
      const deniedCreate = await client.post(apiPath(`/admin/agents/${agent}/deployments`), {
        data: { scene: 'channel:generic_json', execution_mode: 'account', execution_user_id: ordinary.user_id },
      });
      expect(deniedCreate.status()).toBe(404);
    }
    const globalCatalog = await managerClient.get(apiPath('/channel-providers'));
    expect((await globalCatalog.json()).data.every((row: { can_configure_execution_user: boolean }) => !row.can_configure_execution_user)).toBe(true);

    // Both form paths must use the selected Agent's capability, not a global role.
    await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${manager.token}` });
    await page.goto(appPath('/manage/deployments/new'));
    await page.getByLabel('Agent or Assistant', { exact: true }).selectOption(`agent:${own.agent_id}`);
    await page.getByLabel('Trigger', { exact: true }).selectOption('channel:generic_json');
    await expect(page.getByLabel('Run messages as', { exact: true })).toBeEnabled();
    await page.getByLabel('Run messages as', { exact: true }).selectOption('account');
    await expect(page.getByLabel('Execution account user ID', { exact: true })).toBeEditable();
    await page.goto(appPath(`/manage/deployments/${binding.deployment_id}`));
    await page.getByLabel('Run messages as', { exact: true }).selectOption('account');
    await page.getByLabel('Execution account user ID', { exact: true }).fill(manager.user_id);
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect.poll(async () => (await ownPlatform.listDeployments(own.agent_id))
      .find((row) => row.deployment_id === binding.deployment_id)?.execution_user_id).toBe(manager.user_id);
    await page.reload();
    await expect(page.getByLabel('Execution account user ID', { exact: true })).toHaveValue(manager.user_id);
    await ownPlatform.updateDeployment(own.agent_id, binding.deployment_id, { execution_mode: 'linked' });

    const refused = await ownerClient.post(apiPath(`/deployments/${binding.deployment_id}/trigger`), {
      headers: { 'x-channel-secret': String(binding.secret) },
      data: { sender: `unlinked-${id}`, conversation_id: id, message_id: id,
        text: 'This sender has no delegated account.' },
    });
    expect(refused.ok(), await refused.text()).toBe(true);
    await expect.poll(() => channelInboundRows(binding.deployment_id)[0]?.ignored_reason)
      .toBe('CHANNEL_PARTICIPANT_NOT_LINKED');
    const row = channelInboundRows(binding.deployment_id)[0];
    expect(row).toMatchObject({ state: 'IGNORED', session_id: null });
    expect(channelMessageInputs(row.item_id)).toEqual([]);
    expect(agentSessionRows(own.agent_id)).toEqual([]);
    await info.attach('scoped-channel-permissions.json', { body: JSON.stringify({
      agent: own.agent_id, foreignAgent: foreign.agent_id, binding: binding.deployment_id,
      administrators: [admin.user_id, owner.user_id, manager.user_id], refusedInbound: row,
    }), contentType: 'application/json' });
    passed = true;
  } finally {
    if (passed) {
      const platform = new PlatformApi(clients[0]);
      for (const binding of bindings) await platform.deleteDeployment(binding.agent, binding.deployment);
      for (const agent of agents) await new AstraApi(clients[0]).deleteAgent(agent);
    }
    for (const client of clients) await client.dispose();
  }
});
