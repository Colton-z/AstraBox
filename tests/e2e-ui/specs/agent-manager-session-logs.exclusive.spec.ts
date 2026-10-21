/** Resource authorization uses signed non-admin identities and persisted log fixtures, without compute. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresTextRows, sqlLiteral } from '../fixtures/dbOracle';
import { apiPath, appPath } from '../fixtures/env';
import { POSTGRES_CONTAINER_HANDLE, SERVER_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';
import { onPassOnly } from '../fixtures/sessionCleanup';

const manager = `__e2e_manager_${randomUUID()}`;
const agentIds: string[] = [];
const sessionIds = Array.from({ length: 4 }, () => randomUUID());
const sql = (statement: string) => postgresTextRows(
  requireServiceContainer(POSTGRES_CONTAINER_HANDLE), process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
  process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox', statement,
);
const tar = (archive: Buffer, args: string[]) => execFileSync('tar', args, {
  input: archive, encoding: 'utf8', timeout: 10_000,
});

onPassOnly(async ({ request }) => {
  sql(`DELETE FROM sessions WHERE session_id IN (${sessionIds.map(sqlLiteral).join(',')})`);
  const api = new AstraApi(request);
  for (const id of agentIds) await api.deleteAgent(id);
});

test('an administrator of three Agents can read and batch export only their conversation logs', async ({ browser, request }) => {
  const api = new AstraApi(request);
  const agents = [];
  const entries = sessionIds.map((id, index) => ({
    type: 'assistant', uuid: randomUUID(), sessionId: id,
    message: { role: 'assistant', content: [{ type: 'text', text: `LOG_${manager}_${index}` }] },
  }));
  for (const [index, sessionId] of sessionIds.entries()) {
    const agent = await api.createColdTestAgent(`__e2e_logs_${manager}_${index}`);
    agents.push(agent);
    agentIds.push(agent.agent_id);
    if (index < 3) await api.setAgentAccess(agent.agent_id, { admins: [manager], visibility: 'public', allowed_user_ids: [] });
    // Authorize reading other people's history, not just the manager's own Sessions.
    sql(`INSERT INTO sessions (session_id, user_id, session_kind, workspace_kind, agent_id, engine_kind, title, state) `
      + `VALUES (${sqlLiteral(sessionId)}, 'another-conversation-user', 'agent_chat', 'agent', ${sqlLiteral(agent.agent_id)}, 'claude_code', 'Log fixture', 'READY'); `
      + `WITH scope AS (INSERT INTO transcript_scopes (platform_session_id, project_key, native_session_id, subpath, last_sequence) `
      + `VALUES (${sqlLiteral(sessionId)}, '-workspace', ${sqlLiteral(sessionId)}, '', 1) RETURNING scope_id) `
      + `INSERT INTO transcript_entries (scope_id, seq, append_id, batch_index, entry_json) `
      + `SELECT scope_id, 1, 'fixture', 0, ${sqlLiteral(JSON.stringify(entries[index]))} FROM scope; `
      + `INSERT INTO session_responses (session_id, response_id, origin, status, outcome, ended_at) `
      + `VALUES (${sqlLiteral(sessionId)}, 'fixture-response', 'input', 'ended', 'completed', now()); `
      + `INSERT INTO session_output (session_id, seq, response_id, part) `
      + `VALUES (${sqlLiteral(sessionId)}, 1, 'fixture-response', ${sqlLiteral(JSON.stringify({ type: 'text', text: entries[index].message.content[0].text }))})`);
  }
  // Reuse the real deployed signer, as browser-panel authorization does; login itself is outside this case.
  const token = execFileSync('docker', ['exec', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-c',
    'import os; from astrabox.providers.identity_oidc import mint_session_token; '
    + `print(mint_session_token(user_id=${JSON.stringify(manager)}, email=None, display_name=None, roles=[], `
    + 'casdoor_organization=os.environ.get("ASTRABOX_CASDOOR_ORGANIZATION", "")))',
  ], { encoding: 'utf8' }).trim();
  const origin = String(process.env.ASTRABOX_E2E_CONSOLE_URL).replace(/\/$/, '');
  const context = await browser.newContext({ baseURL: origin, storageState: {
    cookies: [{ name: 'astrabox_session', value: token, domain: new URL(origin).hostname,
      path: '/', expires: -1, httpOnly: true, secure: origin.startsWith('https:'), sameSite: 'Lax' }], origins: [],
  } });
  try {
    const who = await context.request.get(apiPath('/user/current'));
    expect((await who.json()).data).toMatchObject({ user_id: manager, is_admin: false });
    const list = await context.request.get(apiPath('/admin/sessions/all?page_size=2'));
    expect(list.status()).toBe(200);
    expect((await list.json()).data.pagination.total_items).toBe(3);
    const page = await context.newPage();
    await page.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
    await page.goto(appPath('/manage/sessions'));
    await expect(page.getByRole('heading', { name: 'Sessions', exact: true })).toBeVisible();
    for (const agent of agents.slice(0, 3)) await expect(page.getByRole('row').filter({ hasText: agent.name })).toBeVisible();
    await expect(page.getByRole('row').filter({ hasText: agents[3].name })).toHaveCount(0);

    const download = page.waitForEvent('download');
    await page.getByRole('link', { name: /Export 3 transcripts/i }).click();
    const saved = await (await download).createReadStream();
    expect(saved).not.toBeNull();
    const chunks: Buffer[] = [];
    for await (const chunk of saved!) chunks.push(Buffer.from(chunk));
    const archive = Buffer.concat(chunks);
    const expected = sessionIds.slice(0, 3).map((id) => `-workspace/${id}.jsonl`);
    expect(tar(archive, ['-tzf', '-']).trim().split('\n').sort()).toEqual(expected.sort());
    for (const [index, path] of expected.entries()) {
      const exported = tar(archive, ['-xOzf', '-', '--', path]).trim().split('\n').map((line) => JSON.parse(line));
      // Member order is independent of the chronological query order.
      const source = entries.find((entry) => path.includes(entry.sessionId));
      expect(exported, `native fields for archive member ${index}`).toEqual([source]);
    }
    await page.getByRole('row').filter({ hasText: agents[0].name }).click();
    await expect(page).toHaveURL(new RegExp(`/manage/sessions/${sessionIds[0]}$`));
    const content = entries[0].message.content[0].text;
    await expect(page.getByText(content, { exact: true }).last()).toBeVisible();
    await page.getByRole('button', { name: new RegExp(content) }).last().click();
    await expect(page.locator('pre').filter({ hasText: content }).last()).toBeVisible();
    for (const suffix of ['detail', 'trace', 'transcript']) {
      expect((await context.request.get(apiPath(`/admin/sessions/${sessionIds[0]}/${suffix}`))).status()).toBe(200);
      expect((await context.request.get(apiPath(`/admin/sessions/${sessionIds[3]}/${suffix}`))).status()).toBe(403);
    }
    for (const suffix of ['kill', 'evict-runtime', 'history-import']) {
      expect((await context.request.post(apiPath(`/admin/sessions/${sessionIds[3]}/${suffix}`), { data: { messages: [] } })).status()).toBe(403);
    }
    expect((await context.request.get(apiPath('/admin/system/overview'))).status()).toBe(403);
    expect((await context.request.get(apiPath('/admin/logs'))).status()).toBe(403);
    const filtered = await context.request.get(apiPath(`/admin/sessions/transcripts?agent_id=${agentIds[3]}`));
    expect(filtered.status()).toBe(200);
    expect(tar(await filtered.body(), ['-tzf', '-'])).toBe('');
    // ACL removal must immediately shrink both the list and the batch export.
    await api.setAgentAccess(agentIds[0], { admins: [], visibility: 'public', allowed_user_ids: [] });
    const revoked = await context.request.get(apiPath('/admin/sessions/all'));
    expect((await revoked.json()).data.pagination.total_items).toBe(2);
    expect((await context.request.get(apiPath(`/admin/sessions/${sessionIds[0]}/detail`))).status()).toBe(403);
    const remaining = await context.request.get(apiPath('/admin/sessions/transcripts'));
    expect(tar(await remaining.body(), ['-tzf', '-']).trim().split('\n').sort())
      .toEqual(sessionIds.slice(1, 3).map((id) => `-workspace/${id}.jsonl`).sort());
    const killed = await context.request.post(apiPath(`/admin/sessions/${sessionIds[1]}/kill`));
    expect(killed.status()).toBe(200);
  } finally {
    await context.close();
  }
});
