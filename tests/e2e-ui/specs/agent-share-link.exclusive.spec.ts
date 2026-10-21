import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { expect, test } from '@playwright/test';

import { AstraApi, type SessionRecord } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { submitIssuerSignIn } from '../fixtures/oidcLogin';
import { SERVER_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('an Agent link survives sign-in and starts the selected Agent for an authorized recipient', async ({ page, context, browser, request }, testInfo) => {
  const api = new AstraApi(request);
  const name = `Shared Agent ${randomUUID()}`;
  const description = 'Reviews release plans.\nKeeps the operational risks visible.';
  const agent = await api.createColdTestAgent(name);
  agentId = agent.agent_id;
  await api.updateAgent(agentId, { ...await api.getAgent(agentId), description, display_meta: { tags: ['release-review'] } });
  await api.setAgentAccess(agentId, { visibility: 'public', admins: [], allowed_user_ids: [] });
  const route = appPath(`/agents/${agentId}`);
  await context.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.goto(appPath('/agents'));
  await page.getByRole('tab', { name: 'All', exact: true }).click();
  await page.getByTestId('agent-option').filter({ hasText: name }).getByRole('link', { name, exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${route}$`));
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
  await expect(page.getByText(description, { exact: true })).toBeVisible();
  await expect(page.getByLabel('Tags')).toContainText('#release-review');
  await testInfo.attach('agent-detail', { body: await page.screenshot(), contentType: 'image/png' });
  const before = await api.data<SessionRecord[]>('GET', '/sessions');
  await page.getByRole('button', { name: 'Copy link', exact: true }).click();
  await expect(page.getByText('Agent link copied', { exact: true })).toBeVisible();
  const link = await page.evaluate(() => navigator.clipboard.readText());
  expect(link).toBe(new URL(route, page.url()).href);
  expect((await api.data<SessionRecord[]>('GET', '/sessions')).filter((s) => s.agent_id === agentId).map((s) => s.session_id).sort()).toEqual(before.filter((s) => s.agent_id === agentId).map((s) => s.session_id).sort());

  const signedOut = await browser.newContext({ baseURL: new URL(link).origin,
    storageState: { cookies: [], origins: [] } });
  try {
    await signedOut.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
    const landing = await signedOut.newPage();
    await landing.goto(link);
    await expect(landing.getByRole('heading', { name: 'Sign in to AstraBox', exact: true })).toBeVisible();
    expect(new URL(landing.url()).searchParams.get('next')).toBe(`/agents/${agentId}`);
    await landing.getByRole('button', { name: 'Continue with your organization account', exact: true }).click();
    const issuer = String(process.env.ASTRABOX_E2E_OIDC_ISSUER).replace(/\/$/, '');
    await landing.waitForURL((url) => url.origin === issuer);
    await submitIssuerSignIn(landing);
    await expect(landing).toHaveURL(link);
    await expect(landing.getByRole('heading', { name, exact: true })).toBeVisible();
  } finally {
    await signedOut.close();
  }

  const recipient = `__e2e_agent_share_${randomUUID()}`;
  const token = execFileSync('docker', ['exec', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-c',
    'import os; from astrabox.providers.identity_oidc import mint_session_token; '
    + `print(mint_session_token(user_id=${JSON.stringify(recipient)}, email=None, display_name=None, roles=[], `
    + 'casdoor_organization=os.environ.get("ASTRABOX_CASDOOR_ORGANIZATION", "")))',
  ], { encoding: 'utf8' }).trim();
  const origin = new URL(link).origin;
  const recipientContext = await browser.newContext({ baseURL: origin, storageState: {
    cookies: [{ name: 'astrabox_session', value: token, domain: new URL(origin).hostname,
      path: '/', expires: -1, httpOnly: true, secure: origin.startsWith('https:'), sameSite: 'Lax' }], origins: [],
  } });
  try {
    await recipientContext.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
    const recipientApi = new AstraApi(recipientContext.request);
    expect(await recipientApi.data('GET', '/user/current')).toMatchObject({ user_id: recipient, is_admin: false });
    const landing = await recipientContext.newPage();
    await landing.goto(link);
    await expect(landing.getByRole('heading', { name, exact: true })).toBeVisible();
    await landing.reload();
    await expect(landing.getByRole('heading', { name, exact: true })).toBeVisible();
    expect(await recipientApi.data<SessionRecord[]>('GET', '/sessions')).toHaveLength(0);
    const creation = landing.waitForResponse((response) => response.request().method() === 'POST'
      && new URL(response.url()).pathname === apiPath(`/agents/${agentId}/conversations`));
    await landing.getByRole('button', { name: 'Start conversation', exact: true }).click();
    const response = await creation;
    expect(response.status()).toBe(200);
    const sid = String((await response.json()).data.session_id);
    sessions.push(sid);
    await expect(landing).toHaveURL(new RegExp(`/sessions/${sid}$`));
    expect(await recipientApi.getSession(sid)).toMatchObject({ agent_id: agentId, user_id: recipient });
    await expect(landing.getByTestId('composer-prompt')).toBeEnabled({ timeout: 90_000 });

    await api.setAgentAccess(agentId, { visibility: 'private', admins: [], allowed_user_ids: [] });
    await landing.goto(link);
    await expect(landing.getByText('This Agent is unavailable', { exact: true })).toBeVisible();
    await expect(landing.getByRole('heading', { name, exact: true })).toHaveCount(0);
    await expect(landing.getByRole('button', { name: 'Start conversation', exact: true })).toHaveCount(0);
    await api.setAgentAccess(agentId, { visibility: 'public', admins: [], allowed_user_ids: [] });
    await api.updateAgent(agentId, { ...await api.getAgent(agentId), enabled: false });
    await landing.reload();
    await expect(landing.getByText('This Agent is unavailable', { exact: true })).toBeVisible();
    await expect(landing.getByRole('heading', { name, exact: true })).toHaveCount(0);
    await expect(landing.getByRole('button', { name: 'Start conversation', exact: true })).toHaveCount(0);
    expect((await recipientApi.data<SessionRecord[]>('GET', '/sessions')).map((s) => s.session_id)).toEqual([sid]);
    await testInfo.attach('agent-share-outcome', { body: JSON.stringify({ agent_id: agentId, session_id: sid,
      recipient, link, login_returned: true, disabled_hidden: true, private_hidden: true }), contentType: 'application/json' });
  } finally {
    await recipientContext.close();
  }
});
