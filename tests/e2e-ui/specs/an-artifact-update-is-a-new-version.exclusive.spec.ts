import { test, expect } from '@playwright/test';

import { expectStoredVersion, storedVersions } from '../fixtures/artifactsApi';
import { simplePage } from '../fixtures/artifactPages';
import { artifactResources, startArtifactScene, publishArtifact, updateArtifact, artifactFrame, marker } from '../fixtures/artifactScenario';

const resources = artifactResources();

test('an artifact update is a new immutable version', async ({ page, request }) => {
  const scene = await startArtifactScene(page, request, resources);
  const first = marker('VERSION_ONE');
  const second = marker('VERSION_TWO');
  const content = simplePage(first);
  const artifact = await publishArtifact(page, scene, marker('Versions'), content);
  await expect(artifactFrame(page).locator('#marker')).toHaveText(first);
  await updateArtifact(page, scene, artifact.artifact_id, first, second);
  await expect(artifactFrame(page).locator('#marker')).toHaveText(second, { timeout: 30_000 });
  const selector = page.getByRole('combobox', { name: 'Version history', exact: true });
  await expect(selector.locator('option')).toHaveCount(2);
  expect((await selector.locator('option').allTextContents()).sort()).toEqual(['Version 1', 'Version 2']);
  await selector.selectOption('1');
  await expect(artifactFrame(page).locator('#marker')).toHaveText(first);
  await selector.selectOption('2');
  await expect(artifactFrame(page).locator('#marker')).toHaveText(second);
  expect((await scene.artifacts.versions(artifact.artifact_id)).map((item) => item.version).sort()).toEqual([1, 2]);
  await expectStoredVersion(scene.artifacts, artifact.artifact_id, 1, content);
  await expectStoredVersion(scene.artifacts, artifact.artifact_id, 2, content.replaceAll(first, second));
  expect(storedVersions(artifact.artifact_id)).toHaveLength(2);
});
