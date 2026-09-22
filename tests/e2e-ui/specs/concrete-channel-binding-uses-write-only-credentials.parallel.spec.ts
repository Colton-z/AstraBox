/**
 * The concrete channel catalogue is a console capability, not just a list of
 * installed npm packages. This walk proves every bundled provider reaches the
 * dynamic form, then takes Matrix through the complete binding lifecycle.
 *
 * Matrix is deliberate: a homeserver fixture runs on loopback in the deployed
 * server container, so the real embedded gateway can register an
 * Application Service without borrowing a third-party account. Registration
 * proves the write-only Application Service token was decrypted; the official
 * user-query callback proves the homeserver token was decrypted. Rotation,
 * disable, and re-enable must all change the live connector, not only the row.
 */
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';

import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import {
  PlatformApi,
  type ChannelProviderRecord,
  type DeploymentRecord,
} from '../fixtures/platformApi';
import {
  requireServiceContainer,
  SERVER_CONTAINER_HANDLE,
} from '../fixtures/serviceContainer';
import { onPassOnly } from '../fixtures/sessionCleanup';

const RUN_ID = new Date().toISOString().replace(/[:.]/g, '-');
const CONCRETE_PROVIDERS = [
  'dingtalk',
  'discord',
  'feishu',
  'kook',
  'lark',
  'line',
  'mail',
  'matrix',
  'qq',
  'satori',
  'slack',
  'telegram',
  'wechat-official',
  'wecom',
  'whatsapp',
  'zulip',
];
const BOT_LOCALPART = `e2e-bot-${RUN_ID}`.toLowerCase();
const HOMESERVER_DOMAIN = 'matrix.e2e.invalid';
const BOT_USER_ID = `@${BOT_LOCALPART}:${HOMESERVER_DOMAIN}`;
const FIRST_HS_TOKEN = `e2e-hs-${RUN_ID}`;
const FIRST_AS_TOKEN = `e2e-as-${RUN_ID}`;
const ROTATED_HS_TOKEN = `e2e-hs-rotated-${RUN_ID}`;
const ROTATED_AS_TOKEN = `e2e-as-rotated-${RUN_ID}`;

let agentId = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (agentId && deploymentId) {
    await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  }
});

type RegistrationFixture = {
  done: Promise<void>;
  port: number;
  stop: () => void;
};

const MATRIX_REGISTRATION_SERVER = String.raw`
import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote, urlsplit

port = int(sys.argv[1])
expected_localpart = sys.argv[2]
expected_user_id = sys.argv[3]
expected_token_hash = sys.argv[4]
access_token = "matrix-e2e-access-token"

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def send_json(self, status, payload):
        response = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_POST(self):
        length = int(self.headers.get("content-length") or "0")
        body = self.rfile.read(length)
        bearer = self.headers.get("authorization", "").removeprefix("Bearer ")
        try:
            payload = json.loads(body or b"{}")
        except Exception:
            payload = {}
        valid = (
            urlsplit(self.path).path == "/_matrix/client/v3/register"
            and payload.get("username") == expected_localpart
            and hashlib.sha256(bearer.encode("utf-8")).hexdigest() == expected_token_hash
        )
        if valid:
            self.send_json(200, {"access_token": access_token, "user_id": expected_user_id})
        else:
            self.send_json(403, {"errcode": "M_FORBIDDEN"})

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        bearer = self.headers.get("authorization", "").removeprefix("Bearer ")
        if bearer != access_token:
            self.send_json(403, {"errcode": "M_FORBIDDEN"})
        elif path == f"/_matrix/client/v3/profile/{expected_user_id}":
            self.send_json(200, {"displayname": "AstraBox E2E Bot"})
        elif path == "/_matrix/client/v3/sync":
            self.send_json(200, {"next_batch": "batch-1", "rooms": {"join": {}}})
        else:
            self.send_json(404, {"errcode": "M_NOT_FOUND"})

    def do_PUT(self):
        length = int(self.headers.get("content-length") or "0")
        body = self.rfile.read(length)
        path = unquote(urlsplit(self.path).path)
        bearer = self.headers.get("authorization", "").removeprefix("Bearer ")
        try:
            payload = json.loads(body or b"{}")
        except Exception:
            payload = {}
        valid = (
            bearer == access_token
            and path == f"/_matrix/client/v3/profile/{expected_user_id}/displayname"
            and payload.get("displayname") == "AstraBox E2E Bot"
        )
        self.send_json(200 if valid else 403, {} if valid else {"errcode": "M_FORBIDDEN"})

server = HTTPServer(("127.0.0.1", port), Handler)
print(f"READY {server.server_address[1]}", flush=True)
for _ in range(4):
    server.handle_request()
server.server_close()
`;

