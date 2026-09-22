/** Assistant tool calls must be visible before history can repair the live stream. */
import { randomUUID } from 'node:crypto';
import { expect, test, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
let assistantId = '';
let sessionId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});
test.afterEach(async ({ page }, info) => {
  await info.attach('assistant-tool-card-stream', {
    body: JSON.stringify({ assistantId, sessionId, stream: await aiStreamBodies(page) }),
    contentType: 'application/json',
  });
});

async function readCompletedCard(page: Page): Promise<string> {
  // A settled response that ran a tool folds its work behind one header — held
  // in the page while the turn is still open, fetched on demand after a reload.
  // Everything below is about the CARD, so the fold in front of it is opened
  // first, the way a reader opens it. Waited for rather than assumed: this turn
  // reads a file and then reports what it read, which is the shape that folds.
  await expect(
    page.getByTestId('assistant-turn-process'),
    'a settled tool response must fold into one header the reader can open',
  ).toHaveCount(1, { timeout: 60_000 });
  await revealAssistantProcess(page);
  // The card root carries the call's own id. A collapsible-shaped locator would
  // also match the turn fold and the process group the card now sits inside.
  const cards = page.getByTestId('run-view').getByTestId('assistant-message')
    .locator('[data-tool-call-id]');
  await expect(cards, 'one requested read must render one tool card').toHaveCount(1);
  const header = cards.locator('[data-slot="collapsible-trigger"]').first();
  await expect(header.locator('[data-slot="badge"]')).toHaveText(/^(Done|已完成)$/);
  const name = await header.getAttribute('aria-label');
  expect(name, 'the tool header must name the supplier tool').toBeTruthy();
  const panel = cards.locator('[data-slot="collapsible-content"]');
  if (!(await panel.isVisible())) await header.click();
  await expect(panel).toBeVisible();
  const result = panel.getByRole('heading', { name: 'Result', exact: true }).locator('..');
  await expect(result).toBeVisible();
  expect((await result.innerText()).replace(/^Result\s*/i, '').trim(),
    'a completed card must expose a nonempty tool result').not.toEqual('');
  return name!;
}

test('an Assistant tool call and its result are visible live and after reload', async ({ page, request }) => {
  const api = new AstraApi(request);
  const environmentName = await api.assistantEnvironmentName();
  const model = await api.assistantModelName(environmentName);
  const assistant = await api.createAssistant({
    display_name: `__e2e_assistant_tool_${Date.now()}`,
    environment_name: environmentName,
    model_config_override: { model_name: model },
  });
  assistantId = String(assistant.assistant_id || '');
  expect(assistantId).not.toEqual('');
  await api.waitForWorkspaceReady(assistantId, 60_000);
  sessionId = (await api.startAssistantConversation(assistantId)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  const fileName = `assistant-tool-${Date.now()}.txt`;
  // The answer is only in the real file, never in the prompt.
  const marker = `assistant-read-${randomUUID()}`;
  await api.uploadFileText(sessionId, '', fileName, marker);
  expect(await api.downloadFileText(sessionId, fileName)).toEqual(marker);

  const uncaught: string[] = [];
  page.on('pageerror', (error) => uncaught.push(error.message));
  await mirrorSseBodies(page);
  let holdHistory = false;
  let initialHistoryReads = 0;
  let releaseHistory = () => {};
  const historyHold = new Promise<void>((resolve) => { releaseHistory = resolve; });
  await page.route((url) => url.pathname.endsWith(`/sessions/${sessionId}/history-blocks`), async (route) => {
    const held = holdHistory;
    if (held) {
      await historyHold;
      await route.continue();
    } else {
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      await route.fulfill({ response });
      initialHistoryReads += 1;
    }
  });

  try {
    await openSessionView(page, sessionId);
    await expect.poll(() => initialHistoryReads).toBeGreaterThan(0);
    await expect(page.getByTestId('composer-prompt')).toBeEnabled();
    holdHistory = true;
    await sendPrompt(page, sessionId,
      `Read /workspace/${fileName} using exactly one available file-reading tool call. `
      + 'Then report the file contents. Do not edit files or call other tools.');
    await expect(page.getByTestId('assistant-text').filter({ hasText: marker })).toBeVisible({ timeout: 60_000 });
    const liveHeader = await readCompletedCard(page);
    await test.info().attach('assistant-tool-live-stream', {
      body: JSON.stringify(await aiStreamBodies(page)), contentType: 'application/json',
    });
    holdHistory = false;
    releaseHistory();
    await api.waitForSessionReady(sessionId);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('assistant-text').filter({ hasText: marker })).toBeVisible();
    expect(await readCompletedCard(page)).toEqual(liveHeader);
    expect(uncaught).toEqual([]);
  } finally {
    releaseHistory();
  }
});
