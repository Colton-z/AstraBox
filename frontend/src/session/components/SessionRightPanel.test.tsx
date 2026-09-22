// @vitest-environment jsdom
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/react';

import type { SubagentRegistry } from '../hooks/useSubagentRegistry';
import { SessionRightPanel } from './SessionRightPanel';

vi.mock('@/components/ui/resizable', () => ({
  ResizablePanel: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
}));

afterEach(cleanup);

const emptyRegistry: SubagentRegistry = {
  agents: [],
  getAgent: () => undefined,
  liveCount: 0,
  totalCount: 0,
};

describe('SessionRightPanel', () => {
  it('renders tab panels only for capabilities that also render a trigger', () => {
    const { container } = render(
      <SessionRightPanel
        rightTab="files"
        setRightTab={() => {}}
        rightPanelCaps={{ tabs: ['files', 'agents'], defaultTab: 'files' }}
        uniqueChangedFiles={0}
        subagentRegistry={emptyRegistry}
        childRunProjectionError={null}
        sessionId="session-1"
        filesPanelEnabled={false}
        runtimeUnavailableMessage="Unavailable"
        filesRefreshNonce={0}
        runtimeAccessReady={false}
        lifecycleState="terminated"
        terminalCwd={null}
        setTerminalCwd={() => {}}
        selectedChildRunId={null}
        setSelectedChildRunId={() => {}}
        fileChanges={[]}
        selectedDiffFile={null}
        setSelectedDiffFile={() => {}}
      />,
    );

    const tabs = [...container.querySelectorAll('[role="tab"]')];
    const panels = [...container.querySelectorAll('[role="tabpanel"]')];
    expect(tabs).toHaveLength(2);
    // A panel per capability that also has a trigger — counted as "no more
    // panels than triggers, and the open one is there". The tabs mount the
    // selected panel only, so pinning this to 2 would assert the library's
    // mount timing rather than which capabilities got a panel. What rules out
    // a panel with no trigger is the reference check below: an orphan panel
    // has no tab pointing at it, and a trigger with no panel dangles.
    expect(panels.length).toBeGreaterThan(0);
    expect(panels.length).toBeLessThanOrEqual(tabs.length);
    for (const element of container.querySelectorAll('[aria-controls], [aria-labelledby]')) {
      for (const attribute of ['aria-controls', 'aria-labelledby']) {
        for (const id of element.getAttribute(attribute)?.split(/\s+/) ?? []) {
          expect(document.getElementById(id), `${attribute} points to missing #${id}`).not.toBeNull();
        }
      }
    }
  });
});
