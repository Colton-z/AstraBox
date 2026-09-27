// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { listSessionChildRuns } from '../../api';
import { useSubagentDrawer } from './useSubagentDrawer';

vi.mock('../../api', () => ({
  listSessionChildRuns: vi.fn(),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const rootChild = {
  child_run_id: 'root-child',
  engine_kind: 'claude_code',
  depth: 1,
  engine_event: 'task.updated',
  engine_status: 'paused',
  closed: false,
  active: false,
  operations: ['stop'],
  tool_call_ids: [],
};

describe('useSubagentDrawer', () => {
  it('uses the backend tree order and depth without rebuilding provider semantics', async () => {
    vi.mocked(listSessionChildRuns).mockResolvedValue({
      session_id: 'session-1',
      child_runs: [
        rootChild,
        {
          child_run_id: 'nested-child',
          parent_child_run_id: 'root-child',
          engine_kind: 'claude_code',
          depth: 2,
          engine_event: 'task.notification',
          engine_status: 'killed',
          closed: true,
          active: false,
          operations: [],
          tool_call_ids: [],
        },
      ],
    });
    const { result } = renderHook(() => useSubagentDrawer({
      sessionId: 'session-1',
      childRunRevision: 0,
      rightPanelCaps: { tabs: ['agents'], defaultTab: 'agents' },
      setRightTab: vi.fn(),
      lifecycleState: 'terminated',
      isSubmitted: false,
      isStreaming: false,
      hasPendingInteraction: false,
    }));

    await waitFor(() => expect(result.current.subagentRegistry.totalCount).toBe(2));
    expect(result.current.subagentRegistry.agents.map((entry) => ({
      id: entry.childRunId,
      parent: entry.parentChildRunId,
      depth: entry.depth,
      status: entry.engineStatus,
    }))).toEqual([
      { id: 'root-child', parent: undefined, depth: 1, status: 'paused' },
      { id: 'nested-child', parent: 'root-child', depth: 2, status: 'killed' },
    ]);
  });

  it('coalesces a burst of stream invalidations into one projection read', async () => {
    vi.mocked(listSessionChildRuns).mockResolvedValue({
      session_id: 'session-1',
      child_runs: [rootChild],
    });
    const { rerender } = renderHook(
      ({ revision }: { revision: number }) => useSubagentDrawer({
        sessionId: 'session-1',
        childRunRevision: revision,
        rightPanelCaps: { tabs: ['agents'], defaultTab: 'agents' },
        setRightTab: vi.fn(),
        lifecycleState: 'terminated',
        isSubmitted: false,
        isStreaming: false,
        hasPendingInteraction: false,
      }),
      { initialProps: { revision: 0 } },
    );

    rerender({ revision: 1 });
    rerender({ revision: 2 });
    rerender({ revision: 3 });

    await waitFor(() => expect(listSessionChildRuns).toHaveBeenCalledTimes(1));
  });

  it('reads the settled projection once when background work becomes ready', async () => {
    // A background Session also polls the projection, first 1 s after mount.
    // On a wall clock a slow run reaches that tick before the ready boundary
    // and reads once more than this test counts, so the clock is the test's:
    // the only reads are the mount read and the one the boundary causes.
    vi.useFakeTimers();
    try {
      vi.mocked(listSessionChildRuns)
        .mockResolvedValueOnce({
          session_id: 'session-1',
          child_runs: [
            { ...rootChild, engine_status: 'completed', closed: true, operations: [] },
            {
              ...rootChild,
              child_run_id: 'nested-child',
              engine_status: 'completed',
              closed: true,
              operations: [],
            },
          ],
        })
        .mockResolvedValue({
          session_id: 'session-1',
          child_runs: [
            { ...rootChild, engine_status: 'completed', closed: true, operations: [] },
            {
              ...rootChild,
              child_run_id: 'nested-child',
              parent_child_run_id: 'root-child',
              depth: 2,
              engine_status: 'completed',
              closed: true,
              operations: [],
            },
          ],
        });
      const { result, rerender } = renderHook(
        ({ lifecycleState }: { lifecycleState: string }) => useSubagentDrawer({
          sessionId: 'session-1',
          childRunRevision: 0,
          rightPanelCaps: { tabs: ['agents'], defaultTab: 'agents' },
          setRightTab: vi.fn(),
          lifecycleState,
          isSubmitted: false,
          isStreaming: false,
          hasPendingInteraction: false,
        }),
        { initialProps: { lifecycleState: 'background' } },
      );

      // The mount read is debounced by 100 ms; act() settles its reply.
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      expect(listSessionChildRuns).toHaveBeenCalledTimes(1);
      expect(result.current.subagentRegistry.getAgent('nested-child')?.depth).toBe(1);

      rerender({ lifecycleState: 'ready' });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(listSessionChildRuns).toHaveBeenCalledTimes(2);
      expect(result.current.subagentRegistry.getAgent('nested-child')?.depth).toBe(2);

      // Once ready with nothing live, neither a repeat render nor time reads again.
      rerender({ lifecycleState: 'ready' });
      await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
      expect(listSessionChildRuns).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
