// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { SWRConfig } from 'swr';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';

import { SidebarProvider } from '@/components/ui/sidebar';
import i18n from '@/i18n';
import {
  ensurePrimaryAssistant,
  getAssistant,
  getAssistantPanel,
  getAssistantUserAction,
  wakeAssistantWorkspace,
} from './api';
import { AssistantMainRoute } from './AssistantMainRoute';
import { AssistantPanelHost } from './AssistantPanelHost';
import type { AssistantRecord } from './types';

vi.mock('./api', () => ({
  ensurePrimaryAssistant: vi.fn(),
  getAssistant: vi.fn(),
  getAssistantPanel: vi.fn(),
  getAssistantUserAction: vi.fn(),
  openMainConversation: vi.fn(),
  performAssistantUserAction: vi.fn(),
  wakeAssistantWorkspace: vi.fn(),
}));

const desk: AssistantRecord = {
  assistant_id: 'as-1',
  owner_id: 'owner',
  display_name: 'Desk',
  icon: null,
  description: null,
  engine_kind: 'hermes',
  runtime_profile_name: 'hermes',
  permission_mode_default: '',
  workspace_state: 'READY',
  current_sandbox_id: 'sbx-1',
  runtime: { conversations: false, owner_secrets: [], panel: true, panel_opens_in_new_tab: false },
} as AssistantRecord;

beforeAll(async () => {
  // The sidebar provider asks whether this is a phone; jsdom has no answer.
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  );
  await i18n.changeLanguage('en');
  vi.mocked(ensurePrimaryAssistant).mockResolvedValue(desk);
  vi.mocked(getAssistant).mockResolvedValue(desk);
  vi.mocked(getAssistantUserAction).mockResolvedValue({
    applicable: true, ready: true, user_action: null, actions: [],
  });
  vi.mocked(wakeAssistantWorkspace).mockResolvedValue({} as never);
  vi.mocked(getAssistantPanel).mockResolvedValue({
    url: 'https://panel-1.sbx.example.test/login?once=1',
    expires_at: new Date(Date.now() + 3_600_000).toISOString(),
  });
});
afterEach(cleanup);

function Nav() {
  const navigate = useNavigate();
  return (
    <>
      <button type="button" onClick={() => navigate('/agents')}>Agents</button>
      <button type="button" onClick={() => navigate('/assistants/as-1')}>Desk</button>
    </>
  );
}

it('keeps an Assistant page loaded while another page is shown', async () => {
  render(
    <MemoryRouter initialEntries={['/assistants/as-1']}>
      <SWRConfig value={{ provider: () => new Map() }}>
        <SidebarProvider>
          <Nav />
          <AssistantPanelHost>
            <Routes>
              <Route path="/agents" element={<p>Agents page</p>} />
              <Route path="/assistants/:assistantId" element={<AssistantMainRoute onOpened={() => {}} />} />
            </Routes>
          </AssistantPanelHost>
        </SidebarProvider>
      </SWRConfig>
    </MemoryRouter>,
  );

  const frame = await screen.findByTitle('Desk');
  expect(frame.getAttribute('src')).toBe('https://panel-1.sbx.example.test/login?once=1');
  expect(getAssistantPanel).toHaveBeenCalledTimes(1);
  expect(wakeAssistantWorkspace).toHaveBeenCalledTimes(1);

  fireEvent.click(screen.getByRole('button', { name: 'Agents' }));
  await screen.findByText('Agents page');
  expect(frame.isConnected).toBe(true);
  expect(frame.closest('[hidden]')).not.toBeNull();

  // Back on the page: the same frame, not a new single-use address. The
  // workspace is woken and read again, and the read names the same box.
  fireEvent.click(screen.getByRole('button', { name: 'Desk' }));
  await waitFor(() => expect(wakeAssistantWorkspace).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(frame.closest('[hidden]')).toBeNull());
  expect(screen.getByTitle('Desk')).toBe(frame);
  expect(screen.queryByText('Agents page')).toBeNull();
  expect(getAssistantPanel).toHaveBeenCalledTimes(1);
});
