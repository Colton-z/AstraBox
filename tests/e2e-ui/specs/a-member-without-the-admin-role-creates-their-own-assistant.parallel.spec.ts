/**
 * E2E: a signed-in user without the admin role creates their own Assistant
 * from the console.
 *
 * The create page offers the RuntimeProfiles that user may run an Assistant
 * on. Its choices must come from a surface open to every signed-in user, not
 * the administrator's RuntimeProfile catalogue, which refuses that user with
 * `ADMIN_ROLE_REQUIRED` and left the page on an error card with no form.
 *
 * Expected choices are read independently, as the administrator: every enabled
 * RuntimeProfile whose installed program declares `assistant_chat`. The page
 * must offer exactly those, the member's Create must land on the new record,
 * and the stored row must name the member as its owner. The administrator's
 * catalogue and its writes stay refused to the member.
 *
 * No workspace is started and no token is spent, so this runs in the parallel
 * lane. Deployment fixtures it reads and never creates: the configured
 * Assistant RuntimeProfile (`api.assistantEnvironmentName()`), the server
 * container (to sign the member's browser session with the deployed signer)
 * and the Postgres container.
 */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';

import { expect, test, type BrowserContext } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, sqlLiteral } from '../fixtures/dbOracle';
import { apiPath, appPath, appPrefix } from '../fixtures/env';
import { onPassOnly } from '../fixtures/sessionCleanup';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

let member: BrowserContext | undefined;
let assistantId = '';
onPassOnly(async () => {
  if (member && assistantId) await new AstraApi(member.request).deleteAssistant(assistantId);
});
test.afterEach(async () => {
  await member?.close();
  member = undefined;
});

test('a member without the admin role picks from their own RuntimeProfile choices and creates an Assistant they own', async ({
  browser,
  request,
}) => {
  const admin = new AstraApi(request);
  const configured = await admin.assistantEnvironmentName();
  const catalogue = await admin.data<Array<Record<string, unknown>>>('GET', '/admin/runtime-profiles');
  const expected = catalogue
    .filter((profile) => profile.enabled !== false
      && profile.engine_available !== false
      && ((profile.supported_session_kinds as string[] | undefined) ?? []).includes('assistant_chat'))
    .map((profile) => String(profile.name))
    .sort();
  expect(expected, 'the configured Assistant RuntimeProfile is one the administrator lists').toContain(configured);

  // The deployed signer issues the member's session, as the account-menu and
  // Agent-manager journeys do; signing in itself is not under test here.
  const memberId = `e2e-assistant-member-${randomUUID()}`;
  const token = execFileSync('docker', ['exec', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-c',
    'import os; from astrabox.providers.identity_oidc import mint_session_token; '
    + `print(mint_session_token(user_id=${JSON.stringify(memberId)}, email=None, display_name=None, roles=[], `
    + 'casdoor_organization=os.environ.get("ASTRABOX_CASDOOR_ORGANIZATION", "")))',
  ], { encoding: 'utf8', timeout: 15_000 }).trim();
  const origin = String(process.env.ASTRABOX_E2E_CONSOLE_URL).replace(/\/$/, '');
  member = await browser.newContext({ baseURL: origin, locale: 'en-US', storageState: {
    cookies: [{ name: 'astrabox_session', value: token, domain: new URL(origin).hostname,
      path: appPrefix || '/', expires: -1, httpOnly: true, secure: origin.startsWith('https:'), sameSite: 'Lax' }],
    origins: [],
  } });
  const memberApi = new AstraApi(member.request);
  expect(await memberApi.data('GET', '/user/current')).toMatchObject({ user_id: memberId, is_admin: false });

  // The administrator's catalogue and its writes remain refused.
  const listing = await member.request.get(apiPath('/admin/runtime-profiles'));
  expect(listing.status(), await listing.text()).toBe(403);
  expect((await listing.json()).code).toBe('ADMIN_ROLE_REQUIRED');
  const write = await member.request.put(apiPath(`/admin/runtime-profiles/__e2e_refused_${randomUUID()}`), {
    data: { engine_kind: 'assistant' },
  });
  expect(write.status(), await write.text()).toBe(403);
  expect((await write.json()).code).toBe('ADMIN_ROLE_REQUIRED');

  const page = await member.newPage();
  await page.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
  await page.goto(appPath('/manage/assistants/new'));
  const nameField = page.getByRole('textbox', { name: 'Name', exact: true });
  await expect(nameField, 'the create form renders for a member').toBeVisible();
  const runtimeProfile = page.getByRole('combobox', { name: 'Runtime environment', exact: true });
  const offered = (await runtimeProfile.locator('option').evaluateAll(
    (options) => options.map((option) => (option as HTMLOptionElement).value),
  )).filter(Boolean).sort();
  expect(offered, 'the form offers exactly the Assistant-capable RuntimeProfiles').toEqual(expected);

  const name = `__e2e_member_assistant_${randomUUID()}`;
  await nameField.fill(name);
  await runtimeProfile.selectOption(configured);
  const created = page.waitForResponse((response) =>
    response.request().method() === 'POST' && new URL(response.url()).pathname === apiPath('/assistants'));
  await page.getByRole('button', { name: 'Create', exact: true }).click();
  const createResponse = await created;
  expect(createResponse.status(), (await createResponse.text()).slice(0, 300)).toBe(200);
  await expect(page).toHaveURL(/\/manage\/assistants\/asst_[0-9a-f]+$/);
  assistantId = new URL(page.url()).pathname.split('/').pop() ?? '';
  test.info().annotations.push({ type: 'e2e_assistant_id', description: assistantId });
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();

  const [row] = postgresRows<{ owner_id: string; display_name: string; runtime_profile_name: string }>(
    `SELECT owner_id, display_name, runtime_profile_name FROM assistants WHERE assistant_id = ${sqlLiteral(assistantId)}`,
  );
  expect(row, 'the Assistant is stored').toBeTruthy();
  expect(row).toEqual({ owner_id: memberId, display_name: name, runtime_profile_name: configured });
});
