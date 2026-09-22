// @vitest-environment jsdom
import type { ReactElement, ReactNode } from 'react';
import { describe, it, expect, afterEach, beforeAll, beforeEach, vi } from 'vitest';
import { cleanup, render as renderBare, screen, type RenderOptions } from '@testing-library/react';

import { SidebarProvider } from '@/components/ui/sidebar';
import i18n from '../../i18n';
import type { SessionRecord } from '../../types';
import { SessionHeader } from './SessionHeader';

// Rendering smoke test for the top page-chrome header: RunId, StatusPill,
// runtime/error hints, ShareDialog, and the terminate, end-conversation, and
// recover actions. ShareDialog starts closed (see ShareDialog.test.tsx), so
// mounting the whole header performs no network call.

beforeAll(async () => {
  await i18n.changeLanguage('en');
});

/**
 * Both of these stand in the app shell's topbar and carry its rail toggle, so
 * `SidebarTrigger` needs the provider the shell mounts around them. Rendering
 * them bare is the one arrangement the product never uses; wrapping here keeps
 * every assertion below unchanged while the component sees its real context.
 * `matchMedia` is stubbed the same way `AppShell.test.tsx` stubs it — the
 * provider asks whether this is a phone, and jsdom has no answer of its own.
 */
const render = (ui: ReactElement, options?: RenderOptions) => {
  const Outer = options?.wrapper;
  const Wrapper = ({ children }: { children: ReactNode }) =>
    Outer ? (
      <Outer>
        <SidebarProvider>{children}</SidebarProvider>
      </Outer>
    ) : (
      <SidebarProvider>{children}</SidebarProvider>
    );
  return renderBare(ui, { ...options, wrapper: Wrapper });
};


// Re-stubbed per test, not once: a test below calls `vi.unstubAllGlobals()`
// for its own reasons, and a single beforeAll stub would be gone for every
// test after that one.
beforeEach(() => {
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
  );
});

afterEach(() => {
  cleanup();
});

function makeSession(overrides: Partial<SessionRecord> = {}): SessionRecord {
  return {
    session_id: 'abcdef12-3456-7890',
    user_id: 'user-1',
    template_name: 'code-agent',
    state: 'READY',
    ...overrides,
  };
}

function baseProps() {
  return {
    headerIsLive: true,
    headerTone: 'running' as const,
    headerStatusLabel: 'Working',
    headerRunState: 'PROCESSING',
    showRuntimeUnavailableBanner: false,
    showSessionLastError: false,
    isAssistantConversation: false,
    isAgentChat: false,
    isTerminated: false,
    lifecycleState: 'ready',
    terminateLoading: false,
    handleEndConversation: () => {},
    handleTerminate: () => {},
    showRecoverButton: false,
    handleRecover: () => {},
  };
}

describe('SessionHeader', () => {
  it('renders the run id, model badge, title, status pill, and both hint lines with no network call', () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);

    render(
      <SessionHeader
        {...baseProps()}
        session={makeSession({ title: 'Fix the flaky test', model_name: 'claude-sonnet' })}
        showRuntimeUnavailableBanner
        showSessionLastError
      />,
    );

    // Sentence case, not small caps — the id is a machine identity, the word
    // in front of it is chrome (docs/frontend-design.md §6).
    expect(screen.getByText('Run · abcdef12')).toBeTruthy();
    expect(screen.getByText('claude-sonnet')).toBeTruthy();
    expect(screen.getByText('Fix the flaky test')).toBeTruthy();
    expect(screen.getByTestId('status-pill').textContent).toContain('Working');
    expect(screen.getByText('Runtime disconnected')).toBeTruthy();
    expect(fetchSpy).not.toHaveBeenCalled();

    vi.unstubAllGlobals();
  });

  it('falls back the title to the template name, and localizes a known last_error wire string', () => {
    render(
      <SessionHeader
        {...baseProps()}
        session={makeSession({ title: undefined, last_error: 'sandbox expired' })}
        showSessionLastError
      />,
    );

    expect(screen.getByText('code-agent')).toBeTruthy();
    // 'sandbox expired' -> misc:wire.sandbox_expired -> 'Sandbox expired' (see utils/format.ts).
    expect(screen.getByText('Sandbox expired')).toBeTruthy();
  });

  it('shows "End conversation" for an assistant conversation and wires it to handleEndConversation', () => {
    const handleEndConversation = vi.fn();
    render(
      <SessionHeader {...baseProps()} session={makeSession()} isAssistantConversation handleEndConversation={handleEndConversation} />,
    );

    screen.getByRole('button', { name: 'End conversation' }).click();
    expect(handleEndConversation).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Terminate' })).toBeNull();
  });

  it('shows "Terminate" for a plain (non-assistant, non-agent-chat) session and wires it', () => {
    const handleTerminate = vi.fn();
    render(<SessionHeader {...baseProps()} session={makeSession()} handleTerminate={handleTerminate} />);

    screen.getByRole('button', { name: 'Terminate' }).click();
    expect(handleTerminate).toHaveBeenCalledTimes(1);
  });

  it('hides both terminate controls for an agent-chat session, and wires the recover button when offered', () => {
    const handleRecover = vi.fn();
    render(
      <SessionHeader {...baseProps()} session={makeSession()} isAgentChat showRecoverButton handleRecover={handleRecover} />,
    );

    expect(screen.queryByRole('button', { name: 'Terminate' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'End conversation' })).toBeNull();
    screen.getByRole('button', { name: 'Recover session' }).click();
    expect(handleRecover).toHaveBeenCalledTimes(1);
  });
});
