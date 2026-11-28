import { describe, expect, it } from 'vitest';

import { getSessionRightPanelCapabilities } from './sessionCapabilities';

describe('getSessionRightPanelCapabilities', () => {
  it('puts Tasks right after Files, ahead of the surfaces an Agent may add', () => {
    const caps = getSessionRightPanelCapabilities({
      session_kind: 'agent',
      workspace_panels: { terminal: true, diff: true },
    });
    expect(caps.tabs).toEqual(['files', 'tasks', 'browser', 'terminal', 'diff', 'artifacts']);
  });
});
