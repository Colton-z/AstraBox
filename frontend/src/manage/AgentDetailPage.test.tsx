// @vitest-environment jsdom
/**
 * A refused save on an Agent's page.
 *
 * Each card saves on its own, so the page can be scrolled far from the top
 * when the reader presses Save. A refusal reported anywhere but beside that
 * Save is, to the reader, a save that quietly did nothing. The refusal here is
 * not hand-made: the PUT answers with the envelope the server's
 * `APIError.to_response_payload()` produces, and the page reads it through the
 * real `updateAgent` request path.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { SWRConfig } from 'swr';
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import {
  getAgentAccess,
  getAgentPreparedRuntime,
  getAgentSchema,
  getCurrentUser,
  getExtensionProvider,
  listAgentCategories,
  listAgentConnectors,
  listAgentRuntimeProfileModels,
  listAgentRuntimeProfiles,
  listAgentTags,
  listAgents,
  listConnectors,
} from '@/api';
import type { AgentConfig, AgentPreparedRuntimeStatus, FormSchema } from '@/types';

import AgentDetailPage from './AgentDetailPage';

// The reads are stubbed; `updateAgent` and the error shaping stay real.
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  getAgentAccess: vi.fn(),
  getAgentPreparedRuntime: vi.fn(),
  getAgentSchema: vi.fn(),
  getCurrentUser: vi.fn(),
  getExtensionProvider: vi.fn(),
  listAgentCategories: vi.fn(),
  listAgentConnectors: vi.fn(),
  listAgentRuntimeProfileModels: vi.fn(),
  listAgentRuntimeProfiles: vi.fn(),
  listAgentTags: vi.fn(),
  listAgents: vi.fn(),
  listConnectors: vi.fn(),
}));

// The groups and fields of `GET /api/v1/agent-configuration/schema` this test
// reaches, field for field as agent_schema.py declares them.
const SCHEMA: FormSchema = {
  version: 1,
  groups: [{ id: 'identity' }, { id: 'runtime' }, { id: 'model' }],
  fields: [
    { key: 'name', type: 'string', group: 'identity', required: true },
    { key: 'description', type: 'text', group: 'identity' },
    { key: 'model', type: 'string', group: 'model', required: true },
    { key: 'system', type: 'text', group: 'model' },
    { key: 'runtime_profile_name', type: 'runtime_profile_ref', group: 'runtime', required: true },
    { key: 'prepared_capacity', type: 'integer', advanced: true, group: 'runtime', default: 0 },
  ],
};

/** One row of `GET /api/v1/agents`, as the server sends it to a manager. */
const RECORD: AgentConfig = {
  agent_id: 'agent-1',
  name: 'research',
  description: 'Reads filings.',
  model: 'claude-opus-4-8',
  runtime_profile_name: 'claude-code',
  prepared_capacity: 0,
  enabled: true,
  version: 3,
  state: 'ACTIVE',
  can_manage: true,
  updated_at: '2026-10-11T08:00:00+00:00',
};

// `APIError(code="INVALID_REQUEST", message="prepared_capacity must be between
// 0 and 1000", status_code=400).to_response_payload()`, as the PUT answers it.
const REFUSAL = {
  code: 'INVALID_REQUEST',
  message: 'prepared_capacity must be between 0 and 1000',
  data: null,
  error: {
    code: 'INVALID_REQUEST',
    status_code: 400,
    category: 'request',
    retryable: false,
    owner: 'client',
    user_message: 'prepared_capacity must be between 0 and 1000',
  },
};

