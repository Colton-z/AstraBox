/**
 * A schedule is a real Deployment trigger: create and edit it in the console,
 * run it immediately without moving the calendar, then replay that invocation
 * as a distinct Run linked to the source.
 */
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi, type DeploymentRunRecord } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const RUN_ID = new Date().toISOString().replace(/[:.]/g, '-');
const RUN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_SCHEDULE_RUN_TIMEOUT_MS', 150_000);
const MARKER = `schedule-run-${RUN_ID}`;

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
// A retained failure keeps its records, but it must not keep firing new Runs.
test.afterEach(async ({ request }) => {
  if (!deploymentId || !agentId) return;
  await new PlatformApi(request).updateDeployment(agentId, deploymentId, { enabled: false });
});
onPassOnly(async ({ request }) => {
  const platform = new PlatformApi(request);
  if (deploymentId && agentId) await platform.deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

async function waitForRun(
  platform: PlatformApi,
  predicate: (run: DeploymentRunRecord) => boolean,
  description: string,
): Promise<DeploymentRunRecord> {
  const deadline = Date.now() + RUN_TIMEOUT_MS;
  let last: DeploymentRunRecord[] = [];
  while (Date.now() < deadline) {
    last = await platform.listDeploymentRuns(agentId, deploymentId);
    const found = last.find(predicate);
    if (found) return found;
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(`${description}; last Runs=${JSON.stringify(last)}`);
}

test('a scheduled Deployment runs, edits, and replays through Sessions', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);

  const base = await api.defaultAgent();
  const environmentName = String(base.environment_name || '').trim();
  expect(environmentName, 'the seeded Agent must name an Environment').not.toEqual('');
  let model = String(base.model || '').trim();
  if (!model || model.includes('*')) {
    const models = await api.listEnvironmentModels(environmentName);
    model = models.find((item) => item && !item.includes('*')) || 'deepseek-chat';
  }
  const agentName = `__e2e_schedule_${RUN_ID}`;
  const agent = await api.createAgent({
    name: agentName,
    model,
    environment_name: environmentName,
  });
  agentId = String(agent.agent_id || '').trim();
  expect(agentId).not.toEqual('');

  await page.goto(appPath('/manage/deployments/new'));
  await page.getByLabel('Agent').selectOption({ label: agentName });
  await page.getByLabel('Trigger').selectOption('schedule');
  await page.getByLabel('Name').fill(`Daily report ${RUN_ID}`);
  await page.getByLabel('Prompt').fill(`Reply briefly and include this marker: ${MARKER}`);
  await page.getByLabel('Cron expression').fill('* * * * *');
  await page.getByLabel('Timezone').fill('UTC');
  await page.getByRole('button', { name: 'Create' }).click();
  await page.waitForURL((url) => {
    const segment = url.pathname.split('/').filter(Boolean).pop();
    return segment !== undefined && segment !== 'new';
  });
  deploymentId = decodeURIComponent(page.url().split('/').pop() || '');
  expect(deploymentId, 'create must navigate to the scheduled Deployment').not.toEqual('');

  const created = (await platform.listDeployments(agentId)).find(
    (item) => item.deployment_id === deploymentId,
  );
  expect(created?.scene).toBe('schedule');
  expect(created?.name).toBe(`Daily report ${RUN_ID}`);
  expect(created?.schedule).toEqual({ cron: '* * * * *', timezone: 'UTC' });
  expect(created?.secret, 'an internal schedule must not mint a credential').toBeUndefined();

  const scheduled = await waitForRun(
    platform,
    (run) => run.trigger === 'schedule' && !!run.session_id,
    'automatic cron Run did not bind a Session',
  );
  sessions.push(String(scheduled.session_id));
  expect(scheduled.scheduled_for).toBeTruthy();
  await page.getByRole('button', { name: 'Disable' }).click();
  await expect(page.getByRole('button', { name: 'Enable' })).toBeVisible();
  await waitForRun(
    platform,
    (run) => run.run_id === scheduled.run_id && run.status === 'COMPLETED',
    'automatic cron Run did not complete',
  );

  const messages = await api.getMessages(String(scheduled.session_id), 50);
  expect(
    messages.messages
      .filter((message) => message.role === 'user')
      .map(messageText)
      .join('\n'),
    'the scheduled prompt must be the Run turn input',
  ).toContain(MARKER);

  await page.getByRole('button', { name: 'Run now' }).click();
  const manual = await waitForRun(
    platform,
    (run) => run.trigger === 'manual' && !!run.session_id,
    'manual Run did not bind a Session',
  );
  sessions.push(String(manual.session_id));
  expect(manual.scheduled_for, 'Run now must not impersonate a cron slot').toBeNull();

  await page.getByLabel('Name').fill(`Weekday report ${RUN_ID}`);
  await page.getByLabel('Cron expression').fill('0 6 * * 1-5');
  await page.getByRole('button', { name: 'Save' }).click();
  await expect(page.getByRole('heading', { name: `Weekday report ${RUN_ID}` })).toBeVisible();
  const edited = (await platform.listDeployments(agentId)).find(
    (item) => item.deployment_id === deploymentId,
  );
  expect(edited?.schedule).toEqual({ cron: '0 6 * * 1-5', timezone: 'UTC' });

  await page.getByRole('button', { name: 'Replay' }).first().click();
  const replay = await waitForRun(
    platform,
    (run) => run.replayed_from_run_id === manual.run_id && !!run.session_id,
    'replay did not bind its own Session',
  );
  sessions.push(String(replay.session_id));
  expect(replay.run_id, 'replay must have a new invocation identity').not.toBe(
    manual.run_id,
  );
});
