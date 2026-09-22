/** Clearing a saved list must survive the real form, update API and reload. */
import { expect, test, type Locator, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { onPassOnly } from '../fixtures/sessionCleanup';

let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
  agentId = '';
});

async function saveAndReload(page: Page, api: AstraApi, field: string, expected: unknown) {
  const saved = page.waitForResponse((response) => (
    response.request().method() === 'PUT'
    && new URL(response.url()).pathname === apiPath(`/agents/${agentId}`)
  ));
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  expect((await saved).status(), `saving ${field} must succeed`).toBe(200);
  const record = await api.data<Record<string, unknown>>('GET', `/agents/${agentId}`);
  expect(record[field], `the server must persist the explicit ${field} list`).toEqual(expected);
  expect(record.prewarm_enabled, 'this form test must not prepare a sandbox').toBe(false);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('[data-slot="card"]').filter({ has: page.locator('#agent-extension-mcp') })).toBeVisible();
}

async function emptyFieldExample(field: Locator): Promise<string> {
  await expect(field).toHaveValue('');
  const example = await field.getAttribute('placeholder');
  expect(example, 'an empty field must show an example without filling its value').toBeTruthy();
  expect(await field.evaluate((element) => element.matches(':placeholder-shown'))).toBe(true);
  return example!;
}

async function emptyJsonExample(field: Locator): Promise<string> {
  const example = await emptyFieldExample(field);
  const parsed: unknown = JSON.parse(example);
  expect(parsed, 'the JSON example must be a configuration block, not null').not.toBeNull();
  expect(typeof parsed, 'the JSON example must be a configuration block, not prose').toBe('object');
  return example;
}

