/** A real authenticated non-admin owns the scheduled Session, not the Agent's administrator. */
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { documentsByField } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { deployedApiClient, issueToken, json, requiredEnvironment } from '../fixtures/oidcMachine';
import { PlatformApi, type DeploymentRunRecord } from '../fixtures/platformApi';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

let member: APIRequestContext | undefined;
let agentId = '';
let deploymentId = '';
let sessionId = '';
let createdAssistantId = '';
let sourceSessionId = '';
const planIds = new Set<string>();
const DEFAULT_FIELDS = [
  'environment_name', 'model_name', 'instructions', 'skills',
  'mcp_servers', 'packages', 'engine_options', 'credential_vault_ids',
] as const;
let defaultsBefore: Record<string, unknown> | undefined;

// Preserve failed scenes, but never leave a retained plan enabled.
test.afterEach(async () => {
  if (member && agentId) {
    const api = new AstraApi(member);
    // The tool may have committed before the Assistant's reply failed.
    const plans = await api.data<Array<{ deployment_id: string; agent_id?: string }>>('GET', '/me/schedules');
    for (const plan of plans.filter((row) => row.agent_id === agentId)) {
      planIds.add(plan.deployment_id);
      await api.data('PUT', `/me/schedules/${plan.deployment_id}`, { enabled: false });
      const runs = await api.data<DeploymentRunRecord[]>('GET', `/me/schedules/${plan.deployment_id}/runs`);
      for (const run of runs) {
        if (run.session_id && !sessions.includes(run.session_id)) sessions.push(run.session_id);
      }
    }
  }
});
test.afterEach(async ({ request }) => {
  if (defaultsBefore) {
    await new AstraApi(request).data('PUT', '/admin/assistant-defaults', defaultsBefore);
  }
});
onPassOnly(async ({ request }) => {
  if (member) {
    const api = new AstraApi(member);
    for (const id of planIds) await api.data('DELETE', `/me/schedules/${id}`);
    for (const id of new Set([...sessions, sourceSessionId, sessionId].filter(Boolean))) await api.deleteSession(id);
    sessions.length = 0;
    if (createdAssistantId) await api.deleteAssistant(createdAssistantId);
  }
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});
const sessions = trackSessions();
test.afterEach(async () => { await member?.dispose(); });

