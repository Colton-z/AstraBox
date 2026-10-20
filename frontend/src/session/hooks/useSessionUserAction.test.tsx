// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { getSessionUserAction, performSessionUserAction } from '../../api';
import { useSessionUserAction } from './useSessionUserAction';

vi.mock('../../api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../api')>(),
  getSessionUserAction: vi.fn(), performSessionUserAction: vi.fn(),
}));
const blocked = { applicable: true, ready: false, user_action: { message: 'Approve access', status: 'pending', url: null }, actions: [{ id: 'check', label: 'Check consent' }] };
const ready = { applicable: true, ready: true, user_action: null, actions: [] };
beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

it('blocks until a read completes, requires an explicit operation, and prevents double submission', async () => {
  vi.mocked(getSessionUserAction).mockResolvedValue(blocked);
  let finish!: (value: typeof ready) => void;
  vi.mocked(performSessionUserAction).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const { result } = renderHook(() => useSessionUserAction('session', 'owner', '', true));
  expect(result.current.ready).toBe(false);
  await waitFor(() => expect(result.current.state).toEqual(blocked));
  expect(performSessionUserAction).not.toHaveBeenCalled();
  act(() => { result.current.perform('check'); result.current.perform('check'); });
  expect(performSessionUserAction).toHaveBeenCalledTimes(1);
  expect(result.current.ready).toBe(false);
  await act(async () => finish(ready));
  expect(result.current.ready).toBe(true);
});

it('keeps a failed query blocked and explicitly refreshes it', async () => {
  vi.mocked(getSessionUserAction).mockRejectedValueOnce(new Error('Unavailable')).mockResolvedValueOnce(ready);
  const { result } = renderHook(() => useSessionUserAction('session', 'owner', '', true));
  await waitFor(() => expect(result.current.error).toContain('Unavailable'));
  expect(result.current.ready).toBe(false);
  act(() => result.current.refresh());
  await waitFor(() => expect(result.current.ready).toBe(true));
});

it('does not apply an earlier session response after navigation', async () => {
  let finish!: (value: typeof ready) => void;
  vi.mocked(getSessionUserAction).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; })).mockResolvedValueOnce(blocked);
  const { result, rerender } = renderHook(({ id }) => useSessionUserAction(id, 'owner', '', true), { initialProps: { id: 'one' } });
  rerender({ id: 'two' });
  await waitFor(() => expect(result.current.state).toEqual(blocked));
  await act(async () => finish(ready));
  expect(result.current.ready).toBe(false);
  expect(result.current.state).toEqual(blocked);
});
