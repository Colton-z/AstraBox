// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type {
  AgentDeployment,
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
let createCalls: Array<{ agentId: string; payload: Record<string, unknown> }> = [];
let updateCalls: Array<{
  agentId: string;
  deploymentId: string;
  patch: Record<string, unknown>;
}> = [];
let triggerCalls: Array<{ agentId: string; deploymentId: string }> = [];
let replayCalls: Array<{ agentId: string; deploymentId: string; runId: string }> = [];

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
      return { ...deployment, ...payload };
    },
    updateAgentDeployment: async (
      agentId: string,
      deploymentId: string,
      patch: Record<string, unknown>,
    ) => {
      updateCalls.push({ agentId, deploymentId, patch });
      deployment = {
        ...deployment,
        ...patch,
        schedule:
          (patch.schedule as AgentDeployment['schedule'] | undefined) ??
          deployment.schedule,
      };
      return deployment;
    },
    triggerDeploymentRun: async (agentId: string, deploymentId: string) => {
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
      agentId: string,
      deploymentId: string,
      runId: string,
    ) => {
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
    deleteAgentDeployment: async () => ({}),
  };
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
