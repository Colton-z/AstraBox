// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { SWRConfig } from 'swr';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { SidebarProvider } from '@/components/ui/sidebar';
import i18n from '@/i18n';

import { MANAGE_NAV_COUNT_KEYS } from './navCounts';

const api = vi.hoisted(() => ({
  getCurrentUser: vi.fn(),
  adminListIntegrations: vi.fn(),
  adminNavigationSummary: vi.fn(),
  adminListSessions: vi.fn(),
  listAdminEnvironments: vi.fn(),
  listAgents: vi.fn(),
  listAgentsPage: vi.fn(),
}));

vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...api,
}));

const { ManageSidebar } = await import('./ManageApp');
const { ModelGatewayLink } = await import('./ModelGatewayLink');

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
  api.getCurrentUser.mockResolvedValue({ user_id: 'admin', is_admin: true });
  api.adminListIntegrations.mockResolvedValue({ services: [] });
  api.adminNavigationSummary.mockResolvedValue({ agents: 3, environments: 4, sessions: 5 });
});

function renderSidebar(pathname: string, fallback: Record<string, number> = {}, withGateway = false) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), fallback, shouldRetryOnError: false }}>
      <MemoryRouter initialEntries={[pathname]}>
        <SidebarProvider>
          <ManageSidebar />
          {withGateway && <ModelGatewayLink />}
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
    expect(api.listAgentsPage).not.toHaveBeenCalled();
    expect(api.listAdminEnvironments).not.toHaveBeenCalled();
    expect(api.adminListSessions).not.toHaveBeenCalled();
  });

  it('lets an open list response override its summary count', async () => {
    renderSidebar('/manage/agents', { [MANAGE_NAV_COUNT_KEYS.agents]: 9 });

    await waitFor(() => expect(badgeFor('Agents')?.textContent).toBe('9'));
  });
});

describe('management integrations permissions', () => {
  it('keeps public Agent navigation without requesting administrator APIs for a visitor', async () => {
    api.getCurrentUser.mockResolvedValue({ user_id: 'visitor', is_admin: false });
    api.adminListIntegrations.mockRejectedValue(new Error('ADMIN_ROLE_REQUIRED'));
    renderSidebar('/manage/agents', { [MANAGE_NAV_COUNT_KEYS.agents]: 1 }, true);

    await act(async () => {});
    expect(api.getCurrentUser).toHaveBeenCalledTimes(1);
    expect(api.adminListIntegrations).not.toHaveBeenCalled();
    expect(api.adminNavigationSummary).not.toHaveBeenCalled();
    expect(badgeFor('Agents')?.textContent).toBe('1');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('waits for the user role before reading administrator APIs', async () => {
    api.getCurrentUser.mockReturnValue(new Promise(() => {}));
    renderSidebar('/manage/agents', {}, true);

    await act(async () => {});
    expect(api.adminListIntegrations).not.toHaveBeenCalled();
    expect(api.adminNavigationSummary).not.toHaveBeenCalled();
  });

  it('shares the integration read between the rail and model field for administrators', async () => {
    api.adminListIntegrations.mockResolvedValue({ services: [{
      id: 'litellm', name: 'LiteLLM', category: 'model_gateway', admin_url: '/litellm',
    }] });
    renderSidebar('/manage/agents', {}, true);

    await waitFor(() => expect(screen.getAllByRole('link').filter(
      (link) => link.getAttribute('href') === '/litellm',
    )).toHaveLength(2));
    expect(api.adminListIntegrations).toHaveBeenCalledTimes(1);
  });

  it('still reports an integration failure to an administrator', async () => {
    api.adminListIntegrations.mockRejectedValue(new Error('Internal server error'));
    renderSidebar('/manage/agents');

    expect((await screen.findByRole('alert')).textContent)
      .toContain('Integrated service links are unavailable.');
  });
});
