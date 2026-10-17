/** Leaving a conversation cancels its history GETs before a held request is released. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { apiPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();

test('switching private conversations aborts older pages and history refreshes, then permits a fresh read', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const environments = await api.data<Array<{ name: string; engine_kind: string }>>('GET', '/admin/environments');
  const environment = environments.find((item) => item.name === agent.environment_name);
  expect(environment?.engine_kind, 'the fixture must use its Agent Environment\'s declared engine').toBeTruthy();
  const user = await api.data<{ user_id: string }>('GET', '/user/current');
  const ids = [randomUUID(), randomUUID()];
  sessions.push(...ids);
  execFileSync('docker', [
    'exec', '-i', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-',
    JSON.stringify({ user_id: user.user_id, agent_id: agent.agent_id, engine_kind: environment!.engine_kind, ids }),
  ], {
    input: `import asyncio, json, sys
from astrabox.deploy.onebox import ensure_database_wiring
ensure_database_wiring()
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.provider import get_persistence
async def seed(scene):
    try:
        for sid in scene['ids']:
            await SessionRepository().create_session({
                'session_id': sid, 'user_id': scene['user_id'], 'agent_id': scene['agent_id'],
                'session_kind': 'agent_chat', 'state': 'TERMINATED', 'title': 'History cancellation ' + sid,
                'engine_kind': scene['engine_kind'],
                'workspace_ref': {'kind': 'agent', 'agent_id': scene['agent_id']},
            })
    finally:
        await get_persistence().dispose()
asyncio.run(seed(json.loads(sys.argv[1])))
`,
    encoding: 'utf8', timeout: 30_000,
  });
  const prompts = Array.from({ length: 65 }, (_, index) => `History ${ids[0]} round ${index + 1}`);
  const destinationText = `Destination ${ids[1]}`;
  for (const [index, sid] of ids.entries()) {
    const messages = (index === 0 ? prompts : [destinationText]).flatMap((text, round) => [
      { source_id: `user-${round}`, role: 'user', created_at: new Date(Date.UTC(2026, 0, 1, 0, round, 0)).toISOString(),
        blocks: [{ type: 'text', text }] },
      { source_id: `reply-${round}`, role: 'assistant', completion: 'complete',
        created_at: new Date(Date.UTC(2026, 0, 1, 0, round, 20)).toISOString(),
        blocks: [{ type: 'text', text: `Recorded reply ${round + 1}` }] },
    ]);
    expect(await api.data('POST', `/admin/sessions/${sid}/history-import`, { messages }))
      .toMatchObject({ imported: messages.length, unchanged: 0 });
  }
  const source = ids[0];
  const newest = await api.getHistoryBlocks(source);
  expect(newest.messages).toHaveLength(50);
  expect(newest.has_more).toBe(true);
  const older = await api.getHistoryBlocks(source, 50, newest.next_cursor!);
  expect(older.messages).toHaveLength(50);
  expect(messageText(older.messages[0])).toBe(prompts[15]);
  for (const sid of ids) {
    const scene = await api.getSession(sid);
    expect(scene.state).toBe('TERMINATED');
    expect(scene.sandbox_id).toBeFalsy();
  }

  const historyPath = apiPath(`/sessions/${source}/history-blocks`);
  const cdp = await page.context().newCDPSession(page);
  let holdNext: 'older' | 'latest' | null = null;
  const held: Array<{ networkId: string; requestId: string }> = [];
  const failures = new Map<string, { canceled?: boolean; errorText: string }>();
  const responses = new Set<string>();
  const finished = new Set<string>();
  const interceptionErrors: string[] = [];
  cdp.on('Network.loadingFailed', (event) => failures.set(event.requestId, event));
  cdp.on('Network.responseReceived', (event) => responses.add(event.requestId));
  cdp.on('Network.loadingFinished', (event) => finished.add(event.requestId));
  cdp.on('Fetch.requestPaused', (event) => {
    const url = new URL(event.request.url);
    if (holdNext && event.request.method === 'GET' && url.pathname === historyPath
      && url.searchParams.has('before') === (holdNext === 'older')) {
      held.push({ networkId: event.networkId ?? '', requestId: event.requestId });
      holdNext = null;
      return;
    }
    void cdp.send('Fetch.continueRequest', { requestId: event.requestId })
      .catch((error: unknown) => interceptionErrors.push(String(error)));
  });
  await cdp.send('Network.enable');
  await cdp.send('Fetch.enable', { patterns: [{ urlPattern: `*${historyPath}*`, requestStage: 'Request' }] });
  const sourceLatest = page.getByTestId('user-message').filter({ hasText: prompts[64] });
  const destination = page.getByTestId('user-message').filter({ hasText: destinationText });
  const rail = page.getByTestId('sessions-table');
  try {
    await openSessionView(page, source);
    await expect(sourceLatest).toBeVisible();
    for (const mode of ['older', 'latest'] as const) {
      const count = held.length;
      holdNext = mode;
      if (mode === 'older') {
        await page.getByTestId('session-conversation').focus();
        await page.keyboard.press('Home');
      } else {
        await page.evaluate(() => window.dispatchEvent(new Event('astrabox:manual-refresh')));
      }
      await expect.poll(() => held.length).toBe(count + 1);
      const paused = held[count];
      expect(paused.networkId, 'the held Fetch request must have an independently observable Network id').not.toBe('');
      await rail.locator(`a[href$="/sessions/${ids[1]}"]`).click();
      await expect(destination).toBeVisible();
      await expect(page.getByTestId('user-message').filter({ hasText: `History ${source}` })).toHaveCount(0);
      // No failRequest, abort or continueRequest is sent for this held id.
      // Only the application's cleanup can cancel it before it is released.
      await expect.poll(() => failures.get(paused.networkId)?.canceled).toBe(true);
      expect(failures.get(paused.networkId)?.errorText).toMatch(/ERR_ABORTED/);
      expect(responses.has(paused.networkId)).toBe(false);
      expect(finished.has(paused.networkId)).toBe(false);
      await expect(page.getByText(/AbortError|NETWORK_ERROR|Failed to fetch/)).toHaveCount(0);
      await rail.locator(`a[href$="/sessions/${source}"]`).click();
      await expect(sourceLatest).toBeVisible();
      await expect(destination).toHaveCount(0);
    }
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(sourceLatest).toBeVisible();
    const nextPage = page.waitForResponse((reply) => {
      const url = new URL(reply.url());
      return url.pathname === historyPath && url.searchParams.has('before') && reply.status() === 200;
    });
    await page.getByTestId('session-conversation').focus();
    await page.keyboard.press('Home');
    const reloadedPage = (await (await nextPage).json()).data;
    expect(reloadedPage.messages.map((message: { id: string }) => message.id))
      .toEqual(older.messages.map((message) => message.id));
    await expect(page.getByTestId('user-message').filter({ hasText: prompts[40] })).toBeInViewport();
    await expect(page.getByText(/AbortError|NETWORK_ERROR|Failed to fetch/)).toHaveCount(0);
    expect(interceptionErrors).toEqual([]);
    for (const paused of held) {
      expect(responses.has(paused.networkId)).toBe(false);
      expect(finished.has(paused.networkId)).toBe(false);
    }
  } finally {
    await cdp.send('Fetch.disable').catch(() => undefined);
    await cdp.detach();
  }
});
