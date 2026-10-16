/**
 * E2E: a Codex Agent installs and loads the Codex plugin its Agent declares,
 * and loses it when the package is removed.
 *
 * Codex installs plugins only from a marketplace and reads them when its
 * app-server starts, so the conversation bootstrap writes a local
 * marketplace from the cached checkout and runs Codex's own installer before
 * the conversation's server starts. The plugin is Anthropic's
 * `frontend-design`, pinned, whose Claude Code manifest Codex accepts and
 * whose one Skill shares its name. Both tenancies run: on the Agent-shared
 * box each conversation starts its own Codex server after the bootstrap; on a
 * dedicated box the image's resident server is already running when the
 * bootstrap installs, and the conversation's thread is what must see it. The
 * evidence is Codex's Skill index on the prewarmed box the conversation
 * claims: asked without using any tool, the Agent names the plugin's Skill,
 * and after the package is removed a new conversation does not.
 *
 * A plugin also carries MCP servers, which Codex reads from the plugin's
 * `.mcp.json`. The `code-context` plugin of FradSer's `dotclaude`, pinned,
 * carries DeepWiki's public server; its host is in the plugin rather than in
 * the Agent, so an administrator's copy of the Codex Environment admits it,
 * as docs/adding-tools.md directs. The evidence is DeepWiki's own answer in
 * the stored conversation.
 *
 * And it carries hooks, which Codex runs only once their hash is recorded as
 * trusted; the platform records it for the plugins it installs. Anthropic's
 * `explanatory-output-style`, pinned, has one SessionStart hook that tells
 * the model it is in 'explanatory' output style mode, a name it has no other
 * way to learn.
 */
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { approveGates } from '../fixtures/approveGates';
import { AstraApi, messageToolCalls, toolCallSettled } from '../fixtures/astraApi';
import { engineProfileFor } from '../fixtures/engineProfile';
import { copyEnvironment } from '../fixtures/environmentCopy';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const PLUGIN = {
  kind: 'codex-plugin',
  url: 'https://github.com/anthropics/claude-plugins-official.git',
  protocol: 'https',
  sha: 'd4226d062928f8d9505dbdeadd10217d23361052',
  paths: ['plugins/frontend-design'],
};
const QUESTION = [
  'Without using any tool, list the names of the Skills available to you, one per line,',
  'exactly as your instructions name them, and nothing else.',
].join(' ');

const MCP_PLUGIN = {
  kind: 'codex-plugin',
  url: 'https://github.com/FradSer/dotclaude.git',
  protocol: 'https',
  sha: '9815c4f485280385ff7657839f03ccc89a559c4d',
  paths: ['code-context'],
};
const DEEPWIKI_HOST = 'mcp.deepwiki.com';
const HOOK_PLUGIN = { ...PLUGIN, paths: ['plugins/explanatory-output-style'] };

const sessions = trackSessions();
const agentIds: string[] = [];
const environments: Array<Record<string, unknown>> = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const agentId of agentIds) await api.deleteAgent(agentId);
  for (const environment of environments) {
    await api.data('PUT', `/admin/environments/${encodeURIComponent(String(environment.name))}`, {
      ...environment, enabled: false,
    });
  }
});

/** The prepared box of the Agent's current version, once there is one. */
async function preparedBox(request: APIRequestContext, id: string, not = ''): Promise<string> {
  const platform = new PlatformApi(request);
  let prepared: Record<string, unknown> = {};
  await expect.poll(async () => {
    prepared = await platform.preparedRuntime(id);
    return prepared.ready === true && Number(prepared.prepared_count || 0) > 0
      && Boolean(prepared.sandbox_id) && prepared.sandbox_id !== not;
  }, { timeout: 120_000, intervals: [2_000], message: 'the Agent must prepare a runtime' }).toBe(true);
  return String(prepared.sandbox_id);
}

async function skillsListed(api: AstraApi, agentId: string, box: string): Promise<string> {
  const sessionId = String((await api.startConversation(agentId)).session_id || '');
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId, 120_000);
  expect(ready.sandbox_id, 'the conversation claims the prepared box').toBe(box);
  const turn = await api.sendTurn(sessionId, QUESTION, 120_000);
  expect(turn.errorText).toBeFalsy();
  expect(turn.toolNames, 'the answer comes from the Skill index, not a tool').toEqual([]);
  return turn.text;
}