const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
  if (url === '/api/v1/agents/agent-1' && init?.method === 'PUT') {
    return new Response(JSON.stringify(REFUSAL), {
      status: 400,
      headers: { 'content-type': 'application/json' },
    });
  }
  throw new Error(`unexpected request: ${init?.method ?? 'GET'} ${url}`);
});

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
beforeEach(() => {
  fetchMock.mockClear();
  vi.stubGlobal('fetch', fetchMock);
  vi.mocked(getCurrentUser).mockReset().mockResolvedValue({
    user_id: 'owner', is_admin: false, roles: [],
    display_name: null, email: null, avatar_url: null,
  });
  vi.mocked(listAgents).mockReset().mockResolvedValue([RECORD]);
  vi.mocked(getAgentAccess).mockReset().mockResolvedValue({
    created_by: 'owner',
    visibility: 'private',
    admins: [],
    allowed_user_ids: [],
  });
  vi.mocked(getAgentSchema).mockReset().mockResolvedValue(SCHEMA);
  vi.mocked(listAgentRuntimeProfiles).mockReset().mockResolvedValue([
    {
      name: 'claude-code',
      enabled: true,
      engine_kind: 'claude_code',
      engine_available: true,
      supported_session_kinds: ['agent_chat'],
    },
  ]);
  vi.mocked(listAgentRuntimeProfileModels).mockReset().mockResolvedValue(['claude-opus-4-8']);
  vi.mocked(listAgentCategories).mockReset().mockResolvedValue([]);
  vi.mocked(listAgentTags).mockReset().mockResolvedValue([]);
  vi.mocked(getExtensionProvider).mockReset().mockResolvedValue(null);
  vi.mocked(getAgentPreparedRuntime).mockReset().mockResolvedValue({
    enabled: false,
    target: 0,
    ready: false,
    prepared_count: 0,
  } as AgentPreparedRuntimeStatus);
  vi.mocked(listConnectors).mockReset().mockResolvedValue([]);
  vi.mocked(listAgentConnectors).mockReset().mockResolvedValue([]);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function show() {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <MemoryRouter initialEntries={['/manage/agents/agent-1']}>
        <Routes>
          <Route path="/manage/agents/:agentId" element={<AgentDetailPage />} />
        </Routes>
      </MemoryRouter>
    </SWRConfig>,
  );
}

/** The settings card under the heading `title`. */
function card(title: string): HTMLElement {
  const heading = screen.getByRole('heading', { name: title });
  return heading.closest('[data-slot="card"]') as HTMLElement;
}

it('keeps a refused save beside its Save and marks the field the refusal names', async () => {
  show();
  const capacity = (await screen.findByRole('spinbutton', {
    name: 'Prepared capacity',
  })) as HTMLInputElement;
  // The control mounts before the fetched record seeds its editable draft.
  await waitFor(() => expect(capacity.value).toBe(String(RECORD.prepared_capacity)));

  fireEvent.change(capacity, { target: { value: '5000' } });
  const runtime = card('Runtime and availability');
  const reads = vi.mocked(listAgents).mock.calls.length;
  // The card's Save renders once the edit makes it dirty, after this event.
  const save = (await within(runtime).findByRole('button', { name: 'Save' })) as HTMLButtonElement;
  fireEvent.click(save);

  const reason = 'INVALID_REQUEST: prepared_capacity must be between 0 and 1000 (category=request)';
  expect(await within(runtime).findByText(reason)).toBeTruthy();
  expect(fetchMock).toHaveBeenCalledTimes(1);
  // In the card whose Save was pressed, and nowhere else on the page.
  expect(screen.getAllByText(reason)).toHaveLength(1);
  // The field the server judged is marked, with a line that says why.
  expect(capacity.getAttribute('aria-invalid')).toBe('true');
  expect(within(runtime).getByText('The server refused this field when it was last saved.')).toBeTruthy();
  // A refusal is not a save: no Saved tag, and the edit is still there to fix.
  expect(within(runtime).queryByText('Saved')).toBeNull();
  expect(capacity.value).toBe('5000');

  // The page reloads the record after a refused save, and the Save comes back
  // only once that reload is done; the reason is still there after it.
  await waitFor(() => expect(save.disabled).toBe(false));
  expect(vi.mocked(listAgents).mock.calls.length).toBeGreaterThan(reads);
  expect(within(runtime).getByText(reason)).toBeTruthy();

  // The next edit retires the refusal and the mark.
  fireEvent.change(capacity, { target: { value: '1000' } });
  expect(within(runtime).queryByText(reason)).toBeNull();
  expect(capacity.getAttribute('aria-invalid')).toBeNull();
});
