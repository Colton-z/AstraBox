/**
 * E2E: the console works when a gateway publishes AstraBox below a path.
 *
 * The deployment runs with ASTRABOX_PUBLIC_PATH_PREFIX set to the path a
 * reverse proxy strips in front of it (fixtures/path-prefix-proxy.Caddyfile),
 * and that proxy answers 404 for every path outside the prefix. A URL the
 * console or the server builds from the host root therefore fails instead of
 * reaching AstraBox at the root by accident. Every request the browser makes to
 * the console's origin is recorded, and each one must stay below the prefix.
 *
 * One reader's journey, each step checked against evidence outside the page:
 * - signing in from a deep link: the callback URL the server gives the identity
 *   provider, both cookie paths in the browser's cookie jar, and the page the
 *   reader is returned to;
 * - refreshing that deep link, and the Agents list compared with the API's own
 *   answer;
 * - a live response that publishes an artifact, delivered over the session's
 *   stream;
 * - the artifact's share link opened without a session. Its frame renders only
 *   when the artifact runtime loads from below the prefix, which the frame's
 *   content policy names;
 * - the conversation's share link, and the file it offers for download;
 * - the MCP endpoint the console tells clients to use, called with the key the
 *   console issues;
 * - signing out;
 * - a request a proxy forwarded with the prefix still on it, which AstraBox
 *   refuses with the setting's name.
 */
import { test, expect, type BrowserContext } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { simplePage } from '../fixtures/artifactPages';
import {
  artifactFrame,
  artifactResources,
  marker,
  openConversation,
  publishArtifact,
  shareArtifact,
  type ArtifactScene,
} from '../fixtures/artifactScenario';
import { ArtifactsApi } from '../fixtures/artifactsApi';
import { apiPath, appPath, appPrefix } from '../fixtures/env';
import { submitIssuerSignIn } from '../fixtures/oidcLogin';

const resources = artifactResources();
const SHARED_FILE = 'below-the-prefix.txt';

/** Record each request *context* makes to *origin* outside the prefix. */
function recordEscapes(context: BrowserContext, origin: string, escaped: string[]): void {
  context.on('request', (request) => {
    const url = new URL(request.url());
    if (url.origin !== origin) return;
    // The browser asks an origin for its icon on its own; no page names it.
    if (url.pathname === '/favicon.ico') return;
    if (url.pathname === appPrefix || url.pathname.startsWith(`${appPrefix}/`)) return;
    escaped.push(`${request.method()} ${url.pathname}`);
  });
}

