/** The same completed Claude Agent runs again through SendMessage and survives reload. */
import { expect, test, type Page } from '@playwright/test';

import { AstraApi, type ChildRunMessagePage } from '../fixtures/astraApi';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
  agentId = '';
});

async function openAgents(page: Page): Promise<void> {
  await page.getByRole('tab', { name: /^Agents/ }).click();
  await expect(page.getByTestId('subagent-agents-panel')).toBeVisible();
}

function hasSuccessfulToolOutput(transcript: ChildRunMessagePage, marker: string): boolean {
  return transcript.messages.some((message) => message.content.some((block) => (
    block.type === 'tool_result'
    && block.is_error !== true
    && JSON.stringify(block.content).includes(marker)
  )));
}

test('SendMessage reopens the same completed child Agent until its new task finishes', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = Date.now();
  const firstMarker = `CHILD_FIRST_${runId}`;
  const secondMarker = `CHILD_RESUMED_${runId}`;
  const startedPath = `/workspace/.astrabox-e2e-reactivated-${runId}.started`;
  const releasePath = `/workspace/.astrabox-e2e-reactivated-${runId}.release`;
  const agent = await api.createColdTestAgent(`e2e-child-reactivation-${runId}`);
  agentId = agent.agent_id;
  const session = await api.startConversation(agentId);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'bypassPermissions');
  await openSessionView(page, sessionId);

  await sendPrompt(page, sessionId, [
    'Launch exactly one general-purpose Agent with run_in_background=true.',
    `Its complete task is: use Bash once to run python3 -c 'print("${firstMarker}")', then give a short completion report.`,
    'Do not launch another Agent or use other tools yourself. End your turn after launch.',
    'Keep the returned Agent ID so we can resume this same Agent later.',
  ].join('\n'));
  const firstChildren = await api.waitForChildRuns(
    sessionId,
    (children) => children.length === 1 && children[0]!.closed,
    60_000,
  );
  const childId = firstChildren[0]!.child_run_id;
  expect(firstChildren[0]!.engine_kind).toBe('claude_code');
  expect(firstChildren[0]!.engine_status, 'the first task must complete, not merely close').toBe('completed');
  await expect.poll(async () => hasSuccessfulToolOutput(
    await api.getChildRunMessages(sessionId, childId), firstMarker,
  ), { timeout: 30_000, message: 'the first child task must have actually executed Bash' }).toBe(true);
  const firstTranscript = await api.getChildRunMessages(sessionId, childId);
  expect(firstTranscript.messages.every((message) => Boolean(message.message_id))).toBe(true);
  const previousMessageIds = new Set(firstTranscript.messages.map((message) => message.message_id));
  await api.waitForSessionReady(sessionId);
  await openAgents(page);
  const rows = page.getByTestId('subagent-agents-panel').getByTestId('subagent-agent-row');
  await expect(rows).toHaveCount(1);
  const sameChildRow = page.locator(`[data-testid="subagent-agent-row"][data-child-run-id="${childId}"]`);
  await expect(sameChildRow.locator('svg.animate-spin')).toHaveCount(0);
  await expect(sameChildRow.getByText('completed', { exact: true })).toBeVisible();

  const childCommand = [
    "python3 - <<'PY'",
    'from pathlib import Path',
    'import time',
    `Path(${JSON.stringify(startedPath)}).write_text("started")`,
    `release = Path(${JSON.stringify(releasePath)})`,
    'deadline = time.monotonic() + 60',
    'while not release.exists():',
    '    if time.monotonic() >= deadline:',
    '        raise TimeoutError("test did not release the resumed child")',
    '    time.sleep(0.1)',
    `print(${JSON.stringify(secondMarker)})`,
    'PY',
  ].join('\n');
  await sendPrompt(page, sessionId, [
    'Resume the one general-purpose Agent that just completed. Do not create another Agent.',
    'Use SendMessage exactly once with the previous Agent ID in its to field.',
    'Send it this complete task verbatim:',
    '---',
    'Use Bash exactly once to run the following command, wait for it, then give a short completion report:',
    childCommand,
    '---',
    'After SendMessage succeeds, give a brief acknowledgement and end your turn.',
    'Do not use Bash, TaskOutput, or any other tool yourself.',
  ].join('\n'));

  // The supplier executes the gate. The file API only observes it until the
  // API and visible row have both proved reactivation of the original child.
  await expect.poll(async () => {
    const files = await platform.listFiles(sessionId, '/workspace', 10_000);
    if (!files.entries?.some((entry) => entry.name === startedPath.split('/').at(-1) && entry.kind === 'file')) return false;
    return await api.downloadFileText(sessionId, startedPath, 10_000) === 'started';
  }, { timeout: 45_000, intervals: [500, 1_000], message: 'the resumed Agent must reach its real Bash gate' }).toBe(true);
  await expect.poll(async () => (await api.listChildRuns(sessionId)).child_runs.map((child) => ({
    id: child.child_run_id, closed: child.closed,
  })), { timeout: 15_000, message: 'running again must reuse the original public child identity' })
    .toEqual([{ id: childId, closed: false }]);
  await expect(rows).toHaveCount(1);
  await expect(sameChildRow.locator('svg.animate-spin')).toHaveCount(1);
  await expect(sameChildRow.getByText('completed', { exact: true }), 'the old completed badge must not survive reactivation').toHaveCount(0);
  await expect.poll(async () => {
    const detail = await api.getSession(sessionId);
    return !detail.current_turn_id && !detail.pending_interaction
      && ['READY', 'BACKGROUND_RUNNING'].includes(String(detail.state));
  }, { timeout: 15_000, message: 'the foreground ends while the resumed child remains live' }).toBe(true);
  await expect(
    page.locator('header').getByText('Running in background', { exact: true }),
    'the header must show the resumed child after the foreground finishes',
  ).toBeVisible();
  await expect(page.getByTestId('composer-prompt')).toBeEnabled();
  const rootHistory = await api.getMessages(sessionId);
  const rootBlocks = rootHistory.messages.flatMap((message) => message.blocks || []);
  const sendCalls = rootBlocks.filter((block) => block.type === 'tool_use' && block.name === 'SendMessage');
  expect(sendCalls, 'reactivation must use the native SendMessage tool exactly once').toHaveLength(1);
  expect(rootBlocks.filter((block) => (
    block.type === 'tool_result' && block.tool_use_id === sendCalls[0]!.id && block.is_error !== true
  )), 'the actual SendMessage call must have a successful matching result').toHaveLength(1);
  const parentReply = rootHistory.messages.find((message) => (
    message.role === 'assistant'
    && (message.blocks || []).some((block) => block.type === 'tool_use' && block.id === sendCalls[0]!.id)
  ));
  expect(parentReply, 'the resumed task must retain its parent response').toBeDefined();
  const parentBlocks = parentReply!.blocks || [];
  const sendResultIndex = parentBlocks.findIndex((block) => (
    block.type === 'tool_result' && block.tool_use_id === sendCalls[0]!.id
  ));
  expect(sendResultIndex, 'the parent response must contain its SendMessage result').toBeGreaterThanOrEqual(0);
  expect(parentBlocks.slice(sendResultIndex + 1).some((block) => (
    block.type === 'text' && String(block.text || '').trim()
  )), 'the parent must answer after SendMessage, not just expose tool output').toBe(true);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await openAgents(page);
  await expect(rows).toHaveCount(1);
  await expect(sameChildRow.locator('svg.animate-spin'), 'reload must not restore the previous terminal state').toHaveCount(1);
  await expect(sameChildRow.getByText('completed', { exact: true })).toHaveCount(0);
  await api.uploadFileText(
    sessionId, '/workspace', releasePath.split('/').at(-1)!, 'release', 10_000,
  );
  expect(await api.downloadFileText(sessionId, releasePath, 10_000)).toBe('release');
  await expect.poll(async () => hasSuccessfulToolOutput(
    await api.getChildRunMessages(sessionId, childId), secondMarker,
  ), { timeout: 45_000, message: 'the same child transcript must retain the second successful Bash result' }).toBe(true);
  await expect.poll(async () => {
    const transcript = await api.getChildRunMessages(sessionId, childId);
    const resultIndex = transcript.messages.findIndex((message) => message.content.some((block) => (
      block.type === 'tool_result' && block.is_error !== true && JSON.stringify(block.content).includes(secondMarker)
    )));
    return resultIndex >= 0 && transcript.messages.slice(resultIndex + 1).some((message) => (
      message.role === 'assistant'
      && Boolean(message.message_id)
      && !previousMessageIds.has(message.message_id)
      && message.content.some((block) => block.type === 'text' && String(block.text || '').trim())
    ));
  }, { timeout: 30_000, message: 'the resumed child must answer after its new tool result, not reuse the old answer' }).toBe(true);
  await expect.poll(async () => (await api.listChildRuns(sessionId)).child_runs.map((child) => ({
    id: child.child_run_id, closed: child.closed, status: child.engine_status,
  })), { timeout: 30_000 }).toEqual([{ id: childId, closed: true, status: 'completed' }]);
  await api.waitForSessionReady(sessionId);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await openAgents(page);
  await expect(rows).toHaveCount(1);
  await expect(sameChildRow.locator('svg.animate-spin')).toHaveCount(0);
  await expect(sameChildRow.getByText('completed', { exact: true })).toBeVisible();
  await expect(page.locator('header').getByText('Running in background', { exact: true })).toHaveCount(0);
  await sameChildRow.click();
  await expect(page.getByTestId('subagent-transcript-drawer')).toBeVisible();
  const finalTranscript = await api.getChildRunMessages(sessionId, childId);
  expect(hasSuccessfulToolOutput(finalTranscript, firstMarker)).toBe(true);
  expect(hasSuccessfulToolOutput(finalTranscript, secondMarker)).toBe(true);
});
