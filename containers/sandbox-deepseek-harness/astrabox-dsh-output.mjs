/** One platform collector, carrying the supplier's authenticated mux unchanged. */
import { request, STATUS_CODES } from 'node:http';

export const inject = ['connection', 'webServer'];

function reject(socket, status) {
  const reason = STATUS_CODES[status] ?? 'Bad Gateway';
  socket.end(
    `HTTP/1.1 ${status} ${reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`,
    () => socket.destroy(),
  );
}

export function apply(ctx) {
  let owner;
  const unregister = ctx.webServer.registerUpgrade({
    path: '/astrabox/output',
    handler(req, socket, head) {
      const refusal = ctx.connection.requestRejection(req);
      if (refusal !== undefined) return reject(socket, refusal);
      const takeover = new URL(req.url, 'http://localhost').searchParams.get('takeover');
      if (takeover !== '0' && takeover !== '1') return reject(socket, 400);
      if (owner && takeover === '0') return reject(socket, 409);

      const previous = owner;
      const link = { socket, upstream: undefined, request: undefined, closed: false, close };
      // Reserve before starting upstream I/O, including during the handshake.
      owner = link;
      previous?.close();

      function close() {
        if (link.closed) return;
        link.closed = true;
        if (owner === link) owner = undefined;
        link.request?.destroy();
        link.upstream?.destroy();
        socket.destroy();
      }

      function fail(status) {
        if (link.closed) return;
        if (owner === link) owner = undefined;
        reject(socket, status);
      }

      socket.once('close', close);
      socket.once('error', close);
      const upstream = request({
        hostname: '127.0.0.1',
        port: ctx.webServer.port,
        path: '/api/remote.mux',
        method: 'GET',
        headers: req.headers,
      });
      link.request = upstream;
      upstream.once('error', () => fail(502));
      upstream.once('response', (response) => {
        response.resume();
        fail(response.statusCode ?? 502);
      });
      upstream.once('upgrade', (response, transport, upstreamHead) => {
        if (link.closed || owner !== link) {
          transport.destroy();
          return;
        }
        link.upstream = transport;
        transport.once('close', close);
        transport.once('error', close);
        let headers = `HTTP/1.1 ${response.statusCode} ${response.statusMessage}\r\n`;
        for (let index = 0; index < response.rawHeaders.length; index += 2) {
          headers += `${response.rawHeaders[index]}: ${response.rawHeaders[index + 1]}\r\n`;
        }
        socket.write(`${headers}\r\n`);
        if (upstreamHead.length) socket.write(upstreamHead);
        if (head.length) transport.write(head);
        // Raw streams retain WebSocket messages, control frames and backpressure.
        socket.pipe(transport);
        transport.pipe(socket);
      });
      upstream.end();
    },
  });
  ctx.effect(() => () => {
    unregister();
    owner?.close();
  });
}
