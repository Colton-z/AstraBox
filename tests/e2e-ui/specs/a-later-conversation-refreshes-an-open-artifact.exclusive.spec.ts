import { test, expect } from '@playwright/test';

import { appPath } from '../fixtures/env';
import { expectStoredVersion, nativeArtifactCalls } from '../fixtures/artifactsApi';
import { simplePage } from '../fixtures/artifactPages';
import { artifactResources, startArtifactScene, publishArtifact, sendArtifactPrompt, artifactFrame, marker } from '../fixtures/artifactScenario';

const resources = artifactResources();

test('a later conversation refreshes an already-open artifact without reload', async ({ page, request, context }) => {
  const scene = await startArtifactScene(page, request, resources);
  const first = marker('BEFORE');
  const second = marker('AFTER');
  const title = marker('Later conversation');
  const content = simplePage(first);
  const artifact = await publishArtifact(page, scene, title, content);
  await page.goto(appPath(`/artifacts/${artifact.artifact_id}`));
  await expect(artifactFrame(page).locator('#marker')).toHaveText(first);
  let navigations = 0;
  page.on('framenavigated', (frame) => { if (frame === page.mainFrame()) navigations += 1; });
  const laterPage = await context.newPage();
  const later = await startArtifactScene(laterPage, request, resources);
  expect(later.sessionId).not.toBe(scene.sessionId);
  await sendArtifactPrompt(laterPage, later,
    `Call the artifacts platform MCP tools in exactly this order: first list_artifacts with `
    + `${JSON.stringify({ query: title, limit: 10 })}, then update_artifact with `
    + `${JSON.stringify({ artifact_id: artifact.artifact_id, base_version: 1, edits: [{ old_string: first, new_string: second, replace_all: true }] })}. `
    + 'Use the exact inputs. Do not create another artifact or use other tools. Reply briefly after success.');
  await expect(artifactFrame(page).locator('#marker')).toHaveText(second, { timeout: 30_000 });
  expect(navigations, 'the original document must remain open during journal delivery').toBe(0);
  await expectStoredVersion(scene.artifacts, artifact.artifact_id, 2, content.replaceAll(first, second));
  await expect.poll(() => nativeArtifactCalls(later.sessionId).map((call) => ({ name: call.name, input: call.input })), {
    timeout: 15_000,
  }).toEqual([
    { name: expect.stringMatching(/list_artifacts$/), input: { query: title, limit: 10 } },
    { name: expect.stringMatching(/update_artifact$/), input: {
      artifact_id: artifact.artifact_id, base_version: 1,
      edits: [{ old_string: first, new_string: second, replace_all: true }],
    } },
  ]);
  expect((await scene.artifacts.get(artifact.artifact_id)).origin.session_id).toBe(scene.sessionId);
  await laterPage.close();
});