for (const tenancy of ['agent', 'conversation'] as const) {
  test(`${tenancy} tenancy: a Codex Agent loads its declared plugin's Skill, and not after the plugin is removed`, async ({ request }) => {
    const api = new AstraApi(request);
    const profile = engineProfileFor('codex');
    // The fixture configures the profile's Environment and one for the other
    // tenancy, named with that tenancy as suffix (configure-agent-engine-matrix.py).
    const environment = tenancy === 'agent' ? profile.environment_name : `${profile.environment_name}-${tenancy}`;
    const stored = (await new PlatformApi(request).listEnvironments())
      .find((item) => String(item.name || '') === environment);
    expect(stored?.sandbox_tenancy, `${environment} is the ${tenancy}-tenancy Codex Environment`).toBe(tenancy);
    const config = {
      name: `__e2e_codex_plugin_${tenancy}_${randomUUID().slice(0, 8)}`,
      model: profile.model,
      environment_name: environment,
      prepared_capacity: 1,
    };
    const agentId = (await api.createAgent({ ...config, packages: [PLUGIN] })).agent_id;
    agentIds.push(agentId);

    const first = await preparedBox(request, agentId);
    expect(await skillsListed(api, agentId, first)).toContain('frontend-design');

    const current = await api.getAgent(agentId);
    await api.updateAgent(agentId, { ...config, version: current.version, packages: [] });
    const second = await preparedBox(request, agentId, first);
    expect(await skillsListed(api, agentId, second)).not.toContain('frontend-design');
  });
}

test('a Codex Agent calls the MCP server its declared plugin carries', async ({ request }) => {
  const api = new AstraApi(request);
  const profile = engineProfileFor('codex');
  const id = randomUUID().slice(0, 8);
  const source = (await new PlatformApi(request).listEnvironments())
    .find((item) => String(item.name || '') === profile.environment_name);
  expect(source, `the Codex Environment ${profile.environment_name}`).toBeTruthy();
  const environment = await copyEnvironment(api, source!, `__e2e_codex_plugin_mcp_${id}`, { hosts: [DEEPWIKI_HOST] });
  environments.push(environment);
  const agentId = (await api.createAgent({
    name: `__e2e_codex_plugin_mcp_${id}`,
    model: profile.model,
    environment_name: String(environment.name),
    prepared_capacity: 0,
    packages: [MCP_PLUGIN],
  })).agent_id;
  agentIds.push(agentId);
  const sessionId = String((await api.startConversation(agentId)).session_id || '');
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId, 120_000);

  await api.postTurnInput(sessionId, [
    'Call the read_wiki_structure tool of the deepwiki-code-context MCP server exactly once',
    'with repoName "facebook/react". Do not call any other tool.',
    `After it returns, reply with exactly DONE_${id} and nothing else.`,
  ].join(' '));
  await approveGates(api, sessionId);
  // History's tool parts carry Codex's own item for the call and for its
  // result (`engine/codex_events.py`), so the tool and its answer are read
  // off the same call.
  const calls = (await api.getMessages(sessionId, 100)).messages.flatMap(messageToolCalls)
    .filter((call) => JSON.stringify(call.input ?? null).includes('read_wiki_structure'));
  expect(calls.length, 'Codex called the plugin\'s DeepWiki tool').toBeGreaterThan(0);
  const results = calls.filter(toolCallSettled).map((call) => JSON.stringify(call.output ?? ''));
  expect(results.some((text) => text.includes('Available pages for facebook/react')),
    `DeepWiki answered through the plugin's server: ${results.join(' | ').slice(0, 600)}`).toBe(true);
});

test('a Codex Agent runs the hooks its declared plugin carries', async ({ request }) => {
  const api = new AstraApi(request);
  const profile = engineProfileFor('codex');
  const agentId = (await api.createAgent({
    name: `__e2e_codex_plugin_hook_${randomUUID().slice(0, 8)}`,
    model: profile.model,
    environment_name: profile.environment_name,
    prepared_capacity: 0,
    packages: [HOOK_PLUGIN],
  })).agent_id;
  agentIds.push(agentId);
  const sessionId = String((await api.startConversation(agentId)).session_id || '');
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId, 120_000);
  const turn = await api.sendTurn(sessionId, [
    'Your context may name an output style mode you are in. Without using any tool,',
    'reply with exactly that mode\'s name, or NONE if your context names none, and nothing else.',
  ].join(' '), 120_000);
  expect(turn.errorText).toBeFalsy();
  expect(turn.toolNames).toEqual([]);
  expect(turn.text.toLowerCase(), 'the SessionStart hook ran and its context reached the model').toContain('explanatory');
});
