/**
 * E2E: a deployment that runs no extension catalog.
 *
 * The server runs with `ASTRABOX_EXTENSION_PROVIDER=none`, and the run says so
 * with `ASTRABOX_E2E_EXTENSION_PROVIDER=none`; without it Playwright does not
 * collect this file (playwright.config.ts). Each case first reads the
 * deployment's own answer, so a deployment that still runs a catalog fails
 * here instead of passing a different scene.
 *
 * What the reader of the console must see, checked against evidence outside
 * the page:
 *
 * - Opening an Agent, an Assistant or the Assistant defaults sends no catalog
 *   read, assignment or management request — every request the page makes is
 *   recorded — and shows no catalog error. The page did read the extension
 *   provider, which is what keeps it from asking a catalog.
 * - An Agent's own MCP servers, Skills and packages, and an Assistant's MCP
 *   servers, are edited, saved and read back after a reload. The stored record
 *   comes from the API, not from the page that sent it.
 * - The Agent's next conversation uses what was saved: its program lists the
 *   saved Skill, and calls the saved stdio MCP server, whose marker reaches the
 *   reply only through a real call to it.
 * - A catalog selection an Agent still holds from before the catalog was turned
 *   off (written into the Agent's rows in the database, the only way a
 *   deployment without a catalog can hold one) is listed on the page, refuses a
 *   new conversation with EXTENSION_CATALOG_DISABLED, leaves an existing
 *   conversation readable, and, once removed from the page, lets the next
 *   conversation start.
 *
 * Deployment fixtures this reads and never creates: the deployment-proven Agent
 * (`ASTRABOX_E2E_AGENT_NAME`, default "Claude Code") for a RuntimeProfile and
 * model, an enabled RuntimeProfile that runs Assistants, and the PostgreSQL
 * service handle the database oracle needs (`ASTRABOX_E2E_POSTGRES_CONTAINER`
 * outside Compose). The Skill and package sources follow
 * `ASTRABOX_E2E_REAL_SKILL_DESCRIPTOR`, `ASTRABOX_E2E_REAL_PLUGIN_REPOSITORY` and
 * `ASTRABOX_E2E_REAL_PLUGIN_PATH` as the console form spec does.
 */
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { approveGates } from '../fixtures/approveGates';
import { AstraApi, messageText } from '../fixtures/astraApi';
import { postgresTextRows, sqlLiteral } from '../fixtures/dbOracle';
import { apiPath, appPath } from '../fixtures/env';
import { POSTGRES_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

test.use({ locale: 'en-US' });

/** The image's own interpreter; the MCP Python SDK is installed for it. */
const PYTHON = '/usr/local/bin/python3.12';
const SERVER = 'e2e_marker';
// MCPServer is the SDK 2.x name of the server FastMCP was in 1.x; either way
// `run()` serves stdio.
const SERVER_SOURCE = [
  'import os',
  'try:',
  '    from mcp.server import MCPServer as Server',
  'except ImportError:',
  '    from mcp.server.fastmcp import FastMCP as Server',
  'app = Server("e2e-marker")',
  '@app.tool()',
  'def read_marker() -> str:',
  '    """Return this server\'s marker."""',
  '    return os.environ["E2E_MARKER"]',
  'app.run()',
].join('\n');
const SKILL_SOURCE = String(
  process.env.ASTRABOX_E2E_REAL_SKILL_DESCRIPTOR
  || 'https://github.com/anthropics/skills.git@683bc88e56f3e09ba94f7055977f3d3aa499f202#skills/brand-guidelines',
);
/** The Skill's directory, which its `SKILL.md` names it after. */
const SKILL_NAME = (SKILL_SOURCE.includes('#') ? SKILL_SOURCE.split('#').pop()! : SKILL_SOURCE.split('@')[0])
  .replace(/\.git$/, '')
  .split('/')
  .filter(Boolean)
  .pop()!;
const LIST_SKILLS = [
  'Without using any tool, list the names of the Skills available to you, one per line,',
  'exactly as your instructions name them, and nothing else.',
].join(' ');

const sessions = trackSessions();
const agentIds: string[] = [];
const assistantIds: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const agentId of agentIds.splice(0)) await api.deleteAgent(agentId);
  for (const assistantId of assistantIds.splice(0)) await api.deleteAssistant(assistantId);
});

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* the en-US navigator locale still applies */
    }
  });
});

