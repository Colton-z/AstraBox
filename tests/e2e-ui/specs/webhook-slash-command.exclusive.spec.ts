import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText, type MessageRecord } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { PlatformApi } from '../fixtures/platformApi';
import { apiPath, parseTimeoutEnv } from '../fixtures/env';
import { openSessionView } from '../fixtures/sessionPage';

const INPUT_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_WEBHOOK_INPUT_TIMEOUT_MS', 180_000);
const SLASH_COMMAND = '/compact';

async function waitForExactUserInput(
  api: AstraApi,
  sessionId: string,
  expected = SLASH_COMMAND,
): Promise<MessageRecord> {
  const deadline = Date.now() + INPUT_TIMEOUT_MS;
  let last: MessageRecord[] = [];
  while (Date.now() < deadline) {
    last = (await api.getMessages(sessionId, 50)).messages || [];
    const hit = last.find((message) => (
      message.role === 'user' && messageText(message) === expected
    ));
    if (hit) return hit;
    await new Promise((resolve) => setTimeout(resolve, 1_500));
  }
  throw new Error(
    `webhook session ${sessionId} did not project ${expected}; `
      + `last=${JSON.stringify(last.map((message) => ({ role: message.role, text: messageText(message) })))}`,
  );
}

// Teardown that changes state runs on the passing path only, and hook
// registration order is the old `finally` order: afterEach hooks run in the
// order they are registered. The deployment binding is released
// before the agent that owns it, as it was.
let agentId = '';
let deploymentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (deploymentId && agentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
  deploymentId = '';
  agentId = '';
});

async function triggerText(
  request: APIRequestContext,
  scene: 'scheduler' | 'hmac',
  secret: string,
  content: string,
): Promise<string> {
  let accepted: { session_id?: string; status?: string } | undefined;
  if (scene === 'hmac') {
    accepted = await new PlatformApi(request).triggerHmacAccepted(
      deploymentId, secret, Buffer.from(content, 'utf-8'),
    );
  } else {
    const response = await request.fetch(apiPath(`/deployments/${deploymentId}/trigger`), {
      method: 'POST',
      data: content,
      headers: {
        'content-type': 'text/plain; charset=utf-8',
        'x-webhook-secret': secret,
      },
      timeout: INPUT_TIMEOUT_MS,
    });
    const raw = await response.text();
    expect(response.ok(), raw).toBe(true);
    accepted = (JSON.parse(raw) as { data?: { session_id?: string; status?: string } }).data;
  }
  expect(accepted?.status).toBe('accepted');
  const sessionId = String(accepted?.session_id || '').trim();
  if (sessionId) sessions.push(sessionId);
  expect(sessionId).not.toEqual('');
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  return sessionId;
}

