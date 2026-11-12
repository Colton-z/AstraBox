/**
 * E2E: work a Claude Code reply leaves running shows apart from the reply, and
 * stops on its own.
 *
 * A reply can start a background shell and end while the shell keeps running.
 * The conversation is then not answering anything: it is READY, the next
 * message is sent rather than queued, and the running shell is listed apart
 * from the conversation with a Stop of its own. Stopping it reaches the shell
 * through Claude Code's own task stop; the runtime then reports the task
 * ended, the list empties, the runtime reads idle, and the next message is
 * answered.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { engineProfileFor, modeName } from '../fixtures/engineProfile';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const SEND = /^(Send|发送)$/;
const STOP = /^(Stop|停止)$/;

const sessions = trackSessions();

test('a background shell left by a reply is listed apart, stops on its own, and the next message is answered', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const profile = engineProfileFor('claude_code');
  const marker = `BG${Date.now().toString(36)}`;
  const sessionId = (await api.startConversation(profile.agent_id)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId, 60_000);
  await api.setPermissionMode(sessionId, modeName(profile, 'unattended'));
  await openSessionView(page, sessionId);

  await sendPrompt(
    page,
    sessionId,
    `Use the Bash tool once with run_in_background set to true to run exactly: sleep 900 # ${marker} `
      + 'Do not wait for it and do not check on it. Then reply with only the word STARTED.',
  );

  // The reply ended; only the shell runs.
  let task: { task_id: string; kind: string; description: string } | undefined;
  await expect.poll(async () => {
    const session = await api.getSession(sessionId);
    task = session.background_tasks?.[0];
    return [session.state, session.runtime_status, session.current_responses?.length ?? 0, session.background_tasks?.length ?? 0];
  }, { message: 'the reply ends with one background task running', timeout: 90_000, intervals: [1_000] })
    .toEqual(['READY', 'background', 0, 1]);
  expect(task?.kind).toEqual('local_bash');

  const strip = page.getByTestId('session-background-tasks');
  await expect(strip.getByTestId('session-background-task')).toHaveCount(1, { timeout: 15_000 });
  await expect(page.getByTestId('composer-submit'), 'the next message is sent, not queued')
    .toHaveAccessibleName(SEND);

  await strip.getByRole('button', { name: STOP }).click();
  await expect.poll(async () => {
    const session = await api.getSession(sessionId);
    return [session.state, session.runtime_status, session.background_tasks?.length ?? 0];
  }, { message: 'the stopped task ends and the runtime is idle', timeout: 30_000, intervals: [1_000] })
    .toEqual(['READY', 'idle', 0]);
  await expect(strip).toHaveCount(0);

  const answer = await api.sendTurn(sessionId, 'What is 17 plus 25? Reply with just the number.', 60_000);
  expect(answer.errorText).toBeNull();
  expect(answer.text).toContain('42');
});
