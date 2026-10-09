/** A real HTTP recipient for the supported generic_json channel. */
import { randomUUID } from 'node:crypto';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';

import { absoluteBaseUrl } from './env';

/** The structured question a generic_json reply carries while the Agent waits. */
export interface ChannelQuestion {
  interaction_id: string;
  presentation: string;
  title: string;
  detail: string;
  choices: Array<{ number: number; label: string; description: string; action: string }>;
  multi_select: boolean;
  free_text: boolean;
  position: [number, number];
  text: string;
  version: number;
}

export interface ChannelDelivery {
  text: string;
  messageId: string;
  question: ChannelQuestion | null;
  receivedAt: number;
}

const DELIVERY_KEYS = new Set(['text', 'message_id', 'question']);

export async function channelCallback() {
  const deliveries: ChannelDelivery[] = [];
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
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        throw new Error('generic_json delivery must be an object');
      }
      const body = payload as { text?: unknown; message_id?: unknown; question?: unknown };
      const unknownKeys = Object.keys(body).filter((key) => !DELIVERY_KEYS.has(key));
      if (unknownKeys.length || typeof body.text !== 'string' || typeof body.message_id !== 'string'
        || !body.message_id || (body.question !== undefined
          && (!body.question || typeof body.question !== 'object' || Array.isArray(body.question)))) {
        throw new Error(`generic_json delivery has an unexpected shape: ${JSON.stringify(body)}`);
      }
      deliveries.push({
        text: body.text,
        messageId: body.message_id,
        question: (body.question as ChannelQuestion | undefined) ?? null,
        receivedAt: Date.now(),
      });
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
