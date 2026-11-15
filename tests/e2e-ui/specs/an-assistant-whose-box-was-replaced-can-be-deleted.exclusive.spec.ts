/**
 * E2E: an Assistant can be deleted after its box was replaced, before its
 * conversation runs again.
 *
 * A conversation's row names the box it last ran in. When the Assistant's box
 * dies, the workspace comes back on a new box by itself, and the row keeps its
 * old box (or none) until the conversation runs again. Deleting the Assistant
 * in that window ends the conversation's processes in the box the workspace
 * binds now; the owner sees the Assistant and its conversation gone, not a
 * refusal to clean up.
 *
 * This journey provisions an initial workspace and its replacement, so it runs
 * in the suite contract's one-worker serial group.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, sqlLiteral } from '../fixtures/dbOracle';
import { parseTimeoutEnv } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { killSandbox, requireSandboxHandle, waitForSandboxStopped } from '../fixtures/sandboxOps';

const KILL_CONVERGE_MS = parseTimeoutEnv('ASTRABOX_E2E_KILL_CONVERGE_MS', 30_000);

const sessions = trackSessions();
let assistantId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

test('an Assistant whose box was replaced is deleted with its conversation before the conversation runs again', async ({ request }) => {
  const api = new AstraApi(request);
  const runtimeProfileName = await api.assistantEnvironmentName();
  const assistant = await api.createAssistant({
    display_name: `__e2e_assistant_delete_after_replace_${Date.now()}`,
    runtime_profile_name: runtimeProfileName,
    model_config_override: { model_name: await api.assistantModelName(runtimeProfileName) },
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
  const conversation = (): { sandbox_id: string | null; deleted: boolean } | undefined => postgresRows<{
    sandbox_id: string | null;
    deleted: boolean;
  }>(`SELECT sandbox_id, deleted FROM sessions WHERE session_id = ${sqlLiteral(sessionId)}`)[0];
  // The window this journey is about: the row does not name the box the workspace binds now.
  expect(conversation()?.sandbox_id ?? null).not.toEqual(replacementSandboxId);

  await api.deleteAssistant(assistantId);
  const deletedId = assistantId;
  assistantId = '';
  expect(conversation()?.deleted, 'the conversation is deleted with its Assistant').toBe(true);
  expect(postgresRows<{ n: number }>(
    `SELECT count(*)::int AS n FROM assistants WHERE assistant_id = ${sqlLiteral(deletedId)} AND NOT deleted`,
  )[0]?.n).toEqual(0);
});
