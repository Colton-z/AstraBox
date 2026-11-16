/**
 * A state shown and taken back before anyone could read it: the header saying
 * Ready for two frames between Sending and Creating, a status line emptying
 * between two steps, a stop button that leaves and returns as a reply starts.
 * Each is small, nothing fails when one appears, and a page full of them looks
 * unsure of itself. They are recorded frame by frame while a conversation runs
 * (fixtures/flicker.ts) and none is allowed.
 *
 * The journeys are the ones a reader makes every day. Each ends a few seconds
 * after the session settles, because the end of a response is where the
 * stream's account and the session's own arrive apart.
 */
import { expect, test, type Page } from '@playwright/test';
import { AstraApi } from '../fixtures/astraApi';
import { recordFlickerFromLoad, startFlickerRecording, stopFlickerRecording } from '../fixtures/flicker';
import { trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled, openSessionView } from '../fixtures/sessionPage';

const sessions = trackSessions();

const WORK = 'Run `sleep 3; echo a1`, then `sleep 3; echo a2`, then write one short sentence saying you are halfway, '
  + 'then run `sleep 3; echo b1`, each command as its own tool call. Finally reply with the single word done.';
const TURN_MS = 150_000;

async function send(page: Page, text: string): Promise<void> {
  await page.getByTestId('composer-prompt').fill(text);
  await page.getByTestId('composer-submit').click();
}

/** The response this page started has run and the session says it is over. */
async function settled(page: Page): Promise<void> {
  const stop = page.getByTestId('run-composer-stop');
  await expect(stop).toBeVisible({ timeout: 60_000 });
  await expect(stop).toHaveCount(0, { timeout: TURN_MS });
  // The summary and the session's own read of the end land after the stream's.
  await page.waitForTimeout(5_000);
}

async function open(page: Page, api: AstraApi): Promise<string> {
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  sessions.push(created.session_id);
  await openSessionView(page, created.session_id);
  await expectComposerEnabled(page);
  return created.session_id;
}

test('a reply that does several steps of work shows no state it takes back', async ({ page, request }) => {
  test.setTimeout(TURN_MS * 2 + 60_000);
  await open(page, new AstraApi(request));

  await startFlickerRecording(page);
  await send(page, WORK);
  await settled(page);
  expect(await stopFlickerRecording(page), 'the first message of a conversation').toEqual([]);

  await startFlickerRecording(page);
  await send(page, 'Now run `sleep 2; echo c1` and reply done.');
  await settled(page);
  expect(await stopFlickerRecording(page), 'a follow-up below a screen of history').toEqual([]);
});

test('a message sent while a reply runs waits its turn without a flicker', async ({ page, request }) => {
  test.setTimeout(TURN_MS * 2 + 60_000);
  await open(page, new AstraApi(request));

  await startFlickerRecording(page);
  await send(page, 'Run `sleep 6; echo d1` and reply done.');
  await expect(page.getByTestId('run-composer-stop')).toBeVisible({ timeout: 60_000 });
  await page.waitForTimeout(3_000);
  await send(page, 'After that, reply with the single word second.');
  // Two responses: the one that was running, then the one the held message starts.
  await expect(page.getByTestId('assistant-message')).toHaveCount(2, { timeout: TURN_MS });
  await expect(page.getByTestId('run-composer-stop')).toHaveCount(0, { timeout: TURN_MS });
  await page.waitForTimeout(5_000);
  expect(await stopFlickerRecording(page)).toEqual([]);
});

test('stopping a reply goes from working to stopping to ready, once', async ({ page, request }) => {
  test.setTimeout(TURN_MS + 60_000);
  await open(page, new AstraApi(request));

  await startFlickerRecording(page);
  await send(page, 'Run `sleep 30; echo e1` and reply done.');
  await expect(page.getByTestId('assistant-process-activity')).toContainText('sleep 30', { timeout: TURN_MS });
  await page.waitForTimeout(3_000);
  await page.getByTestId('run-composer-stop').click();
  await expect(page.getByTestId('run-composer-stop')).toHaveCount(0, { timeout: 60_000 });
  await page.waitForTimeout(5_000);
  expect(await stopFlickerRecording(page)).toEqual([]);
});

test('reloading the page during a reply, and after it, paints each state once', async ({ page, request }) => {
  test.setTimeout(TURN_MS + 90_000);
  await open(page, new AstraApi(request));

  await send(page, 'Run `sleep 12; echo f1` and reply done.');
  await expect(page.getByTestId('assistant-process-activity')).toContainText('sleep 12', { timeout: TURN_MS });
  await recordFlickerFromLoad(page);
  await page.reload();
  await expect(page.getByTestId('run-composer-stop')).toHaveCount(0, { timeout: TURN_MS });
  await page.waitForTimeout(5_000);
  expect(await stopFlickerRecording(page), 'reloaded while the reply ran').toEqual([]);

  await page.reload();
  await expectComposerEnabled(page);
  await page.waitForTimeout(5_000);
  expect(await stopFlickerRecording(page), 'reloaded after it settled').toEqual([]);
});
