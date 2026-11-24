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
const serving = { starting: false, serving: true, sandboxId: 'box-1' };
beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

it('blocks until a read completes, requires an explicit operation, and prevents double submission', async () => {
  vi.mocked(getSessionUserAction).mockResolvedValue(blocked);
  let finish!: (value: typeof ready) => void;
  vi.mocked(performSessionUserAction).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const { result } = renderHook(() => useSessionUserAction('session', 'owner', '', true, serving));
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
  const { result } = renderHook(() => useSessionUserAction('session', 'owner', '', true, serving));
  await waitFor(() => expect(result.current.error).toContain('Unavailable'));
  expect(result.current.ready).toBe(false);
  act(() => result.current.refresh());
  await waitFor(() => expect(result.current.ready).toBe(true));
});

it('does not apply an earlier session response after navigation', async () => {
  let finish!: (value: typeof ready) => void;
  vi.mocked(getSessionUserAction).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; })).mockResolvedValueOnce(blocked);
  const { result, rerender } = renderHook(({ id }) => useSessionUserAction(id, 'owner', '', true, serving), { initialProps: { id: 'one' } });
  rerender({ id: 'two' });
  await waitFor(() => expect(result.current.state).toEqual(blocked));
  await act(async () => finish(ready));
  expect(result.current.ready).toBe(false);
  expect(result.current.state).toEqual(blocked);
});

it('takes a start it watched bind a serving box as met, and reads after any other start', async () => {
  // A read after the start would hold the send button; it never answers here.
  vi.mocked(getSessionUserAction)
    .mockResolvedValueOnce(ready)
    .mockResolvedValueOnce(ready)
    .mockResolvedValueOnce(blocked)
    .mockReturnValue(new Promise(() => {}));
  const { result, rerender } = renderHook(
    ({ invalidation, runtime }) => useSessionUserAction('session', 'owner', invalidation, true, runtime),
    { initialProps: { invalidation: 'creating', runtime: { starting: true, serving: false, sandboxId: null as string | null } } },
  );
  await waitFor(() => expect(result.current.ready).toBe(true));

  // The startup checked the owner's prerequisites before it took the box.
  rerender({ invalidation: 'box-1', runtime: serving });
  expect(result.current.ready).toBe(true);
  expect(getSessionUserAction).toHaveBeenCalledTimes(1);

  // A replacement start that stops on the owner's action leaves no serving
  // runtime: it is read again, and its answer blocks sending.
  rerender({ invalidation: 'replacing', runtime: { starting: true, serving: false, sandboxId: null } });
  await waitFor(() => expect(getSessionUserAction).toHaveBeenCalledTimes(2));
  rerender({ invalidation: 'unavailable', runtime: { starting: false, serving: false, sandboxId: null } });
  await waitFor(() => expect(result.current.state).toEqual(blocked));
  expect(result.current.ready).toBe(false);
  expect(getSessionUserAction).toHaveBeenCalledTimes(3);
});
