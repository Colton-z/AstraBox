// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { deleteAdminEnvironment, listAdminEnvironments, getEnvironmentSchema, adminDescribeSandboxIdleAction } from '@/api';
import EnvironmentDetailPage from './EnvironmentDetailPage';

vi.mock('@/api', () => ({
  deleteAdminEnvironment: vi.fn(), listAdminEnvironments: vi.fn(),
  getEnvironmentSchema: vi.fn(), adminDescribeSandboxIdleAction: vi.fn(),
  upsertAdminEnvironment: vi.fn(),
}));

beforeAll(async () => { await i18n.changeLanguage('en'); });
beforeEach(() => {
  vi.mocked(deleteAdminEnvironment).mockReset().mockResolvedValue(undefined);
  vi.mocked(listAdminEnvironments).mockResolvedValue([{ name: 'research' }]);
  vi.mocked(getEnvironmentSchema).mockRejectedValue(new Error('no schema'));
  vi.mocked(adminDescribeSandboxIdleAction).mockResolvedValue({
    actions: ['terminate'], backend: 'open_sandbox', default_action: 'terminate',
    detail: null, supported_actions: ['terminate'],
  });
});
afterEach(cleanup);

function show() {
  return render(<MemoryRouter initialEntries={['/manage/environments/research']}>
    <Routes>
      <Route path="/manage/environments/:name" element={<EnvironmentDetailPage />} />
      <Route path="/manage/environments" element={<p>Environment list</p>} />
    </Routes>
  </MemoryRouter>);
}

it('requires named confirmation and allows cancellation without deleting', async () => {
  show();
  fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
  expect(await screen.findByRole('alertdialog')).toBeTruthy();
  expect(screen.getByText('Delete Environment “research”?')).toBeTruthy();
  expect(deleteAdminEnvironment).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(deleteAdminEnvironment).not.toHaveBeenCalled();
});

it('returns to the list only after the confirmed deletion succeeds', async () => {
  show();
  fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
  fireEvent.click(await screen.findByTestId('environment-delete'));
  expect(await screen.findByText('Environment list')).toBeTruthy();
  expect(deleteAdminEnvironment).toHaveBeenCalledExactlyOnceWith('research');
});

it('keeps the record and shows the server refusal so the owner can resolve it', async () => {
  vi.mocked(deleteAdminEnvironment).mockRejectedValue(new Error('Environment is still used by assistant Workspace.'));
  show();
  fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
  fireEvent.click(await screen.findByTestId('environment-delete'));
  expect(await screen.findByText('Environment is still used by assistant Workspace.')).toBeTruthy();
  expect(screen.queryByText('Environment list')).toBeNull();
  await waitFor(() => expect((screen.getByRole('button', { name: 'Delete' }) as HTMLButtonElement).disabled).toBe(false));
});
