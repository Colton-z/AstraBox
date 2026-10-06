import { test, expect } from '@playwright/test';

import { engineCases } from '../fixtures/engineProfile';
import { simplePage } from '../fixtures/artifactPages';
import { artifactResources, startArtifactScene, publishArtifact, artifactFrame, marker } from '../fixtures/artifactScenario';

const resources = artifactResources();

for (const engine of engineCases()) {
  test(`${engine.engine_kind}: an Agent publishes an artifact through its native delivery path`, async ({ page, request }) => {
    const scene = await startArtifactScene(page, request, resources, { engine: engine.engine_kind });
    const value = marker(engine.engine_kind);
    const title = marker(`${engine.engine_kind} artifact`);
    await publishArtifact(page, scene, title, simplePage(value));
    await expect(artifactFrame(page).locator('#marker')).toHaveText(value);
    await page.reload();
    const card = page.getByTestId('artifact-tool-card').filter({ hasText: title });
    await expect(card, 'durable history must retain the artifact outside process folds').toBeVisible();
    await card.getByRole('button', { name: 'Open', exact: true }).click();
    await expect(artifactFrame(page).locator('#marker')).toHaveText(value);
  });
}

test('Assistant: a conversation publishes an artifact through its workspace MCP binding', async ({ page, request }) => {
  const scene = await startArtifactScene(page, request, resources, { assistant: true });
  const value = marker('assistant');
  const title = marker('Assistant artifact');
  await publishArtifact(page, scene, title, simplePage(value));
  await expect(artifactFrame(page).locator('#marker')).toHaveText(value);
  await page.reload();
  const card = page.getByTestId('artifact-tool-card').filter({ hasText: title });
  await expect(card).toBeVisible();
  await card.getByRole('button', { name: 'Open', exact: true }).click();
  await expect(artifactFrame(page).locator('#marker')).toHaveText(value);
});
