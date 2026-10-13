// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { ApiError } from '@/api';
import type { AssistantRecord } from '@/assistant/types';
import type {
  AgentDeployment,
  ChannelAccountLink,
  ChannelProviderDescriptor,
  DeploymentRun,
} from '@/types';

const channelProviders: ChannelProviderDescriptor[] = [
  {
    name: 'generic_json',
    label: 'Generic JSON webhook',
    scene: 'channel:generic_json',
    config_fields: [],
    credential_fields: [],
    callback_path: null,
    setup_url: null,
    documentation_url: null,
    supports_source: false,
    uses_trigger_secret: true,
  },
  {
    name: 'telegram',
    label: 'Telegram',
    scene: 'channel:telegram',
    config_fields: [],
    credential_fields: [
      {
        key: 'token',
        label: 'Bot token',
        required: true,
        secret: true,
        kind: 'string',
        options: [],
        help: 'Create the bot with BotFather.',
      },
    ],
    callback_path: null,
    setup_url: 'https://t.me/BotFather',
    documentation_url: 'https://astrabox.ai/docs/channels',
    supports_source: true,
    uses_trigger_secret: false,
  },
  {
    name: 'line',
    label: 'LINE',
    scene: 'channel:line',
    config_fields: [],
    credential_fields: [
      {
        key: 'token',
        label: 'Channel access token',
        required: true,
        secret: true,
        kind: 'string',
        options: [],
      },
      {
        key: 'secret',
        label: 'Channel secret',
        required: true,
        secret: true,
        kind: 'string',
        options: [],
      },
    ],
    callback_path: '/line',
    setup_url: 'https://developers.line.biz/console/',
    documentation_url: 'https://astrabox.ai/docs/channels',
    supports_source: true,
    uses_trigger_secret: false,
  },
];

let deployment: AgentDeployment;
let runs: DeploymentRun[];
let assistants: AssistantRecord[] = [];
let saveError: Error | null = null;
let createCalls: Array<{ agentId?: string; assistantId?: string; payload: Record<string, unknown> }> = [];
let updateCalls: Array<{
  agentId: string;
  assistantId?: string;
  deploymentId: string;
  patch: Record<string, unknown>;
}> = [];
let triggerCalls: Array<{ agentId: string; deploymentId: string }> = [];
let replayCalls: Array<{ agentId: string; deploymentId: string; runId: string }> = [];
let links: ChannelAccountLink[] = [];

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return {
    ...actual,
    listAgents: async () => [{ agent_id: 'agent-1', name: 'Analyst' }],
    listChannelProviders: async () => channelProviders,
    listDeployments: async () => [{ ...deployment, agent_name: 'Analyst' }],
    listAgentDeployments: async () => {
      throw new Error('the console must not fan out through per-Agent deployment lists');
    },
    listDeploymentRuns: async () => runs,
    createAgentDeployment: async (agentId: string, payload: Record<string, unknown>) => {
      createCalls.push({ agentId, payload });
      if (saveError) throw saveError;
      return { ...deployment, ...payload };
    },
    createAssistantDeployment: async (assistantId: string, payload: Record<string, unknown>) => {
      createCalls.push({ assistantId, payload });
      if (saveError) throw saveError;
      return { ...deployment, ...payload, agent_id: undefined, assistant_id: assistantId };
    },
    updateDeployment: async (
      owner: { agent_id?: string; assistant_id?: string },
      deploymentId: string,
      patch: Record<string, unknown>,
    ) => {
      updateCalls.push({
        agentId: owner.agent_id ?? '',
        ...(owner.assistant_id ? { assistantId: owner.assistant_id } : {}),
        deploymentId,
        patch,
      });
      if (saveError) throw saveError;
      deployment = {
        ...deployment,
        ...patch,
        schedule:
          (patch.schedule as AgentDeployment['schedule'] | undefined) ??
          deployment.schedule,
      };
      return deployment;
    },
    triggerDeploymentRun: async (owner: { agent_id?: string }, deploymentId: string) => {
      const agentId = owner.agent_id ?? '';
      triggerCalls.push({ agentId, deploymentId });
      const created: DeploymentRun = {
        run_id: 'run-manual',
        deployment_id: deploymentId,
        agent_id: agentId,
        trigger: 'manual',
        status: 'QUEUED',
      };
      runs = [created, ...runs];
      return created;
    },
    replayDeploymentRun: async (
      owner: { agent_id?: string },
      deploymentId: string,
      runId: string,
    ) => {
      const agentId = owner.agent_id ?? '';
      replayCalls.push({ agentId, deploymentId, runId });
      const created: DeploymentRun = {
        run_id: 'run-replay',
        deployment_id: deploymentId,
        agent_id: agentId,
        trigger: 'replay',
        replayed_from_run_id: runId,
        status: 'QUEUED',
      };
      runs = [created, ...runs];
      return created;
    },
    deleteDeployment: async () => ({}),
    listDeploymentAccountLinks: async () => links,
    linkDeploymentAccount: async (
      _agentId: string,
      _deploymentId: string,
      link: { participant: string; user_id: string },
    ) => {
      const stored = { ...link, linked_by: 'admin', linked_at: '2026-10-10T00:00:00Z' };
      links = [...links.filter((row) => row.participant !== link.participant), stored];
      return stored;
    },
  };
});

