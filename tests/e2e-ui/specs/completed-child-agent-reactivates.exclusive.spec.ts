/** The same completed Claude Agent runs again through SendMessage and survives reload. */
import { expect, test, type Page, type Response } from '@playwright/test';

import {
  AstraApi,
  answersInput,
  messageToolCalls,
  type ChildRunMessagePage,
  type InputReceipt,
} from '../fixtures/astraApi';
import { nativeRootEntries, object } from '../fixtures/nativeMcpServer';
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

/** The receipt of a message the page sent, checked against the id the page minted for it. */
async function receiptOf(response: Response): Promise<InputReceipt> {
  const receipt = (await response.json() as { data: InputReceipt }).data;
  const sent = response.request().postDataJSON() as { client_message_id?: string };
  expect(receipt.client_message_id, 'the receipt names the message the page sent').toBe(sent.client_message_id);
  return receipt;
}

/** Claude's own content blocks of the root conversation, in transcript order. */
function nativeRootBlocks(sessionId: string): Record<string, unknown>[] {
  return nativeRootEntries(sessionId).flatMap(({ entry }) => {
    if (entry.isSidechain === true || !entry.message) return [];
    const content = object(entry.message).content;
    return Array.isArray(content) ? content.map(object) : [];
  });
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
  const resumeReceipt = await receiptOf(await sendPrompt(page, sessionId, [
    'Resume the one general-purpose Agent that just completed. Do not create another Agent.',
    'Use SendMessage exactly once with the previous Agent ID in its to field.',
    'Send it this complete task verbatim:',
    '---',
    'Use Bash exactly once to run the following command, wait for it, then give a short completion report:',
    childCommand,
    '---',
    'After SendMessage succeeds, give a brief acknowledgement and end your turn.',
    'Do not use Bash, TaskOutput, or any other tool yourself.',
  ].join('\n')));

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
  // The held native child keeps the runtime PROCESSING after the foreground response ends.
  await expect.poll(async () => {
    const detail = await api.getSession(sessionId);
    return detail.state === 'PROCESSING' && (detail.current_responses ?? []).length === 0
      && !detail.pending_interaction;
  }, { timeout: 15_000, message: 'the foreground ends while the resumed child remains live' }).toBe(true);
  expect((await api.listChildRuns(sessionId)).child_runs.map((child) => ({
    id: child.child_run_id, closed: child.closed, active: child.active,
  })), 'the resumed child is still working after the foreground ends').toEqual([{ id: childId, closed: false, active: true }]);
  await expect(
    sameChildRow.locator('svg.animate-spin'),
    'the Agents panel must show the resumed child working after the foreground finishes',
  ).toHaveCount(1);
  await expect(page.getByTestId('composer-prompt')).toBeEnabled();
  const rootHistory = await api.getMessages(sessionId);
  const sendCalls = rootHistory.messages.flatMap((message) => messageToolCalls(message)
    .filter((call) => call.toolName === 'SendMessage')
    .map((call) => ({ message, call })));
  expect(sendCalls, 'reactivation must use the native SendMessage tool exactly once').toHaveLength(1);
  const { message: parentReply, call: sendCall } = sendCalls[0]!;
  expect(sendCall.state, 'the actual SendMessage call must have a successful matching result').toBe('output-available');
  // Claude's own transcript is the independent record of the same call.
  await expect.poll(() => {
    const nativeBlocks = nativeRootBlocks(sessionId);
    return {
      calls: nativeBlocks.filter((block) => block.type === 'tool_use' && block.name === 'SendMessage')
        .map((block) => block.id),
      failed: nativeBlocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === sendCall.toolCallId)
        .map((block) => block.is_error === true),
    };
  }, {
    timeout: 30_000,
    message: 'history shows the one SendMessage call Claude made, and Claude recorded its successful result',
  }).toEqual({ calls: [sendCall.toolCallId], failed: [false] });
  expect(
    answersInput(parentReply, resumeReceipt.input_id),
    'the resumed task must retain its parent response: the reply to the resume message',
  ).toBe(true);
  const sendCallIndex = parentReply.parts.findIndex((part) => part.toolCallId === sendCall.toolCallId);
  expect(parentReply.parts.slice(sendCallIndex + 1).some((part) => (
    part.type === 'text' && String(part.text || '').trim()
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
  await sameChildRow.click();
  await expect(page.getByTestId('subagent-transcript-drawer')).toBeVisible();
  const finalTranscript = await api.getChildRunMessages(sessionId, childId);
  expect(hasSuccessfulToolOutput(finalTranscript, firstMarker)).toBe(true);
  expect(hasSuccessfulToolOutput(finalTranscript, secondMarker)).toBe(true);
});
