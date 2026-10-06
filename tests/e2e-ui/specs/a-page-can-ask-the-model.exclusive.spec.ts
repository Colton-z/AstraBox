import { test, expect } from '@playwright/test';

import { samplePage } from '../fixtures/artifactPages';
import { artifactResources, startArtifactScene, publishArtifact, artifactFrame, marker } from '../fixtures/artifactScenario';

const resources = artifactResources();

test('a page asks the model only after consent and refuses an anonymous audience', async ({ page, request }) => {
  const scene = await startArtifactScene(page, request, resources);
  const token = marker('MODEL_ANSWER');
  const artifact = await publishArtifact(page, scene, marker('Ask the model'), samplePage(
    `Reply with exactly this token and no other text: ${token}`,
  ), { sample: {} });
  const calls = scene.artifacts.observeCalls(page, artifact.artifact_id, 'sample');
  const frame = artifactFrame(page);
  await frame.getByRole('button', { name: 'Ask model', exact: true }).click();
  const consent = page.getByRole('dialog', { name: 'Allow model requests?', exact: true });
  await expect(consent).toBeVisible();
  await expect(frame.locator('#answer')).toBeEmpty();
  expect(calls(), 'no model request may precede shell consent').toBe(0);
  await consent.getByRole('button', { name: 'Confirm', exact: true }).click();
  await expect(frame.locator('#answer')).toContainText(token, { timeout: 60_000 });
  await expect(frame.locator('#error')).toBeEmpty();
  expect(calls()).toBe(1);
  await page.getByTestId('artifact-viewer').getByRole('button', { name: 'Share artifact', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Share artifact', exact: true });
  const audience = dialog.getByRole('combobox', { name: 'Audience', exact: true });
  await expect(audience).toHaveValue('signed_in');
  await expect(
    audience.getByRole('option', { name: 'Anyone with the link', exact: true, disabled: true }),
  ).toBeAttached();
  await expect(
    audience.getByRole('option', { name: 'Anyone with the link', exact: true, disabled: false }),
  ).toHaveCount(0);
  await expect(dialog.getByText('This artifact calls a model or runs an Agent. Viewers must sign in.')).toBeVisible();
  expect(await scene.artifacts.refusePublicShare(artifact.artifact_id)).toBe('ARTIFACT_SHARE_AUDIENCE_RESTRICTED');
});
