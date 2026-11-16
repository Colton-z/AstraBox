/**
 * A halo, a ring or a focus ring that a container cuts off reads as a broken
 * product at a glance: half a breathing dot, a focused row with no outline on
 * three sides. Nothing fails when it happens, so it is checked on the real
 * pages rather than left to whoever notices next.
 *
 * The pages are walked rather than sampled: a decoration is cut by whatever
 * happens to contain it, so the property is about every page and every state a
 * conversation passes through, not about the component that paints it.
 */
import { expect, test, type Page } from '@playwright/test';
import { AstraApi } from '../fixtures/astraApi';
import { findClippedDecorations } from '../fixtures/clippedDecorations';
import { appPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled, openSessionView, sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();

const DESKTOP = { width: 1280, height: 800 };
const PHONE = { width: 390, height: 844 };

/** Every page a signed-in administrator can open without a record of their own. */
const PAGES = [
  '/agents', '/assistants', '/artifacts', '/connections',
  '/manage/agents', '/manage/agents/new', '/manage/assistants', '/manage/assistants/new',
  '/manage/assistant-defaults', '/manage/agent-categories', '/manage/connectors', '/manage/credentials',
  '/manage/deployments', '/manage/deployments/new', '/manage/environment', '/manage/errors', '/manage/logs',
  '/manage/mcp-tokens', '/manage/model-accounts', '/manage/runtime-profiles', '/manage/runtime-profiles/new',
  '/manage/sandboxes', '/manage/sessions', '/manage/system',
];

async function settle(page: Page): Promise<void> {
  await page.waitForLoadState('networkidle').catch(() => undefined);
  // Enter motion moves a block by a few pixels while it fades in.
  await page.waitForTimeout(500);
}

for (const [name, viewport] of [['a desktop window', DESKTOP], ['a phone', PHONE]] as const) {
  test(`no page cuts off a decoration on ${name}`, async ({ page, request }) => {
    const api = new AstraApi(request);
    const agent = await api.defaultAgent();
    await page.setViewportSize(viewport);
    const cut: string[] = [];
    for (const route of [...PAGES, `/agents/${agent.agent_id}`, `/manage/agents/${agent.agent_id}`]) {
      await page.goto(appPath(route), { waitUntil: 'domcontentloaded' });
      await settle(page);
      for (const finding of await findClippedDecorations(page)) cut.push(`${route}: ${finding}`);
    }
    expect(cut, 'decorations cut off by a container').toEqual([]);
  });
}

test('a conversation cuts off no decoration while it waits, works and shows its work', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  sessions.push(created.session_id);
  await page.setViewportSize(DESKTOP);
  await openSessionView(page, created.session_id);
  await expectComposerEnabled(page);
  const cut: string[] = [];
  const read = async (state: string) => {
    for (const finding of await findClippedDecorations(page)) cut.push(`${state}: ${finding}`);
  };

  await sendPrompt(page, created.session_id,
    'Run `sleep 5; seq 1 40`, then `sleep 5; seq 41 80`, then `sleep 5; seq 81 120`, each as its own tool call. '
    + 'Then reply with the single word done.');
  // The line that holds the reply's place: the reported defect was its dot.
  await expect(page.getByTestId('response-pending').first()).toBeVisible({ timeout: 60_000 });
  await read('waiting for the reply');

  const process = page.locator('[data-streaming="true"] [data-testid="assistant-process-trigger"]').last();
  await expect(process).toBeVisible({ timeout: 120_000 });
  await read('working, folded');

  await process.click();
  const cards = page.locator(
    '[data-streaming="true"] [data-testid="assistant-process-panel"] [data-slot="collapsible-trigger"]',
  );
  await expect(cards.first()).toBeVisible({ timeout: 30_000 });
  await cards.first().click();
  await settle(page);
  await read('working, opened');

  expect(cut, 'decorations cut off by a container').toEqual([]);
});
