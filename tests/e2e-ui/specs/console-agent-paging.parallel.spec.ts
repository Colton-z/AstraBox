/** Real Agent pages, counts and loaded-page refresh; no response fixtures. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, type AgentRecord } from '../fixtures/astraApi';
import { postgresTextRows } from '../fixtures/dbOracle';
import { apiPath, appPath } from '../fixtures/env';
import { deployedApiClient, issueToken, json, requiredEnvironment } from '../fixtures/oidcMachine';
import { onPassOnly } from '../fixtures/sessionCleanup';
import {
  POSTGRES_CONTAINER_HANDLE, SERVER_CONTAINER_HANDLE, requireServiceContainer,
} from '../fixtures/serviceContainer';

const ROWS = 250;
const PAGE_SIZE = 50;
const createdIds: string[] = [];

async function batches<T>(items: T[], operation: (item: T) => Promise<unknown>): Promise<void> {
  for (let offset = 0; offset < items.length; offset += 10) {
    await Promise.all(items.slice(offset, offset + 10).map(operation));
  }
}

type NativeAgent = {
  agent_id: string; name: string; deleted?: boolean; enabled?: boolean;
  created_by?: string; visibility?: string; admins?: string[]; allowed_user_ids?: string[];
};

function nativeAgents(): NativeAgent[] {
  return postgresTextRows(
    requireServiceContainer(POSTGRES_CONTAINER_HANDLE),
    process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox',
    `SELECT json_build_object(
      'agent_id', doc->'agent_id', 'name', doc->'name', 'deleted', doc->'deleted',
      'enabled', doc->'enabled', 'created_by', doc->'created_by',
      'visibility', doc->'visibility', 'admins', doc->'admins',
      'allowed_user_ids', doc->'allowed_user_ids')::text
      FROM astrabox_documents WHERE collection = 'agents'`,
  ).map((line) => JSON.parse(line) as NativeAgent).filter((row) => row.deleted !== true);
}

function ordered(rows: NativeAgent[]): NativeAgent[] {
  return [...rows].sort((left, right) => {
    for (const field of ['name', 'agent_id'] as const) {
      const a = field === 'name' ? left[field].toLowerCase() : left[field];
      const b = field === 'name' ? right[field].toLowerCase() : right[field];
      if (a !== b) return a < b ? -1 : 1;
    }
    return 0;
  });
}

onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  await batches(createdIds, (id) => api.deleteAgent(id));
});
test.use({ locale: 'en-US' });

test('250 real Agents retain complete cursor pages, access counts and loaded rows on refresh', async ({
  page, request, playwright,
}) => {
  const api = new AstraApi(request);
  const base = await api.defaultAgent();
  const environment = String(base.environment_name || '');
  const models = await api.listEnvironmentModels(environment);
  const model = models.find((item) => item && !item.includes('*'));
  expect(model).toBeTruthy();
  const prefix = `zz_e2e_scale_${randomUUID().replaceAll('-', '').slice(0, 12)}_`;
  const config = (index: number) => ({
    name: `${prefix}${String(index).padStart(3, '0')}`, model,
    environment_name: environment, prewarm_enabled: false,
    enabled: index % 5 !== 4,
    visibility: index === 0 || index % 7 === 3 ? 'private' : 'public',
  });
  const machine = await playwright.request.newContext({
    baseURL: requiredEnvironment('ASTRABOX_E2E_CONSOLE_URL'),
    storageState: { cookies: [], origins: [] },
  });
  try {
    const issuer = requiredEnvironment('ASTRABOX_E2E_OIDC_ISSUER');
    const discoveryResponse = await machine.get(`${issuer}/.well-known/openid-configuration`);
    expect(discoveryResponse.ok()).toBe(true);
    const discovery = await json(discoveryResponse);
    const credentials = deployedApiClient(requireServiceContainer(SERVER_CONTAINER_HANDLE));
    const token = await issueToken(
      machine, String(discovery.token_endpoint), credentials.clientId, credentials.clientSecret,
      'astrabox:read astrabox:write',
    );
    const machineApiContext = await playwright.request.newContext({
      baseURL: requiredEnvironment('ASTRABOX_E2E_CONSOLE_URL'),
      extraHTTPHeaders: { Authorization: `Bearer ${token.value}` },
      storageState: { cookies: [], origins: [] },
    });
    try {
      const machineApi = new AstraApi(machineApiContext);
      const own = await machineApi.createAgent({ ...config(ROWS - 1), visibility: 'private' });
      createdIds.push(own.agent_id);
      await batches(Array.from({ length: ROWS - 1 }, (_, index) => index), async (index) => {
        const row = await api.createAgent(config(index));
        createdIds.push(row.agent_id);
      });
      let native = nativeAgents();
      const ownNative = native.find((row) => row.agent_id === own.agent_id)!;
      expect(ownNative.created_by).toBeTruthy();
      const machineOwner = ownNative.created_by!;
      const first = native.find((row) => row.name === config(0).name)!;
      const allowlisted = native.find((row) => row.name === config(1).name)!;
      const managed = native.find((row) => row.name === config(2).name)!;
      await api.setAgentAccess(allowlisted.agent_id, {
        visibility: 'allowlist', admins: [], allowed_user_ids: [machineOwner],
      });
      await api.setAgentAccess(managed.agent_id, {
        visibility: 'private', admins: [machineOwner], allowed_user_ids: [],
      });
      const adminView = await api.data<AgentRecord>('GET', `/agents/${first.agent_id}`);
      expect(adminView.agent_id).toBe(first.agent_id);
      const refused = await machineApiContext.get(apiPath(`/agents/${first.agent_id}`));
      expect(refused.status(), 'a private Agent must be concealed from the independent API principal').toBe(404);
      expect((await refused.json()).code).toBe('AGENT_NOT_FOUND');
      for (const id of [own.agent_id, allowlisted.agent_id, managed.agent_id]) {
        expect((await machineApiContext.get(apiPath(`/agents/${id}`))).ok()).toBe(true);
      }
      native = nativeAgents();
      const visible = native.filter((row) => (
        !['private', 'allowlist'].includes(row.visibility || '')
        || row.created_by === machineOwner || row.admins?.includes(machineOwner)
        || (row.visibility === 'allowlist' && row.allowed_user_ids?.includes(machineOwner))
      ));
      const expected = ordered(visible.filter((row) => row.name.startsWith(prefix)));
      const hiddenIndices = Array.from({ length: ROWS }, (_, index) => index)
        .filter((index) => index === 0 || index % 7 === 3);
      expect(expected).toHaveLength(ROWS - hiddenIndices.length);
      const received: string[] = [];
      let cursor: string | null = null;
      do {
        const suffix: string = cursor ? `&cursor=${encodeURIComponent(cursor)}` : '';
        const response = await machineApiContext.get(apiPath(
          `/agents?page=1&limit=${PAGE_SIZE}&q=${prefix}${suffix}`,
        ));
        expect(response.ok()).toBe(true);
        const body = (await response.json()).data;
        expect(body.agents.map((row: AgentRecord) => row.agent_id))
          .toEqual(expected.slice(received.length, received.length + PAGE_SIZE).map((row) => row.agent_id));
        if (!cursor) {
          expect(body.total).toBe(visible.length);
          expect(body.enabled).toBe(visible.filter((row) => row.enabled !== false).length);
        } else {
          expect(body).not.toHaveProperty('total');
          expect(body).not.toHaveProperty('enabled');
        }
        received.push(...body.agents.map((row: AgentRecord) => row.agent_id));
        expect(body.has_more).toBe(received.length < expected.length);
        cursor = body.next_cursor;
        expect(Boolean(cursor)).toBe(body.has_more);
      } while (cursor);
      expect(received).toEqual(expected.map((row) => row.agent_id));
      expect(new Set(received).size).toBe(received.length);
      const disabled = await machineApiContext.get(apiPath(
        `/agents?page=1&limit=${PAGE_SIZE}&q=${prefix}&status=disabled`,
      ));
      expect(disabled.ok()).toBe(true);
      const disabledBody = (await disabled.json()).data;
      expect(disabledBody.agents.map((row: AgentRecord) => row.agent_id))
        .toEqual(expected.filter((row) => row.enabled === false).map((row) => row.agent_id));
      expect(disabledBody.has_more).toBe(false);
      expect(disabledBody.total).toBe(visible.length);

      await page.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
      await page.goto(appPath('/manage/agents'));
      const headerCount = page.getByRole('heading', { level: 1 }).locator('xpath=following-sibling::span[1]');
      const expectedMeta = () => `${native.length} agents · ${native.filter((row) => row.enabled !== false).length} enabled`;
      await expect(headerCount).toHaveText(expectedMeta());
      const rail = page.locator('[data-slot="sidebar"]');
      if ((await rail.getAttribute('data-state')) === 'collapsed') {
        await page.locator('[data-slot="sidebar-trigger"]').first().click();
      }
      const railCount = rail.locator(
        '[data-slot="sidebar-menu-item"]:has(a[href$="/manage/agents"]) [data-slot="sidebar-menu-badge"]',
      );
      await expect(railCount).toHaveText(String(native.length));
      const shellControl = page.locator('[data-slot="sidebar-trigger"]').first();
      const shellControlBefore = await shellControl.boundingBox();
      await Promise.all([
        page.waitForResponse((response) => {
          const url = new URL(response.url());
          return response.request().method() === 'GET' && url.pathname === apiPath('/agents')
            && url.searchParams.get('q') === prefix && !url.searchParams.has('cursor');
        }),
        page.getByPlaceholder('Search by name or model').fill(prefix),
      ]);
      const rows = page.getByTestId('console-table').locator('.console-row');
      const rowNames = () => rows.evaluateAll((elements, beginning) => elements.map((element) => (
        element.querySelector('td')?.textContent?.match(new RegExp(`${beginning}\\d{3}(?:_moved)?`))?.[0] ?? null
      )), prefix);
      await expect(rows).toHaveCount(PAGE_SIZE);
      await expect.poll(rowNames).toEqual(
        ordered(native.filter((row) => row.name.startsWith(prefix))).slice(0, PAGE_SIZE).map((row) => row.name),
      );
      const loadMore = page.getByRole('button', { name: 'Load more', exact: true });
      await loadMore.click();
      await expect(rows).toHaveCount(PAGE_SIZE * 2);
      await expect.poll(rowNames).toEqual(
        ordered(native.filter((row) => row.name.startsWith(prefix))).slice(0, PAGE_SIZE * 2).map((row) => row.name),
      );
      await expect(headerCount).toHaveText(expectedMeta());

      // Moving a later row into the loaded range invalidates the old page-one
      // cursor. Refresh must rebuild exactly the two pages the reader loaded.
      const stored = await api.data<AgentRecord>('GET', `/agents/${own.agent_id}`);
      await api.updateAgent(own.agent_id, { ...stored, name: `${prefix}000_moved` });
      const refreshReads: string[] = [];
      const observe = (item: { method(): string; url(): string }) => {
        const url = new URL(item.url());
        if (item.method() === 'GET' && url.pathname === apiPath('/agents') && url.searchParams.get('q') === prefix) {
          refreshReads.push(url.searchParams.get('cursor') || 'first');
        }
      };
      page.on('request', observe);
      await page.getByRole('button', { name: 'Refresh', exact: true }).click();
      native = nativeAgents();
      const browserExpected = ordered(native.filter((row) => row.name.startsWith(prefix)));
      await expect.poll(rowNames).toEqual(browserExpected.slice(0, PAGE_SIZE * 2).map((row) => row.name));
      page.off('request', observe);
      expect(refreshReads).toHaveLength(2);
      expect(refreshReads[0]).toBe('first');
      expect(refreshReads[1]).not.toBe('first');
      for (let count = PAGE_SIZE * 2; count < ROWS; count += PAGE_SIZE) {
        await loadMore.click();
        await expect(rows).toHaveCount(count + PAGE_SIZE);
      }
      await expect.poll(rowNames).toEqual(browserExpected.map((row) => row.name));
      await expect(loadMore).toHaveCount(0);
      await expect(headerCount).toHaveText(expectedMeta());
      await expect(railCount).toHaveText(String(native.length));
      await rows.last().scrollIntoViewIfNeeded();
      await expect(rows.last()).toBeInViewport();
      await expect(shellControl).toBeInViewport();
      expect((await shellControl.boundingBox())?.y).toBe(shellControlBefore?.y);
      const layout = await rows.first().evaluate((row) => {
        const documentRoot = document.documentElement;
        let scrollOwner: { slot: string | null; top: number } | null = null;
        for (let node = row.parentElement; node && node !== documentRoot; node = node.parentElement) {
          if (/auto|scroll/.test(getComputedStyle(node).overflowY) && node.scrollHeight > node.clientHeight + 1) {
            scrollOwner = { slot: node.getAttribute('data-slot'), top: node.scrollTop };
            break;
          }
        }
        return { scrollOwner, width: documentRoot.scrollWidth, clientWidth: documentRoot.clientWidth,
          height: documentRoot.scrollHeight, clientHeight: documentRoot.clientHeight };
      });
      expect(layout.scrollOwner?.slot).toBe('page-scroll');
      expect(layout.scrollOwner?.top).toBeGreaterThan(0);
      expect(layout.width).toBeLessThanOrEqual(layout.clientWidth + 1);
      expect(layout.height).toBeLessThanOrEqual(layout.clientHeight + 2);
      await page.locator('[data-slot="page-scroll"]').evaluate((node) => { node.scrollTop = 0; });
      await expect(page.getByRole('heading', { level: 1 })).toBeInViewport();
      await test.info().attach('real-agent-pages.json', {
        body: JSON.stringify({ createdIds, machineVisibleIds: received, refreshReads, layout,
          browserCounts: { total: native.length, enabled: native.filter((row) => row.enabled !== false).length } }),
        contentType: 'application/json',
      });
    } finally {
      await machineApiContext.dispose();
    }
  } finally {
    await machine.dispose();
  }
});
