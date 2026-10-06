import { test, expect } from '@playwright/test';

import { storedArtifact, expectStoredVersion, nativeArtifactCalls } from '../fixtures/artifactsApi';
import { PlatformApi } from '../fixtures/platformApi';
import { apiPath } from '../fixtures/env';
import { simplePage } from '../fixtures/artifactPages';
import { artifactResources, startArtifactScene, publishArtifact, shareArtifact, sendArtifactPrompt, marker } from '../fixtures/artifactScenario';

const resources = artifactResources();

test('artifact tool results keep owner and share credentials out of shared history', async ({ page, request, browser }) => {
  const scene = await startArtifactScene(page, request, resources);
  const title = marker('Private metadata');
  const first = marker('FIRST');
  const second = marker('SECOND');
  const artifact = await publishArtifact(page, scene, title, simplePage(first));
  const artifactShare = await shareArtifact(page);
  const [record] = storedArtifact(artifact.artifact_id);
  expect(record?.owner_id, 'independent storage must identify the private owner').toBeTruthy();
  expect(record?.share).toMatchObject({ enabled: true, token: artifactShare.token });
  await sendArtifactPrompt(page, scene,
    'Call the artifacts platform MCP tools exactly once each, in this order and with these exact inputs: '
    + `list_artifacts ${JSON.stringify({ query: title, limit: 10 })}; `
    + `get_artifact ${JSON.stringify({ artifact_id: artifact.artifact_id })}; `
    + `update_artifact ${JSON.stringify({ artifact_id: artifact.artifact_id, base_version: 1, edits: [{ old_string: first, new_string: second }] })}. `
    + 'Use astrabox_artifact.version for the base version. Do not use other tools. Reply briefly after success.');
  await expectStoredVersion(scene.artifacts, artifact.artifact_id, 2, simplePage(second));
  await expect.poll(() => nativeArtifactCalls(scene.sessionId).map((call) => call.name), {
    timeout: 15_000,
  }).toEqual([
    expect.stringMatching(/create_artifact$/), expect.stringMatching(/list_artifacts$/),
    expect.stringMatching(/get_artifact$/), expect.stringMatching(/update_artifact$/),
  ]);
  const sessionShare = await new PlatformApi(request).createShare(scene.sessionId);
  expect(sessionShare.token).toBeTruthy();
  const anonymous = await browser.newContext({
    baseURL: new URL(page.url()).origin, storageState: { cookies: [], origins: [] },
  });
  try {
    const response = await anonymous.request.get(apiPath(`/share/${sessionShare.token}/history-blocks?limit=100`));
    expect(response.ok()).toBe(true);
    const shared = (await response.json()).data;
    for (const history of [await scene.api.getHistoryBlocks(scene.sessionId, 100), shared]) {
      const results = history.messages.flatMap((message: { blocks?: Array<Record<string, unknown>> }) =>
        (message.blocks ?? []).filter((block) => block.type === 'tool_result'));
      expect(results, 'all four artifact tool results must be present outside process folds').toHaveLength(4);
      const serialized = JSON.stringify(results);
      expect(serialized).toContain('astrabox_artifact');
      expect(serialized).toContain(artifact.artifact_id);
      expect(serialized, 'get must still return the independently stored page content').toContain(first);
      expect(serialized).not.toContain('owner_id');
      expect(serialized).not.toContain(String(record!.owner_id));
      expect(serialized).not.toContain(artifactShare.token);
      expect(serialized).not.toMatch(/\\?"share\\?"\s*:/);
    }
  } finally {
    await anonymous.close();
  }
});
