/**
 * E2E: an environment credential whose value the platform obtains by an OAuth2
 * grant reaches the sandbox's requests, and is renewed while a response runs.
 *
 * This spec is the token endpoint: it answers the password grant of a public
 * client with tokens that live 90 seconds and records every request. The
 * credential binds one host that returns the headers it received
 * (`ASTRABOX_E2E_CREDENTIAL_ECHO_URL`, an HTTPS endpoint answering with
 * `{"headers": {...}}`), so what a request from the box carried is read from
 * the far side, not from the platform.
 *
 * - The box holds a placeholder; the echo host receives a token this endpoint
 *   issued, obtained with the grant the credential names and no client secret.
 * - While one response runs past the first token's renewal, a request from
 *   the box carries a token issued after that response started: the renewal
 *   reached the egress boundary without waiting for the next turn.
 *
 * Needs `ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT` (a conversation-tenancy
 * Claude Code RuntimeProfile) and a base URL the server container can reach
 * this runner on.
 */
import { randomUUID } from 'node:crypto';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { engineProfileFor, modeName } from '../fixtures/engineProfile';
import { absoluteBaseUrl } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const ECHO_URL = String(process.env.ASTRABOX_E2E_CREDENTIAL_ECHO_URL || '').trim();
const TOKEN_LIFETIME_S = 90;
const RESPONSE_S = 75;
const GRANT = { client_id: 'e2e-public-client', username: 'e2e-service-user', scope: 'openid api:read' };

interface Issued { token: string; at: number; form: Record<string, string> }

async function tokenEndpoint(password: string) {
  const issued: Issued[] = [];
  const refused: string[] = [];
  const route = `/token/${randomUUID()}`;
  const advertised = new URL(absoluteBaseUrl());
  if (['localhost', '127.0.0.1', '[::1]'].includes(advertised.hostname)) {
    throw new Error('the token endpoint needs a base URL the server container reaches this runner on, not loopback');
  }
  const server = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    const form = Object.fromEntries(new URLSearchParams(Buffer.concat(chunks).toString('utf8')));
    if (request.method !== 'POST' || request.url !== route || form.password !== password) {
      refused.push(`${request.method} ${request.url} ${JSON.stringify({ ...form, password: '<redacted>' })}`);
      response.writeHead(401, { 'Content-Type': 'application/json' }).end('{"error":"invalid_grant"}');
      return;
    }
    const token = `tok-${randomUUID()}-${issued.length + 1}`;
    issued.push({ token, at: Date.now(), form });
    response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({
      access_token: token, token_type: 'Bearer', expires_in: TOKEN_LIFETIME_S, refresh_token: `unused-${randomUUID()}`,
    }));
  });
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '0.0.0.0', () => resolve());
  });
  advertised.protocol = 'http:';
  advertised.port = String((server.address() as AddressInfo).port);
  advertised.pathname = route;
  return {
    url: advertised.toString(), issued, refused,
    close: () => new Promise<void>((resolve) => { server.close(() => resolve()); server.closeAllConnections(); }),
  };
}

const sessions = trackSessions();
let agentId = '';
let vaultId = '';
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  if (agentId) {
    await api.data('PUT', `/admin/agents/${agentId}/credential-vaults`, { vault_ids: [] });
    await api.deleteAgent(agentId);
  }
  if (vaultId) await new PlatformApi(request).deleteVault(vaultId);
});

test('a token obtained by a grant is what the box\'s requests carry, and is renewed while a response runs', async ({ request }) => {
  expect(ECHO_URL, 'ASTRABOX_E2E_CREDENTIAL_ECHO_URL must name an HTTPS endpoint that returns the request headers')
    .toMatch(/^https:\/\/[A-Za-z0-9.-]+\//);
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const profile = engineProfileFor('claude_code');
  const password = `pw-${randomUUID()}`;
  const endpoint = await tokenEndpoint(password);
  try {
    const name = `__e2e_granted_token_${Date.now().toString(36)}`;
    const secretName = `GRANTED_${randomUUID().replaceAll('-', '').toUpperCase()}`;
    vaultId = (await platform.createVault(name)).vault_id;
    const credential = await platform.createCredential(vaultId, {
      type: 'environment_variable', secret_name: secretName,
      oauth: { grant_type: 'password', token_endpoint: endpoint.url, auth_method: 'none', ...GRANT, password },
      networking: { type: 'limited', allowed_hosts: [new URL(ECHO_URL).hostname] },
      injection_location: { header: true, body: false },
    });
    const view = JSON.stringify(credential);
    expect(view, 'the grant is shown, its password is not').toContain(endpoint.url);
    expect(view).not.toContain(password);
    agentId = (await api.createColdTestAgent(name)).agent_id;
    await api.data('PUT', `/admin/agents/${agentId}/credential-vaults`, { vault_ids: [vaultId] });
    const sessionId = (await api.startConversation(agentId)).session_id;
    sessions.push(sessionId);
    await api.waitForSessionReady(sessionId, 60_000);
    await api.setPermissionMode(sessionId, modeName(profile, 'unattended'));

    // What the echo host received from one request made in the box.
    const carried = async (): Promise<string> => {
      const raw = await api.runTerminalCommand(sessionId, [
        `case "\${${secretName}}" in ASTRABOX-VAULT-CRED::*) ;; *) printf 'HELD=%s\\n' unprotected;; esac`,
        `curl --max-time 10 -sS '${ECHO_URL}' -H "Authorization: Bearer \${${secretName}}" | tr -d ' \\n' | grep -o 'tok-[A-Za-z0-9-]*' | head -1 | sed 's/^/CARRIED=/'`,
      ].join('; '));
      // The stream repeats the command's text; only its output joins the two words.
      expect(raw, 'the box holds a placeholder, not a token').not.toContain('HELD=unprotected');
      return raw.match(/CARRIED=(tok-[A-Za-z0-9-]+)/)?.[1] ?? '';
    };

    const first = await carried();
    expect(endpoint.issued.map((item) => item.token), 'the box\'s request carried a token this endpoint issued').toContain(first);
    for (const { form } of endpoint.issued) {
      expect(form).toMatchObject({ grant_type: 'password', ...GRANT, password });
      expect(form, 'a public client sends no secret').not.toHaveProperty('client_secret');
    }

    await api.postTurnInput(
      sessionId,
      `Use the Bash tool once, in the foreground with timeout 600000, to run exactly: sleep ${RESPONSE_S} `
        + 'Then reply with only the word DONE.',
    );
    let startedAt = 0;
    await expect.poll(async () => {
      const running = (await api.getSession(sessionId)).current_responses?.[0];
      startedAt = running?.started_at ? Date.parse(running.started_at) : 0;
      return startedAt;
    }, { message: 'the response starts', timeout: 30_000, intervals: [1_000] }).toBeGreaterThan(0);

    let renewed: Issued | undefined;
    await expect.poll(async () => {
      const token = await carried();
      renewed = endpoint.issued.find((item) => item.token === token && item.at > startedAt + 5_000);
      // Only a renewal seen while the response still runs counts.
      const running = ((await api.getSession(sessionId)).current_responses?.length ?? 0) > 0;
      return Boolean(renewed) && running;
    }, {
      message: 'a request made while the response runs carries a token issued after it started',
      timeout: (RESPONSE_S - 10) * 1_000,
      intervals: [3_000],
    }).toBe(true);
    expect(renewed!.token).not.toEqual(first);
    expect(endpoint.refused, 'every grant request was well formed').toEqual([]);
  } finally {
    await endpoint.close();
  }
});
