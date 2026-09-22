/** A real HTTP recipient for the supported generic_json channel. */
import { randomUUID } from 'node:crypto';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';

import { absoluteBaseUrl } from './env';

export async function channelCallback() {
  const deliveries: Array<{ text: string; receivedAt: number }> = [];
  const errors: string[] = [];
  const route = `/channel-reply/${randomUUID()}`;
  // The maintained AWS runner and the selected deployment share this host.
  // A container cannot reach the runner at the container's own loopback.
  const advertised = new URL(absoluteBaseUrl());
  if (['localhost', '127.0.0.1', '[::1]'].includes(advertised.hostname)) {
    throw new Error('channel callback requires the selected AWS worker address, not a loopback base URL');
  }
  const server = createServer(async (request, response) => {
    if (request.method !== 'POST' || request.url !== route) {
      response.writeHead(404).end();
      return;
    }
    try {
      const chunks: Buffer[] = [];
      for await (const chunk of request) chunks.push(Buffer.from(chunk));
      const payload: unknown = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)
        || Object.keys(payload).join(',') !== 'text'
        || typeof (payload as { text?: unknown }).text !== 'string') {
        throw new Error('generic_json delivery must be exactly a text object');
      }
      deliveries.push({ text: (payload as { text: string }).text, receivedAt: Date.now() });
      response.writeHead(200, { 'Content-Type': 'application/json' }).end('{}');
    } catch (error) {
      errors.push(String(error));
      response.writeHead(400).end('invalid channel delivery');
    }
  });
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '0.0.0.0', () => {
      server.off('error', reject);
      resolve();
    });
  });
  server.on('error', (error) => errors.push(error.message));
  const address = server.address() as AddressInfo;
  advertised.protocol = 'http:';
  advertised.port = String(address.port);
  advertised.pathname = route;
  advertised.search = '';
  advertised.hash = '';
  return {
    url: advertised.toString(), deliveries, errors,
    close: () => new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
      server.closeAllConnections();
    }),
  };
}
