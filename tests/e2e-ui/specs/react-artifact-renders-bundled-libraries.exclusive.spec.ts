import { test, expect } from '@playwright/test';

import { appPath } from '../fixtures/env';
import { expectStoredVersion } from '../fixtures/artifactsApi';
import {
  artifactResources, startArtifactScene, publishArtifact, updateArtifact,
  artifactFrame, marker,
} from '../fixtures/artifactScenario';

const resources = artifactResources();

test('a React artifact renders its bundled libraries after update and cold open', async ({ page, request, browser }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const scene = await startArtifactScene(page, request, resources);
  const plain = marker('PLAIN_REACT');
  const chart = marker('RECHARTS');
  const portal = marker('PORTAL');
  const first = `import React, { useState } from 'react';
export default function Demo() {
  const [count, setCount] = useState(0);
  return <main><h1 id="marker">${plain}</h1>
    <button onClick={() => setCount(count + 1)}>Count {count}</button></main>;
}`;
  const second = `import React, { useState } from 'react';
import { createPortal } from 'react-dom';
import { BarChart, Bar, XAxis, YAxis } from 'recharts';
export default function Demo() {
  const [count, setCount] = useState(0);
  const data = [{ name: 'Alpha', value: 3 }, { name: 'Beta', value: 6 + count }];
  return <main><h1 id="marker">${chart}</h1>
    <button onClick={() => setCount(count + 1)}>Count {count}</button>
    <BarChart width={400} height={240} data={data}>
      <XAxis dataKey="name" /><YAxis domain={[0, 10]} />
      <Bar dataKey="value" fill="#2563eb" isAnimationActive={false} />
    </BarChart>{createPortal(<p id="portal-proof">${portal}</p>, document.body)}
  </main>;
}`;
  const artifact = await publishArtifact(page, scene, marker('React libraries'), first, {}, 'react');
  const frame = artifactFrame(page);
  await expect(frame.locator('#marker')).toHaveText(plain);
  await frame.getByRole('button', { name: 'Count 0', exact: true }).click();
  await expect(frame.getByRole('button', { name: 'Count 1', exact: true })).toBeVisible();

  await updateArtifact(page, scene, artifact.artifact_id, first, second);
  await expect(frame.locator('#marker')).toHaveText(chart, { timeout: 30_000 });
  await expect(frame.locator('#portal-proof')).toHaveText(portal);
  const bars = frame.locator('.recharts-bar-rectangle');
  await expect(bars).toHaveCount(2);
  await expect(bars.nth(1)).toBeVisible();
  const before = await bars.nth(1).boundingBox();
  expect(before!.height).toBeGreaterThan(0);
  await frame.getByRole('button', { name: 'Count 0', exact: true }).click();
  await expect(frame.getByRole('button', { name: 'Count 1', exact: true })).toBeVisible();
  await expect.poll(async () => (await bars.nth(1).boundingBox())?.height ?? 0).toBeGreaterThan(before!.height);
  await expect(frame.getByRole('alert')).toHaveCount(0);
  expect(errors).toEqual([]);
  await expectStoredVersion(scene.artifacts, artifact.artifact_id, 1, first);
  await expectStoredVersion(scene.artifacts, artifact.artifact_id, 2, second);

  const cold = await browser.newContext({ storageState: await page.context().storageState() });
  try {
    const coldPage = await cold.newPage();
    coldPage.on('pageerror', (error) => errors.push(error.message));
    const url = new URL(appPath(`/artifacts/${artifact.artifact_id}`), page.url()).href;
    await coldPage.goto(url);
    const coldFrame = artifactFrame(coldPage);
    await expect(coldFrame.locator('#marker')).toHaveText(chart);
    await expect(coldFrame.locator('#portal-proof')).toHaveText(portal);
    await expect(coldFrame.locator('.recharts-bar-rectangle')).toHaveCount(2);
    await expect(coldFrame.locator('.recharts-bar-rectangle').nth(1)).toBeVisible();
    await expect(coldFrame.getByRole('alert')).toHaveCount(0);
    expect(errors).toEqual([]);
    await test.info().attach('recharts-cold-render', { body: await coldPage.screenshot(), contentType: 'image/png' });

    await coldPage.route('**/artifact-runtime/lib-recharts.js?*', (route) => route.fulfill({
      status: 200, contentType: 'application/javascript',
      body: 'throw new Error("ARTIFACT_LIBRARY_INIT_BROKEN");',
    }));
    await coldPage.reload();
    await expect(coldFrame.getByRole('alert')).toContainText(
      /ARTIFACT_LIBRARY_INIT_BROKEN|Artifact library "recharts" did not initialize/,
    );
    await expect(coldPage.getByTestId('artifact-frame-shell')).not.toContainText(/Unknown import|reading 'createRoot'/);
    await expect(coldFrame.locator('#marker')).toHaveCount(0);
  } finally {
    await cold.close();
  }
});
