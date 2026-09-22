import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { expect, type Browser, type BrowserContext, type Page, type Response } from '@playwright/test';
import { type AgentRecord, type MessagePage } from './astraApi';
import { engineProfiles } from './engineProfile';
import { absoluteBaseUrl, apiPath, appPath } from './env';
import { resolveServiceContainer, SERVER_CONTAINER_HANDLE } from './serviceContainer';
import { WorkspaceResumeNodes } from './workspaceResumeNodes';

type Prepared = { ready: boolean; prepared_count: number; sandbox_id: string;
  client_pool_name: string; runtime_generation: string };
type Detail = { session_id: string; sandbox_id: string; workspace_id: string;
  agent_id: string; state: string };
type Environment = { name: string; engine_kind: string; enabled: boolean;
  sandbox_tenancy: string; runtime_template_name: string };

function url(origin: string, route: string): string { return `${origin}${appPath(route)}`; }

export function responseFor(page: Page, route: string, method = 'GET'): Promise<Response> {
  return page.waitForResponse((response) => !response.request().isNavigationRequest()
    && response.request().method() === method && new URL(response.url()).pathname === apiPath(route));
}

export async function data<T>(response: Response): Promise<T> {
  expect(response.status(), `browser request ${new URL(response.url()).pathname}`).toBe(200);
  const envelope = await response.json();
  expect(envelope.code).toBe('OK');
  return envelope.data as T;
}

/** The same management and conversation UI journey for each backend topology. */
export function backendBrowserFlow(sessions: string[], evidence: Record<string, unknown>, onAgent: (id: string) => void) {
  let agentId = '';
  async function createAgent(page: Page, origin: string): Promise<AgentRecord> {
    const profiles = engineProfiles().filter((profile) => profile.engine_kind === 'deepseek_harness');
    expect(profiles).toHaveLength(1);
    const profile = profiles[0];
    const sourceRead = responseFor(page, '/agents');
    await page.goto(url(origin, `/manage/agents/${profile.agent_id}`));
    const sources = (await data<AgentRecord[]>(await sourceRead))
      .filter((agent) => agent.agent_id === profile.agent_id);
    expect(sources, 'the management page must load the exact deployed DSH Agent').toHaveLength(1);
    const source = sources[0];
    const environmentsRead = responseFor(page, '/admin/environments');
    await page.goto(url(origin, '/manage/environments'));
    const environments = (await data<Environment[]>(await environmentsRead)).filter((environment) =>
      environment.enabled && environment.sandbox_tenancy === 'conversation'
      && environment.engine_kind === profile.engine_kind && environment.runtime_template_name === profile.image);
    expect(environments, 'reuse the deployed DSH conversation recipe').toHaveLength(1);
    await page.goto(url(origin, '/manage/agents/new'));
    await page.locator('#agent-new-name').fill(`distributed-prewarm-${randomUUID()}`);
    await page.locator('#agent-new-environment_name').selectOption(environments[0].name);
    const model = String(source.model || '');
    expect(model).not.toBe('');
    await page.locator('#agent-new-model').fill(model);
    await page.getByRole('option', { name: model, exact: true }).click();
    await page.getByRole('button', { name: /^Show advanced settings/ }).click();
    for (const [key, value] of Object.entries(source.engine_options || {})) {
      await page.locator(`[id="agent-new-engine_options.${key}"]`).fill(JSON.stringify(value));
    }
    await page.getByRole('switch', { name: 'Keep a sandbox ready', exact: true }).check();
    await page.locator('[role="switch"][aria-labelledby="agent-new-terminal_panel-label"]').check();
    const created = responseFor(page, '/agents', 'POST');
    await page.getByRole('button', { name: 'Create', exact: true }).click();
    const agent = await data<AgentRecord>(await created);
    agentId = agent.agent_id;
    onAgent(agentId);
    evidence.agent = { agent_id: agentId, environment_name: agent.environment_name, model: agent.model };
    await page.waitForURL(new RegExp(`/manage/agents/${agentId}$`));
    return agent;
  }

  async function prepared(page: Page, origin: string): Promise<Prepared> {
    const path = `/agents/${agentId}/prepared-runtime`;
    const read = responseFor(page, path);
    await page.goto(url(origin, `/manage/agents/${agentId}`));
    let status = await data<Prepared>(await read);
    await expect.poll(async () => {
      evidence.lastPrepared = { origin, ...status };
      if (status.ready && status.prepared_count > 0) return true;
      const refreshed = responseFor(page, path);
      await page.getByTestId('agent-prewarm-status').getByRole('button', { name: 'Refresh', exact: true }).click();
      status = await data<Prepared>(await refreshed);
      return status.ready && status.prepared_count > 0;
    }, { timeout: 60_000, intervals: [1_000, 2_000] }).toBe(true);
    await expect(page.getByTestId('prewarm-state')).toHaveText('Ready');
    await expect(page.getByTestId('prewarm-count')).toHaveText(`Available: ${status.prepared_count}`);
    expect(status.sandbox_id).toBeTruthy();
    expect(status.client_pool_name).toBeTruthy();
    expect(status.runtime_generation).toBeTruthy();
    return status;
  }

  async function detail(page: Page, origin: string, sessionId: string): Promise<Detail> {
    const read = responseFor(page, `/admin/sessions/${sessionId}/detail`);
    await page.goto(url(origin, `/manage/sessions/${sessionId}`));
    return data<Detail>(await read);
  }

  function ownership(record: Detail) {
    return { sessionId: record.session_id, sandboxId: record.sandbox_id,
      workspaceId: record.workspace_id, agentId: record.agent_id };
  }

  async function start(page: Page, origin: string, agent: AgentRecord): Promise<Detail> {
    await page.goto(url(origin, '/agents'));
    await page.getByTestId('agent-option').and(page.locator(`[data-agent-name="${agent.name}"]`))
      .getByRole('button', { name: 'Start conversation', exact: true }).click();
    await page.waitForURL(/\/sessions\/[^/]+$/);
    const id = new URL(page.url()).pathname.split('/').pop()!;
    sessions.push(id);
    await expect(page.getByTestId('composer-prompt')).toBeEnabled({ timeout: 60_000 });
    const record = await detail(page, origin, id);
    expect(record.session_id).toBe(id);
    expect(record.state).toBe('READY');
    expect(record.workspace_id).toBeTruthy();
    expect(record.sandbox_id).toBeTruthy();
    return record;
  }

  async function terminal(page: Page, origin: string, id: string, command: string, output: string) {
    await page.goto(url(origin, `/sessions/${id}`));
    await page.getByRole('tab', { name: 'Terminal', exact: true }).click();
    const input = page.getByPlaceholder('Type a command…');
    await expect(input).toBeEnabled({ timeout: 30_000 });
    await input.fill(command);
    await input.press('Enter');
    await expect(page.getByText(output, { exact: true })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText('Exit code: 0', { exact: true })).toBeVisible();
    await expect(input).toBeEnabled();
  }

  async function history(page: Page, origin: string, id: string): Promise<MessagePage> {
    const read = responseFor(page, `/sessions/${id}/history-blocks`);
    await page.goto(url(origin, `/sessions/${id}`));
    const result = await data<MessagePage>(await read);
    expect(result.has_more).not.toBe(true);
    return result;
  }

  return { createAgent, prepared, detail, ownership, start, terminal, history };
}