for (const scene of ['scheduler', 'hmac'] as const) {
  test(`${scene} webhook preserves and executes a standalone slash command`, async ({ request, page }) => {
    const api = new AstraApi(request);
    const platform = new PlatformApi(request);
    let sessionId = '';

    try {
      const base = await api.defaultAgent();
      const environmentName = String(base.environment_name || '').trim();
      expect(environmentName).not.toEqual('');
      let model = String(base.model || '').trim();
      if (!model || model.includes('*')) {
        const models = await api.listEnvironmentModels(environmentName);
        model = models.find((item) => item && !item.includes('*')) || 'deepseek-chat';
      }
      const agent = await api.createAgent({
        name: `__e2e_webhook_slash_${Date.now()}`,
        model,
        environment_name: environmentName,
      });
      agentId = agent.agent_id;

      const deployment = await platform.createDeployment(agentId, {
        scene,
        prompt_prefix: '',
      });
      deploymentId = String(deployment.deployment_id || '');
      const secret = String(deployment.secret || '');
      expect(deploymentId).not.toEqual('');
      expect(secret).not.toEqual('');

      sessionId = await triggerText(request, scene, secret, SLASH_COMMAND);

      await waitForExactUserInput(api, sessionId);
      const ready = await api.waitForSession(sessionId, (session) => (
        session.state === 'READY'
        && !session.current_turn_id
        && String(session.last_turn_status || '') === 'COMPLETED'
      ), INPUT_TIMEOUT_MS);
      const commandNames = (Array.isArray(ready.slash_commands) ? ready.slash_commands : [])
        .map((item) => {
          if (typeof item === 'string') return item;
          if (!item || typeof item !== 'object') return '';
          const command = item as Record<string, unknown>;
          return String(command.name || command.command || '');
        })
        .map((name) => name.replace(/^\/+/, '').trim())
        .filter(Boolean);
      expect(commandNames, 'the completed webhook session must expose the command it executed')
        .toContain(SLASH_COMMAND.replace(/^\/+/, ''));

      await expect.poll(
        () => api.adminSessionTranscript(sessionId),
        {
          timeout: INPUT_TIMEOUT_MS,
          intervals: [500, 1_000, 1_500],
          message: 'the official Claude Code transcript must retain command-envelope truth',
        },
      ).toContain(`<command-name>${SLASH_COMMAND}</command-name>`);

      const history = await api.getMessages(sessionId, 50);
      const userMessages = history.messages.filter((message) => message.role === 'user');
      expect(userMessages).toHaveLength(1);
      expect(messageText(userMessages[0]).trim()).toBe(SLASH_COMMAND);

      await openSessionView(page, sessionId);
      const bubble = page.getByTestId('user-message').filter({ hasText: SLASH_COMMAND });
      await expect(bubble).toHaveCount(1, { timeout: 60_000 });
      await expect(bubble).toHaveText(SLASH_COMMAND);
      await expect(bubble).not.toContainText(/Received a webhook event|webhook payload/i);
    } finally {
      // Nothing here is released on a failing run. `trackSessions()` and
      // `onPassOnly()` decide in afterEach hooks, where the test's real status is
      // known — see that fixture on why the unit is the whole block.
    }
  });
}

for (const prefix of ['', 'Reply in one short sentence. Do not use tools.']) {
  test(`scheduler webhook forwards ordinary text ${prefix ? 'with only the configured prefix' : 'without a platform envelope'}`, async ({ request, page }) => {
    const api = new AstraApi(request);
    const platform = new PlatformApi(request);
    const agent = await api.createColdTestAgent(`e2e-webhook-text-${Date.now()}`);
    agentId = agent.agent_id;
    const deployment = await platform.createDeployment(agentId, {
      scene: 'scheduler',
      prompt_prefix: prefix,
    });
    deploymentId = String(deployment.deployment_id || '');
    const secret = String(deployment.secret || '');
    expect(deploymentId).not.toEqual('');
    expect(secret).not.toEqual('');
    const content = `用一句话解释什么是会话。不要使用工具。\nReference: ${Date.now()}`;
    const expected = prefix ? `${prefix}\n\n${content}` : content;
    const sessionId = await triggerText(request, 'scheduler', secret, content);
    await waitForExactUserInput(api, sessionId, expected);
    const answer = await api.waitForAssistantMessageMatching(
      sessionId, 0, (message) => messageText(message).trim().length > 0, 60_000,
    );
    expect(messageText(answer).trim()).not.toEqual('');
    await api.waitForSession(sessionId, (session) => (
      session.state === 'READY'
      && !session.current_turn_id
      && session.last_turn_status === 'COMPLETED'
    ), 60_000);
    const history = await api.getMessages(sessionId, 50);
    expect(history.messages.filter((message) => message.role === 'user').map(messageText))
      .toEqual([expected]);
    await openSessionView(page, sessionId);
    await expect(page.getByTestId('user-message')).toHaveCount(1);
    await expect(page.getByTestId('user-message')).toHaveText(expected);
  });
}
