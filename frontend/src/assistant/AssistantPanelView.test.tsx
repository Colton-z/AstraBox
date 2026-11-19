// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest';

import { SidebarProvider } from '@/components/ui/sidebar';
import i18n from '@/i18n';
import type { SessionUserActionState } from '@/types';
import {
  getAssistant,
  getAssistantPanel,
  getAssistantUserAction,
  performAssistantUserAction,
  wakeAssistantWorkspace,
} from './api';
import { AssistantPanelView } from './AssistantPanelView';
import type { AssistantRecord } from './types';

vi.mock('./api', () => ({
  getAssistant: vi.fn(),
  getAssistantPanel: vi.fn(),
  getAssistantUserAction: vi.fn(),
  performAssistantUserAction: vi.fn(),
  wakeAssistantWorkspace: vi.fn(),
}));

const assistant: AssistantRecord = {
  assistant_id: 'as-1',
  owner_id: 'owner',
  display_name: 'Desk',
  icon: null,
  description: null,
  engine_kind: 'dsh_superagent',
  runtime_profile_name: 'superagent',
  permission_mode_default: '',
  workspace_state: 'HIBERNATING',
};

const consent = (url: string): SessionUserActionState => ({
  applicable: true,
  ready: false,
  user_action: { status: 'user_action_required', message: 'Authorize Desk to act for you.', url },
  actions: [{ id: 'renew', label: 'Get a new link' }],
});
const ready: SessionUserActionState = { applicable: true, ready: true, user_action: null, actions: [] };

beforeAll(async () => {
  // The sidebar provider asks whether this is a phone; jsdom has no answer.
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  );
  await i18n.changeLanguage('en');
});
beforeEach(() => {
  vi.mocked(getAssistant).mockReset().mockResolvedValue(assistant);
  vi.mocked(getAssistantPanel).mockReset().mockReturnValue(new Promise(() => {}));
  vi.mocked(getAssistantUserAction).mockReset();
  vi.mocked(performAssistantUserAction).mockReset();
  vi.mocked(wakeAssistantWorkspace).mockReset().mockResolvedValue({} as never);
});
afterEach(cleanup);

function open() {
  render(
    <MemoryRouter>
      <SidebarProvider>
        <AssistantPanelView initial={assistant} />
      </SidebarProvider>
    </MemoryRouter>,
  );
}

it('starts the workspace only after the deployment says the owner has nothing left to do', async () => {
  vi.mocked(getAssistantUserAction).mockResolvedValue(consent('https://consent.example.test/expired'));
  vi.mocked(performAssistantUserAction).mockResolvedValue(consent('https://consent.example.test/fresh'));
  open();

  const link = await screen.findByRole('link', { name: 'Open required action' });
  expect(link.getAttribute('href')).toBe('https://consent.example.test/expired');
  expect(screen.getByText('Authorize Desk to act for you.')).toBeTruthy();

  fireEvent.click(screen.getByRole('button', { name: 'Get a new link' }));
  await waitFor(() => {
    expect(screen.getByRole('link', { name: 'Open required action' }).getAttribute('href'))
      .toBe('https://consent.example.test/fresh');
  });
  expect(performAssistantUserAction).toHaveBeenCalledWith('as-1', 'renew');
  expect(wakeAssistantWorkspace).not.toHaveBeenCalled();

  vi.mocked(getAssistantUserAction).mockResolvedValue(ready);
  fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }));
  await waitFor(() => expect(wakeAssistantWorkspace).toHaveBeenCalledTimes(1));
  expect(wakeAssistantWorkspace).toHaveBeenCalledWith('as-1');
});

it('shows the link of the action a stopped start recorded, and asks before starting again', async () => {
  vi.mocked(getAssistantUserAction).mockResolvedValue(ready);
  vi.mocked(getAssistant).mockResolvedValue({
    ...assistant,
    workspace_action: {
      status: 'user_action_required', message: 'Authorize Desk to act for you.',
      url: 'https://consent.example.test/first',
    },
  });
  open();

  const link = await screen.findByRole('link', { name: 'Open required action' });
  expect(link.getAttribute('href')).toBe('https://consent.example.test/first');
  expect(screen.getByText('Desk did not start')).toBeTruthy();
  expect(wakeAssistantWorkspace).toHaveBeenCalledTimes(1);

  vi.mocked(getAssistantUserAction).mockResolvedValue(consent('https://consent.example.test/second'));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }));
  await waitFor(() => {
    expect(screen.getByRole('link', { name: 'Open required action' }).getAttribute('href'))
      .toBe('https://consent.example.test/second');
  });
  expect(wakeAssistantWorkspace).toHaveBeenCalledTimes(1);
});
