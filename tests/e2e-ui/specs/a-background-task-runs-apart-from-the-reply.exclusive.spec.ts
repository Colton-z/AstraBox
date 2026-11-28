/**
 * E2E: work a Claude Code reply leaves running shows apart from the reply, and
 * stops on its own.
 *
 * A reply can start a background shell and end while the shell keeps running.
 * The conversation is then not answering anything: it is READY, the next
 * message is sent rather than queued, and the running shell is counted in one
 * quiet line above the composer, its list folded until the reader opens it,
 * with a Stop of its own. The line takes room from the conversation, and a
 * reader at the end of the conversation stays at its end. Stopping the task
 * reaches the shell through Claude Code's own task stop; the runtime then
 * reports the task ended, the line goes, the runtime reads idle, and the next
 * message is answered.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { engineProfileFor, modeName } from '../fixtures/engineProfile';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const SEND = /^(Send|发送)$/;
const SUMMARY = /^(1 background task running|1 个后台任务运行中)$/;
/** Lines that make the conversation taller than its viewport. */
const PADDING = Array.from({ length: 40 }, (_, line) => `Padding line ${line + 1}; ignore it.`).join('\n');
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
  await page.setViewportSize({ width: 1280, height: 720 });
  await openSessionView(page, sessionId);

  await sendPrompt(
    page,
    sessionId,
    `Use the Bash tool once with run_in_background set to true to run exactly: sleep 900 # ${marker} `
      + 'Do not wait for it and do not check on it. Then reply with only the word STARTED.\n\n'
      + PADDING,
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
  const summary = page.getByTestId('session-background-tasks-summary');
  await expect(summary).toHaveText(SUMMARY, { timeout: 15_000 });
  await expect(page.getByTestId('session-background-task'), 'the list stays folded until asked for')
    .toHaveCount(0);
  await expect(page.getByTestId('composer-submit'), 'the next message is sent, not queued')
    .toHaveAccessibleName(SEND);
  await expect.poll(() => page.getByTestId('session-conversation').evaluate((element) => ({
    overflows: element.scrollHeight > element.clientHeight,
    atEnd: element.scrollHeight - element.clientHeight - element.scrollTop <= 2,
  })), { message: 'the line above the composer leaves the conversation at its end' })
    .toEqual({ overflows: true, atEnd: true });

  await summary.click();
  const listed = page.getByTestId('session-background-task');
  await expect(listed).toHaveCount(1);
  await listed.getByRole('button', { name: STOP }).click();
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
