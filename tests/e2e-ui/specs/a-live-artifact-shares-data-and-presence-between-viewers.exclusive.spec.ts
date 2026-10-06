import { test, expect } from '@playwright/test';

import { ArtifactsApi, storedData } from '../fixtures/artifactsApi';
import { livePage } from '../fixtures/artifactPages';
import { artifactResources, startArtifactScene, publishArtifact, shareArtifact, artifactFrame, marker } from '../fixtures/artifactScenario';

const resources = artifactResources();

test('a live artifact shares owner notes and presence with a read-only anonymous viewer', async ({ page, request, browser }) => {
  test.info().annotations.push({
    type: 'coverage_gap',
    description: 'oidcLogin.ts provides one deployment identity. Anonymous read-only coverage does not prove two signed-in viewers can write or isolate their private documents.',
  });
  const scene = await startArtifactScene(page, request, resources);
  const pageMarker = marker('LIVE_PAGE');
  const artifact = await publishArtifact(page, scene, marker('Live notes'), livePage(pageMarker), {
    db: { rules: [
      { path: 'notes', read: 'view', write: 'interact' },
      { path: 'data/users/{self}', read: 'view', write: 'interact' },
    ] },
    room: { topics: { ping: 'interact' } }, user: {},
  });
  const shared = await shareArtifact(page, { access: 'interact' });
  expect(await scene.artifacts.share(artifact.artifact_id)).toMatchObject({ access: 'interact', audience: 'anyone' });
  const anonymous = await browser.newContext({
    baseURL: new URL(page.url()).origin, storageState: { cookies: [], origins: [] },
  });
  try {
    const viewerPage = await anonymous.newPage();
    await viewerPage.goto(shared.url);
    const owner = artifactFrame(page);
    const viewer = artifactFrame(viewerPage);
    await expect(owner.locator('#marker')).toHaveText(pageMarker);
    await expect(viewer.locator('#marker')).toHaveText(pageMarker);
    const guestId = marker('GUEST');
    const viewerApi = new ArtifactsApi(anonymous.request, guestId);
    const guest = (await viewerApi.runtime({ token: shared.token })).viewer;
    expect(guest).toMatchObject({ level: 'view', guest: true });
    expect(guest.id).toMatch(/^guest_/);
    expect(guest.id).not.toContain(guestId);
    await expect(owner.locator('#peers')).toHaveText('2', { timeout: 30_000 });
    await expect(viewer.locator('#peers')).toHaveText('2', { timeout: 30_000 });
    let navigations = 0;
    page.on('framenavigated', () => { navigations += 1; });
    viewerPage.on('framenavigated', () => { navigations += 1; });
    for (const value of [marker('NOTE_ONE'), marker('NOTE_TWO')]) {
      await owner.getByRole('textbox', { name: 'Note', exact: true }).fill(value);
      await owner.getByRole('button', { name: 'Add note', exact: true }).click();
      await expect(owner.locator('#notes')).toContainText(value);
      await expect(viewer.locator('#notes')).toContainText(value, { timeout: 30_000 });
      expect((await scene.artifacts.queryData({ id: artifact.artifact_id }, 'notes')).items.map((row) => row.data.value)).toContain(value);
    }
    const ping = marker('PING');
    await owner.getByRole('textbox', { name: 'Note', exact: true }).fill(ping);
    await owner.getByRole('button', { name: 'Send ping', exact: true }).click();
    await expect(owner.locator('#ping')).toHaveText(ping);
    await expect(viewer.locator('#ping')).toHaveText(ping);

    const ownerId = (await scene.artifacts.runtime({ id: artifact.artifact_id })).viewer.id;
    expect(ownerId).toBeTruthy();
    expect(guest.id).not.toBe(ownerId);
    expect(await viewerApi.profiles({ token: shared.token }, [ownerId!])).toEqual({
      profiles: [{ id: ownerId, display_name: '' }],
    });
    const privatePath = `data/users/${ownerId}/profile`;
    const secret = marker('PRIVATE');
    await owner.getByRole('textbox', { name: 'Private field', exact: true }).fill(secret);
    await owner.getByRole('button', { name: 'Save private', exact: true }).click();
    await expect.poll(() => storedData(artifact.artifact_id, privatePath), { timeout: 15_000 }).toHaveLength(1);
    expect(storedData(artifact.artifact_id, privatePath)[0]).toMatchObject({ data: { value: secret } });
    const privateRefusal = await viewerApi.refusal({ token: shared.token }, 'read', privatePath);
    expect(privateRefusal.status).toBe(403);
    expect(privateRefusal.code).toBe('ARTIFACT_DATA_DENIED');
    expect(JSON.stringify(privateRefusal)).not.toContain(secret);

    const refused = marker('REFUSED_WRITE');
    await viewer.getByRole('textbox', { name: 'Note', exact: true }).fill(refused);
    await viewer.getByRole('button', { name: 'Add note', exact: true }).click();
    await expect(viewer.locator('#result')).toContainText('not_writer');
    expect((await viewerApi.refusal({ token: shared.token }, 'write', 'notes/anonymous', { value: refused })).status).toBe(403);
    expect(storedData(artifact.artifact_id, 'notes/anonymous')).toEqual([]);
    expect((await scene.artifacts.queryData({ id: artifact.artifact_id }, 'notes')).items).toHaveLength(2);
    await expect(viewer.locator('#notes')).not.toContainText(refused);
    await expect(owner.locator('#error')).toBeEmpty();
    await expect(viewer.locator('#error')).toBeEmpty();
    expect(navigations, 'data and room events must reach the mounted frames without reload').toBe(0);
  } finally {
    await anonymous.close();
  }
});
