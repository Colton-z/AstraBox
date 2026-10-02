// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { AgentPreparedRuntimeStatus } from '@/types';
import { getAgentPreparedRuntime, refreshAgentPreparedRuntime } from '@/api';
import { AgentPrewarmStatus } from './AgentPrewarmStatus';

vi.mock('@/api', () => ({
  getAgentPreparedRuntime: vi.fn(),
  refreshAgentPreparedRuntime: vi.fn(),
}));

const prepared: AgentPreparedRuntimeStatus = {
  enabled: true, ready: true, prepared_count: 1, state: 'prepared',
  sandbox_id: 'box-prepared-123', prepared_at: '2026-09-28T09:00:00Z',
};

beforeAll(async () => { await i18n.changeLanguage('en'); });
beforeEach(() => {
  vi.mocked(getAgentPreparedRuntime).mockReset().mockResolvedValue(prepared);
  vi.mocked(refreshAgentPreparedRuntime).mockReset();
});
afterEach(cleanup);

function show(dirty = false) {
  return render(<AgentPrewarmStatus agentId="agent-1" savedAt="saved-1" enabled dirty={dirty} />);
}

describe('Agent warm capacity', () => {
  it('identifies the actual waiting box and the time its slot was prepared', async () => {
    const { container } = show();
    expect(await screen.findByText(/^The next conversation starts warm/)).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Warm capacity' })).toBeTruthy();
    expect(screen.getByText('box-prepared-123')).toBeTruthy();
    expect(screen.getByText('Available: 1')).toBeTruthy();
    expect(container.querySelector('time')?.getAttribute('datetime')).toBe(prepared.prepared_at);
    expect(getAgentPreparedRuntime).toHaveBeenCalledWith('agent-1');
  });

  it.each([
    [{ enabled: false, ready: false, prepared_count: 0 }, /^Warm start is off for this Agent/],
    [{ ready: false, prepared_count: 0, state: 'preparing' }, /^No sandbox is standing by/],
    [{ ready: false, prepared_count: 0, state: 'expired' }, /^The prepared sandbox has expired/],
    [{ ready: false, prepared_count: 0, last_error: 'Preparation refused' }, /^AstraBox could not prepare a sandbox/],
  ])('does not promise a warm start for unavailable capacity %j', async (status, reading) => {
    vi.mocked(getAgentPreparedRuntime).mockResolvedValue({ ...prepared, ...status });
    show();
    expect(await screen.findByText(reading)).toBeTruthy();
    expect(screen.queryByText(/^The next conversation starts warm/)).toBeNull();
    expect(screen.getByText('Available: 0')).toBeTruthy();
  });

  it('refreshes the observation without requesting replacement capacity', async () => {
    show();
    await screen.findByText(/^The next conversation starts warm/);
    vi.mocked(getAgentPreparedRuntime).mockResolvedValue({
      ...prepared, ready: false, prepared_count: 0, state: 'expired',
    });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByText(/^The prepared sandbox has expired/)).toBeTruthy();
    expect(getAgentPreparedRuntime).toHaveBeenCalledTimes(2);
    expect(refreshAgentPreparedRuntime).not.toHaveBeenCalled();
  });

  it('keeps replacement unavailable until edits are saved', async () => {
    show(true);
    await screen.findByText(/^The next conversation starts warm/);
    const button = screen.getByRole('button', { name: 'Reprepare' });
    expect(button.hasAttribute('disabled')).toBe(true);
    fireEvent.click(button);
    expect(refreshAgentPreparedRuntime).not.toHaveBeenCalled();
  });

  it('reports a failed explicit observation and can be re-asked', async () => {
    vi.mocked(getAgentPreparedRuntime).mockRejectedValueOnce(new Error('Capacity read failed'));
    show();
    expect(await screen.findByText('Capacity read failed')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText(/^The next conversation starts warm/);
    await waitFor(() => expect(screen.queryByText('Capacity read failed')).toBeNull());
  });
});
