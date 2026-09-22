import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';


function commandName(value: unknown): string {
  if (typeof value === 'string') return value.replace(/^\/+/, '').trim();
  if (!value || typeof value !== 'object') return '';
  const item = value as Record<string, unknown>;
  return String(item.name || item.command || '').replace(/^\/+/, '').trim();
}

function sorted(values: string[]): string[] {
  return [...values].sort((left, right) => left.localeCompare(right));
}

function expectCompleteCommandMetadata(session: Record<string, unknown>): string[] {
  expect(Array.isArray(session.slash_command_details), 'slash_command_details must be present').toBe(true);
  expect(Array.isArray(session.slash_commands), 'slash_commands must be present').toBe(true);
  const detailNames = (session.slash_command_details as unknown[]).map(commandName).filter(Boolean);
  const legacyNames = (session.slash_commands as unknown[]).map(commandName).filter(Boolean);
  expect(detailNames.length, 'slash_command_details must not be empty').toBeGreaterThan(0);
  expect(new Set(detailNames).size, 'READY command names must be deduplicated').toBe(detailNames.length);
  expect(
    sorted(legacyNames),
    'slash_commands and slash_command_details must expose the same names',
  ).toEqual(sorted(detailNames));
  return detailNames;
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('new agent_chat is READY with slash commands visible in the composer', async ({ request, page }) => {
  await test.step('deployed title timeout admits a slow response and honors a shorter override', async () => {
    const output = execFileSync('docker', [
      'exec', '-i', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-',
    ], {
      input: readFileSync(resolve(__dirname, '../fixtures/titleModelTimeout.py'), 'utf8'),
      encoding: 'utf8',
      timeout: 30_000,
    });
    expect(JSON.parse(output)).toEqual({
      configured_timeout_seconds: 60,
      slow_response_seconds: 9,
      short_override: 'ReadTimeout',
      requests_per_attempt: 1,
    });
  });
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  try {
    await api.waitForSessionReady(sessionId);
    const ready = await api.getSession(sessionId);
    const initialTitles = new Set(
      [ready.title, agent.name, ready.template_name]
        .map((value) => String(value || '').trim())
        .filter(Boolean),
    );
    expect(ready.state, 'command metadata and READY must be committed together').toBe('READY');
    const readyNames = expectCompleteCommandMetadata(ready);
    expect(readyNames, 'the built-in compact command must be present at READY').toContain('compact');

    await openSessionView(page, sessionId);
    const composer = page.getByTestId('composer-prompt');
    const commandTrigger = page.getByTestId('composer-command-menu-trigger');
    await expect(
      commandTrigger,
      'slash commands must have a visible pointer-accessible entry point',
    ).toBeVisible();
    await commandTrigger.click();
    const menu = page.getByTestId('slash-command-menu');
    await expect(menu).toBeVisible();
    const [menuBox, composerBox] = await Promise.all([
      menu.boundingBox(),
      composer.boundingBox(),
    ]);
    expect(menuBox, 'the command menu must have a painted box').not.toBeNull();
    expect(composerBox, 'the composer must have a painted box').not.toBeNull();
    expect(menuBox!.height, 'the command menu must paint a non-empty surface').toBeGreaterThan(0);
    expect(menuBox!.y, 'the command menu must stay inside the viewport').toBeGreaterThanOrEqual(0);
    expect(
      menuBox!.y + menuBox!.height,
      'the command menu must be positioned above the composer',
    ).toBeLessThanOrEqual(composerBox!.y + 1);
    expect(
      await menu.evaluate((element) => {
        const rect = element.getBoundingClientRect();
        const pointX = rect.left + Math.min(24, rect.width / 2);
        const pointY = rect.top + Math.min(24, rect.height / 2);
        const painted = document.elementFromPoint(pointX, pointY);
        return painted !== null && element.contains(painted);
      }),
      'the command menu must not be clipped or covered by a layout ancestor',
    ).toBe(true);
    const menuNames = (await menu.getByTestId('slash-command-name').allTextContents())
      .map(commandName);
    expect(menuNames).toContain('compact');
    await composer.fill('');

    const before = await api.assistantCount(sessionId);
    const runId = Date.now();
    const responseMarker = `SDK_AUTO_TITLE_DONE_${runId}`;
    const delivery = await sendPrompt(page, sessionId, [
      `请帮我制定一个 PostgreSQL 慢查询索引优化方案，测试编号 ${runId}。`,
      '不要使用任何工具。',
      `请用一句简短中文回答，并在结尾原样包含 ${responseMarker}。`,
    ].join('\n'));
    expect(delivery.status(), 'the titleable first-turn prompt must be accepted').toBe(200);
    await api.waitForAssistantMessageCount(sessionId, before, 180_000);
    await api.waitForSessionReady(sessionId);
    await expect(
      page.getByTestId('assistant-message').filter({ hasText: responseMarker }),
      'the completed first turn must render before checking its generated title',
    ).toHaveCount(1);

    const after = await api.getSession(sessionId);
    expect(
      sorted(expectCompleteCommandMetadata(after)),
      'the first live turn must not erase command metadata',
    ).toEqual(sorted(readyNames));

    // Generation metadata is operator-only; owners see the resulting title.
    await expect.poll(async () => {
      const detail = await api.adminSessionDetail(sessionId);
      return {
        source: detail.title_source,
        status: detail.title_generation_status,
        turnIndex: detail.title_generation_turn_index,
        error: detail.title_generation_error,
      };
    }, {
      timeout: 60_000,
      intervals: [250, 500, 1_000],
      message: 'the first completed topic-bearing turn must persist its automatic title',
    }).toMatchObject({ source: 'first_turn_model', status: 'COMPLETED', turnIndex: 1 });
    const titled = await api.getSession(sessionId);
    const generatedTitle = String(titled.title || '').trim();
    expect(generatedTitle).not.toEqual('');
    expect(initialTitles.has(generatedTitle)).toBe(false);
    await expect(
      page.getByTestId('run-view').locator('header h1'),
      'the already-open header must refresh to the persisted title without a reload',
    ).toHaveText(generatedTitle, { timeout: 30_000 });
    await expect(
      page.locator(`[data-testid="session-row"][data-session-id="${sessionId}"]`)
        .getByText(generatedTitle, { exact: true }),
      'the sidebar must refresh to the same persisted title',
    ).toHaveCount(1, { timeout: 30_000 });
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
