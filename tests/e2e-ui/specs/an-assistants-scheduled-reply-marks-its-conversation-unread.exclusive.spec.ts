/**
 * E2E: a reply the owner did not wait for reaches them in the console.
 *
 * An Assistant's scheduled job, created in its main conversation, reports
 * back there while the owner is on another page. The Assistant's row in the
 * rail turns unread (the main conversation is that row, not a second entry in
 * the sessions list), the console raises a browser notification for it, and
 * opening the Assistant lands in the conversation and reads it.
 *
 * The browser's own notification display is the browser's: the page's
 * `Notification` is replaced by a recorder that reports permission granted, so
 * the evidence is the notification the console asked for, with the
 * conversation it names. Unread is read back from the API as well as the rail.
 *
 * This journey provisions a workspace and waits for Hermes' scheduler, so it
 * stays in the suite contract's one-worker serial group.
 */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

test.use({ locale: 'en-US' });

const sessions = trackSessions();
let assistantId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

type Recorded = { title: string; tag: string };

test("an Assistant's scheduled reply marks its conversation unread and notifies the owner", async ({ page, request }) => {
  const api = new AstraApi(request);
  const id = randomUUID().slice(0, 8);
  const ready = `READY_${id}`;
  const cronMarker = `CRON_${id}`;
  // Hermes' scheduler checks for due jobs once a minute; a one-shot due
  // shortly after the reply runs at the next check, late enough that the
  // owner reads the conversation before it fires.
  const dueAt = new Date(Date.now() + 60_000).toISOString().replace(/\.\d{3}Z$/, 'Z');

  const runtimeProfileName = await api.assistantEnvironmentName();
  assistantId = String((await api.createAssistant({
    display_name: `__e2e_assistant_unread_${id}`,
    runtime_profile_name: runtimeProfileName,
    model_config_override: { model_name: await api.assistantModelName(runtimeProfileName) },
  })).assistant_id || '');
  const sessionId = String((await api.openMainConversation(assistantId)).session_id || '');
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  const scheduled = await api.sendTurn(sessionId, [
    `Use the cronjob_manage tool once, with action="create", schedule=${JSON.stringify(dueAt)}`,
    `and this exact prompt: ${JSON.stringify(`Reply with exactly ${cronMarker} and nothing else.`)}`,
    `After it succeeds, reply ${ready} and finish your response.`,
    `Do not run the job now, do not use action="run", and never write ${cronMarker} yourself.`,
  ].join('\n'), 90_000);
  expect(scheduled.errorText).toBeFalsy();
  expect(scheduled.text).toContain(ready);

  // The owner has read the conversation up to the job's creation.
  const read = await request.post(apiPath(`/sessions/${sessionId}/read`));
  expect(read.ok(), await read.text()).toBe(true);

  await page.addInitScript(() => {
    const recorded: Array<{ title: string; tag: string }> = [];
    (window as unknown as { __notifications: typeof recorded }).__notifications = recorded;
    class RecordingNotification {
      static permission = 'granted';
      static requestPermission = async () => 'granted';
      onclick: (() => void) | null = null;
      constructor(title: string, options?: { tag?: string }) {
        recorded.push({ title, tag: String(options?.tag ?? '') });
      }
      close() {}
    }
    Object.defineProperty(window, 'Notification', { value: RecordingNotification, configurable: true });
  });
  // Elsewhere in the console: the Agents page, with the rail beside it.
  await page.goto(appPath('/agents'));
  const row = page.locator(`[data-testid="assistant-row"][data-assistant-id="${assistantId}"]`);
  await expect(row).toBeVisible();
  await expect(row, 'a conversation read up to now is not unread').not.toHaveAttribute('data-unread', 'true');
  await expect(
    page.locator(`[data-testid="session-row"][data-session-id="${sessionId}"]`),
    'the main conversation is the Assistant row, not also a sessions-list row',
  ).toHaveCount(0);

  await expect(row, 'the scheduled reply marks the conversation unread').toHaveAttribute(
    'data-unread', 'true', { timeout: 150_000 },
  );
  const notified = await page.evaluate(
    () => (window as unknown as { __notifications: Recorded[] }).__notifications,
  );
  expect(notified.map((entry) => entry.tag), 'the console notifies the owner of that conversation').toContain(sessionId);
  const session = await api.data<{ last_output_at?: string; read_at?: string }>('GET', `/sessions/${sessionId}`);
  expect(String(session.last_output_at) > String(session.read_at), 'the API agrees it is unread').toBe(true);

  // Opening the Assistant lands in its main conversation and reads it.
  await row.getByRole('link').click();
  await expect(page).toHaveURL(new RegExp(`/sessions/${sessionId}$`));
  await expect(page.getByText(cronMarker).first()).toBeVisible({ timeout: 30_000 });
  await expect.poll(async () => {
    const now = await api.data<{ last_output_at?: string; read_at?: string }>('GET', `/sessions/${sessionId}`);
    return String(now.read_at) >= String(now.last_output_at);
  }, { timeout: 15_000 }).toBe(true);
  await page.goto(appPath('/agents'));
  await expect(row).toBeVisible();
  await expect(row, 'a read conversation is not unread').not.toHaveAttribute('data-unread', 'true');
});