async function startRegistrationFixture(
  serverContainer: string,
  applicationServiceToken: string,
  port = 0,
): Promise<RegistrationFixture> {
  const expectedDigest = createHash('sha256').update(applicationServiceToken).digest('hex');
  const child = spawn(
    'docker',
    [
      'exec',
      serverContainer,
      'python',
      '-c',
      MATRIX_REGISTRATION_SERVER,
      String(port),
      BOT_LOCALPART,
      BOT_USER_ID,
      expectedDigest,
    ],
    { stdio: ['ignore', 'pipe', 'pipe'] },
  );
  let output = '';
  let ready = false;
  let readyResolve!: (port: number) => void;
  let readyReject!: (error: Error) => void;
  let doneResolve!: () => void;
  let doneReject!: (error: Error) => void;
  const readyPromise = new Promise<number>((resolve, reject) => {
    readyResolve = resolve;
    readyReject = reject;
  });
  const done = new Promise<void>((resolve, reject) => {
    doneResolve = resolve;
    doneReject = reject;
  });
  void done.catch(() => {});
  const append = (chunk: Buffer) => {
    output = `${output}${chunk.toString('utf8')}`.slice(-2_000);
    const match = output.match(/READY ([0-9]{1,5})/);
    if (!ready && match) {
      ready = true;
      readyResolve(Number(match[1]));
    }
  };
  child.stdout.on('data', append);
  child.stderr.on('data', append);
  child.once('error', (error) => {
    if (!ready) readyReject(error);
    doneReject(error);
  });
  child.once('exit', (code, signal) => {
    if (code === 0) {
      doneResolve();
      return;
    }
    const error = new Error(
      `Matrix registration fixture exited code=${code} signal=${signal}; output=${output}`,
    );
    if (!ready) readyReject(error);
    doneReject(error);
  });
  const timer = setTimeout(() => {
    readyReject(new Error(`Matrix registration fixture did not listen; output=${output}`));
  }, 10_000);
  try {
    port = await readyPromise;
  } catch (error) {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
    throw error;
  } finally {
    clearTimeout(timer);
  }
  return {
    done,
    port,
    stop: () => {
      if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
    },
  };
}

function assertCredentialsAbsent(payload: unknown, where: string): void {
  const serialized = JSON.stringify(payload ?? null);
  if (
    [FIRST_HS_TOKEN, FIRST_AS_TOKEN, ROTATED_HS_TOKEN, ROTATED_AS_TOKEN]
      .some((secret) => serialized.includes(secret))
  ) {
    throw new Error(`${where} exposed a write-only channel credential`);
  }
}

async function matrixUserProbe(
  request: APIRequestContext,
  token: string,
): Promise<{ status: number; body: string }> {
  const path = apiPath(
    `/deployments/${deploymentId}/callback/matrix/_matrix/app/v1/users/`
      + `${encodeURIComponent(BOT_USER_ID)}?access_token=${encodeURIComponent(token)}`,
  );
  const response = await request.get(path);
  return { status: response.status(), body: await response.text() };
}

async function waitForMatrixUserProbe(
  request: APIRequestContext,
  token: string,
): Promise<void> {
  await expect.poll(
    () => matrixUserProbe(request, token),
    {
      message: 'the embedded Matrix connector did not accept its configured homeserver token',
      timeout: 45_000,
      intervals: [250, 500, 1_000],
    },
  ).toEqual({ status: 200, body: '{}' });
}

async function oneDeployment(platform: PlatformApi): Promise<DeploymentRecord> {
  const deployment = (await platform.listDeployments(agentId)).find(
    (item) => item.deployment_id === deploymentId,
  );
  expect(deployment, 'the channel binding must remain listable').toBeTruthy();
  return deployment as DeploymentRecord;
}

