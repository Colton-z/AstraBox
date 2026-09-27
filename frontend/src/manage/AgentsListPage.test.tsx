// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import useSWR, { SWRConfig } from 'swr';

import i18n from '@/i18n';
import type { AgentConfig, AgentListPage, ListPageQuery } from '@/types';
import { MANAGE_NAV_COUNT_KEYS, type ManageNavCount } from './navCounts';

const AGENTS = [
  { agent_id: 'ag-1', name: 'support', enabled: true, updated_at: '2026-08-01T00:00:00+00:00' },
  { agent_id: 'ag-2', name: 'triage', enabled: false, updated_at: '2026-08-02T00:00:00+00:00' },
] as unknown as AgentConfig[];

const ONE_PAGE: AgentListPage = { agents: AGENTS, has_more: false, next_cursor: null, total: 2, enabled: 1 };

let listAgentsPage: (query: ListPageQuery) => Promise<AgentListPage> = async () => ONE_PAGE;
const asked: ListPageQuery[] = [];

vi.mock('@/api', () => ({
  listAgentsPage: async (query: ListPageQuery) => {
    asked.push(query);
    return listAgentsPage(query);
  },
}));

const { default: AgentsListPage } = await import('./AgentsListPage');

function NavCountProbe() {
  const { data } = useSWR<ManageNavCount>(MANAGE_NAV_COUNT_KEYS.agents, null);
  return <span data-testid="agent-nav-count">{data ?? 'missing'}</span>;
}

afterEach(cleanup);
beforeAll(async () => {
  await i18n.changeLanguage('en');
});
beforeEach(() => {
  asked.length = 0;
  listAgentsPage = async () => ONE_PAGE;
});

const renderPage = () =>
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <MemoryRouter>
        <AgentsListPage />
        <NavCountProbe />
      </MemoryRouter>
    </SWRConfig>,
  );

function agent(index: number): AgentConfig {
  return {
    agent_id: `ag-${index}`,
    name: `agent-${String(index).padStart(3, '0')}`,
    enabled: true,
  } as unknown as AgentConfig;
}

describe('AgentsListPage', () => {
  it('offers the status filter while there is a list under it', async () => {
    renderPage();

    await waitFor(() => expect(screen.getByText('support')).toBeTruthy());
    // One enabled and one disabled, so the chips can change what is on screen —
    // which is the condition they render on (docs/frontend-design.md §5).
    expect(screen.getByRole('button', { name: /Disabled/ })).toBeTruthy();
    expect(screen.getByTestId('agent-nav-count').textContent).toBe('2');
  });

  it('takes the filter away when a failed refresh leaves no rows to narrow', async () => {
    // A refresh fails while rows are already on screen. If `load()` kept the
    // rows it last read, the chips would go on counting agents the table has
    // dropped, narrowing a list that is not on screen (docs/frontend-design.md
    // §5 — a control renders when the data earns it). The counts are what hides
    // that from a weaker assertion: they stay arithmetically right while
    // describing nothing.
    renderPage();
    await waitFor(() => expect(screen.getByText('support')).toBeTruthy());

    listAgentsPage = async () => {
      throw new Error('the deployment did not answer');
    };
    fireEvent.click(screen.getByRole('button', { name: /Refresh/i }));

    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
    expect(screen.getByText("Couldn't load agents")).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Disabled/ })).toBeNull();
    expect(screen.getByTestId('agent-nav-count').textContent).toBe('missing');
    // And it comes back with the rows, rather than being lost for the session.
    listAgentsPage = async () => ONE_PAGE;
    fireEvent.click(screen.getByRole('button', { name: /Retry/i }));
    await waitFor(() => expect(screen.getByRole('button', { name: /Disabled/ })).toBeTruthy());
    expect(screen.getByTestId('agent-nav-count').textContent).toBe('2');
  });

  it('counts the whole list and reads the rest of it on request', async () => {
    // The server holds 250 Agents; one page of 50 is on screen. The header and
    // the rail state the list's size, not the page's, and the reader can reach
    // every Agent past the first page.
    const all = Array.from({ length: 250 }, (_, index) => agent(index));
    listAgentsPage = async ({ cursor }) => {
      const start = cursor ? Number(cursor) : 0;
      const agents = all.slice(start, start + 50);
      const more = start + 50 < all.length;
      return {
        agents,
        has_more: more,
        next_cursor: more ? String(start + 50) : null,
        ...(cursor ? {} : { total: all.length, enabled: all.length }),
      };
    };
    renderPage();

    await waitFor(() => expect(screen.getByText('agent-049')).toBeTruthy());
    expect(screen.queryByText('agent-050')).toBeNull();
    expect(screen.getByText('250 agents · 250 enabled')).toBeTruthy();
    expect(screen.getByTestId('agent-nav-count').textContent).toBe('250');

    for (let shown = 50; shown < all.length; shown += 50) {
      fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
      await waitFor(() => expect(screen.getByText(`agent-${String(shown + 49).padStart(3, '0')}`)).toBeTruthy());
    }
    expect(screen.getByText('agent-249')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
    // The totals came with the first page and stay put while pages are added.
    expect(screen.getByTestId('agent-nav-count').textContent).toBe('250');
  });

  it('sends the search and the status to the server instead of filtering the loaded rows', async () => {
    const all = Array.from({ length: 30 }, (_, index) => agent(index));
    listAgentsPage = async ({ q, status }) => {
      const agents = status === 'disabled' ? [agent(199)] : q ? [agent(240)] : all;
      return { agents, has_more: false, next_cursor: null, total: 250, enabled: 200 };
    };
    renderPage();
    await waitFor(() => expect(screen.getByText('agent-000')).toBeTruthy());

    fireEvent.change(screen.getByPlaceholderText('Search by name or model'), {
      target: { value: 'agent-24' },
    });

    // agent-240 was never on a loaded page; only the server can find it.
    await waitFor(() => expect(screen.getByText('agent-240')).toBeTruthy());
    expect(asked.at(-1)).toMatchObject({ q: 'agent-24', status: 'all', cursor: null });
    expect(screen.queryByText('agent-000')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /Disabled/ }));
    await waitFor(() => expect(screen.getByText('agent-199')).toBeTruthy());
    expect(asked.at(-1)).toMatchObject({ q: 'agent-24', status: 'disabled', cursor: null });
  });
});
