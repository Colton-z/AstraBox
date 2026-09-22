// @vitest-environment jsdom
import type { ReactElement, ReactNode } from 'react';
import { describe, it, expect, afterEach, beforeAll, vi } from 'vitest';
import { cleanup, render as renderBare, screen, type RenderOptions } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { SidebarProvider } from '@/components/ui/sidebar';
import i18n from '../../i18n';
import {
  SessionLoadingState,
  SessionUnavailableState,
  SessionHistoryBlockingState,
  SessionHistoryErrorState,
} from './SessionBootstrapStates';

// Rendering smoke tests for SessionPage's presentational early-return states.
// Each owns its own branch of "what to show instead of the conversation"; the
// parent only decides which one to mount. All of them render a react-router
// <Link>, so each is wrapped in a MemoryRouter.

beforeAll(async () => {
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
  );
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


afterEach(() => {
  cleanup();
});

describe('SessionLoadingState', () => {
  it('renders the loading landmark', () => {
    render(<SessionLoadingState />, { wrapper: MemoryRouter });
    expect(screen.getByText('Loading session…')).toBeTruthy();
  });
});

describe('SessionUnavailableState', () => {
  it('renders the not-found copy (with a home link) for a not-found/permission error', () => {
    render(<SessionUnavailableState loadError="session_not_found" onRetry={() => {}} />, { wrapper: MemoryRouter });
    expect(screen.getByText("This session doesn't exist or you don't have access.")).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Back to home' })).toBeTruthy();
    expect(screen.queryByText('Retry now')).toBeNull();
  });

  it('renders the not-found copy as the fallback when there is no load error at all', () => {
    render(<SessionUnavailableState loadError="" onRetry={() => {}} />, { wrapper: MemoryRouter });
    expect(screen.getByText("This session doesn't exist or you don't have access.")).toBeTruthy();
  });

  it('renders a transient-retry message plus a working retry button for a transient load error', () => {
    const onRetry = vi.fn();
    render(<SessionUnavailableState loadError="session_detail_timeout" onRetry={onRetry} />, { wrapper: MemoryRouter });

    expect(screen.getByText('Session details are temporarily unavailable. Retrying automatically.')).toBeTruthy();
    expect(screen.getByText('session_detail_timeout')).toBeTruthy();
    screen.getByRole('button', { name: 'Retry now' }).click();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('treats a browser transport interruption as transient instead of a terminal session failure', () => {
    render(
      <SessionUnavailableState
        loadError="NETWORK_ERROR: can't reach the backend (/api/v1/sessions/session-1): Failed to fetch"
        onRetry={() => {}}
      />,
      { wrapper: MemoryRouter },
    );

    expect(screen.getByText('Session details are temporarily unavailable. Retrying automatically.')).toBeTruthy();
    expect(screen.queryByText('Failed to load session.')).toBeNull();
  });

  it('renders the generic load-failed message for a non-transient, non-permission error', () => {
    render(<SessionUnavailableState loadError="boom" onRetry={() => {}} />, { wrapper: MemoryRouter });
    expect(screen.getByText('Failed to load session.')).toBeTruthy();
    expect(screen.getByText('boom')).toBeTruthy();
  });
});

describe('SessionHistoryBlockingState', () => {
  it('renders the syncing copy for a non-transient (or absent) history error', () => {
    render(<SessionHistoryBlockingState historyError={null} />, { wrapper: MemoryRouter });
    expect(screen.getByText('Syncing messages…')).toBeTruthy();
  });

  it('renders the transient-message copy and the raw error text for a transient history error', () => {
    render(<SessionHistoryBlockingState historyError="Failed to fetch" />, { wrapper: MemoryRouter });
    expect(screen.getByText('Messages are temporarily unavailable. Retrying automatically.')).toBeTruthy();
    expect(screen.getByText('Failed to fetch')).toBeTruthy();
  });
});

describe('SessionHistoryErrorState', () => {
  it('renders the load-failed message, the raw error, and wires the retry button', () => {
    const onRetry = vi.fn();
    render(<SessionHistoryErrorState historyError="boom" onRetry={onRetry} />, { wrapper: MemoryRouter });

    expect(screen.getByText('Failed to load messages.')).toBeTruthy();
    expect(screen.getByText('boom')).toBeTruthy();
    screen.getByRole('button', { name: 'Retry now' }).click();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
