/**
 * E2E: the built terminal renders the command echo and process output, stops
 * a command through the terminal's own interrupt, and follows a command that
 * outlived the page that started it.
 *
 * `<Ansi>` mounts only after output exists, so the browser also asserts that
 * rendering the first line raises no page error.
 *
 * The stop button calls `POST …/terminal/interrupt`, which answers once the
 * command has ended; the conversation's interrupt only reaches the agent
 * runtime and leaves a running command alone, so the page must not call it.
 * A command runs on when its page goes away, and a page that opens while it
 * runs reads it back with `GET …/terminal/stream`.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath } from '../fixtures/env';

const sessions = trackSessions();

test('a command typed in the terminal prints its output, stops on Stop, and is followed after a reload', async ({
  page,
  request,
}) => {
  const uncaught: string[] = [];
  // The first output can fail during render, so attach before navigation.
  page.on('pageerror', (error) => uncaught.push(error.message));

  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  expect(sessionId, 'conversation created').toBeTruthy();

  await api.waitForSessionReady(sessionId);
  await page.goto(appPath(`/sessions/${sessionId}`));

  await page.getByRole('tab', { name: 'Terminal' }).click();
  // The panel says it is ready before it will accept anything; without this the
  // command lands in a disabled input and the test passes for the wrong reason.
  await expect(page.getByText('Terminal ready — type a command below.')).toBeVisible();

  const input = page.getByPlaceholder('Type a command…');
  await input.fill('echo astrabox-terminal-probe');
  await input.press('Enter');

  // The echo of the command, then the process's own bytes. Both, because the
  // command line is drawn by this panel and the output by the ANSI reader —
  // asserting only the first would pass with the reader broken.
  await expect(page.getByText('$ echo astrabox-terminal-probe')).toBeVisible();
  await expect(page.getByText('astrabox-terminal-probe', { exact: true })).toBeVisible();

  // Stop: the terminal's own interrupt, never the conversation's.
  const runId = `${Date.now()}`;
  const interrupts: string[] = [];
  page.on('request', (candidate) => {
    if (candidate.method() === 'POST' && /\/interrupt$/.test(new URL(candidate.url()).pathname)) {
      interrupts.push(new URL(candidate.url()).pathname);
    }
  });
  await expect(input).toBeEnabled();
  await input.fill(`sleep 60; echo never-printed-${runId}`);
  await input.press('Enter');
  await expect(page.getByText('Running', { exact: true })).toBeVisible();
  const stopped = page.waitForResponse((candidate) => (
    candidate.request().method() === 'POST'
    && new URL(candidate.url()).pathname.endsWith(`/sessions/${sessionId}/terminal/interrupt`)
  ));
  await page.getByRole('button', { name: 'Stop', exact: true }).click();
  const answer = await stopped;
  expect(answer.status(), 'the terminal interrupt is accepted').toBe(200);
  expect((await answer.json()).data, 'the interrupt answers once the command has ended')
    .toMatchObject({ session_id: sessionId, status: 'completed' });
  await expect(page.getByText('Command interrupted by user')).toBeVisible();
  await expect(input, 'a stopped command frees the prompt').toBeEnabled({ timeout: 15_000 });
  await expect(page.getByText(`never-printed-${runId}`, { exact: true })).toHaveCount(0);
  expect(interrupts, 'Stop calls the terminal interrupt and nothing else').toEqual([
    expect.stringMatching(/\/terminal\/interrupt$/),
  ]);

  // A command outlives the page that started it; the page that opens next
  // follows it to its end.
  await input.fill(`echo tick-1-${runId}; sleep 4; echo tick-2-${runId}`);
  await input.press('Enter');
  await expect(page.getByText(`tick-1-${runId}`, { exact: true })).toBeVisible();
  const followed = page.waitForRequest((candidate) => (
    candidate.method() === 'GET'
    && new URL(candidate.url()).pathname.endsWith(`/sessions/${sessionId}/terminal/stream`)
  ));
  await page.reload();
  await page.getByRole('tab', { name: 'Terminal' }).click();
  await followed;
  await expect(page.getByText(`tick-1-${runId}`, { exact: true }), 'the command is read back from its start').toBeVisible();
  await expect(page.getByText(`tick-2-${runId}`, { exact: true }), 'and followed to its end').toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('Exit code: 0'), 'its exit arrives').toBeVisible({ timeout: 30_000 });
  await expect(page.getByPlaceholder('Type a command…')).toBeEnabled();

  expect(uncaught, `uncaught exception while running a terminal command:\n${uncaught.join('\n')}`)
    .toEqual([]);
});
