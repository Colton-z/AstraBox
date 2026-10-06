import { test, expect } from '@playwright/test';

import { ArtifactsApi, storedArtifact } from '../fixtures/artifactsApi';
import { appPath } from '../fixtures/env';
import { simplePage } from '../fixtures/artifactPages';
import { artifactResources, startArtifactScene, publishArtifact, sendArtifactPrompt, exactToolPrompt, shareArtifact, artifactFrame, marker } from '../fixtures/artifactScenario';

const resources = artifactResources();

test('an artifact share admits anonymous viewers until revoked and can pin a version', async ({ page, request, browser }) => {
  const scene = await startArtifactScene(page, request, resources);
  const first = marker('SHARED_ONE');
  const second = marker('SHARED_TWO');
  const artifact = await publishArtifact(page, scene, marker('Anonymous share'), simplePage(first), { artifact: {} });
  const identity = await scene.api.data<{ user?: { display_name?: string; email?: string } }>('GET', '/auth/session');
  const shared = await shareArtifact(page);
  expect(await scene.artifacts.share(artifact.artifact_id)).toMatchObject({
    enabled: true, token: shared.token, audience: 'anyone', access: 'view', version: null,
  });
  const anonymous = await browser.newContext({
    baseURL: new URL(page.url()).origin, storageState: { cookies: [], origins: [] },
  });
  try {
    const viewer = await anonymous.newPage();
    const guestId = marker('GUEST');
    const viewerApi = new ArtifactsApi(anonymous.request, guestId);
    await viewer.goto(shared.url);
    await expect(artifactFrame(viewer).locator('#marker')).toHaveText(first);
    await expect(viewer.getByText('Content is user-generated and unverified', { exact: true })).toBeVisible();
    const runtime = await viewerApi.runtime({ token: shared.token });
    expect(runtime.viewer).toMatchObject({ level: 'view', guest: true });
    expect(runtime.viewer.id).toMatch(/^guest_/);
    expect(runtime.viewer.id).not.toContain(guestId);
    expect(runtime.artifact.share?.version).toBeNull();
    expect(runtime.artifact).not.toHaveProperty('owner_id');
    expect(runtime.artifact).not.toHaveProperty('author');
    const ownerId = String(storedArtifact(artifact.artifact_id)[0]?.owner_id || '');
    expect(ownerId, 'the independent record must identify its owner').not.toBe('');
    expect(JSON.stringify(runtime)).not.toContain(ownerId);
    await expect(viewer.locator('body')).not.toContainText(ownerId);
    for (const authorDetail of [identity.user?.display_name, identity.user?.email].filter((value): value is string => Boolean(value))) {
      expect(JSON.stringify(runtime)).not.toContain(authorDetail);
      await expect(viewer.locator('body')).not.toContainText(authorDetail);
    }
    await expect(viewer.getByRole('link', { name: /Origin/ })).toHaveCount(0);
    await expect(viewer.getByTestId('composer-prompt')).toHaveCount(0);

    await page.getByTestId('artifact-viewer').getByRole('button', { name: 'Share artifact', exact: true }).click();
    await page.getByRole('dialog', { name: 'Share artifact', exact: true }).getByRole('button', { name: 'Revoke link', exact: true }).click();
    await expect.poll(() => scene.artifacts.share(artifact.artifact_id)).toMatchObject({ enabled: false });
    expect((await viewerApi.refusal({ token: shared.token }, 'runtime')).status).toBe(404);
    await viewer.reload();
    await expect(viewer.getByRole('heading', { name: "Can't open this share", exact: true })).toBeVisible();
    await expect(viewer.getByTestId('artifact-frame')).toHaveCount(0);
    await page.keyboard.press('Escape');

    const pinned = await shareArtifact(page, { pin: true });
    expect(await scene.artifacts.share(artifact.artifact_id)).toMatchObject({ version: 1, token: pinned.token });
    await viewer.goto(pinned.url);
    await expect(artifactFrame(viewer).locator('#marker')).toHaveText(first);
    expect(await viewerApi.runtime({ token: pinned.token })).toMatchObject({
      version: 1, pinned: true, artifact: { share: { version: 1, access: 'view', audience: 'anyone' } },
    });
    expect(await artifactFrame(viewer).locator('body').evaluate(async () =>
      Boolean(await (window as any).astrabox.use('artifact'))),
    'a share pinned to the current version must not grant publication').toBe(false);
    await page.goto(appPath(`/sessions/${scene.sessionId}`));
    await sendArtifactPrompt(page, scene, exactToolPrompt('update_artifact', {
      artifact_id: artifact.artifact_id, base_version: 1,
      edits: [{ old_string: first, new_string: second }], capabilities: { user: {} },
    }));
    await expect(artifactFrame(page).locator('#marker')).toHaveText(second);
    await expect(artifactFrame(viewer).locator('#marker')).toHaveText(first);
    expect(await viewerApi.runtime({ token: pinned.token })).toMatchObject({
      version: 1, content: simplePage(first), manifest: { user: {} },
    });
    await viewer.reload();
    await expect(artifactFrame(viewer).locator('#marker')).toHaveText(first);
    expect(await artifactFrame(viewer).locator('body').evaluate(async () =>
      Boolean(await (window as any).astrabox.use('user'))),
    'pinned content must use the current version capability policy').toBe(true);
  } finally {
    await anonymous.close();
  }
});
