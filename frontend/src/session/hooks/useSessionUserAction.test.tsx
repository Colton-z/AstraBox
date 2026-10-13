// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { getSessionUserAction, performSessionUserAction } from '../../api';
import { useSessionUserAction } from './useSessionUserAction';
import { useMessageQueue } from './useMessageQueue';
import type { OutboxItem } from '../../types';

vi.mock('../../api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../api')>(),
  getSessionUserAction: vi.fn(), performSessionUserAction: vi.fn(),
}));
const blocked = { ready: false, user_action: { message: 'Approve access', status: 'pending', url: null }, actions: [{ id: 'check', label: 'Check consent' }] };
const ready = { ready: true, user_action: null, actions: [] };
beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

it('blocks until a read completes, requires an explicit operation, and prevents double submission', async () => {
  vi.mocked(getSessionUserAction).mockResolvedValue(blocked);
  let finish!: (value: typeof ready) => void;
  vi.mocked(performSessionUserAction).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const { result } = renderHook(() => useSessionUserAction('session', 'owner', ''));
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
  const { result } = renderHook(() => useSessionUserAction('session', 'owner', ''));
  await waitFor(() => expect(result.current.error).toContain('Unavailable'));
  expect(result.current.ready).toBe(false);
  act(() => result.current.refresh());
  await waitFor(() => expect(result.current.ready).toBe(true));
});

it('does not apply an earlier session response after navigation', async () => {
  let finish!: (value: typeof ready) => void;
  vi.mocked(getSessionUserAction).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; })).mockResolvedValueOnce(blocked);
  const { result, rerender } = renderHook(({ id }) => useSessionUserAction(id, 'owner', ''), { initialProps: { id: 'one' } });
  rerender({ id: 'two' });
  await waitFor(() => expect(result.current.state).toEqual(blocked));
  await act(async () => finish(ready));
  expect(result.current.ready).toBe(false);
  expect(result.current.state).toEqual(blocked);
});

it('keeps the original content and retries a settled refusal once under a new identity', () => {
  const images: OutboxItem['images'] = [{ type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'aW1hZ2U=' } }];
  const item: OutboxItem = { client_message_id: 'old', text: 'preserved', images, status: 'failed', resend_as_new: true };
  const send = vi.fn();
  const { result, rerender } = renderHook(({ allowed }) => useMessageQueue({ outbox: [item], setOutbox: vi.fn(), canSendNow: allowed, canQueueMessage: false, sendClientMessageNow: send }), { initialProps: { allowed: false } });
  act(() => result.current.retryQueueItem('old'));
  expect(send).not.toHaveBeenCalled();
  rerender({ allowed: true });
  act(() => { result.current.retryQueueItem('old'); result.current.retryQueueItem('old'); });
  expect(send).toHaveBeenCalledTimes(1);
  expect(send.mock.calls[0][0]).not.toBe('old');
  expect(send.mock.calls[0].slice(1)).toEqual(['preserved', images]);
});
