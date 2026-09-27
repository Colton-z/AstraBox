import { randomUUID } from 'node:crypto';
import { createServer, request as httpRequest } from 'node:http';
import { isIP, type AddressInfo } from 'node:net';

export interface NativeMcpRelay {
  url: URL;
  close(): Promise<void>;
}

/** Forward the test's one MCP route from the node to its server Pod. */
export async function startNativeMcpRelay(
  host: string, endpoint: URL, port: number, errors: string[], listenPort: number,
): Promise<NativeMcpRelay> {
  if (isIP(host) !== 4 || isIP(endpoint.hostname) !== 4 || endpoint.protocol !== 'http:'
    || endpoint.pathname !== '/' || endpoint.port !== String(port)
    || endpoint.search || endpoint.hash || endpoint.username || endpoint.password) {
    throw new Error(`direct MCP relay requires a node IPv4 base URL and a plain Pod endpoint: ${endpoint}`);
  }
  const path = `/e2e-mcp-${randomUUID()}`;
  const active = new Set<() => Promise<void>>();
  const relay = createServer((incoming, outgoing) => {
    if (incoming.url !== `${path}/mcp`) {
      outgoing.writeHead(404).end();
      return;
    }
    let cancelled = false;
    let failed = false;
    const fail = (error: Error) => {
      if (cancelled || failed) return;
      failed = true;
      errors.push(error.message);
      if (!outgoing.headersSent) outgoing.writeHead(502).end(error.message);
      else outgoing.destroy();
      upstream.destroy();
    };
    const upstream = httpRequest({
      hostname: endpoint.hostname, port, path: '/mcp', method: incoming.method,
      headers: { ...incoming.headers, host: `${endpoint.hostname}:${port}` },
      agent: false,
    }, (response) => {
      response.on('error', fail);
      outgoing.writeHead(response.statusCode || 502, response.headers);
      response.pipe(outgoing);
    });
    const closed = new Promise<void>((resolve) => upstream.once('close', resolve));
    const cancel = () => {
      cancelled = true;
      upstream.destroy();
      return closed;
    };
    active.add(cancel);
    upstream.once('close', () => active.delete(cancel));
    upstream.on('error', fail);
    incoming.on('error', () => { void cancel(); });
    incoming.on('close', () => { if (!incoming.complete) void cancel(); });
    outgoing.on('error', fail);
    // A POST body can finish long before its streaming response is cancelled.
    outgoing.on('close', () => { if (!outgoing.writableFinished) void cancel(); });
    incoming.pipe(upstream);
  });
  await new Promise<void>((resolve, reject) => {
    relay.once('error', reject);
    relay.listen(listenPort, host, () => {
      relay.off('error', reject);
      resolve();
    });
  });
  relay.on('error', (error) => errors.push(error.message));
  return {
    url: new URL(`http://${host}:${(relay.address() as AddressInfo).port}${path}`),
    async close() {
      const closed = new Promise<void>((resolve, reject) => {
        relay.close((error) => error ? reject(error) : resolve());
      });
      const upstreams = [...active].map((cancel) => cancel());
      relay.closeAllConnections();
      await Promise.all([closed, ...upstreams]);
    },
  };
}
