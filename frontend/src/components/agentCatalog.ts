// How the home page sorts the Agents a reader can see into tabs, narrows a tab
// by search and tag, and remembers which tab the reader last chose.
import type { AgentCategory, AgentConfig } from '@/types';
import { agentDisplayName } from '@/manage/agentConfig';

export const FAVORITES_TAB = 'favorites';
export const ALL_TAB = 'all';

/**
 * The tab value of one category.
 *
 * Prefixed, so a category id can never be read as one of the two fixed tabs.
 */
export function categoryTab(categoryId: string): string {
  return `category:${categoryId}`;
}

/** An Agent's tags as stored under `display_meta.tags`. */
export function agentTags(agent: AgentConfig): string[] {
  const tags = agent.display_meta?.tags;
  return Array.isArray(tags)
    ? tags.filter((tag): tag is string => typeof tag === 'string' && tag !== '')
    : [];
}

/** The ids of the categories an Agent is listed under. */
function agentCategoryIds(agent: AgentConfig): string[] {
  return Array.isArray(agent.categories)
    ? agent.categories.filter((id): id is string => typeof id === 'string')
    : [];
}

/**
 * The categories that get a tab, in the administrator's order.
 *
 * A category none of the reader's Agents is in would open onto an empty page
 * (docs/frontend-design.md §5), so it has no tab.
 */
export function categoriesWithAgents(
  categories: AgentCategory[],
  agents: AgentConfig[],
): AgentCategory[] {
  const held = new Set(agents.flatMap(agentCategoryIds));
  return categories.filter((category) => held.has(category.category_id));
}

/** The Agents a tab holds, before any search or tag narrows it. */
export function agentsInTab(agents: AgentConfig[], tab: string): AgentConfig[] {
  if (tab === FAVORITES_TAB) return agents.filter((agent) => agent.favorited === true);
  if (tab === ALL_TAB) return agents;
  return agents.filter((agent) => agentCategoryIds(agent).some((id) => categoryTab(id) === tab));
}

/**
 * The tab the page opens on.
 *
 * The reader's last choice wins while it is still a tab; a category deleted
 * since, or left without an Agent they can see, has no tab and does not count.
 * Without one, the favorites when there are any, and every Agent otherwise.
 */
export function initialHomeTab({
  remembered,
  tabs,
  favorites,
}: {
  remembered: string | null;
  tabs: string[];
  favorites: number;
}): string {
  if (remembered && tabs.includes(remembered)) return remembered;
  return favorites > 0 ? FAVORITES_TAB : ALL_TAB;
}

const TAB_KEY_PREFIX = 'astrabox.agent-home.tab.';

/**
 * The tab this user last chose, from this browser.
 *
 * Keyed by user, so two people sharing a browser each get their own. Storage
 * can be unavailable (a private window, blocked site data), and then nothing
 * is remembered.
 */
export function readRememberedTab(userId: string | undefined): string | null {
  if (!userId) return null;
  try {
    return localStorage.getItem(`${TAB_KEY_PREFIX}${userId}`);
  } catch {
    return null;
  }
}

export function rememberTab(userId: string | undefined, tab: string): void {
  if (!userId) return;
  try {
    localStorage.setItem(`${TAB_KEY_PREFIX}${userId}`, tab);
  } catch {
    /* Storage can be unavailable. */
  }
}

/** What a search reads of one Agent. */
export interface AgentSearchFields {
  displayName: string;
  name: string;
  tags: string[];
  categoryNames: string[];
  description: string;
}

export function agentSearchFields(
  agent: AgentConfig,
  categoryNames: Map<string, string>,
): AgentSearchFields {
  // The record's own type allows a nameless row; it sorts and matches as an
  // empty name rather than failing the page in render.
  return {
    displayName: String(agentDisplayName(agent) ?? ''),
    name: String(agent.name ?? ''),
    tags: agentTags(agent),
    categoryNames: agentCategoryIds(agent).flatMap((id) => {
      const name = categoryNames.get(id);
      return name ? [name] : [];
    }),
    description: String(agent.description ?? ''),
  };
}

/**
 * How well an Agent matches a search, lower first, or null for no match.
 *
 * The name is what a reader types most often and is the strongest evidence,
 * so a whole-name match comes before a name that starts with the query, then
 * one that contains it. Tags and categories are the Agent's labels, ahead of
 * its description, which matches incidental words. A leading `#` is how a tag
 * is shown on a card, so it is ignored when matching tags.
 */
function matchRank(fields: AgentSearchFields, query: string): number | null {
  const names = [fields.displayName, fields.name].map((value) => value.toLowerCase());
  if (names.includes(query)) return 0;
  if (names.some((name) => name.startsWith(query))) return 1;
  if (names.some((name) => name.includes(query))) return 2;
  const tag = query.replace(/^#+/, '');
  if (
    (tag !== '' && fields.tags.some((value) => value.toLowerCase().includes(tag)))
    || fields.categoryNames.some((value) => value.toLowerCase().includes(query))
  ) {
    return 3;
  }
  if (fields.description.toLowerCase().includes(query)) return 4;
  return null;
}

const byName = new Intl.Collator(undefined, { sensitivity: 'accent' });

function compareNames(a: AgentSearchFields, b: AgentSearchFields): number {
  return byName.compare(a.displayName, b.displayName) || byName.compare(a.name, b.name);
}

/** Agents in display-name order, without regard to case. */
function sortByName<T>(items: T[], fieldsOf: (item: T) => AgentSearchFields): T[] {
  return items
    .map((item) => ({ item, fields: fieldsOf(item) }))
    .sort((a, b) => compareNames(a.fields, b.fields))
    .map(({ item }) => item);
}

/**
 * The Agents matching `query`, best match first and equal matches by name.
 *
 * An empty query matches everything, in name order.
 */
export function rankAgents<T>(
  items: T[],
  query: string,
  fieldsOf: (item: T) => AgentSearchFields,
): T[] {
  const needle = query.trim().toLowerCase();
  if (needle === '') return sortByName(items, fieldsOf);
  return items
    .flatMap((item) => {
      const fields = fieldsOf(item);
      const rank = matchRank(fields, needle);
      return rank === null ? [] : [{ item, fields, rank }];
    })
    .sort((a, b) => a.rank - b.rank || compareNames(a.fields, b.fields))
    .map(({ item }) => item);
}
