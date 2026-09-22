import { execFileSync } from 'node:child_process';
import path from 'node:path';

import { expect, type Browser, type BrowserContext } from '@playwright/test';

import { absoluteBaseUrl, apiPath, repoRoot } from './env';
import { resolveServiceContainer, SERVER_CONTAINER_HANDLE } from './serviceContainer';

const helper = path.join(repoRoot, 'tests/e2e-ui/fixtures/add_server_replica.py');

function docker(args: string[]): string {
  return execFileSync('docker', args, { encoding: 'utf8', timeout: 30_000 });
}

function identity(container: string) {
  // Never retain Config.Env: the original deployment carries credentials there.
  return JSON.parse(docker(['inspect', '--format',
    '{"id":{{json .Id}},"image":{{json .Image}},"pid":{{json .State.Pid}},'
    + '"started":{{json .State.StartedAt}},"running":{{json .State.Running}},'
    + '"restarts":{{json .RestartCount}},"labels":{{json .Config.Labels}},'
    + '"ports":{{json .NetworkSettings.Ports}}}', container]));
}

/** Two independent processes, not a replacement process or a second database. */
export class DistributedBackend {
  readonly source: string;
  readonly clone: string;
  readonly primaryOrigin = new URL(absoluteBaseUrl()).origin;
  readonly secondaryOrigin: string;
  readonly evidence: Record<string, unknown>;
  private readonly initial: ReturnType<typeof identity>[];

  constructor(name: string) {
    const source = resolveServiceContainer(SERVER_CONTAINER_HANDLE);
    if (source.kind !== 'ready') throw new Error(source.reason);
    this.source = source.container;
    this.clone = name;
    const a = identity(this.source);
    const origin = new URL(this.primaryOrigin);
    expect(origin.protocol, 'the diagnostic fixture exposes private HTTP only').toBe('http:');
    expect(origin.hostname, 'the remote browser must reach the private host').not.toBe('127.0.0.1');
    const port = origin.port || '80';
    const mappings = Object.entries(a.ports as Record<string, Array<{ HostPort: string }> | null>)
      .filter(([, bindings]) => bindings?.some((binding) => binding.HostPort === port));
    expect(mappings, 'resolve the application listener from the selected deployment').toHaveLength(1);
    execFileSync('python3', [helper, '--source', this.source, '--name', this.clone,
      '--bind-ip', origin.hostname, '--host-port', '0',
      '--container-port', mappings[0][0].split('/')[0]], { encoding: 'utf8', timeout: 30_000 });
    const project = String(a.labels['com.docker.compose.project'] || '');
    expect(project).not.toBe('');
    const matches = docker(['ps', '--filter', `label=com.docker.compose.project=${project}`,
      '--filter', 'label=astrabox.probe.replica-clone=1', '--format', '{{.Names}}'])
      .trim().split('\n').filter(Boolean);
    expect(matches, 'the selected deployment must have exactly this owned replica').toEqual([this.clone]);
    const b = identity(this.clone);
    expect(b.image).toBe(a.image);
    expect(b.id).not.toBe(a.id);
    expect(b.pid).not.toBe(a.pid);
    const published = Object.values(b.ports as Record<string, Array<{ HostIp: string; HostPort: string }> | null>)
      .flatMap((bindings) => bindings || []);
    expect(published).toHaveLength(1);
    expect(published[0].HostIp).toBe(origin.hostname);
    origin.port = published[0].HostPort;
    this.secondaryOrigin = origin.origin;
    this.initial = [a, b];
    this.evidence = { topology: 'two live processes on one host, shared deployment services',
      helper: path.relative(repoRoot, helper), origins: [this.primaryOrigin, this.secondaryOrigin],
      before: this.initial };
  }

  async ready(): Promise<void> {
    await expect.poll(async () => {
      this.assertUnchanged();
      try {
        return (await fetch(`${this.secondaryOrigin}/readyz`, { signal: AbortSignal.timeout(2_000) })).status;
      } catch (error) {
        if (!(error instanceof TypeError) && !(error instanceof DOMException)) throw error;
        return 0;
      }
    }, { timeout: 30_000, intervals: [500, 1_000] }).toBe(200);
  }

  assertUnchanged(): void {
    const now = [identity(this.source), identity(this.clone)];
    expect(now.every((item) => item.running)).toBe(true);
    expect(now.map(({ id, pid, started, restarts }) => ({ id, pid, started, restarts })))
      .toEqual(this.initial.map(({ id, pid, started, restarts }) => ({ id, pid, started, restarts })));
    this.evidence.after = now;
  }

  remove(): void {
    execFileSync('python3', [helper, '--source', this.source, '--name', this.clone, '--remove'],
      { encoding: 'utf8', timeout: 30_000 });
  }

  async authenticatedContext(browser: Browser, issued: BrowserContext): Promise<BrowserContext> {
    const consoleUrl = String(process.env.ASTRABOX_E2E_CONSOLE_URL || '');
    const cookies = (await issued.cookies(consoleUrl)).filter((cookie) => cookie.name === 'astrabox_session');
    expect(cookies, 'reuse the actual OIDC-issued browser credential, never forge one').toHaveLength(1);
    const context = await browser.newContext({ baseURL: this.primaryOrigin });
    // This private-HTTP fixture tests cross-replica authorization, not public TLS/cookie policy.
    await context.addCookies([{ ...cookies[0], domain: new URL(this.primaryOrigin).hostname, secure: false }]);
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
