import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { apiPath, appPath } from '../fixtures/env';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('unsent text survives session switching and reload without returning after send', async ({ request, page }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const first = await api.startConversation(String(agent.agent_id));
  sessions.push(first.session_id);
  const second = await api.startConversation(String(agent.agent_id));
  sessions.push(second.session_id);
  await Promise.all([
    api.waitForSessionReady(first.session_id),
    api.waitForSessionReady(second.session_id),
  ]);
  const firstDraft = `DRAFT_${Date.now()}: Say hello briefly without using tools.`;
  const secondDraft = 'A separate unsent draft for the second conversation.';
  const composer = page.getByTestId('composer-prompt');

  await openSessionView(page, first.session_id);
  await composer.fill(firstDraft);
  await openSessionView(page, second.session_id);
  await expect(composer).toHaveValue('');
  await composer.fill(secondDraft);
  await openSessionView(page, first.session_id);
  await expect(composer).toHaveValue(firstDraft);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(composer).toHaveValue(firstDraft);
  expect((await api.getMessages(first.session_id)).messages.filter((message) => message.role === 'user')).toHaveLength(0);

  const assistantCount = await api.assistantCount(first.session_id);
  await sendPrompt(page, first.session_id, firstDraft);
  await api.waitForAssistantMessageCount(first.session_id, assistantCount);
  await api.waitForSession(first.session_id, (session) => (
    session.state === 'READY' && session.last_turn_status === 'COMPLETED'
  ));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(composer).toHaveValue('');
  expect((await api.getMessages(first.session_id)).messages.filter((message) => message.role === 'user')).toHaveLength(1);

  await openSessionView(page, second.session_id);
  await expect(composer).toHaveValue(secondDraft);
  await composer.fill('');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(composer).toHaveValue('');
});

test('session history stays listed after its Agent is deleted', async ({ request, page }) => {
  const api = new AstraApi(request);
  const seeded = await api.defaultAgent();
  const environmentName = String(seeded.environment_name || '').trim();
  expect(environmentName, 'seeded default agent must name an environment').not.toEqual('');
  const models = await api.listEnvironmentModels(environmentName);
  const model = models.find((item) => item && !item.includes('*'));
  expect(model, `${environmentName} must expose a concrete model`).toBeTruthy();
  const agentName = `__e2e_session_list_history_${Date.now()}`;

  try {
    const agent = await api.createAgent({
      name: agentName,
      model,
      environment_name: environmentName,
    });
    const agentId = String(agent.agent_id || '');
    expect(agentId, 'created agent must have an id').not.toEqual('');

    const created = await api.startConversation(agentId);
    const sessionId = created.session_id;
    sessions.push(sessionId);
    const ready = await api.waitForSessionReady(sessionId);
    expect(ready.sandbox_id, 'the historical Session must first have real compute').toBeTruthy();
    const reclaim = await api.terminateSandbox(sessionId);
    expect(reclaim.session_id).toBe(sessionId);
    expect(reclaim.status).toBe('sandbox-reclaimed');
    expect(reclaim.sandbox_id).toBe(ready.sandbox_id);
    const released = await api.getSession(sessionId);
    expect(released.state).toBe('READY');
    expect(released.sandbox_id).toBeFalsy();
    expect(released.runtime_unavailable).toBe(true);

    const listResponse = await request.get(apiPath('/sessions?page=1&limit=50'));
    const listBody = await listResponse.text();
    expect(listResponse.ok(), listBody).toBe(true);
    const listPayload = JSON.parse(listBody) as {
      data?: { sessions?: Array<Record<string, unknown>> };
    };
    expect(listPayload.data?.sessions?.filter((row) => row.session_id === sessionId)).toHaveLength(1);

    const browserListResponsePromise = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === 'GET'
        && url.pathname === apiPath('/sessions')
        && url.searchParams.get('page') === '1';
    });
    await page.goto(appPath('/agents'), { waitUntil: 'domcontentloaded' });
    const browserListResponse = await browserListResponsePromise;
    const browserListBody = await browserListResponse.text();
    expect(browserListResponse.ok(), browserListBody).toBe(true);
    const browserListPayload = JSON.parse(browserListBody) as {
      data?: { sessions?: Array<Record<string, unknown>> };
    };
    const browserListRows = browserListPayload.data?.sessions?.filter(
      (row) => row.session_id === sessionId,
    );
    expect(browserListRows).toHaveLength(1);
    const browserListRow = browserListRows![0];
    expect(browserListRow.state).toBe('READY');
    expect(browserListRow.sandbox_id).toBeFalsy();
    for (const detailField of [
      'slash_commands',
      'slash_command_details',
      'runtime_identity',
      'agentpass_authorization',
      'mcp_proxy_auth_secret',
      'terminal_cwd',
      'engine_session_key',
      'sandbox_endpoint',
      'workspace_ref',
    ]) {
      expect(browserListRow).not.toHaveProperty(detailField);
    }
    expect(Buffer.byteLength(browserListBody, 'utf8')).toBeLessThan(128 * 1024);
    const sidebarRow = page.getByTestId('session-row').filter({
      has: page.locator(`a[href$="/sessions/${sessionId}"]`),
    });
    await expect(sidebarRow.getByText(agentName, { exact: true })).toBeVisible();
    await expect(sidebarRow).toContainText(/Ready|就绪/);
    await expect(page.getByTestId('sessions-empty-hero')).toHaveCount(0);

    const deleted = await api.deleteAgent(agentId);
    expect(String(deleted.state || ''), 'delete should leave a historical DELETED Agent').toBe('DELETED');

    await page.goto(appPath('/agents'), { waitUntil: 'domcontentloaded' });
    await expect(
      page.getByTestId('sessions-page').getByText(agentName, { exact: true }),
      'deleting an Agent must not remove its durable Session from the user history',
    ).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId('sessions-empty-hero')).toHaveCount(0);
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in afterEach;
    // Agent deletion is the state this test deliberately observes.
  }
});

test('session-list failure is displayed instead of being rendered as an empty sidebar', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(String(agent.agent_id));
  sessions.push(created.session_id);
  await api.waitForSessionReady(created.session_id);
  await page.route('**/api/v1/sessions?**', async (route) => {
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({
        code: 'SESSION_LIST_E2E_FAILURE',
        message: 'synthetic session list failure',
      }),
    });
  });

  for (const path of ['/agents', '/assistants', `/sessions/${created.session_id}`]) {
    await test.step(`shared session-list error remains visible on ${path}`, async () => {
      await page.goto(appPath(path), { waitUntil: 'domcontentloaded' });
      await expect(page.getByText(/Request failed|请求失败/)).toBeVisible();
      await expect(page.getByRole('alert').filter({ hasText: 'synthetic session list failure' })).toBeVisible();
      await expect(page.getByTestId('sessions-empty-hero')).toHaveCount(0);
      if (path.startsWith('/sessions/')) {
        await expect(page.getByTestId('run-view')).toBeVisible();
        await expect(page.getByTestId('composer-prompt')).toBeEnabled();
      }
    });
  }

  await page.unroute('**/api/v1/sessions?**');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await expect(page.locator(`a[href$="/sessions/${created.session_id}"]`)).toBeVisible();
  await expect(page.getByText('synthetic session list failure')).toHaveCount(0);
});
