import { randomUUID } from 'node:crypto';
import { test, expect, type Page, type APIRequestContext } from '@playwright/test';

import { AstraApi } from './astraApi';
import { ArtifactsApi, expectStoredVersion, type Manifest } from './artifactsApi';
import { engineProfileFor, decisionId, type EngineProfile } from './engineProfile';
import { appPath } from './env';
import { onPassOnly, trackSessions } from './sessionCleanup';

export function artifactResources() {
  const resources = { sessions: trackSessions(), artifacts: [] as string[], agents: [] as string[], assistants: [] as string[] };
  onPassOnly(async ({ request }) => {
    const api = new AstraApi(request);
    for (const id of resources.artifacts.splice(0)) await new ArtifactsApi(request).remove(id);
    for (const id of resources.assistants.splice(0)) await api.deleteAssistant(id);
    for (const id of resources.agents.splice(0)) await api.deleteAgent(id);
  });
  return resources;
}
type Resources = ReturnType<typeof artifactResources>;
export interface ArtifactScene {
  api: AstraApi;
  artifacts: ArtifactsApi;
  sessionId: string;
  agentId?: string;
  assistantId?: string;
  profile?: EngineProfile;
  resources: Resources;
}
export function marker(prefix: string) { return `${prefix}_${randomUUID().replaceAll('-', '')}`; }

export async function openConversation(page: Page, sessionId: string) {
  await page.goto(appPath(`/sessions/${sessionId}`));
  await expect(page.getByTestId('run-view')).toBeVisible();
}
export async function startArtifactScene(
  page: Page, request: APIRequestContext, resources: Resources,
  options: { engine?: string; cold?: boolean; assistant?: boolean } = {},
): Promise<ArtifactScene> {
  const api = new AstraApi(request);
  let agentId: string | undefined;
  let assistantId: string | undefined;
  let profile: EngineProfile | undefined;
  let sessionId: string;
  if (options.assistant) {
    const environment = await api.assistantEnvironmentName();
    const assistant = await api.createAssistant({
      display_name: marker('__e2e_artifact_assistant'), environment_name: environment,
      model_config_override: { model_name: await api.assistantModelName(environment) },
    });
    assistantId = assistant.assistant_id;
    resources.assistants.push(assistantId);
    await api.waitForWorkspaceReady(assistantId);
    sessionId = (await api.startAssistantConversation(assistantId)).session_id;
  } else {
    profile = engineProfileFor(options.engine ?? 'claude_code');
    agentId = profile.agent_id;
    if (options.cold) {
      const agent = await api.createColdTestAgent(marker('__e2e_artifact_cold'));
      agentId = agent.agent_id;
      resources.agents.push(agentId);
      expect(String(agent.sandbox_id || ''), 'the cold Agent must not own shared compute').toBe('');
    }
    sessionId = (await api.startConversation(agentId)).session_id;
  }
  resources.sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  const ready = await api.waitForSessionReady(sessionId);
  if (assistantId) expect(ready.assistant_id, 'the panel needs the Assistant identity from Session detail').toBe(assistantId);
  test.info().annotations.push({ type: 'e2e_sandbox_id', description: String(ready.sandbox_id) });
  await openConversation(page, sessionId);
  return { api, artifacts: new ArtifactsApi(request), sessionId, agentId, assistantId, profile, resources };
}

