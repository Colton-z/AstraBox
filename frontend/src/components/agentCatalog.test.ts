// How the home page orders a search's results and picks the tab it opens on.
import { describe, expect, it } from 'vitest';

import type { AgentCategory, AgentConfig } from '@/types';
import {
  ALL_TAB,
  FAVORITES_TAB,
  agentSearchFields,
  categoriesWithAgents,
  categoryTab,
  initialHomeTab,
  rankAgents,
} from './agentCatalog';

/** One row of `GET /api/v1/agents`, as the server sends it. */
function agent(over: Partial<AgentConfig> & { agent_id: string; name: string }): AgentConfig {
  return {
    model: 'm',
    state: 'ACTIVE',
    description: '',
    categories: [],
    display_meta: {},
    favorited: false,
    can_manage: false,
    ...over,
  };
}

const CATEGORIES: AgentCategory[] = [
  { category_id: 'cat_writing', name: 'Writing', position: 0 },
  { category_id: 'cat_data', name: 'Data', position: 1 },
  { category_id: 'cat_empty', name: 'Legal', position: 2 },
];

const names = new Map(CATEGORIES.map((c) => [c.category_id, c.name]));
const search = (agents: AgentConfig[], query: string) =>
  rankAgents(agents, query, (a) => agentSearchFields(a, names)).map((a) => a.agent_id);

describe('search ranks the name first, then labels, then the description', () => {
  // Each Agent matches "report" in exactly one way, so the order is the ranking.
  const AGENTS = [
    agent({ agent_id: 'in-description', name: 'helper', description: 'Writes the weekly report.' }),
    agent({ agent_id: 'in-tag', name: 'summarizer', display_meta: { tags: ['report-writing'] } }),
    agent({ agent_id: 'contains', name: 'quarterly-report-bot' }),
    agent({ agent_id: 'prefix', name: 'report-builder' }),
    agent({ agent_id: 'exact', name: 'x-1', display_meta: { display_name: 'Report' } }),
  ];

  it('orders exact name, name prefix, name contains, tag, description', () => {
    expect(search(AGENTS, 'report')).toEqual([
      'exact',
      'prefix',
      'contains',
      'in-tag',
      'in-description',
    ]);
  });

  it('ignores case and surrounding space, and leaves out what does not match', () => {
    expect(search(AGENTS, '  REPORT-BU ')).toEqual(['prefix']);
    expect(search(AGENTS, 'nothing like it')).toEqual([]);
  });

  it('matches a category by its name and a tag written the way a card shows it', () => {
    const agents = [
      agent({ agent_id: 'charts', name: 'charts', categories: ['cat_data'] }),
      agent({ agent_id: 'pdf', name: 'reader', display_meta: { tags: ['pdf'] } }),
    ];
    expect(search(agents, 'data')).toEqual(['charts']);
    expect(search(agents, '#pdf')).toEqual(['pdf']);
  });

  it('breaks a tie by display name without regard to case, and sorts by it without a query', () => {
    const agents = [
      agent({ agent_id: 'b', name: 'zeta', display_meta: { display_name: 'beta report' } }),
      agent({ agent_id: 'a', name: 'omega', display_meta: { display_name: 'Alpha report' } }),
      agent({ agent_id: 'c', name: 'Charlie' }),
    ];
    expect(search(agents, 'report')).toEqual(['a', 'b']);
    expect(search(agents, '')).toEqual(['a', 'b', 'c']);
  });
});

describe('the tab the home page opens on', () => {
  const tabs = [FAVORITES_TAB, ALL_TAB, categoryTab('cat_writing')];

  it('is the tab the reader chose last', () => {
    expect(initialHomeTab({ remembered: categoryTab('cat_writing'), tabs, favorites: 2 }))
      .toBe(categoryTab('cat_writing'));
    expect(initialHomeTab({ remembered: ALL_TAB, tabs, favorites: 2 })).toBe(ALL_TAB);
  });

  it('is the favorites when nothing is remembered and the reader has one', () => {
    expect(initialHomeTab({ remembered: null, tabs, favorites: 1 })).toBe(FAVORITES_TAB);
  });

  it('is every Agent when nothing is remembered and nothing is favorited', () => {
    expect(initialHomeTab({ remembered: null, tabs, favorites: 0 })).toBe(ALL_TAB);
  });

  it('falls back when the remembered category no longer has a tab', () => {
    // Deleted by an administrator since, or left with no Agent this reader sees.
    expect(initialHomeTab({ remembered: categoryTab('cat_gone'), tabs, favorites: 0 }))
      .toBe(ALL_TAB);
    expect(initialHomeTab({ remembered: categoryTab('cat_gone'), tabs, favorites: 3 }))
      .toBe(FAVORITES_TAB);
  });
});

describe('category tabs', () => {
  it('leaves out a category none of the reader\'s Agents is in, keeping the administrator\'s order', () => {
    const agents = [
      agent({ agent_id: 'a', name: 'a', categories: ['cat_data'] }),
      agent({ agent_id: 'b', name: 'b', categories: ['cat_writing', 'cat_data'] }),
    ];
    expect(categoriesWithAgents(CATEGORIES, agents).map((c) => c.name)).toEqual([
      'Writing',
      'Data',
    ]);
  });
});
