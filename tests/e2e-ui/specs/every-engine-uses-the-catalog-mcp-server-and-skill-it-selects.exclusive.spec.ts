/**
 * E2E: every bundled engine uses the MCP server and the Skill selected for it
 * from the deployment's extension catalog (LiteLLM's MCP gateway and Skill
 * marketplace).
 *
 * The catalog Skill is registered the way an Agent manager adds one: the
 * console opens an Agent-scoped extension-console session, and the catalog's
 * own management API is called with the capability that session hands the
 * gateway UI. The MCP server is the deployment's managed one, whose upstream
 * credential lives in the Vault the lane names; the gateway's own key is
 * swapped in by the sandbox's egress, so neither reaches the box.
 *
 * Four Agents (Claude Code, Codex, Pi, DeepSeek Harness) and one Assistant
 * (Hermes) each select both entries, are bound to that Vault, and then prepare
 * the box their conversation claims. In it the program lists the catalog
 * Skill without using a tool, then calls the managed search tool; the
 * evidence is the stored conversation's own tool calls — a call that names
 * the tool, by name or inside a Pi codemode script, and whose result is the
 * search provider's own answer (its `organic` result set, which may be empty
 * for a live query) rather than an authorization, rate-limit or quota error,
 * which is what the gateway and the Vault credential decide. A gate an
 * engine raises on an MCP tool is answered as the user would answer it.
 */
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { approveGates } from '../fixtures/approveGates';
import { AstraApi, messageToolCalls, type MessageRecord, type ToolCallPart } from '../fixtures/astraApi';
import { engineProfileFor } from '../fixtures/engineProfile';
import { appPath } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

function requiredEnv(name: string): string {
  const value = String(process.env[name] || '').trim();
  if (!value) throw new Error(`${name} is required: the lane names the managed MCP fixture`);
  return value;
}

// Read inside the tests: Playwright loads every spec to list a lane, and a
// variable read at module scope would stop the whole lane collecting.
const managedMcp = () => requiredEnv('ASTRABOX_E2E_MANAGED_MCP_NAME');
const managedVault = () => requiredEnv('ASTRABOX_E2E_MANAGED_MCP_VAULT_ID');
/** A catalog entry for Anthropic's brand-guidelines Skill, pinned. */
const CATALOG_SKILL = {
  name: 'e2e-brand-guidelines',
  description: "Anthropic's brand guidelines, pinned for the catalog E2E.",
  source: {
    source: 'git-subdir',
    url: 'https://github.com/anthropics/skills.git',
    path: 'skills/brand-guidelines',
    ref: '683bc88e56f3e09ba94f7055977f3d3aa499f202',
  },
};
const SKILL_NAME = 'brand-guidelines';
const LIST_SKILLS = [
  'Without using any tool, list the names of the Skills available to you, one per line,',
  'exactly as your instructions name them, and nothing else.',
].join(' ');
const search = () => [
  `Call the ${managedMcp()} search_engine MCP tool exactly once with query "IBM investor relations"`,
  'and engine google, and return its result unchanged. Do not call any other tool.',
].join(' ');
const AUTH_OR_QUOTA_ERROR = /unauthori[sz]ed|forbidden|authentication failed|authorization failed|rate[- ]?limit|too many requests|quota exceeded|invalid(?: or missing)? (?:api ?)?(?:key|token)/i;

const sessions = trackSessions();
const agentIds: string[] = [];
const assistantIds: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const agentId of agentIds) await api.deleteAgent(agentId);
  for (const assistantId of assistantIds) await api.deleteAssistant(assistantId);
});

interface ExtensionCatalog {
  mcp_servers?: Array<{ id?: string; name?: string }>;
  skills?: Array<{ id?: string; name?: string }>;
  selected_mcp_server_ids?: string[];
  selected_skill_ids?: string[];
}

