/**
 * Home reaches the first loaded message of a mixed-height transcript and
 * loads one earlier page. Each prepend preserves the reader's position;
 * opening, reloading and an exhausted boundary fetch no extra pages.
 */
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { apiPath, parseTimeoutEnv } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

const sessions = trackSessions();

const OLDER_PAGE_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_HISTORY_OLDER_PAGE_TIMEOUT_MS', 15_000);
const TOP_REACH_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_HISTORY_TOP_REACH_TIMEOUT_MS', 10_000);
const EXHAUSTED_OBSERVE_MS = parseTimeoutEnv('ASTRABOX_E2E_HISTORY_EXHAUSTED_OBSERVE_MS', 2_000);

test('a keyboard reader reaching the top of a reloaded long transcript loads its older history', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);

  // Match the reported 65-round import without spending model turns on data.
  const runId = Date.now();
  const prompts = Array.from({ length: 65 }, (_, index) => `历史问题 ${runId}-${String(index + 1).padStart(3, '0')}。`);
  const answers = prompts.map((_, index) => `历史回答 ${runId}-${String(index + 1).padStart(3, '0')}。`);
  const messages = prompts.flatMap((prompt, index) => {
    const blocks: Array<Record<string, unknown>> = [];
    if (index % 3 === 0) {
      blocks.push({ type: 'thinking', thinking: `整理第 ${index + 1} 轮的合成记录。`.repeat(8) });
    }
    if (index % 7 === 0) {
      const toolId = `history_tool_${runId}_${index}`;
      blocks.push(
        { type: 'tool_use', id: toolId, name: 'Bash', input: { command: 'printf synthetic' } },
        { type: 'tool_result', tool_use_id: toolId, content: 'synthetic', is_error: false },
      );
    }
    const detail = index % 2 === 0
      ? '\n\n| 项目 | 状态 |\n| --- | --- |\n' + '| 历史记录 | 已保存 |\n'.repeat(4 + index % 5)
      : '\n\n```python\n' + 'print("synthetic history")\n'.repeat(3 + index % 9) + '```';
    blocks.push({ type: 'text', text: answers[index] + detail });
    return [
      {
        source_id: `${runId}-user-${index}`, role: 'user',
        created_at: new Date(Date.UTC(2026, 0, 5, 9, index, 0)).toISOString(),
        blocks: [{ type: 'text', text: prompt }],
      },
      {
        source_id: `${runId}-assistant-${index}`, role: 'assistant', completion: 'complete',
        created_at: new Date(Date.UTC(2026, 0, 5, 9, index, 20)).toISOString(),
        blocks,
      },
    ];
  });
  expect(await api.data('POST', `/admin/sessions/${sessionId}/history-import`, { messages }))
    .toMatchObject({ imported: 130, unchanged: 0 });

  // The fixture is independent of the console's virtualized DOM. Read the
  // real API's opaque cursors and check the expected imported page contents.
  const newest = await api.getHistoryBlocks(sessionId);
  expect(newest.messages).toHaveLength(50);
  expect(messageText(newest.messages[0])).toBe(prompts[40]);
  expect(newest.has_more).toBe(true);
  const firstCursor = String(newest.next_cursor || '');
  expect(firstCursor).not.toBe('');
  const middle = await api.getHistoryBlocks(sessionId, 50, firstCursor);
  expect(middle.messages).toHaveLength(50);
  expect(messageText(middle.messages[0])).toBe(prompts[15]);
  expect(middle.has_more).toBe(true);
  const secondCursor = String(middle.next_cursor || '');
  expect(secondCursor).not.toBe('');
  const oldest = await api.getHistoryBlocks(sessionId, 50, secondCursor);
  expect(oldest.messages).toHaveLength(30);
  expect(messageText(oldest.messages[0])).toBe(prompts[0]);
  expect(messageText(oldest.messages[1])).toContain(answers[0]);
  expect(oldest.has_more).toBe(false);

  const historyPath = apiPath(`/sessions/${sessionId}/history-blocks`);
  const olderRequests: string[] = [];
  page.on('request', (observed) => {
    if (observed.method() !== 'GET') return;
    const url = new URL(observed.url());
    if (url.pathname !== historyPath) return;
    const before = url.searchParams.get('before');
    if (before) olderRequests.push(before);
  });

  const newestInput = page.getByTestId('user-message').filter({ hasText: prompts[64] });
  await openSessionView(page, sessionId);
  const scroller = page.getByTestId('session-conversation');
  await expect(scroller).toBeVisible();
  await expect(newestInput).toBeVisible();
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(newestInput).toBeVisible();
  expect(olderRequests, 'opening and reloading must not eagerly fetch older pages').toEqual([]);

  await expect(scroller, 'the transcript must be in the keyboard tab order').toHaveJSProperty('tabIndex', 0);
  await scroller.focus();
  await expect(scroller).toBeFocused();

  // No wheel or programmatic scrolling: each Home reaches the current loaded
  // boundary. The newly prepended tail must mount before checking its anchor.
  await page.keyboard.press('Home');
  await expect.poll(() => olderRequests, { timeout: OLDER_PAGE_BUDGET_MS }).toEqual([firstCursor]);
  await expect(scroller.locator(`[data-message-id="${middle.messages.at(-1)!.id}"]`))
    .toBeAttached({ timeout: OLDER_PAGE_BUDGET_MS });
  await expect(
    page.getByTestId('user-message').filter({ hasText: prompts[40] }),
    'prepending rounds 16–40 must retain round 41 in the reading viewport',
  ).toBeInViewport();

  await scroller.focus();
  await page.keyboard.press('Home');
  await expect.poll(() => olderRequests, { timeout: OLDER_PAGE_BUDGET_MS })
    .toEqual([firstCursor, secondCursor]);
  await expect(scroller.locator(`[data-message-id="${oldest.messages.at(-1)!.id}"]`))
    .toBeAttached({ timeout: OLDER_PAGE_BUDGET_MS });
  await expect(
    page.getByTestId('user-message').filter({ hasText: prompts[15] }),
    'prepending rounds 1–15 must retain round 16 in the reading viewport',
  ).toBeInViewport();

  await scroller.focus();
  await page.keyboard.press('Home');
  await expect.poll(() => scroller.evaluate((element) => element.scrollTop), {
    message: 'Home must also reach the oldest page when no earlier page remains',
    timeout: TOP_REACH_BUDGET_MS,
  }).toBeLessThanOrEqual(1);
  await expect(page.getByTestId('user-message').filter({ hasText: prompts[0] })).toBeInViewport();
  await expect(
    scroller.locator(`[data-message-id="${oldest.messages[1].id}"]`).getByTestId('assistant-message'),
    'the oldest imported answer must render, not merely arrive on the wire',
  ).toContainText(answers[0]);

  const observationDeadline = Date.now() + EXHAUSTED_OBSERVE_MS;
  while (Date.now() < observationDeadline) {
    await page.keyboard.press('Home');
    await page.waitForTimeout(250);
  }
  await expect(page.getByText(/NETWORK_ERROR|Failed to fetch/)).toHaveCount(0);
  expect(olderRequests, 'each Home loads one page and the exhausted boundary loads none')
    .toEqual([firstCursor, secondCursor]);
});
