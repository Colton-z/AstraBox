// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import useSWR, { SWRConfig } from 'swr';

import i18n from '@/i18n';
import type { AgentConfig } from '@/types';
import { MANAGE_NAV_COUNT_KEYS, type ManageNavCount } from './navCounts';

const AGENTS = [
  { agent_id: 'ag-1', name: 'support', enabled: true, updated_at: '2026-08-01T00:00:00+00:00' },
  { agent_id: 'ag-2', name: 'triage', enabled: false, updated_at: '2026-08-02T00:00:00+00:00' },
] as unknown as AgentConfig[];

let listAgents: () => Promise<AgentConfig[]> = async () => AGENTS;

vi.mock('@/api', () => ({
  listAgents: async () => listAgents(),
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
  listAgents = async () => AGENTS;
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

    listAgents = async () => {
      throw new Error('the deployment did not answer');
    };
    fireEvent.click(screen.getByRole('button', { name: /Refresh/i }));

    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
    expect(screen.getByText("Couldn't load agents")).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Disabled/ })).toBeNull();
    expect(screen.getByTestId('agent-nav-count').textContent).toBe('missing');
    // And it comes back with the rows, rather than being lost for the session.
    listAgents = async () => AGENTS;
    fireEvent.click(screen.getByRole('button', { name: /Retry/i }));
    await waitFor(() => expect(screen.getByRole('button', { name: /Disabled/ })).toBeTruthy());
    expect(screen.getByTestId('agent-nav-count').textContent).toBe('2');
  });
});
