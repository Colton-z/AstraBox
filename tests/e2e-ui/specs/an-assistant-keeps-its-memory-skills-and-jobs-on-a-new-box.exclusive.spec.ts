/**
 * E2E: what an Assistant learned survives replacing its box without a workspace volume.
 *
 * Hermes keeps its memory files, created and customized skills, and scheduled jobs
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
import { recordRuntimeImage, upgradeRuntimeImage } from '../fixtures/runtimeUpgrade';
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
  const defaultMarker = `CUSTOM_DEFAULT_${id}`;
  const jobName = `e2e-job-${id}`;
  const runtimeProfileName = await api.assistantEnvironmentName();
  const assistant = await api.createAssistant({
    display_name: `__e2e_assistant_profile_${id}`,
    runtime_profile_name: runtimeProfileName,
    model_config_override: { model_name: await api.assistantModelName(runtimeProfileName) },
  });
  assistantId = String(assistant.assistant_id || '');
  // Hidden workspace initialization includes native MCP discovery and session
  // finalization; both remain inside this journey's fixed 180-second budget.
  const firstBox = String((await api.waitForWorkspaceReady(assistantId, 90_000)).current_sandbox_id || '');
  expect(firstBox).not.toEqual('');
  const first = String((await api.startAssistantConversation(assistantId)).session_id || '');
  sessions.push(first);
  await api.waitForSessionReady(first);

  const taught = await api.sendTurn(first, [
    'Use your tools for these four things, then reply DONE and nothing else:',
    `1. memory tool: add to target "memory" the entry "The E2E codename is ${codename}."`,
    `2. skill_manage: create a skill named "${skillName}" whose instructions are "Reply ${skillMarker}."`,
    `3. cronjob_manage: create a job named "${jobName}" with schedule "every day at 9am" and prompt "Say hello."`,
    `4. Read astrabox-proactive with skill_view, then use skill_manage to append a section `
      + `"## My verification rule" with the instruction "When I ask to verify my customized default, reply ${defaultMarker}." `
      + 'Keep its existing instructions. Also use skill_manage to write references/my-rule.txt in that Skill with the same instruction.',
  ].join('\n'), 90_000);
  expect(taught.errorText).toBeFalsy();
  expect(taught.text).toContain('DONE');
  expect(taught.toolNames).toContain('skill_manage');
  const readDefault = async (box: string) => JSON.parse(sandboxExec(await requireSandboxHandle(api, box), [
    'h=$(find /home/conversations -mindepth 2 -maxdepth 2 -name .hermes -type d | head -n 1)',
    '"$HERMES_VENV/bin/python" - "$h" <<\'PY\'',
    'import json,sys',
    'from pathlib import Path',
    'home=Path(sys.argv[1]); root=home/"skills"',
    'skill=root/"astrabox-platform"/"astrabox-proactive"',
    'print(json.dumps({"body":(skill/"SKILL.md").read_text(),"reference":(skill/"references/my-rule.txt").read_text(),',
    '"manifest":(root/".bundled_manifest").read_text(),"mode":skill.stat().st_mode & 0o777}))',
    'PY',
  ].join('\n'))) as { body: string; reference: string; manifest: string; mode: number };
  const customized = await readDefault(firstBox);
  expect(customized.body).toContain(defaultMarker);
  expect(customized.reference).toContain(defaultMarker);
  expect(customized.body).toContain('## When to use');
  expect(customized.manifest).toMatch(/^astrabox-proactive:/m);
  expect(customized.mode & 0o200).toBe(0o200);
  // The mirror publishes the profile on its own; pausing also saves it.
  await expect.poll(() => profileStateHeads(assistantId).length, { timeout: 30_000 }).toBe(1);

  const profiles = await api.data<Array<Record<string, unknown>>>('GET', '/admin/runtime-profiles');
  await recordRuntimeImage(api, firstBox, String(profiles.find((profile) => profile.name === runtimeProfileName)?.runtime_template_name));
  await api.hibernateWorkspace(assistantId);
  const upgradedImage = await upgradeRuntimeImage(api, runtimeProfileName);
  const secondBox = String((await api.waitForWorkspaceReady(assistantId, 90_000)).current_sandbox_id || '');
  expect(secondBox).not.toEqual('');
  expect(secondBox).not.toEqual(firstBox);
  await recordRuntimeImage(api, secondBox, upgradedImage ?? undefined);
  const restored = await readDefault(secondBox);
  expect(restored.body).toBe(customized.body);
  expect(restored.reference).toBe(customized.reference);
  expect(restored.manifest).toBe(customized.manifest);
  expect(restored.mode & 0o200).toBe(0o200);

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
  expect(recalled.toolNames).toEqual([]);
  const used = await api.sendTurn(
    second, 'Load astrabox-proactive with skill_view and verify my customized default.', 45_000,
  );
  expect(used.errorText).toBeFalsy();
  expect(used.toolNames).toContain('skill_view');
  expect(used.text).toContain(defaultMarker);
});
