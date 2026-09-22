// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { AppShell } from './AppShell';

beforeAll(() => {
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
  );
});

afterEach(cleanup);

describe('AppShell', () => {
  it('keeps the shared rail exposed as a named navigation landmark', () => {
    render(
      <AppShell
        sidebarLabel="Primary navigation"
        skipLabel="Skip to content"
        sidebarHeader={<a href="/">Home</a>}
        sidebarContent={<a href="/manage/agents">Agents</a>}
      >
        <p>Content</p>
      </AppShell>,
    );

    const navigation = screen.getByRole('navigation', { name: 'Primary navigation' });
    expect(navigation.contains(screen.getByRole('link', { name: 'Home' }))).toBe(true);
    expect(navigation.contains(screen.getByRole('link', { name: 'Agents' }))).toBe(true);
  });
});
