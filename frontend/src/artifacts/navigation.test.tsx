// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, renderHook, act } from '@testing-library/react';
import { useArtifactPanelNavigation } from './useArtifactPanelNavigation';
import type { UIMessage } from 'ai';
afterEach(cleanup);
it('opens each new live marker once and leaves replayed history alone', () => {
  const marker = {
    id: `art_${'a'.repeat(24)}`,
    version: 1,
    title: 'Page',
    kind: 'html',
  } as const;
  const history = [
    {
      id: 'history',
      role: 'assistant',
      parts: [
        {
          type: 'dynamic-tool',
          toolCallId: 'old',
          toolName: 'arbitrary-mcp-name',
          state: 'output-available',
          output: JSON.stringify({ astrabox_artifact: marker }),
        },
      ],
    },
  ] as UIMessage[];
  const show = vi.fn();
  const { result } = renderHook(() =>
    useArtifactPanelNavigation(history, show),
  );
  act(() => result.current.appeared(marker, 'old'));
  expect(show).not.toHaveBeenCalled();
  act(() => result.current.appeared({ ...marker, version: 2 }, 'new'));
  expect(show).toHaveBeenCalledTimes(1);
  expect(result.current.selectedId).toBe(marker.id);
  expect(result.current.revision).toBe(1);
  act(() => result.current.appeared({ ...marker, version: 2 }, 'new'));
  expect(show).toHaveBeenCalledTimes(1);
});
