/** A deployment's HTTP transport, installed before the console starts. */
export type ConsoleTransport = (request: Request) => Promise<Response>;

declare global {
  var astraboxConsoleTransport: ConsoleTransport | undefined;
}

/** Send console API traffic through the deployment's transport when configured. */
export async function consoleFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const transport = globalThis.astraboxConsoleTransport;
  if (transport === undefined) return globalThis.fetch(input, init);
  const target = typeof input === 'string' ? new URL(input, window.location.href) : input;
  const request = new Request(target, init);
  request.signal.throwIfAborted();
  return transport(request);
}