test('an Assistant creates a personal schedule that fires as its owner and refuses another account', async ({
  request, playwright,
}, info) => {
  const startedAt = Date.now();
  const remaining = () => {
    const budget = 155_000 - (Date.now() - startedAt);
    expect(budget, 'Assistant creation and cron execution share the fixed test budget').toBeGreaterThan(0);
    return budget;
  };
  const adminApi = new AstraApi(request);
  const environmentName = await adminApi.assistantEnvironmentName();
  const modelName = await adminApi.assistantModelName(environmentName);
  const defaults = await adminApi.data<Record<string, unknown>>('GET', '/admin/assistant-defaults');
  defaultsBefore = Object.fromEntries(DEFAULT_FIELDS.map((field) => [field, defaults[field] ?? null]));
  await adminApi.data('PUT', '/admin/assistant-defaults', { ...defaultsBefore, model_name: modelName });
  const issuerRequest = await playwright.request.newContext({ storageState: { cookies: [], origins: [] } });
  try {
    const discovery = await json(await issuerRequest.get(
      `${requiredEnvironment('ASTRABOX_E2E_OIDC_ISSUER')}/.well-known/openid-configuration`,
    ));
    const { clientId, clientSecret } = deployedApiClient(requireServiceContainer(SERVER_CONTAINER_HANDLE));
    const token = await issueToken(issuerRequest, String(discovery.token_endpoint), clientId, clientSecret,
      'astrabox:read astrabox:write');
    member = await playwright.request.newContext({
      baseURL: requiredEnvironment('ASTRABOX_E2E_CONSOLE_URL'),
      storageState: { cookies: [], origins: [] },
      extraHTTPHeaders: { Authorization: `Bearer ${token.value}` },
    });
  } finally {
    await issuerRequest.dispose();
  }
  const api = new AstraApi(member!);
  const admin = await adminApi.data<{ user_id: string; is_admin: boolean }>('GET', '/user/current');
  const owner = await api.data<{ user_id: string; is_admin: boolean }>('GET', '/user/current');
  expect([admin.is_admin, owner.is_admin]).toEqual([true, false]);
  expect(owner.user_id).toBeTruthy();
  expect(owner.user_id).not.toBe(admin.user_id);

  const priorAssistants = await api.data<Array<{ assistant_id: string }>>('GET', '/assistants');
  const assistant = await api.data<{ assistant_id: string }>('POST', '/assistants/primary', {
    display_name: 'Assistant', timezone: 'UTC',
  });
  if (!priorAssistants.some((row) => row.assistant_id === assistant.assistant_id)) {
    createdAssistantId = assistant.assistant_id;
  }
  const id = randomUUID();
  agentId = (await adminApi.createColdTestAgent(`__e2e_personal_schedule_${id}`)).agent_id;
  await adminApi.setAgentAccess(agentId, {
    visibility: 'allowlist', admins: [], allowed_user_ids: [owner.user_id],
  });
  const marker = `PERSONAL_${id.replace(/-/g, '')}`;
  const schedule = { cron: '* * * * *', timezone: 'UTC' };
  const assistantRecord = documentsByField('assistant_catalog', '$.assistant_id', assistant.assistant_id)[0];
  expect(assistantRecord.owner_id).toBe(owner.user_id);
  const ownerOrgId = String(assistantRecord.org_id || '');
  expect(ownerOrgId, 'the pre-existing Assistant identity supplies the organization oracle').not.toBe('');
  sourceSessionId = (await api.startAssistantConversation(assistant.assistant_id)).session_id;
  sessions.push(sourceSessionId);
  await api.waitForSessionReady(sourceSessionId, Math.min(90_000, remaining()));
  const created = await api.sendTurn(sourceSessionId, [
    'Create exactly one personal recurring Agent schedule using your create_schedule tool.',
    `agent_id: ${agentId}`,
    `name: Personal report ${id}`,
    `prompt: Reply with exactly ${marker}. Do not use tools.`,
    'schedule: {"cron":"* * * * *","timezone":"UTC"}',
    'Call create_schedule now and report the result. Do not use terminal, curl or A2A to create it.',
    'Do not run the Agent yourself, run the schedule manually, or change or disable the created schedule.',
  ].join('\n'), remaining());
  await info.attach('assistant-create-schedule-reply', {
    body: JSON.stringify(created), contentType: 'application/json',
  });
  const matching = (await api.data<Array<Record<string, unknown>>>('GET', '/me/schedules'))
    .filter((row) => row.agent_id === agentId && row.source_assistant_id === assistant.assistant_id
      && String(row.prompt_prefix || '').includes(marker));
  for (const row of matching) planIds.add(String(row.deployment_id));
  expect(created.errorText).toBeNull();
  expect(created.toolNames.filter((name) => /(?:^|_)create_schedule$/.test(name)),
    JSON.stringify(created.toolNames)).toHaveLength(1);
  expect(matching, 'find the tool-created plan by independent task marker, not the model reply').toHaveLength(1);
  const plan = matching[0];
  deploymentId = String(plan.deployment_id);
  const storedPlan = documentsByField('deployments', '$.deployment_id', deploymentId)[0];
  expect(storedPlan).toMatchObject({
    owner_user_id: owner.user_id, owner_org_id: ownerOrgId,
    agent_id: agentId, source_assistant_id: assistant.assistant_id, schedule,
  });
  expect(plan).toMatchObject({
    scope: 'personal', owner_user_id: owner.user_id, owner_org_id: ownerOrgId, created_by: owner.user_id,
    agent_id: agentId, source_assistant_id: assistant.assistant_id, schedule,
  });
  expect(plan.assistant_id ?? '', 'the source Assistant is not the execution target').toBe('');
  expect((await api.data<Array<{ deployment_id: string }>>('GET', '/me/schedules'))
    .some((row) => row.deployment_id === deploymentId)).toBe(true);
  expect((await adminApi.data<Array<{ deployment_id: string }>>('GET', '/me/schedules'))
    .some((row) => row.deployment_id === deploymentId)).toBe(false);
  expect((await new PlatformApi(request).listDeployments(agentId))
    .some((row) => row.deployment_id === deploymentId)).toBe(false);
  for (const [method, route, data] of [
    ['GET', `/me/schedules/${deploymentId}/runs`, undefined],
    ['PUT', `/me/schedules/${deploymentId}`, { enabled: false }],
    ['DELETE', `/me/schedules/${deploymentId}`, undefined],
    ['POST', `/me/schedules/${deploymentId}/runs`, undefined],
    ['PUT', `/admin/agents/${agentId}/deployments/${deploymentId}`, { enabled: false }],
  ] as const) {
    const denied = await request.fetch(apiPath(route), { method, data });
    expect(denied.status(), `${method} ${route}: ${await denied.text()}`).toBe(404);
  }

  let completed: DeploymentRunRecord | undefined;
  let firstRunId = '';
  await expect.poll(async () => {
    const runs = await api.data<DeploymentRunRecord[]>('GET', `/me/schedules/${deploymentId}/runs`);
    expect(runs.filter((run) => ['FAILED', 'CANCELLED'].includes(run.status)), JSON.stringify(runs)).toEqual([]);
    expect(runs.every((run) => run.trigger === 'schedule'), 'only automatic cron invocations are allowed').toBe(true);
    const run = firstRunId ? runs.find((row) => row.run_id === firstRunId)
      : runs.filter((row) => row.trigger === 'schedule')
        .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)))[0];
    if (run && !firstRunId) firstRunId = run.run_id;
    if (run?.session_id && !sessionId) {
      sessionId = String(run.session_id);
      sessions.push(sessionId);
    }
    completed = run;
    if (run?.status === 'COMPLETED' && sessionId && run.turn_id) {
      const settled = await api.getSession(sessionId);
      return settled.last_turn_id === run.turn_id && settled.last_turn_status === 'COMPLETED'
        ? 'COMPLETED' : 'RUNNING';
    }
    return run?.status;
  }, { timeout: remaining(), intervals: [1000], message: 'the untouched personal cron must finish its real Agent turn' })
    .toBe('COMPLETED');
  expect(sessionId).not.toBe('');
  expect(completed?.scheduled_for).toBeTruthy();
  const persisted = documentsByField('sessions', '$.session_id', sessionId)[0];
  expect(persisted).toMatchObject({ user_id: owner.user_id, agent_id: agentId });
  expect(persisted.user_id).not.toBe(admin.user_id);
  const messages = await api.getMessages(sessionId, 50);
  expect(messages.messages.filter((row) => row.role === 'assistant').map(messageText).join('\n')).toContain(marker);
  expect(messages.messages.filter((row) => row.role === 'user').map(messageText)).toEqual([
    `Reply with exactly ${marker}. Do not use tools.`,
  ]);
  await info.attach('personal-schedule-evidence', {
    body: JSON.stringify({ plan, storedPlan, sourceSessionId, created, ownerOrgId,
      run: completed, ownerUserId: owner.user_id,
      agentOwnerUserId: admin.user_id, sessionOwnerUserId: persisted.user_id, sessionId, marker }),
    contentType: 'application/json',
  });

  // Losing target use access blocks new work, without taking away ownership of the plan.
  await adminApi.setAgentAccess(agentId, { visibility: 'private', admins: [], allowed_user_ids: [] });
  for (const route of [`/me/schedules/${deploymentId}/runs`,
    `/me/schedules/${deploymentId}/runs/${completed!.run_id}/replay`]) {
    const refused = await member!.post(apiPath(route));
    expect(refused.status(), await refused.text()).toBe(404);
  }
  expect((await api.data<Array<{ deployment_id: string }>>('GET', '/me/schedules'))
    .some((row) => row.deployment_id === deploymentId)).toBe(true);
  await api.data('PUT', `/me/schedules/${deploymentId}`, { enabled: false });
});
