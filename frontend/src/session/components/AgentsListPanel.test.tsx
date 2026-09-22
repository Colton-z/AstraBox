// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

import i18n from '../../i18n';
import type { SubagentEntry, SubagentRegistry } from '../hooks/useSubagentRegistry';
import { AgentsListPanel } from './AgentsListPanel';

// Rendering smoke test for a split-out shell component: mounts and checks the
// key landmarks for both states it owns (empty vs. populated registry).

beforeEach(async () => {
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
});

function makeRegistry(agents: SubagentEntry[]): SubagentRegistry {
  return {
    agents,
    getAgent: (id) => agents.find((a) => a.childRunId === id),
    liveCount: agents.filter((a) => a.active).length,
    totalCount: agents.length,
  };
}

function makeAgent(overrides: Partial<SubagentEntry> = {}): SubagentEntry {
  return {
    childRunId: 'tool-use-123456',
    engineKind: 'claude_code',
    depth: 1,
    engineEvent: 'task.finished',
    engineStatus: 'completed',
    closed: true,
    active: false,
    operations: [],
    toolCallIds: [],
    ...overrides,
  };
}

describe('AgentsListPanel', () => {
  it('renders the empty-state landmark when the registry has no agents', () => {
    render(
      <AgentsListPanel registry={makeRegistry([])} selectedChildRunId={null} onSelect={() => {}} />,
    );

    expect(screen.getByTestId('subagent-agents-panel')).toBeTruthy();
    // The positive control for the e2e specs' "the empty state is gone once
    // rows render". That is an assertion of absence, so it passes whether the
    // empty state is absent or the handle is misspelled; this is where the
    // handle is proved to fire in the state it names.
    expect(screen.getByTestId('empty-state')).toBeTruthy();
    expect(screen.getByText('No subagents in this Session yet.')).toBeTruthy();
    expect(
      screen.getByText(
        'When the Agent or Task tool starts a subtask, its messages and tool activity appear here.',
      ),
    ).toBeTruthy();
  });

  it('uses readable Chinese for the empty state', async () => {
    await i18n.changeLanguage('zh');
    render(
      <AgentsListPanel registry={makeRegistry([])} selectedChildRunId={null} onSelect={() => {}} />,
    );

    expect(screen.getByText('本次 Session 还没有子 Agent。')).toBeTruthy();
    expect(
      screen.getByText('Agent 或 Task 工具启动子任务后，其消息和工具活动会显示在这里。'),
    ).toBeTruthy();
  });

  it('renders one row per agent, with the selected row calling onSelect(null) to toggle off', () => {
    const onSelect = vi.fn();
    const agent = makeAgent({
      childRunId: 'abcdef123456',
      description: 'Investigate the bug',
      engineStatus: 'failed',
    });
    render(
      <AgentsListPanel registry={makeRegistry([agent])} selectedChildRunId="abcdef123456" onSelect={onSelect} />,
    );

    const row = screen.getByTestId('subagent-agent-row');
    expect(row.textContent).toContain('Investigate the bug');
    expect(row.textContent).toContain('failed');
    // The other half of the pair above: rows and the empty state are exclusive,
    // asserted here where both states are cheap to render, so the e2e specs
    // only have to confirm the same holds in a real browser.
    expect(screen.queryByTestId('empty-state')).toBeNull();

    row.click();
    expect(onSelect).toHaveBeenCalledWith(null);
  });

  it('renders canonical child lineage and depth as a tree', () => {
    const root = makeAgent({
      childRunId: 'root-child',
      description: 'Root child',
    });
    const nested = makeAgent({
      childRunId: 'nested-child',
      parentChildRunId: 'root-child',
      depth: 2,
      description: 'Nested child',
    });
    render(
      <AgentsListPanel
        registry={makeRegistry([root, nested])}
        selectedChildRunId={null}
        onSelect={() => {}}
      />,
    );

    const rows = screen.getAllByTestId('subagent-agent-row');
    expect(rows).toHaveLength(2);
    expect(rows[0].getAttribute('data-child-run-id')).toBe('root-child');
    expect(rows[0].getAttribute('data-parent-child-run-id')).toBe('');
    expect(rows[0].getAttribute('data-subagent-depth')).toBe('1');
    expect(rows[1].getAttribute('data-child-run-id')).toBe('nested-child');
    expect(rows[1].getAttribute('data-parent-child-run-id')).toBe('root-child');
    expect(rows[1].getAttribute('data-subagent-depth')).toBe('2');
  });

  it('renders each engine status verbatim without translating vendor vocabulary', () => {
    render(
      <AgentsListPanel
        registry={makeRegistry([
          makeAgent({ childRunId: 'paused-child', engineStatus: 'paused', closed: false }),
          makeAgent({ childRunId: 'killed-child', engineStatus: 'killed' }),
          makeAgent({ childRunId: 'stopped-child', engineStatus: 'stopped' }),
        ])}
        selectedChildRunId={null}
        onSelect={() => {}}
      />,
    );

    const rows = screen.getAllByTestId('subagent-agent-row');
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining('paused'),
      expect.stringContaining('killed'),
      expect.stringContaining('stopped'),
    ]);
  });

  it('falls back from engine status to reason and then event', () => {
    render(
      <AgentsListPanel
        registry={makeRegistry([
          makeAgent({
            childRunId: 'reason-child',
            engineStatus: undefined,
            engineReason: 'max-tokens',
          }),
          makeAgent({
            childRunId: 'event-child',
            engineStatus: undefined,
            engineEvent: 'subagent.finished',
          }),
        ])}
        selectedChildRunId={null}
        onSelect={() => {}}
      />,
    );

    const rows = screen.getAllByTestId('subagent-agent-row');
    expect(rows[0].textContent).toContain('max-tokens');
    expect(rows[1].textContent).toContain('subagent.finished');
  });
});
