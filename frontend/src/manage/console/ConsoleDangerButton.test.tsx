// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { ConsoleDangerButton } from './ConsoleDangerButton';

afterEach(cleanup);
beforeAll(async () => {
  await i18n.changeLanguage('en');
});

/**
 * A destructive action asks before it acts, and the asking is this component's.
 *
 * Pinned by rendering rather than by reading the source, because the two halves
 * that can silently stop working are both behaviours of the kit: the trigger
 * only opens the dialog if `render` hands the popup this button rather than
 * leaving it a second one beside it, and the kit's `AlertDialogAction` is a
 * plain `Button` that leaves its own dialog open — which looks finished on
 * screen right up until the action fails and the reader is left holding a
 * dialog over a page they cannot reach.
 */
describe('ConsoleDangerButton', () => {
  it('acts on its own click when no confirmation is asked for', () => {
    const onClick = vi.fn();
    render(<ConsoleDangerButton onClick={onClick}>Delete</ConsoleDangerButton>);
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('asks first, then runs the action once and closes', () => {
    const onConfirm = vi.fn();
    render(
      <ConsoleDangerButton
        confirm={{ title: 'Delete claude-code', action: 'Delete environment', onConfirm }}
      >
        Delete
      </ConsoleDangerButton>,
    );

    expect(screen.queryByText('Delete claude-code')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    // The question is on screen and nothing has happened yet.
    expect(screen.getByText('Delete claude-code')).toBeTruthy();
    expect(onConfirm).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Delete environment' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('Delete claude-code')).toBeNull();
  });

  it('offers a way out that does not act', () => {
    const onConfirm = vi.fn();
    render(
      <ConsoleDangerButton confirm={{ title: 'Kill session', action: 'Kill', onConfirm }}>
        Kill
      </ConsoleDangerButton>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Kill' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.queryByText('Kill session')).toBeNull();
  });

  it('wears the console crimson on both faces — quiet trigger, armed confirm', () => {
    render(
      <ConsoleDangerButton confirm={{ title: 'Delete', action: 'Delete it', onConfirm: vi.fn() }}>
        Delete
      </ConsoleDangerButton>,
    );
    const trigger = screen.getByRole('button', { name: 'Delete' });
    expect(trigger.className).toContain('console-danger-btn');
    expect(trigger.getAttribute('data-danger')).toBe('soft');

    fireEvent.click(trigger);
    const armed = screen.getByRole('button', { name: 'Delete it' });
    expect(armed.className).toContain('console-danger-btn');
    expect(armed.getAttribute('data-danger')).toBe('solid');
  });

  it('is sized by the band it stands in, not by the component', () => {
    // A Delete beside a page heading stands as tall as the New on the list that
    // heading was reached from (docs/frontend-design.md §9).
    render(<ConsoleDangerButton size="sm">Delete</ConsoleDangerButton>);
    expect(screen.getByRole('button', { name: 'Delete' }).className).toContain('h-7');
  });
});
