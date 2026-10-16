import { test, expect } from '@playwright/test';

import { postgresRows, sessionOutputRows, sqlLiteral } from '../fixtures/dbOracle';
import { storedData } from '../fixtures/artifactsApi';
import { agentPage } from '../fixtures/artifactPages';
import { artifactResources, startArtifactScene, publishArtifact, artifactFrame, marker } from '../fixtures/artifactScenario';
import { mirrorSseBodies, sessionStreamBodies } from '../fixtures/sseBodies';

test.afterEach(async ({ page }, info) => {
  const sessionIds = info.annotations.filter((item) => item.type === 'e2e_session_id')
    .map((item) => item.description!).filter(Boolean);
  const reads = await Promise.allSettled([
    sessionStreamBodies(page),
    Promise.resolve().then(() => sessionIds.map((sessionId) => ({ sessionId, output: sessionOutputRows(sessionId) }))),
  ]);
  await info.attach('artifact-stream-scene', {
    body: JSON.stringify({ sessionIds, reads }), contentType: 'application/json',
  });
});

const resources = artifactResources();

/** The person a conversation belongs to, as the `sessions` row records it. */
function sessionOwner(sessionId: string): string | null {
  const rows = postgresRows<{ user_id: string | null }>(
    `SELECT user_id FROM sessions WHERE session_id = ${sqlLiteral(sessionId)}`,
  );
  return rows[0]?.user_id ?? null;
}

test('a page asks its Agent to refresh its data in a new conversation after consent', async ({ page, request }) => {
  await mirrorSseBodies(page);
  const scene = await startArtifactScene(page, request, resources);
  const value = marker('REFRESHED');
  const prompt = 'Use the artifact id named in the platform preamble as artifact_id. '
    + 'Call the artifacts platform MCP artifact_data tool exactly once with that artifact_id and these exact remaining fields: '
    + `${JSON.stringify({ action: 'set', path: 'live/status', data: { value } })}. `
    + 'Do not publish a version or write a workspace file. Reply briefly after success.';
  const artifact = await publishArtifact(page, scene, marker('Agent refresh'), agentPage(prompt), { agent: {}, db: {} });
  const origin = sessionOwner(scene.sessionId);
  expect(origin).toBeTruthy();
  const frame = artifactFrame(page);
  await expect(frame.locator('#value')).toHaveText('empty');
  const calls = scene.artifacts.observeCalls(page, artifact.artifact_id, 'agent-runs');
  let navigations = 0;
  page.on('framenavigated', () => { navigations += 1; });
  await frame.getByRole('button', { name: 'Refresh', exact: true }).click();
  const consent = page.getByRole('dialog', { name: 'Run the Agent?', exact: true });
  await expect.poll(() => consent.locator('pre').textContent()).toBe(prompt);
  expect(calls(), 'a new conversation cannot be started before consent').toBe(0);
  expect(storedData(artifact.artifact_id, 'live/status')).toEqual([]);
  const runResponse = scene.artifacts.nextAgentRun(page, artifact.artifact_id).then((run) => {
    resources.sessions.push(run.session_id);
    return run;
  });
  await consent.getByRole('button', { name: 'Confirm', exact: true }).click();
  const run = await runResponse;
  expect(calls()).toBe(1);
  expect(run.run_id).toBe(run.session_id);
  expect(run.session_id).not.toBe(scene.sessionId);
  expect((await scene.api.getSession(run.session_id)).agent_id).toBe(scene.agentId);
  expect(sessionOwner(run.session_id)).toBe(origin);
  await expect(frame.locator('#run')).toHaveText(run.session_id);
  await expect(frame.locator('#value')).toHaveText(value, { timeout: 60_000 });
  await expect(frame.locator('#error')).toBeEmpty();
  expect(navigations).toBe(0);
  expect(await scene.artifacts.getData({ id: artifact.artifact_id }, 'live/status')).toMatchObject({ data: { value } });
  expect(storedData(artifact.artifact_id, 'live/status')).toHaveLength(1);
  expect(storedData(artifact.artifact_id, 'live/status')[0]).toMatchObject({ data: { value } });
});
