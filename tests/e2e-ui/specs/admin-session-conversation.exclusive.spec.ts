import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { normalizeRendered } from '../fixtures/renderedTranscript';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

const sessions = trackSessions();

test('the management record reads the same conversation one page at a time', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const { session_id: sessionId } = await api.startConversation(agent.agent_id);
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);

  const run = Date.now();
  const prompts = Array.from({ length: 28 }, (_, index) => `Historical question ${run}-${index}.`);
  const answers = prompts.map((_, index) => `Historical answer ${run}-${index} with **Markdown**.`);
  const messages = prompts.flatMap((prompt, index) => [
    {
      source_id: `question-${index}`, role: 'user',
      created_at: new Date(Date.UTC(2026, 0, 1, 0, index, 0)).toISOString(),
      blocks: [{ type: 'text', text: prompt }],
    },
    {
      source_id: `answer-${index}`, role: 'assistant', completion: 'complete',
      created_at: new Date(Date.UTC(2026, 0, 1, 0, index, 1)).toISOString(),
      blocks: [{ type: 'text', text: answers[index] }],
    },
  ]);
  expect(await api.data('POST', `/admin/sessions/${sessionId}/history-import`, { messages }))
    .toMatchObject({ imported: messages.length });
  await api.setPermissionMode(sessionId, 'bypassPermissions');

  const rootMarker = `ROOT_OUTPUT_${run}`;
  const childMarker = `CHILD_OUTPUT_${run}`;
  const conclusion = `The management console should show this entire answer in a single readable reply, number ${run}.`;
  const turn = await api.sendTurn(sessionId, [
    `First use Bash exactly once to run: printf '%s\\n' '${rootMarker}'.`,
    'Then use exactly one Agent tool with subagent_type=general-purpose and run_in_background=false.',
    `Tell the child Agent: use Bash to run printf '%s\\n' '${childMarker}', then reply exactly ${childMarker}.`,
    `After both return, reply exactly: ${conclusion}`,
  ].join('\n'));
  expect(turn.errorText).toBeNull();
  expect(normalizeRendered(turn.text)).toContain(normalizeRendered(conclusion));
  await api.waitForSession(sessionId, (session) => session.last_response?.outcome === 'completed');
  const { child_runs: children } = await api.listChildRuns(sessionId);
  expect(children).toHaveLength(1);

  await page.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
  await openSessionView(page, sessionId);
  const ownerChat = page.getByTestId('session-conversation');
  const ownerReply = ownerChat.getByTestId('assistant-text').filter({ hasText: conclusion });
  await expect(ownerReply).toHaveCount(1);
  const ownerText = await ownerReply.innerText();

  const adminPath = apiPath(`/admin/sessions/${sessionId}`);
  const older: string[] = [];
  const technicalRequests: string[] = [];
  page.on('request', (observed) => {
    const url = new URL(observed.url());
    if (url.pathname === `${adminPath}/trace`) technicalRequests.push(observed.url());
    if (url.pathname === `${adminPath}/history-blocks` && url.searchParams.has('before')) {
      older.push(url.searchParams.get('before')!);
    }
  });

  await page.goto(appPath(`/manage/sessions/${sessionId}`));
  const chat = page.getByTestId('session-conversation');
  await expect(chat.getByTestId('assistant-text').filter({ hasText: conclusion })).toHaveText(ownerText);
  await expect(chat.getByText(prompts[0], { exact: true })).toHaveCount(0);
  await expect(page.getByTestId('composer-submit')).toHaveCount(0);
  expect(older).toEqual([]);
  expect(technicalRequests).toEqual([]);

  await chat.getByTestId('assistant-turn-process-trigger').last().click();
  await chat.getByRole('button', { name: /^Tool calls\b/ }).click();
  const bash = chat.getByRole('button', { name: /^Bash / }).last();
  await bash.click();
  const result = chat.getByRole('heading', { name: 'Result', exact: true });
  await expect(result.locator('..')).toContainText(rootMarker);

  await page.getByRole('button', { name: 'Subagents', exact: true }).click();
  await page.getByTestId('subagent-agent-row').click();
  const drawer = page.getByTestId('subagent-transcript-drawer');
  await expect(drawer.getByTestId('assistant-text').filter({ hasText: childMarker })).toBeVisible();
  await drawer.getByRole('button', { name: /^Bash / }).click();
  await expect(drawer.getByRole('heading', { name: 'Result', exact: true }).locator('..')).toContainText(childMarker);
  await expect(drawer.getByTestId('subagent-stop-button')).toHaveCount(0);
  await drawer.getByTestId('subagent-close-button').click();

  const firstLoaded = chat.locator('[data-message-id]').first();
  const firstId = await firstLoaded.getAttribute('data-message-id');
  expect(firstId).toBeTruthy();
  await chat.focus();
  await page.keyboard.press('Home');
  await expect(chat.getByText(prompts[0], { exact: true })).toBeAttached();
  await expect(chat.locator(`[data-message-id="${firstId}"]`)).toBeInViewport();
  const visiblePrompts = await chat.getByTestId('user-message').allInnerTexts();
  expect(visiblePrompts.slice(0, prompts.length).map((prompt) => prompt.trim())).toEqual(prompts);
  const renderedAnswers = await chat.getByTestId('assistant-text').filter({ hasText: `Historical answer ${run}-` }).allInnerTexts();
  expect(renderedAnswers.map(normalizeRendered)).toEqual(answers.map(normalizeRendered));
  await expect(chat.getByText('Markdown', { exact: true }).first()).toHaveJSProperty('tagName', 'STRONG');
  expect(older).toHaveLength(1);
  expect(technicalRequests).toEqual([]);

  await page.getByRole('button', { name: 'Technical events', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Response detail' })).toBeVisible();
  expect(technicalRequests).toHaveLength(1);
});
