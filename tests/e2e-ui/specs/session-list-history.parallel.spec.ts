import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, postgresTextRows, sqlLiteral } from '../fixtures/dbOracle';
import { POSTGRES_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';
import { trackSessions } from '../fixtures/sessionCleanup';
import { apiPath, appPath } from '../fixtures/env';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

/** One `sessions` row as the listing boundary is judged from it. */
interface SessionRow {
  session_id: string;
  user_id: string;
  /** `updated_at` as epoch seconds, so the order below compares numbers. */
  updated_epoch: number;
  deleted: boolean;
  hidden: boolean;
}

function sessionRows(where: string): SessionRow[] {
  return postgresRows<SessionRow>(
    `SELECT session_id, user_id, extract(epoch FROM updated_at)::float8 AS updated_epoch, deleted, hidden `
      + `FROM sessions WHERE ${where}`,
  );
}

/** Set columns on one `sessions` row; the ids of the rows it changed. */
function patchSessionRow(sessionId: string, assignments: string): string[] {
  return postgresTextRows(
    requireServiceContainer(POSTGRES_CONTAINER_HANDLE),
    process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox',
    `UPDATE sessions SET ${assignments} WHERE session_id = ${sqlLiteral(sessionId)} RETURNING session_id`,
  );
}

test('cursor pages retain every visible conversation once and exclude hidden and foreign histories', async ({ request, page }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await Promise.all(Array.from({ length: 5 }, async () => {
    const item = await api.startConversation(String(agent.agent_id));
    sessions.push(item.session_id);
    return item;
  }));
  await Promise.all(created.map((item) => api.waitForSessionReady(item.session_id)));
  const [original] = sessionRows(`session_id = ${sqlLiteral(created[0].session_id)}`);
  expect(original?.user_id).toBeTruthy();
  const userId = String(original!.user_id);
  for (const item of created) {
    expect(patchSessionRow(item.session_id, `updated_at = TIMESTAMPTZ '2099-01-01T00:00:00Z'`)).toHaveLength(1);
  }
  const hiddenId = created[3].session_id;
  const foreignId = created[4].session_id;
  expect(patchSessionRow(hiddenId, 'hidden = true')).toHaveLength(1);
  // A test-owned durable row with a different owner tests the listing boundary;
  // it does not claim a second principal's runtime/history is exercised here.
  expect(patchSessionRow(foreignId, `user_id = ${sqlLiteral(randomUUID())}`)).toHaveLength(1);
  // The listing's promise: the owner's sessions that are neither deleted nor
  // hidden, newest activity first, ties broken by session id.
  const nativeVisible = sessionRows(`user_id = ${sqlLiteral(userId)}`)
    .filter((row) => !row.deleted && !row.hidden)
    .sort((left, right) => (
      right.updated_epoch - left.updated_epoch
      || (left.session_id < right.session_id ? 1 : left.session_id > right.session_id ? -1 : 0)
    ));
  const expectedIds = nativeVisible.map((row) => String(row.session_id));
  expect(expectedIds.slice(0, 3).sort()).toEqual(created.slice(0, 3).map((item) => item.session_id).sort());
  const received: string[] = [];
  const cursors: string[] = [];
  let cursor: string | null = null;
  do {
    const suffix = cursor ? `&cursor=${encodeURIComponent(cursor)}` : '';
    const response = await request.get(apiPath(`/sessions?page=1&limit=2${suffix}`));
    expect(response.ok(), await response.text()).toBe(true);
    const data = (await response.json()).data as {
      sessions: Array<{ session_id: string }>; has_more: boolean; next_cursor: string | null;
    };
    const ids = data.sessions.map((row) => row.session_id);
    expect(ids).toEqual(expectedIds.slice(received.length, received.length + 2));
    received.push(...ids);
    expect(data.has_more).toBe(received.length < expectedIds.length);
    cursor = data.next_cursor;
    if (data.has_more) {
      expect(cursor).toBeTruthy();
      expect(cursors).not.toContain(cursor);
      cursors.push(cursor!);
    } else {
      expect(cursor).toBeNull();
    }
  } while (cursor);
  expect(received).toEqual(expectedIds);
  expect(new Set(received).size).toBe(received.length);

  await page.goto(appPath('/agents'), { waitUntil: 'domcontentloaded' });
  for (const item of created.slice(0, 3)) {
    await expect(page.locator(`[data-testid="session-row"][data-session-id="${item.session_id}"]`)).toHaveCount(1);
  }
  for (const id of [hiddenId, foreignId]) {
    await expect(page.locator(`[data-testid="session-row"][data-session-id="${id}"]`)).toHaveCount(0);
  }
  await test.info().attach('session-cursor-native-oracle.json', {
    body: JSON.stringify({ created: created.map((item) => item.session_id), expectedIds, received, hiddenId, foreignId, cursors }),
    contentType: 'application/json',
  });
  // Restore this fixture's owner only on success, before ordinary API cleanup.
  expect(patchSessionRow(foreignId, `user_id = ${sqlLiteral(userId)}`)).toHaveLength(1);
});

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
    session.state === 'READY' && session.last_response?.outcome === 'completed'
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
  const runtimeProfileName = String(seeded.runtime_profile_name || '').trim();
  expect(runtimeProfileName, 'seeded default agent must name an environment').not.toEqual('');
  const models = await api.listEnvironmentModels(runtimeProfileName);
  const model = models.find((item) => item && !item.includes('*'));
  expect(model, `${runtimeProfileName} must expose a concrete model`).toBeTruthy();
  const agentName = `__e2e_session_list_history_${Date.now()}`;

  try {
    const agent = await api.createAgent({
      name: agentName,
      model,
      runtime_profile_name: runtimeProfileName,
      prepared_capacity: 0,
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

  for (const path of ['/agents', `/sessions/${created.session_id}`]) {
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