vi.mock('@/assistant/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/assistant/api')>();
  return { ...actual, listAssistants: async () => assistants };
});

const { default: DeploymentCreatePage } = await import('./DeploymentCreatePage');
const { default: DeploymentDetailPage } = await import('./DeploymentDetailPage');

afterEach(async () => {
  cleanup();
  await i18n.changeLanguage('en');
});
beforeAll(async () => {
  await i18n.changeLanguage('en');
});
beforeEach(() => {
  for (const provider of channelProviders) delete provider.can_configure_execution_user;
  deployment = {
    deployment_id: 'deployment-1',
    agent_id: 'agent-1',
    scene: 'schedule',
    name: 'Daily report',
    prompt_prefix: 'Prepare the report',
    schedule: { cron: '0 9 * * *', timezone: 'UTC' },
    enabled: true,
  };
  runs = [
    {
      run_id: 'run-1',
      deployment_id: 'deployment-1',
      agent_id: 'agent-1',
      trigger: 'schedule',
      status: 'COMPLETED',
      session_id: 'session-1',
      created_at: '2026-08-13T09:00:00+00:00',
    },
  ];
  createCalls = [];
  updateCalls = [];
  triggerCalls = [];
  replayCalls = [];
  links = [];
  assistants = [];
  saveError = null;
});

