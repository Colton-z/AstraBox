/**
 * E2E: an Assistant whose box dies while nobody uses it is back before anyone asks.
 *
 * An Assistant runs scheduled jobs and answers messaging channels between its
 * owner's messages, so recovery cannot wait for the owner to open it, which is
 * what `assistant-workspace-oob-death-recovers` and its `-on-open` sibling
 * exercise. Here nothing is clicked, sent or woken after the fault: the only
 * reads are the database and the substrate. The workspace must name a new,
 * running box, and the existing conversation must answer its next message
 * on the first attempt.
 *
 * This journey provisions an initial workspace and its replacement. It stays
 * in the suite contract's one-worker serial group so unrelated cold workspaces
 * cannot consume its fixed 180-second budget.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, sqlLiteral } from '../fixtures/dbOracle';
import { parseTimeoutEnv } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import {
  killSandbox,
  requireSandboxHandle,
  sandboxRunning,
  waitForSandboxStopped,
} from '../fixtures/sandboxOps';

const KILL_CONVERGE_MS = parseTimeoutEnv('ASTRABOX_E2E_KILL_CONVERGE_MS', 30_000);

const sessions = trackSessions();
let assistantId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

test('an idle Assistant whose box dies is rebuilt without any action and answers on the first try', async ({ request }) => {
  const api = new AstraApi(request);
  const environmentName = await api.assistantEnvironmentName();
  const assistant = await api.createAssistant({
    display_name: `__e2e_assistant_unprompted_heal_${Date.now()}`,
    environment_name: environmentName,
    model_config_override: { model_name: await api.assistantModelName(environmentName) },
  });
  assistantId = String(assistant.assistant_id || '');
  expect(assistantId).not.toEqual('');
  const deadSandboxId = String((await api.waitForWorkspaceReady(assistantId, 60_000)).current_sandbox_id || '');
  expect(deadSandboxId).not.toEqual('');
  const sessionId = String((await api.startAssistantConversation(assistantId)).session_id || '');
  sessions.push(sessionId);
  expect(String((await api.waitForSessionReady(sessionId)).sandbox_id || '')).toEqual(deadSandboxId);

  const deadHandle = await requireSandboxHandle(api, deadSandboxId);
  killSandbox(deadHandle);
  await waitForSandboxStopped(deadHandle, KILL_CONVERGE_MS);
  expect(sandboxRunning(deadHandle)).toBe(false);

  // No product action from here until the workspace is back.
  const workspace = (): { state?: string; current_sandbox_id?: string | null } => postgresRows<{
    state: string;
    current_sandbox_id: string | null;
  }>(
    `SELECT state, current_sandbox_id FROM assistant_workspaces WHERE assistant_id = ${sqlLiteral(assistantId)}`,
  )[0] ?? {};
  await expect.poll(() => {
    const row = workspace();
    return row.state === 'READY' && Boolean(row.current_sandbox_id) && row.current_sandbox_id !== deadSandboxId;
  }, { timeout: 100_000, intervals: [1_000, 2_000] }).toBe(true);
  const replacementSandboxId = String(workspace().current_sandbox_id);
  expect(sandboxRunning(await requireSandboxHandle(api, replacementSandboxId))).toBe(true);

  const turn = await api.sendTurn(sessionId, 'Reply with one short sentence. Do not use tools.', 60_000);
  expect(turn.errorText, 'the rebuilt Assistant answers without a retry').toBeFalsy();
  expect(turn.text.trim()).not.toEqual('');
  expect(String((await api.waitForSessionReady(sessionId)).sandbox_id || '')).toEqual(replacementSandboxId);
});
