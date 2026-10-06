import { expect, it } from 'vitest';
import { readArtifactSSE } from './api';
it('reads event ids and JSON when network chunks split CRLF boundaries', async () => {
  const chunks = [
    'id: 7\r',
    '\nevent: version\r\n',
    'data: {"version":2}\r',
    '\n\r',
    '\n',
  ];
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
      controller.close();
    },
  });
  const events: unknown[][] = [];
  await readArtifactSSE(new Response(stream), (...event) => events.push(event));
  expect(events).toEqual([['version', { version: 2 }, '7']]);
});
