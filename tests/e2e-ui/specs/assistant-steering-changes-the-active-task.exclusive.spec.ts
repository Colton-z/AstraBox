/** A busy Assistant consumes a correction in its current run, preserving earlier work. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { framesForTurn, sessionEvents } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

test.use({ locale: 'en-US' });
const sessions = trackSessions();
let assistantId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

test('a correction changes the current Assistant task before its planned next action', async ({ page, request }, info) => {
  const deadline = Date.now() + 155_000;
  const remaining = () => {
    const value = deadline - Date.now();
    expect(value, 'the business journey must fit the unchanged test budget').toBeGreaterThan(0);
    return value;
  };
  const api = new AstraApi(request);
  const marker = `STEER_${randomUUID().replaceAll('-', '')}`;
  assistantId = (await api.createAssistant({
    display_name: `__e2e_${marker}`, environment_name: await api.assistantEnvironmentName(),
    engine_options: { config: { display: { busy_input_mode: 'steer' } } },
  })).assistant_id;
  const sessionId = (await api.startAssistantConversation(assistantId)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId, Math.min(90_000, remaining()));
  const started = `${marker}.started`;
  const release = `${marker}.release`;
  const obsolete = `${marker}.obsolete`;
  const toolOutput = `${marker}_TOOL_OUTPUT`;
  const corrected = `${marker}_CORRECTED`;
  const command = `printf '%s' '${marker}' > '/workspace/${started}'; `
    + `until test -f '/workspace/${release}'; do sleep 0.2; done; printf '%s' '${toolOutput}'`;
  const original = [
    'Perform these steps in order, without delegating or using background processes.',
    `First use terminal exactly once with background=false and timeout=90 to run: ${command}`,
    `When it finishes, use terminal once more to write '${marker}_OBSOLETE' to /workspace/${obsolete}.`,
    `Then reply exactly ${marker}_ORIGINAL_FINAL. Do not create the release file yourself.`,
  ].join('\n');
  await openSessionView(page, sessionId);
  await sendPrompt(page, sessionId, original);
  await expect.poll(async () => {
    const response = await request.get(apiPath(`/sessions/${sessionId}/files/download?path=${encodeURIComponent(`/workspace/${started}`)}`), {
      timeout: Math.min(10_000, remaining()),
    });
    expect([200, 404], 'a ready runtime must answer file presence, not hide an unavailable binding')
      .toContain(response.status());
    return response.status() === 200 ? await response.text() : '';
  }, { timeout: Math.min(60_000, remaining()), intervals: [250, 500] }).toBe(marker);
  const active = await api.getSession(sessionId);
  const turnId = String(active.current_turn_id || '');
  expect(turnId, 'the correction must be submitted during the original active run').not.toBe('');
  const correction = `Change the current task: after the running terminal returns, do not perform the planned second action or create ${obsolete}. Do not call any additional tools. Reply with exactly ${corrected}.`;
  const composer = page.getByTestId('composer-prompt');
  await expect(composer, 'a working Assistant still accepts a correction').toBeEnabled();
  await composer.fill(correction);
  const sent = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === apiPath(`/sessions/${sessionId}/turn-inputs`));
  await page.getByTestId('composer-submit').click();
  const acknowledgement = await sent;
  expect(acknowledgement.ok(), await acknowledgement.text()).toBe(true);
  const correctionId = String(acknowledgement.request().postDataJSON().client_message_id || '');
  expect(correctionId).not.toBe('');
  await expect(page.getByTestId('user-message').filter({ hasText: correction }),
    'Hermes accepts the correction while the original tool is still running').toHaveCount(1, {
    timeout: Math.min(15_000, remaining()),
  });
  await expect(page.getByTestId('composer-queue').filter({ hasText: correction })).toHaveCount(0);
  expect((await api.getSession(sessionId)).current_turn_id).toBe(turnId);
  await api.uploadFileText(sessionId, '/workspace', release, 'release', Math.min(15_000, remaining()));
  await expect(page.getByTestId('assistant-text').filter({ hasText: corrected })).toBeVisible({
    timeout: Math.min(60_000, remaining()),
  });
  await api.waitForSession(sessionId, (row) => !row.current_turn_id && row.last_turn_status === 'COMPLETED', remaining());
  await expect(page.getByTestId('composer-queue').filter({ hasText: correction })).toHaveCount(0);
  await expect(page.getByTestId('user-message').filter({ hasText: correction })).toHaveCount(1);
  const history = await api.getMessages(sessionId, 100);
  expect(history.messages.filter((row) => row.role === 'user').map(messageText)).toEqual([original, correction]);
  const assistantText = history.messages.filter((row) => row.role === 'assistant').map(messageText).join('\n');
  expect(assistantText).toContain(corrected);
  expect(assistantText).not.toContain(`${marker}_ORIGINAL_FINAL`);
  const journal = sessionEvents(sessionId);
  const terminal = journal.find((event) => event.turn_id === turnId && event.event_type === 'turn.completed');
  expect(terminal).toBeDefined();
  const commandEvents = journal.filter((event) => event.event_type === 'command.accepted');
  expect(commandEvents.filter((event) => record(event.payload).command_type === 'StartTurn')).toHaveLength(1);
  const accepted = commandEvents.find((event) => record(event.payload).client_message_id === correctionId);
  expect(accepted).toBeDefined();
  const inputId = String(record(accepted!.payload).input_id || '');
  const consumed = journal.find((event) => event.event_type === 'input.consumed'
    && record(event.payload).input_id === inputId);
  expect(consumed).toBeDefined();
  expect(consumed!.turn_id).toBe(turnId);
  expect(Number(consumed!.event_seq)).toBeLessThan(Number(terminal!.event_seq));
  const frames = framesForTurn(turnId);
  const commandId = String(accepted!.causation_id || '');
  expect(commandId).not.toBe('');
  const nativeReceipt = journal.find((event) => {
    const payload = record(event.payload);
    return event.turn_id === turnId && event.event_type === 'engine.diagnostic'
      && payload.engine_kind === 'assistant' && payload.event_type === 'prompt.submit'
      && payload.subtype === 'receipt' && record(payload.raw).command_id === commandId;
  });
  expect(nativeReceipt, 'the supplier receipt must identify the exact correction command').toBeDefined();
  const nativeResponse = record(record(nativeReceipt!.payload).raw).response;
  expect(record(record(nativeResponse).result).status).toBe('steered');
  const blocks = history.messages.flatMap((message) => (message.blocks ?? []) as Array<Record<string, unknown>>);
  const calls = blocks.filter((block) => block.type === 'tool_use' && block.name === 'terminal');
  expect(calls, 'the native correction prevents the second tool call in the original task').toHaveLength(1);
  const toolId = String(calls[0].id);
  expect(blocks.filter((block) => block.type === 'tool_use' && block.id === toolId)).toHaveLength(1);
  const results = blocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === toolId);
  expect(results).toHaveLength(1);
  expect(JSON.stringify(results[0])).toContain(toolOutput);
  const forbidden = await request.get(apiPath(`/sessions/${sessionId}/files/download?path=${encodeURIComponent(`/workspace/${obsolete}`)}`));
  expect(forbidden.status(), 'the steering instruction must prevent the original planned side effect').toBe(404);
  await revealAssistantProcess(page);
  const card = page.getByTestId('run-view').locator(`[data-tool-call-id="${toolId}"]`);
  await expect(card, 'earlier work survives the live input boundary').toHaveCount(1);
  const readResult = async () => {
    const header = card.locator('[data-slot="collapsible-trigger"]').first();
    await expect(header.locator('[data-slot="badge"]')).toHaveText(/^(Done|已完成)$/);
    const panel = card.locator('[data-slot="collapsible-content"]');
    if (!(await panel.isVisible())) await header.click();
    await expect(panel).toBeVisible();
    const result = panel.getByRole('heading', { name: 'Result', exact: true }).locator('..');
    await expect(result).toContainText(toolOutput);
  };
  await readResult();
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('user-message').filter({ hasText: correction })).toHaveCount(1);
  await expect(page.getByTestId('assistant-text').filter({ hasText: corrected })).toBeVisible();
  await revealAssistantProcess(page);
  await expect(card, 'the same supplier tool survives a cold history read').toHaveCount(1);
  await readResult();
  await info.attach('assistant-native-steering', { contentType: 'application/json',
    body: JSON.stringify({ sessionId, original, correction, acknowledgement: await acknowledgement.json(),
      consumed, terminal, nativeReceipt, journal, frames, history }) });
});
