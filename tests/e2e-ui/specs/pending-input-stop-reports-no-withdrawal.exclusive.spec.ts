/** Stopping an empty response set must explain retained inputs and preserve their history. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, sessionInputRows, sessionResponseRows, sqlLiteral } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();

function reader(scene: Record<string, unknown>): string {
  return execFileSync('docker', [
    'exec', '-i', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-', JSON.stringify(scene),
  ], { input: `import asyncio, json, sys
from dataclasses import asdict
from astrabox.deploy.onebox import ensure_database_wiring
ensure_database_wiring()
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import ReaderLease, SessionRuntimeRepository
async def run(scene):
    try:
        repo = SessionRuntimeRepository()
        if 'lease' in scene:
            await repo.release_reader(ReaderLease(**scene['lease']))
        else:
            await SessionRepository().create_session({
                'session_id': scene['session_id'], 'user_id': scene['user_id'],
                'agent_id': scene['agent_id'], 'engine_kind': scene['engine_kind'],
                'session_kind': 'agent_chat', 'state': 'READY',
                'title': 'Pending input stop',
                'workspace_ref': {'kind': 'agent', 'agent_id': scene['agent_id']},
            })
            lease = await repo.claim_reader(scene['session_id'], owner='pending-stop-fixture', ttl_s=240)
            assert lease is not None, 'the fixture requires an idle Session without a reader'
            print(json.dumps(asdict(lease)))
    finally:
        await get_persistence().dispose()
asyncio.run(run(json.loads(sys.argv[1])))
`, encoding: 'utf8', timeout: 15_000 }).trim();
}

test('a pending input refuses stop explicitly and can take its whole conversation offline through Archive', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const environments = await api.data<Array<{ name: string; engine_kind: string }>>('GET', '/admin/environments');
  const environment = environments.find((item) => item.name === agent.environment_name);
  expect(environment?.engine_kind).toBeTruthy();
  const user = await api.data<{ user_id: string }>('GET', '/user/current');
  const sid = randomUUID();
  sessions.push(sid);
  // The disconnected fixture owns a real reader lease; normal API acceptance still writes the input.
  const lease = JSON.parse(reader({ session_id: sid, user_id: user.user_id,
    agent_id: agent.agent_id, engine_kind: environment!.engine_kind }));
  expect(postgresRows<{ owner_type: string | null }>(
    `SELECT owner_type FROM sessions WHERE session_id=${sqlLiteral(sid)}`,
  )[0].owner_type).toBeNull();
  expect(await api.data('POST', `/admin/sessions/${sid}/history-import`, { messages: [
    { source_id: 'before-user', role: 'user', created_at: '2026-01-01T00:00:00Z',
      blocks: [{ type: 'text', text: `Saved request ${sid}` }] },
    { source_id: 'before-assistant', role: 'assistant', completion: 'complete', created_at: '2026-01-01T00:00:01Z',
      blocks: [{ type: 'thinking', thinking: 'Recorded reasoning remains available.' },
        { type: 'text', text: `Saved answer ${sid}` }] },
  ] })).toMatchObject({ imported: 2, unchanged: 0 });
  const idle = await request.post(apiPath(`/sessions/${sid}/interrupt`));
  expect(idle.status()).toBe(200);
  expect((await idle.json()).data.status).toBe('idle');

  await page.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
  await openSessionView(page, sid);
  const prompt = `Retained pending input ${sid}`;
  const accepted = await sendPrompt(page, sid, prompt);
  expect(accepted.status()).toBe(200);
  const receipt = (await accepted.json()).data;
  expect(receipt.state).toBe('pending');
  const before = sessionInputRows(sid);
  expect(before.filter((row) => row.input_id === receipt.input_id)).toEqual([
    expect.objectContaining({ state: 'pending', content: prompt, delivered_at: null }),
  ]);
  expect(sessionResponseRows(sid).filter((row) => row.status !== 'ended')).toEqual([]);
  const history = await api.getMessages(sid);
  expect(history.messages).toHaveLength(3);
  let stopPosts = 0;
  page.on('request', (outgoing) => {
    if (outgoing.method() === 'POST' && new URL(outgoing.url()).pathname === apiPath(`/sessions/${sid}/interrupt`)) stopPosts += 1;
  });
  const stopped = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === apiPath(`/sessions/${sid}/interrupt`));
  await page.getByRole('button', { name: 'Stop generating', exact: true }).click();
  const refused = await stopped;
  expect(refused.status()).toBe(409);
  expect(await refused.json()).toMatchObject({
    code: 'PENDING_INPUTS_NOT_CANCELLED', data: { input_ids: [receipt.input_id] },
    error: { category: 'state', retryable: false, owner: 'session' },
  });
  await expect(page.getByText('No response is running to stop. Accepted messages were not withdrawn and may still run. To take the conversation offline, use Archive in the conversation list.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop generating', exact: true })).toBeEnabled();
  expect(stopPosts).toBe(1);
  expect(sessionInputRows(sid), 'the refusal changes no recorded input').toEqual(before);
  expect((await api.getMessages(sid)).messages).toEqual(history.messages);

  const row = page.getByTestId('session-row').filter({ has: page.locator(`a[href$="/sessions/${sid}"]`) });
  await row.hover();
  const archived = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === apiPath(`/sessions/${sid}/archive`));
  await row.getByRole('button', { name: 'Archive', exact: true }).click();
  expect((await archived).status()).toBe(200);
  expect(postgresRows<{ archived_at: string | null }>(
    `SELECT archived_at FROM sessions WHERE session_id=${sqlLiteral(sid)}`,
  )[0].archived_at).toBeTruthy();
  reader({ session_id: sid, lease });
  await expect.poll(() => sessionInputRows(sid).find((input) => input.input_id === receipt.input_id))
    .toMatchObject({ state: 'failed', failure: { code: 'CONVERSATION_ENDED' } });
  const after = sessionInputRows(sid);
  expect(after.filter((input) => input.input_id !== receipt.input_id))
    .toEqual(before.filter((input) => input.input_id !== receipt.input_id));
  expect(after.find((input) => input.input_id === receipt.input_id))
    .toMatchObject({ content: prompt, delivered_at: null, admitted_at: null, response_id: null });
  const content = (messages: typeof history.messages) => messages.map(({ id, role, parts }) => ({ id, role, parts }));
  expect(content((await api.getMessages(sid)).messages)).toEqual(content(history.messages));
  expect(sessionResponseRows(sid).filter((response) => response.status !== 'ended')).toEqual([]);
});
