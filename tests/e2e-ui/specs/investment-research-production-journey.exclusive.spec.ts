/**
 * Production-shaped golden journey for the seeded Investment Research Agent.
 *
 * The deployment fixture owns the Agent, Vault credential and prepared runtime. The
 * spec only reads that state, claims one real conversation, and calls the two
 * data providers from inside its sandbox. A passing run deletes the session; a
 * failing run keeps the session and sandbox for diagnosis.
 */
import { expect, test, type Page } from '@playwright/test';

import { AstraApi, type AdminSessionRecord } from '../fixtures/astraApi';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { trackSessions } from '../fixtures/sessionCleanup';

const OFFICIAL_PLUGIN_REPOSITORY = 'https://github.com/anthropics/financial-services.git';
const FINANCIAL_ANALYSIS_PATH = 'plugins/vertical-plugins/financial-analysis';
const EQUITY_RESEARCH_PATH = 'plugins/vertical-plugins/equity-research';
// How long the Agent pool gets to put its one spare back. It refills in
// seconds; this is sized to fit inside the lane's 180s per-test wall rather
// than to outlast a broken pool, which fails here with its own state.
const PREWARM_READY_MS = parseTimeoutEnv('ASTRABOX_E2E_IR_PREWARM_READY_MS', 45_000);

test.describe.configure({ retries: 0 });

const sessions = trackSessions();

interface JourneyConfiguration {
  agentName: string;
  directMcpName: string;
  managedMcpHeader: string;
  managedMcpName: string;
  managedTargetUrl: string;
  vaultId: string;
  credentialId: string;
  pluginRevisions: string[];
  oidcIssuer: string;
}

interface ExtensionCatalog {
  mcp_servers?: unknown[];
  selected_mcp_server_ids?: unknown[];
}

function requiredEnv(name: string): string {
  const value = String(process.env[name] || '').trim();
  if (!value) throw new Error(`${name} is required for the Investment Research journey`);
  return value;
}

