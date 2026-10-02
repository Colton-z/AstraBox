/**
 * Casdoor owns the long-lived client secret; AstraBox accepts only the
 * short-lived access tokens minted from it. This spec uses the deployed secret
 * mount, the real Casdoor token endpoint, and fresh request contexts with no
 * browser cookie so a passing call cannot accidentally borrow the admin UI's
 * identity. Each route proves one independent scope boundary.
 */
import {
  expect,
  test,
} from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { appPath } from '../fixtures/env';
import {
  call, deployedApiClient, issueToken, json, requiredEnvironment,
  type ApiAnswer, type JsonObject,
} from '../fixtures/oidcMachine';
import {
  requireServiceContainer,
  SERVER_CONTAINER_HANDLE,
} from '../fixtures/serviceContainer';

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
      prewarm_enabled: false,
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
