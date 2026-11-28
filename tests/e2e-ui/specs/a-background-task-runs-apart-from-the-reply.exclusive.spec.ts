/**
 * E2E: work a Claude Code reply leaves running shows apart from the reply, and
 * stops on its own.
 *
 * A reply can start a background shell and a background subagent and end while
 * both keep running. The conversation is then not answering anything: it is
 * READY and the next message is sent rather than queued. The composer carries
 * one quiet line with how many tasks run — the subagent once, although Claude
 * Code reports it both as a task and as a child run — and the line opens the
 * Tasks tab, which lists the shell with a Stop of its own and the subagent,
 * whose transcript is where it stops. The line takes room from the
 * conversation, and a reader at the end of the conversation stays at its end.
 * Each stop reaches Claude Code's own task stop; the runtime then reports both
 * ended, the line goes, the runtime reads idle, and the next message is
 * answered.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { engineProfileFor, modeName } from '../fixtures/engineProfile';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const SEND = /^(Send|发送)$/;
/** Lines that make the conversation taller than its viewport. */
const PADDING = Array.from({ length: 40 }, (_, line) => `Padding line ${line + 1}; ignore it.`).join('\n');
const STOP = /^(Stop|停止)$/;

const sessions = trackSessions();

type Task = { task_id: string; kind: string; description: string; child_run_id?: string | null };

test('a background shell left by a reply is listed apart, stops on its own, and the next message is answered', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const api = new AstraApi(request);
  const profile = engineProfileFor('claude_code');
  const marker = `BG${Date.now().toString(36)}`;
  const sessionId = (await api.startConversation(profile.agent_id)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId, 60_000);
  await api.setPermissionMode(sessionId, modeName(profile, 'unattended'));
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      // The page still renders in its default language.
    }
  });
  await page.setViewportSize({ width: 1280, height: 720 });
  await openSessionView(page, sessionId);

  await sendPrompt(
    page,
    sessionId,
    [
      `1. Use the Bash tool once with run_in_background set to true to run exactly: sleep 900 # ${marker}-shell`,
      // Claude Code refuses a standalone foreground `sleep`; a Python wait is
      // what keeps the child working until it is stopped.
      '2. Launch exactly one Agent with run_in_background set to true. Tell the child to use Bash exactly once, '
        + 'with run_in_background set to false and timeout set to 600000, to run exactly: '
        + `python3 -c "import time; [time.sleep(1) for _ in range(900)]" # ${marker}-child`,
      'Do not wait for either and do not check on them. Then reply with only the word STARTED.',
      '',
      PADDING,
    ].join('\n'),
  );

  // The reply ended; the shell and the subagent run.
  let tasks: Task[] = [];
  await expect.poll(async () => {
    const session = await api.getSession(sessionId);
    tasks = session.background_tasks ?? [];
    return [
      session.state, session.runtime_status, session.current_responses?.length ?? 0,
      tasks.some((task) => task.kind === 'local_bash' && !task.child_run_id),
      tasks.filter((task) => task.child_run_id).length,
    ];
  }, { message: 'the reply ends with a shell and a subagent running', timeout: 120_000, intervals: [1_000] })
    .toEqual(['READY', 'background', 0, true, 1]);
  const agent = tasks.find((task) => task.child_run_id)!;
  const commandTasks = tasks.filter((task) => !task.child_run_id);
  const child = (await api.listChildRuns(sessionId)).child_runs.find((run) => run.child_run_id === agent.child_run_id);
  expect(child, 'the subagent task names a child run the Session lists').toBeTruthy();

  // Each task once: the subagent is its child run, not a second command.
  const summary = page.getByTestId('session-tasks-summary');
  await expect(summary).toHaveText(`${tasks.length} tasks running`, { timeout: 30_000 });
  const tab = page.getByRole('tab', { name: `Tasks (${tasks.length})`, exact: true });
  await expect(tab).toBeVisible();
  await expect(page.getByTestId('composer-submit'), 'the next message is sent, not queued')
    .toHaveAccessibleName(SEND);
  await expect.poll(() => page.getByTestId('session-conversation').evaluate((element) => ({
    overflows: element.scrollHeight > element.clientHeight,
    atEnd: element.scrollHeight - element.clientHeight - element.scrollTop <= 2,
  })), { message: 'the line above the composer leaves the conversation at its end' })
    .toEqual({ overflows: true, atEnd: true });

  // The line opens the Tasks tab: each shell is a command, the subagent its row.
  await summary.click();
  await expect(tab).toHaveAttribute('aria-selected', 'true');
  const running = page.getByTestId('session-tasks-running');
  const commands = running.getByTestId('session-task-command');
  await expect.poll(() => commands.evaluateAll((rows) => rows.map((row) => row.getAttribute('data-task-id')).sort()))
    .toEqual(commandTasks.map((task) => task.task_id).sort());
  const agentRow = running.locator(`[data-testid="subagent-agent-row"][data-child-run-id="${agent.child_run_id}"]`);
  await expect(agentRow, 'the subagent is listed as itself').toBeVisible({ timeout: 30_000 });

  for (const task of commandTasks) {
    await commands.and(page.locator(`[data-task-id="${task.task_id}"]`)).getByRole('button', { name: STOP }).click();
  }
  await expect.poll(async () => (await api.getSession(sessionId)).background_tasks?.map((task) => task.task_id) ?? [], {
    message: 'the stopped shells end', timeout: 30_000, intervals: [1_000],
  }).toEqual([agent.task_id]);
  await expect(summary).toHaveText('1 task running');

  await agentRow.click();
  await page.getByTestId('subagent-stop-button').click();
  await expect.poll(async () => {
    const session = await api.getSession(sessionId);
    return [session.state, session.runtime_status, session.background_tasks?.length ?? 0];
  }, { message: 'the stopped subagent ends and the runtime is idle', timeout: 60_000, intervals: [1_000] })
    .toEqual(['READY', 'idle', 0]);
  await expect(page.getByTestId('session-tasks-line')).toHaveCount(0);
  await expect(page.getByRole('tab', { name: 'Tasks', exact: true })).toBeVisible();
  await expect(
    page.getByTestId('session-tasks-finished')
      .locator(`[data-testid="subagent-agent-row"][data-child-run-id="${agent.child_run_id}"]`),
    'the stopped subagent stays listed with what it did',
  ).toBeVisible({ timeout: 30_000 });

  const answer = await api.sendTurn(sessionId, 'What is 17 plus 25? Reply with just the number.', 60_000);
  expect(answer.errorText).toBeNull();
  expect(answer.text).toContain('42');
});
