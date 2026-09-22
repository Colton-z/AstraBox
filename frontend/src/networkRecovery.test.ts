import { afterEach, describe, expect, it, vi } from 'vitest';

import { fetchWithNetworkRecovery } from './networkRecovery';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('fetchWithNetworkRecovery', () => {
  it('retries a safe read after a transport rejection', async () => {
    const response = new Response('ok', { status: 200 });
    const mockFetch = vi.fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(response);
    vi.stubGlobal('fetch', mockFetch);

    await expect(fetchWithNetworkRecovery('/api/v1/agents')).resolves.toBe(response);
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it('never replays a mutation without an operation-specific idempotency contract', async () => {
    const failure = new TypeError('Failed to fetch');
    const mockFetch = vi.fn().mockRejectedValue(failure);
    vi.stubGlobal('fetch', mockFetch);

    await expect(fetchWithNetworkRecovery('/api/v1/agents', { method: 'POST' })).rejects.toBe(failure);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it('replays a mutation only when its API is explicitly convergent', async () => {
    const response = new Response('ok', { status: 200 });
    const mockFetch = vi.fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(response);
    vi.stubGlobal('fetch', mockFetch);

    await expect(fetchWithNetworkRecovery(
      '/api/v1/sessions/session-1/files/delete',
      { method: 'POST' },
      { mutationReplay: 'convergent' },
    )).resolves.toBe(response);
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it('backs off across a longer route change for an idempotency-keyed mutation', async () => {
    vi.useFakeTimers();
    const response = new Response('ok', { status: 200 });
    const mockFetch = vi.fn()
      .mockRejectedValueOnce(new TypeError('network changed'))
      .mockRejectedValueOnce(new TypeError('network changed'))
      .mockRejectedValueOnce(new TypeError('network changed'))
      .mockResolvedValueOnce(response);
    vi.stubGlobal('fetch', mockFetch);

    const recovered = fetchWithNetworkRecovery(
      '/api/v1/sessions/session-1/turn-inputs',
      { method: 'POST' },
      { mutationReplay: 'idempotent' },
    );
    await vi.runAllTimersAsync();
    await expect(recovered).resolves.toBe(response);
    expect(mockFetch).toHaveBeenCalledTimes(4);
  });

  it('stops safe-read recovery immediately when the caller aborts', async () => {
    const controller = new AbortController();
    const mockFetch = vi.fn().mockImplementation(async () => {
      controller.abort();
      throw new TypeError('Failed to fetch');
    });
    vi.stubGlobal('fetch', mockFetch);

    await expect(
      fetchWithNetworkRecovery('/api/v1/agents', { signal: controller.signal }),
    ).rejects.toMatchObject({ name: 'AbortError' });
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
});
