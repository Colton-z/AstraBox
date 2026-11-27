/** An Agent's configuration history through its management page, by two managers and someone who is neither. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';

import { AstraApi, type AgentRecord } from '../fixtures/astraApi';
import { apiPath, appPath, appPrefix } from '../fixtures/env';
import { deployedApiClient, issueToken, json, requiredEnvironment } from '../fixtures/oidcMachine';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

type VersionRecord = {
  version: number;
  operation: 'create' | 'update' | 'restore' | 'baseline';
  restored_from_version?: number | null;
  author_user_id?: string | null;
  created_at: string;
  changed_fields?: string[] | null;
};
type VersionPage = { current_version: number; versions: VersionRecord[]; has_more: boolean };
type VersionDetail = VersionRecord & { definition: Record<string, unknown> };
type StoredAgent = AgentRecord & {
  version: number;
  description?: string | null;
  default_repo?: { url?: string } | null;
};

// Nothing is cloned: the Agent prepares no sandbox and no conversation starts.
const REPO_BEFORE = 'https://github.com/anthropics/skills.git';
const REPO_AFTER = 'https://github.com/anthropics/claude-plugins-official.git';
const DESCRIPTION_BEFORE = 'Reads the version history e2e repository.';
const DESCRIPTION_AFTER = 'Reads the version history e2e repository, edited.';
const DESCRIPTION_MEANWHILE = 'Changed through the API while a restore was open.';
/** How many versions the history reads at a time. */
const HISTORY_PAGE = 10;

const readVersions = (api: AstraApi, agentId: string) =>
  api.data<VersionPage>('GET', `/agents/${agentId}/versions?limit=100`);
const readVersion = (api: AstraApi, agentId: string, version: number) =>
  api.data<VersionDetail>('GET', `/agents/${agentId}/versions/${version}`);
const readAgent = (api: AstraApi, agentId: string) =>
  api.data<StoredAgent>('GET', `/agents/${agentId}`);
/** An authoring save through the API: it carries the schema's required fields with the change. */
async function saveAgent(api: AstraApi, agentId: string, changes: Record<string, unknown>): Promise<StoredAgent> {
  const current = await readAgent(api, agentId);
  return api.data<StoredAgent>('PUT', `/agents/${agentId}`, {
    name: current.name, model: current.model, runtime_profile_name: current.runtime_profile_name, ...changes,
  });
}

/** The card that holds `inner`, or the one headed `inner`. */
function card(page: Page, inner: Locator | string): Locator {
  const has = typeof inner === 'string'
    ? page.getByRole('heading', { name: inner, exact: true })
    : inner;
  return page.locator('[data-slot="card"]').filter({ has });
}

/** The history row of one version, found by its version cell. */
function versionRow(page: Page, history: Locator, version: number): Locator {
  return history
    .getByRole('row')
    .filter({ has: page.getByRole('cell', { name: String(version), exact: true }) });
}

/** One change in a list of changes, found by the name the edit form gives its field. */
function change(list: Locator, field: string): Locator {
  return list.getByRole('listitem').filter({ hasText: field });
}

function restoreResponse(page: Page, agentId: string, version: number) {
  return page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === apiPath(`/agents/${agentId}/versions/${version}/restore`)
  ));
}