interface Pod {
  metadata: { name: string; namespace: string; uid: string; annotations?: Record<string, string>;
    ownerReferences?: Array<{ kind: string; name: string; uid: string }> };
  spec: { nodeName: string; containers: Array<{ name: string; image: string;
    ports?: Array<{ containerPort: number; hostPort?: number; hostIP?: string }> }> };
  status: { phase: string; conditions?: Array<{ type: string; status: string }>;
    containerStatuses?: Array<{ name: string; imageID: string; containerID: string; restartCount: number }> };
}

function kube(args: string[]): any {
  return JSON.parse(execFileSync('kubectl', [...args, '-o', 'json'], {
    encoding: 'utf8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'],
  }));
}

function sourceIdentity(container: string) {
  return JSON.parse(execFileSync('docker', ['inspect', '--format',
    '{"id":{{json .Id}},"image":{{json .Image}},"pid":{{json .State.Pid}},'
    + '"started":{{json .State.StartedAt}},"running":{{json .State.Running}}}', container],
  { encoding: 'utf8', timeout: 30_000 }));
}

function podIdentity(pod: Pod) {
  return { name: pod.metadata.name, namespace: pod.metadata.namespace, uid: pod.metadata.uid,
    node: pod.spec.nodeName, containers: pod.status.containerStatuses };
}

/** Observe the task-owned backend on B; do not create a second control plane. */
export class CrossHostBackend {
  readonly primaryOrigin = new URL(absoluteBaseUrl()).origin;
  readonly secondaryOrigin: string;
  readonly evidence: Record<string, unknown>;
  private readonly source: string;
  private readonly initialSource: ReturnType<typeof sourceIdentity>;
  private readonly pod: Pod;
  private readonly initialPod: ReturnType<typeof podIdentity>;

