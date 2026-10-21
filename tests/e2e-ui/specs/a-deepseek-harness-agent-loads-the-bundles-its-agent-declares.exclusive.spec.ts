/**
 * E2E: a DeepSeek Harness Agent installs and loads the plugin Bundle its
 * Agent declares, and loses it when the dependency is removed.
 *
 * The Agent's `engine_options.profile_package` is a fragment of the
 * harness's web profile package.json; the conversation bootstrap writes it
 * and moves its dependencies with the harness's own `dsh plugin --profile web
 * add` and `remove` before the harness starts, which is when it reads its
 * Bundles. The Bundle is `humanizer-ru`, pinned: a patch layer that mounts the
 * harness's filesystem Skill provider on the package's own skills directory,
 * so its Skill joins the session's catalog. Both tenancies run, as for Codex
 * plugins, from the npm registry; the same release also comes as the
 * registry's tarball URL and as a GitHub commit, the other two dependency
 * forms whose hosts the adapter admits. The evidence is that catalog on the
 * prewarmed box the conversation claims:
 * asked without using any tool, the Agent names the Bundle's Skill, and after
 * the dependency is removed a new conversation does not.
 */
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { engineProfileFor } from '../fixtures/engineProfile';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const BUNDLE = { dependencies: { 'humanizer-ru': '3.31.3' } };
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

for (const tenancy of ['agent', 'conversation'] as const) {
  test(`${tenancy} tenancy: a DeepSeek Harness Agent loads its declared Bundle's Skill, and not after the Bundle is removed`, async ({ request }) => {
    const api = new AstraApi(request);
    const profile = engineProfileFor('deepseek_harness');
    // The fixture configures the profile's RuntimeProfile and one for the other
    // tenancy, named with that tenancy as suffix (configure-agent-engine-matrix.py).
    const environment = tenancy === 'agent' ? profile.runtime_profile_name : `${profile.runtime_profile_name}-${tenancy}`;
    const stored = (await new PlatformApi(request).listEnvironments())
      .find((item) => String(item.name || '') === environment);
    expect(stored?.sandbox_tenancy, `${environment} is the ${tenancy}-tenancy DeepSeek Harness RuntimeProfile`).toBe(tenancy);
    const config = {
      name: `__e2e_dsh_bundle_${tenancy}_${randomUUID().slice(0, 8)}`,
      model: profile.model,
      runtime_profile_name: environment,
      prepared_capacity: 1,
    };
    const agentId = (await api.createAgent({ ...config, engine_options: { profile_package: BUNDLE } })).agent_id;
    agentIds.push(agentId);

    const first = await preparedBox(request, agentId);
    expect(await skillsListed(api, agentId, first)).toContain('humanizer-ru');

    const current = await api.getAgent(agentId);
    await api.updateAgent(agentId, {
      ...config, version: current.version, engine_options: { profile_package: { dependencies: {} } },
    });
    const second = await preparedBox(request, agentId, first);
    expect(await skillsListed(api, agentId, second)).not.toContain('humanizer-ru');
  });
}

for (const [source, spec] of [
  ['tarball', 'https://registry.npmjs.org/humanizer-ru/-/humanizer-ru-3.31.3.tgz'],
  ['git', 'github:ilyautov/humanizer-ru#1e035ad6b64c56c6e770ed28d076e2cbda3d1aed'],
] as const) {
  test(`${source} source: a DeepSeek Harness Agent loads the Bundle its Agent declares`, async ({ request }) => {
    const api = new AstraApi(request);
    const profile = engineProfileFor('deepseek_harness');
    const agentId = (await api.createAgent({
      name: `__e2e_dsh_bundle_${source}_${randomUUID().slice(0, 8)}`,
      model: profile.model,
      runtime_profile_name: profile.runtime_profile_name,
      prepared_capacity: 1,
      engine_options: { profile_package: { dependencies: { 'humanizer-ru': spec } } },
    })).agent_id;
    agentIds.push(agentId);
    expect(await skillsListed(api, agentId, await preparedBox(request, agentId))).toContain('humanizer-ru');
  });
}
