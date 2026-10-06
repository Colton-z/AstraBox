/** A gateway alias uses DSH's generic model route and produces a retained reply. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { engineProfileFor } from '../fixtures/engineProfile';
import { nativeRows } from '../fixtures/nativeChildLifecycle';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('DSH answers through a configured gateway alias outside its native catalog', async ({ request, page }) => {
  const api = new AstraApi(request);
  const profile = engineProfileFor('deepseek_harness');
  // This configured alias uses Chat Completions explicitly. Its prefixed
  // name is outside DSH's native catalog. The separate anthropic/ alias is for
  // Messages clients; it cannot establish generic Chat Completions routing.
  const model = 'openai-compatible/deepseek-flash';
  expect(model).not.toBe(profile.model);
  expect(await api.listEnvironmentModels(profile.environment_name)).toContain(model);
  const marker = `GATEWAY_REPLY_${randomUUID()}`;
  agentId = (await api.createAgent({
    name: `__e2e_dsh_gateway_${randomUUID()}`,
    environment_name: profile.environment_name,
    model,
    prewarm_enabled: false,
  })).agent_id;
  const sessionId = (await api.startConversation(agentId)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  await expect.poll(() => nativeRows(sessionId)
    .map((row) => JSON.parse(String(row.entry_json)))
    .filter((row) => row.type === 'model/selection')
    .map((row) => ({ model: row.data.model, provider: row.data.provider })),
  ).toContainEqual({ model, provider: 'astrabox-gateway' });

  await openSessionView(page, sessionId);
  const before = await api.assistantCount(sessionId);
  await sendPrompt(page, sessionId, `Reply exactly ${marker}. Do not use tools.`);
  const settled = await api.waitForSession(sessionId, (session) =>
    session.last_turn_status === 'COMPLETED' || session.last_turn_status === 'FAILED');
  expect(settled.last_turn_status, String(settled.last_turn_error ?? '')).toBe('COMPLETED');
  await api.waitForAssistantMessageMatching(sessionId, before, (message) => messageText(message).includes(marker));
  await api.waitForSessionReady(sessionId);
  await expect(page.getByTestId('assistant-message').last()).toContainText(marker);
  await page.reload();
  await expect(page.getByTestId('assistant-message').last()).toContainText(marker);
  await expect(page.getByTestId('composer-prompt')).toBeEnabled();
});
