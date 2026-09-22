// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { FilterChips } from './FilterChips';

afterEach(cleanup);

/**
 * §5 of docs/frontend-design.md: a filter renders when it can change the rows
 * below it. These pin the two gates — a chip that selects nothing does not
 * render, and a group whose every choice yields the same rows does not render
 * at all. The one-row fixture proves that zero-count alternatives do not make
 * an otherwise redundant group useful.
 */
describe('FilterChips earn their place', () => {
  it('renders nothing when every choice selects the same rows', () => {
    render(
      <FilterChips
        value="all"
        onChange={vi.fn()}
        options={[
          { value: 'all', label: 'All', count: 1 },
          { value: 'enabled', label: 'Enabled', count: 1 },
          { value: 'disabled', label: 'Disabled', count: 0 },
        ]}
      />,
    );
    expect(screen.queryByRole('group')).toBeNull();
  });

  it('drops the zero-count chip but keeps a group whose counts differ', () => {
    render(
      <FilterChips
        value="all"
        onChange={vi.fn()}
        options={[
          { value: 'all', label: 'All', count: 3 },
          { value: 'enabled', label: 'Enabled', count: 2 },
          { value: 'disabled', label: 'Disabled', count: 1 },
          { value: 'archived', label: 'Archived', count: 0 },
        ]}
      />,
    );
    expect(screen.getByText('All')).toBeTruthy();
    expect(screen.getByText('Disabled')).toBeTruthy();
    expect(screen.queryByText('Archived')).toBeNull();
  });

  it('keeps the active chip on screen even at zero, so the narrowing can be undone', () => {
    // The reader filtered to Disabled, then the last disabled row was enabled
    // elsewhere: the chip they are standing on must not vanish under them.
    render(
      <FilterChips
        value="disabled"
        onChange={vi.fn()}
        options={[
          { value: 'all', label: 'All', count: 2 },
          { value: 'enabled', label: 'Enabled', count: 2 },
          { value: 'disabled', label: 'Disabled', count: 0 },
        ]}
      />,
    );
    expect(screen.getByText('Disabled')).toBeTruthy();
  });

  it('treats an uncounted option as unknown, not empty, and keeps the group', () => {
    render(
      <FilterChips
        value="all"
        onChange={vi.fn()}
        options={[
          { value: 'all', label: 'All' },
          { value: 'mine', label: 'Mine' },
        ]}
      />,
    );
    expect(screen.getByText('Mine')).toBeTruthy();
  });
});
