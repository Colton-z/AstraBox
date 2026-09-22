/**
 * E2E: the first product action after substrate loss is opening the conversation.
 *
 * `assistant-workspace-oob-death-recovers` drives the same fault, but it clicks
 * **Start conversation** first and only reads the pre-existing conversation once
 * that click has already driven the workspace to a replacement sandbox. The
 * conversation therefore never meets a workspace that still names the dead box.
 *
 * This spec removes that help. After the substrate is gone, the only thing the
 * user does is open the conversation they already had and send the next
 * message. Recovery must not depend on having first visited the Assistant list
 * and clicked a button that happens to drive a wake.
 *
 * The load-bearing assertion is the turn. Losing the box is a state with a name
 * and an answer, so reporting it and asking for a retry is allowed; failing as
 * something unexpected is not, and neither is never recovering.
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

test('an Assistant conversation recovers when opening it is the first action after its box dies', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  const environmentName = await api.assistantEnvironmentName();
  expect(environmentName, 'an assistant environment must exist').not.toEqual('');
  const assistantModel = await api.assistantModelName(environmentName);

  const assistant = await api.createAssistant({
    display_name: `__e2e_assistant_oob_open_${runId}`,
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

  // T1 — the substrate is removed outside every AstraBox owner API, and
  // nothing is clicked or sent while it converges.
  const deadHandle = await requireSandboxHandle(api, deadSandboxId);
  expect(sandboxRunning(deadHandle), 'workspace sandbox must be running at fault time').toBe(true);
  killSandbox(deadHandle);
  await waitForSandboxStopped(deadHandle, KILL_CONVERGE_MS);
  expect(sandboxRunning(deadHandle), 'the removed workspace sandbox must be gone').toBe(false);
  test.info().annotations.push({
    type: 'e2e_assistant_dead_sandbox_id',
    description: deadSandboxId,
  });

  // T2 — the first product action is opening the conversation the user already
  // had. The Assistant list is never visited, so no wake has been driven.
  await page.goto(appPath(`/sessions/${sessionId}`));
  await expect(page.getByTestId('run-view')).toBeVisible();
  await expect(
    page.getByText('SANDBOX_GONE'),
    'opening a conversation must not surface the provider absence to the user',
  ).toHaveCount(0);

  // T3 — the next message in that same conversation. This is the assertion the
  // sibling spec cannot make, because by the time it opens this conversation a
  // replacement box already exists.
  // Losing the box is a state this platform has an answer for, so the first
  // message may report it and ask for a retry; what it may never do is fail as
  // something unexpected, because that tells the caller neither what happened
  // nor whether retrying is worth anything. The retry must then succeed.
  const attempts: string[] = [];
  let turnText = '';
  for (let attempt = 1; attempt <= 2 && turnText.trim() === ''; attempt += 1) {
    let failure: string | null = null;
    try {
      const turn = await api.sendTurn(
        sessionId,
        '请简短回复一句话，不要使用工具。',
        RECOVERED_TURN_MS,
      );
      failure = turn.errorText;
      turnText = turn.text;
    } catch (error) {
      failure = error instanceof Error ? error.message : String(error);
    }
    if (failure) attempts.push(failure);
  }

  for (const failure of attempts) {
    expect(
      failure,
      'losing a sandbox must be reported as itself, never as an internal fault',
    ).not.toMatch(/UNEXPECTED_SERVER_ERROR|unexpected \w*Error|-> 5\d\d/);
  }
  expect(
    turnText.trim(),
    `the conversation must serve a turn within two attempts; failures=${JSON.stringify(attempts)}`,
  ).not.toEqual('');

  // The conversation must name a replacement, never the sandbox the provider
  // has already reported absent.
  const reboundSession = await api.waitForSessionReady(sessionId);
  const replacementSandboxId = String(reboundSession.sandbox_id || '').trim();
  expect(replacementSandboxId, 'a recovered conversation must name a sandbox').not.toEqual('');
  expect(
    replacementSandboxId,
    `recovery must rebind the conversation to a new sandbox (dead=${deadSandboxId})`,
  ).not.toEqual(deadSandboxId);
  expect(String(reboundSession.last_error || '').trim()).toEqual('');

  const recoveredWorkspace = await api.waitForWorkspaceReady(assistantId);
  expect(
    String(recoveredWorkspace.current_sandbox_id || '').trim(),
    'the workspace must publish the same replacement the conversation is using',
  ).toEqual(replacementSandboxId);
  test.info().annotations.push({
    type: 'e2e_assistant_replacement_sandbox_id',
    description: replacementSandboxId,
  });
});
