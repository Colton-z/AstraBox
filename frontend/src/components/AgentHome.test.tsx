// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SWRConfig } from 'swr';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { AgentCategory, AgentConfig, AgentFavorite, UserInfo } from '@/types';

const USER: UserInfo = {
  user_id: 'u-ada',
  display_name: 'Ada',
  email: 'ada@example.com',
  avatar_url: null,
  is_admin: false,
  roles: [],
};

// Plain functions rather than spies: a spy inspects the promise it returns,
// and a refusal — the case the star's rollback exists for — would surface as an
// unhandled rejection before the page ever saw it. Calls are recorded by hand.
let listed: AgentConfig[] = [];
let categories: AgentCategory[] = [];
let favorite: (agentId: string, favorited: boolean) => Promise<AgentFavorite> = async (
  agentId,
  favorited,
) => ({ agent_id: agentId, favorited });
const favoriteCalls: [string, boolean][] = [];
const toasts: unknown[][] = [];

vi.mock('@/api', () => ({
  MODULE_BASE: '',
  getCurrentUser: async () => USER,
  listAgents: async () => listed,
  listAgentCategories: async () => categories,
  setAgentFavorite: (agentId: string, favorited: boolean) => {
    favoriteCalls.push([agentId, favorited]);
    return favorite(agentId, favorited);
  },
  startAgentConversation: async () => ({ session_id: 's-1', agent_id: 'a1' }),
}));
vi.mock('sonner', async (importOriginal) => ({
  ...(await importOriginal<typeof import('sonner')>()),
  toast: { error: (...args: unknown[]) => toasts.push(args) },
}));

import { AgentHome } from './AgentHome';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
beforeEach(() => {
  listed = [];
  categories = [];
  favorite = async (agentId, favorited) => ({ agent_id: agentId, favorited });
  favoriteCalls.length = 0;
  toasts.length = 0;
});
afterEach(() => {
  cleanup();
  localStorage.clear();
});

/** One row of `GET /api/v1/agents` (the full list), as the server sends it. */
function agent(over: Partial<AgentConfig> & { agent_id: string; name: string }): AgentConfig {
  return {
    model: 'm',
    state: 'ACTIVE',
    description: '',
    categories: [],
    display_meta: {},
    favorited: false,
    can_manage: false,
    can_start_conversation: true,
    ...over,
  };
}

const WRITING: AgentCategory = { category_id: 'cat_writing', name: 'Writing', position: 0 };
const DATA: AgentCategory = { category_id: 'cat_data', name: 'Data', position: 1 };
const LEGAL: AgentCategory = { category_id: 'cat_legal', name: 'Legal', position: 2 };

function renderHome() {
  // A cache per render, so the signed-in user is read the way a fresh page
  // reads it rather than left over from the test before.
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <AgentHome onConversationCreated={() => {}} />
    </SWRConfig>,
  );
}

const cardNames = () =>
  screen.getAllByTestId('agent-option').map((card) => card.getAttribute('data-agent-name'));

const selectedTab = () =>
  screen.getAllByRole('tab').find((tab) => tab.getAttribute('aria-selected') === 'true')
    ?.textContent;

/**
 * The page exists so somebody can choose. That makes "the cards say something
 * different from each other" the property worth pinning: a single constant
 * repeated on every card is exactly as useful as no copy at all
 * (docs/frontend-design.md §2).
 */
describe('AgentHome cards', () => {
  it('shows each agent its own description, so two cards can be told apart', async () => {
    listed = [
      agent({ agent_id: 'a1', name: 'Data Analyst', description: 'Inspects CSV and plots it.' }),
      agent({ agent_id: 'a2', name: 'Claude Code', description: 'Works in a repo checkout.' }),
    ];
    renderHome();

    expect(await screen.findByText('Inspects CSV and plots it.')).toBeTruthy();
    expect(screen.getByText('Works in a repo checkout.')).toBeTruthy();
  });

  it('says nothing about an agent that set no description', async () => {
    listed = [
      agent({ agent_id: 'a1', name: 'Described', description: 'Its own line.' }),
      agent({ agent_id: 'a2', name: 'Bare', description: '   ' }),
    ];
    renderHome();

    expect(await screen.findByText('Its own line.')).toBeTruthy();
    // A card must not repeat its own button. The fallback sentence for a
    // description-less agent is "Start a new conversation with this Agent."
    // directly above a button reading "Start conversation" — the same words
    // twice, telling a reader choosing between cards nothing
    // (docs/frontend-design.md §2). Silence is the honest answer.
    const cards = screen.getAllByTestId('agent-option');
    const bare = cards.find((c) => c.getAttribute('data-agent-name') === 'Bare');
    expect(bare).toBeTruthy();
    const buttonLabel = bare!.querySelector('[data-testid="agent-start"]')?.textContent?.trim() ?? '';
    expect(buttonLabel).not.toEqual('');
    expect(bare!.textContent?.split(buttonLabel).length).toBe(2);
  });
});

