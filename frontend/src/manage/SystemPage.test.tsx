// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { AdminProcessHealth, AdminSystemOverview } from '@/types';

const overview: AdminSystemOverview = {
  machine_id: 'machine-1',
  server_env: 'test',
  total_sessions: 0,
};
const health: AdminProcessHealth = {
  severity: 'ok',
  machine_id: 'machine-1',
  pid: 42,
  threads: {
    count: 1,
    non_daemon_count: 0,
    items: [{ name: 'AnyIO worker thread', daemon: true, alive: true, ident: 17, native_id: 23 }],
  },
  runtimes: { count: 0, items: [] },
};

const api = vi.hoisted(() => ({
  adminSystemOverview: vi.fn(),
  adminProcessHealth: vi.fn(),
}));

vi.mock('@/api', () => api);

const { default: SystemPage } = await import('./SystemPage');

beforeEach(() => {
  api.adminSystemOverview.mockReset().mockResolvedValue(overview);
  api.adminProcessHealth.mockReset().mockResolvedValue(health);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
beforeAll(async () => {
  await i18n.changeLanguage('en');
});

describe('SystemPage runtime names', () => {
  it('marks a thread name as runtime-provided verbatim text', async () => {
    const { container } = render(
      <MemoryRouter>
        <SystemPage />
      </MemoryRouter>,
    );

    await screen.findByText('AnyIO worker thread');
    const quoted = container.querySelector('[data-slot="verbatim"]');
    expect(quoted).not.toBeNull();
    expect(quoted?.textContent).toBe('AnyIO worker thread');
  });

  it('waits a full interval after a poll settles before starting another', async () => {
    vi.useFakeTimers();
    let releaseOverview!: (value: AdminSystemOverview) => void;
    let releaseHealth!: (value: AdminProcessHealth) => void;
    api.adminSystemOverview
      .mockResolvedValueOnce(overview)
      .mockImplementationOnce(() => new Promise((resolve) => { releaseOverview = resolve; }));
    api.adminProcessHealth
      .mockResolvedValueOnce(health)
      .mockImplementationOnce(() => new Promise((resolve) => { releaseHealth = resolve; }));

    render(
      <MemoryRouter>
        <SystemPage />
      </MemoryRouter>,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(api.adminSystemOverview).toHaveBeenCalledTimes(1);
    expect(api.adminProcessHealth).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(api.adminSystemOverview).toHaveBeenCalledTimes(2);
    expect(api.adminProcessHealth).toHaveBeenCalledTimes(2);

    // A slow read owns the polling lane: elapsed wall time alone cannot start
    // another request while this one is unresolved.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(api.adminSystemOverview).toHaveBeenCalledTimes(2);
    expect(api.adminProcessHealth).toHaveBeenCalledTimes(2);

    await act(async () => {
      releaseOverview(overview);
      releaseHealth(health);
      await Promise.resolve();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9_999);
    });
    expect(api.adminSystemOverview).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(api.adminSystemOverview).toHaveBeenCalledTimes(3);
    expect(api.adminProcessHealth).toHaveBeenCalledTimes(3);
  });
});
