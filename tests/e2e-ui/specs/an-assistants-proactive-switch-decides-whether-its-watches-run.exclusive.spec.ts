/**
 * E2E: the owner's Proactive switch decides whether the watches an Assistant set up for itself run.
 *
 * The owner switched the Assistant's Proactive work off before its workspace
 * started. Asked to watch for something, the Assistant still records the job
 * in Hermes' own store (`cron/jobs.json`), but nothing runs it: past its due
 * time the store shows it never ran, no result reached the conversation, and
 * no Hermes scheduler is alive in the box. The owner then
 * switches Proactive on in the Assistant's profile. The running workspace's
 * profile is prepared again, Hermes' scheduler starts, the overdue job runs
 * under Hermes' own catch-up rule, and its result reaches the conversation
 * that created it.
 *
 * Absence is read from Hermes' records rather than waited out. Every
 * scheduler loop stamps `cron/ticker_heartbeat` with its time and process id,
 * and Hermes counts a scheduler alive only while that process runs
 * (`cron/jobs.py` `ticker_heartbeat_writer_alive`); a stamp older than the
 * job's due time from a process that is gone says nothing could dispatch it.
 */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { requireSandboxHandle, sandboxExec, type SandboxHandle } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

test.use({ locale: 'en-US' });

const sessions = trackSessions();
let assistantId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

type HermesJob = { id?: string; prompt?: string; last_run_at?: string | null };
/** Hermes' scheduler liveness stamp: when a loop last ran, and whether that process still runs. */
type Heartbeat = { at: number; writerAlive: boolean } | null;

/**
 * The profile's jobs as Hermes stored them, and whether a scheduler ever wrote its heartbeat.
 * One box carries one Assistant profile, so its Hermes home is the only one under the workspace root.
 */
function hermesCron(handle: SandboxHandle): { jobs: HermesJob[]; heartbeat: Heartbeat } {
  const output = sandboxExec(handle, [
    'h=$(find /home/conversations -mindepth 2 -maxdepth 2 -name .hermes -type d)',
    'test "$(printf "%s\n" "$h" | grep -c .)" = 1',
    'if [ -f "$h/cron/jobs.json" ]; then cat "$h/cron/jobs.json"; else echo "[]"; fi',
    'echo; echo ===',
    'if [ -f "$h/cron/ticker_heartbeat" ]; then',
    '  read -r at pid < "$h/cron/ticker_heartbeat"',
    '  if kill -0 "$pid" 2>/dev/null; then echo "$at alive"; else echo "$at gone"; fi',
    'else echo none; fi',
  ].join('\n'));
  const [stored, stamp] = output.split('===');
  const parsed = JSON.parse(String(stored).trim()) as HermesJob[] | { jobs?: HermesJob[] };
  const [at, writer] = String(stamp).trim().split(/\s+/);
  return {
    jobs: Array.isArray(parsed) ? parsed : parsed.jobs ?? [],
    heartbeat: at === 'none' ? null : { at: Number(at) * 1000, writerAlive: writer === 'alive' },
  };
}

test("an Assistant's Proactive switch decides whether its watches run", async ({ page, request }) => {
  const started = Date.now();
  const remaining = () => Math.max(1_000, started + 170_000 - Date.now());
  const api = new AstraApi(request);
  const id = randomUUID().slice(0, 8);
  const marker = `WATCH_${id}`;
  const ready = `READY_${id}`;

  const runtimeProfileName = await api.assistantEnvironmentName();
  assistantId = String((await api.createAssistant({
    display_name: `__e2e_assistant_proactive_${id}`,
    runtime_profile_name: runtimeProfileName,
    model_config_override: { model_name: await api.assistantModelName(runtimeProfileName) },
  })).assistant_id || '');
  const off = await api.data<{ proactive?: boolean }>('PATCH', `/assistants/${assistantId}`, { proactive: false });
  expect(off.proactive).toBe(false);
  const mainId = String((await api.openMainConversation(assistantId)).session_id || '');
  sessions.push(mainId);
  await api.waitForSessionReady(mainId, remaining());
  const detail = await api.adminSessionDetail(mainId);
  const handle = await requireSandboxHandle(api, String(detail.sandbox_id));

  // Due shortly after the reply; Hermes keeps a one-shot due within its
  // two-minute grace, so it can still run once the switch is on.
  const dueAt = new Date(Date.now() + 25_000).toISOString().replace(/\.\d{3}Z$/, 'Z');
  const created = await api.sendTurn(mainId, [
    `Use the cronjob_manage tool once, with action="create", schedule=${JSON.stringify(dueAt)}`,
    `and this exact prompt: ${JSON.stringify(`Reply with exactly ${marker} and nothing else.`)}`,
    `After it succeeds, reply ${ready} and finish your response.`,
    `Do not run the job now, do not use action="run", and never write ${marker} yourself.`,
  ].join('\n'), Math.min(90_000, remaining()));
  expect(created.errorText).toBeFalsy();
  expect(created.text).toContain(ready);

  const job = () => hermesCron(handle).jobs
    .find((entry) => String(entry.prompt ?? '').includes(marker));
  expect(job(), "the watch is in Hermes' own store").toBeTruthy();
  const untilOverdue = Date.parse(dueAt) + 10_000 - Date.now();
  if (untilOverdue > 0) await page.waitForTimeout(untilOverdue);
  const held = hermesCron(handle);
  expect(held.heartbeat?.writerAlive ?? false, 'no Hermes scheduler is alive in the box').toBe(false);
  expect(held.heartbeat?.at ?? 0, 'no scheduler loop ran since the watch came due').toBeLessThan(Date.parse(dueAt));
  expect(
    held.jobs.find((entry) => String(entry.prompt ?? '').includes(marker))?.last_run_at ?? null,
    'the overdue watch has not run',
  ).toBeNull();
  const reported = async () => (await api.getMessages(mainId, 30)).messages
    .some((message) => message.role === 'assistant' && messageText(message).includes(marker));
  expect(await reported(), 'nothing reached the conversation').toBe(false);

  await page.goto(appPath(`/sessions/${mainId}`));
  await page.getByTestId('assistant-profile-open').click();
  const toggle = page.getByTestId('assistant-proactive');
  await expect(toggle, 'the profile shows the switch off').not.toBeChecked();
  const saved = page.waitForResponse((response) => response.request().method() === 'PATCH'
    && new URL(response.url()).pathname === apiPath(`/assistants/${assistantId}`));
  await toggle.click();
  const answer = await saved;
  expect(answer.ok(), await answer.text()).toBe(true);
  await expect(toggle).toBeChecked();
  await page.keyboard.press('Escape');

  await expect.poll(() => job()?.last_run_at ?? null, {
    timeout: remaining(), intervals: [2000], message: 'the overdue watch runs once the scheduler starts',
  }).not.toBeNull();
  expect(hermesCron(handle).heartbeat?.writerAlive, "Hermes' scheduler is alive").toBe(true);
  await expect.poll(reported, {
    timeout: remaining(), intervals: [2000], message: 'its result reaches the conversation that created it',
  }).toBe(true);
});
