import { expect, test, type Page } from '@playwright/test';

import { appPath } from '../fixtures/env';

async function revealProcess(page: Page): Promise<string[]> {
  const trigger = page.getByTestId('assistant-turn-process-trigger');
  await expect(trigger).toHaveCount(1);
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  await expect(page.locator('[data-tool-call-id]:visible')).toHaveCount(0);
  await trigger.click();
  const groups = page.getByTestId('assistant-process-trigger');
  await expect(groups.first()).toBeVisible();
  for (const group of await groups.all()) {
    if (await group.getAttribute('aria-expanded') === 'false') await group.click();
  }
  const cards = page.locator('[data-tool-call-id]:visible');
  await expect(cards.first()).toBeVisible();
  const ids: string[] = [];
  for (const card of await cards.all()) {
    const id = await card.getAttribute('data-tool-call-id');
    expect(id).toBeTruthy();
    ids.push(id!);
    for (const toggle of await card.locator('button[aria-expanded="false"]').all()) {
      await toggle.click();
    }
  }
  return ids;
}

test('browser-created tool work folds without hiding its answer or reloaded details', async ({ page }) => {
  const marker = `fold-browser-${Date.now()}`;
  await page.goto(appPath('/agents'));
  const agent = page.getByTestId('agent-option').and(page.locator('[data-agent-name="Investment Research"]'));
  await expect(agent).toBeVisible();
  await agent.getByRole('button', { name: /^(Start conversation|开始对话)$/ }).click();
  await page.waitForURL(/\/sessions\/[^/]+$/);
  const sessionUrl = page.url();
  test.info().annotations.push({ type: 'retained-session', description: sessionUrl });
  const composer = page.getByTestId('composer-prompt');
  await expect(composer).toBeEnabled({ timeout: 60_000 });
  await composer.fill(
    `Please use your shell tool to write the literal text ${marker} to /workspace/fold-browser.txt, `
      + 'then read that file using a tool. This is a local workspace check, not a research question; '
      + 'do not use network tools. Briefly announce the steps and finish by reporting the text read from the file.',
  );
  await page.getByTestId('composer-submit').click();
  const answer = page.locator('[data-testid="assistant-text"]:visible').filter({ hasText: marker });
  await expect(answer.last()).toBeVisible({ timeout: 90_000 });
  await expect(page.getByTestId('run-view').getByTestId('status-pill').first())
    .toHaveAttribute('data-pulse', 'false', { timeout: 20_000 });
  await expect(page.getByTestId('assistant-turn-process-trigger'))
    .toHaveAttribute('aria-expanded', 'false', { timeout: 20_000 });
  const liveAnswer = await answer.last().innerText();
  const liveIds = await revealProcess(page);
  expect(liveIds.length).toBeGreaterThan(0);
  await expect(page.getByTestId('assistant-turn-process')).toContainText(marker);
  await page.getByTestId('assistant-turn-process-trigger').click();
  await expect(answer.last()).toHaveText(liveAnswer);

  await page.reload();
  await expect(answer.last()).toHaveText(liveAnswer, { timeout: 30_000 });
  const coldIds = await revealProcess(page);
  expect(coldIds).toEqual(liveIds);
  await expect(page.getByTestId('assistant-turn-process')).toContainText(marker);
  await page.getByTestId('assistant-turn-process-trigger').click();
  await expect(answer.last()).toHaveText(liveAnswer);
  // Keep the browser-created conversation for the operator's independent review.
});
