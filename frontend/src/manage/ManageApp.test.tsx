// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { SWRConfig } from 'swr';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { SidebarProvider } from '@/components/ui/sidebar';
import i18n from '@/i18n';

import { MANAGE_NAV_COUNT_KEYS } from './navCounts';

const api = vi.hoisted(() => ({
  adminListIntegrations: vi.fn(),
  adminNavigationSummary: vi.fn(),
  adminListSessions: vi.fn(),
  listAdminEnvironments: vi.fn(),
  listAgents: vi.fn(),
}));

vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...api,
}));

const { ManageSidebar } = await import('./ManageApp');

afterEach(cleanup);
beforeAll(async () => {
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
  );
  await i18n.changeLanguage('en');
});
beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset();
  api.adminListIntegrations.mockResolvedValue({ services: [] });
  api.adminNavigationSummary.mockResolvedValue({ agents: 3, environments: 4, sessions: 5 });
});

function renderSidebar(pathname: string, fallback: Record<string, number> = {}) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), fallback }}>
      <MemoryRouter initialEntries={[pathname]}>
        <SidebarProvider>
          <ManageSidebar />
        </SidebarProvider>
      </MemoryRouter>
    </SWRConfig>,
  );
}

function badgeFor(linkName: string): Element | null | undefined {
  return screen
    .getByRole('link', { name: linkName })
    .parentElement
    ?.querySelector('[data-slot="sidebar-menu-badge"]');
}

describe('ManageSidebar collection counts', () => {
  it('loads one count-only summary instead of three collection listings', async () => {
    renderSidebar('/manage/credentials');

    await waitFor(() => expect(badgeFor('Agents')?.textContent).toBe('3'));
    expect(badgeFor('Environments')?.textContent).toBe('4');
    expect(badgeFor('Sessions')?.textContent).toBe('5');
    expect(api.adminNavigationSummary).toHaveBeenCalledTimes(1);
    expect(api.listAgents).not.toHaveBeenCalled();
    expect(api.listAdminEnvironments).not.toHaveBeenCalled();
    expect(api.adminListSessions).not.toHaveBeenCalled();
  });

  it('lets an open list response override its summary count', async () => {
    renderSidebar('/manage/agents', { [MANAGE_NAV_COUNT_KEYS.agents]: 9 });

    await waitFor(() => expect(badgeFor('Agents')?.textContent).toBe('9'));
  });
});
