import { expect, test, type Page, type Response } from '@playwright/test';

import { apiPath, appPath } from '../fixtures/env';

type Box = { sandbox_id: string; state: string };
type Agent = { agent_id: string; name: string; model: string };
type Prepared = { ready: boolean; prepared_count: number; sandbox_id: string; client_pool_name: string };

// All writes and reads are driven by visible controls. Responses only identify
// records that those controls created or displayed; no API client is used.
function responseFor(page: Page, path: string, method = 'GET'): Promise<Response> {
  return page.waitForResponse((response) => (
    new URL(response.url()).pathname === apiPath(path)
    && response.request().method() === method
    && !response.request().isNavigationRequest()
  ));
}

async function data<T>(response: Response): Promise<T> {
  expect(response.status(), `browser request ${new URL(response.url()).pathname}`).toBe(200);
  const envelope = await response.json();
  expect(envelope.code).toBe('OK');
  return envelope.data as T;
}

async function inventory(page: Page): Promise<Box[]> {
  const read = responseFor(page, '/admin/sandboxes');
  await page.goto(appPath('/manage/sandboxes'));
  const payload = await data<{ items: Box[]; pagination: { has_next_page: boolean } }>(await read);
  // Refuse an incomplete inventory rather than mistaking a later page for
  // deleted capacity or claiming an unseen cold-created box was prewarmed.
  expect(payload.pagination.has_next_page, 'the complete testbed inventory must fit the visible page').toBe(false);
  return payload.items;
}

async function newRunningBoxes(page: Page, before: Set<string>): Promise<Box[]> {
  let boxes: Box[] = [];
  await expect.poll(async () => {
    boxes = (await inventory(page)).filter((box) => (
      !before.has(box.sandbox_id) && box.state.toLowerCase() === 'running'
    ));
    return boxes.length;
  }, { timeout: 60_000, intervals: [1_000, 2_000] }).toBeGreaterThan(0);
  return boxes;
}

async function preparedCapacity(page: Page, agent: Agent): Promise<Prepared> {
  const read = responseFor(page, `/agents/${agent.agent_id}/prepared-runtime`);
  await page.goto(appPath(`/manage/agents/${agent.agent_id}`));
  await expect(page.getByTestId('agent-prewarm-status')).toBeVisible();
  let status = await data<Prepared>(await read);
  await expect.poll(async () => {
    if (!status.ready || status.prepared_count < 1) {
      const next = responseFor(page, `/agents/${agent.agent_id}/prepared-runtime`);
      await page.getByTestId('agent-prewarm-status').getByRole('button', { name: 'Refresh', exact: true }).click();
      status = await data<Prepared>(await next);
    }
    return status.ready && status.prepared_count > 0;
  }, { timeout: 60_000, intervals: [1_000, 2_000] }).toBe(true);
  await expect(page.getByTestId('prewarm-state')).toHaveText('Ready');
  await expect(page.getByTestId('prewarm-count')).toHaveText(`Available: ${status.prepared_count}`);
  expect(status.sandbox_id).toBeTruthy();
  return status;
}

async function createAgent(page: Page, environment: string, name: string): Promise<Agent> {
  expect(environment, 'the configured prewarm Environment is required').not.toBe('');
  const researchAgent = String(process.env.ASTRABOX_E2E_RESEARCH_AGENT || '').trim();
  expect(researchAgent, 'the deployment must name its proven research Agent').not.toBe('');
  const listed = responseFor(page, '/agents');
  await page.goto(appPath('/agents'));
  const agents = await data<Agent[]>(await listed);
  const reference = agents.find((agent) => agent.agent_id === researchAgent || agent.name === researchAgent);
  expect(reference, 'reuse the deployed Claude model without substituting a test model').toBeTruthy();

  await page.goto(appPath('/manage/agents/new'));
  await page.locator('#agent-new-name').fill(name);
  await page.locator('#agent-new-environment_name').selectOption(environment);
  await page.locator('#agent-new-model').fill(reference!.model);
  await page.getByRole('option', { name: reference!.model, exact: true }).click();
  await page.getByRole('button', { name: /^Show advanced settings/ }).click();
  await page.getByRole('switch', { name: 'Keep a sandbox ready', exact: true }).check();
  await page.locator('[role="switch"][aria-labelledby="agent-new-terminal_panel-label"]').check();
  const created = responseFor(page, '/agents', 'POST');
  await page.getByRole('button', { name: 'Create', exact: true }).click();
  const agent = await data<Agent>(await created);
  await page.waitForURL(new RegExp(`/manage/agents/${agent.agent_id}$`));
  test.info().annotations.push({ type: 'retained-agent', description: page.url() });
  return agent;
}

