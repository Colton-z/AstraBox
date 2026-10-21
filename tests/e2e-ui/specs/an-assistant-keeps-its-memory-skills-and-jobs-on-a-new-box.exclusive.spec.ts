/**
 * E2E: what an Assistant learned survives replacing its box without a workspace volume.
 *
 * Hermes keeps its memory files, the skills it creates and its scheduled jobs
 * in its profile home, beside the SessionDB the platform already saves. A
 * pause releases the box and a resume starts a fresh one, so these must come
 * back from the platform database. The evidence is Hermes' own files in the
 * new box, and a new conversation that knows the remembered fact without
 * using any tool (with tools it could find the fact in old conversations).
 *
 * This journey provisions a workspace and its replacement. It stays in the
 * suite contract's one-worker serial group so unrelated cold workspaces
 * cannot consume its fixed 180-second budget.
 */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, sqlLiteral } from '../fixtures/dbOracle';
import { requireSandboxHandle, sandboxExec } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

/**
 * The published snapshots of the Assistant's profile state: the archive the
 * platform keeps beside the engine's native session store, one head per owner.
 */
function profileStateHeads(id: string): Array<{ snapshot_id: string }> {
  return postgresRows<{ snapshot_id: string }>(
    `SELECT snapshot_id FROM runtime_state_heads WHERE archive = 'profile_state' `
      + `AND subject_kind = 'assistant' AND subject_id = ${sqlLiteral(id)}`,
  );
}

const sessions = trackSessions();
let assistantId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

test('an Assistant keeps its memory, its own skill and its scheduled job on a new box', async ({ request }) => {
  const api = new AstraApi(request);
  const id = randomUUID().slice(0, 8);
  const codename = `KESTREL-${id}`;
  const skillName = `e2e-skill-${id}`;
  const skillMarker = `SKILL_BODY_${id}`;
  const jobName = `e2e-job-${id}`;
  const runtimeProfileName = await api.assistantEnvironmentName();
  const assistant = await api.createAssistant({
    display_name: `__e2e_assistant_profile_${id}`,
    runtime_profile_name: runtimeProfileName,
    model_config_override: { model_name: await api.assistantModelName(runtimeProfileName) },
  });
  assistantId = String(assistant.assistant_id || '');
  const firstBox = String((await api.waitForWorkspaceReady(assistantId, 60_000)).current_sandbox_id || '');
  expect(firstBox).not.toEqual('');
  const first = String((await api.startAssistantConversation(assistantId)).session_id || '');
  sessions.push(first);
  await api.waitForSessionReady(first);

  const taught = await api.sendTurn(first, [
    'Use your tools for these three things, then reply DONE and nothing else:',
    `1. memory tool: add to target "memory" the entry "The E2E codename is ${codename}."`,
    `2. skill_manage: create a skill named "${skillName}" whose instructions are "Reply ${skillMarker}."`,
    `3. cronjob_manage: create a job named "${jobName}" with schedule "every day at 9am" and prompt "Say hello."`,
  ].join('\n'), 90_000);
  expect(taught.errorText).toBeFalsy();
  expect(taught.text).toContain('DONE');
  // The mirror publishes the profile on its own; pausing also saves it.
  await expect.poll(() => profileStateHeads(assistantId).length, { timeout: 30_000 }).toBe(1);

  await api.hibernateWorkspace(assistantId);
  const secondBox = String((await api.waitForWorkspaceReady(assistantId, 90_000)).current_sandbox_id || '');
  expect(secondBox).not.toEqual('');
  expect(secondBox).not.toEqual(firstBox);

  const files = sandboxExec(await requireSandboxHandle(api, secondBox), [
    'h=$(find /home/conversations -mindepth 2 -maxdepth 2 -name .hermes -type d | head -n 1)',
    'cat "$h/memories/MEMORY.md"; echo; echo ===',
    'cat "$h/cron/jobs.json"; echo; echo ===',
    `find "$h/skills" -path "*${skillName}*" -name SKILL.md -exec cat {} +`,
  ].join('\n'));
  const [memory, jobs, skill] = files.split('===');
  expect(memory).toContain(codename);
  expect(jobs).toContain(jobName);
  expect(skill).toContain(skillMarker);

  const second = String((await api.startAssistantConversation(assistantId)).session_id || '');
  sessions.push(second);
  await api.waitForSessionReady(second);
  const recalled = await api.sendTurn(
    second,
    'Without using any tool, reply with only the E2E codename you remember.',
    60_000,
  );
  expect(recalled.errorText).toBeFalsy();
  expect(recalled.text).toContain(codename);
});
