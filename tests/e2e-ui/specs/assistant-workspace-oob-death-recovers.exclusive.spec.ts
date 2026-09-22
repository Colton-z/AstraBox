/**
 * E2E: one terminal sandbox fact repairs the Assistant workspace and its Session.
 *
 * The fault removes the substrate resource directly, outside AstraBox's owner
 * APIs. Wake must replace the workspace box, and an existing conversation must
 * attach to that replacement without exposing SANDBOX_GONE or losing its
 * durable identity. Exact sandbox ids remain API assertions because the
 * conversation page does not display them.
 *
 * This journey provisions an initial workspace and its replacement. It must
 * stay in the suite contract's one-worker serial group so unrelated cold
 * workspaces cannot consume its fixed 180-second budget.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import {
  killSandbox,
  requireSandboxHandle,
  sandboxRunning,
  waitForSandboxStopped,
} from '../fixtures/sandboxOps';

const KILL_CONVERGE_MS = parseTimeoutEnv('ASTRABOX_E2E_KILL_CONVERGE_MS', 30_000);
const RECOVERED_TURN_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_ASSISTANT_RECOVERY_TURN_TIMEOUT_MS',
  120_000,
);

const sessions = trackSessions();
let assistantId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

test('out-of-band sandbox death rebuilds the Assistant workspace and preserves its conversation', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  const environmentName = await api.assistantEnvironmentName();
  expect(environmentName, 'an assistant environment must exist').not.toEqual('');
  const assistantModel = await api.assistantModelName(environmentName);

  const assistant = await api.createAssistant({
    display_name: `__e2e_assistant_oob_death_${runId}`,
    environment_name: environmentName,
    model_config_override: { model_name: assistantModel },
  });
  assistantId = String(assistant.assistant_id || '');
  expect(assistantId, 'created assistant must have an id').not.toEqual('');

  const initialWorkspace = await api.waitForWorkspaceReady(assistantId);
  const deadSandboxId = String(initialWorkspace.current_sandbox_id || '').trim();
  expect(deadSandboxId, 'a READY workspace must name its sandbox').not.toEqual('');

  const created = await api.startAssistantConversation(assistantId);
  const sessionId = String(created.session_id || '').trim();
  expect(sessionId, 'starting a conversation must open a session').not.toEqual('');
  sessions.push(sessionId);
  const initialSession = await api.waitForSessionReady(sessionId);
  expect(String(initialSession.sandbox_id || '')).toEqual(deadSandboxId);

  await page.goto(appPath('/assistants'));
  const assistantCard = page.locator(
    `[data-testid="assistant-option"][data-assistant-id="${assistantId}"]`,
  );
  await expect(assistantCard).toHaveAttribute('data-assistant-state', 'READY');
  const startButton = assistantCard.getByRole('button', { name: 'Start conversation' });
  await expect(startButton).toBeVisible();

  const deadHandle = await requireSandboxHandle(api, deadSandboxId);
  expect(sandboxRunning(deadHandle), 'workspace sandbox must be running at fault time').toBe(true);
  killSandbox(deadHandle);
  await waitForSandboxStopped(deadHandle, KILL_CONVERGE_MS);
  expect(sandboxRunning(deadHandle), 'the removed workspace sandbox must be gone').toBe(false);
  test.info().annotations.push({
    type: 'e2e_assistant_dead_sandbox_id',
    description: deadSandboxId,
  });

  await startButton.click();
  await expect(page).toHaveURL(/\/sessions\/[^/?#]+$/);
  const recoveredConversationId = new URL(page.url()).pathname.split('/').pop() || '';
  expect(
    recoveredConversationId,
    'one click on the stale READY card must open the shared Session startup flow',
  ).not.toEqual('');
  expect(recoveredConversationId).not.toEqual(sessionId);
  sessions.push(recoveredConversationId);

  const recoveredWorkspace = await api.waitForWorkspaceReady(assistantId);
  const replacementSandboxId = String(
    recoveredWorkspace.current_sandbox_id || '',
  ).trim();
  expect(replacementSandboxId, 'recovered workspace must name a sandbox').not.toEqual('');
  expect(
    replacementSandboxId,
    `workspace recovery must publish a new sandbox (dead=${deadSandboxId})`,
  ).not.toEqual(deadSandboxId);
  test.info().annotations.push({
    type: 'e2e_assistant_replacement_sandbox_id',
    description: replacementSandboxId,
  });

  const convergedSession = await api.getSession(sessionId);
  expect(String(convergedSession.state || '')).toBe('READY');
  expect(
    String(convergedSession.sandbox_id || ''),
    'an existing conversation must not retain the terminal workspace pointer',
  ).not.toEqual(deadSandboxId);

  const recoveredConversation = await api.waitForSessionReady(recoveredConversationId);
  expect(String(recoveredConversation.sandbox_id || '')).toEqual(replacementSandboxId);

  await page.goto(appPath(`/sessions/${sessionId}`));
  await expect(page.getByTestId('run-view')).toBeVisible();
  const turn = await api.sendTurn(
    sessionId,
    '请简短回复一句话，不要使用工具。',
    RECOVERED_TURN_MS,
  );
  expect(turn.errorText, 'the recovered conversation turn must not error').toBeNull();
  expect(turn.text.trim(), 'the recovered conversation turn must produce text').not.toEqual('');

  const reboundSession = await api.waitForSessionReady(sessionId);
  expect(String(reboundSession.sandbox_id || '')).toEqual(replacementSandboxId);
  expect(String(reboundSession.last_error || '').trim()).toEqual('');
});