describe('the tab the home page opens on', () => {
  it('is the one this user chose last time', async () => {
    const user = userEvent.setup();
    categories = [WRITING, DATA];
    listed = [
      agent({ agent_id: 'w', name: 'Editor', categories: ['cat_writing'] }),
      agent({ agent_id: 'd', name: 'Charts', categories: ['cat_data'] }),
    ];
    const first = renderHome();
    await user.click(await screen.findByRole('tab', { name: 'Data' }));
    expect(cardNames()).toEqual(['Charts']);
    first.unmount();

    renderHome();
    await screen.findByRole('tab', { name: 'Data' });
    expect(selectedTab()).toBe('Data');
    expect(cardNames()).toEqual(['Charts']);
  });

  it('is the favorites when nothing is remembered and the user has one', async () => {
    listed = [
      agent({ agent_id: 'k', name: 'Kept', favorited: true }),
      agent({ agent_id: 'o', name: 'Other' }),
    ];
    renderHome();

    await screen.findByRole('tab', { name: 'Favorites' });
    expect(selectedTab()).toBe('Favorites');
    expect(cardNames()).toEqual(['Kept']);
  });

  it('is every Agent when nothing is remembered and nothing is favorited', async () => {
    listed = [agent({ agent_id: 'b', name: 'Beta' }), agent({ agent_id: 'a', name: 'alpha' })];
    renderHome();

    await screen.findByRole('tab', { name: 'All' });
    expect(selectedTab()).toBe('All');
    // In name order, without regard to case.
    expect(cardNames()).toEqual(['alpha', 'Beta']);
  });

  it('falls back when the remembered category is gone', async () => {
    const user = userEvent.setup();
    categories = [WRITING, DATA];
    listed = [
      agent({ agent_id: 'w', name: 'Editor', categories: ['cat_writing'] }),
      agent({ agent_id: 'd', name: 'Charts', categories: ['cat_data'] }),
    ];
    const first = renderHome();
    await user.click(await screen.findByRole('tab', { name: 'Data' }));
    first.unmount();

    // An administrator deleted it since; the server took it off its Agents.
    categories = [WRITING];
    listed = [
      agent({ agent_id: 'w', name: 'Editor', categories: ['cat_writing'] }),
      agent({ agent_id: 'd', name: 'Charts' }),
    ];
    renderHome();
    await screen.findByRole('tab', { name: 'All' });
    expect(screen.queryByRole('tab', { name: 'Data' })).toBeNull();
    expect(selectedTab()).toBe('All');
  });
});

describe('category tabs', () => {
  it('has no tab for a category none of this user\'s Agents is in', async () => {
    categories = [WRITING, DATA, LEGAL];
    listed = [
      agent({ agent_id: 'w', name: 'Editor', categories: ['cat_writing'] }),
      agent({ agent_id: 'd', name: 'Charts', categories: ['cat_data', 'cat_writing'] }),
    ];
    renderHome();

    await screen.findByRole('tab', { name: 'Writing' });
    expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual([
      'Favorites',
      'All',
      'Writing',
      'Data',
    ]);
  });
});

describe('the star on a card', () => {
  it('favorites at once and takes it back, with a message, when the write fails', async () => {
    const user = userEvent.setup();
    listed = [agent({ agent_id: 'a1', name: 'Data Analyst' })];
    let refuse!: (error: Error) => void;
    favorite = () => new Promise<AgentFavorite>((_resolve, reject) => {
      refuse = reject;
    });
    renderHome();

    const star = await screen.findByRole('button', { name: 'Favorite Data Analyst' });
    expect(star.getAttribute('aria-pressed')).toBe('false');
    await user.click(star);
    // Shown before the server answers.
    expect(screen.getByRole('button', { name: 'Favorite Data Analyst' }).getAttribute('aria-pressed'))
      .toBe('true');
    expect(favoriteCalls).toEqual([['a1', true]]);

    await act(async () => {
      refuse(new Error('AGENT_NOT_FOUND: Agent a1 does not exist'));
    });
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: 'Favorite Data Analyst' }).getAttribute('aria-pressed'),
      ).toBe('false'),
    );
    expect(toasts).toEqual([
      [
        "Couldn't add Data Analyst to favorites",
        { description: 'AGENT_NOT_FOUND: Agent a1 does not exist' },
      ],
    ]);
  });
});

describe('a tag on a card', () => {
  it('narrows the tab to the Agents carrying it until the filter is removed', async () => {
    const user = userEvent.setup();
    listed = [
      agent({ agent_id: 'p', name: 'PDF Reader', display_meta: { tags: ['pdf', 'research'] } }),
      agent({ agent_id: 'w', name: 'Writer', display_meta: { tags: ['writing'] } }),
    ];
    renderHome();

    await user.click(await screen.findByRole('button', { name: 'Show Agents tagged #pdf' }));
    expect(cardNames()).toEqual(['PDF Reader']);

    const active = screen.getByRole('button', { name: 'Remove the #pdf filter' });
    expect(active.textContent).toBe('#pdf');
    await user.click(active);
    expect(cardNames()).toEqual(['PDF Reader', 'Writer']);
    expect(screen.queryByRole('button', { name: 'Remove the #pdf filter' })).toBeNull();
  });
});