/** Require the deployment this file describes; refuse to test another one. */
async function requireNoCatalog(api: AstraApi): Promise<void> {
  const declared = await api.data<{ provider: unknown }>('GET', '/extension-provider');
  expect(
    declared.provider,
    'this spec needs a deployment whose server runs ASTRABOX_EXTENSION_PROVIDER=none',
  ).toBeNull();
}

/** Every catalog read, assignment or management request a page sends, from now on. */
function catalogRequests(page: Page): { catalog: string[]; providerReads: string[] } {
  const seen = { catalog: [] as string[], providerReads: [] as string[] };
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname;
    if (/\/(extensions|extension-console)(\/|$)/.test(path)) {
      seen.catalog.push(`${request.method()} ${path}`);
    } else if (path === apiPath('/extension-provider')) {
      seen.providerReads.push(path);
    }
  });
  return seen;
}

/**
 * Failure notes about the extension catalog. Every catalog failure the console
 * can show names the extension catalog; other cards (connectors, prepared
 * capacity) may raise notes of their own that say nothing about it.
 */
function catalogErrors(page: Page) {
  return page.getByRole('alert').filter({ hasText: /extension|catalog/i });
}

/**
 * Give an Agent the catalog selection it would keep from before the catalog was
 * turned off: one gateway MCP server (`agent_mcp_assignments`) and one compiled
 * catalog Skill (`agents.catalog_skill_ids` / `catalog_skills`). One statement,
 * so the Agent never holds half of it; anything but one row of each is refused.
 */
function holdStaleCatalogSelection(
  agentId: string,
  mcp: { provider: string; itemId: string },
  skill: { id: string; source: string },
): void {
  const counts = postgresTextRows(
    requireServiceContainer(POSTGRES_CONTAINER_HANDLE),
    process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox',
    `WITH assigned AS (INSERT INTO agent_mcp_assignments (agent_id, provider, item_id, position) `
      + `VALUES (${sqlLiteral(agentId)}, ${sqlLiteral(mcp.provider)}, ${sqlLiteral(mcp.itemId)}, 0) `
      + `RETURNING agent_id), `
      + `compiled AS (UPDATE agents SET catalog_skill_ids = ARRAY[${sqlLiteral(skill.id)}]::text[], `
      + `catalog_skills = ${sqlLiteral(JSON.stringify([skill.source]))}::jsonb `
      + `WHERE agent_id = ${sqlLiteral(agentId)} RETURNING agent_id) `
      + `SELECT (SELECT count(*) FROM assigned), (SELECT count(*) FROM compiled)`,
  );
  expect(counts, 'the stale selection must be one assignment and one compiled Skill on this Agent')
    .toEqual(['1|1']);
}

async function refusal(request: APIRequestContext, method: 'GET' | 'POST', route: string) {
  const response = await request.fetch(apiPath(route), { method });
  return { status: response.status(), code: String((await response.json()).code || '') };
}

/** Everything the program has answered in the conversation, in one string. */
async function replies(api: AstraApi, sessionId: string): Promise<string> {
  return (await api.getMessages(sessionId)).messages
    .filter((message) => message.role === 'assistant')
    .map(messageText)
    .join('\n');
}