test('removing the final skill and plugin repository persists empty lists after reload', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(`__e2e_clear_lists_${Date.now()}`);
  agentId = agent.agent_id;
  await page.goto(appPath(`/manage/agents/${encodeURIComponent(agentId)}`));
  await expect(page.locator('[data-slot="card"]').filter({ has: page.locator('#agent-extension-mcp') })).toBeVisible();

  const environment = page.getByRole('combobox', { name: 'Runtime environment', exact: true });
  const model = page.getByRole('combobox', { name: 'Model', exact: true });
  await expect(environment).toBeVisible();
  await expect(model).toBeVisible();
  const environmentBox = await environment.boundingBox();
  const modelBox = await model.boundingBox();
  expect(environmentBox).not.toBeNull();
  expect(modelBox).not.toBeNull();
  expect(environmentBox!.y).toBeLessThan(modelBox!.y);

  const integrations = await api.data<{ services: Array<{ category: string; admin_url: string }> }>(
    'GET', '/admin/integrations',
  );
  const gateway = integrations.services.find((service) => service.category === 'model_gateway');
  expect(gateway, 'the test deployment supplies its model gateway management URL').toBeDefined();
  const manageModels = page.getByRole('link', { name: /Manage models in LiteLLM/ });
  await expect(manageModels).toHaveAttribute('href', gateway!.admin_url);
  await expect(manageModels).toHaveAttribute('target', '_blank');
  await expect(manageModels).toHaveAttribute('rel', 'noopener noreferrer');

  await test.step('examples stay unsaved until accepted and explicitly saved', async () => {
    const description = page.getByRole('textbox', { name: 'Description', exact: true });
    const example = await emptyFieldExample(description);
    const before = await api.data<Record<string, unknown>>('GET', `/agents/${agentId}`);
    await description.focus();
    await description.press('Shift+Tab');
    await expect(description).not.toBeFocused();
    await expect(description).toHaveValue('');
    await description.focus();
    await description.press('Tab');
    await expect(description).toHaveValue(example);
    await expect(description).toBeFocused();
    expect(await description.evaluate((element) => element.matches(':placeholder-shown'))).toBe(false);
    await description.fill('A developer-authored description.');
    await description.press('Tab');
    await expect(description).not.toBeFocused();
    await expect(description).toHaveValue('A developer-authored description.');
    const after = await api.data<Record<string, unknown>>('GET', `/agents/${agentId}`);
    expect(after.description).toEqual(before.description);
    expect(after.version, 'accepting or editing an example must not save the Agent').toEqual(before.version);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(description).toHaveValue('');
  });

  const options = page.getByRole('textbox', { name: 'Claude SDK options', exact: true });
  await emptyJsonExample(options);
  await options.fill('{"max_turns":7}');
  expect(await options.evaluate((element) => element.matches(':placeholder-shown'))).toBe(false);
  await options.blur();
  await saveAndReload(page, api, 'engine_options', { sdk_options: { max_turns: 7 } });
  expect(JSON.parse(await options.inputValue())).toEqual({ max_turns: 7 });
  await options.fill('{}');
  await options.blur();
  await saveAndReload(page, api, 'engine_options', { sdk_options: {} });
  expect(JSON.parse(await options.inputValue())).toEqual({});
  await options.fill('');
  await options.blur();
  await saveAndReload(page, api, 'engine_options', {});
  await expect(options).toHaveValue('');

  const descriptor = String(process.env.ASTRABOX_E2E_REAL_SKILL_DESCRIPTOR
    || 'https://github.com/anthropics/skills.git@main#skills/skill-creator');
  const skills = page.getByRole('group', { name: 'Custom Skills', exact: true })
    .filter({ has: page.getByRole('button', { name: 'Add', exact: true }) });
  await skills.getByRole('button', { name: 'Add', exact: true }).click();
  const skillExample = await emptyFieldExample(skills.getByRole('textbox'));
  await skills.getByRole('textbox').focus();
  await skills.getByRole('textbox').press('Tab');
  await expect(skills.getByRole('textbox')).toHaveValue(skillExample);
  await expect(skills.getByRole('textbox')).toBeFocused();
  await skills.getByRole('textbox').fill(descriptor);
  await saveAndReload(page, api, 'skills', [descriptor]);
  await expect(skills.getByRole('textbox')).toHaveValue(descriptor);
  await skills.getByRole('button', { name: 'Remove item', exact: true }).click();
  await saveAndReload(page, api, 'skills', []);
  await expect(skills.getByRole('textbox')).toHaveCount(0);

  const repositories = [{
    url: String(process.env.ASTRABOX_E2E_REAL_PLUGIN_REPOSITORY
      || 'https://github.com/anthropics/claude-plugins-official.git'),
    protocol: 'https',
    deploy_key_secret_name: '',
    branch: 'main',
    depth: 1,
    sha: '',
    plugin_paths: [String(process.env.ASTRABOX_E2E_REAL_PLUGIN_PATH || 'plugins/frontend-design')],
  }];
  const plugins = page.getByRole('textbox', { name: 'Plugin repositories', exact: true });
  await emptyJsonExample(plugins);
  await emptyJsonExample(page.locator('#agent-default_repo'));
  await plugins.fill(JSON.stringify(repositories));
  await plugins.blur();
  await saveAndReload(page, api, 'plugin_repos', repositories);
  expect(JSON.parse(await plugins.inputValue())).toEqual(repositories);
  await plugins.fill('[]');
  await plugins.blur();
  await saveAndReload(page, api, 'plugin_repos', []);
  expect(JSON.parse(await plugins.inputValue())).toEqual([]);

  await test.step('managed and custom runtime configuration share named groups and save together', async () => {
    const tools = page.locator('[data-slot="card"]').filter({
      has: page.locator('#agent-extension-mcp'),
    });
    await expect(tools).toHaveCount(1);
    await expect(tools.getByRole('heading', { name: 'MCP, Skills and Plugins', exact: true })).toBeVisible();
    const mcpGroup = tools.getByRole('group', { name: 'MCP configuration', exact: true });
    const skillGroup = tools.getByRole('group', { name: 'Skill configuration', exact: true });
    for (const group of [mcpGroup, skillGroup]) {
      await expect(group).toHaveCount(1);
      await expect(group).toHaveJSProperty('tagName', 'FIELDSET');
      await expect(group.locator(':scope > legend')).toBeVisible();
    }
    await expect(mcpGroup.getByRole('combobox', { name: 'LiteLLM-managed MCP', exact: true })).toBeVisible();
    await expect(mcpGroup.getByRole('textbox', { name: 'Custom MCP', exact: true })).toBeVisible();
    await expect(skillGroup.getByRole('combobox', { name: 'LiteLLM-managed Skills', exact: true })).toBeVisible();
    await expect(skillGroup.getByRole('group', { name: 'Custom Skills', exact: true })).toBeVisible();
    await expect(mcpGroup.locator('#agent-extension-skills')).toHaveCount(0);
    await expect(skillGroup.locator('#agent-extension-mcp')).toHaveCount(0);
    await expect(mcpGroup.locator('#agent-plugin_repos')).toHaveCount(0);
    await expect(skillGroup.locator('#agent-plugin_repos')).toHaveCount(0);
    for (const id of ['agent-extension-mcp', 'agent-extension-skills', 'agent-mcp_servers', 'agent-plugin_repos']) {
      await expect(tools.locator(`[id="${id}"]`)).toBeVisible();
    }
    const workspace = page.locator('[data-slot="card"]').filter({
      has: page.locator('#agent-default_repo'),
    });
    await expect(workspace).toHaveCount(1);
    await expect(tools.locator('#agent-default_repo')).toHaveCount(0);
    await expect(tools.getByRole('group', { name: 'Custom Skills', exact: true })).toBeVisible();
    await expect(workspace.locator('#agent-extension-mcp')).toHaveCount(0);

    const customMcp = mcpGroup.getByRole('textbox', { name: 'Custom MCP', exact: true });
    const mcpExample = await emptyJsonExample(customMcp);
    const beforeExample = await api.data<Record<string, unknown>>('GET', `/agents/${agentId}`);
    await customMcp.focus();
    await customMcp.press('Tab');
    await expect(customMcp).toHaveValue(mcpExample);
    await expect(customMcp).toBeFocused();
    expect(await customMcp.evaluate((element) => element.matches(':placeholder-shown'))).toBe(false);
    await customMcp.blur();
    const afterExample = await api.data<Record<string, unknown>>('GET', `/agents/${agentId}`);
    expect(afterExample.mcp_servers).toEqual(beforeExample.mcp_servers);
    expect(afterExample.version, 'accepting a JSON example still requires an explicit Save').toEqual(beforeExample.version);
    await customMcp.fill('');
    await customMcp.blur();

    const catalog = await api.data<{
      mcp_servers: Array<{ id: string; name: string }>;
      selected_mcp_server_ids: string[];
      selected_skill_ids: string[];
    }>('GET', `/agents/${agentId}/extensions`);
    expect(catalog.mcp_servers.length, 'the fixture exposes a managed MCP catalog').toBeGreaterThan(0);
    const managed = catalog.mcp_servers[0];
    // The open popup hides surrounding headings from the accessibility tree.
    const managedMcp = page.locator('#agent-extension-mcp');
    await managedMcp.click();
    await managedMcp.fill(managed.name);
    await page.getByRole('option', { name: managed.name, exact: true }).click();
    await page.keyboard.press('Escape');

    const directMcp = { local_form_fixture: { command: 'true', args: [] } };
    await customMcp.fill(JSON.stringify(directMcp));
    expect(await customMcp.evaluate((element) => element.matches(':placeholder-shown'))).toBe(false);
    await customMcp.blur();
    await skills.getByRole('button', { name: 'Add', exact: true }).click();
    await skills.getByRole('textbox').fill(descriptor);
    await plugins.fill(JSON.stringify(repositories));
    await plugins.blur();
    await expect(tools.getByRole('button', { name: 'Save', exact: true })).toHaveCount(1);
    const agentSaved = page.waitForResponse((response) => response.request().method() === 'PUT'
      && new URL(response.url()).pathname === apiPath(`/agents/${agentId}`));
    const catalogSaved = page.waitForResponse((response) => response.request().method() === 'PUT'
      && new URL(response.url()).pathname === apiPath(`/agents/${agentId}/extensions`));
    await tools.getByRole('button', { name: 'Save', exact: true }).click();
    expect((await agentSaved).status()).toBe(200);
    expect((await catalogSaved).status()).toBe(200);
    const stored = await api.data<Record<string, unknown>>('GET', `/agents/${agentId}`);
    expect(stored.mcp_servers).toEqual(directMcp);
    expect(stored.skills).toEqual([descriptor]);
    expect(stored.plugin_repos).toEqual(repositories);
    expect(stored.prewarm_enabled).toBe(false);
    const savedCatalog = await api.data<typeof catalog>('GET', `/agents/${agentId}/extensions`);
    expect(savedCatalog.selected_mcp_server_ids).toEqual([managed.id]);
    expect(savedCatalog.selected_skill_ids).toEqual(catalog.selected_skill_ids);

    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(tools.locator(`[aria-label="${managed.name}"]`)).toBeVisible();
    await expect(tools.locator('#agent-extension-skills')).toBeVisible();
    await expect(skills.getByRole('textbox')).toHaveValue(descriptor);
    expect(JSON.parse(await tools.locator('#agent-mcp_servers').inputValue())).toEqual(directMcp);
    expect(JSON.parse(await plugins.inputValue())).toEqual(repositories);
    await expect(tools.getByRole('button', { name: 'Save', exact: true })).toHaveCount(0);
  });
});
