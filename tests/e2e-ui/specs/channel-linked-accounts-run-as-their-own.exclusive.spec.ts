/**
 * A Satori trigger that runs as linked accounts runs each participant's
 * messages as the AstraBox account an administrator linked to that
 * participant. Two participants' requests in one room become Sessions owned
 * by their own accounts. An unlinked participant, an account the Agent does
 * not admit, and a reply into another account's conversation are refused
 * with a notice under the refused message, and none is answered as anyone
 * else.
 */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField } from '../fixtures/dbOracle';
import { apiPath, appPath } from '../fixtures/env';
import { channelCallback } from '../fixtures/channelCallback';
import { call, deployedApiClient, issueToken, json, requiredEnvironment } from '../fixtures/oidcMachine';
import { PlatformApi } from '../fixtures/platformApi';
import { satoriSource } from '../fixtures/satoriSource';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
let genericDeploymentId = '';
onPassOnly(async ({ request }) => {
  if (genericDeploymentId) await new PlatformApi(request).deleteDeployment(agentId, genericDeploymentId);
  if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('linked participants run as their own accounts, and anyone else is refused with a notice', async ({
  request, playwright, page,
}, info) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const machine = await playwright.request.newContext({
    baseURL: requiredEnvironment('ASTRABOX_E2E_CONSOLE_URL'),
    storageState: { cookies: [], origins: [] },
  });
  const recipient = await channelCallback();
  let source: Awaited<ReturnType<typeof satoriSource>> | undefined;
  try {
    const discovery = await json(
      await machine.get(`${requiredEnvironment('ASTRABOX_E2E_OIDC_ISSUER')}/.well-known/openid-configuration`),
    );
    const { clientId, clientSecret } = deployedApiClient(requireServiceContainer(SERVER_CONTAINER_HANDLE));
    const token = await issueToken(
      machine, String(discovery.token_endpoint), clientId, clientSecret, 'astrabox:read astrabox:write',
    );
    const memberAnswer = await call(machine, token.value, 'GET', '/user/current');
    expect(memberAnswer.status).toBe(200);
    const member = memberAnswer.payload.data as { user_id: string; is_admin: boolean };
    const adminResponse = await request.get(apiPath('/user/current'));
    const admin = (await adminResponse.json()).data as { user_id: string; is_admin: boolean };
    expect([admin.is_admin, member.is_admin]).toEqual([true, false]);
    expect(member.user_id).not.toBe(admin.user_id);

    const id = randomUUID();
    const room = `room-${id}`;
    source = await satoriSource();
    const live = source;
    agentId = (await api.createColdTestAgent(`__e2e_channel_linked_${id}`)).agent_id;
    // The Agent admits its creator, the administrator, and the member; nobody else.
    await api.setAgentAccess(agentId, {
      visibility: 'allowlist', admins: [], allowed_user_ids: [member.user_id],
    });
    // Generic JSON identities are the raw sender, with no platform prefix.
    await page.goto(appPath('/manage/deployments/new'));
    await page.getByLabel('Trigger', { exact: true }).selectOption('channel:generic_json');
    await expect(page.getByText(/the request's sender value, exactly as sent/)).toBeVisible();
    await expect(page.getByLabel('Allowed senders', { exact: true })).toHaveAttribute('placeholder', 'user123');
    await page.getByLabel('Trigger', { exact: true }).selectOption('channel:satori');
    await expect(page.getByText(/platform:user-id, as Satori names the platform and the user/)).toBeVisible();
    const generic = await platform.createDeployment(agentId, {
      scene: 'channel:generic_json', prompt_prefix: '', execution_mode: 'linked',
      allowed_participants: ['user123'],
    });
    genericDeploymentId = generic.deployment_id;
    await page.goto(appPath(`/manage/deployments/${genericDeploymentId}`));
    await expect(page.getByText(/the request's sender value, exactly as sent/)).toHaveCount(2);
    await page.getByLabel('Sender', { exact: true }).fill('user123');
    await page.getByLabel('AstraBox user ID', { exact: true }).fill(admin.user_id);
    await page.getByRole('button', { name: 'Link', exact: true }).click();
    await expect(page.getByRole('cell', { name: 'user123', exact: true })).toBeVisible();
    const marker = `RAW_${id.slice(0, 8)}`;
    for (const sender of ['generic_json:user123', 'other-user', 'user123']) {
      const response = await request.post(apiPath(`/deployments/${genericDeploymentId}/trigger`), {
        headers: { 'x-channel-secret': String(generic.secret) },
        data: { sender, conversation_id: id, message_id: `${id}-${sender}`,
          text: `Reply with exactly ${marker}. Do not use tools.`,
          reply: { callback_url: recipient.url } },
      });
      expect(response.ok(), await response.text()).toBe(true);
      const receipt = (await response.json()).data;
      expect(receipt.status).toBe(sender === 'user123' ? 'accepted' : 'ignored');
      if (sender === 'user123') sessions.push(String(receipt.session_id));
      else expect(receipt.session_id ?? '').toBe('');
    }
    await expect.poll(() => recipient.deliveries.map((item) => item.text), { timeout: 60_000 })
      .toContainEqual(expect.stringContaining(marker));
    expect(recipient.errors).toEqual([]);
    deploymentId = (await platform.createDeployment(agentId, {
      scene: 'channel:satori', prompt_prefix: '', attention_policy: 'mentions',
      execution_mode: 'linked',
      channel_config: { endpoint: live.endpoint }, credentials: { token: live.token },
    })).deployment_id;
    await live.waitConnected();
    await page.goto(appPath(`/manage/deployments/${deploymentId}`));
    await expect(page.getByText(/platform:user-id, as Satori names the platform and the user/)).toHaveCount(2);

    const linksRoute = `/admin/agents/${agentId}/deployments/${deploymentId}/account-links`;
    const denied = await call(machine, token.value, 'PUT', linksRoute, {
      participant: 'e2e:p-member', user_id: member.user_id,
    });
    expect(denied.status, 'only an administrator links accounts').toBe(403);
    await platform.linkAccount(agentId, deploymentId, 'e2e:p-member', member.user_id);
    await platform.linkAccount(agentId, deploymentId, 'e2e:p-admin', admin.user_id);
    await platform.linkAccount(agentId, deploymentId, 'e2e:p-outsider', `outsider-${id}`);

    const mention = `<at id="${live.botId}"/>`;
    const alias = (messageId: string) => `e2e:${live.botId}:channel:${room}:message:${messageId}`;
    let clock = Date.now();
    function say(messageId: string, user: string, content: string) {
      const timestamp = clock += 1000;
      live.send({ type: 'message-created', timestamp,
        channel: { id: room, type: 0 }, user: { id: user, name: user },
        message: { id: messageId, content, created_at: timestamp } });
    }
    const row = (messageId: string) => documentsByField('channel_inbound', '$.deployment_id', deploymentId)
      .find((item) => item.dedup_key === alias(messageId));
    const repliesTo = (messageId: string) => live.deliveries
      .filter((delivery) => delivery.content.includes(`<quote id="${messageId}"`))
      .map((delivery) => delivery.content);
    async function settled(messageId: string): Promise<string> {
      await expect.poll(() => { live.healthy(); return row(messageId)?.state; }, { timeout: 90_000 })
        .toBe('SETTLED');
      const sessionId = String(row(messageId)!.session_id);
      if (!sessions.includes(sessionId)) sessions.push(sessionId);
      return sessionId;
    }
    async function refused(messageId: string, reason: string, notice: string) {
      await expect.poll(() => { live.healthy(); return row(messageId)?.ignored_reason; }, { timeout: 60_000 })
        .toBe(reason);
      expect(row(messageId)).toMatchObject({ state: 'IGNORED', context_excluded: true, session_id: null });
      await expect.poll(() => repliesTo(messageId)).toEqual([expect.stringContaining(notice)]);
    }

    const memberToken = `MEMBER_${id.slice(0, 8)}`;
    const memberPrompt = `${mention}Remember the token ${memberToken}. Reply with only that token. Do not use tools.`;
    say('m-member', 'p-member', memberPrompt);
    say('m-admin', 'p-admin', `${mention}Reply with only the word READY. Do not use tools.`);
    const [memberSession, adminSession] = await Promise.all([settled('m-member'), settled('m-admin')]);
    expect(memberSession).not.toBe(adminSession);
    expect(documentsByField('sessions', '$.session_id', memberSession)[0]?.user_id).toBe(member.user_id);
    expect(documentsByField('sessions', '$.session_id', adminSession)[0]?.user_id).toBe(admin.user_id);
    expect((await call(machine, token.value, 'GET', `/sessions/${memberSession}`)).status).toBe(200);
    expect([403, 404]).toContain((await call(machine, token.value, 'GET', `/sessions/${adminSession}`)).status);
    await expect.poll(() => repliesTo('m-member'), { timeout: 60_000 })
      .toEqual([expect.stringContaining(memberToken)]);

    say('m-stranger', 'p-stranger', `${mention}Reply with only the word HELLO. Do not use tools.`);
    say('m-outsider', 'p-outsider', `${mention}Reply with only the word HELLO. Do not use tools.`);
    await refused('m-stranger', 'CHANNEL_PARTICIPANT_NOT_LINKED', 'not linked');
    await refused('m-outsider', 'CHANNEL_ACCOUNT_NOT_AUTHORIZED', 'cannot use this agent');

    // The administrator's participant replies into the member's conversation:
    // to the agent's answer there, and to the member's own message.
    const answerIndex = live.deliveries.findIndex((delivery) => delivery.content.includes(memberToken));
    const answerId = `reply-${answerIndex + 1}`;
    await expect.poll(() => documentsByField('channel_aliases', '$.deployment_id', deploymentId)
      .find((item) => item.alias === alias(answerId))?.session_id).toBe(memberSession);
    say('m-cross-answer', 'p-admin', `<quote id="${answerId}"/>${mention}Which token was that? Do not use tools.`);
    say('m-cross-message', 'p-admin', `<quote id="m-member"/>${mention}Which token was that? Do not use tools.`);
    await refused('m-cross-answer', 'CHANNEL_REFERENCE_OWNER_MISMATCH', 'cannot continue');
    await refused('m-cross-message', 'CHANNEL_REFERENCE_OWNER_MISMATCH', 'cannot continue');

    // Nothing the refused replies said reached the member's conversation.
    const memberMessages = await call(machine, token.value, 'GET', `/sessions/${memberSession}/messages`);
    expect(memberMessages.status).toBe(200);
    expect(JSON.stringify(memberMessages.payload)).toContain(memberToken);
    expect(JSON.stringify(memberMessages.payload)).not.toContain('Which token was that?');
    expect(documentsByField('sessions', '$.agent_id', agentId).map((item) => String(item.session_id)).sort())
      .toEqual([...sessions].sort());
  } finally {
    await info.attach('channel-linked-accounts-evidence', {
      body: JSON.stringify({
        agentId, deploymentId, deliveries: source?.deliveries, sourceErrors: source?.errors,
        inbound: deploymentId ? documentsByField('channel_inbound', '$.deployment_id', deploymentId) : [],
      }),
      contentType: 'application/json',
    });
    await source?.close();
    await recipient.close();
    await machine.dispose();
  }
});