function journeyConfiguration(): JourneyConfiguration {
  const managedTargetUrl = requiredEnv('ASTRABOX_E2E_MANAGED_MCP_TARGET_URL');
  const pluginRevisions = requiredEnv('ASTRABOX_E2E_PLUGIN_REVISIONS').split(',');
  const managedMcpName = requiredEnv('ASTRABOX_E2E_MANAGED_MCP_NAME');
  if (!/^https:\/\//.test(managedTargetUrl)) {
    throw new Error('ASTRABOX_E2E_MANAGED_MCP_TARGET_URL must be HTTPS');
  }
  if (!pluginRevisions.every((revision) => /^[0-9a-f]{40}$/.test(revision))) {
    throw new Error('ASTRABOX_E2E_PLUGIN_REVISIONS must contain comma-separated full Git SHAs');
  }
  if (!/^[A-Za-z0-9_.-]+$/.test(managedMcpName)) {
    throw new Error('ASTRABOX_E2E_MANAGED_MCP_NAME is not safe for the bridge CLI');
  }
  return {
    agentName: requiredEnv('ASTRABOX_E2E_RESEARCH_AGENT'),
    directMcpName: requiredEnv('ASTRABOX_E2E_DIRECT_MCP_NAME'),
    managedMcpHeader: requiredEnv('ASTRABOX_E2E_MANAGED_MCP_HEADER'),
    managedMcpName,
    managedTargetUrl,
    vaultId: requiredEnv('ASTRABOX_E2E_MANAGED_MCP_VAULT_ID'),
    credentialId: requiredEnv('ASTRABOX_E2E_MANAGED_MCP_CREDENTIAL_ID'),
    pluginRevisions,
    oidcIssuer: requiredEnv('ASTRABOX_E2E_OIDC_ISSUER'),
  };
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function commandName(value: unknown): string {
  if (typeof value === 'string') return value.replace(/^\/+/, '').trim();
  if (!value || typeof value !== 'object') return '';
  const item = value as Record<string, unknown>;
  return String(item.name || item.command || '').replace(/^\/+/, '').trim();
}

function hasCommand(names: string[], command: string): boolean {
  return names.some((name) => name === command || name.endsWith(`:${command}`));
}

function shellLiteral(value: string): string {
  return `'${value.replace(/'/g, `'"'"'`)}'`;
}

function terminalFrames(raw: string): Array<Record<string, unknown>> {
  const frames: Array<Record<string, unknown>> = [];
  for (const line of raw.split('\n')) {
    if (!line.startsWith('data:')) continue;
    try {
      frames.push(JSON.parse(line.slice(5).trim()) as Record<string, unknown>);
    } catch {
      // Terminal keepalives and partial trailing lines are not product frames.
    }
  }
  return frames;
}

function terminalOutput(raw: string): string {
  return terminalFrames(raw)
    .filter((frame) => frame.type === 'stdout' || frame.type === 'stderr')
    .map((frame) => String(frame.text || ''))
    .join('');
}

function expectTerminalSuccess(raw: string, label: string): string {
  const exitCodes = terminalFrames(raw)
    .filter((frame) => frame.type === 'exit')
    .map((frame) => Number(frame.exit_code));
  // The output goes into the failure message, not just the return value. These
  // probes run under `set -e`, where a failing check exits 1 having printed
  // nothing, so the exit code alone names no step — and reporting the code
  // while discarding what the box said costs a whole round to recover from the
  // trace. An empty transcript is itself the finding, and says so.
  const output = terminalOutput(raw);
  expect(
    exitCodes,
    `${label} must settle with exactly one successful terminal exit\n`
      + `--- terminal output ---\n${output || '(the command printed nothing)'}\n-----------------------`,
  ).toEqual([0]);
  return output;
}

function keyvexClientSource(endpoint: string): string {
  return `
const endpoint = ${JSON.stringify(endpoint)};
const protocolVersion = '2025-06-18';
let sessionId = '';

function decodeMessages(text, contentType) {
  if (contentType.includes('application/json')) return [JSON.parse(text)];
  return text.split('\\n')
    .map((line) => line.trim())
    .filter((line) => line.startsWith('data:'))
    .map((line) => JSON.parse(line.slice(5).trim()));
}

async function rpc(message, expectsResponse = true) {
  const headers = {
    accept: 'application/json, text/event-stream',
    'content-type': 'application/json',
    'mcp-protocol-version': protocolVersion,
  };
  if (sessionId) headers['mcp-session-id'] = sessionId;
  const response = await fetch(endpoint, {
    method: 'POST',
    headers,
    body: JSON.stringify(message),
    signal: AbortSignal.timeout(12000),
  });
  if (!response.ok) {
    throw new Error('KeyVex ' + message.method + ' returned HTTP ' + response.status);
  }
  const returnedSessionId = response.headers.get('mcp-session-id') || '';
  if (returnedSessionId && !/^[\\x21-\\x7e]+$/.test(returnedSessionId)) {
    throw new Error('KeyVex returned an invalid MCP session id');
  }
  if (sessionId && returnedSessionId && returnedSessionId !== sessionId) {
    throw new Error('KeyVex changed MCP session id mid-session');
  }
  if (returnedSessionId) sessionId = returnedSessionId;
  const text = await response.text();
  if (!expectsResponse) return undefined;
  const payload = decodeMessages(text, response.headers.get('content-type') || '')
    .find((candidate) => candidate && candidate.id === message.id);
  if (!payload) throw new Error('KeyVex ' + message.method + ' returned no matching response');
  if (payload.error) throw new Error('KeyVex ' + message.method + ' returned a JSON-RPC error');
  return payload.result;
}

(async () => {
  const initialized = await rpc({
    jsonrpc: '2.0',
    id: 1,
    method: 'initialize',
    params: {
      protocolVersion,
      capabilities: {},
      clientInfo: { name: 'astrabox-e2e', version: '1.0.0' },
    },
  });
  if (!initialized || initialized.protocolVersion !== protocolVersion) {
    throw new Error('KeyVex did not negotiate the requested MCP protocol');
  }
  if (!initialized.serverInfo || String(initialized.serverInfo.name).toLowerCase() !== 'keyvex') {
    throw new Error('MCP initialize did not identify the KeyVex server');
  }

  await rpc({ jsonrpc: '2.0', method: 'notifications/initialized' }, false);
  const listed = await rpc({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} });
  const tools = listed && Array.isArray(listed.tools) ? listed.tools : [];
  if (!tools.some((tool) => tool && tool.name === 'get_congressional_trades')) {
    throw new Error('KeyVex tools/list omitted get_congressional_trades');
  }

  const called = await rpc({
    jsonrpc: '2.0',
    id: 3,
    method: 'tools/call',
    params: {
      name: 'get_congressional_trades',
      arguments: { ticker: 'NVDA', limit: 1 },
    },
  });
  if (!called || called.isError === true || !Array.isArray(called.content)) {
    throw new Error('KeyVex tools/call did not return successful content');
  }
  const textResult = called.content.find((item) => item && item.type === 'text');
  if (!textResult || typeof textResult.text !== 'string') {
    throw new Error('KeyVex tools/call returned no text result');
  }
  const result = JSON.parse(textResult.text);
  const rows = Array.isArray(result.results) ? result.results : [];
  if (rows.length < 1 || !rows.some((row) => row && row.ticker === 'NVDA')) {
    throw new Error('KeyVex tools/call returned no NVDA congressional-trade row');
  }

  if (sessionId) {
    const closed = await fetch(endpoint, {
      method: 'DELETE',
      headers: { 'mcp-session-id': sessionId, 'mcp-protocol-version': protocolVersion },
      signal: AbortSignal.timeout(12000),
    });
    if (!closed.ok && closed.status !== 405) {
      throw new Error('KeyVex MCP session cleanup returned HTTP ' + closed.status);
    }
  }
  console.log('KEYVEX_MCP_OK server=keyvex tool=get_congressional_trades ticker=NVDA');
})().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
`.trim();
}

async function invokeOfficialCompsSkill(
  api: AstraApi,
  sessionId: string,
  command: string,
): Promise<void> {
  const raw = await api.streamPrompt(
    sessionId,
    `/${command} NVDA\n不要追问，只加载 comps-analysis 后停止。`,
    'bypassPermissions',
    45_000,
  );
  const frames = terminalFrames(raw);
  const compsInputs = frames.filter((frame) => {
    if (frame.type !== 'tool-input-available' || frame.toolName !== 'Skill') return false;
    const input = frame.input;
    if (!input || typeof input !== 'object' || Array.isArray(input)) return false;
    const skill = String((input as Record<string, unknown>).skill || '');
    return skill === 'comps-analysis' || skill.endsWith(':comps-analysis');
  });
  expect(
    compsInputs.length,
    `/${command} must invoke the official comps-analysis Skill exactly once`,
  ).toBe(1);
  const toolCallId = String(compsInputs[0].toolCallId || '').trim();
  expect(toolCallId, 'the comps-analysis Skill call must expose its engine tool id').not.toEqual('');
  const terminals = frames.filter((frame) => (
    String(frame.toolCallId || '') === toolCallId
    && String(frame.type || '').startsWith('tool-output-')
  ));
  expect(
    terminals.map((frame) => frame.type),
    'the comps-analysis Skill call must have one successful terminal frame',
  ).toEqual(['tool-output-available']);
  const output = typeof terminals[0].output === 'string'
    ? terminals[0].output
    : JSON.stringify(terminals[0].output || '');
  expect(
    /Launching skill: .*comps-analysis/i.test(output),
    'the Skill tool must confirm that comps-analysis was loaded',
  ).toBe(true);
  expect(
    frames.some((frame) => frame.type === 'error'),
    `the /${command} turn must not emit an error`,
  ).toBe(false);
  expect(
    frames.some((frame) => frame.type === 'finish'),
    `the /${command} turn must settle`,
  ).toBe(true);
}

async function invokeManagedSearchTool(
  api: AstraApi,
  sessionId: string,
  managedMcpName: string,
): Promise<void> {
  const raw = await api.streamPrompt(
    sessionId,
    [
      `Call the ${managedMcpName} search_engine MCP tool exactly once with query`,
      '"IBM investor relations" and engine google.',
      'Do not call any other tool. Return the tool result without asking a question.',
    ].join(' '),
    'bypassPermissions',
    45_000,
  );
  const frames = terminalFrames(raw);
  const searchInputs = frames.filter((frame) => {
    if (frame.type !== 'tool-input-available') return false;
    const name = String(frame.toolName || '').toLowerCase();
    return name.includes(managedMcpName.toLowerCase()) && name.includes('search_engine');
  });
  expect(searchInputs.length, 'the Agent must invoke the managed search_engine tool exactly once')
    .toBe(1);
  const input = record(searchInputs[0].input, 'managed search_engine input');
  expect(String(input.query || '').toLowerCase()).toContain('ibm investor relations');
  const toolCallId = String(searchInputs[0].toolCallId || '').trim();
  expect(toolCallId, 'the managed search call must expose its engine tool id').not.toEqual('');
  const terminals = frames.filter((frame) => (
    String(frame.toolCallId || '') === toolCallId
    && String(frame.type || '').startsWith('tool-output-')
  ));
  expect(
    terminals.map((frame) => frame.type),
    'the managed search call must have one successful terminal frame',
  ).toEqual(['tool-output-available']);
  const output = typeof terminals[0].output === 'string'
    ? terminals[0].output
    : JSON.stringify(terminals[0].output || '');
  expect(
    /organic/i.test(output),
    'search_engine must return an organic result set rather than an authorization error',
  ).toBe(true);
  // Shape, not ranking: which domains a live search engine returns for the
  // query is the engine's business and changes run to run. Real organic
  // results carry links; an error payload or an empty set does not.
  expect(
    /https?:\/\//i.test(output),
    'the organic results must carry at least one real link',
  ).toBe(true);
  expect(
    /unauthori[sz]ed|forbidden|authentication failed|authorization failed|rate[- ]?limit|too many requests|quota exceeded|invalid(?: or missing)? (?:api ?)?(?:key|token)/i
      .test(output),
    'the managed search must not return an auth, rate-limit, or quota error payload',
  ).toBe(false);
  expect(frames.some((frame) => frame.type === 'error'), 'the managed search turn must not error')
    .toBe(false);
  expect(frames.some((frame) => frame.type === 'finish'), 'the managed search turn must settle')
    .toBe(true);
}

async function invokeManagedAlphaTool(api: AstraApi, sessionId: string): Promise<void> {
  const raw = await api.streamPrompt(
    sessionId,
    [
      'Call the alpha_vantage TIME_SERIES_DAILY MCP tool exactly once with symbol IBM',
      'and outputsize compact.',
      'Do not call any other tool. Return the tool result without asking a question.',
    ].join(' '),
    'bypassPermissions',
    45_000,
  );
  const frames = terminalFrames(raw);
  const alphaInputs = frames.filter((frame) => {
    if (frame.type !== 'tool-input-available') return false;
    const name = String(frame.toolName || '').toLowerCase();
    return name.includes('alpha_vantage') && name.includes('time_series_daily');
  });
  expect(alphaInputs.length, 'the Agent must invoke Alpha Vantage TIME_SERIES_DAILY exactly once')
    .toBe(1);
  const input = record(alphaInputs[0].input, 'Alpha Vantage TIME_SERIES_DAILY input');
  expect(String(input.symbol || '').toUpperCase()).toBe('IBM');
  expect(String(input.outputsize || '').toLowerCase()).toBe('compact');
  const toolCallId = String(alphaInputs[0].toolCallId || '').trim();
  expect(toolCallId, 'the Alpha Vantage call must expose its engine tool id').not.toEqual('');
  const terminals = frames.filter((frame) => (
    String(frame.toolCallId || '') === toolCallId
    && String(frame.type || '').startsWith('tool-output-')
  ));
  expect(
    terminals.map((frame) => frame.type),
    'the managed Alpha Vantage call must have one successful terminal frame',
  ).toEqual(['tool-output-available']);
  const output = typeof terminals[0].output === 'string'
    ? terminals[0].output
    : JSON.stringify(terminals[0].output || '');
  expect(
    /timestamp,open,high,low,close,volume/i.test(output),
    'Alpha Vantage must return compact daily OHLCV data rather than an authorization error',
  ).toBe(true);
  expect(
    /\d{4}-\d{2}-\d{2},\d+(?:\.\d+)?,\d+(?:\.\d+)?,\d+(?:\.\d+)?,\d+(?:\.\d+)?,\d+/.test(output),
    'Alpha Vantage TIME_SERIES_DAILY must return at least one real data row',
  ).toBe(true);
  expect(
    /\berror\b|unauthori[sz]ed|forbidden|authentication failed|authorization failed|rate[- ]?limit|call frequency|too many requests|quota|invalid(?: or missing)? api(?: |_)key|apikey is invalid|api key is invalid|invalid api call/i
      .test(output),
    'Alpha Vantage must not return an auth, rate-limit, quota, or provider error payload',
  ).toBe(false);
  expect(frames.some((frame) => frame.type === 'error'), 'the Alpha Vantage turn must not error')
    .toBe(false);
  expect(frames.some((frame) => frame.type === 'finish'), 'the Alpha Vantage turn must settle')
    .toBe(true);
}

async function expectCasdoorAdministrator(page: Page, oidcIssuer: string): Promise<void> {
  expect(new URL(oidcIssuer).protocol, 'the configured Casdoor issuer must use HTTP(S)')
    .toMatch(/^https?:$/);
  await page.goto(appPath('/'));
  await expect(page.locator('body')).not.toContainText('Sign in to AstraBox');
  const session = await page.evaluate(async () => {
    const response = await fetch('/api/v1/auth/session', { credentials: 'same-origin' });
    if (!response.ok) throw new Error(`auth session returned HTTP ${response.status}`);
    return response.json() as Promise<{
      authenticated?: boolean;
      user?: { user_id?: string; roles?: string[] };
    }>;
  });
  expect(session.authenticated, 'the Playwright context must carry the Casdoor login').toBe(true);
  expect(String(session.user?.user_id || ''), 'the OIDC session must identify its user').not.toEqual('');
  expect(session.user?.roles, 'Casdoor must map the production administrator role')
    .toContain('admin');
}

/**
 * Poll until the Agent actually holds a claimable prepared runtime.
 *
 * `ready` is an instantaneous reading and five lane workers share this Agent,
 * so a single sample says more about timing than about preparation. The error
 * carries the last payload so a refill can be distinguished from a failure.
 *
 * The budget is a lane budget: the whole test has 180s and replenishment takes
 * seconds. A broken preparation path fails here with its own state rather than
 * at some later step that merely depends on it.
 */
async function waitForClaimablePrewarm(
  platform: PlatformApi,
  agentId: string,
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + PREWARM_READY_MS;
  let last: Record<string, unknown> = {};
  while (Date.now() < deadline) {
    last = await platform.preparedRuntime(agentId);
    if (last.ready === true && Number(last.prepared_count || 0) > 0) return last;
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
  throw new Error(
    `the Agent held no claimable prepared runtime within ${PREWARM_READY_MS}ms; `
      + `last status=${JSON.stringify(last)}`,
  );
}

function runtimePaths(
  detail: AdminSessionRecord,
): { configDir: string; homeDir: string; pluginRepo: string } {
  const identity = record(detail.runtime_identity, 'READY runtime_identity');
  const configDir = String(identity.config_dir || '').trim();
  const homeDir = String(identity.home_dir || '').trim();
  expect(configDir, 'READY runtime_identity must expose config_dir').toMatch(/^\/.+/);
  expect(homeDir, 'READY runtime_identity must expose home_dir').toMatch(/^\/.+/);
  // Read checkout paths from the runtime plan: prepared slots install plugins
  // before a conversation exists, so reconstructing a path from sessionId would
  // inspect a different directory and miss the installed repository.
  const capabilityPlan = record(identity.capability_plan, 'READY capability_plan');
  const plan = Array.isArray(capabilityPlan.plugin_repo_plan)
    ? capabilityPlan.plugin_repo_plan
    : [];
  expect(plan.length, 'READY capability_plan must plan the Agent plugin repository').toBeGreaterThan(0);
  const pluginRepo = String(record(plan[0], 'plugin_repo_plan[0]').target_path || '').trim();
  expect(pluginRepo, 'plugin_repo_plan[0] must name its checkout path').toMatch(/^\/.+/);
  return { configDir, homeDir, pluginRepo };
}

test('authenticated Investment Research uses pinned Plugins and both real MCP data paths', async ({
  page,
  request,
}) => {
  const config = journeyConfiguration();
  await expectCasdoorAdministrator(page, config.oidcIssuer);

  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const agents = (await api.listAgents()).filter((candidate) => candidate.name === config.agentName);
  expect(agents, 'the deployment must have exactly one configured Investment Research Agent')
    .toHaveLength(1);
  const agent = agents[0];
  const agentId = String(agent.agent_id || '').trim();
  expect(agentId).not.toEqual('');
  expect(agent.created_by, 'this journey must use the seeded default Agent').toBe('system');
  expect(agent.visibility, 'the seeded example Agent must remain public').toBe('public');
  expect(agent.prewarm_enabled, 'the production journey requires Agent prewarming').toBe(true);
  expect(String(agent.environment_name || ''), 'the Agent must name its prewarm Environment')
    .not.toEqual('');

  const directServers = record(agent.mcp_servers, 'Agent direct MCP map');
  const direct = record(directServers[config.directMcpName], `direct MCP ${config.directMcpName}`);
  // No `backend`: a server is either a named platform capability or one the
  // sandbox dials, and this one names a URL. What has to hold is the shape the
  // engine is handed — a vendor transport and a TLS endpoint.
  expect(direct.backend, 'a direct MCP server carries no backend field').toBeUndefined();
  expect(direct.type).toBe('http');
  const directUrl = new URL(String(direct.url || ''));
  expect(directUrl.protocol).toBe('https:');
  expect(directUrl.hostname, 'the direct public-data provider must be KeyVex').toBe('mcp.keyvex.com');

  const pluginRepos = Array.isArray(agent.plugin_repos)
    ? agent.plugin_repos.map((item, index) => record(item, `plugin_repos[${index}]`))
    : [];
  expect(pluginRepos, 'the default Agent must have exactly the reviewed official Plugin repo')
    .toHaveLength(1);
  expect(pluginRepos[0].url).toBe(OFFICIAL_PLUGIN_REPOSITORY);
  expect(pluginRepos.map((repo) => String(repo.sha || ''))).toEqual(config.pluginRevisions);
  const pluginPaths = Array.isArray(pluginRepos[0].plugin_paths)
    ? pluginRepos[0].plugin_paths.map(String)
    : [];
  expect(pluginPaths).toEqual(expect.arrayContaining([FINANCIAL_ANALYSIS_PATH, EQUITY_RESEARCH_PATH]));

  const catalog = await api.data<ExtensionCatalog>('GET', `/agents/${agentId}/extensions`);
  const managedMatches = (Array.isArray(catalog.mcp_servers) ? catalog.mcp_servers : [])
    .map((item, index) => record(item, `mcp_servers[${index}]`))
    .filter((item) => item.name === config.managedMcpName);
  expect(managedMatches, 'the LiteLLM managed MCP catalogue entry must be unique').toHaveLength(1);
  const managedId = String(managedMatches[0].id || managedMatches[0].item_id || '').trim();
  expect(managedId, 'the managed MCP catalogue entry must expose its id').not.toEqual('');
  expect((catalog.selected_mcp_server_ids || []).map(String)).toContain(managedId);

  const vault = await platform.getVault(config.vaultId);
  expect(vault.vault_id).toBe(config.vaultId);
  const credentials = await platform.listCredentials(config.vaultId);
  const credentialMatches = credentials.filter((item) => item.credential_id === config.credentialId);
  expect(
    credentialMatches.length,
    'the runner-selected managed MCP credential must be unique',
  ).toBe(1);
  const credentialAuth = record(credentialMatches[0].auth, 'managed MCP credential auth');
  expect(credentialAuth.type).toBe('mcp_static_header');
  expect(credentialAuth.mcp_server_url).toBe(config.managedTargetUrl);
  // The fixture verifies the exact upstream header while configuring LiteLLM.
  // Providers may use a native header or an x-mcp-* gateway rewrite.
  expect(credentialAuth.header_name).toBe(config.managedMcpHeader);
  expect(
    Object.prototype.hasOwnProperty.call(credentialAuth, 'value'),
    'Vault read-back must omit the provider secret value',
  ).toBe(false);
  const secretFieldNames = [
    'token',
    'access_token',
    'refresh_token',
    'client_secret',
    'secret_value',
    'api_key',
    'apiKey',
  ];
  expect(
    secretFieldNames.filter((name) => Object.prototype.hasOwnProperty.call(credentialAuth, name)),
    'Vault read-back must expose no alternate secret field',
  ).toEqual([]);
  const binding = await platform.data<{ vault_ids?: unknown[] }>(
    'GET',
    `/admin/agents/${agentId}/credential-vaults`,
  );
  expect((binding.vault_ids || []).map(String)).toContain(config.vaultId);

  // Wait for the spare rather than sampling for it. Five lane workers open
  // conversations against one Agent, so its single spare is as likely to be
  // claimed and replenishing as sitting ready.
  const prewarm = await waitForClaimablePrewarm(platform, agentId);
  expect(
    String(prewarm.client_pool_name || ''),
    'the prepared-runtime status must name the OpenSandbox client pool',
  )
    .not.toEqual('');
  expect(
    String(prewarm.runtime_generation || ''),
    'the prepared-runtime status must expose its runtime generation',
  )
    .not.toEqual('');

  const created = await api.startConversation(agentId);
  const sessionId = String(created.session_id || '').trim();
  expect(sessionId).not.toEqual('');
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId);
  expect(ready.state).toBe('READY');
  expect(String(ready.sandbox_id || ''), 'the journey must run in a real sandbox').not.toEqual('');
  const { configDir, homeDir, pluginRepo } = runtimePaths(
    await api.adminSessionDetail(sessionId),
  );

  const commandNames = (Array.isArray(ready.slash_commands) ? ready.slash_commands : [])
    .map(commandName)
    .filter(Boolean);
  expect(hasCommand(commandNames, 'comps'), 'the financial-analysis Command must load at READY')
    .toBe(true);
  expect(hasCommand(commandNames, 'earnings'), 'the equity-research Command must load at READY')
    .toBe(true);
  const compsCommand = commandNames.find((name) => name === 'financial-analysis:comps');
  expect(
    compsCommand,
    'the READY command catalogue must preserve the financial-analysis plugin namespace',
  ).toBeTruthy();

  await page.goto(appPath(`/sessions/${sessionId}`), { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 30_000 });
  await page.getByTestId('composer-command-menu-trigger').click();
  const commandMenu = page.getByTestId('slash-command-menu');
  await expect(commandMenu).toBeVisible();
  await expect(
    commandMenu.locator(
      `[data-testid="slash-command-option"][data-command-name="/${compsCommand}"]`,
    ),
    'the visible command catalogue must expose the installed Investment Research command',
  ).toHaveCount(1);
  await page.getByTestId('composer-prompt').fill('');

  await invokeOfficialCompsSkill(api, sessionId, compsCommand!);

  // The checkout still has to be the repository this Agent pins, so the name
  // the runtime planned is checked against the configured URL rather than taken
  // on trust — `pluginRepo` itself comes from `plugin_repo_plan` (runtimePaths).
  const repositorySlug = new URL(OFFICIAL_PLUGIN_REPOSITORY).pathname
    .split('/')
    .filter(Boolean)
    .at(-1)
    ?.replace(/\.git$/, '');
  expect(repositorySlug).toBe('financial-services');
  expect(
    pluginRepo,
    'the planned checkout must be this Agent\'s pinned repository, at index 0',
  ).toMatch(new RegExp(`/00-${repositorySlug}$`));
  const encodedKeyvexClient = Buffer.from(
    keyvexClientSource(directUrl.toString()),
    'utf8',
  ).toString('base64');
  const terminalProbe = [
    'set -euo pipefail',
    `repo=${shellLiteral(pluginRepo)}`,
    'test -d "$repo/.git"',
    'repo_real="$(readlink -f "$repo")"',
    `test "$(git -c safe.directory="$repo_real" -C "$repo" rev-parse HEAD)" = ${shellLiteral(config.pluginRevisions[0])}`,
    `test -f "$repo/${FINANCIAL_ANALYSIS_PATH}/skills/comps-analysis/SKILL.md"`,
    `test -f "$repo/${FINANCIAL_ANALYSIS_PATH}/commands/comps.md"`,
    `test -f "$repo/${EQUITY_RESEARCH_PATH}/skills/earnings-analysis/SKILL.md"`,
    `test -f "$repo/${EQUITY_RESEARCH_PATH}/commands/earnings.md"`,
    "printf 'PINNED_PLUGIN_SKILL_COMMAND_OK=1\\n'",
    "if env | sed 's/=.*//' | grep -Eiq '^(BRIGHT_?DATA(_API)?_(KEY|TOKEN)|BRIGHT_?DATA_APIKEY|ALPHA_VANTAGE(_API)?_(KEY|TOKEN)|ALPHAVANTAGE(_API)?_(KEY|TOKEN)|ALPHA_VANTAGE_APIKEY|ALPHAVANTAGE_APIKEY)$'; then exit 91; fi",
    `if find ${shellLiteral(homeDir)} ${shellLiteral(configDir)} -type f \\( -iname '*bright*data*api*key*' -o -iname '*brightdata*api*key*' -o -iname '*bright*data*api*token*' -o -iname '*brightdata*api*token*' -o -iname '*alpha*vantage*api*key*' -o -iname '*alphavantage*api*key*' -o -iname '*alpha*vantage*api*token*' -o -iname '*alphavantage*api*token*' \\) -print -quit 2>/dev/null | grep -q .; then exit 92; fi`,
    "printf 'PROVIDER_SECRET_NAMES_ABSENT=1\\n'",
    `printf '%s' ${shellLiteral(encodedKeyvexClient)} | base64 -d | node`,
  ].join('; ');
  // The probe ends in a live call to a third-party MCP server, so its budget
  // has to clear that latency with room, not sit on it: this whole test runs
  // in 41-44s when KeyVex answers normally, which left the old 45s budget with
  // no margin at all and turned an ordinary slow response into a failed round.
  // A probe that returns when it is done costs nothing extra for waiting.
  const terminalOutput = expectTerminalSuccess(
    await api.runTerminalCommand(
      sessionId,
      terminalProbe,
      undefined,
      parseTimeoutEnv('ASTRABOX_E2E_IR_TERMINAL_PROBE_TIMEOUT_MS', 90_000),
    ),
    'integrated Plugin, secret, and KeyVex probe',
  );
  expect(terminalOutput.includes('PINNED_PLUGIN_SKILL_COMMAND_OK=1')).toBe(true);
  expect(terminalOutput.includes('PROVIDER_SECRET_NAMES_ABSENT=1')).toBe(true);
  expect(
    terminalOutput.includes(
      'KEYVEX_MCP_OK server=keyvex tool=get_congressional_trades ticker=NVDA',
    ),
    'KeyVex must complete initialize, initialized, tools/list, and tools/call',
  ).toBe(true);

  if (config.managedMcpName === 'bright_data') {
    await invokeManagedSearchTool(api, sessionId, config.managedMcpName);
  } else if (config.managedMcpName === 'alpha_vantage') {
    await invokeManagedAlphaTool(api, sessionId);
  } else {
    throw new Error(`no managed MCP data oracle is defined for ${config.managedMcpName}`);
  }
});
