/** Official Satori references cannot cross a channel's execution-account boundary. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField } from '../fixtures/dbOracle';
import { call, deployedApiClient, issueToken, json, requiredEnvironment } from '../fixtures/oidcMachine';
import { PlatformApi } from '../fixtures/platformApi';
import { satoriSource } from '../fixtures/satoriSource';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('Satori acknowledges a cross-account reply refusal and keeps ordinary context and later replies working', async ({
  request, playwright,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const machine = await playwright.request.newContext({
    baseURL: requiredEnvironment('ASTRABOX_E2E_CONSOLE_URL'),
    storageState: { cookies: [], origins: [] },
  });
  let source: Awaited<ReturnType<typeof satoriSource>> | undefined;
  try {
    const discoveryResponse = await machine.get(`${requiredEnvironment('ASTRABOX_E2E_OIDC_ISSUER')}/.well-known/openid-configuration`);
    expect(discoveryResponse.ok()).toBe(true);
    const discovery = await json(discoveryResponse);
    const { clientId, clientSecret } = deployedApiClient(requireServiceContainer(SERVER_CONTAINER_HANDLE));
    const token = await issueToken(machine, String(discovery.token_endpoint), clientId, clientSecret, 'astrabox:read');
    const identity = await call(machine, token.value, 'GET', '/user/current');
    expect(identity.status).toBe(200);
    const executor = identity.payload.data as { user_id: string; is_admin: boolean };
    expect(executor.is_admin).toBe(false);
    expect(executor.user_id).toBeTruthy();
    source = await satoriSource();
    const id = randomUUID();
    const firstMarker = `FIRST_${id}`;
    const secondMarker = `SECOND_${id}`;
    const rejectedMarker = `REJECTED_${id}`;
    const contextMarker = `CONTEXT_${id}`;
    agentId = (await api.createColdTestAgent(`__e2e_channel_reference_${id}`)).agent_id;
    deploymentId = (await platform.createDeployment(agentId, {
      scene: 'channel:satori', attention_policy: 'mentions',
      channel_config: { endpoint: source.endpoint }, credentials: { token: source.token },
    })).deployment_id;
    await source.waitConnected();

    const alias = (messageId: string) => `e2e:${source!.botId}:message:${messageId}`;
    const inbound = (messageId: string) => documentsByField('channel_inbound', '$.deployment_id', deploymentId)
      .find((row) => row.dedup_key === alias(messageId));
    const sourceCursor = () => Number(documentsByField('deployments', '$.deployment_id', deploymentId)[0]?.source_cursor || 0);
    function send(messageId: string, content: string) {
      const timestamp = Date.now();
      source!.send({ type: 'message-created', timestamp,
        channel: { id, type: 0 }, user: { id: 'participant', name: 'Participant' },
        message: { id: messageId, content, created_at: timestamp } });
    }
    async function settled(messageId: string, deliveries: number, marker: string) {
      await expect.poll(() => {
        source!.healthy();
        const row = inbound(messageId);
        if (row?.session_id && !sessions.includes(String(row.session_id))) sessions.push(String(row.session_id));
        return row?.state;
      }, { timeout: 65_000 }).toBe('SETTLED');
      await expect.poll(() => { source!.healthy(); return source!.deliveries.length; }).toBe(deliveries);
      expect(source!.deliveries[deliveries - 1].content).toContain(marker);
      return String(inbound(messageId)!.session_id);
    }
    send('first', `<at id="${source.botId}"/>Remember ${firstMarker} for this conversation and reply with that token only. Do not use tools.`);
    const original = await settled('first', 1, firstMarker);
    const originalOwner = documentsByField('sessions', '$.session_id', original)[0]?.user_id;
    expect(originalOwner).toBeTruthy();
    expect(originalOwner).not.toBe(executor.user_id);
    await expect.poll(() => documentsByField('channel_aliases', '$.deployment_id', deploymentId)
      .find((row) => row.alias === alias('reply-1'))?.session_id).toBe(original);

    await platform.updateDeployment(agentId, deploymentId, { execution_user_id: executor.user_id });
    const acceptedCursor = sourceCursor();
    expect(acceptedCursor).toBeGreaterThan(0);
    send('forbidden', `<quote id="reply-1">${firstMarker}</quote><at id="${source.botId}"/>${rejectedMarker}: continue that old reply.`);
    await expect.poll(() => { source!.healthy(); return inbound('forbidden')?.state; }).toBe('IGNORED');
    expect(inbound('forbidden')).toMatchObject({
      ignored_reason: 'CHANNEL_REFERENCE_OWNER_MISMATCH', context_excluded: true,
      session_id: null, command_id: null, turn_id: null,
    });
    // Management responses omit the internal cursor; its durable advance owns ACK safety.
    await expect.poll(sourceCursor).toBeGreaterThan(acceptedCursor);
    expect(source.deliveries).toHaveLength(1);
    expect(source.connectionCount).toBe(1);

    send('ordinary', contextMarker);
    await expect.poll(() => inbound('ordinary')?.state).toBe('IGNORED');
    send('second', `<at id="${source.botId}"/>Reply only ${secondMarker}. Do not use tools.`);
    const delegated = await settled('second', 2, secondMarker);
    expect(delegated).not.toBe(original);
    expect(documentsByField('sessions', '$.session_id', delegated)[0]?.user_id).toBe(executor.user_id);
    const frozen = inbound('second')!.frozen_input as { content: string; context: Array<{ message_id: string }> };
    expect(frozen.context.map((row) => row.message_id)).toEqual([alias('ordinary')]);
    expect(frozen.content).toContain(contextMarker);
    expect(frozen.content).not.toContain(rejectedMarker);
    expect(frozen.content).not.toContain(firstMarker);
    const ownMessages = await call(machine, token.value, 'GET', `/sessions/${delegated}/messages`);
    expect(ownMessages.status).toBe(200);
    expect(JSON.stringify(ownMessages.payload)).not.toContain(firstMarker);
    expect(JSON.stringify(ownMessages.payload)).not.toContain(rejectedMarker);

    await platform.updateDeployment(agentId, deploymentId, { execution_user_id: '' });
    send('same-owner', `<quote id="reply-1"/><at id="${source.botId}"/>Reply only with the token I asked you to remember earlier. Do not use tools.`);
    expect(await settled('same-owner', 3, firstMarker)).toBe(original);
    expect(source.connectionCount).toBe(1);
    expect(inbound('forbidden')?.state).toBe('IGNORED');
    expect(inbound('forbidden')?.context_submitted).not.toBe(true);
    expect(source.deliveries).toHaveLength(3);
  } finally {
    await source?.close();
    await machine.dispose();
  }
});