test('all concrete channels render and a Matrix binding rotates its live credentials', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const serverContainer = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const baseAgent = await api.defaultAgent();
  agentId = String(baseAgent.agent_id || '').trim();
  expect(agentId, 'the seeded Agent must have an id').not.toEqual('');

  const providers = await platform.listChannelProviders();
  const concrete = providers.filter((provider) => provider.name !== 'generic_json');
  expect(concrete.map((provider) => provider.name).sort()).toEqual(CONCRETE_PROVIDERS);
  expect(concrete).toHaveLength(16);
  for (const provider of concrete) {
    expect(provider.supports_source, `${provider.name} must own a source connector`).toBe(true);
    expect(provider.uses_trigger_secret, `${provider.name} uses named credentials`).toBe(false);
    expect(provider.credential_fields.length, `${provider.name} must declare credentials`).toBeGreaterThan(0);
    expect(
      provider.credential_fields.every((field) => field.secret),
      `${provider.name} credential fields must stay write-only`,
    ).toBe(true);
  }

  await page.goto(appPath('/manage/deployments/new'));
  await page.getByLabel('Agent').selectOption(agentId);
  const trigger = page.getByLabel('Trigger');
  const optionLabels = await trigger.locator('option').allTextContents();
  expect(optionLabels).toEqual(expect.arrayContaining(concrete.map((provider) => provider.label)));
  for (const provider of concrete) {
    await trigger.selectOption(provider.scene);
    for (const field of [...provider.config_fields, ...provider.credential_fields]) {
      // A boolean renders as the kit switch, whose label deliberately names
      // BOTH halves (the hidden checkbox via label-for and the visible
      // role=switch span via labelledby — see ConsoleToggle), so a bare
      // getByLabel is ambiguous there by design. The role query names the
      // one element a user operates.
      const control = field.kind === 'boolean'
        ? page.getByRole('switch', { name: field.label })
        : page.getByLabel(field.label);
      await expect(control, `${provider.label} must render ${field.label}`).toBeVisible();
      if (field.secret) await expect(control).toHaveAttribute('type', 'password');
    }
    const setup = page.getByRole('link', { name: /Open official console/ });
    if (provider.setup_url) await expect(setup).toHaveAttribute('href', provider.setup_url);
    else await expect(setup).toHaveCount(0);
  }

  const matrix = concrete.find((provider) => provider.name === 'matrix') as ChannelProviderRecord;
  expect(matrix.callback_path).toBe('/matrix');
  await trigger.selectOption(matrix.scene);
  await page.getByLabel('Bot localpart').fill(BOT_LOCALPART);
  await page.getByLabel('Homeserver domain').fill(HOMESERVER_DOMAIN);
  await page.getByLabel('Bot display name').fill('AstraBox E2E Bot');
  await page.getByLabel('Homeserver token').fill(FIRST_HS_TOKEN);
  await page.getByLabel('Application service token').fill(FIRST_AS_TOKEN);

  let registration: RegistrationFixture | null = await startRegistrationFixture(
    serverContainer,
    FIRST_AS_TOKEN,
  );
  const matrixPort = registration.port;
  await page.getByLabel('Homeserver URL').fill(`http://127.0.0.1:${matrixPort}`);
  try {
    await page.getByRole('button', { name: 'Create' }).click();
    await page.waitForURL((url) => {
      const segment = url.pathname.split('/').filter(Boolean).pop();
      return segment !== undefined && segment !== 'new';
    });
    deploymentId = decodeURIComponent(page.url().split('/').pop() || '');
    expect(deploymentId, 'create must navigate to the Matrix binding').not.toEqual('');

    await waitForMatrixUserProbe(request, FIRST_HS_TOKEN);
    await registration.done;
    registration = null;

    const created = await oneDeployment(platform);
    expect(created.scene).toBe('channel:matrix');
    expect(created.agent_id).toBe(agentId);
    expect(created.credentials_configured).toBe(true);
    expect(created.channel_config).toMatchObject({
      id: BOT_LOCALPART,
      host: HOMESERVER_DOMAIN,
      endpoint: `http://127.0.0.1:${matrixPort}`,
    });
    expect(created.callback_base_url).toContain(
      `/api/v1/deployments/${deploymentId}/callback`,
    );
    expect(Object.hasOwn(created, 'credentials')).toBe(false);
    assertCredentialsAbsent(created, 'the Deployment listing');
    assertCredentialsAbsent(await page.locator('body').innerText(), 'the detail page');
    await expect(page.getByText('Configured', { exact: true })).toBeVisible();
    await expect(page.getByText(`${created.callback_base_url}/matrix`, { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: /Open official console/ })).toHaveAttribute(
      'href',
      matrix.setup_url || '',
    );
    expect((await matrixUserProbe(request, 'wrong-homeserver-token')).status).toBe(403);

    registration = await startRegistrationFixture(
      serverContainer,
      ROTATED_AS_TOKEN,
      matrixPort,
    );
    await page.getByLabel('Homeserver token').fill(ROTATED_HS_TOKEN);
    await page.getByLabel('Application service token').fill(ROTATED_AS_TOKEN);
    await page.getByRole('button', { name: 'Save' }).click();
    await waitForMatrixUserProbe(request, ROTATED_HS_TOKEN);
    await registration.done;
    registration = null;
    expect((await matrixUserProbe(request, FIRST_HS_TOKEN)).status).toBe(403);
    assertCredentialsAbsent(await oneDeployment(platform), 'the rotated Deployment listing');
    await expect(page.getByLabel('Homeserver token')).toHaveValue('');
    await expect(page.getByLabel('Application service token')).toHaveValue('');

    await page.getByRole('button', { name: 'Disable' }).click();
    await expect(page.getByRole('button', { name: 'Enable' })).toBeVisible();
    expect((await matrixUserProbe(request, ROTATED_HS_TOKEN)).status).toBe(404);

    registration = await startRegistrationFixture(
      serverContainer,
      ROTATED_AS_TOKEN,
      matrixPort,
    );
    await page.getByRole('button', { name: 'Enable' }).click();
    await waitForMatrixUserProbe(request, ROTATED_HS_TOKEN);
    await registration.done;
    registration = null;
    await expect(page.getByRole('button', { name: 'Disable' })).toBeVisible();
  } finally {
    registration?.stop();
  }
});
