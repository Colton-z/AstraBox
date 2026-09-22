// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';

const { DateRangeFilter } = await import('./DateRangeFilter');

afterEach(cleanup);
beforeAll(async () => {
  await i18n.changeLanguage('en');
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
  Element.prototype.scrollIntoView = () => {};
});

describe('DateRangeFilter', () => {
  it('renders closed without throwing', () => {
    render(<DateRangeFilter value={{}} onChange={vi.fn()} />);
    expect(screen.getByRole('button', { name: /all dates/i })).toBeTruthy();
  });

  it('opens to a calendar without throwing', () => {
    render(<DateRangeFilter value={{}} onChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /all dates/i }));
    expect(screen.getByRole('button', { name: /last 7 days/i })).toBeTruthy();
  });

  it('renders with a range already selected, and shows it on the trigger', () => {
    // A pre-selected range initializes the calendar through `defaultMonth`, so
    // this exercises a different mount path from the empty-range case above.
    render(<DateRangeFilter value={{ since: '2026-08-01', until: '2026-08-07' }} onChange={vi.fn()} />);

    const trigger = screen.getAllByRole('button')[0];
    expect(trigger.textContent).toMatch(/Aug/);
    fireEvent.click(trigger);
    expect(screen.getByRole('button', { name: /last 30 days/i })).toBeTruthy();
  });

  it('offers a way back to all dates once a range is set', () => {
    // A filter that cannot be undone is a trap.
    const onChange = vi.fn();
    render(<DateRangeFilter value={{ since: '2026-08-01', until: '2026-08-07' }} onChange={onChange} />);

    fireEvent.click(screen.getByRole('button', { name: /clear date range/i }));
    expect(onChange).toHaveBeenCalledWith({});
  });
});