export async function sendArtifactPrompt(page: Page, scene: ArtifactScene, prompt: string) {
  const previousTurn = (await scene.api.getSession(scene.sessionId)).last_turn_id;
  await page.getByTestId('composer-prompt').fill(prompt);
  await page.getByTestId('composer-submit').click();
  const answered = new Set<string>();
  await expect.poll(async () => {
    const detail = await scene.api.getSession(scene.sessionId);
    const pending = detail.pending_interaction;
    if (pending && !answered.has(pending.interaction_id)) {
      const panel = page.getByTestId('pending-interaction-panel');
      await expect(panel).toBeVisible();
      if (pending.presentation === 'decision') {
        if (!scene.profile) throw new Error('Assistant offered an undeclared decision vocabulary');
        await panel.getByTestId(`decision-option-${decisionId(scene.profile, 'approve')}`).click();
        await panel.getByTestId('decision-submit').click();
      } else {
        expect(pending.presentation).toBe('tool_approval');
        await panel.getByRole('button', { name: /Allow and continue|Apply suggestion and continue/ }).last().click();
      }
      answered.add(pending.interaction_id);
    }
    if (detail.last_turn_status === 'FAILED' || detail.last_error) {
      throw new Error(`artifact turn failed: ${JSON.stringify(detail)}`);
    }
    return Boolean(detail.last_turn_id && detail.last_turn_id !== previousTurn && !detail.current_turn_id);
  }, { timeout: 90_000, intervals: [500, 1000] }).toBe(true);
}

export function exactToolPrompt(tool: string, input: Record<string, unknown>) {
  return `Call the artifacts platform MCP server's ${tool} tool exactly once with this exact JSON input. `
    + `Preserve the title, kind, content and every marker verbatim. Do not create workspace files or use other tools. `
    + `After the tool succeeds, reply briefly.\n${JSON.stringify(input)}`;
}
export async function publishArtifact(page: Page, scene: ArtifactScene, title: string, content: string, capabilities: Manifest = {}) {
  await sendArtifactPrompt(page, scene, exactToolPrompt('create_artifact', { title, kind: 'html', content, capabilities }));
  const matches = (await scene.artifacts.list({ q: title })).items.filter((item) => item.title === title);
  expect(matches, 'one durable artifact must match this exact title').toHaveLength(1);
  const artifact = matches[0]!;
  scene.resources.artifacts.push(artifact.artifact_id);
  test.info().annotations.push({ type: 'e2e_artifact_id', description: artifact.artifact_id });
  expect(artifact).toMatchObject({ title, kind: 'html', current_version: 1, manifest: capabilities });
  expect(artifact.artifact_id).toMatch(/^art_[0-9a-f]{24}$/);
  if (scene.assistantId) expect(artifact.origin.assistant_id).toBe(scene.assistantId);
  else expect(artifact.origin).toMatchObject({ session_id: scene.sessionId, agent_id: scene.agentId });
  await expectStoredVersion(scene.artifacts, artifact.artifact_id, 1, content);
  await expect(page.getByTestId('artifact-tool-card').filter({ hasText: title })).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Artifacts', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('artifact-frame')).toHaveAttribute('title', title);
  return artifact;
}
export async function updateArtifact(page: Page, scene: ArtifactScene, id: string, oldMarker: string, newMarker: string) {
  await sendArtifactPrompt(page, scene, exactToolPrompt('update_artifact', {
    artifact_id: id, base_version: 1, edits: [{ old_string: oldMarker, new_string: newMarker, replace_all: true }],
  }));
}
export function artifactFrame(page: Page) { return page.frameLocator('[data-testid="artifact-frame"]'); }
export async function shareArtifact(page: Page, settings: { access?: 'view' | 'interact'; pin?: boolean } = {}) {
  await page.getByTestId('artifact-viewer').getByRole('button', { name: 'Share artifact', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Share artifact', exact: true });
  await expect(dialog.getByRole('button', { name: 'Save', exact: true })).toBeEnabled();
  await dialog.getByRole('combobox', { name: 'Access', exact: true }).selectOption(settings.access ?? 'view');
  await dialog.getByRole('combobox', { name: 'Audience', exact: true }).selectOption('anyone');
  await dialog.getByRole('combobox', { name: 'Shared version', exact: true }).selectOption(settings.pin ? 'pin' : 'latest');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  const field = dialog.getByRole('textbox', { name: 'Share link', exact: true });
  await expect(field).toHaveValue(/\/share\/artifacts\/[a-f0-9]+$/);
  const url = await field.inputValue();
  const token = new URL(url).pathname.split('/').at(-1)!;
  await page.keyboard.press('Escape');
  return { url, token };
}
