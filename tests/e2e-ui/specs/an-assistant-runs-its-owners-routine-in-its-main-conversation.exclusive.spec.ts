/**
 * E2E: a routine the owner asks their Assistant for is a personal schedule it runs itself.
 *
 * The owner asks in the main conversation for work at a time they name. The
 * Assistant creates it with its create_schedule tool and no Agent, so it is
 * the owner's personal schedule on the Assistant: listed under Personal
 * schedules with the Assistant as the one doing it, and not among the
 * Assistant's console triggers, which do not take a schedule.
 * Run now from that dialog puts the routine into the Assistant's main
 * conversation, marked as sent by a schedule, and the reply arrives there;
 * the Run names that conversation and the response that answered it.
 *
 * The routine's calendar is a date that does not come during the test, so
 * the only Run is the one the owner starts.
 */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, answersInput, messageText } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import type { DeploymentRunRecord } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

test.use({ locale: 'en-US' });

const sessions = trackSessions();
let assistantId = '';
onPassOnly(async ({ request }) => {
  // Deleting the Assistant deletes the personal schedules created from it.
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

test("an Assistant runs its owner's routine in its main conversation", async ({ page, request }) => {
  const api = new AstraApi(request);
  const id = randomUUID().slice(0, 8);
  const marker = `ROUTINE_${id}`;
  const ready = `READY_${id}`;
  const name = `Routine ${id}`;
  const schedule = { cron: '0 3 1 1 *', timezone: 'UTC' };
  const displayName = `__e2e_assistant_routine_${id}`;

  const runtimeProfileName = await api.assistantEnvironmentName();
  assistantId = String((await api.createAssistant({
    display_name: displayName,
    runtime_profile_name: runtimeProfileName,
    model_config_override: { model_name: await api.assistantModelName(runtimeProfileName) },
  })).assistant_id || '');
  const mainId = String((await api.openMainConversation(assistantId)).session_id || '');
  sessions.push(mainId);
  await api.waitForSessionReady(mainId);

  const refused = await request.post(apiPath(`/assistants/${assistantId}/deployments`), {
    data: { scene: 'schedule', name, prompt_prefix: 'Reply OK.', schedule },
  });
  expect(refused.status(), `an Assistant's schedule is not a console trigger: ${await refused.text()}`).toBe(400);

  const created = await api.sendTurn(mainId, [
    'The owner asked for a routine at a set time. Create exactly one with your create_schedule tool,',
    'without agent_id, so that you run it yourself:',
    `name: ${name}`,
    `prompt: Reply with exactly ${marker} and nothing else. Do not use tools.`,
    `schedule: ${JSON.stringify(schedule)}`,
    `After it succeeds, reply ${ready} and finish your response.`,
    // The routine runs later in this same conversation, so nothing here may
    // forbid what it asks for then.
    'Do not run the schedule now and do not use cronjob_manage.',
  ].join('\n'), 90_000);
  expect(created.errorText).toBeFalsy();
  expect(
    created.toolNames.filter((tool) => /(?:^|_)create_schedule$/.test(tool)),
    JSON.stringify(created.toolNames),
  ).toHaveLength(1);
  expect(created.text).toContain(ready);

  const plans = (await api.data<Array<Record<string, unknown>>>('GET', '/me/schedules'))
    .filter((row) => row.name === name);
  expect(plans, 'find the routine by its name, not the model reply').toHaveLength(1);
  const plan = plans[0]!;
  const deploymentId = String(plan.deployment_id);
  expect(plan).toMatchObject({
    scope: 'personal', scene: 'schedule', enabled: true,
    assistant_id: assistantId, source_assistant_id: assistantId, schedule,
  });
  expect(plan.agent_id ?? '', 'the Assistant runs it, not an Agent').toBe('');
  expect(
    (await api.data<Array<{ deployment_id: string }>>('GET', `/assistants/${assistantId}/deployments`))
      .some((row) => row.deployment_id === deploymentId),
    'a personal schedule is not one of the console triggers',
  ).toBe(false);

  await page.goto(appPath('/'));
  await page.getByTestId('user-menu-trigger').click();
  await page.getByRole('menuitem', { name: 'Personal schedules', exact: true }).click();
  const row = page.getByTestId('personal-schedules').getByRole('button', { name, exact: true });
  await expect(row, 'the list names the Assistant as the one doing it').toContainText(displayName);
  await row.click();
  const runResponse = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === apiPath(`/me/schedules/${deploymentId}/runs`));
  await page.getByRole('button', { name: 'Run now', exact: true }).click();
  const receipt = await runResponse;
  expect(receipt.ok(), await receipt.text()).toBe(true);
  const runId = String((await receipt.json()).data?.run_id || '');
  expect(runId).not.toBe('');

  let run: DeploymentRunRecord | undefined;
  await expect.poll(async () => {
    run = (await api.data<DeploymentRunRecord[]>('GET', `/me/schedules/${deploymentId}/runs`))
      .find((record) => record.run_id === runId);
    expect(['FAILED', 'CANCELLED', 'UNKNOWN'], JSON.stringify(run)).not.toContain(run?.status);
    return run?.status;
  }, { timeout: 120_000, intervals: [1000] }).toBe('COMPLETED');
  expect(run?.session_id, 'the Run is in the main conversation').toBe(mainId);
  const inputId = String(run?.input_id || '');
  expect(inputId).not.toBe('');

  const messages = (await api.getMessages(mainId, 30)).messages;
  const delivered = messages.find((message) => message.role === 'user' && message.metadata?.input_id === inputId);
  expect(delivered?.metadata?.source, 'a schedule sent it, not the owner').toBe('schedule');
  expect(messageText(delivered!)).toContain(marker);
  const reply = messages.find((message) => answersInput(message, inputId));
  expect(reply?.metadata).toMatchObject({ status: 'ended', outcome: 'completed' });
  expect(messageText(reply!).trim()).toBe(marker);

  await page.keyboard.press('Escape');
  await page.goto(appPath(`/sessions/${mainId}`));
  const scheduled = page.getByTestId('user-message').filter({ has: page.getByTestId('scheduled-input') });
  await expect(scheduled, 'the conversation shows the routine as sent by a schedule').toHaveCount(1);
  await expect(scheduled).toContainText(marker);
  await expect(page.getByTestId('assistant-text').filter({ hasText: new RegExp(`^\\s*${marker}\\s*$`) })).toHaveCount(1);
});
