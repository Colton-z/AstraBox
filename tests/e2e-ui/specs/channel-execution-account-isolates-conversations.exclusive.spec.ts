/** A delegated channel owns its history even when its administrator changes accounts. */
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { channelCallback } from '../fixtures/channelCallback';
import { documentsByField } from '../fixtures/dbOracle';
import { apiPath, appPath } from '../fixtures/env';
import { deployedApiClient, issueToken, json, requiredEnvironment } from '../fixtures/oidcMachine';
import { PlatformApi } from '../fixtures/platformApi';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('changing a channel execution account isolates history and switching back resumes its own conversation', async ({
  request, page, playwright,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const recipient = await channelCallback();
  const issuer = requiredEnvironment('ASTRABOX_E2E_OIDC_ISSUER');
  const unauthenticated = await playwright.request.newContext({ storageState: { cookies: [], origins: [] } });
  let machine: APIRequestContext | undefined;
  try {
    const { clientId, clientSecret } = deployedApiClient(requireServiceContainer(SERVER_CONTAINER_HANDLE));
    const discoveryResponse = await unauthenticated.get(`${issuer}/.well-known/openid-configuration`);
    expect(discoveryResponse.ok()).toBe(true);
    const discovery = await json(discoveryResponse);
    const token = await issueToken(
      unauthenticated, String(discovery.token_endpoint), clientId, clientSecret, 'astrabox:read astrabox:write',
    );
    machine = await playwright.request.newContext({
      baseURL: requiredEnvironment('ASTRABOX_E2E_CONSOLE_URL'),
      storageState: { cookies: [], origins: [] },
      extraHTTPHeaders: { Authorization: `Bearer ${token.value}` },
    });
    const adminResponse = await request.get(apiPath('/user/current'));
    const machineResponse = await machine.get(apiPath('/user/current'));
    expect(adminResponse.status()).toBe(200);
    expect(machineResponse.status()).toBe(200);
    const admin = (await adminResponse.json()).data;
    const executor = (await machineResponse.json()).data;
    expect(admin.is_admin).toBe(true);
    expect(executor.is_admin).toBe(false);
    expect(executor.user_id).toBeTruthy();
    expect(executor.user_id).not.toBe(admin.user_id);

    const id = randomUUID();
    agentId = (await api.createColdTestAgent(`__e2e_channel_execution_${id}`)).agent_id;
    expect((await api.getAgentAccess(agentId)).visibility).toBe('private');
    const deniedStart = await machine.post(apiPath(`/agents/${agentId}/conversations`), { data: {} });
    expect(deniedStart.status()).toBe(403);
    expect((await deniedStart.json()).code).toBe('TEMPLATE_NOT_ALLOWED');

    const deployment = await platform.createDeployment(agentId, { scene: 'channel:generic_json' });
    deploymentId = deployment.deployment_id;
    expect(deployment.secret).toBeTruthy();
    await page.goto(appPath(`/manage/deployments/${deploymentId}`));
    const account = page.getByRole('textbox', { name: 'Execution account user ID', exact: true });
    await account.fill(executor.user_id);
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect.poll(async () => (await platform.listDeployments(agentId))
      .find((row) => row.deployment_id === deploymentId)?.execution_user_id).toBe(executor.user_id);
    await page.reload();
    await expect(account).toHaveValue(executor.user_id);

    const deniedChange = await machine.put(apiPath(`/admin/agents/${agentId}/deployments/${deploymentId}`), {
      data: { execution_user_id: '' },
    });
    expect(deniedChange.status()).toBe(403);
    expect((await deniedChange.json()).code).toBe('API_TOKEN_SCOPE_INSUFFICIENT');
    expect((await platform.listDeployments(agentId))
      .find((row) => row.deployment_id === deploymentId)?.execution_user_id).toBe(executor.user_id);

    async function send(text: string, messageId: string) {
      const response = await request.post(apiPath(`/deployments/${deploymentId}/trigger`), {
        headers: { 'x-channel-secret': String(deployment.secret) },
        data: { text, message_id: messageId, conversation_id: id, reply: { callback_url: recipient.url } },
      });
      expect(response.ok(), await response.text()).toBe(true);
      return (await response.json()).data;
    }
    async function delivered(count: number, marker: string) {
      await expect.poll(() => {
        expect(recipient.errors).toEqual([]);
        return recipient.deliveries.map((row) => row.text);
      }, { timeout: 65_000 }).toHaveLength(count);
      expect(recipient.deliveries[count - 1].text).toContain(marker);
    }
    const secret = `REMEMBER_${randomUUID()}`;
    const firstPrompt = `Remember this token in this conversation: ${secret}. Reply with that token only. Do not use tools.`;
    const firstId = randomUUID();
    const first = await send(firstPrompt, firstId);
    expect(first.status).toBe('accepted');
    sessions.push(first.session_id);
    await delivered(1, secret);
    expect(documentsByField('sessions', '$.session_id', first.session_id)[0]?.user_id).toBe(executor.user_id);
    expect((await machine.get(apiPath(`/sessions/${first.session_id}`))).status()).toBe(200);

    await platform.updateDeployment(agentId, deploymentId, { execution_user_id: '' });
    const duplicate = await send(firstPrompt, firstId);
    expect(duplicate.status).toBe('duplicate');
    expect(duplicate.session_id).toBe(first.session_id);
    const defaultMarker = `DEFAULT_${randomUUID()}`;
    const defaultPrompt = `Reply only ${defaultMarker}. Do not use tools.`;
    const original = await send(defaultPrompt, randomUUID());
    expect(original.status).toBe('accepted');
    expect(original.session_id).not.toBe(first.session_id);
    sessions.push(original.session_id);
    await delivered(2, defaultMarker);
    expect(documentsByField('sessions', '$.session_id', original.session_id)[0]?.user_id).toBe(admin.user_id);
    expect([403, 404]).toContain((await machine.get(apiPath(`/sessions/${original.session_id}`))).status());

    await platform.updateDeployment(agentId, deploymentId, { execution_user_id: executor.user_id });
    const recallPrompt = 'Reply only with the token I asked you to remember earlier. Do not use tools.';
    const resumed = await send(recallPrompt, randomUUID());
    expect(resumed.status).toBe('accepted');
    expect(resumed.session_id).toBe(first.session_id);
    await delivered(3, secret);
    const delegatedMessages = await new AstraApi(machine).getMessages(first.session_id);
    const defaultMessages = await api.getMessages(original.session_id);
    expect(delegatedMessages.messages.filter((row) => row.role === 'user').map(messageText))
      .toEqual([firstPrompt, recallPrompt]);
    expect(defaultMessages.messages.filter((row) => row.role === 'user').map(messageText)).toEqual([defaultPrompt]);
    expect(JSON.stringify(defaultMessages)).not.toContain(secret);
    expect(recipient.deliveries).toHaveLength(3);
  } finally {
    await machine?.dispose();
    await unauthenticated.dispose();
    await recipient.close();
  }
});
