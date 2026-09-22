/**
 * Casdoor owns the long-lived client secret; AstraBox accepts only the
 * short-lived access tokens minted from it. This spec uses the deployed secret
 * mount, the real Casdoor token endpoint, and fresh request contexts with no
 * browser cookie so a passing call cannot accidentally borrow the admin UI's
 * identity. Each route proves one independent scope boundary.
 */
import { spawnSync } from 'node:child_process';

import {
  expect,
  test,
  type APIRequestContext,
} from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import {
  requireServiceContainer,
  SERVER_CONTAINER_HANDLE,
} from '../fixtures/serviceContainer';

type JsonObject = Record<string, unknown>;
type ApiAnswer = { status: number; payload: JsonObject };
type MachineToken = { value: string; expiresIn: number };

function requiredEnvironment(name: string): string {
  const value = String(process.env[name] || '').trim();
  if (!value) throw new Error(`${name} is required for the scoped API credential E2E`);
  return value.replace(/\/$/, '');
}

function dockerText(args: string[], description: string): string {
  const result = spawnSync('docker', args, {
    encoding: 'utf8',
    timeout: 30_000,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (result.error || result.status !== 0) {
    throw new Error(`${description} failed inside the deployed server container`);
  }
  return String(result.stdout || '').trim();
}

function deployedApiClient(serverContainer: string): { clientId: string; clientSecret: string } {
  const clientId = dockerText(
    ['exec', serverContainer, 'printenv', 'ASTRABOX_OIDC_API_CLIENT_ID'],
    'reading the API client id',
  );
  const secretFile = dockerText(
    ['exec', serverContainer, 'printenv', 'ASTRABOX_OIDC_API_CLIENT_SECRET_FILE'],
    'reading the API client secret mount',
  );
  if (clientId !== 'astrabox-api') {
    throw new Error('the maintained Casdoor deployment did not select the astrabox-api client');
  }
  if (!secretFile.startsWith('/run/secrets/')) {
    throw new Error('the long-lived API client secret is not mounted from the deployment secret store');
  }
  const configuredEnvironment = dockerText(
    ['inspect', '--format', '{{range .Config.Env}}{{println .}}{{end}}', serverContainer],
    'inspecting the deployed API client configuration',
  );
  if (configuredEnvironment.includes('ASTRABOX_OIDC_API_CLIENT_SECRET=')) {
    throw new Error('the long-lived API client secret was exposed in container environment metadata');
  }
  const clientSecret = dockerText(
    ['exec', serverContainer, 'cat', secretFile],
    'reading the mounted API client secret',
  );
  if (!clientSecret) throw new Error('the deployed API client secret mount is empty');
  return { clientId, clientSecret };
}

async function json(response: Awaited<ReturnType<APIRequestContext['fetch']>>): Promise<JsonObject> {
  const text = await response.text();
  try {
    return JSON.parse(text) as JsonObject;
  } catch {
    throw new Error(`machine API returned non-JSON HTTP ${response.status()}`);
  }
}

async function issueToken(
  machine: APIRequestContext,
  tokenEndpoint: string,
  clientId: string,
  clientSecret: string,
  scope: string,
): Promise<MachineToken> {
  const response = await machine.post(tokenEndpoint, {
    form: {
      grant_type: 'client_credentials',
      client_id: clientId,
      client_secret: clientSecret,
      scope,
    },
  });
  const payload = await json(response);
  if (!response.ok()) {
    throw new Error(`Casdoor refused the configured client_credentials grant with HTTP ${response.status()}`);
  }
  const value = String(payload.access_token || '').trim();
  if (!value) throw new Error('Casdoor client_credentials response has no access_token');
  expect(String(payload.token_type || '').toLowerCase()).toBe('bearer');
  const expiresIn = Number(payload.expires_in);
  expect(expiresIn, 'the bundled API access token lifetime is one hour').toBe(3_600);
  return { value, expiresIn };
}

async function call(
  machine: APIRequestContext,
  token: string,
  method: string,
  route: string,
  data?: unknown,
): Promise<ApiAnswer> {
  const response = await machine.fetch(apiPath(route), {
    method,
    headers: { Authorization: `Bearer ${token}` },
    data,
  });
  return { status: response.status(), payload: await json(response) };
}

function expectScopeDenial(answer: ApiAnswer, requiredScope: string): void {
  expect(answer.status).toBe(403);
  expect(answer.payload.code).toBe('API_TOKEN_SCOPE_INSUFFICIENT');
  expect((answer.payload.data as JsonObject | undefined)?.required_scope).toBe(requiredScope);
}

test('Casdoor client credentials mint independent read, write, and admin API scopes', async ({
  page,
  playwright,
  request,
}) => {
  const consoleUrl = requiredEnvironment('ASTRABOX_E2E_CONSOLE_URL');
  const issuer = requiredEnvironment('ASTRABOX_E2E_OIDC_ISSUER');
  const serverContainer = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const { clientId, clientSecret } = deployedApiClient(serverContainer);
  const browserApi = new AstraApi(request);
  const baseAgent = await browserApi.defaultAgent();
  const environmentName = String(baseAgent.environment_name || '').trim();
  let model = String(baseAgent.model || '').trim();
  if (!model || model.includes('*')) {
    const models = await browserApi.listEnvironmentModels(environmentName);
    model = models.find((item) => item && !item.includes('*')) || 'deepseek-chat';
  }

  await page.goto(appPath('/manage/agents'));
  const apiAccessLink = page.getByRole('link', { name: /AstraBox API API access/ });
  await expect(apiAccessLink).toHaveAttribute(
    'href',
    `${issuer}/applications/astrabox/astrabox-api`,
  );
  await expect(apiAccessLink).toHaveAttribute('target', '_blank');

  const machine = await playwright.request.newContext({
    baseURL: requiredEnvironment('ASTRABOX_E2E_CONSOLE_URL'),
    extraHTTPHeaders: { Accept: 'application/json' },
    storageState: { cookies: [], origins: [] },
  });
  let createdAgentId = '';
  let writeToken = '';
  try {
    const discoveryResponse = await machine.get(`${issuer}/.well-known/openid-configuration`);
    expect(discoveryResponse.ok()).toBe(true);
    const discovery = await json(discoveryResponse);
    const tokenEndpoint = String(discovery.token_endpoint || '').trim();
    const introspectionEndpoint = String(discovery.introspection_endpoint || '').trim();
    expect(tokenEndpoint).toMatch(/^https?:\/\//);
    expect(introspectionEndpoint).toMatch(/^https?:\/\//);

    const read = await issueToken(
      machine,
      tokenEndpoint,
      clientId,
      clientSecret,
      'astrabox:read',
    );
    const write = await issueToken(
      machine,
      tokenEndpoint,
      clientId,
      clientSecret,
      'astrabox:write',
    );
    writeToken = write.value;
    const admin = await issueToken(
      machine,
      tokenEndpoint,
      clientId,
      clientSecret,
      'astrabox:admin',
    );
    expect(new Set([read.expiresIn, write.expiresIn, admin.expiresIn])).toEqual(new Set([3_600]));

    const readSessions = await call(machine, read.value, 'GET', '/sessions?limit=1');
    expect(readSessions.status).toBe(200);
    expect(readSessions.payload.code).toBe('OK');
    expectScopeDenial(
      await call(machine, read.value, 'POST', '/agents', {
        name: `__e2e_read_must_not_create_${Date.now()}`,
        model,
        environment_name: environmentName,
      }),
      'astrabox:write',
    );
    expectScopeDenial(
      await call(machine, read.value, 'GET', '/admin/vaults'),
      'astrabox:admin',
    );

    expectScopeDenial(
      await call(machine, write.value, 'GET', '/sessions?limit=1'),
      'astrabox:read',
    );
    const created = await call(machine, write.value, 'POST', '/agents', {
      name: `__e2e_api_client_${Date.now()}`,
      model,
      environment_name: environmentName,
    });
    expect(created.status).toBe(200);
    expect(created.payload.code).toBe('OK');
    createdAgentId = String((created.payload.data as JsonObject | undefined)?.agent_id || '');
    expect(createdAgentId, 'the write-scoped client must create its own Agent').not.toEqual('');
    expectScopeDenial(
      await call(machine, write.value, 'GET', `/agents/${createdAgentId}`),
      'astrabox:read',
    );

    const readCreated = await call(machine, read.value, 'GET', `/agents/${createdAgentId}`);
    expect(readCreated.status).toBe(200);
    expect((readCreated.payload.data as JsonObject | undefined)?.agent_id).toBe(createdAgentId);

    const adminVaults = await call(machine, admin.value, 'GET', '/admin/vaults');
    expect(adminVaults.status).toBe(200);
    expect(adminVaults.payload.code).toBe('OK');
    expectScopeDenial(
      await call(machine, admin.value, 'GET', '/sessions?limit=1'),
      'astrabox:read',
    );

    const invalid = await call(machine, 'not-an-issued-access-token', 'GET', '/sessions?limit=1');
    expect(invalid.status).toBe(401);
    expect(invalid.payload.code).toBe('AUTH_REQUIRED');
  } finally {
    if (createdAgentId && writeToken) {
      const deleted = await call(machine, writeToken, 'DELETE', `/agents/${createdAgentId}`);
      expect(deleted.status).toBe(200);
    }
    await machine.dispose();
  }

  expect(consoleUrl).toBe(new URL(page.url()).origin);
});
