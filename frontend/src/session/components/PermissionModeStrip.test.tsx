// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeAll, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import i18n from '../../i18n';
import type { PermissionMode } from '../../types';
import { PermissionModeStripInline } from './PermissionModeStrip';

// The permission-mode control embedded in both the composer and the
// pending-interaction footer. What is asserted is that the roster is
// reachable: the control's job is to let a reader see every mode the engine
// declared and pick one, and a control that shows a subset reads to them as
// the engine having only those.

beforeAll(async () => {
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
});

const LABELS: Record<PermissionMode, string> = {
  default: 'Default mode',
  acceptEdits: 'Accept edits',
  plan: 'Plan mode',
  bypassPermissions: 'Skip confirmations',
  dontAsk: 'Deny unapproved tools',
  auto: 'Automatic approval',
};
const MODES = Object.keys(LABELS) as PermissionMode[];

function renderStrip(overrides: Partial<React.ComponentProps<typeof PermissionModeStripInline>> = {}) {
  return render(
    <PermissionModeStripInline
      permissionMode="default"
      permissionModes={MODES}
      canChange
      modeSwitching={false}
      onSelect={async () => {}}
      {...overrides}
    />,
  );
}

describe('PermissionModeStripInline', () => {
  it.each(MODES)('names the current mode on the trigger for %s', (mode) => {
    renderStrip({ permissionMode: mode });

    const badge = screen.getByTestId('permission-mode-badge');
    expect(badge.textContent).toContain(LABELS[mode]);
    expect(badge.getAttribute('data-permission-mode')).toBe(mode);
  });

  it('opens onto every mode the engine declared, not just the next one', async () => {
    const user = userEvent.setup();
    renderStrip({ permissionMode: 'plan' });

    await user.click(screen.getByRole('button', { name: 'Choose permission mode' }));

    const items = await screen.findAllByRole('menuitemradio');
    expect(items.map((item) => item.getAttribute('data-permission-option'))).toEqual(MODES);
    // The one in force is marked in the list rather than only on the trigger,
    // so the reader can see where they are among the choices.
    expect(items.find((item) => item.getAttribute('aria-checked') === 'true')
      ?.getAttribute('data-permission-option')).toBe('plan');
  });

  it('asks for the mode that was picked', async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn().mockResolvedValue(undefined);
    renderStrip({ onSelect });

    await user.click(screen.getByRole('button', { name: 'Choose permission mode' }));
    await user.click(await screen.findByRole('menuitemradio', { name: LABELS.bypassPermissions }));

    await waitFor(() => expect(onSelect).toHaveBeenCalledWith('bypassPermissions'));
  });

  it('says a change is in flight and offers no second one until it lands', () => {
    renderStrip({ modeSwitching: true });

    expect(screen.getByTestId('permission-mode-badge').textContent).toContain('Switching…');
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('still names the mode when the session cannot change it', () => {
    renderStrip({ canChange: false });

    expect(screen.getByTestId('permission-mode-badge').textContent).toContain(LABELS.default);
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('keeps an engine-defined mode distinct instead of styling it as default', () => {
    renderStrip({ permissionMode: 'reviewOnly', permissionModes: ['reviewOnly'], canChange: false });

    const badge = screen.getByTestId('permission-mode-badge');
    expect(badge.textContent).toBe('reviewOnly');
    expect(badge.getAttribute('data-permission-mode')).toBe('reviewOnly');
    expect(badge.classList.contains('permmode-engine')).toBe(true);
    expect(badge.classList.contains('permmode-default')).toBe(false);
  });

  it('carries an engine-defined mode into the list verbatim', async () => {
    const user = userEvent.setup();
    renderStrip({ permissionMode: 'read-only', permissionModes: ['read-only', 'workspace-write', 'danger-full-access'] });

    await user.click(screen.getByRole('button', { name: 'Choose permission mode' }));

    const items = await screen.findAllByRole('menuitemradio');
    expect(items.map((item) => item.textContent)).toEqual([
      'read-only', 'workspace-write', 'danger-full-access',
    ]);
  });
});
