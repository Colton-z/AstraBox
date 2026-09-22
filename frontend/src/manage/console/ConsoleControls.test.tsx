// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ConsoleSearch, NameCell } from './ConsoleControls';

afterEach(cleanup);

/**
 * §5 of docs/frontend-design.md: search answers "there is too much here to
 * scan". Below that it does not render — except while a typed query is still
 * narrowing the list, which must stay visible to be cleared.
 */
describe('ConsoleSearch earns its place', () => {
  it('does not render over a scannable list', () => {
    render(<ConsoleSearch value="" onChange={vi.fn()} total={2} placeholder="Search" />);
    expect(screen.queryByPlaceholderText('Search')).toBeNull();
  });

  it('renders once the list outgrows a screenful', () => {
    render(<ConsoleSearch value="" onChange={vi.fn()} total={40} placeholder="Search" />);
    expect(screen.getByPlaceholderText('Search')).toBeTruthy();
  });

  it('stays while a query is filtering, whatever the row count', () => {
    // Hiding the field here would leave the list invisibly narrowed with
    // nothing on screen to clear.
    render(<ConsoleSearch value="claude" onChange={vi.fn()} total={2} placeholder="Search" />);
    expect(screen.getByDisplayValue('claude')).toBeTruthy();
  });
});

/**
 * §6: mono marks machine identity. The secondary line under a name is mono for
 * a slug the reader will type, and the text face for prose — a description in
 * mono claims an identity it does not have.
 */
describe('NameCell secondary line', () => {
  it('sets a machine identity in mono by default', () => {
    render(<NameCell name="Claude Code" sub="claude-code" />);
    expect(screen.getByText('claude-code').className).toContain('console-val');
  });

  it('sets prose in the text face', () => {
    render(<NameCell name="Data Analyst" sub="Inspects CSV and plots it." subKind="text" />);
    expect(screen.getByText('Inspects CSV and plots it.').className).not.toContain('console-val');
  });
});