async function setPrewarm(page: Page, agent: Agent, enabled: boolean): Promise<void> {
  await page.goto(appPath(`/manage/agents/${agent.agent_id}`));
  const control = page.getByRole('switch', { name: 'Keep a sandbox ready', exact: true });
  await control.setChecked(enabled);
  const saved = responseFor(page, `/agents/${agent.agent_id}`, 'PUT');
  await page.locator('[data-slot="card"]').filter({ has: control })
    .getByRole('button', { name: 'Save', exact: true }).click();
  expect((await data<{ prewarm_enabled: boolean }>(await saved)).prewarm_enabled).toBe(enabled);
}

async function startAndIdentify(page: Page, agent: Agent): Promise<{ sessionId: string; sandboxId: string }> {
  await page.goto(appPath('/agents'));
  await expect(page.getByTestId('agent-prewarm-status')).toHaveCount(0);
  const card = page.getByTestId('agent-option').and(page.locator(`[data-agent-name="${agent.name}"]`));
  await card.getByRole('button', { name: 'Start conversation', exact: true }).click();
  await page.waitForURL(/\/sessions\/[^/]+$/);
  const sessionId = new URL(page.url()).pathname.split('/').pop()!;
  test.info().annotations.push({ type: 'retained-session', description: page.url() });
  await expect(page.getByTestId('composer-prompt')).toBeEnabled({ timeout: 60_000 });
  await expect(page.getByTestId('agent-prewarm-status')).toHaveCount(0);
  await page.getByRole('tab', { name: 'Terminal', exact: true }).click();
  await expect(page.getByPlaceholder('Type a command…')).toBeEnabled({ timeout: 30_000 });
  const detail = responseFor(page, `/admin/sessions/${sessionId}/detail`);
  await page.goto(appPath(`/manage/sessions/${sessionId}`));
  const session = await data<{ sandbox_id: string }>(await detail);
  expect(session.sandbox_id).toBeTruthy();
  await expect(page.getByText(session.sandbox_id, { exact: true })).toBeVisible();
  return { sessionId, sandboxId: session.sandbox_id };
}

async function terminal(page: Page, sessionId: string, command: string, output: string): Promise<void> {
  await page.goto(appPath(`/sessions/${sessionId}`));
  await page.getByRole('tab', { name: 'Terminal', exact: true }).click();
  const input = page.getByPlaceholder('Type a command…');
  await expect(input).toBeEnabled({ timeout: 30_000 });
  await input.fill(command);
  await input.press('Enter');
  // Exact output does not match the terminal's echoed command line.
  await expect(page.getByText(output, { exact: true })).toBeVisible({ timeout: 20_000 });
  await expect(input).toBeEnabled();
}

async function installedExtensionCommands(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Commands', exact: true }).click();
  const commands = page.getByRole('dialog');
  await expect(commands.getByText('/skill-creator', { exact: true })).toBeVisible();
  await expect(commands.getByText('/frontend-design:frontend-design', { exact: true })).toBeVisible();
  await page.getByTestId('composer-prompt').fill('');
  await expect(commands).toHaveCount(0);
}

