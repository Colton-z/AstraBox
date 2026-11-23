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
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { SWRConfig } from 'swr';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

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
  listAgentVersions,
  listAgents,
  listConnectors,
} from '@/api';
import type {
  AgentConfig,
  AgentPreparedRuntimeStatus,
  AgentVersionRecord,
  FormSchema,
} from '@/types';

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
  listAgentVersions: vi.fn(),
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
  vi.mocked(listAgentVersions).mockReset().mockResolvedValue({
    current_version: RECORD.version!,
    versions: [],
    has_more: false,
  });
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

/** Moves the mounted page to another Agent, as a link in the console would. */
function OpenAgent({ agentId }: { agentId: string }) {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(`/manage/agents/${agentId}`)}>
      {`open ${agentId}`}
    </button>
  );
}

function show(agentId = 'agent-1', next?: string) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <MemoryRouter initialEntries={[`/manage/agents/${agentId}`]}>
        {next && <OpenAgent agentId={next} />}
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

/**
 * One prepared-capacity card, beside the version history, on a warm Agent.
 *
 * Both cards are keyed by the Agent so that each starts afresh when the page
 * moves to another Agent. Keyed by the bare Agent ID they were two siblings
 * with one key, and every render of the page after it loaded left the previous
 * status card in the page and mounted another that started reading again.
 */
describe('prepared capacity beside the version history', () => {
  const WARM: AgentConfig = { ...RECORD, prepared_capacity: 2 };
  const OTHER: AgentConfig = {
    ...RECORD,
    agent_id: 'agent-2',
    name: 'drafting',
    prepared_capacity: 3,
    version: 5,
  };
  const status = (target: number, prepared: number): AgentPreparedRuntimeStatus => ({
    environment_name: 'astrabox',
    prewarm_enabled: true,
    configured_target: target,
    enabled: true,
    target,
    ready: prepared > 0,
    prepared_count: prepared,
    state: 'prepared',
  });
  const edit = (agentId: string, version: number, field: string): AgentVersionRecord => ({
    agent_id: agentId,
    version,
    operation: 'update',
    changed_fields: [field],
    author_user_id: 'owner',
    created_at: '2026-11-18T08:00:00+00:00',
  });
  const HISTORY: Record<string, AgentVersionRecord[]> = {
    'agent-1': [edit('agent-1', 3, 'prepared_capacity')],
    'agent-2': [edit('agent-2', 5, 'system')],
  };

  let errors: MockInstance<typeof console.error>;
  beforeEach(() => {
    errors = vi.spyOn(console, 'error');
    vi.mocked(listAgents).mockResolvedValue([WARM, OTHER]);
    vi.mocked(getAgentPreparedRuntime).mockImplementation(async (agentId) =>
      agentId === 'agent-2' ? status(3, 1) : status(2, 2));
    vi.mocked(listAgentVersions).mockImplementation(async (agentId) => ({
      current_version: HISTORY[agentId][0].version,
      versions: HISTORY[agentId],
      has_more: false,
    }));
  });
  afterEach(() => {
    errors.mockRestore();
  });

  const statusCards = () => document.querySelectorAll('[data-testid="agent-prewarm-status"]');
  /** The one status card, asserted to be the only one on the page. */
  const onlyStatusCard = () => {
    expect(statusCards()).toHaveLength(1);
    return statusCards()[0] as HTMLElement;
  };
  /** The version history card, asserted to be the only one on the page. */
  const history = () => {
    expect(screen.getAllByRole('heading', { name: 'Version history' })).toHaveLength(1);
    return card('Version history');
  };
  const sameKeyWarnings = () =>
    errors.mock.calls.filter((call) => String(call[0]).includes('same key'));

  it('shows one live status card on load, after it refreshes and after the page renders again', async () => {
    show();
    expect(await screen.findByText('Available: 2 of 2')).toBeTruthy();
    expect(within(onlyStatusCard()).getByTestId('prewarm-state').textContent)
      .toBe('New conversations start warm while prepared capacity remains.');
    // The version history reads beside it, its changed fields named as their cards name them.
    expect(await within(history()).findByText('Prepared capacity')).toBeTruthy();
    expect(within(history()).getByText('Edited')).toBeTruthy();

    // The card refreshes its own observation.
    vi.mocked(getAgentPreparedRuntime).mockResolvedValue(status(2, 1));
    fireEvent.click(within(onlyStatusCard()).getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByText('Available: 1 of 2')).toBeTruthy();
    expect(onlyStatusCard().textContent).toContain('Available: 1 of 2');

    // An edit renders the whole page again; the card it holds is the one that was there.
    const capacity = screen.getByRole('spinbutton', { name: 'Prepared capacity' }) as HTMLInputElement;
    await waitFor(() => expect(capacity.value).toBe('2'));
    fireEvent.change(capacity, { target: { value: '4' } });
    expect(await within(onlyStatusCard()).findByText(/^Save or revert your changes before repreparing/)).toBeTruthy();
    expect(onlyStatusCard().textContent).toContain('Available: 1 of 2');
    fireEvent.click(within(card('Runtime and availability')).getByRole('button', { name: 'Revert' }));
    await waitFor(() => expect(capacity.value).toBe('2'));
    expect(onlyStatusCard().textContent).toContain('Available: 1 of 2');

    // The history still reads, and the settings hold one capacity field.
    expect(within(history()).getByText('Edited')).toBeTruthy();
    expect(screen.getAllByRole('spinbutton', { name: 'Prepared capacity' })).toHaveLength(1);
    expect(sameKeyWarnings()).toEqual([]);
  });

  it('follows the Agent the page moves to', async () => {
    show('agent-1', 'agent-2');
    expect(await screen.findByText('Available: 2 of 2')).toBeTruthy();
    expect(await within(history()).findByText('Prepared capacity')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'open agent-2' }));
    expect(await screen.findByText('Available: 1 of 3')).toBeTruthy();
    expect(onlyStatusCard().textContent).not.toContain('2 of 2');
    expect(getAgentPreparedRuntime).toHaveBeenLastCalledWith('agent-2');
    // The history is the second Agent's, and the first Agent's is gone.
    expect(await within(history()).findByText('System prompt')).toBeTruthy();
    expect(within(history()).queryByText('Prepared capacity')).toBeNull();
    expect(vi.mocked(listAgentVersions).mock.calls.at(-1)?.[0]).toBe('agent-2');
    expect(sameKeyWarnings()).toEqual([]);
  });
});