test('an Agent page edits its own MCP servers, Skills and packages, and its conversation uses them', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  await requireNoCatalog(api);
  const base = await api.defaultAgent();
  const agent = await api.createAgent({
    name: `__e2e_no_catalog_${randomUUID().slice(0, 8)}`,
    model: base.model,
    runtime_profile_name: base.runtime_profile_name,
    prepared_capacity: 0,
  });
  agentIds.push(agent.agent_id);
  expect(await refusal(request, 'GET', `/agents/${agent.agent_id}/extensions`)).toEqual({
    status: 409,
    code: 'EXTENSION_CATALOG_DISABLED',
  });

  const requests = catalogRequests(page);
  await page.goto(appPath(`/manage/agents/${encodeURIComponent(agent.agent_id)}`));
  const tools = page.locator('[data-slot="card"]').filter({ has: page.locator('#agent-mcp_servers') });
  await expect(tools).toHaveCount(1);
  await expect(tools.locator('#agent-extension-mcp'), 'no catalog picker').toHaveCount(0);
  await expect(tools.locator('#agent-extension-skills'), 'no catalog picker').toHaveCount(0);
  await expect(page.getByRole('button', { name: /^Manage .* in / })).toHaveCount(0);
  await expect(catalogErrors(page), 'no catalog error').toHaveCount(0);

  const marker = `E2E_NO_CATALOG_${randomUUID().replace(/-/g, '').slice(0, 16)}`;
  const mcpServers = {
    [SERVER]: { type: 'stdio', command: PYTHON, args: ['-c', SERVER_SOURCE], env: { E2E_MARKER: marker } },
  };
  const packages = [{
    kind: 'claude-plugin',
    url: String(process.env.ASTRABOX_E2E_REAL_PLUGIN_REPOSITORY
      || 'https://github.com/anthropics/claude-plugins-official.git'),
    protocol: 'https',
    deploy_key_secret_name: '',
    branch: 'main',
    depth: 1,
    sha: '',
    paths: [String(process.env.ASTRABOX_E2E_REAL_PLUGIN_PATH || 'plugins/frontend-design')],
  }];
  const mcpField = tools.getByRole('textbox', { name: 'MCP servers', exact: true });
  await mcpField.fill(JSON.stringify(mcpServers, null, 2));
  await mcpField.blur();
  const skills = tools.getByRole('textbox', { name: 'Skills', exact: true });
  await skills.fill(JSON.stringify([SKILL_SOURCE]));
  await skills.blur();
  const packagesField = tools.getByRole('textbox', { name: 'Packages', exact: true });
  await packagesField.fill(JSON.stringify(packages));
  await packagesField.blur();

  const saved = page.waitForResponse((response) => response.request().method() === 'PUT'
    && new URL(response.url()).pathname === apiPath(`/agents/${agent.agent_id}`));
  await tools.getByRole('button', { name: 'Save', exact: true }).click();
  expect((await saved).status(), 'saving the Agent').toBe(200);
  const stored = await api.getAgent(agent.agent_id);
  expect(stored.mcp_servers).toEqual(mcpServers);
  expect(stored.skills).toEqual([SKILL_SOURCE]);
  expect(stored.packages).toEqual(packages);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(mcpField).toBeVisible();
  expect(JSON.parse(await mcpField.inputValue())).toEqual(mcpServers);
  expect(JSON.parse(await skills.inputValue())).toEqual([SKILL_SOURCE]);
  expect(JSON.parse(await packagesField.inputValue())).toEqual(packages);
  await expect(catalogErrors(page)).toHaveCount(0);
  expect(requests.catalog, 'opening, saving and reloading sends no catalog request').toEqual([]);
  expect(requests.providerReads.length, 'the page reads the extension provider').toBeGreaterThan(0);

  const sessionId = String((await api.startConversation(agent.agent_id)).session_id || '');
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId, 120_000);

  await api.postTurnInput(sessionId, LIST_SKILLS);
  await approveGates(api, sessionId);
  expect(await replies(api, sessionId), 'the saved Skill is loaded').toContain(SKILL_NAME);

  // Posted rather than streamed: a gate raised while no stream holds the turn
  // waits parked for its answer, as it does for a user.
  await api.postTurnInput(sessionId, [
    `Call the read_marker tool of the ${SERVER} MCP server once,`,
    'then reply with exactly the text it returned and nothing else.',
  ].join(' '));
  await approveGates(api, sessionId);
  expect(await replies(api, sessionId), 'only the saved MCP server knows the marker').toContain(marker);
});

