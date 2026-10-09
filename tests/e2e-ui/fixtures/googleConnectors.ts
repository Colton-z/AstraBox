/**
 * Google connectors against a real Google account, for the external specs.
 *
 * Google's consent screen needs a person signed in to Google, so each consent
 * prints `EXTERNAL_CONNECTOR_CONSENT` with the URL to open in a browser where
 * the tester is signed in, and the caller waits for the result.
 *
 * Expected answers come from Google, not from AstraBox: `callHubTool` asks the
 * hub's server for one connector directly, with one AstraBox user's hub
 * identity, from a fresh process of the deployed server image, so it reads the
 * deployment's own hub settings and no Agent is involved.
 *
 * Fixture: ASTRABOX_E2E_GOOGLE_CLIENT_FILE names an absolute, mode-0600 JSON
 * file holding the deployment's Google OAuth web client, `client_id` and
 * `client_secret`, whose redirect URIs include the deployment's
 * `/connector-hub/oauth/callback`.
 */
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

import { expect, type APIRequestContext } from '@playwright/test';

import type { AstraApi } from './astraApi';
import { apiPath } from './env';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from './serviceContainer';

export const CONSENT_TIMEOUT_MS = 150_000;

export interface ConnectorRow {
  connector_id: string;
  name?: string | null;
  available?: boolean | null;
  status?: string | null;
  deployed?: boolean | null;
  configured?: boolean | null;
  published?: boolean | null;
  redirect_uri?: string | null;
}

export interface HubAnswer {
  is_error: boolean;
  text: string;
}

const HUB_TOOL_PROBE = String.raw`
import asyncio, json, sys
import httpx
from astrabox.core.service.connectors.contextforge import hub_from_env

async def main():
    user_id, server_name, tool, arguments = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
    hub = hub_from_env()
    server = await hub.find_server(server_name)
    if server is None:
        raise SystemExit(f"the hub has no server {server_name}")
    token = hub.session_token(hub.member_email(user_id), ttl_seconds=300)
    url = f"{hub.config.api_url}/servers/{server['id']}/mcp"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}

    def body(response):
        if "text/event-stream" in response.headers.get("content-type", ""):
            return json.loads(next(line[5:] for line in response.text.splitlines() if line.startswith("data:")))
        return response.json()

    async with httpx.AsyncClient(timeout=60) as client:
        opened = await client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "astrabox-e2e", "version": "1"}}})
        opened.raise_for_status()
        if opened.headers.get("mcp-session-id"):
            headers["Mcp-Session-Id"] = opened.headers["mcp-session-id"]
        await client.post(url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        called = body(await client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}))
    if "result" not in called:
        raise SystemExit(f"the hub refused the call: {json.dumps(called)[:300]}")
    result = called["result"]
    text = "".join(part.get("text", "") for part in result.get("content", []) if isinstance(part, dict))
    print(json.dumps({"is_error": bool(result.get("isError")), "text": text}))

asyncio.run(main())
`;

export function readGoogleClient(): { client_id: string; client_secret: string } {
  const envName = 'ASTRABOX_E2E_GOOGLE_CLIENT_FILE';
  const fixturePath = String(process.env[envName] || '').trim();
  if (!path.isAbsolute(fixturePath)) throw new Error(`${envName} must name an absolute mode-0600 JSON file`);
  const stat = fs.lstatSync(fixturePath);
  if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o777) !== 0o600) {
    throw new Error(`${envName} must name a regular, non-symlink mode-0600 JSON file`);
  }
  const parsed = JSON.parse(fs.readFileSync(fixturePath, 'utf8')) as Record<string, unknown>;
  if (typeof parsed.client_id !== 'string' || typeof parsed.client_secret !== 'string') {
    throw new Error(`${envName} must hold client_id and client_secret strings`);
  }
  return { client_id: parsed.client_id, client_secret: parsed.client_secret };
}