  constructor(readonly nodes: WorkspaceResumeNodes) {
    const source = resolveServiceContainer(SERVER_CONTAINER_HANDLE);
    if (source.kind !== 'ready') throw new Error(source.reason);
    this.source = source.container;
    this.initialSource = sourceIdentity(this.source);
    expect(this.initialSource.running).toBe(true);
    const sourceNode = kube(['get', 'node', nodes.name('source')]);
    const sourceAddresses = sourceNode.status.addresses.filter((item: { type: string }) => item.type === 'InternalIP');
    expect(sourceAddresses).toHaveLength(1);
    expect(new URL(this.primaryOrigin).hostname).toBe(sourceAddresses[0].address);
    const pods: Pod[] = kube(['get', 'pods', '-A', '-l', 'astrabox.probe/backend=secondary']).items;
    expect(pods, 'provision exactly one task-owned secondary backend before this case').toHaveLength(1);
    this.pod = pods[0];
    const annotations = this.pod.metadata.annotations || {};
    expect(annotations['astrabox.probe/source-container']).toBe(this.source);
    expect(annotations['astrabox.probe/source-image']).toBe(this.initialSource.image);
    const imageRef = annotations['astrabox.probe/source-image-ref'];
    expect(imageRef).toMatch(/@sha256:[0-9a-f]{64}$/);
    expect(this.pod.spec.nodeName).toBe(nodes.name('target'));
    expect(this.pod.status.phase).toBe('Running');
    expect(this.pod.status.conditions?.some((item) => item.type === 'Ready' && item.status === 'True')).toBe(true);
    expect(this.pod.spec.containers).toHaveLength(1);
    const container = this.pod.spec.containers[0];
    expect(container.image).toBe(imageRef);
    const statuses = this.pod.status.containerStatuses || [];
    expect(statuses).toHaveLength(1);
    expect(statuses[0].name).toBe(container.name);
    expect(statuses[0].imageID.split('@').pop()).toBe(imageRef.split('@').pop());
    const ports = (container.ports || []).filter((port) => port.containerPort === 8000);
    expect(ports).toHaveLength(1);
    expect(ports[0].hostPort).toBeGreaterThan(0);
    const target = kube(['get', 'node', nodes.name('target')]);
    const addresses = target.status.addresses.filter((item: { type: string }) => item.type === 'InternalIP');
    expect(addresses).toHaveLength(1);
    expect(ports[0].hostIP).toBe(addresses[0].address);
    expect(new URL(this.primaryOrigin).hostname).not.toBe(addresses[0].address);
    this.secondaryOrigin = `http://${addresses[0].address}:${ports[0].hostPort}`;
    this.initialPod = podIdentity(this.pod);
    this.evidence = { topology: 'backends on different hosts, B claims sandbox on A',
      origins: [this.primaryOrigin, this.secondaryOrigin], source: this.initialSource, secondary: this.initialPod };
  }

  assertUnchanged(): void {
    expect(sourceIdentity(this.source)).toEqual(this.initialSource);
    const pod: Pod = kube(['get', 'pod', this.pod.metadata.name, '-n', this.pod.metadata.namespace]);
    expect(podIdentity(pod)).toEqual(this.initialPod);
    expect(pod.status.conditions?.some((item) => item.type === 'Ready' && item.status === 'True')).toBe(true);
  }

  sandboxOnSource(id: string) {
    const namespace = String(process.env.ASTRABOX_E2E_KUBE_NAMESPACE || 'opensandbox');
    const resource = kube(['get', 'batchsandbox', id, '-n', namespace]);
    const pods: Pod[] = kube(['get', 'pods', '-n', namespace]).items;
    const matches = pods.filter((pod) => pod.metadata.ownerReferences?.some((owner) =>
      owner.kind === 'BatchSandbox' && owner.name === id && owner.uid === resource.metadata.uid));
    expect(matches, 'the exact supplier resource must own one live sandbox Pod').toHaveLength(1);
    expect(matches[0].status.phase).toBe('Running');
    expect(matches[0].spec.nodeName).toBe(this.nodes.name('source'));
    expect(matches[0].spec.nodeName).not.toBe(this.pod.spec.nodeName);
    return { sandboxId: id, batchUid: resource.metadata.uid, pod: podIdentity(matches[0]) };
  }

  async authenticatedContext(browser: Browser, issued: BrowserContext): Promise<BrowserContext> {
    const consoleUrl = String(process.env.ASTRABOX_E2E_CONSOLE_URL || '');
    const cookies = (await issued.cookies(consoleUrl)).filter((cookie) => cookie.name === 'astrabox_session');
    expect(cookies, 'use an actual OIDC-issued credential, never forge one').toHaveLength(1);
    const context = await browser.newContext({ baseURL: this.primaryOrigin });
    await context.addCookies([this.primaryOrigin, this.secondaryOrigin].map((origin) =>
      ({ ...cookies[0], domain: new URL(origin).hostname, secure: false })));
    const page = await context.newPage();
    const users: unknown[] = [];
    for (const origin of [this.primaryOrigin, this.secondaryOrigin]) {
      const response = await page.goto(`${origin}${apiPath('/auth/session')}`);
      expect(response?.status()).toBe(200);
      const body = await response!.json();
      expect(body.authenticated).toBe(true);
      expect(body.user.user_id).toBeTruthy();
      users.push(body.user);
    }
    expect(users[1]).toEqual(users[0]);
    this.evidence.authorizedUser = users[0];
    await page.close();
    return context;
  }
}
