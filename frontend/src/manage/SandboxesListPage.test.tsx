// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { AdminSandboxPage } from '@/types';

// Same reason as the diagnostics panel's mock: the refusal path is the point of
// this file, and a spy that returns a rejected promise reports it as unhandled
// before the page can render it.
let listing: () => Promise<AdminSandboxPage> = async () => page();

vi.mock('@/api', () => ({
  adminListSandboxes: async () => listing(),
  adminReadSandboxDiagnostics: async () => {
    throw new Error('not used in this file');
  },
}));

const { default: SandboxesListPage } = await import('./SandboxesListPage');

afterEach(cleanup);
beforeAll(async () => {
  await i18n.changeLanguage('en');
});
beforeEach(() => {
  listing = async () => page();
});

function page(overrides: Partial<AdminSandboxPage> = {}): AdminSandboxPage {
  return {
    backend: 'open_sandbox',
    items: [
      {
        sandbox_id: 'sbx-ours-0001',
        backend: 'open_sandbox',
        state: 'Running',
        created_at: '2026-07-20T09:30:00+00:00',
        expires_at: null,
        image: 'astrabox/sandbox-claude-code:1',
        entrypoint: ['/opt/gem/run.sh'],
        metadata: { 'astrabox.session-id': 'sess-1' },
        session_id: 'sess-1',
      },
      {
        sandbox_id: 'sbx-foreign-0002',
        backend: 'open_sandbox',
        // A state this console has no opinion about — the backend's own word.
        state: 'CrashLoopBackOff',
        created_at: '2026-07-20T10:00:00+00:00',
        expires_at: null,
        image: 'unknown',
        entrypoint: [],
        metadata: {},
        session_id: null,
      },
    ],
    pagination: {
      page: 1,
      page_size: 50,
      total_items: 2,
      total_pages: 1,
      has_next_page: false,
    },
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <SandboxesListPage />
    </MemoryRouter>,
  );
}

describe('the sandboxes list', () => {
  it('shows the backend’s own state string, including one it has no opinion about', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('CrashLoopBackOff')).toBeTruthy());
    expect(screen.getByText('Running')).toBeTruthy();
  });

  it('claims no session for a box that carries none', async () => {
    const { container } = renderPage();
    await waitFor(() => expect(screen.getByText('CrashLoopBackOff')).toBeTruthy());
    const rows = container.querySelectorAll('.console-row');
    // Two boxes; only the one whose create metadata names a session shows one.
    expect(rows).toHaveLength(2);
    expect(screen.getAllByText('sess-1')).toHaveLength(1);
  });

  it('says a backend cannot enumerate rather than showing an empty table', async () => {
    listing = async () => {
      throw new Error(
        "sandbox backend 'direct_docker' cannot enumerate its sandboxes: its control plane has no listing operation",
      );
    };
    renderPage();

    await waitFor(() => expect(screen.getByText(/cannot enumerate/)).toBeTruthy());
    expect(screen.getByText(i18n.t('manage:sandboxes.error_title'))).toBeTruthy();
    // The empty state would assert something the backend never said: that
    // nothing is running.
    expect(screen.queryByText(i18n.t('manage:sandboxes.empty_title'))).toBeNull();
  });

  it('prints only the count the backend reported', async () => {
    listing = async () =>
      page({
        pagination: { page: 1, page_size: 50, total_items: 120, total_pages: 3, has_next_page: true },
      });
    renderPage();

    // 120 is the inventory; the two loaded rows are a window onto it. A
    // "running" tally taken from the window and printed beside the inventory
    // total would state, by subtraction, how many of the 120 are not running —
    // which the backend never said.
    await waitFor(() =>
      expect(screen.getByText(i18n.t('manage:sandboxes.meta', { count: 120 }))).toBeTruthy(),
    );
    expect(screen.queryByText(/\d+\s+running/i)).toBeNull();
  });

  it('says a filter reached only this page, and that an empty result is not an absence', async () => {
    listing = async () =>
      page({
        pagination: { page: 1, page_size: 50, total_items: 120, total_pages: 3, has_next_page: true },
      });
    renderPage();
    await waitFor(() => expect(screen.getByText('Running')).toBeTruthy());

    // Nothing is claimed while nothing is filtered.
    expect(
      screen.queryByText(i18n.t('manage:sandboxes.filter_scope_note', { page: 1, pages: 3 })),
    ).toBeNull();

    fireEvent.change(screen.getByPlaceholderText(i18n.t('manage:sandboxes.search_placeholder')), {
      target: { value: 'sbx-on-page-3' },
    });

    await waitFor(() =>
      expect(
        screen.getByText(i18n.t('manage:sandboxes.filter_scope_note', { page: 1, pages: 3 })),
      ).toBeTruthy(),
    );
    // The box may well exist on page 2 or 3 — the client never looked there, so
    // the flat "No results match." would be a claim about the inventory.
    expect(
      screen.getByText(i18n.t('manage:sandboxes.no_match_on_page', { page: 1, pages: 3 })),
    ).toBeTruthy();
    expect(screen.queryByText(i18n.t('manage:sandboxes.no_match'))).toBeNull();
  });

  it('does say "no results" when the one page IS the whole inventory', async () => {
    // An inventory big enough to earn the search box (frontend-design §5), but
    // still one page — the case where "no results" is a claim about everything.
    listing = async () =>
      page({
        items: Array.from({ length: 14 }, (_, i) => ({
          ...page().items[0],
          sandbox_id: `sbx-ours-${String(i).padStart(4, '0')}`,
          session_id: `sess-${i}`,
        })),
        pagination: { page: 1, page_size: 50, total_items: 14, total_pages: 1, has_next_page: false },
      });
    renderPage();
    await waitFor(() => expect(screen.getAllByText('Running').length).toBeGreaterThan(0));

    fireEvent.change(screen.getByPlaceholderText(i18n.t('manage:sandboxes.search_placeholder')), {
      target: { value: 'nothing-like-this' },
    });

    await waitFor(() =>
      expect(screen.getByText(i18n.t('manage:sandboxes.no_match'))).toBeTruthy(),
    );
    expect(
      screen.queryByText(i18n.t('manage:sandboxes.filter_scope_note', { page: 1, pages: 1 })),
    ).toBeNull();
  });

  it('offers a pager only when the backend says there is more', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('Running')).toBeTruthy());
    expect(screen.queryByText(i18n.t('manage:sandboxes.next_page'))).toBeNull();

    cleanup();
    listing = async () =>
      page({
        pagination: {
          page: 1,
          page_size: 50,
          total_items: 120,
          total_pages: 3,
          has_next_page: true,
        },
      });
    renderPage();
    // The counters are the backend's; the page never implies it is showing the
    // whole inventory when the backend says otherwise.
    await waitFor(() => expect(screen.getByText(i18n.t('manage:sandboxes.next_page'))).toBeTruthy());
    expect(
      screen.getByText(
        i18n.t('manage:sandboxes.page_of', { page: 1, pages: 3, total: 120 }),
      ),
    ).toBeTruthy();
  });
});