test('a console published below a gateway path keeps every address below it', async ({ page, request, browser }) => {
  const origin = String(process.env.ASTRABOX_E2E_CONSOLE_URL || '').replace(/\/$/, '');
  const issuer = String(process.env.ASTRABOX_E2E_OIDC_ISSUER || '').replace(/\/$/, '');
  expect(origin, 'ASTRABOX_E2E_CONSOLE_URL is the origin browsers reach the gateway at').not.toBe('');
  expect(new URL(origin).pathname, 'the console URL is an origin; the path comes from ASTRABOX_E2E_APP_PREFIX').toBe('/');
  expect(issuer, 'ASTRABOX_E2E_OIDC_ISSUER is the identity provider the deployment signs in with').not.toBe('');
  expect(appPrefix, 'ASTRABOX_E2E_APP_PREFIX is the path the deployment is published under').toMatch(/^(\/[A-Za-z0-9._~-]+)+$/);

  // Every cookie the browser holds for the console's host, whatever its path.
  // cookies(url) returns only the ones the browser would send to that URL, so
  // a cookie scoped below the prefix is missing from cookies(origin).
  const hostCookies = async (context: BrowserContext) => (
    (await context.cookies()).filter((cookie) => cookie.domain === new URL(origin).hostname)
  );

  const escaped: string[] = [];
  recordEscapes(page.context(), origin, escaped);
  const contexts: BrowserContext[] = [];
  try {
    // ── Sign in from a deep link, in a browser with no session ─────────────
    const signIn = await browser.newContext({ baseURL: origin, storageState: { cookies: [], origins: [] } });
    contexts.push(signIn);
    recordEscapes(signIn, origin, escaped);
    const reader = await signIn.newPage();
    await reader.goto(appPath('/manage/agents'));
    await reader.waitForURL((url) => url.pathname === appPath('/login') && url.searchParams.get('next') === '/manage/agents');

    const startLogin = reader.waitForResponse((response) => new URL(response.url()).pathname === apiPath('/auth/login'));
    await reader.getByRole('button', { name: 'Continue with your organization account' }).click();
    const toProvider = new URL(String((await startLogin).headers().location || ''));
    expect(toProvider.origin, 'sign-in starts at the identity provider').toBe(issuer);
    expect(toProvider.searchParams.get('redirect_uri'), 'the provider must send the browser back below the prefix')
      .toBe(`${origin}${apiPath('/auth/callback')}`);
    await reader.waitForURL((url) => url.origin === issuer, { timeout: 30_000 });
    const flowCookie = (await hostCookies(signIn)).find((cookie) => cookie.name === 'astrabox_auth_flow');
    expect(flowCookie?.path, 'the sign-in flow cookie must reach the callback below the prefix').toBe(apiPath('/auth/'));

    await submitIssuerSignIn(reader);
    await reader.waitForURL((url) => url.origin === origin && url.pathname === appPath('/manage/agents'), { timeout: 30_000 });
    const jar = await hostCookies(signIn);
    expect(jar.find((cookie) => cookie.name === 'astrabox_session')?.path, 'the session is scoped to the prefix').toBe(appPrefix);
    expect(jar.find((cookie) => cookie.name === 'astrabox_auth_flow'), 'the callback consumes the flow cookie').toBeUndefined();

    // ── Refresh the deep link; the rows are the API's own answer ───────────
    const agentsRead = reader.waitForResponse((response) => (
      new URL(response.url()).pathname === apiPath('/agents') && response.request().method() === 'GET'
    ));
    await reader.reload();
    const agentsResponse = await agentsRead;
    expect(agentsResponse.status(), 'the Agents page reads the API below the prefix').toBe(200);
    const listed = (await agentsResponse.json()) as { data: { agents: Array<{ name: string }> } };
    expect(listed.data.agents.length, 'a deployment carries at least its seeded Agent').toBeGreaterThan(0);
    for (const agent of listed.data.agents.slice(0, 3)) {
      await expect(reader.getByText(agent.name, { exact: true }).first()).toBeVisible();
    }

    // ── A live response publishes an artifact over the session's stream ────
    const streams: string[] = [];
    page.on('response', (response) => {
      const url = new URL(response.url());
      if (
        /\/sessions\/[^/]+\/stream$/.test(url.pathname)
        && String(response.headers()['content-type'] || '').startsWith('text/event-stream')
      ) {
        streams.push(url.pathname);
      }
    });
    const api = new AstraApi(request);
    const agent = await api.defaultAgent();
    const sessionId = (await api.startConversation(String(agent.agent_id))).session_id;
    resources.sessions.push(sessionId);
    test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
    const ready = await api.waitForSessionReady(sessionId);
    test.info().annotations.push({ type: 'e2e_sandbox_id', description: String(ready.sandbox_id) });
    await openConversation(page, sessionId);
    const scene: ArtifactScene = {
      api, artifacts: new ArtifactsApi(request), sessionId, agentId: String(agent.agent_id), resources,
    };
    const shown = marker('PREFIXED');
    await publishArtifact(page, scene, marker('Prefixed artifact'), simplePage(shown), {});
    await expect(artifactFrame(page).locator('#marker')).toHaveText(shown);
    expect(streams, 'the response arrives over the session stream below the prefix')
      .toContain(apiPath(`/sessions/${sessionId}/stream`));

    // ── The artifact's share link, opened without a session ────────────────
    const sharedArtifact = await shareArtifact(page);
    expect(sharedArtifact.url.startsWith(`${origin}${appPath('/share/artifacts/')}`), sharedArtifact.url).toBe(true);
    const anonymous = await browser.newContext({ baseURL: origin, storageState: { cookies: [], origins: [] } });
    contexts.push(anonymous);
    recordEscapes(anonymous, origin, escaped);
    const artifactViewer = await anonymous.newPage();
    await artifactViewer.goto(sharedArtifact.url);
    await expect(artifactFrame(artifactViewer).locator('#marker')).toHaveText(shown);

    // ── The conversation's share link and the file it offers ──────────────
    const fileText = marker('SHARED_FILE');
    await api.runTerminalCommand(sessionId, `printf '%s' ${JSON.stringify(fileText)} > ${SHARED_FILE}`);
    await page.getByRole('button', { name: 'Share', exact: true }).click();
    const shareDialog = page.getByRole('dialog', { name: 'Share conversation' });
    await shareDialog.getByRole('switch').first().click();
    await shareDialog.getByRole('button', { name: 'Generate share link' }).click();
    const shareField = shareDialog.locator('input').first();
    await expect(shareField).toHaveValue(/\/share\/[^/]+$/);
    const sessionShare = new URL(await shareField.inputValue());
    await page.keyboard.press('Escape');
    expect(sessionShare.origin + sessionShare.pathname.replace(/[^/]+$/, ''), 'the share link sits below the prefix')
      .toBe(`${origin}${appPath('/share/')}`);
    const token = sessionShare.pathname.split('/').at(-1)!;
    const conversationViewer = await anonymous.newPage();
    await conversationViewer.goto(sessionShare.toString());
    await expect(conversationViewer.getByTestId('assistant-message').last()).toBeVisible({ timeout: 60_000 });
    const fileLink = conversationViewer.getByRole('link', { name: SHARED_FILE });
    await expect(fileLink).toBeVisible({ timeout: 60_000 });
    const download = new URL(String(await fileLink.getAttribute('href')), origin);
    expect(download.pathname, 'the download link sits below the prefix').toBe(apiPath(`/share/${token}/files/download`));
    const downloaded = await anonymous.request.get(download.toString());
    expect(downloaded.status()).toBe(200);
    expect(await downloaded.text(), 'the download is the file in the conversation').toBe(fileText);

    // ── The MCP endpoint the console tells clients to use ──────────────────
    await page.goto(appPath('/manage/mcp-tokens'));
    const keyName = marker('prefixed_mcp');
    await page.getByRole('textbox', { name: 'Name', exact: true }).fill(keyName);
    await page.getByRole('button', { name: 'Issue key' }).click();
    await expect(page.getByRole('heading', { name: `${keyName} is ready` })).toBeVisible();
    // Read as the text a reader pastes: the page renders one `<pre>` while a
    // key is on screen.
    const pasted = String(await page.locator('pre').textContent());
    const endpoint = pasted.match(/"url":\s*"([^"]+)"/)?.[1];
    const authorization = pasted.match(/"Authorization":\s*"(Bearer [^"]+)"/)?.[1];
    expect(endpoint, 'the pasted block names the MCP endpoint below the prefix').toBe(`${origin}${apiPath('/mcp')}`);
    expect(authorization, 'the pasted block carries the key').toBeTruthy();
    const mcp = async (body: Record<string, unknown>, protocolVersion?: string) => {
      const response = await fetch(String(endpoint), {
        method: 'POST',
        headers: {
          Authorization: String(authorization),
          'Content-Type': 'application/json',
          Accept: 'application/json, text/event-stream',
          ...(protocolVersion ? { 'MCP-Protocol-Version': protocolVersion } : {}),
        },
        body: JSON.stringify({ jsonrpc: '2.0', ...body }),
      });
      expect(response.status, `MCP ${String(body.method)} through the gateway`).toBe(200);
      return await response.json() as { result: Record<string, unknown> };
    };
    const initialized = await mcp({
      id: 1, method: 'initialize',
      params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'astrabox-e2e', version: '1' } },
    });
    const toolList = await mcp({ id: 2, method: 'tools/list' }, String(initialized.result.protocolVersion));
    const tools = (toolList.result.tools as Array<{ name: string }>).map((tool) => tool.name);
    expect(tools, 'the key reaches the Agent tools through the endpoint the console showed').toContain('list_agents');
    await page.getByRole('button', { name: 'I have copied it' }).click();
    const issuedRow = page.getByRole('row').filter({ hasText: keyName });
    await issuedRow.getByRole('button', { name: 'Revoke' }).click();
    await expect(issuedRow).toHaveCount(0);

    // ── Sign out ────────────────────────────────────────────────────────────
    const signOut = reader.waitForResponse((response) => new URL(response.url()).pathname === apiPath('/auth/logout'));
    await reader.getByRole('button', { name: 'Settings', exact: true }).click();
    await reader.getByRole('menuitem', { name: 'Sign out' }).click();
    expect((await signOut).status()).toBe(204);
    await reader.waitForURL((url) => url.pathname === appPath('/login'), { timeout: 30_000 });
    expect(
      (await hostCookies(signIn)).find((cookie) => cookie.name === 'astrabox_session'),
      'signing out clears the session cookie scoped to the prefix',
    ).toBeUndefined();

    // ── A request forwarded with the prefix still on it ────────────────────
    const shell = await (await request.get(appPath('/'))).text();
    const entry = shell.match(/\/assets\/main-[\w-]+\.js/)?.[0];
    expect(entry, 'the served console names its hashed entry').toBeTruthy();
    expect(shell, 'the served console names its entry below the prefix').toContain(`${appPrefix}${entry}`);
    const forwarded = await request.get(`${appPrefix}${appPrefix}${entry}`);
    expect(forwarded.status(), 'AstraBox refuses a path that still carries the prefix').toBe(404);
    expect((await forwarded.json()).code).toBe('PUBLIC_PATH_PREFIX_NOT_REMOVED');

    expect(escaped, 'every request the console makes stays below the prefix').toEqual([]);
  } finally {
    for (const context of contexts) await context.close();
  }
});
