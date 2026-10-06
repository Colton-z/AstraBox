import { test, expect } from '@playwright/test';

import { PlatformApi } from '../fixtures/platformApi';
import { appPath } from '../fixtures/env';
import { expectStoredVersion } from '../fixtures/artifactsApi';
import { isolatedPage } from '../fixtures/artifactPages';
import { artifactResources, startArtifactScene, publishArtifact, artifactFrame, marker } from '../fixtures/artifactScenario';

const resources = artifactResources();

test('an artifact an Agent publishes outlives its sandbox', async ({ page, request }) => {
  const scene = await startArtifactScene(page, request, resources, { cold: true });
  const bodyMarker = marker('DURABLE');
  const scriptMarker = marker('SCRIPT_RAN');
  const title = marker('Durable artifact');
  const content = isolatedPage(bodyMarker, scriptMarker);
  const artifact = await publishArtifact(page, scene, title, content);
  const frame = artifactFrame(page);
  await expect(frame.locator('#marker')).toHaveText(bodyMarker);
  await expect(frame.locator('#script')).toHaveText(scriptMarker);
  const sandbox = (await page.getByTestId('artifact-frame').getAttribute('sandbox'))!.split(/\s+/);
  expect(sandbox).toContain('allow-scripts');
  expect(sandbox).not.toContain('allow-same-origin');
  expect(sandbox).not.toContain('allow-downloads');
  await expect(frame.locator('#cookie')).toHaveText('cookie-blocked:SecurityError');
  await expect(frame.locator('#parent')).toHaveText('parent-blocked:SecurityError');

  const sandboxId = String((await scene.api.getSession(scene.sessionId)).sandbox_id || '');
  expect(sandboxId).not.toBe('');
  const reclaimed = await scene.api.terminateSandbox(scene.sessionId);
  expect(reclaimed).toMatchObject({ session_id: scene.sessionId, sandbox_id: sandboxId, status: 'sandbox-reclaimed', killed: true });
  const platform = new PlatformApi(request);
  await expect.poll(async () => (await platform.listSandboxes()).items?.some((item) => item.sandbox_id === sandboxId), {
    timeout: 30_000,
  }).toBe(false);

  await page.goto(appPath(`/artifacts/${artifact.artifact_id}`));
  await page.reload();
  await expect(artifactFrame(page).locator('#marker')).toHaveText(bodyMarker);
  await expect(artifactFrame(page).locator('#script')).toHaveText(scriptMarker);
  await expectStoredVersion(scene.artifacts, artifact.artifact_id, 1, content);
  await page.goto(appPath('/artifacts'));
  const entry = page.getByRole('link').filter({ has: page.getByRole('heading', { name: title, exact: true }) });
  await expect(entry).toHaveAttribute('href', `/artifacts/${artifact.artifact_id}`);
  await entry.click();
  await expect(artifactFrame(page).locator('#marker')).toHaveText(bodyMarker);
});
