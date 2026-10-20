/** User-action cards describe a current required operation, never an unknown read. */
import { execFileSync } from 'node:child_process';
import { expect, test, type APIResponse, type Route } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();

test('loading never impersonates a required operation across refresh, navigation and termination', async ({ page, request }) => {
  test.info().annotations.push({ type: 'deployment_shape', description: 'runtime-preparation provider' });
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const pair = await Promise.all([api.startConversation(agent.agent_id), api.startConversation(agent.agent_id)]);
  const [sid, other] = pair.map((item) => item.session_id);
  sessions.push(sid, other);
  await Promise.all(sessions.map((id) => api.waitForSessionReady(id)));
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const flag = (file: string, enabled: boolean, id = sid) => execFileSync('docker', ['exec', server, 'python', '-c',
    "from pathlib import Path; import json,sys; p=Path('/data/runtime-preparation-'+sys.argv[1]+'.json'); "
    + "ids=json.loads(p.read_text()) if p.exists() else []; ids=[x for x in ids if x!=sys.argv[2]]; "
    + "p.write_text(json.dumps(ids+([sys.argv[2]] if sys.argv[3]=='yes' else [])))", file, id, enabled ? 'yes' : 'no',
  ], { timeout: 15_000 });
  await page.addInitScript(() => {
    localStorage.setItem('astrabox-lang', 'en');
    const scene = window as unknown as { actionCards: string[]; watchActions: boolean };
    scene.actionCards = []; scene.watchActions = true;
    new MutationObserver(() => {
      const card = document.querySelector('[data-testid="session-user-action"]');
      if (scene.watchActions && card) scene.actionCards.push(card.textContent || '');
    }).observe(document, { childList: true, subtree: true, characterData: true });
  });
  const composer = page.getByTestId('composer-prompt');
  const card = page.getByTestId('session-user-action');
  const authorize = page.getByRole('button', { name: 'I have authorized', exact: true });
  let actionPosts = 0;
  page.on('request', (outbound) => {
    if (outbound.method() === 'POST' && /\/user-action$/.test(new URL(outbound.url()).pathname)) actionPosts += 1;
  });
  const holds: Array<() => Promise<void>> = [];
  const hold = async (id: string, method = 'GET') => {
    let release!: () => void; let captured!: (response: APIResponse) => void;
    const paused = new Promise<void>((resolve) => { release = resolve; });
    const response = new Promise<APIResponse>((resolve) => { captured = resolve; });
    const path = `**${apiPath(`/sessions/${id}/user-action`)}`;
    const handler = async (route: Route) => {
      if (route.request().method() !== method) return route.continue();
      const actual = await route.fetch(); captured(actual); await paused; await route.fulfill({ response: actual });
    };
    await page.route(path, handler);
    holds.push(async () => { release(); await page.unroute(path, handler); });
    return { response, release };
  };
  const watch = (enabled: boolean) => page.evaluate((enabled) => {
    const scene = window as unknown as { actionCards: string[]; watchActions: boolean };
    scene.actionCards = []; scene.watchActions = enabled;
  }, enabled);
  const noCard = async () => {
    await expect(card).toHaveCount(0);
    expect(await page.evaluate(() => (window as unknown as { actionCards: string[] }).actionCards)).toEqual([]);
  };
  try {
    for (const cold of [false, true]) {
      const initial = await hold(sid);
      if (cold) await page.reload({ waitUntil: 'domcontentloaded' });
      else await openSessionView(page, sid);
      expect((await (await initial.response).json()).data).toEqual({ applicable: true, ready: true, user_action: null, actions: [] });
      await expect(composer).toBeDisabled(); await noCard();
      initial.release(); await expect(composer).toBeEnabled(); await noCard();
      await holds.pop()!();
    }
    flag('user-actions', true);
    const blocked = await hold(sid);
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    const required = (await (await blocked.response).json()).data;
    expect(required.ready).toBe(false); expect(required.user_action.message).toBe('Authorize this Agent to continue');
    await expect(composer).toBeDisabled(); await noCard();
    await watch(false); blocked.release(); await expect(authorize).toBeEnabled(); await expect(card).toContainText(required.user_action.message);
    await holds.pop()!();

    const operation = await hold(sid, 'POST');
    await authorize.click(); expect((await operation.response).status()).toBe(200);
    await expect(card).toBeVisible(); await expect(authorize).toBeDisabled(); await expect(composer).toBeDisabled();
    operation.release(); await expect(composer).toBeEnabled(); await expect(card).toHaveCount(0); await holds.pop()!();
    expect(actionPosts).toBe(1);

    flag('user-actions', true);
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    await expect(authorize).toBeEnabled();
    flag('user-actions', false);
    const refreshed = await hold(sid); await watch(true);
    await page.getByRole('button', { name: 'Refresh status', exact: true }).click();
    expect((await (await refreshed.response).json()).data.ready).toBe(true);
    await expect(composer).toBeDisabled(); await noCard();
    refreshed.release(); await expect(composer).toBeEnabled(); await noCard(); await holds.pop()!();

    flag('user-action-read-failures', true); await watch(false);
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    await expect(page.getByRole('alert').filter({ hasText: 'Could not check session requirements.' })).toBeVisible();
    await expect(composer).toBeDisabled(); await expect(authorize).toHaveCount(0);
    expect(await page.textContent('body')).not.toContain('SECRET_SENTINEL');
    flag('user-action-read-failures', false);
    await page.getByRole('button', { name: 'Refresh status', exact: true }).click(); await expect(composer).toBeEnabled();

    flag('user-actions', true); const stale = await hold(sid); await watch(true);
    await page.evaluate(() => window.dispatchEvent(new Event('online'))); await stale.response; await noCard();
    await page.getByTestId('sessions-table').locator(`a[href$="/sessions/${other}"]`).click();
    await expect(composer).toBeEnabled();
    const late = page.waitForResponse((reply) => reply.request().method() === 'GET' && new URL(reply.url()).pathname === apiPath(`/sessions/${sid}/user-action`));
    stale.release(); await (await late).finished();
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(composer).toBeEnabled(); await noCard(); await holds.pop()!();

    flag('user-actions', true, other);
    const ending = await hold(other); await watch(true);
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    expect((await (await ending.response).json()).data.ready).toBe(false);
    await noCard();
    await api.data('POST', `/admin/sessions/${other}/kill`);
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    await expect(page.getByTestId('session-recover')).toBeEnabled();
    await expect(composer).toBeDisabled(); await noCard();
    const endedRead = page.waitForResponse((reply) => reply.request().method() === 'GET' && new URL(reply.url()).pathname === apiPath(`/sessions/${other}/user-action`));
    ending.release(); await (await endedRead).finished();
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(composer).toBeDisabled(); await noCard();
    expect(await api.data('GET', `/sessions/${other}/user-action`)).toEqual({ applicable: false, ready: false, user_action: null, actions: [] });
    expect(actionPosts).toBe(1);
    console.log(JSON.stringify({ phase: 'user-action-loading-accepted', sessions: pair.map((item) => item.session_id), actionPosts }));
  } finally {
    for (const close of holds.reverse()) await close();
    flag('user-actions', false); flag('user-actions', false, other); flag('user-action-read-failures', false);
  }
});
