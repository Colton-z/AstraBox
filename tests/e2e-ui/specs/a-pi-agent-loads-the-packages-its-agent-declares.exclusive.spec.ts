/**
 * E2E: a Pi Agent loads the Pi package its Agent declares, and loses it when
 * the declaration is removed.
 *
 * The Agent's `engine_options.settings.packages` is Pi's own settings list;
 * Pi installs a declared npm or git source that is missing when it starts,
 * through the box's limited network, whose registry and git hosts the adapter
 * admits. The package is `@dietrichgebert/ponytail` 5.0.0, from npm and from
 * the commit of its v5.0.0 tag; Pi's object form narrows it to its Skills
 * (`extensions: []`), so no package code runs. The evidence is the Skill
 * catalog on the prewarmed box the conversation claims: asked without using
 * any tool, the Agent names the package's `ponytail-audit` Skill, and after
 * the declaration is removed a new conversation does not.
 */
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { engineProfileFor } from '../fixtures/engineProfile';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const SKILL = 'ponytail-audit';
const QUESTION = [
  'Without using any tool, list the names of the Skills available to you, one per line,',
  'exactly as your instructions name them, and nothing else.',
].join(' ');

const sessions = trackSessions();
const agentIds: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const agentId of agentIds) await api.deleteAgent(agentId);
});

/** The prepared box of the Agent's current version, once there is one. */
async function preparedBox(request: APIRequestContext, id: string, not = ''): Promise<string> {
  const platform = new PlatformApi(request);
  let prepared: Record<string, unknown> = {};
  await expect.poll(async () => {
    prepared = await platform.preparedRuntime(id);
    return prepared.ready === true && Number(prepared.prepared_count || 0) > 0
      && Boolean(prepared.sandbox_id) && prepared.sandbox_id !== not;
  }, { timeout: 180_000, intervals: [2_000], message: 'the Agent must prepare a runtime' }).toBe(true);
  return String(prepared.sandbox_id);
}

async function skillsListed(api: AstraApi, agentId: string, box: string): Promise<string> {
  const sessionId = String((await api.startConversation(agentId)).session_id || '');
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId, 120_000);
  expect(ready.sandbox_id, 'the conversation claims the prepared box').toBe(box);
  const turn = await api.sendTurn(sessionId, QUESTION, 120_000);
  expect(turn.errorText).toBeFalsy();
  expect(turn.toolNames, 'the answer comes from the Skill catalog, not a tool').toEqual([]);
  return turn.text;
}

for (const [label, source] of [
  ['npm', 'npm:@dietrichgebert/ponytail@5.0.0'],
  ['git', 'git:github.com/DietrichGebert/ponytail@b088b2df6e08d4306c6a3c3d575fe38c2d2d2989'],
] as const) {
  test(`${label} source: a Pi Agent loads its declared package's Skill, and not after the package is removed`, async ({ request }) => {
    const api = new AstraApi(request);
    const profile = engineProfileFor('pi');
    const config = {
      name: `__e2e_pi_package_${label}_${randomUUID().slice(0, 8)}`,
      model: profile.model,
      runtime_profile_name: profile.runtime_profile_name,
      prepared_capacity: 1,
    };
    const agentId = (await api.createAgent({
      ...config, engine_options: { settings: { packages: [{ source, extensions: [] }] } },
    })).agent_id;
    agentIds.push(agentId);

    const first = await preparedBox(request, agentId);
    expect(await skillsListed(api, agentId, first)).toContain(SKILL);

    const current = await api.getAgent(agentId);
    await api.updateAgent(agentId, {
      ...config, version: current.version, engine_options: { settings: { packages: [] } },
    });
    const second = await preparedBox(request, agentId, first);
    expect(await skillsListed(api, agentId, second)).not.toContain(SKILL);
  });
}
