/** A gateway alias uses DSH's generic model route and produces a retained reply. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { sessionInputRows } from '../fixtures/dbOracle';
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
  expect(await api.listEnvironmentModels(profile.runtime_profile_name)).toContain(model);
  const marker = `GATEWAY_REPLY_${randomUUID()}`;
  agentId = (await api.createAgent({
    name: `__e2e_dsh_gateway_${randomUUID()}`,
    runtime_profile_name: profile.runtime_profile_name,
    model,
    prepared_capacity: 0,
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
  const delivery = await sendPrompt(page, sessionId, `Reply exactly ${marker}. Do not use tools.`);
  const clientMessageId = String(delivery.request().postDataJSON().client_message_id || '');
  const inputs = sessionInputRows(sessionId).filter((row) => row.client_message_id === clientMessageId);
  expect(inputs, 'the page message is one accepted input').toHaveLength(1);
  // A reply that does not complete still ends; the assertion message carries
  // the error its response recorded.
  const reply = await api.waitForReplyTo(sessionId, inputs[0]!.input_id);
  expect(reply.metadata?.outcome, JSON.stringify(reply.metadata?.error ?? null)).toBe('completed');
  expect(messageText(reply)).toContain(marker);
  await api.waitForSessionReady(sessionId);
  await expect(page.getByTestId('assistant-message').last()).toContainText(marker);
  await page.reload();
  await expect(page.getByTestId('assistant-message').last()).toContainText(marker);
  await expect(page.getByTestId('composer-prompt')).toBeEnabled();
});
