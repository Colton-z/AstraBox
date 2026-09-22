/**
 * E2E: an in-box HTTP/WebSocket service is reached through OpenSandbox's URL.
 *
 * This deliberately crosses the whole product path: create a real conversation
 * whose sandbox belongs to that conversation, start a service through the
 * terminal API, ask AstraBox for the exposed-port URL, then let Chromium load
 * HTML, a relative stylesheet, and a WebSocket from that URL. A shared Agent's
 * terminal uses a disposable OpenSandbox isolation session, where a background
 * listener is deliberately not durable, so that placement cannot prove this
 * endpoint contract. The returned address must not be AstraBox's former byte
 * proxy.
 */
import { test, expect } from '@playwright/test';
import type { Locator, Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';

interface ExposedPortUrl {
  url: string;
  port: number;
}

const ENDPOINT_KINDS = ['docker', 'direct', 'gateway-uri'] as const;
type EndpointKind = (typeof ENDPOINT_KINDS)[number];

function resolveExposePort(): number {
  const raw = process.env.ASTRABOX_E2E_EXPOSE_PORT;
  if (!raw) return 5173;
  const value = Number.parseInt(raw, 10);
  if (!Number.isInteger(value) || value <= 0 || value > 65535) {
    throw new Error('ASTRABOX_E2E_EXPOSE_PORT must be a TCP port (1-65535)');
  }
  return value;
}

function resolveEndpointKind(): EndpointKind {
  const value = process.env.ASTRABOX_E2E_ENDPOINT_KIND || 'docker';
  const kind = ENDPOINT_KINDS.find((candidate) => candidate === value);
  if (!kind) {
    throw new Error(
      `ASTRABOX_E2E_ENDPOINT_KIND must be ${ENDPOINT_KINDS.join(', ')}; got ${value}`,
    );
  }
  return kind;
}

const PORT = resolveExposePort();
const ENDPOINT_KIND = resolveEndpointKind();
const GREEN = 'rgb(0, 128, 0)';
const FETCH_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_EXPOSE_PORT_FETCH_TIMEOUT_MS', 15_000);

async function gotoWithRetry(page: Page, url: string, budgetMs = FETCH_TIMEOUT_MS): Promise<void> {
  const deadline = Date.now() + budgetMs;
  let lastErr = 'no attempt completed';
  for (;;) {
    try {
      const response = await page.goto(url, { timeout: FETCH_TIMEOUT_MS });
      if (response?.ok()) return;
      lastErr = response ? `HTTP ${response.status()}` : 'no navigation response';
    } catch (error) {
      lastErr = error instanceof Error ? error.message : String(error);
    }
    if (Date.now() >= deadline) {
      throw new Error(`GET ${url} did not become reachable within ${budgetMs}ms: ${lastErr}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
}

async function expectBodyToContainMarker(body: Locator, marker: string): Promise<void> {
  try {
    await expect(body).toContainText(marker, { useInnerText: true });
  } catch (error) {
    const received = await body.innerText();
    expect(
      received,
      `in-box marker ${JSON.stringify(marker)} missing; received body prefix (500 chars max): ${JSON.stringify(received.slice(0, 500))}`,
    ).toContain(marker);
    throw error;
  }
}

function expectEndpointShape(endpoint: URL, kind: EndpointKind, port: number): void {
  expect(endpoint.hostname, `${kind} endpoint must identify a host`).not.toBe('');
  const endpointPath = endpoint.pathname;
  if (kind === 'docker') {
    const expectedPath = `/proxy/${port}`;
    expect(
      endpointPath,
      `expected Docker endpoint path to contain ${JSON.stringify(expectedPath)}; received endpoint path prefix (500 chars max): ${JSON.stringify(endpointPath.slice(0, 500))}`,
    ).toContain(expectedPath);
    return;
  }
  if (kind === 'direct') {
    expect(
      endpointPath,
      `expected OpenSandbox direct endpoint to use the root path; received ${JSON.stringify(endpointPath)}`,
    ).toBe('/');
    return;
  }
  if (kind === 'gateway-uri') {
    expect(endpointPath).not.toContain(`/proxy/${port}`);
    expect(endpointPath).not.toBe('/');
    return;
  }
  const unsupported: never = kind;
  throw new Error(`unsupported endpoint kind ${unsupported}`);
}

function inBoxServer(marker: string): string {
  return String.raw`
import asyncio, base64, hashlib, sys

PORT = int(sys.argv[1])
MARKER = ${JSON.stringify(marker)}
GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

async def read_frame(reader):
    head = await reader.readexactly(2)
    length = head[1] & 0x7f
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), 'big')
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), 'big')
    mask = await reader.readexactly(4) if head[1] & 0x80 else b''
    payload = await reader.readexactly(length)
    if mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return payload

async def handle(reader, writer):
    try:
        raw = await reader.readuntil(b'\r\n\r\n')
        lines = raw.decode('latin1').split('\r\n')
        path = lines[0].split(' ')[1]
        headers = {}
        for line in lines[1:]:
            if ':' in line:
                key, value = line.split(':', 1)
                headers[key.strip().lower()] = value.strip()
        if headers.get('upgrade', '').lower() == 'websocket':
            accept = base64.b64encode(hashlib.sha1((headers['sec-websocket-key'] + GUID).encode()).digest()).decode()
            writer.write(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n').encode())
            await writer.drain()
            payload = await read_frame(reader)
            reply = b'pong:' + payload
            writer.write(bytes([0x81, len(reply)]) + reply)
            await writer.drain()
            return
        if path.split('?', 1)[0].endswith('/style.css'):
            body, content_type = b'body{color:green}', 'text/css'
        elif path.split('?', 1)[0].endswith('/index.html') or path.split('?', 1)[0].endswith('/'):
            body = ('''<!doctype html><html><head><link rel="stylesheet" href="style.css"></head>
<body>''' + MARKER + '''<script>
const wsUrl = new URL('socket', location.href); wsUrl.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
const ws = new WebSocket(wsUrl); ws.onopen = () => ws.send('astrabox');
ws.onmessage = (event) => document.body.dataset.ws = event.data;
</script></body></html>''').encode()
            content_type = 'text/html; charset=utf-8'
        else:
            body, content_type = b'not found', 'text/plain'
        status = '200 OK' if body != b'not found' else '404 Not Found'
        writer.write((f'HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode() + body)
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()

async def main():
    server = await asyncio.start_server(handle, '0.0.0.0', PORT)
    async with server:
        await server.serve_forever()

asyncio.run(main())
`;
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('endpoint kinds preserve their deployment-native URL shapes', () => {
  expectEndpointShape(new URL(`http://127.0.0.1:41000/proxy/${PORT}`), 'docker', PORT);
  expectEndpointShape(new URL(`http://10.42.0.8:${PORT}/`), 'direct', PORT);
  expectEndpointShape(new URL(`https://sandbox.example/sandbox-id/${PORT}`), 'gateway-uri', PORT);
});

test('expose-port uses the OpenSandbox HTTP and WebSocket data plane', async ({ page, request }) => {
  const api = new AstraApi(request);
  const marker = `EXPOSE_PORT_E2E_OK_${new Date().toISOString().replace(/[:.]/g, '-')}`;
  const browserResponses: Array<{ url: string; status: number }> = [];
  page.on('response', (response) => browserResponses.push({ url: response.url(), status: response.status() }));

  const agent = await api.createColdTestAgent(
    `__e2e_expose_port_${new Date().toISOString().replace(/[:.]/g, '-')}`,
  );
  agentId = String(agent.agent_id || '');
  expect(agentId, 'conversation-tenancy Agent created').not.toEqual('');
  expect(
    String(agent.sandbox_id || '').trim(),
    'conversation-tenancy Agent must not own a shared sandbox',
  ).toEqual('');
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  expect(sessionId, 'conversation created').toBeTruthy();
  let exposedUrl = '';

  try {
    await api.waitForSessionReady(sessionId);
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible();

    const source = Buffer.from(inBoxServer(marker), 'utf8').toString('base64');
    await api.runTerminalCommand(
      sessionId,
      `printf '%s' '${source}' | base64 -d > /tmp/astrabox_expose_e2e.py && ` +
        `nohup python3 /tmp/astrabox_expose_e2e.py ${PORT} >/tmp/astrabox_expose_e2e.log 2>&1 & sleep 2`,
    );

    const result = await api.data<ExposedPortUrl>('GET', `/exposed-ports/${sessionId}/${PORT}/url`);
    expect(result.port).toBe(PORT);
    expect(result.url).toMatch(/^https?:\/\//);
    expect(result.url).not.toContain('/api/v1/exposed-ports/');
    exposedUrl = result.url.replace(/\/+$/, '');
    expectEndpointShape(new URL(exposedUrl), ENDPOINT_KIND, PORT);

    await gotoWithRetry(page, `${exposedUrl}/`);
    const body = page.locator('body');
    await expect(body).toBeVisible();
    await expectBodyToContainMarker(body, marker);
    await expect
      .poll(
        () => browserResponses.filter((entry) => /\/style\.css(?:\?|$)/.test(entry.url)).map((entry) => entry.status),
        { timeout: FETCH_TIMEOUT_MS },
      )
      .toContain(200);
    await expect.poll(() => body.getAttribute('data-ws'), { timeout: FETCH_TIMEOUT_MS }).toBe('pong:astrabox');
    expect(await body.evaluate((element) => getComputedStyle(element).color)).toBe(GREEN);

    await gotoWithRetry(page, `${exposedUrl}/index.html`);
    await expectBodyToContainMarker(page.locator('body'), marker);

    // The owner can refresh a native/signed URL through AstraBox's control plane.
    const refreshed = await api.data<ExposedPortUrl>('GET', `/exposed-ports/${sessionId}/${PORT}/url`);
    expect(refreshed.url).toMatch(/^https?:\/\//);
    expect(refreshed.url).not.toContain('/api/v1/exposed-ports/');
  } catch (error) {
    const log = await api
      .runTerminalCommand(sessionId, 'tail -n 60 /tmp/astrabox_expose_e2e.log 2>/dev/null || true')
      .catch(() => '');
    const message = error instanceof Error ? error.message : String(error);
    const seen = browserResponses
      .filter((entry) => !exposedUrl || entry.url.startsWith(exposedUrl))
      .map((entry) => `${entry.status} ${entry.url}`)
      .join('\n');
    throw new Error(`${message}\n--- native endpoint responses ---\n${seen}\n--- in-box server log ---\n${log.slice(0, 1200)}`);
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
