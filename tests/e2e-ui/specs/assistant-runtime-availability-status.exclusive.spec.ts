/** Persisted historical states exercise real API and console reads, without starting compute. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, postgresTextRows, sqlLiteral } from '../fixtures/dbOracle';
import { apiPath, appPath } from '../fixtures/env';
import { POSTGRES_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';
import { onPassOnly } from '../fixtures/sessionCleanup';

const assistantId = randomUUID();
const sessionId = randomUUID();
const error = 'AGENT_RUNTIME_ERROR: hermes runtime start failed: Hermes backend attachment detached';
const sql = (statement: string) => postgresTextRows(
  requireServiceContainer(POSTGRES_CONTAINER_HANDLE),
  process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
  process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox',
  statement,
);

onPassOnly(async () => {
  sql(`DELETE FROM sessions WHERE session_id = ${sqlLiteral(sessionId)}; `
    + `DELETE FROM assistants WHERE assistant_id = ${sqlLiteral(assistantId)}`);
});

test('Assistant history distinguishes an unavailable runtime, startup, readiness and terminal errors without allocating compute', async ({ page, request }) => {
  const api = new AstraApi(request);
  const response = await request.get(apiPath('/user/current'));
  expect(response.ok()).toBe(true);
  const owner = String((await response.json()).data.user_id);
  expect(owner).not.toBe('undefined');
  const environment = await api.assistantEnvironmentName();
  await page.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
  // This is a stored-state display fixture, not a claim to reproduce the deployment failure.
  // Bypass Assistant creation's automatic wake so a history read has no compute to adopt.
  sql(`INSERT INTO assistants (assistant_id, owner_id, display_name, engine_kind, environment_name) `
    + `VALUES (${sqlLiteral(assistantId)}, ${sqlLiteral(owner)}, '__e2e_runtime_status', 'assistant', ${sqlLiteral(environment)}); `
    + `INSERT INTO sessions (session_id, user_id, session_kind, workspace_kind, assistant_id, engine_kind, title, state) `
    + `VALUES (${sqlLiteral(sessionId)}, ${sqlLiteral(owner)}, 'assistant_chat', 'assistant', ${sqlLiteral(assistantId)}, 'assistant', '__e2e_runtime_status', 'READY')`);
  const writes: string[] = [];
  page.on('request', (r) => {
    // The shell ensures a primary catalog record; files/list and read are reads.
    const readPost = ['/assistants/primary', `/sessions/${sessionId}/files/list`, `/sessions/${sessionId}/read`]
      .some((path) => new URL(r.url()).pathname === apiPath(path));
    if (r.method() !== 'GET' && r.url().includes('/api/v1/') && !readPost) {
      writes.push(`${r.method()} ${new URL(r.url()).pathname}`);
    }
  });
  const observed = [];
  for (const [state, unavailable, projected, label] of [
    ['READY', true, 'RECOVERY_REQUIRED', 'Recovery needed'],
    ['CREATING', true, 'CREATING', 'Creating'],
    ['READY', false, 'READY', 'Ready'],
    ['TERMINATED', true, 'TERMINATED', 'Terminated'],
  ] as const) {
    sql(`UPDATE sessions SET state=${sqlLiteral(state)}, runtime_unavailable=${unavailable}, `
      + `last_error=${unavailable ? sqlLiteral(error) : 'NULL'} WHERE session_id=${sqlLiteral(sessionId)}`);
    const detail = await api.getSession(sessionId);
    expect(detail.state).toBe(projected);
    const listed = await request.get(apiPath('/sessions'));
    expect(listed.ok()).toBe(true);
    expect((await listed.json()).data.find((s: { session_id: string }) => s.session_id === sessionId).state).toBe(projected);
    await page.goto(appPath(`/sessions/${sessionId}`));
    const header = page.getByTestId('run-view').locator('header').first();
    await expect(header.getByTestId('status-pill')).toHaveAttribute('data-state', projected);
    await expect(header.getByTestId('status-pill')).toHaveText(label);
    if (state === 'READY' && unavailable || state === 'TERMINATED') {
      await expect(header.getByText(error, { exact: true })).toBeVisible();
    }
    if (!unavailable) await expect(header.getByText('Runtime disconnected', { exact: true })).toHaveCount(0);
    const [stored] = postgresRows<{ state: string; sandbox_id: string | null; runtime_unavailable: boolean }>(
      `SELECT state, sandbox_id, runtime_unavailable FROM sessions WHERE session_id=${sqlLiteral(sessionId)}`,
    );
    expect(stored).toEqual({ state, sandbox_id: null, runtime_unavailable: unavailable });
    observed.push({ stored, projected, label });
  }
  expect(postgresRows(`SELECT assistant_id FROM assistant_workspaces WHERE assistant_id=${sqlLiteral(assistantId)}`)).toEqual([]);
  expect(writes).toEqual([]);
  await test.info().attach('persisted-status-and-visible-labels.json', { body: JSON.stringify(observed), contentType: 'application/json' });
});
