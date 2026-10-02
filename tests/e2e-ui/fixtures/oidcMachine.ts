/** Real Casdoor machine credentials, read from the deployed secret mount. */
import { spawnSync } from 'node:child_process';
import { expect, type APIRequestContext } from '@playwright/test';
import { apiPath } from './env';

export type JsonObject = Record<string, unknown>;
export type ApiAnswer = { status: number; payload: JsonObject };
type MachineToken = { value: string; expiresIn: number };

export function requiredEnvironment(name: string): string {
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

export function deployedApiClient(serverContainer: string): { clientId: string; clientSecret: string } {
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

export async function json(response: Awaited<ReturnType<APIRequestContext['fetch']>>): Promise<JsonObject> {
  const text = await response.text();
  try {
    return JSON.parse(text) as JsonObject;
  } catch {
    throw new Error(`machine API returned non-JSON HTTP ${response.status()}`);
  }
}

export async function issueToken(
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

export async function call(
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