/**
 * Register the catalog Skill as an Agent manager does, once per deployment.
 *
 * The console's session hands the gateway UI a token whose `key` is the
 * capability the catalog API accepts; the grant cookie it sets rides along.
 * Parallel tests may race the create, which the catalog answers with 409.
 */
async function ensureCatalogSkill(request: APIRequestContext, api: AstraApi, agentId: string): Promise<void> {
  await api.data('POST', `/agents/${agentId}/extension-console/session`, { section: 'skills' });
  const ui = (await request.storageState()).cookies.find((cookie) => cookie.name === 'astrabox_extension_ui');
  expect(ui?.value, 'the extension-console session sets its UI token').toBeTruthy();
  const claims = JSON.parse(Buffer.from(String(ui!.value).split('.')[1], 'base64url').toString('utf8'));
  const headers = { Authorization: `Bearer ${String(claims.key || '')}` };
  const listed = await request.get(appPath('/claude-code/plugins'), { headers });
  expect(listed.status(), await listed.text()).toBe(200);
  const plugins = ((await listed.json()).plugins ?? []) as Array<{ name?: string }>;
  if (plugins.some((plugin) => plugin.name === CATALOG_SKILL.name)) return;
  const created = await request.post(appPath('/claude-code/plugins'), { headers, data: CATALOG_SKILL });
  expect([200, 201, 409], `register the catalog Skill: ${await created.text()}`).toContain(created.status());
}

/** The catalog ids of the managed MCP server and the catalog Skill. */
function selection(catalog: ExtensionCatalog): { mcp_server_ids: string[]; skill_ids: string[] } {
  const mcp = (catalog.mcp_servers ?? []).find((item) => item.name === managedMcp());
  const skill = (catalog.skills ?? []).find((item) => item.name === CATALOG_SKILL.name);
  expect(mcp?.id, `the catalog offers ${managedMcp()}`).toBeTruthy();
  expect(skill?.id, `the catalog offers ${CATALOG_SKILL.name}`).toBeTruthy();
  return { mcp_server_ids: [String(mcp!.id)], skill_ids: [String(skill!.id)] };
}

/** The tool calls the messages carry that name the managed search tool, in their input or as the tool. */
function searchCalls(messages: MessageRecord[]): ToolCallPart[] {
  return messages.flatMap(messageToolCalls)
    .filter((call) => JSON.stringify({ toolName: call.toolName, input: call.input }).includes('search_engine'));
}

/**
 * A tool call's result as text. Engines shape a result differently (a string,
 * content blocks, or an object around them); whatever the shape, the
 * provider's answer is inside its JSON.
 */
function resultText(output: unknown): string {
  return typeof output === 'string' ? output : JSON.stringify(output ?? '');
}

