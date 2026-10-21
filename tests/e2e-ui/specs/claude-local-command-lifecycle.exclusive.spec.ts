/** Native local command output and custody survive the following turns and a host restart. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { nativeTranscriptRows, sessionInputRows, sessionResponseRows } from '../fixtures/dbOracle';
import { absoluteBaseUrl } from '../fixtures/env';
import { restartServerContainer } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, startPromptDelivery } from '../fixtures/sessionPage';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

function nativeEntries(sessionId: string): Array<Record<string, unknown>> {
  return nativeTranscriptRows(sessionId).filter((row) => row.subpath === null)
    .map((row) => JSON.parse(row.entry_json) as Record<string, unknown>);
}

test('native local output closes input custody and survives following turns and reader takeover', async ({ page, request }, info) => {
  const api = new AstraApi(request);
  const marker = randomUUID().replace(/-/g, '');
  // Unknown names can be ordinary model prompts; use native local commands.
  const commands = ['/model', `/rename local-${marker}`];
  const agent = await api.createColdTestAgent(`__e2e_local_command_${marker}`);
  agentId = agent.agent_id;
  const sessionId = (await api.startConversation(agentId)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId, 90_000);
  await openSessionView(page, sessionId);

  const localIds: string[] = [];
  const localResponses: string[] = [];
  for (const command of commands) {
    const delivery = await startPromptDelivery(page, sessionId, command);
    const response = await delivery.response;
    expect(response.status()).toBe(200);
    await expect.poll(() => sessionInputRows(sessionId).find((row) => row.client_message_id === delivery.clientMessageId)?.state,
      { timeout: 30_000 }).toBe('admitted');
    const input = sessionInputRows(sessionId).find((row) => row.client_message_id === delivery.clientMessageId)!;
    localIds.push(input.input_id);
    expect(input.admitted_at).not.toBeNull();
    await expect.poll(() => sessionResponseRows(sessionId).find((row) => row.response_id === input.response_id)?.outcome,
      { timeout: 30_000 }).toBe('completed');
    expect(input.response_id).toBeTruthy();
    localResponses.push(input.response_id!);
    expect(input.failure).toBeNull();
  }

  // Compare the rendered result with the supplier's own stored output, rather
  // than treating our Result adapter's fields as the expected business output.
  await expect.poll(() => nativeEntries(sessionId).filter((row) =>
    row.type === 'system' && row.subtype === 'local_command'
    && String(row.content || '').includes('local-command-stdout')).length,
  { timeout: 30_000 }).toBe(2);
  const native = nativeEntries(sessionId).filter((row) => row.type === 'system' && row.subtype === 'local_command');
  const outcomes = native.filter((row) => String(row.content || '').includes('local-command-stdout')).map((row) =>
    String(row.content).replace(/^<local-command-stdout>/, '').replace(/<\/local-command-stdout>$/, '').trim());
  expect(outcomes[0]).toContain(commands[0]!.slice(1));
  expect(outcomes[1]).toContain(`local-${marker}`);
  for (const [index, text] of outcomes.entries()) {
    await expect(page.getByTestId('assistant-text').filter({ hasText: text.replaceAll('`', '') })).toHaveCount(1);
    const card = page.getByTestId('raw-event-card').filter({ hasText: String(native[index]!.uuid) })
      .filter({ has: page.locator('summary').filter({ hasText: 'claude_code.sdk / assistant' }) });
    await expect(card).toHaveCount(1);
    await card.locator('summary').click();
    await expect(card.locator('pre')).toBeVisible();
    const raw = JSON.parse((await card.locator('pre').textContent())!) as Record<string, unknown>;
    expect(raw.uuid).toBe(native[index]!.uuid);
    expect(JSON.stringify(raw.content)).toContain(text.split('\n')[0]!);
  }
  await expect.poll(async () => (await api.getSession(sessionId)).held_inputs?.length ?? 0).toBe(0);
  await expect(page.getByTestId('user-message').getByRole('status').filter({ hasText: /^(Queued|等待发送)$/ })).toHaveCount(0);
  const localRows = sessionResponseRows(sessionId).filter((row) => row.input_ids.some((id) => localIds.includes(id)));
  expect(localRows).toHaveLength(commands.length);
  for (const row of localRows) {
    expect(row.input_ids).toHaveLength(1);
    expect(row.usage?.input_tokens).toBe(0);
    expect(row.usage?.output_tokens).toBe(0);
  }

  const firstPrompt = `What is two plus two? This request is labelled first-${marker}. Reply briefly.`;
  const first = await api.sendTurn(sessionId, firstPrompt, 90_000);
  expect(first.errorText).toBeNull();
  expect(first.text.trim()).not.toBe('');
  const firstRows = sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(first.inputId));
  expect(firstRows).toHaveLength(1);
  expect(firstRows[0]!.input_ids).toEqual([first.inputId]);
  expect(firstRows[0]!.outcome).toBe('completed');

  const nativeIds = native.map((row) => row.uuid);
  await restartServerContainer(absoluteBaseUrl());
  await openSessionView(page, sessionId);
  for (const [index, text] of outcomes.entries()) {
    await expect(page.getByTestId('assistant-text').filter({ hasText: text.replaceAll('`', '') })).toHaveCount(1);
    await expect(page.getByTestId('raw-event-card').filter({ hasText: String(native[index]!.uuid) })
      .filter({ has: page.locator('summary').filter({ hasText: 'claude_code.sdk / assistant' }) })).toHaveCount(1);
  }
  expect(nativeEntries(sessionId).filter((row) => row.type === 'system' && row.subtype === 'local_command')
    .map((row) => row.uuid)).toEqual(nativeIds);
  expect(sessionInputRows(sessionId).filter((row) => localIds.includes(row.input_id))
    .map((row) => ({ state: row.state, admitted: row.admitted_at, response: row.response_id })))
    .toEqual(localResponses.map((response) => ({ state: 'admitted', admitted: expect.any(String), response })));

  const nextPrompt = `What is three plus three? This request is labelled next-${marker}. Reply briefly.`;
  const next = await api.sendTurn(sessionId, nextPrompt, 90_000);
  expect(next.errorText).toBeNull();
  expect(next.text.trim()).not.toBe('');
  const nextRows = sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(next.inputId));
  expect(nextRows).toHaveLength(1);
  expect(nextRows[0]!.input_ids).toEqual([next.inputId]);
  expect(nextRows[0]!.response_id).not.toBe(firstRows[0]!.response_id);
  expect(nextRows[0]!.outcome).toBe('completed');
  const userTexts = nativeEntries(sessionId).filter((row) => row.type === 'user')
    .map((row) => (row.message as { content?: unknown })?.content);
  expect(userTexts.filter((text) => text === firstPrompt)).toHaveLength(1);
  expect(userTexts.filter((text) => text === nextPrompt)).toHaveLength(1);
  expect(userTexts.indexOf(firstPrompt)).toBeLessThan(userTexts.indexOf(nextPrompt));
  await info.attach('native-local-command-custody', { body: JSON.stringify({
    native: nativeEntries(sessionId), inputs: sessionInputRows(sessionId), responses: sessionResponseRows(sessionId),
  }), contentType: 'application/json' });
  await page.screenshot({ path: info.outputPath('native-local-command-history.png'), fullPage: true });
});
