/**
 * E2E: the built terminal renders the command echo and process output.
 *
 * `<Ansi>` mounts only after output exists, so the browser also asserts that
 * rendering the first line raises no page error.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath } from '../fixtures/env';

const sessions = trackSessions();

test('a command typed in the terminal prints its output, and nothing throws', async ({
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

  expect(uncaught, `uncaught exception while running a terminal command:\n${uncaught.join('\n')}`)
    .toEqual([]);
});
