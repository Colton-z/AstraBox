// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { deleteAdminRuntimeProfile, listAdminRuntimeProfiles, getRuntimeProfileSchema, adminDescribeSandboxIdleAction } from '@/api';
import RuntimeProfileDetailPage from './RuntimeProfileDetailPage';

vi.mock('@/api', () => ({
  deleteAdminRuntimeProfile: vi.fn(), listAdminRuntimeProfiles: vi.fn(),
  getRuntimeProfileSchema: vi.fn(), adminDescribeSandboxIdleAction: vi.fn(),
  upsertAdminRuntimeProfile: vi.fn(),
}));

beforeAll(async () => { await i18n.changeLanguage('en'); });
beforeEach(() => {
  vi.mocked(deleteAdminRuntimeProfile).mockReset().mockResolvedValue(undefined);
  vi.mocked(listAdminRuntimeProfiles).mockResolvedValue([{ name: 'research' }]);
  vi.mocked(getRuntimeProfileSchema).mockRejectedValue(new Error('no schema'));
  vi.mocked(adminDescribeSandboxIdleAction).mockResolvedValue({
    actions: ['terminate'], backend: 'open_sandbox', default_action: 'terminate',
    detail: null, supported_actions: ['terminate'],
  });
});
afterEach(cleanup);

function show() {
  return render(<MemoryRouter initialEntries={['/manage/runtime-profiles/research']}>
    <Routes>
      <Route path="/manage/runtime-profiles/:name" element={<RuntimeProfileDetailPage />} />
      <Route path="/manage/runtime-profiles" element={<p>RuntimeProfile list</p>} />
    </Routes>
  </MemoryRouter>);
}

it('requires named confirmation and allows cancellation without deleting', async () => {
  show();
  fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
  expect(await screen.findByRole('alertdialog')).toBeTruthy();
  expect(screen.getByText('Delete runtime profile “research”?')).toBeTruthy();
  expect(deleteAdminRuntimeProfile).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(deleteAdminRuntimeProfile).not.toHaveBeenCalled();
});

it('returns to the list only after the confirmed deletion succeeds', async () => {
  show();
  fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
  fireEvent.click(await screen.findByTestId('environment-delete'));
  expect(await screen.findByText('RuntimeProfile list')).toBeTruthy();
  expect(deleteAdminRuntimeProfile).toHaveBeenCalledExactlyOnceWith('research');
});

it('keeps the record and shows the server refusal so the owner can resolve it', async () => {
  vi.mocked(deleteAdminRuntimeProfile).mockRejectedValue(new Error('RuntimeProfile is still used by assistant Workspace.'));
  show();
  fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
  fireEvent.click(await screen.findByTestId('environment-delete'));
  expect(await screen.findByText('RuntimeProfile is still used by assistant Workspace.')).toBeTruthy();
  expect(screen.queryByText('RuntimeProfile list')).toBeNull();
  await waitFor(() => expect((screen.getByRole('button', { name: 'Delete' }) as HTMLButtonElement).disabled).toBe(false));
});
