// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SessionUserAction, UserActionNotice } from './SessionUserAction';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
afterEach(cleanup);

it('shows a message without inventing a link when the operation has no URL', () => {
  render(<UserActionNotice action={{ status: 'pending', message: 'Ask your administrator', url: null }} />);
  expect(screen.getByText('Ask your administrator')).toBeTruthy();
  expect(screen.queryByRole('link')).toBeNull();
});

it('shows the external operation and only refreshes on explicit user action', () => {
  const refresh = vi.fn();
  const perform = vi.fn();
  render(<SessionUserAction gate={{ applicable: true, ready: false, busy: false, error: null, refresh, perform,
    state: { applicable: true, ready: false, user_action: { status: 'pending', message: 'Approve access', url: 'https://identity.example/consent' }, actions: [{ id: 'check', label: 'Check consent' }] },
  }} />);
  expect(screen.getByRole('link').getAttribute('href')).toBe('https://identity.example/consent');
  expect(refresh).not.toHaveBeenCalled();
  expect(perform).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Check consent' }));
  expect(perform).toHaveBeenCalledWith('check');
  fireEvent.click(screen.getByRole('button', { name: 'chat:user_action.refresh' }));
  expect(refresh).toHaveBeenCalledTimes(1);
});