async function createAndAssignVault(page: Page, agent: Agent, secretName: string): Promise<string> {
  await page.goto(appPath('/manage/credentials'));
  await page.getByTestId('credential-vault-create').click();
  await page.getByTestId('credential-vault-name').fill(agent.name);
  await page.getByTestId('credential-vault-save').click();
  await page.waitForURL(/\/manage\/credentials\/vlt_[^/]+$/);
  const vaultId = new URL(page.url()).pathname.split('/').pop()!;
  test.info().annotations.push({ type: 'retained-vault', description: page.url() });
  await page.getByTestId('credential-add').click();
  await page.getByTestId('credential-type').click();
  await page.getByRole('option', { name: 'Environment variable for another service', exact: true }).click();
  await page.getByTestId('credential-target').fill(secretName);
  await page.getByTestId('credential-secret').fill('astrabox-e2e-request-match-secret');
  // The UI intentionally does not authorize cleartext injection. This case
  // checks real prepared-box delivery, not an HTTP probe with a hidden bypass.
  await page.getByTestId('credential-allowed-hosts').fill('example.com');
  const created = responseFor(page, `/admin/vaults/${vaultId}/credentials`, 'POST');
  await page.getByTestId('credential-save').click();
  const credential = await data<{ credential_id: string }>(await created);
  await expect(page.getByTestId('managed-credential-row').filter({ hasText: secretName })).toBeVisible();
  await page.getByTestId('credential-binding-open').click();
  await page.getByTestId('credential-binding-target').click();
  await page.getByRole('option', { name: agent.name, exact: true }).click();
  const assigned = responseFor(page, `/admin/agents/${agent.agent_id}/credential-vaults`, 'PUT');
  await page.getByTestId('credential-binding-save').click();
  await data(await assigned);
  await expect(page.getByTestId('credential-assignment-row').filter({ hasText: agent.name })).toBeVisible();
  return credential.credential_id;
}

test('assigning a Vault after prewarming refreshes the prepared box without resaving the Agent', async ({ page, context }) => {
  const observer = await context.newPage();
  const before = new Set((await inventory(observer)).map((box) => box.sandbox_id));
  const agent = await createAgent(page,
    String(process.env.ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT || '').trim(),
    `e2e-vault-after-prewarm-${Date.now()}`);
  await newRunningBoxes(observer, before);
  const secretName = `PREWARM_TOKEN_${Date.now()}`;
  const credentialId = await createAndAssignVault(page, agent, secretName);
  const binding = `astrabox-vault-${credentialId}`;
  let preparedBox = '';
  // Inspect credentials before claim so claim-time repair cannot conceal stale
  // credentials in unclaimed prepared inventory.
  await expect.poll(async () => {
    const candidates = (await inventory(observer)).filter((box) => (
      !before.has(box.sandbox_id) && box.state.toLowerCase() === 'running'
    ));
    for (const box of candidates) {
      const posture = responseFor(observer, `/admin/sandboxes/${box.sandbox_id}/security`);
      await observer.goto(appPath(`/manage/sandboxes/${box.sandbox_id}`));
      const security = await data<{ available: boolean; binding_names: string[] }>(await posture);
      if (security.available && security.binding_names.includes(binding)) {
        await expect(observer.getByText(binding, { exact: true })).toBeVisible();
        preparedBox = box.sandbox_id;
        return preparedBox;
      }
    }
    return '';
  }, { timeout: 60_000, intervals: [1_000, 2_000] }).not.toBe('');
  expect((await preparedCapacity(observer, agent)).sandbox_id).toBe(preparedBox);
  const claimed = await startAndIdentify(page, agent);
  expect(claimed.sandboxId, 'the credential-ready box must be claimed, not replaced by cold creation').toBe(preparedBox);
  await terminal(page, claimed.sessionId,
    `case "\${${secretName}}" in ASTRABOX-VAULT-CRED::*) printf 'PREWARM_PLACEHOLDER_OK\\n';; *) printf 'PREWARM_PLACEHOLDER_MISSING\\n'; exit 91;; esac`,
    'PREWARM_PLACEHOLDER_OK');
});

test('disabling and reenabling prewarming prepares and claims a usable conversation pool', async ({ page, context }) => {
  const observer = await context.newPage();
  const agent = await createAgent(page,
    String(process.env.ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT || '').trim(),
    `e2e-prewarm-reenable-${Date.now()}`);
  const firstReady = await preparedCapacity(observer, agent);
  const first = await startAndIdentify(page, agent);
  expect(first.sandboxId, 'claim the exact supplier-ready box').toBe(firstReady.sandbox_id);
  await page.getByRole('button', { name: 'Kill', exact: true }).click();
  await page.getByRole('button', { name: 'End Session', exact: true }).click();
  await page.waitForURL(/\/manage\/sessions$/);
  await setPrewarm(page, agent, false);
  await expect.poll(async () => (await inventory(observer)).some((box) => (
    box.sandbox_id === first.sandboxId && box.state.toLowerCase() === 'running'
  )), { timeout: 30_000, intervals: [1_000, 2_000] }).toBe(false);
  await setPrewarm(page, agent, true);
  const nextReady = await preparedCapacity(observer, agent);
  expect(nextReady.client_pool_name).not.toBe(firstReady.client_pool_name);
  const second = await startAndIdentify(page, agent);
  expect(second.sandboxId).not.toBe(first.sandboxId);
  expect(second.sandboxId, 'reenabling must supply a prepared box, not cold-create on claim')
    .toBe(nextReady.sandbox_id);
  await terminal(page, second.sessionId, "printf 'REOPENED_POOL_USABLE\\n'", 'REOPENED_POOL_USABLE');
});

