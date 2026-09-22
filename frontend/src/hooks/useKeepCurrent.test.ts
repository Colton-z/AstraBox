// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { FOLLOW_INTERVAL_MS, RETURN_THROTTLE_MS, useKeepCurrent } from './useKeepCurrent';

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  document.dispatchEvent(new Event('visibilitychange'));
}

beforeEach(() => {
  vi.useFakeTimers();
  setVisibility('visible');
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('useKeepCurrent', () => {
  it('re-reads when the tab comes back after the throttle window', async () => {
    const reload = vi.fn(async () => {});
    renderHook(() => useKeepCurrent(reload));
    expect(reload).not.toHaveBeenCalled();

    // A focus delivered while the mount read is still fresh is the same read.
    await act(async () => { window.dispatchEvent(new Event('focus')); });
    expect(reload).not.toHaveBeenCalled();

    vi.advanceTimersByTime(RETURN_THROTTLE_MS);
    await act(async () => { setVisibility('hidden'); setVisibility('visible'); });
    expect(reload).toHaveBeenCalledTimes(1);

    // Focus right after the visibility re-read is the same return.
    await act(async () => { window.dispatchEvent(new Event('focus')); });
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('does not re-read while the tab is hidden', async () => {
    const reload = vi.fn(async () => {});
    renderHook(() => useKeepCurrent(reload, { follow: true }));
    await act(async () => { setVisibility('hidden'); });
    await act(async () => { vi.advanceTimersByTime(RETURN_THROTTLE_MS + FOLLOW_INTERVAL_MS * 3); });
    // A focus delivered to a hidden document (devtools, a second monitor's
    // window manager) is not the reader coming back.
    await act(async () => { window.dispatchEvent(new Event('focus')); });
    expect(reload).not.toHaveBeenCalled();
  });

  it('follows a record in motion on a fixed cadence, without overlapping reads', async () => {
    let settle: () => void = () => {};
    const reload = vi.fn(() => new Promise<void>((resolve) => { settle = resolve; }));
    const { rerender } = renderHook(({ follow }) => useKeepCurrent(reload, { follow }), {
      initialProps: { follow: true },
    });

    await act(async () => { vi.advanceTimersByTime(FOLLOW_INTERVAL_MS); });
    expect(reload).toHaveBeenCalledTimes(1);

    // The first read has not answered: the next tick must not start a second.
    await act(async () => { vi.advanceTimersByTime(FOLLOW_INTERVAL_MS); });
    expect(reload).toHaveBeenCalledTimes(1);

    await act(async () => { settle(); });
    await act(async () => { vi.advanceTimersByTime(FOLLOW_INTERVAL_MS); });
    expect(reload).toHaveBeenCalledTimes(2);

    // Once the record has settled, the page stops asking.
    await act(async () => { settle(); });
    rerender({ follow: false });
    await act(async () => { vi.advanceTimersByTime(FOLLOW_INTERVAL_MS * 3); });
    expect(reload).toHaveBeenCalledTimes(2);
  });

  it('calls the latest reload the page rendered with', async () => {
    const first = vi.fn(async () => {});
    const second = vi.fn(async () => {});
    const { rerender } = renderHook(({ reload }) => useKeepCurrent(reload), {
      initialProps: { reload: first },
    });
    rerender({ reload: second });
    vi.advanceTimersByTime(RETURN_THROTTLE_MS);
    await act(async () => { setVisibility('hidden'); setVisibility('visible'); });
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});
