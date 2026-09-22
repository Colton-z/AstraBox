// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { AssistantRecord, AssistantWorkspaceState } from './types';

let workspaceState: AssistantWorkspaceState = 'NOT_MATERIALIZED';
const startConversation = vi.fn(async (_assistantId: string) => ({ session_id: 'session-1' }));
const wakeWorkspace = vi.fn(async (_assistantId: string) => ({}));

function assistant(): AssistantRecord {
  return {
    assistant_id: 'assistant-1',
    owner_id: 'user-1',
    display_name: 'Research Assistant',
    icon: null,
    description: null,
    engine_kind: 'assistant',
    environment_name: 'assistant-env',
    permission_mode_default: '',
    workspace_state: workspaceState,
  };
}

vi.mock('./api', () => ({
  listAssistants: async () => [assistant()],
  getAssistant: async () => assistant(),
  startAssistantConversation: async (assistantId: string) => startConversation(assistantId),
  wakeAssistantWorkspace: async (assistantId: string) => wakeWorkspace(assistantId),
}));

const { default: AssistantCards } = await import('./AssistantsPage');

beforeAll(async () => {
  await i18n.changeLanguage('en');
});

beforeEach(() => {
  workspaceState = 'NOT_MATERIALIZED';
  startConversation.mockClear();
  wakeWorkspace.mockClear();
});

afterEach(cleanup);

describe('Assistant conversation startup', () => {
  it.each<AssistantWorkspaceState>([
    'NOT_MATERIALIZED',
    'MATERIALIZING',
    'READY',
    'HIBERNATING',
    'RECOVERY_REQUIRED',
  ])('starts one Session directly while the workspace is %s', async (state) => {
    workspaceState = state;
    const onConversationCreated = vi.fn();
    render(<AssistantCards onConversationCreated={onConversationCreated} />);

    const start = await screen.findByRole('button', { name: 'Start conversation' });
    fireEvent.click(start);

    await waitFor(() => expect(startConversation).toHaveBeenCalledWith('assistant-1'));
    expect(wakeWorkspace).not.toHaveBeenCalled();
    expect(onConversationCreated).toHaveBeenCalledWith('session-1');
  });
});