for (const shared of [false, true]) {
test(`an administrator reprepares installed extensions without interrupting an existing conversation${shared ? ' in a shared box' : ''}`, async ({ page, context }) => {
  const observer = await context.newPage();
  const agent = await createAgent(page,
    String((shared ? process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT
      : process.env.ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT) || '').trim(),
    `e2e-prewarm-refresh-${Date.now()}`);
  // Same real public extension sources used by the existing preparation suite.
  const skills = page.locator('[role="group"][aria-labelledby="agent-skills-label"]');
  await skills.getByRole('button', { name: 'Add', exact: true }).click();
  await skills.getByRole('textbox').fill('https://github.com/anthropics/skills.git@main#skills/skill-creator');
  await page.locator('#agent-plugin_repos').fill(JSON.stringify([{
    url: 'https://github.com/anthropics/claude-plugins-official.git',
    protocol: 'https', branch: 'main', depth: 1, plugin_paths: ['plugins/frontend-design'],
  }]));
  const saved = responseFor(page, `/agents/${agent.agent_id}`, 'PUT');
  await page.locator('[data-slot="card"]').filter({ has: skills }).getByRole('button', { name: 'Save', exact: true }).click();
  await data(await saved);
  const initial = await preparedCapacity(observer, agent);
  const first = await startAndIdentify(page, agent);
  expect(first.sandboxId).toBe(initial.sandbox_id);
  // The management terminal HOME is not the engine's native config directory.
  // Check the real prepared cache plus engine-discovered commands in the UI.
  const checkExtensions = "test -s /opt/conversation-runtime/claude-skills-cache/skill-creator/SKILL.md && test -n \"$(find /opt/conversation-runtime/claude-plugin-repos-cache/repos -path '*/frontend-design/skills/frontend-design/SKILL.md' -print -quit)\"";
  await terminal(page, first.sessionId,
    `${checkExtensions} && printf 'before-refresh' > prewarm-refresh-marker && printf 'EXTENSIONS_BEFORE_REFRESH_OK\\n'`,
    'EXTENSIONS_BEFORE_REFRESH_OK');
  await installedExtensionCommands(page);
  const waiting = await preparedCapacity(observer, agent);
  const refreshed = responseFor(observer, `/agents/${agent.agent_id}/prepared-runtime/refresh`, 'POST');
  await observer.getByRole('button', { name: 'Reprepare', exact: true }).click();
  const pending = await data<Prepared>(await refreshed);
  expect(pending.ready).toBe(false);
  expect(pending.prepared_count).toBe(0);
  const replacement = await preparedCapacity(observer, agent);
  expect(replacement.client_pool_name).not.toBe(waiting.client_pool_name);
  expect(replacement.sandbox_id).not.toBe(waiting.sandbox_id);
  const second = await startAndIdentify(page, agent);
  expect(second.sandboxId).toBe(replacement.sandbox_id);
  await terminal(page, second.sessionId, `${checkExtensions} && printf 'EXTENSIONS_AFTER_REFRESH_OK\\n'`, 'EXTENSIONS_AFTER_REFRESH_OK');
  await installedExtensionCommands(page);
  await terminal(page, first.sessionId,
    "test \"$(cat prewarm-refresh-marker)\" = before-refresh && printf 'EXISTING_SESSION_UNCHANGED\\n'",
    'EXISTING_SESSION_UNCHANGED');
  const detail = responseFor(observer, `/admin/sessions/${first.sessionId}/detail`);
  await observer.goto(appPath(`/manage/sessions/${first.sessionId}`));
  expect((await data<{ sandbox_id: string }>(await detail)).sandbox_id).toBe(first.sandboxId);
});
}
