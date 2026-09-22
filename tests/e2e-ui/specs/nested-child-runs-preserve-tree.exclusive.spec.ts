/**
 * One real Claude Code turn proves the platform's engine-neutral ChildRun tree.
 *
 * Claude currently stores every nested Agent transcript as a flat SessionStore
 * subpath. AstraBox must reconstruct the invocation tree at the engine seam and
 * expose the same hierarchy in the live tab, after a browser reload, and after
 * the host drops its resident runtime. The assertions intentionally use only
 * platform UI terms (child-run id, parent id, depth), never Claude's native
 * agentId / parent_tool_use_id vocabulary.
 */
import { expect, test, type Page } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { appPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();

async function openAgents(page: Page): Promise<void> {
  await page.getByRole('tab', { name: /^Agents/ }).click();
  await expect(page.getByTestId('subagent-agents-panel')).toBeVisible({ timeout: 15_000 });
}

function childRows(page: Page) {
  return page.getByTestId('subagent-agents-panel').getByTestId('subagent-agent-row');
}

function childRowsAtDepth(page: Page, depth: number) {
  return page
    .getByTestId('subagent-agents-panel')
    .locator(`[data-testid="subagent-agent-row"][data-subagent-depth="${depth}"]`);
}

async function expectTwoLevelTree(page: Page, label: string): Promise<{ rootId: string; nestedId: string }> {
  const rows = childRows(page);
  await expect(rows, `${label}: exactly two child runs should be visible`).toHaveCount(2, {
    timeout: 30_000,
  });
  const root = childRowsAtDepth(page, 1);
  const nested = childRowsAtDepth(page, 2);
  await expect(root, `${label}: the first child run should be the tree root`).toHaveCount(1);
  await expect(nested, `${label}: the nested child run should remain depth two`).toHaveCount(1);

  const rootId = await root.getAttribute('data-child-run-id');
  const nestedId = await nested.getAttribute('data-child-run-id');
  const nestedParentId = await nested.getAttribute('data-parent-child-run-id');
  expect(rootId, `${label}: root row must expose its canonical child-run id`).toBeTruthy();
  expect(nestedId, `${label}: nested row must expose its canonical child-run id`).toBeTruthy();
  expect(nestedParentId, `${label}: nested row must expose its canonical parent id`).toBe(rootId);
  return { rootId: rootId!, nestedId: nestedId! };
}

test('nested child runs preserve their tree live, after reload, and after runtime recovery', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const leafMarker = `NESTED_CHILD_LEAF_${runId}`;
  const parentMarker = `NESTED_CHILD_PARENT_${runId}`;

  const expectDurableTranscripts = async (rootId: string, nestedId: string, label: string) => {
    const tree = await api.listChildRuns(sessionId);
    expect(tree.session_id).toBe(sessionId);
    expect(tree.child_runs, `${label}: API and UI must expose the same two children`).toHaveLength(2);
    expect(tree.child_runs.find((child) => child.child_run_id === rootId)?.depth).toBe(1);
    const nestedChild = tree.child_runs.find((child) => child.child_run_id === nestedId);
    expect(nestedChild?.depth).toBe(2);
    expect(nestedChild?.parent_child_run_id).toBe(rootId);

    for (const [childId, marker] of [[rootId, parentMarker], [nestedId, leafMarker]]) {
      const transcript = await api.getChildRunMessages(sessionId, childId);
      expect(transcript.session_id).toBe(sessionId);
      expect(transcript.child_run_id).toBe(childId);
      expect(
        transcript.messages.some((message) => message.role === 'assistant'
          && message.content.some((block) => block.type === 'text'
            && typeof block.text === 'string' && block.text.includes(marker))),
        `${label}: child ${childId} must retain its actual assistant completion, not its prompt or summary`,
      ).toBe(true);

      if (childId === nestedId) {
        const blocks = transcript.messages.flatMap((message) => message.content);
        const bash = blocks.filter((block) => block.type === 'tool_use' && block.name === 'Bash');
        expect(bash, `${label}: the nested transcript must retain its real Bash invocation`).toHaveLength(1);
        expect(bash[0].id).toBeTruthy();
        const results = blocks.filter((block) => block.type === 'tool_result'
          && block.tool_use_id === bash[0].id);
        expect(results, `${label}: Bash must have its matching durable result`).toHaveLength(1);
        expect(results[0].is_error).not.toBe(true);
        expect(JSON.stringify(results[0].content), `${label}: Bash output must carry the leaf proof`).toContain(leafMarker);
      }
    }
  };

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });

  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'bypassPermissions');
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto(appPath(`/sessions/${sessionId}`));
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 30_000 });

  // The subject here is the TREE, not whether a model chooses to nest. The
  // root only spawns the first level; the second exists only if the root
  // forwards the instruction to it, and a summarised forward loses the nesting
  // and leaves one row where the spec asserts two. So the forwarded text is
  // given verbatim, as a block the root is told to copy rather than to
  // paraphrase.
  const forwarded = [
    `You are the first-level Agent of E2E tree ${runId}. Follow every step exactly.`,
    'Call exactly one Agent with subagent_type=general-purpose and run_in_background=false.',
    'Give that second-level Agent exactly this instruction, copied verbatim:',
    '---',
    'Use Bash exactly once to run:',
    "python3 - <<'PY'",
    `print('${leafMarker}')`,
    'PY',
    `Then answer exactly ${leafMarker}. Do not call any other tools.`,
    '---',
    `After that Agent returns, answer exactly ${parentMarker}.`,
    'Do not call any other tools.',
  ].join('\n');
  const prompt = [
    `E2E recursive child-run tree ${runId}. Follow every step exactly.`,
    'Call exactly one Agent with subagent_type=general-purpose and run_in_background=true.',
    'Give that Agent exactly this instruction, copied verbatim, with no summary',
    'and nothing added:',
    '===',
    forwarded,
    '===',
    'Do not call any other tools yourself.',
  ].join('\n');

  const composer = page.getByTestId('composer-prompt');
  await expect(composer).toBeEnabled({ timeout: 15_000 });
  await composer.fill(prompt);
  await page.getByTestId('composer-submit').click();
  const queuedPrompt = page.getByTestId('composer-queue').filter({ hasText: leafMarker });
  const consumedPrompt = page.getByTestId('user-message').filter({ hasText: leafMarker }).last();
  await expect(
    queuedPrompt.or(consumedPrompt).first(),
    'the prompt should appear in the platform queue or the consumed transcript',
  ).toBeVisible({ timeout: 15_000 });

  await api.waitForAssistantMessageMatching(
    sessionId,
    0,
    (message) => messageText(message).includes(parentMarker),
    120_000,
  );
  await api.waitForSessionReady(sessionId);

  // Same-tab evidence: no reload or history fetch has reconstructed this tree.
  await openAgents(page);
  const liveTree = await expectTwoLevelTree(page, 'live');

  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 30_000 });
  await openAgents(page);
  const reloadTree = await expectTwoLevelTree(page, 'reload');
  expect(reloadTree, 'reload must preserve both public child identities').toEqual(liveTree);
  await expectDurableTranscripts(reloadTree.rootId, reloadTree.nestedId, 'reload');

  const nested = childRowsAtDepth(page, 2);
  await nested.click();
  const drawer = page.getByTestId('subagent-transcript-drawer');
  await expect(drawer).toBeVisible({ timeout: 15_000 });
  await expect(drawer, 'the nested child transcript should contain its real Bash output').toContainText(
    leafMarker,
    { timeout: 30_000 },
  );
  const bashTool = drawer.getByRole('button', { name: /^Bash / });
  await expect(bashTool, 'the nested drawer must render its actual Bash tool card').toHaveCount(1);
  await expect(bashTool).toHaveAttribute('aria-expanded', 'false');
  await bashTool.click();
  await expect(bashTool).toHaveAttribute('aria-expanded', 'true');
  const bashResult = drawer.getByRole('heading', { name: 'Result', exact: true });
  await expect(bashResult, 'Bash must render a successful result, not a summary').toBeVisible();
  await expect(bashResult.locator('..')).toContainText(leafMarker);
  await page.getByTestId('subagent-close-button').click();

  const root = childRowsAtDepth(page, 1);
  await root.click();
  await expect(
    page.getByTestId('subagent-transcript-drawer'),
    'the parent child transcript should contain its durable completion',
  ).toContainText(parentMarker, { timeout: 30_000 });
  const agentTool = drawer.getByRole('button', { name: /^Agent / });
  await expect(agentTool, 'the parent drawer must render its actual nested Agent tool card').toHaveCount(1);
  await expect(agentTool).toHaveAttribute('aria-expanded', 'false');
  await agentTool.click();
  await expect(agentTool).toHaveAttribute('aria-expanded', 'true');
  const agentResult = drawer.getByRole('heading', { name: 'Result', exact: true });
  await expect(agentResult, 'Agent must render its successful child result, not a summary').toBeVisible();
  await expect(agentResult.locator('..')).toContainText(leafMarker);
  await page.getByTestId('subagent-close-button').click();

  // A cold host-side runtime must not be required to rebuild durable lineage.
  await api.adminEvictRuntime(sessionId);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 30_000 });
  await openAgents(page);
  const recoveredTree = await expectTwoLevelTree(page, 'runtime recovery');
  expect(recoveredTree, 'runtime recovery must preserve both public child identities').toEqual(liveTree);
  await expectDurableTranscripts(recoveredTree.rootId, recoveredTree.nestedId, 'runtime recovery');
});
