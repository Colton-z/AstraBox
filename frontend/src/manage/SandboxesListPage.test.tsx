// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { AdminSandboxPage, AdminSandboxQuota, AdminSandboxQuotaBackend } from '@/types';
import { formatDateTime } from './agentConfig';

// Same reason as the diagnostics panel's mock: the refusal path is the point of
// this file, and a spy that returns a rejected promise reports it as unhandled
// before the page can render it.
let listing: () => Promise<AdminSandboxPage> = async () => page();
let quotaRead: () => Promise<AdminSandboxQuota> = async () => ({ backends: [quotaBackend()] });

vi.mock('@/api', () => ({
  adminListSandboxes: async () => listing(),
  adminSandboxQuota: async () => quotaRead(),
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
  quotaRead = async () => ({ backends: [quotaBackend()] });
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

const GIB = 2 ** 30;
// What the quota charges one conversation's sandbox on the bundled Docker
// server: its own limits (1 CPU, 1Gi) plus the egress sidecar beside it (1 CPU,
// 128Mi).
const CHARGE = { cpu: 2, memory: GIB + 128 * 2 ** 20 };

/**
 * One backend's entry as `quota_report` builds it: two conversations' sandboxes
 * and one pool member nobody has claimed in the inventory, one create still in
 * flight, and one create that has answered but that no inventory shows yet.
 * Usage is what the quota counts from those five.
 */
function quotaBackend(overrides: Partial<AdminSandboxQuotaBackend> = {}): AdminSandboxQuotaBackend {
  return {
    backend: 'open_sandbox',
    quota: {
      boxes: 5,
      // A host of 16,750,000,000 bytes less the 4Gi the derived ceiling leaves.
      memory_bytes: 12_455_032_704,
      cpu: null,
      concurrent_creates: 2,
      sources: {
        boxes: 'configured',
        memory: 'derived: host memory 15.6Gi less 4Gi for the platform and the host',
        cpu: 'unlimited (unset)',
        concurrent_creates: 'configured',
      },
    },
    usage: { boxes: 5, memory_bytes: 5 * CHARGE.memory, cpu: 5 * CHARGE.cpu, creating: 1 },
    inventory_at: '2026-10-10T09:30:00+00:00',
    boxes: [
      { sandbox_id: 'sbx-conv-0001', ...CHARGE, state: 'Running', session_id: 'sess-1' },
      { sandbox_id: 'sbx-conv-0002', ...CHARGE, state: 'Running', session_id: 'sess-2' },
      {
        sandbox_id: 'sbx-pool-0003',
        ...CHARGE,
        state: 'Running',
        session_id: 'client-pool-3f2a9c1b7d4e-0a1b2c3d4e5f60718293a4b5',
      },
    ],
    reservations: [
      {
        state: 'creating',
        reason: 'conversation',
        session_id: 'sess-3',
        sandbox_id: null,
        ...CHARGE,
        reserved_at: '2026-10-10T09:31:00+00:00',
      },
      {
        state: 'created',
        reason: 'prepared-conversation',
        session_id: 'client-pool-3f2a9c1b7d4e-9f8e7d6c5b4a39281706f5e4',
        sandbox_id: 'sbx-pool-0004',
        ...CHARGE,
        reserved_at: '2026-10-10T09:29:00+00:00',
      },
    ],
    ...overrides,
  };
}

/** The quota card, found the way a reader finds it: by its heading. */
async function quotaCard(): Promise<HTMLElement> {
  const heading = await screen.findByRole('heading', { name: 'Quota' });
  return heading.closest('[data-slot="card"]') as HTMLElement;
}

/** Everything shown under one ceiling's label: its figure and where it came from. */
function reading(card: HTMLElement, label: string): HTMLElement {
  return within(card).getByText(label).parentElement as HTMLElement;
}

/** The one element whose whole text is `text`, such as a count with its word. */
function phrase(card: HTMLElement, text: string): HTMLElement {
  return within(card).getByText((_, element) => element?.textContent === text);
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

describe('the quota card', () => {
  it('shows each ceiling with what is held against it, and where the ceiling came from', async () => {
    renderPage();
    const card = await quotaCard();
    await within(card).findByText('5 / 5');

    const sandboxes = reading(card, 'Sandboxes');
    expect(within(sandboxes).getByText('5 / 5')).toBeTruthy();
    expect(within(sandboxes).getByText('Configured')).toBeTruthy();
    // Five sandboxes of 1Gi + 128Mi against the ceiling derived from host
    // memory; how it was derived is the quota's own account, quoted as given.
    const memory = reading(card, 'Memory');
    expect(within(memory).getByText('5.6 Gi / 11.6 Gi')).toBeTruthy();
    expect(
      within(memory).getByText('derived: host memory 15.6Gi less 4Gi for the platform and the host'),
    ).toBeTruthy();
    const creates = reading(card, 'Concurrent creates');
    expect(within(creates).getByText('1 / 2')).toBeTruthy();
    expect(within(creates).getByText('Configured')).toBeTruthy();
    expect(
      within(card).getByText(`As of ${formatDateTime('2026-10-10T09:30:00+00:00')}`),
    ).toBeTruthy();
  });

  it('reads a ceiling that is not set as no limit', async () => {
    renderPage();
    const card = await quotaCard();
    await within(card).findByText('CPU');

    const cpu = reading(card, 'CPU');
    expect(within(cpu).getByText('10 · no limit')).toBeTruthy();
    expect(within(cpu).getByText('Not set')).toBeTruthy();
    // The wire's null is the absence of a ceiling, not a value to print.
    expect(within(card).queryByText(/null/)).toBeNull();
  });

  it('counts the sandboxes by what they are doing', async () => {
    const base = quotaBackend();
    quotaRead = async () => ({
      backends: [
        quotaBackend({
          boxes: [
            ...base.boxes,
            { sandbox_id: 'sbx-conv-0005', ...CHARGE, state: 'Stopping', session_id: 'sess-5' },
          ],
        }),
      ],
    });
    renderPage();
    const card = await quotaCard();
    await within(card).findByText('In use');

    // Two conversations hold a sandbox each and the unclaimed pool member is
    // prepared. Of the two reservations only the create still in flight is
    // starting: the other already has a sandbox, which the next inventory will
    // list. The sandbox the backend is stopping is cleaning up, not in use.
    expect(phrase(card, 'In use 2')).toBeTruthy();
    expect(phrase(card, 'Prepared 1')).toBeTruthy();
    expect(phrase(card, 'Starting 1')).toBeTruthy();
    expect(phrase(card, 'Cleaning up 1')).toBeTruthy();
  });

  it('marks the ceiling the sandboxes have reached, and only that one', async () => {
    renderPage();
    const card = await quotaCard();

    // At 5 of 5 the quota refuses the next sandbox, which is why a new
    // conversation fails. The figure says so in colour, with a glyph that
    // survives a greyscale screen, and in words for a screen reader.
    const full = await within(card).findByText('5 / 5');
    expect(full.classList.contains('text-crimson-fg')).toBe(true);
    expect(full.querySelector('svg')).not.toBeNull();
    expect(full.textContent).toContain('at the limit');

    for (const [label, figure] of [
      ['Memory', '5.6 Gi / 11.6 Gi'],
      ['CPU', '10 · no limit'],
      ['Concurrent creates', '1 / 2'],
    ]) {
      const shown = within(reading(card, label)).getByText(figure);
      expect(shown.classList.contains('text-crimson-fg')).toBe(false);
      expect(shown.querySelector('svg')).toBeNull();
      expect(shown.textContent).not.toContain('at the limit');
    }
  });

  it('says the quota could not be read instead of showing zeros', async () => {
    quotaRead = async () => {
      throw new Error("sandbox quota ledger for 'open_sandbox' stayed contended");
    };
    renderPage();
    const card = await quotaCard();

    await within(card).findByText("Couldn't load the quota");
    expect(within(card).getByText(/stayed contended/)).toBeTruthy();
    expect(within(card).queryByText(/\d \/ \d/)).toBeNull();
    expect(within(card).queryByText('In use')).toBeNull();
    // The inventory is a read of its own, and still answers.
    expect(await screen.findByText('CrashLoopBackOff')).toBeTruthy();
  });

  it('does not count sandboxes before the backend has been counted', async () => {
    // A ledger that no inventory has filled holds only its reservations, so its
    // usage leaves out every sandbox already running.
    quotaRead = async () => ({
      backends: [
        quotaBackend({
          usage: { boxes: 2, memory_bytes: 2 * CHARGE.memory, cpu: 2 * CHARGE.cpu, creating: 1 },
          inventory_at: null,
          boxes: [],
        }),
      ],
    });
    renderPage();
    const card = await quotaCard();

    await within(card).findByText(
      "The backend's sandboxes have not been counted yet, so these figures leave out any already running.",
    );
    expect(phrase(card, 'In use —')).toBeTruthy();
    expect(phrase(card, 'Prepared —')).toBeTruthy();
    expect(phrase(card, 'Starting 1')).toBeTruthy();
    expect(phrase(card, 'Cleaning up —')).toBeTruthy();
    expect(within(card).queryByText(/^As of/)).toBeNull();
  });
});