function renderCreate() {
  return render(
    <MemoryRouter initialEntries={['/manage/deployments/new']}>
      <Routes>
        <Route path="/manage/deployments/new" element={<DeploymentCreatePage />} />
        <Route path="/manage/deployments/:deploymentId" element={<div>created</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={['/manage/deployments/deployment-1']}>
      <Routes>
        <Route
          path="/manage/deployments/:deploymentId"
          element={<DeploymentDetailPage />}
        />
        <Route path="/manage/sessions/:sessionId" element={<div>Session opened</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function runRow(runId: string): HTMLTableRowElement {
  const row = screen.getByText(runId).closest('tr');
  if (!row) throw new Error(`Run ${runId} is not inside a table row`);
  return row;
}

describe('Deployment management', () => {
  it('creates a named schedule with cron and timezone as one definition', async () => {
    renderCreate();

    fireEvent.change(await screen.findByLabelText(/Name/), {
      target: { value: 'Morning report' },
    });
    fireEvent.change(screen.getByLabelText(/Prompt/), {
      target: { value: 'Summarize yesterday' },
    });
    fireEvent.change(screen.getByLabelText(/Cron expression/), {
      target: { value: '30 8 * * 1-5' },
    });
    fireEvent.change(screen.getByLabelText(/Timezone/), {
      target: { value: 'America/Los_Angeles' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(createCalls).toHaveLength(1));
    expect(createCalls[0]).toEqual({
      agentId: 'agent-1',
      payload: {
        scene: 'schedule',
        name: 'Morning report',
        prompt_prefix: 'Summarize yesterday',
        schedule: {
          cron: '30 8 * * 1-5',
          timezone: 'America/Los_Angeles',
        },
      },
    });
  });

  it('creates a concrete Telegram binding with write-only credentials', async () => {
    renderCreate();

    fireEvent.change(await screen.findByLabelText(/Trigger/), {
      target: { value: 'channel:telegram' },
    });
    fireEvent.change(await screen.findByLabelText(/Bot token/), {
      target: { value: 'telegram-secret' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(createCalls).toHaveLength(1));
    expect(createCalls[0]).toEqual({
      agentId: 'agent-1',
      payload: {
        scene: 'channel:telegram',
        name: undefined,
        prompt_prefix: undefined,
        attention_policy: 'all',
        response_instruction: null,
        channel_config: {},
        credentials: { token: 'telegram-secret' },
        schedule: undefined,
      },
    });
  });

  it('requires the selected platform credentials before creation', async () => {
    renderCreate();

    fireEvent.change(await screen.findByLabelText(/Trigger/), {
      target: { value: 'channel:telegram' },
    });

    expect(
      screen.getByRole('button', { name: 'Create' }).hasAttribute('disabled'),
    ).toBe(true);
    expect(screen.getByText(/Enter Bot token/)).toBeTruthy();
    expect(createCalls).toEqual([]);
  });

  it('shows the concrete provider callback and never returns credentials', async () => {
    deployment = {
      ...deployment,
      scene: 'channel:line',
      name: undefined,
      secret: undefined,
      schedule: undefined,
      channel_config: {},
      callback_base_url:
        'http://localhost:3000/api/v1/deployments/deployment-1/callback',
      credentials_configured: true,
    };

    renderDetail();

    await screen.findByRole('heading', { name: 'LINE' });
    expect(
      screen.getByText(
        'http://localhost:3000/api/v1/deployments/deployment-1/callback/line',
      ),
    ).toBeTruthy();
    expect(screen.getByRole('link', { name: /Open official console/ })).toBeTruthy();
    expect(screen.getByText('Configured')).toBeTruthy();
    expect(screen.queryByText('Shared secret')).toBeNull();
  });

  it('renders the generic webhook product label instead of its registry key', async () => {
    deployment = {
      ...deployment,
      scene: 'channel:generic_json',
      name: undefined,
      schedule: undefined,
      channel_config: {},
    };

    renderDetail();

    await screen.findByRole('heading', { name: 'Generic JSON webhook' });
    expect(screen.queryByText('generic_json', { exact: true })).toBeNull();
  });

  it('rotates all write-only provider credentials as one update', async () => {
    deployment = {
      ...deployment,
      scene: 'channel:line',
      name: undefined,
      schedule: undefined,
      channel_config: {},
      credentials_configured: true,
    };
    renderDetail();

    fireEvent.change(await screen.findByLabelText(/Channel access token/), {
      target: { value: 'next-token' },
    });
    fireEvent.change(screen.getByLabelText(/Channel secret/), {
      target: { value: 'next-secret' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(updateCalls).toHaveLength(1));
    expect(updateCalls[0]).toEqual({
      agentId: 'agent-1',
      deploymentId: 'deployment-1',
      patch: {
        attention_policy: 'all',
        response_instruction: null,
        channel_config: {},
        credentials: { token: 'next-token', secret: 'next-secret' },
      },
    });
  });

  it('offers Run now and replay as separate Run identities', async () => {
    renderDetail();
    await screen.findByRole('heading', { name: 'Daily report' });

    fireEvent.click(screen.getByRole('button', { name: 'Replay' }));
    await waitFor(() =>
      expect(replayCalls).toEqual([
        {
          agentId: 'agent-1',
          deploymentId: 'deployment-1',
          runId: 'run-1',
        },
      ]),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Run now' }));
    await waitFor(() =>
      expect(triggerCalls).toEqual([
        { agentId: 'agent-1', deploymentId: 'deployment-1' },
      ]),
    );
  });

  it('only gives Session-backed Runs the semantics and behaviour of an openable row', async () => {
    runs = [
      { ...runs[0], run_id: 'run-with-session' },
      {
        ...runs[0],
        run_id: 'run-without-session',
        session_id: undefined,
        status: 'QUEUED',
      },
    ];
    renderDetail();

    await screen.findByText('run-with-session');
    const sessionRow = runRow('run-with-session');
    const noSessionRow = runRow('run-without-session');

    expect(sessionRow.getAttribute('tabindex')).toBe('0');
    expect(sessionRow.getAttribute('aria-description')).toBe('Open record');
    expect(sessionRow.getAttribute('aria-keyshortcuts')).toBe('Enter Space');
    expect(sessionRow.classList.contains('console-row--click')).toBe(true);
    expect(sessionRow.querySelector('[aria-label="Open record"]')).not.toBeNull();
    expect(sessionRow.querySelector('.console-row-chevron')).not.toBeNull();

    expect(noSessionRow.hasAttribute('tabindex')).toBe(false);
    expect(noSessionRow.hasAttribute('aria-description')).toBe(false);
    expect(noSessionRow.hasAttribute('aria-keyshortcuts')).toBe(false);
    expect(noSessionRow.classList.contains('console-row--click')).toBe(false);
    expect(noSessionRow.querySelector('[aria-label="Open record"]')).toBeNull();
    expect(noSessionRow.querySelector('.console-row-chevron')).toBeNull();

    fireEvent.click(noSessionRow);
    fireEvent.keyDown(noSessionRow, { key: 'Enter' });
    fireEvent.keyDown(noSessionRow, { key: ' ' });
    expect(screen.queryByText('Session opened')).toBeNull();

    fireEvent.keyDown(sessionRow, { key: 'Enter' });
    await screen.findByText('Session opened');
  });

  it.each([
    [
      'en',
      { schedule: 'Schedule', manual: 'Manual', replay: 'Replay' },
      'Unknown',
    ],
    [
      'zh',
      { schedule: '定时计划', manual: '手动触发', replay: '重放' },
      '未知',
    ],
  ] as const)(
    'renders %s Run trigger labels without exposing known wire values',
    async (language, labels, unknownLabel) => {
      await i18n.changeLanguage(language);
      runs = [
        { ...runs[0], run_id: 'run-schedule', trigger: 'schedule' },
        { ...runs[0], run_id: 'run-manual', trigger: 'manual' },
        { ...runs[0], run_id: 'run-replay', trigger: 'replay' },
        {
          ...runs[0],
          run_id: 'run-unknown',
          trigger: 'future_event' as DeploymentRun['trigger'],
        },
      ];
      renderDetail();

      await screen.findByText('run-schedule');
      for (const [wireValue, label] of Object.entries(labels)) {
        const triggerCell = runRow(`run-${wireValue}`).cells[1];
        expect(within(triggerCell).getByText(label, { exact: true })).toBeTruthy();
        expect(within(triggerCell).queryByText(wireValue, { exact: true })).toBeNull();
      }

      const unknownCell = runRow('run-unknown').cells[1];
      const verbatimValue = within(unknownCell).getByText('future_event', {
        exact: true,
      });
      expect(unknownCell.textContent).toBe(`${unknownLabel} future_event`);
      expect(verbatimValue.getAttribute('data-slot')).toBe('verbatim');
      expect(verbatimValue.classList.contains('font-mono')).toBe(true);
    },
  );

  it('edits the prompt and calendar as one schedule definition', async () => {
    renderDetail();
    await screen.findByDisplayValue('Daily report');

    fireEvent.change(screen.getByLabelText(/Name/), {
      target: { value: 'Weekday report' },
    });
    fireEvent.change(screen.getByLabelText(/Cron expression/), {
      target: { value: '30 8 * * 1-5' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(updateCalls).toHaveLength(1));
    expect(updateCalls[0]).toEqual({
      agentId: 'agent-1',
      deploymentId: 'deployment-1',
      patch: {
        name: 'Weekday report',
        prompt_prefix: 'Prepare the report',
        schedule: { cron: '30 8 * * 1-5', timezone: 'UTC' },
      },
    });
  });

  it('refreshes an active Run until its Session-backed status settles', async () => {
    runs = [{ ...runs[0], status: 'RUNNING' }];
    renderDetail();
    await screen.findByText('RUNNING');

    runs = [{ ...runs[0], status: 'COMPLETED' }];

    await screen.findByText('COMPLETED', {}, { timeout: 3_000 });
  });
});


describe('channel response policy', () => {
  function channelDeployment(owner: 'agent' | 'assistant' = 'agent') {
    deployment = {
      ...deployment,
      agent_id: owner === 'agent' ? 'agent-1' : undefined,
      assistant_id: owner === 'assistant' ? 'assistant-1' : undefined,
      scene: 'channel:generic_json',
      name: undefined,
      schedule: undefined,
      channel_config: {},
      allowed_participants: ['sender-1'],
    };
  }

  async function createChannel(owner: 'agent' | 'assistant' = 'agent') {
    if (owner === 'assistant') {
      assistants = [{
        assistant_id: 'assistant-1',
        owner_id: 'user-1',
        display_name: 'Personal assistant',
        icon: null,
        description: null,
        engine_kind: 'claude-code',
        environment_name: 'default',
        permission_mode_default: 'default',
      }];
    }
    renderCreate();
    fireEvent.change(await screen.findByLabelText(/Trigger/), {
      target: { value: 'channel:generic_json' },
    });
    if (owner === 'assistant') {
      fireEvent.change(screen.getByLabelText(/Agent or Assistant/), {
        target: { value: 'assistant:assistant-1' },
      });
      fireEvent.change(screen.getByLabelText(/Allowed senders/), {
        target: { value: 'sender-1' },
      });
    }
  }

  function selectPolicy(policy: NonNullable<AgentDeployment['attention_policy']>) {
    fireEvent.change(screen.getByRole('combobox', { name: 'Response policy' }), {
      target: { value: policy },
    });
  }

  it('hides response controls for schedules on both pages', async () => {
    renderCreate();
    await screen.findByLabelText(/Cron expression/);
    expect(screen.queryByRole('combobox', { name: 'Response policy' })).toBeNull();
    expect(screen.queryByRole('textbox', { name: 'Decision instruction' })).toBeNull();
    fireEvent.change(screen.getByLabelText(/Trigger/), {
      target: { value: 'channel:generic_json' },
    });
    selectPolicy('proactive');
    fireEvent.change(screen.getByLabelText(/Trigger/), { target: { value: 'schedule' } });
    expect(screen.queryByRole('combobox', { name: 'Response policy' })).toBeNull();
    expect(screen.queryByRole('textbox', { name: 'Decision instruction' })).toBeNull();
    cleanup();

    renderDetail();
    await screen.findByDisplayValue('Daily report');
    expect(screen.queryByRole('combobox', { name: 'Response policy' })).toBeNull();
    expect(screen.queryByText('Response policy')).toBeNull();
    expect(screen.queryByRole('textbox', { name: 'Decision instruction' })).toBeNull();
  });

  it.each(['create', 'detail'] as const)(
    'shows all four policies and conditional instructions on the %s page',
    async (page) => {
      if (page === 'create') await createChannel();
      else {
        channelDeployment();
        renderDetail();
        await screen.findByRole('combobox', { name: 'Response policy' });
      }
      const select = screen.getByRole('combobox', { name: 'Response policy' });
      expect((select as HTMLSelectElement).value).toBe('all');
      for (const [policy, label] of [
        ['all', 'Answer every message'],
        ['mentions', 'Only when addressed'],
        ['participated', 'Model decides after participation'],
        ['proactive', 'Model decides proactively'],
      ] as const) {
        expect(within(select).getByRole('option', { name: new RegExp(`^${label} — .+`) })).toBeTruthy();
        selectPolicy(policy);
        if (policy === 'participated' || policy === 'proactive') {
          expect(screen.getByRole('textbox', { name: 'Decision instruction' })).toBeTruthy();
          expect(screen.getByText(
            'Tells the model when the agent should speak; the agent stays silent otherwise.',
          )).toBeTruthy();
        } else expect(screen.queryByRole('textbox', { name: 'Decision instruction' })).toBeNull();
      }
      selectPolicy('mentions');
      expect(screen.queryByRole('textbox', { name: 'Decision instruction' })).toBeNull();
    },
  );

  const saves = (['agent', 'assistant'] as const).flatMap((owner) =>
    (['all', 'mentions', 'participated', 'proactive'] as const).map((policy) => ({ owner, policy })),
  );

  it.each(saves)('creates a $owner channel with $policy and the applicable trimmed instruction', async ({ owner, policy }) => {
    await createChannel(owner);
    selectPolicy('proactive');
    fireEvent.change(screen.getByRole('textbox', { name: 'Decision instruction' }), {
      target: { value: '  Answer only when you can help. \n ' },
    });
    selectPolicy(policy);
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    await screen.findByText('created');
    expect(createCalls).toHaveLength(1);
    expect(createCalls[0][owner === 'agent' ? 'agentId' : 'assistantId']).toBe(`${owner}-1`);
    expect(createCalls[0].payload).toMatchObject({
      attention_policy: policy,
      response_instruction: policy === 'participated' || policy === 'proactive'
        ? 'Answer only when you can help.' : null,
    });
  });

  it.each(saves)('updates a $owner channel with $policy and the applicable trimmed instruction', async ({ owner, policy }) => {
    channelDeployment(owner);
    deployment.attention_policy = 'proactive';
    deployment.response_instruction = 'Stored instruction';
    renderDetail();
    const instruction = await screen.findByRole('textbox', { name: 'Decision instruction' });
    fireEvent.change(instruction, { target: { value: '  Answer only when you can help. \n ' } });
    selectPolicy(policy);
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(updateCalls).toHaveLength(1));
    expect(updateCalls[0][owner === 'agent' ? 'agentId' : 'assistantId']).toBe(`${owner}-1`);
    expect(updateCalls[0].patch).toMatchObject({
      attention_policy: policy,
      response_instruction: policy === 'participated' || policy === 'proactive'
        ? 'Answer only when you can help.' : null,
    });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save' })).toBeNull());
    expect(screen.getByText({
      all: 'Answer every message',
      mentions: 'Only when addressed',
      participated: 'Model decides after participation',
      proactive: 'Model decides proactively',
    }[policy], { exact: true })).toBeTruthy();
    if (policy === 'participated' || policy === 'proactive') {
      expect(screen.getByText('Answer only when you can help.', { exact: true, selector: 'p' })).toBeTruthy();
    } else expect(screen.queryByText('Stored instruction', { exact: true })).toBeNull();
  });

  it('reverts the policy and instruction to the stored values', async () => {
    channelDeployment();
    deployment.attention_policy = 'participated';
    deployment.response_instruction = 'Stored instruction';
    renderDetail();
    await screen.findByRole('combobox', { name: 'Response policy' });
    selectPolicy('mentions');
    fireEvent.click(screen.getByRole('button', { name: 'Revert' }));

    expect((screen.getByRole('combobox', { name: 'Response policy' }) as HTMLSelectElement).value)
      .toBe('participated');
    expect((screen.getByRole('textbox', { name: 'Decision instruction' }) as HTMLTextAreaElement).value)
      .toBe('Stored instruction');
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
    expect(updateCalls).toEqual([]);
  });

  it.each(['create', 'detail'] as const)(
    'shows a refused %s save beside the form and keeps the draft',
    async (page) => {
      if (page === 'create') await createChannel();
      else {
        channelDeployment();
        renderDetail();
        await screen.findByRole('combobox', { name: 'Response policy' });
      }
      saveError = new ApiError('CHANNEL_RESPONSE_JUDGE_UNAVAILABLE', 'No response judge is configured.', 409);
      selectPolicy('proactive');
      fireEvent.change(screen.getByRole('textbox', { name: 'Decision instruction' }), {
        target: { value: 'Keep this instruction' },
      });
      fireEvent.click(screen.getByRole('button', { name: page === 'create' ? 'Create' : 'Save' }));

      const alert = await screen.findByRole('alert');
      expect(alert.textContent).toBe(saveError.message);
      expect(alert.getAttribute('data-slot')).toBe('verbatim');
      if (page === 'detail') {
        expect(alert.closest('[data-slot="card"]')).toBe(
          screen.getByRole('textbox', { name: 'Decision instruction' }).closest('[data-slot="card"]'),
        );
      }
      expect((screen.getByRole('textbox', { name: 'Decision instruction' }) as HTMLTextAreaElement).value)
        .toBe('Keep this instruction');
      expect(screen.queryByText('created')).toBeNull();
    },
  );

  it.each(['create', 'detail'] as const)(
    'blocks instructions over 4000 characters only when applicable on the %s page',
    async (page) => {
      if (page === 'create') await createChannel();
      else {
        channelDeployment();
        renderDetail();
        await screen.findByRole('combobox', { name: 'Response policy' });
      }
      selectPolicy('proactive');
      const instruction = screen.getByRole('textbox', { name: 'Decision instruction' });
      const buttonName = page === 'create' ? 'Create' : 'Save';
      fireEvent.change(instruction, { target: { value: 'x'.repeat(4001) } });
      expect(screen.getByRole('button', { name: buttonName }).hasAttribute('disabled')).toBe(true);
      expect(screen.getByText('Decision instruction must be 4000 characters or fewer.')).toBeTruthy();
      fireEvent.change(instruction, { target: { value: 'x'.repeat(4000) } });
      expect(screen.getByRole('button', { name: buttonName }).hasAttribute('disabled')).toBe(false);
      fireEvent.change(instruction, { target: { value: 'x'.repeat(4001) } });
      selectPolicy('mentions');
      expect(screen.getByRole('button', { name: buttonName }).hasAttribute('disabled')).toBe(false);
    },
  );

  it('renders Chinese response policy labels and guidance', async () => {
    await i18n.changeLanguage('zh');
    channelDeployment();
    deployment.attention_policy = 'participated';
    renderDetail();
    const select = await screen.findByRole('combobox', { name: '回应策略' });
    for (const label of ['全部回应', '仅被提及时回应', '参与后由模型判断', '由模型主动判断']) {
      expect(within(select).getByRole('option', { name: new RegExp(`^${label} — .+`) })).toBeTruthy();
    }
    expect(screen.getByRole('textbox', { name: '判断指令' })).toBeTruthy();
    expect(screen.getByText('告诉模型 Agent 何时应该发言；其他时候 Agent 保持沉默。')).toBeTruthy();
  });
});

describe('channel execution accounts', () => {
  it('lets an administrator change the execution account through the channel editor', async () => {
    channelProviders[0].can_configure_execution_user = true;
    deployment = {
      ...deployment, scene: 'channel:generic_json', name: undefined, schedule: undefined,
      execution_mode: 'account', execution_user_id: 'alice', channel_config: {},
    };
    renderDetail();
    const account = await screen.findByRole('textbox', { name: 'Execution account user ID' });
    expect((account as HTMLInputElement).value).toBe('alice');
    fireEvent.change(account, { target: { value: 'bob' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(updateCalls).toHaveLength(1));
    expect(updateCalls[0].patch.execution_user_id).toBe('bob');
  });

  it('shows a delegated account without offering a non-administrator an editor', async () => {
    deployment = {
      ...deployment, scene: 'channel:generic_json', name: undefined, schedule: undefined,
      execution_mode: 'account', execution_user_id: 'alice', channel_config: {},
    };
    renderDetail();
    await screen.findByText('alice');
    expect(screen.queryByRole('textbox', { name: 'Execution account user ID' })).toBeNull();
  });

  it('lets an administrator link a sender on a trigger that runs as linked accounts', async () => {
    channelProviders[0].can_configure_execution_user = true;
    deployment = {
      ...deployment, scene: 'channel:generic_json', name: undefined, schedule: undefined,
      execution_mode: 'linked', execution_user_id: '', channel_config: {},
    };
    renderDetail();

    await screen.findByText('No linked senders');
    // Each sender runs as its own account, so there is no single account to enter.
    expect(screen.queryByRole('textbox', { name: 'Execution account user ID' })).toBeNull();
    fireEvent.change(screen.getByLabelText(/^Sender/), { target: { value: 'telegram:42' } });
    fireEvent.change(screen.getByLabelText(/^AstraBox user ID/), { target: { value: 'alice' } });
    fireEvent.click(screen.getByRole('button', { name: 'Link' }));

    await screen.findByText('telegram:42');
    expect(links.map(({ participant, user_id }) => ({ participant, user_id }))).toEqual([
      { participant: 'telegram:42', user_id: 'alice' },
    ]);
  });
});
