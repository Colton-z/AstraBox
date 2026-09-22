/**
 * Cut a session stream whose socket has died without saying so.
 *
 * The server writes an SSE keepalive comment every 15 seconds of silence, so
 * a healthy stream never goes long without bytes even while the engine is
 * thinking. A socket that dies underneath the browser — a laptop sleep, a VPN
 * flip, a network change — does not: the fetch stays pending, the page keeps
 * a composer that works and a status that reads ready, and the reply to the
 * next message goes to a connection nobody is reading. Ending the body here
 * turns that silence into the disconnect the hook already knows how to resume.
 *
 * Timers are suspended while the machine sleeps, so on wake the next check
 * sees the whole gap at once and ends the stream then.
 */

/** Three missed keepalives: long enough for a slow proxy, short enough to notice. */
export const STREAM_SILENCE_LIMIT_MS = 45_000;

const CHECK_EVERY_MS = 5_000;

export function withStreamLiveness(
  response: Response,
  options: { limitMs?: number; now?: () => number } = {},
): Response {
  const body = response.body;
  if (!body) return response;
  const limit = options.limitMs ?? STREAM_SILENCE_LIMIT_MS;
  const now = options.now ?? (() => Date.now());
  const reader = body.getReader();
  let lastByteAt = now();
  let ended = false;
  let watchdog: ReturnType<typeof setInterval> | null = null;
  let controllerRef: ReadableStreamDefaultController<Uint8Array> | null = null;

  const finish = () => {
    if (ended) return;
    ended = true;
    if (watchdog !== null) clearInterval(watchdog);
    watchdog = null;
  };

  const guarded = new ReadableStream<Uint8Array>({
    start(controller) {
      controllerRef = controller;
      watchdog = setInterval(() => {
        if (ended) return;
        if (now() - lastByteAt <= limit) return;
        // The socket is silent past what a live one can be. Ending the body
        // cleanly makes the reader see a closed stream, which is the
        // disconnect path; the underlying fetch is released with it.
        finish();
        // A body that already errored rejects its cancel; there is nothing
        // to release and nothing to report.
        reader.cancel().catch(() => undefined);
        try {
          controller.close();
        } catch {
          // already closed by a concurrent pull
        }
      }, CHECK_EVERY_MS);
    },
    async pull(controller) {
      const { done, value } = await reader.read();
      if (done) {
        finish();
        try {
          controller.close();
        } catch {
          // closed by the watchdog first
        }
        return;
      }
      lastByteAt = now();
      if (!ended) controller.enqueue(value);
    },
    cancel(reason) {
      finish();
      return reader.cancel(reason);
    },
  });
  void controllerRef;
  return new Response(guarded, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
}
