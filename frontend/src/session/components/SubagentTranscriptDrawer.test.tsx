// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import { getSessionChildRunMessages } from '../../api';
import i18n from '../../i18n';
import type { SubagentEntry } from '../hooks/useSubagentRegistry';
import { SubagentTranscriptDrawer } from './SubagentTranscriptDrawer';

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>();
  return { ...actual, getSessionChildRunMessages: vi.fn() };
});

beforeEach(async () => {
  await i18n.changeLanguage('en');
  vi.mocked(getSessionChildRunMessages).mockResolvedValue({
    session_id: 'session-1',
    child_run_id: 'child-1',
    messages: [],
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const activeChild: SubagentEntry = {
  childRunId: 'child-1',
  engineKind: 'hermes',
  depth: 1,
  engineEvent: 'subagent.progress',
  engineStatus: 'stopped',
  closed: false,
  active: true,
  operations: ['stop'],
  toolCallIds: [],
};

describe('SubagentTranscriptDrawer', () => {
  it('keeps roles and shared content rendering while a later result updates the same tool card', async () => {
    const messages = [
      { role: 'user' as const, message_id: 'user-task', content: [{ type: 'text', text: 'Child task' }] },
      { role: 'assistant' as const, message_id: 'assistant-call', content: [
        { type: 'thinking', thinking: 'Native reasoning' },
        { type: 'tool_use', id: 'same-id', name: 'native/tool', input: { command: 'native input' } },
      ] },
    ];
    vi.mocked(getSessionChildRunMessages).mockResolvedValue({
      session_id: 'session-1', child_run_id: 'child-1', messages,
    });
    const props = {
      sessionId: 'session-1', refreshRevision: 0, backgroundTasksPending: false,
      childRunId: 'child-1', entry: activeChild, onClose: () => {},
    };
    const { rerender } = render(<SubagentTranscriptDrawer {...props} />);
    const header = await screen.findByRole('button', { name: 'native/tool Working' });
    const column = screen.getByTestId('subagent-transcript-column');
    expect(column.querySelector('[data-role="user"]')?.textContent).toContain('Child task');
    fireEvent.click(within(screen.getByTestId('reasoning-part')).getByRole('button'));
    expect(screen.getByText('Native reasoning')).toBeTruthy();
    fireEvent.click(header);
    expect(column.textContent).toContain('native input');

    vi.mocked(getSessionChildRunMessages).mockResolvedValue({
      session_id: 'session-1', child_run_id: 'child-1', messages: [...messages,
        { role: 'user', message_id: 'later-result', content: [
          { type: 'tool_result', tool_use_id: 'same-id', content: 'native output', is_error: false },
        ] },
        { role: 'assistant', message_id: 'answer', content: [{ type: 'text', text: 'Child answer' }] },
      ],
    });
    rerender(<SubagentTranscriptDrawer {...props} refreshRevision={1} />);
    const settled = await screen.findByRole('button', { name: 'native/tool Done' });
    expect(settled).toBe(header);
    expect(screen.getAllByRole('button', { name: /^native\/tool / })).toHaveLength(1);
    expect(column.textContent).toContain('native output');
    expect(screen.getByTestId('assistant-text').textContent).toBe('Child answer');

    // A sibling may use the same supplier-local id. Its transcript starts
    // without a result and must not inherit the first child's output.
    vi.mocked(getSessionChildRunMessages).mockResolvedValue({
      session_id: 'session-1', child_run_id: 'child-2', messages,
    });
    rerender(<SubagentTranscriptDrawer {...props} childRunId="child-2"
      entry={{ ...activeChild, childRunId: 'child-2' }} />);
    await screen.findByRole('button', { name: 'native/tool Working' });
    expect(column.textContent).not.toContain('native output');
    expect(screen.queryByRole('button', { name: 'native/tool Done' })).toBeNull();
  });

  it('keeps stop failures visible and makes the control usable again', async () => {
    const onStopChildRun = vi.fn().mockRejectedValue(new Error('engine refused stop'));
    render(
      <SubagentTranscriptDrawer
        sessionId="session-1"
        refreshRevision={0}
        backgroundTasksPending={false}
        childRunId="child-1"
        entry={activeChild}
        onClose={() => {}}
        onStopChildRun={onStopChildRun}
      />,
    );

    const stop = screen.getByTestId('subagent-stop-button') as HTMLButtonElement;
    fireEvent.click(stop);

    await waitFor(() => expect(stop.disabled).toBe(false));
    expect(screen.getByText(/Could not stop this subagent: engine refused stop/)).toBeTruthy();
    expect(onStopChildRun).toHaveBeenCalledWith('child-1');
  });

  it('disables the stop control while the adapter request is pending', async () => {
    let resolveStop: (() => void) | undefined;
    const onStopChildRun = vi.fn(() => new Promise<void>((resolve) => {
      resolveStop = resolve;
    }));
    render(
      <SubagentTranscriptDrawer
        sessionId="session-1"
        refreshRevision={0}
        backgroundTasksPending={false}
        childRunId="child-1"
        entry={activeChild}
        onClose={() => {}}
        onStopChildRun={onStopChildRun}
      />,
    );

    const stop = screen.getByTestId('subagent-stop-button') as HTMLButtonElement;
    fireEvent.click(stop);
    await waitFor(() => expect(stop.disabled).toBe(true));
    expect(stop.textContent).toBe('Stopping…');

    resolveStop?.();
    await waitFor(() => expect(stop.disabled).toBe(false));
  });

  it('shows stop from the operation capability even when the vendor status says stopped', () => {
    render(
      <SubagentTranscriptDrawer
        sessionId="session-1"
        refreshRevision={0}
        backgroundTasksPending={false}
        childRunId="child-1"
        entry={activeChild}
        onClose={() => {}}
        onStopChildRun={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    expect(screen.getByText('stopped')).toBeTruthy();
    expect(screen.getByTestId('subagent-stop-button')).toBeTruthy();
  });

  it('hides stop without the operation capability even when the vendor status says running', () => {
    render(
      <SubagentTranscriptDrawer
        sessionId="session-1"
        refreshRevision={0}
        backgroundTasksPending={false}
        childRunId="child-1"
        entry={{
          ...activeChild,
          engineStatus: 'running',
          operations: [],
        }}
        onClose={() => {}}
        onStopChildRun={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    expect(screen.getByText('running')).toBeTruthy();
    expect(screen.queryByTestId('subagent-stop-button')).toBeNull();
  });

  it('reads the final transcript when background materialization settles after child closure', async () => {
    vi.mocked(getSessionChildRunMessages)
      .mockResolvedValueOnce({
        session_id: 'session-1',
        child_run_id: 'child-1',
        messages: [],
      })
      .mockResolvedValueOnce({
        session_id: 'session-1',
        child_run_id: 'child-1',
        messages: [],
      })
      .mockResolvedValueOnce({
        session_id: 'session-1',
        child_run_id: 'child-1',
        messages: [
          {
            role: 'assistant',
            message_id: 'assistant-tool',
            content: [{
              type: 'tool_use',
              id: 'tool-1',
              name: 'Bash',
              input: { command: 'printf done' },
            }],
          },
          {
            role: 'user',
            message_id: 'tool-result',
            content: [{
              type: 'tool_result',
              tool_use_id: 'tool-1',
              content: 'done',
              is_error: false,
            }],
          },
        ],
      });
    const closedChild = {
      ...activeChild,
      engineStatus: 'completed',
      closed: true,
      active: false,
      operations: [],
    };
    const { rerender } = render(
      <SubagentTranscriptDrawer
        sessionId="session-1"
        refreshRevision={0}
        backgroundTasksPending
        childRunId="child-1"
        entry={activeChild}
        onClose={() => {}}
      />,
    );

    await waitFor(() => expect(getSessionChildRunMessages).toHaveBeenCalledTimes(1));
    rerender(
      <SubagentTranscriptDrawer
        sessionId="session-1"
        refreshRevision={0}
        backgroundTasksPending
        childRunId="child-1"
        entry={closedChild}
        onClose={() => {}}
      />,
    );
    await waitFor(() => expect(getSessionChildRunMessages).toHaveBeenCalledTimes(2));
    rerender(
      <SubagentTranscriptDrawer
        sessionId="session-1"
        refreshRevision={0}
        backgroundTasksPending={false}
        childRunId="child-1"
        entry={closedChild}
        onClose={() => {}}
      />,
    );

    await waitFor(() => expect(getSessionChildRunMessages).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Bash Done' })).toBeTruthy());
  });
});
