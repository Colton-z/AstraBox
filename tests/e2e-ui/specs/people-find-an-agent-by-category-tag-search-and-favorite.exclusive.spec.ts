/**
 * E2E: people find an Agent by its category, its tags, a search, and their favorites.
 *
 * A platform administrator defines two categories; three Agents are written
 * through the API the way their authors would, with categories and tags. The
 * home page then has to offer what was written: a tab per category holding
 * only its Agents, a tag on a card that filters by it, a search that finds an
 * Agent by its tag, a star that puts an Agent under Favorites, and a fresh
 * visit that opens on Favorites once the user has one. Deleting a category
 * takes it off its Agent and its tab goes.
 *
 * EXCLUSIVE because a favorite changes which tab every other spec's home page
 * opens on, and those specs look for their Agent under All. For the same
 * reason the favorite is undone whether this case passes or fails.
 *
 * The home page offers its search box only above twelve Agents, so the case
 * tops the user's Agents up to thirteen with Agents that hold no box.
 */

import { randomUUID } from 'node:crypto';
import { expect, test, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { appPath } from '../fixtures/env';

const FAVORITES = /^(Favorites|收藏)$/;
const ALL = /^(All|全部)$/;
const SEARCH = /^(Search Agents|搜索 Agent)$/;

interface Category {
  category_id: string;
  name: string;
}

interface ListedAgent {
  agent_id: string;
  name: string;
  favorited?: boolean;
  categories?: string[];
  display_meta?: { tags?: string[] };
}

function card(page: Page, name: string) {
  return page.locator(`[data-testid="agent-option"][data-agent-name="${name}"]`);
}

async function selected(page: Page, name: RegExp | string) {
  await expect(page.getByRole('tab', { name })).toHaveAttribute('aria-selected', 'true');
}

test('people find an Agent by category, tag, search and favorite', async ({ page, request }) => {
  test.setTimeout(170_000);
  const api = new AstraApi(request);
  const run = randomUUID().slice(0, 8);
  const base = await api.defaultAgent();
  const categoryIds: string[] = [];
  const agentIds: string[] = [];
  const make = async (suffix: string, extra: Record<string, unknown>) => {
    const agent = await api.createAgent({
      name: `__e2e_find_${run}_${suffix}`,
      model: base.model,
      environment_name: base.environment_name,
      prepared_capacity: 0,
      ...extra,
    });
    agentIds.push(agent.agent_id);
    return agent;
  };

  try {
    const research = await api.data<Category>('POST', '/admin/agent-categories', {
      name: `E2E Research ${run}`,
    });
    categoryIds.push(research.category_id);
    const writing = await api.data<Category>('POST', '/admin/agent-categories', {
      name: `E2E Writing ${run}`,
    });
    categoryIds.push(writing.category_id);

    const scout = await make('scout', {
      categories: [research.category_id],
      display_meta: { tags: ['#Deep Dive', 'PDF'] },
    });
    const scribe = await make('scribe', {
      categories: [writing.category_id],
      display_meta: { tags: ['pdf'] },
    });
    const plain = await make('plain', {});
    const stored = await api.data<ListedAgent>('GET', `/agents/${scout.agent_id}`);
    expect(stored.display_meta?.tags, 'tags are stored in one form').toEqual(['deep-dive', 'pdf']);

    const listed = await api.data<ListedAgent[]>('GET', '/agents');
    for (let count = listed.length; count <= 12; count += 1) await make(`filler-${count}`, {});
    const already = listed.filter((agent) => agent.favorited).map((agent) => agent.name);
    expect(already, 'this case starts from a user with no favorites').toEqual([]);

    await page.goto(appPath('/agents'));
    await page.evaluate(() => window.localStorage.clear());
    await page.reload();
    await selected(page, ALL);
    await expect(page.getByRole('tab', { name: FAVORITES })).toBeVisible();
    await expect(page.getByRole('tab', { name: research.name })).toBeVisible();
    await expect(page.getByRole('tab', { name: writing.name })).toBeVisible();

    await page.getByRole('tab', { name: research.name }).click();
    await expect(card(page, scout.name)).toBeVisible();
    await expect(card(page, scribe.name)).toHaveCount(0);
    await expect(card(page, plain.name)).toHaveCount(0);

    await page.getByRole('tab', { name: ALL }).click();
    await card(page, scout.name).getByRole('button', { name: /#pdf\b/ }).click();
    await expect(card(page, scout.name)).toBeVisible();
    await expect(card(page, scribe.name)).toBeVisible();
    await expect(card(page, plain.name)).toHaveCount(0);
    await page.getByRole('button', { name: /^(Remove the #pdf filter|移除 #pdf 筛选)$/ }).click();
    await expect(card(page, plain.name)).toBeVisible();

    await page.getByPlaceholder(SEARCH).fill('deep-dive');
    await expect(card(page, scout.name)).toBeVisible();
    await expect(card(page, scribe.name)).toHaveCount(0);
    await page.getByPlaceholder(SEARCH).fill('');

    await card(page, scribe.name).locator('button[aria-pressed]').click();
    await expect(card(page, scribe.name).locator('button[aria-pressed="true"]')).toBeVisible();
    await expect
      .poll(async () => (await api.data<ListedAgent[]>('GET', '/agents'))
        .filter((agent) => agent.favorited)
        .map((agent) => agent.agent_id))
      .toEqual([scribe.agent_id]);

    await page.evaluate(() => window.localStorage.clear());
    await page.reload();
    await selected(page, FAVORITES);
    await expect(card(page, scribe.name)).toBeVisible();
    await expect(card(page, scout.name)).toHaveCount(0);

    await page.getByRole('tab', { name: writing.name }).click();
    await page.reload();
    await selected(page, writing.name);

    const deleted = await api.data<{ unbound_agents: number }>(
      'DELETE',
      `/admin/agent-categories/${research.category_id}`,
    );
    categoryIds.splice(categoryIds.indexOf(research.category_id), 1);
    expect(deleted.unbound_agents, 'the category is taken off its one Agent').toBe(1);
    const unbound = await api.data<ListedAgent>('GET', `/agents/${scout.agent_id}`);
    expect(unbound.categories).toEqual([]);
    await page.reload();
    await expect(page.getByRole('tab', { name: research.name })).toHaveCount(0);
  } finally {
    for (const agentId of agentIds) {
      await api.data('DELETE', `/agents/${agentId}/favorite`).catch(() => {});
      await api.deleteAgent(agentId).catch(() => {});
    }
    for (const categoryId of categoryIds) {
      await api.data('DELETE', `/admin/agent-categories/${categoryId}`).catch(() => {});
    }
  }
});