test('an Agent keeps each edit as a version its managers compare and restore, and nobody else reads', async ({
  playwright, page, request,
}) => {
  // Two real principals: the suite's signed-in administrator, who creates the
  // Agent, and the deployment's API client, who manages it only once named.
  const ownerApi = new AstraApi(request);
  const consoleUrl = requiredEnvironment('ASTRABOX_E2E_CONSOLE_URL');
  const issuer = await playwright.request.newContext({ storageState: { cookies: [], origins: [] } });
  let memberClient: APIRequestContext | undefined;
  let memberToken = '';
  let agentId = '';
  let passed = false;
  try {
    const discovery = await json(await issuer.get(
      `${requiredEnvironment('ASTRABOX_E2E_OIDC_ISSUER')}/.well-known/openid-configuration`,
    ));
    const { clientId, clientSecret } = deployedApiClient(requireServiceContainer(SERVER_CONTAINER_HANDLE));
    memberToken = (await issueToken(
      issuer, String(discovery.token_endpoint), clientId, clientSecret, 'astrabox:read astrabox:write',
    )).value;
    memberClient = await playwright.request.newContext({
      baseURL: consoleUrl,
      storageState: { cookies: [], origins: [] },
      extraHTTPHeaders: { Authorization: `Bearer ${memberToken}` },
    });
    const memberApi = new AstraApi(memberClient);
    const owner = await ownerApi.data<{ user_id: string; is_admin: boolean }>('GET', '/user/current');
    const member = await memberApi.data<{ user_id: string; is_admin: boolean }>('GET', '/user/current');
    expect([owner.is_admin, member.is_admin]).toEqual([true, false]);
    expect(member.user_id).toBeTruthy();
    expect(member.user_id).not.toBe(owner.user_id);

    const base = await ownerApi.defaultAgent();
    const profile = process.env.ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT;
    expect(profile, 'the configured cold RuntimeProfile is required').toBeTruthy();
    const created = await ownerApi.createAgent({
      name: `__e2e_versions_${randomUUID()}`,
      model: base.model,
      runtime_profile_name: profile,
      prepared_capacity: 0,
      description: DESCRIPTION_BEFORE,
      default_repo: { url: REPO_BEFORE, protocol: 'https', branch: 'main', depth: 1 },
    });
    agentId = created.agent_id;
    await ownerApi.setAgentAccess(agentId, { visibility: 'public', admins: [], allowed_user_ids: [] });
    const beforeManaging = (await readVersions(ownerApi, agentId)).current_version;

    await test.step('someone who may not manage the Agent reads no history and restores nothing', async () => {
      for (const [method, route] of [
        ['GET', `/agents/${agentId}/versions`],
        ['GET', `/agents/${agentId}/versions/1`],
        ['GET', `/agents/${agentId}/versions/${beforeManaging}/changes`],
        ['POST', `/agents/${agentId}/versions/1/restore`],
      ] as const) {
        const response = await memberClient!.fetch(apiPath(route), {
          method, data: method === 'POST' ? { version: beforeManaging } : undefined,
        });
        const body = await response.text();
        expect(response.status(), `${method} ${route}: ${body.slice(0, 300)}`).toBe(403);
        expect(body, 'a refusal carries no stored configuration').not.toContain(REPO_BEFORE);
      }
      expect((await readVersions(ownerApi, agentId)).current_version).toBe(beforeManaging);

      // The console signs a browser in with a session cookie, not a bearer
      // header, so the member's browser carries a session the deployed signer
      // issues for the same principal; signing in itself is not under test.
      const session = execFileSync('docker', ['exec', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-c',
        'import os; from astrabox.providers.identity_oidc import mint_session_token; '
        + `print(mint_session_token(user_id=${JSON.stringify(member.user_id)}, email=None, display_name=None, roles=[], `
        + 'casdoor_organization=os.environ.get("ASTRABOX_CASDOOR_ORGANIZATION", "")))',
      ], { encoding: 'utf8', timeout: 15_000 }).trim();
      const visitor = await page.context().browser()!.newContext({
        baseURL: consoleUrl,
        storageState: {
          cookies: [{ name: 'astrabox_session', value: session, domain: new URL(consoleUrl).hostname,
            path: appPrefix || '/', expires: -1, httpOnly: true, secure: consoleUrl.startsWith('https:'), sameSite: 'Lax' }],
          origins: [],
        },
      });
      try {
        const theirs = await visitor.newPage();
        await theirs.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
        await theirs.goto(appPath(`/manage/agents/${encodeURIComponent(agentId)}`));
        // The Agent is public, so its page loads for them, without the history.
        await expect(theirs.getByRole('complementary').getByText('Version', { exact: true })).toBeVisible();
        await expect(card(theirs, 'Version history')).toHaveCount(0);
      } finally {
        await visitor.close();
      }
    });

    // Managed by its creator and one more person from here on.
    const access = { visibility: 'public' as const, admins: [member.user_id], allowed_user_ids: [] };
    await ownerApi.setAgentAccess(agentId, access);
    const manager = member;
    const managerApi = memberApi;
    const start = (await readVersions(ownerApi, agentId)).current_version;
    expect(start).toBe(beforeManaging + 1);
    const repoVersion = start + 1;
    const descriptionVersion = start + 2;
    const restoredVersion = start + 3;
    const meanwhileVersion = start + 4;
    const authoring = await ownerApi.data<{ fields: Array<{ key: string; path?: string }> }>(
      'GET', '/agent-configuration/schema',
    );
    const editableFields = [...new Set(authoring.fields.map((field) => (field.path || field.key).split('.')[0]))];

    await page.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
    await page.goto(appPath(`/manage/agents/${encodeURIComponent(agentId)}`));
    const history = card(page, 'Version history');
    await expect(history).toBeVisible();
    await expect(versionRow(page, history, 1)).toContainText('Created');

    await test.step('edits by two managers become two versions with their fields, author and time', async () => {
      const repoField = page.locator('#agent-default_repo');
      const repo = JSON.parse(await repoField.inputValue()) as Record<string, unknown>;
      expect(repo.url, 'the editor shows the repository the Agent was created with').toBe(REPO_BEFORE);
      await repoField.fill(JSON.stringify({ ...repo, url: REPO_AFTER }, null, 2));
      await repoField.blur();
      const saved = page.waitForResponse((response) => (
        response.request().method() === 'PUT'
        && new URL(response.url()).pathname === apiPath(`/agents/${agentId}`)
      ));
      await card(page, repoField).getByRole('button', { name: 'Save', exact: true }).click();
      expect((await saved).status(), 'saving the repository must succeed').toBe(200);
      // The history follows the save without a reload.
      await expect(versionRow(page, history, repoVersion)).toContainText('Project repository');

      const edited = await saveAgent(managerApi, agentId, { description: DESCRIPTION_AFTER, version: repoVersion });
      expect(edited.version).toBe(descriptionVersion);

      const recorded = await readVersions(ownerApi, agentId);
      expect(recorded.current_version).toBe(descriptionVersion);
      const [descriptionEdit, repoEdit] = recorded.versions;
      expect(repoEdit).toMatchObject({
        version: repoVersion, operation: 'update', changed_fields: ['default_repo'], author_user_id: owner.user_id,
      });
      expect(descriptionEdit).toMatchObject({
        version: descriptionVersion, operation: 'update', changed_fields: ['description'],
        author_user_id: manager.user_id,
      });
      expect(Date.parse(descriptionEdit.created_at)).toBeGreaterThanOrEqual(Date.parse(repoEdit.created_at));

      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page.getByRole('complementary').getByText('Version', { exact: true })
        .locator('xpath=following-sibling::div[1]')).toHaveText(String(descriptionVersion));
      for (const [edit, field, author] of [
        [repoEdit, 'Project repository', owner], [descriptionEdit, 'Description', manager],
      ] as const) {
        const row = versionRow(page, history, edit.version);
        await expect(row).toContainText('Edited');
        await expect(row).toContainText(field);
        await expect(row).toContainText(author.user_id);
        await expect(row.locator('time')).toHaveAttribute('datetime', edit.created_at);
      }
    });

    await test.step('a version shows the values it changed, against any other version', async () => {
      const [before, after] = await Promise.all([
        readVersion(ownerApi, agentId, start), readVersion(ownerApi, agentId, repoVersion),
      ]);
      expect((before.definition.default_repo as { url: string }).url).toBe(REPO_BEFORE);
      expect((after.definition.default_repo as { url: string }).url).toBe(REPO_AFTER);

      await versionRow(page, history, repoVersion).click();
      const repoChange = card(page, `Version ${repoVersion}`)
        .getByRole('list', { name: `Changes from version ${start}`, exact: true });
      await expect(repoChange.getByRole('listitem')).toHaveCount(1);
      const url = change(repoChange, 'Project repository');
      await expect(url).toContainText('url');
      await expect(url.getByRole('deletion')).toHaveText(REPO_BEFORE);
      await expect(url.getByRole('insertion')).toHaveText(REPO_AFTER);

      await versionRow(page, history, descriptionVersion).click();
      const latest = card(page, `Version ${descriptionVersion}`);
      await latest.getByLabel('Compare with', { exact: true }).selectOption({ label: `Version ${start}` });
      const both = latest.getByRole('list', { name: `Changes from version ${start}`, exact: true });
      await expect(both.getByRole('listitem')).toHaveCount(2);
      await expect(change(both, 'Description').getByRole('deletion')).toHaveText(DESCRIPTION_BEFORE);
      await expect(change(both, 'Description').getByRole('insertion')).toHaveText(DESCRIPTION_AFTER);
      await expect(change(both, 'Project repository').getByRole('deletion')).toHaveText(REPO_BEFORE);
      await expect(change(both, 'Project repository').getByRole('insertion')).toHaveText(REPO_AFTER);
    });

    await test.step('restoring the first version writes its configuration back and leaves access alone', async () => {
      // Version 1 predates the access settings above, so it differs from the
      // current version in fields a restore writes and in fields it does not.
      await versionRow(page, history, 1).click();
      await card(page, 'Version 1').getByRole('button', { name: 'Restore this version', exact: true }).click();
      const dialog = page.getByRole('alertdialog');
      const restoring = dialog.getByRole('list', { name: 'Will be restored', exact: true });
      await expect(restoring.getByRole('listitem')).toHaveCount(2);
      await expect(change(restoring, 'Description').getByRole('deletion')).toHaveText(DESCRIPTION_AFTER);
      await expect(change(restoring, 'Description').getByRole('insertion')).toHaveText(DESCRIPTION_BEFORE);
      await expect(change(restoring, 'Project repository').getByRole('deletion')).toHaveText(REPO_AFTER);
      await expect(change(restoring, 'Project repository').getByRole('insertion')).toHaveText(REPO_BEFORE);
      const kept = dialog.getByRole('list', { name: 'Not restored', exact: true });
      await expect(kept.getByRole('listitem').first()).toBeVisible();
      await expect(change(restoring, 'Visibility')).toHaveCount(0);

      const restored = restoreResponse(page, agentId, 1);
      await dialog.getByRole('button', { name: 'Restore version 1', exact: true }).click();
      expect((await restored).status(), 'the restore must succeed').toBe(200);
      await expect(dialog).toBeHidden();

      const recorded = await readVersions(ownerApi, agentId);
      expect(recorded.current_version).toBe(restoredVersion);
      expect(recorded.versions[0]).toMatchObject({
        version: restoredVersion, operation: 'restore', restored_from_version: 1,
        changed_fields: ['default_repo', 'description'], author_user_id: owner.user_id,
      });
      const [target, written, agent] = await Promise.all([
        readVersion(ownerApi, agentId, 1),
        readVersion(ownerApi, agentId, restoredVersion),
        readAgent(ownerApi, agentId),
      ]);
      for (const field of editableFields) {
        expect(written.definition[field] ?? null, `the restore stores ${field} as version 1 had it`)
          .toEqual(target.definition[field] ?? null);
        expect((agent as Record<string, unknown>)[field] ?? null, `the Agent reads ${field} as version 1 had it`)
          .toEqual(target.definition[field] ?? null);
      }
      expect(agent.description).toBe(DESCRIPTION_BEFORE);
      expect(agent.default_repo?.url).toBe(REPO_BEFORE);
      expect(await ownerApi.getAgentAccess(agentId), 'a restore leaves the access settings as they are')
        .toMatchObject(access);

      await expect(versionRow(page, history, restoredVersion)).toContainText('Restored from version 1');
      // The cards show the restored configuration without a reload, and hold no
      // edit of their own: a draft left from before the restore would read as one.
      const description = page.getByRole('textbox', { name: 'Description', exact: true });
      await expect(description).toHaveValue(DESCRIPTION_BEFORE);
      await expect.poll(async () => (
        JSON.parse(await page.locator('#agent-default_repo').inputValue()) as { url?: string }
      ).url).toBe(REPO_BEFORE);
      await expect(card(page, description).getByRole('button', { name: 'Save', exact: true })).toHaveCount(0);
      await expect(card(page, page.locator('#agent-default_repo')).getByRole('button', { name: 'Save', exact: true }))
        .toHaveCount(0);
    });

    await test.step('a restore confirmed after another manager changed the Agent writes nothing', async () => {
      await versionRow(page, history, repoVersion).click();
      await card(page, `Version ${repoVersion}`)
        .getByRole('button', { name: 'Restore this version', exact: true }).click();
      const dialog = page.getByRole('alertdialog');
      await expect(dialog.getByRole('list', { name: 'Will be restored', exact: true })).toBeVisible();

      const meanwhile = await saveAgent(
        managerApi, agentId, { description: DESCRIPTION_MEANWHILE, version: restoredVersion },
      );
      expect(meanwhile.version).toBe(meanwhileVersion);

      const refused = restoreResponse(page, agentId, repoVersion);
      await dialog.getByRole('button', { name: `Restore version ${repoVersion}`, exact: true }).click();
      expect((await refused).status(), 'the server refuses a restore compared against an old version').toBe(409);
      await expect(dialog.getByRole('alert').filter({ hasText: 'was changed while you were comparing' }))
        .toBeVisible();
      // The comparison is redone against the version the Agent has now.
      await expect(dialog).toContainText(`the current version, ${meanwhileVersion}`);

      const recorded = await readVersions(ownerApi, agentId);
      expect(recorded.current_version).toBe(meanwhileVersion);
      expect(recorded.versions).toHaveLength(meanwhileVersion);
      const agent = await readAgent(ownerApi, agentId);
      expect(agent.version).toBe(meanwhileVersion);
      expect(agent.description).toBe(DESCRIPTION_MEANWHILE);
      expect(agent.default_repo?.url).toBe(REPO_BEFORE);

      await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
      await expect(dialog).toBeHidden();
    });

    await test.step('a save that changes nothing adds no version', async () => {
      const unchanged = await saveAgent(
        ownerApi, agentId, { description: DESCRIPTION_MEANWHILE, version: meanwhileVersion },
      );
      expect(unchanged.version).toBe(meanwhileVersion);
      const after = await readVersions(ownerApi, agentId);
      expect(after.current_version).toBe(meanwhileVersion);
      expect(after.versions).toHaveLength(meanwhileVersion);
    });

    await test.step('older versions load when the reader scrolls to them', async () => {
      let version = meanwhileVersion;
      for (let edit = 1; version <= HISTORY_PAGE; edit += 1) {
        version = (await saveAgent(
          ownerApi, agentId, { description: `${DESCRIPTION_MEANWHILE} Edit ${edit}.`, version },
        )).version;
      }
      const recorded = await readVersions(ownerApi, agentId);
      expect(recorded.versions).toHaveLength(version);
      expect(version, 'two pages hold every version').toBeLessThanOrEqual(2 * HISTORY_PAGE);

      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(versionRow(page, history, version)).toBeVisible();
      await expect(versionRow(page, history, 1)).toHaveCount(0);
      await history.locator('[data-slot="console-load-more"]').scrollIntoViewIfNeeded();
      await expect(versionRow(page, history, 1)).toContainText('Created');
      for (const kept of recorded.versions) {
        await expect(versionRow(page, history, kept.version)).toHaveCount(1);
      }
      await expect(history.locator('[data-slot="console-load-more"]')).toHaveCount(0);
    });
    passed = true;
  } finally {
    // A failed run keeps the Agent and its history to read.
    if (passed && agentId) await ownerApi.deleteAgent(agentId);
    await memberClient?.dispose();
    await issuer.dispose();
  }
});