/** One tool of a connector's hub server called as one AstraBox user, without AstraBox's Agents. */
export function callHubTool(
  userId: string, connectorId: string, tool: string, args: Record<string, unknown>,
): HubAnswer {
  const result = spawnSync(
    'docker',
    [
      'exec', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-c', HUB_TOOL_PROBE,
      userId, connectorId, tool, JSON.stringify(args),
    ],
    { encoding: 'utf8', timeout: 90_000, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  if (result.error || result.status !== 0) {
    throw new Error(`the direct hub call ${tool} failed: ${String(result.stderr || result.error).slice(-600)}`);
  }
  const line = String(result.stdout || '').trim().split('\n').reverse().find((row) => row.startsWith('{'));
  if (!line) throw new Error(`the direct hub call ${tool} printed no JSON`);
  return JSON.parse(line) as HubAnswer;
}

export async function connectorRow(api: AstraApi, route: string, connectorId: string): Promise<ConnectorRow> {
  const row = (await api.data<ConnectorRow[]>('GET', route)).find((item) => item.connector_id === connectorId);
  expect(row, `${route} must list ${connectorId}`).toBeTruthy();
  return row!;
}

function announceConsent(role: string, connectorId: string, url: string): void {
  console.log(
    `EXTERNAL_CONNECTOR_CONSENT role=${role} connector=${connectorId} `
      + `open this URL where the tester is signed in to Google and allow access: ${url}`,
  );
}

/**
 * An administrator enters the deployment's Google client for one connector and
 * publishes it. Publishing lists the connector's tools with the administrator's
 * own Google token, which a hub that kept one from an earlier run already has;
 * otherwise the administrator consents first.
 */
export async function setUpGoogleConnector(
  api: AstraApi, request: APIRequestContext, connectorId: string,
): Promise<void> {
  const client = readGoogleClient();
  const before = await connectorRow(api, '/admin/connectors', connectorId);
  expect(before.deployed, `the deployment must run the Google overlay's ${connectorId} server`).toBe(true);
  expect(new URL(String(before.redirect_uri)).pathname).toBe('/connector-hub/oauth/callback');

  await api.data('PUT', `/admin/connectors/${connectorId}`, client);
  const { authorize_url: authorizeUrl } = await api.data<{ authorize_url: string }>(
    'POST', `/admin/connectors/${connectorId}/verify`,
  );
  const consent = new URL(authorizeUrl);
  expect(consent.origin).toBe('https://accounts.google.com');
  expect(Object.fromEntries(consent.searchParams)).toMatchObject({
    client_id: client.client_id,
    redirect_uri: String(before.redirect_uri),
    access_type: 'offline',
    prompt: 'consent',
    response_type: 'code',
  });

  const publish = () => request.post(apiPath(`/admin/connectors/${connectorId}/publish`));
  let published = await publish();
  if (!published.ok()) {
    announceConsent('administrator', connectorId, authorizeUrl);
    await expect.poll(async () => {
      published = await publish();
      return published.status();
    }, { timeout: CONSENT_TIMEOUT_MS, intervals: [3_000] }).toBe(200);
  }
  const after = await connectorRow(api, '/admin/connectors', connectorId);
  expect(after).toMatchObject({ configured: true, published: true });
  expect(await connectorRow(api, '/connectors', connectorId)).toMatchObject({ available: true });
}

/** The signed-in person connects one connector from scratch, consenting at Google. */
export async function connectPerson(api: AstraApi, connectorId: string): Promise<void> {
  if ((await connectorRow(api, '/connectors', connectorId)).status === 'connected') {
    await api.data('DELETE', `/connectors/${connectorId}/connection`);
  }
  expect((await connectorRow(api, '/connectors', connectorId)).status).toBe('disconnected');

  const { authorize_url: authorizeUrl } = await api.data<{ authorize_url: string }>(
    'POST', `/connectors/${connectorId}/authorize`,
  );
  expect(new URL(authorizeUrl).searchParams.get('access_type')).toBe('offline');
  announceConsent('person', connectorId, authorizeUrl);
  await expect.poll(
    async () => (await connectorRow(api, '/connectors', connectorId)).status,
    { timeout: CONSENT_TIMEOUT_MS, intervals: [3_000] },
  ).toBe('connected');
}
