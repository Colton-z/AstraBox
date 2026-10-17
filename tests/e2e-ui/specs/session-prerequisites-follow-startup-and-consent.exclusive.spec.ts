/** Provider readiness follows real startup and tab return without repeating consent. */
import { execFileSync } from 'node:child_process';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { returnToTab } from '../fixtures/tabReturn';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => { if (agentId) await new AstraApi(request).deleteAgent(agentId); });

test('prerequisites refresh after startup, consent return and a stale action without creating grants', async ({ page, request }) => {
  test.info().annotations.push({ type: 'deployment_shape', description: 'runtime-preparation provider' });
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const control = (command: string, id: string) => execFileSync('docker', [
    'exec', server, 'python', '-c',
    `import json,sys
from pathlib import Path
command, key = sys.argv[1:]
hold = Path('/data/runtime-preparation-ui-startup-' + key)
if command == 'arm': hold.write_text('')
elif command == 'entered': print(hold.read_text() if hold.exists() else '')
elif command == 'release': hold.unlink(missing_ok=True)
elif command == 'receipts':
    p=Path('/data/runtime-preparation-user-action-calls.jsonl')
    print(json.dumps([json.loads(line) for line in p.read_text().splitlines() if json.loads(line)['session_id']==key] if p.exists() else []))
else:
    p=Path('/data/runtime-preparation-user-actions.json')
    ids=json.loads(p.read_text()) if p.exists() else []
    ids=[sid for sid in ids if sid!=key]
    p.write_text(json.dumps(ids+[key] if command=='block' else ids))
`, command, id,
  ], { encoding: 'utf8', timeout: 15_000 }).trim();
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(`__e2e_prerequisite_${Date.now()}`);
  agentId = agent.agent_id;
  control('arm', agentId);
  const { session_id: sid } = await api.startConversation(agentId);
  sessions.push(sid);
  await expect.poll(() => control('entered', agentId), { timeout: 60_000 }).toBe(sid);
  const actionPath = apiPath(`/sessions/${sid}/user-action`);
  const pending = () => api.data<{ ready: boolean; actions: Array<{ id: string }> }>('GET', `/sessions/${sid}/user-action`);
  expect((await api.getSession(sid)).state).toBe('CREATING');
  expect((await pending()).ready).toBe(false);
  await page.addInitScript(() => window.localStorage.setItem('astrabox-lang', 'en'));
  let actionPosts = 0;
  page.on('request', (outbound) => {
    if (outbound.method() === 'POST' && new URL(outbound.url()).pathname === actionPath) actionPosts += 1;
  });
  await openSessionView(page, sid);
  const composer = page.getByTestId('composer-prompt');
  await expect(page.getByText('Authorize this Agent to continue', { exact: true })).toBeVisible();
  await expect(composer).toBeDisabled();
  control('release', agentId);
  await api.waitForSessionReady(sid);
  await expect(composer).toBeEnabled();
  expect(actionPosts).toBe(0);
  // Real delivery proves that readiness is usable, rather than just a button state.
  await sendPrompt(page, sid, 'Reply with STARTUP_READY and do not use tools.');
  await api.waitForSession(sid, (scene) => scene.last_response?.outcome === 'completed');
  await expect(page.getByTestId('assistant-message').filter({ hasText: 'STARTUP_READY' })).toBeVisible();

  control('block', sid);
  await returnToTab(page, Date.now());
  await expect(composer).toBeDisabled();
  const consentLink = page.getByRole('link', { name: 'Open required action', exact: true });
  await expect(consentLink).toHaveAttribute('href', 'https://example.com/authorize');
  await page.context().route('https://example.com/authorize', (route) => route.fulfill({
    contentType: 'text/html', body: '<h1>Consent fixture</h1>',
  }));
  try {
    const opened = page.waitForEvent('popup');
    await consentLink.click();
    const consent = await opened;
    await expect(consent.getByRole('heading', { name: 'Consent fixture' })).toBeVisible();
    // External completion changes the provider's source state, never the UI response.
    control('clear', sid);
    await consent.close();
    await page.bringToFront();
    await returnToTab(page, Date.now());
    await expect(composer).toBeEnabled();
    expect((await pending()).ready).toBe(true);
    expect(actionPosts).toBe(0);
  } finally {
    await page.context().unroute('https://example.com/authorize');
  }

  control('block', sid);
  await returnToTab(page, Date.now());
  await expect(page.getByRole('button', { name: 'I have authorized', exact: true })).toBeVisible();
  control('clear', sid);
  const refusal = page.waitForResponse((reply) => reply.request().method() === 'POST'
    && new URL(reply.url()).pathname === actionPath);
  await page.getByRole('button', { name: 'I have authorized', exact: true }).click();
  expect((await refusal).status()).toBe(409);
  await expect(composer).toBeEnabled();
  await expect(page.getByRole('button', { name: 'I have authorized', exact: true })).toHaveCount(0);
  await expect(page.getByRole('alert').filter({ hasText: /INVALID_REQUEST|no longer available/ })).toHaveCount(0);
  expect(actionPosts, 'a stale command is read again, never replayed').toBe(1);
  expect(JSON.parse(control('receipts', sid)), 'no consent operation reached the provider').toEqual([]);

  const other = await api.startConversation(agentId);
  sessions.push(other.session_id);
  await api.waitForSessionReady(other.session_id);
  control('block', sid);
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  let captured!: () => void;
  const arrived = new Promise<void>((resolve) => { captured = resolve; });
  await page.route(`**${actionPath}`, async (route) => {
    const response = await route.fetch();
    captured();
    await held;
    await route.fulfill({ response });
  });
  try {
    await returnToTab(page, Date.now());
    await arrived;
    await page.getByTestId('sessions-table').locator(`a[href$="/sessions/${other.session_id}"]`).click();
    await expect(composer).toBeEnabled();
    const late = page.waitForResponse((reply) => reply.request().method() === 'GET'
      && new URL(reply.url()).pathname === actionPath);
    release();
    await (await late).finished();
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(page.getByText('Authorize this Agent to continue', { exact: true })).toHaveCount(0);
    await expect(composer).toBeEnabled();
    expect(JSON.parse(control('receipts', sid))).toEqual([]);
  } finally {
    release();
    await page.unroute(`**${actionPath}`);
  }
});
