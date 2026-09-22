// @vitest-environment jsdom
import { cleanup, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { useSessionRefreshEffects } from './useSessionRefreshEffects';

afterEach(cleanup);

type Props = { isStreaming: boolean; isSubmitted: boolean; lifecycleState: string };

function renderNonce(initial: Props) {
  const handleRehydrate = async () => {};
  return renderHook(
    (props: Props) => useSessionRefreshEffects({ ...props, handleRehydrate }).filesRefreshNonce,
    { initialProps: initial },
  );
}

const idle: Props = { isStreaming: false, isSubmitted: false, lifecycleState: 'ready' };

describe('useSessionRefreshEffects files nonce', () => {
  it('refetches once when a turn ends', () => {
    const { result, rerender } = renderNonce(idle);
    expect(result.current).toBe(0);
    rerender({ ...idle, isSubmitted: true, lifecycleState: 'busy' });
    rerender({ ...idle, isStreaming: true, lifecycleState: 'busy' });
    expect(result.current).toBe(0);
    rerender(idle);
    expect(result.current).toBe(1);
  });

  it('refetches when a background child finishes while nobody types', () => {
    const { result, rerender } = renderNonce(idle);
    rerender({ ...idle, lifecycleState: 'background' });
    expect(result.current).toBe(0);
    rerender(idle);
    expect(result.current).toBe(1);
  });

  it('waits for the turn that answers a finished child before refetching', () => {
    const { result, rerender } = renderNonce(idle);
    rerender({ ...idle, lifecycleState: 'background' });
    rerender({ ...idle, isStreaming: true, lifecycleState: 'busy' });
    expect(result.current).toBe(0);
    rerender(idle);
    expect(result.current).toBe(1);
  });

  it('does not refetch while nothing has been working', () => {
    const { result, rerender } = renderNonce(idle);
    rerender({ ...idle, lifecycleState: 'error' });
    rerender(idle);
    expect(result.current).toBe(0);
  });
});