/** List the catalog Skill, then call the managed search tool, in one conversation. */
async function useTheCatalog(api: AstraApi, sessionId: string): Promise<void> {
  const listed = await api.sendTurn(sessionId, LIST_SKILLS, 120_000);
  expect(listed.errorText).toBeFalsy();
  expect(listed.toolNames, 'the Skill list comes from the Skill index, not a tool').toEqual([]);
  expect(listed.text, 'the catalog Skill is in the program\'s Skill index').toContain(SKILL_NAME);

  const before = (await api.getMessages(sessionId, 100)).messages.length;
  await api.postTurnInput(sessionId, search());
  await approveGates(api, sessionId);
  const calls = searchCalls((await api.getMessages(sessionId, 100)).messages.slice(before));
  expect(calls.length, 'the program called the managed search_engine tool').toBeGreaterThan(0);
  const results = calls.map((call) => resultText(call.output));
  const answered = results.filter((text) => /"organic"/.test(text.replace(/\\"/g, '"')));
  expect(answered.length, `the search provider answered through the gateway: ${results.join(' | ').slice(0, 600)}`)
    .toBeGreaterThan(0);
  expect(AUTH_OR_QUOTA_ERROR.test(answered[0]), 'the answer is not an auth, rate-limit or quota error').toBe(false);
}

async function preparedBox(request: APIRequestContext, agentId: string): Promise<string> {
  const platform = new PlatformApi(request);
  let prepared: Record<string, unknown> = {};
  await expect.poll(async () => {
    prepared = await platform.preparedRuntime(agentId);
    return prepared.ready === true && Number(prepared.prepared_count || 0) > 0 && Boolean(prepared.sandbox_id);
  }, { timeout: 150_000, intervals: [2_000], message: 'the Agent must prepare a runtime' }).toBe(true);
  return String(prepared.sandbox_id);
}

for (const engineKind of ['claude_code', 'codex', 'pi', 'deepseek_harness'] as const) {
  test(`${engineKind}: an Agent uses the catalog MCP server and Skill selected for it`, async ({ request }) => {
    const api = new AstraApi(request);
    const profile = engineProfileFor(engineKind);
    const config = {
      name: `__e2e_catalog_${engineKind}_${randomUUID().slice(0, 8)}`,
      model: profile.model,
      environment_name: profile.environment_name,
      prepared_capacity: 0,
    };
    const agentId = (await api.createAgent(config)).agent_id;
    agentIds.push(agentId);
    await ensureCatalogSkill(request, api, agentId);

    const catalog = await api.data<ExtensionCatalog>('GET', `/agents/${agentId}/extensions`);
    const selected = await api.data<ExtensionCatalog>('PUT', `/agents/${agentId}/extensions`, selection(catalog));
    expect(selected.selected_skill_ids?.length).toBe(1);
    await api.data('PUT', `/admin/agents/${agentId}/credential-vaults`, { vault_ids: [managedVault()] });
    // Prewarm only now, so the one prepared box is built from the selection.
    const current = await api.getAgent(agentId);
    await api.updateAgent(agentId, { ...config, version: current.version, prepared_capacity: 1 });
    const box = await preparedBox(request, agentId);

    const sessionId = String((await api.startConversation(agentId)).session_id || '');
    sessions.push(sessionId);
    const ready = await api.waitForSessionReady(sessionId, 120_000);
    expect(ready.sandbox_id, 'the conversation claims the prepared box').toBe(box);
    await useTheCatalog(api, sessionId);
  });
}

test('hermes: an Assistant uses the catalog MCP server and Skill selected for it', async ({ request }) => {
  const api = new AstraApi(request);
  const environmentName = await api.assistantEnvironmentName();
  const assistant = await api.createAssistant({
    display_name: `__e2e_catalog_hermes_${randomUUID().slice(0, 8)}`,
    environment_name: environmentName,
    model_config_override: { model_name: await api.assistantModelName(environmentName) },
  });
  const assistantId = String(assistant.assistant_id || '');
  assistantIds.push(assistantId);
  // A console session is scoped to an Agent; the catalog it edits is the
  // deployment's, which the Assistant's catalog then offers.
  const helper = (await api.createAgent({
    name: `__e2e_catalog_console_${randomUUID().slice(0, 8)}`,
    model: engineProfileFor('claude_code').model,
    environment_name: engineProfileFor('claude_code').environment_name,
    prepared_capacity: 0,
  })).agent_id;
  agentIds.push(helper);
  await ensureCatalogSkill(request, api, helper);

  const catalog = await api.data<ExtensionCatalog>('GET', `/assistants/${assistantId}/extensions`);
  const selected = await api.data<ExtensionCatalog>('PUT', `/assistants/${assistantId}/extensions`, selection(catalog));
  expect(selected.selected_mcp_server_ids?.length).toBe(1);
  expect((await api.data<ExtensionCatalog>('GET', `/assistants/${assistantId}/extensions`)).selected_skill_ids)
    .toEqual(selected.selected_skill_ids);
  await api.data('PUT', `/admin/assistants/${assistantId}/credential-vaults`, { vault_ids: [managedVault()] });

  const sessionId = String((await api.startAssistantConversation(assistantId)).session_id || '');
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  await useTheCatalog(api, sessionId);
});