test('an Assistant page and the Assistant defaults work without a catalog', async ({ page, request }) => {
  const api = new AstraApi(request);
  await requireNoCatalog(api);
  const assistant = await api.createAssistant({
    display_name: `__e2e_no_catalog_${randomUUID().slice(0, 8)}`,
    runtime_profile_name: await api.assistantEnvironmentName(),
  });
  assistantIds.push(assistant.assistant_id);
  expect(await refusal(request, 'GET', `/assistants/${assistant.assistant_id}/extensions`)).toEqual({
    status: 409,
    code: 'EXTENSION_CATALOG_DISABLED',
  });

  const requests = catalogRequests(page);
  await page.goto(appPath(`/manage/assistants/${encodeURIComponent(assistant.assistant_id)}`));
  const extensions = page.locator('[data-slot="card"]').filter({ has: page.locator('#assistant-mcp_servers') });
  await expect(extensions).toHaveCount(1);
  await expect(extensions.locator('#assistant-extension-mcp'), 'no catalog picker').toHaveCount(0);
  await expect(catalogErrors(page), 'no catalog error').toHaveCount(0);

  const mcpServers = { e2e_local: { type: 'stdio', command: 'true', args: [] } };
  const mcpField = extensions.getByRole('textbox', { name: 'MCP servers', exact: true });
  await mcpField.fill(JSON.stringify(mcpServers));
  await mcpField.blur();
  const saved = page.waitForResponse((response) => response.request().method() === 'PATCH'
    && new URL(response.url()).pathname === apiPath(`/assistants/${assistant.assistant_id}`));
  await extensions.getByRole('button', { name: 'Save', exact: true }).click();
  expect((await saved).status(), 'saving the Assistant').toBe(200);
  expect((await api.getAssistant(assistant.assistant_id)).mcp_servers).toEqual(mcpServers);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(mcpField).toBeVisible();
  expect(JSON.parse(await mcpField.inputValue())).toEqual(mcpServers);
  await expect(catalogErrors(page)).toHaveCount(0);

  // The defaults page lists the layer's own catalog selection only if it holds
  // one; what it holds is read from the server, not assumed.
  const layer = await api.data<Record<string, unknown>>('GET', '/admin/assistant-defaults');
  const snapshot = (layer.extension_catalog ?? {}) as Record<string, unknown>;
  const holdsSelection = ((layer.mcp_assignments as unknown[] | null) ?? []).length > 0
    || ((snapshot.skill_ids as unknown[] | undefined) ?? []).length > 0;
  await page.goto(appPath('/manage/assistant-defaults'));
  await expect(page.getByRole('heading', { name: 'Assistant defaults', exact: true })).toBeVisible();
  await expect(page.getByTestId('unavailable-catalog-selection')).toHaveCount(holdsSelection ? 1 : 0);
  expect(requests.catalog, 'no catalog request from either page').toEqual([]);
});

test('a catalog selection kept from before is shown, refused at use, and removed from the page', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  await requireNoCatalog(api);
  const base = await api.defaultAgent();
  const agent = await api.createAgent({
    name: `__e2e_stale_catalog_${randomUUID().slice(0, 8)}`,
    model: base.model,
    runtime_profile_name: base.runtime_profile_name,
    prepared_capacity: 0,
  });
  agentIds.push(agent.agent_id);
  const existing = String((await api.startConversation(agent.agent_id)).session_id || '');
  sessions.push(existing);
  await api.waitForSessionReady(existing, 120_000);

  // What an Agent stores after selecting from a catalog that has since been
  // turned off: a gateway MCP server and a compiled catalog Skill.
  const staleSkill = 'https://github.com/example/catalog-skills.git#review';
  holdStaleCatalogSelection(
    agent.agent_id,
    { provider: 'litellm', itemId: 'e2e-stale-mcp' },
    { id: 'e2e-stale-skill', source: staleSkill },
  );
  const held = await api.getAgent(agent.agent_id);
  expect(held.mcp_assignments, 'the Agent reads back the stale MCP selection')
    .toEqual([{ provider: 'litellm', item_id: 'e2e-stale-mcp' }]);
  expect(held.extension_catalog, 'the Agent reads back the stale compiled Skill')
    .toEqual({ skill_ids: ['e2e-stale-skill'], skills: [staleSkill] });

  expect(await refusal(request, 'POST', `/agents/${agent.agent_id}/conversations`)).toEqual({
    status: 409,
    code: 'EXTENSION_CATALOG_DISABLED',
  });
  const readable = await request.get(apiPath(`/sessions/${existing}`));
  expect(readable.status(), 'an existing conversation stays readable').toBe(200);

  const requests = catalogRequests(page);
  await page.goto(appPath(`/manage/agents/${encodeURIComponent(agent.agent_id)}`));
  const unavailable = page.getByTestId('unavailable-catalog-selection');
  await expect(unavailable).toContainText('litellm: e2e-stale-mcp');
  await expect(unavailable).toContainText(staleSkill);
  expect(requests.catalog, 'showing the selection asks no catalog').toEqual([]);

  const removed = page.waitForResponse((response) => response.request().method() === 'PUT'
    && new URL(response.url()).pathname === apiPath(`/agents/${agent.agent_id}/extensions`));
  await unavailable.getByRole('button', { name: 'Remove selections', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Remove selections', exact: true }).click();
  expect((await removed).status(), 'removing the selection').toBe(200);
  await expect(unavailable).toHaveCount(0);
  const cleared = await api.getAgent(agent.agent_id);
  expect(cleared.mcp_assignments).toEqual([]);
  expect((cleared.extension_catalog as Record<string, unknown>).skill_ids).toEqual([]);

  const next = String((await api.startConversation(agent.agent_id)).session_id || '');
  sessions.push(next);
  await api.waitForSessionReady(next, 120_000);
});
