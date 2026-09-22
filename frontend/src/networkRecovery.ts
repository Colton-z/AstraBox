const SAFE_METHODS = new Set(['GET', 'HEAD']);
const SAFE_RETRY_DELAYS_MS = [100, 300] as const;
const IDEMPOTENT_MUTATION_RETRY_DELAYS_MS = [100, 300, 1_000, 2_000, 5_000] as const;

/** A request failed before an HTTP response could be read. */
export class NetworkRequestError extends Error {
  override name = 'NetworkRequestError';
}

export function isNetworkRequestError(error: unknown): error is NetworkRequestError {
  return error instanceof NetworkRequestError;
}

export interface NetworkRecoveryOptions {
  mutationReplay?: 'convergent' | 'idempotent';
}

function abortReason(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException('The operation was aborted.', 'AbortError');
}

function waitForRetry(delayMs: number, signal?: AbortSignal | null): Promise<void> {
  if (signal?.aborted) return Promise.reject(abortReason(signal));
  return new Promise((resolve, reject) => {
    const timer = globalThis.setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, delayMs);
    const onAbort = () => {
      globalThis.clearTimeout(timer);
      reject(abortReason(signal!));
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

export function isAbortError(error: unknown): boolean {
  return Boolean(
    error
    && typeof error === 'object'
    && String((error as { name?: unknown }).name ?? '') === 'AbortError',
  );
}

/**
 * Recover a safe browser read from a short-lived transport change.
 *
 * A browser can reject in-flight requests while its network route is changing
 * even though the backend remains healthy. GET and HEAD are safe to replay, so
 * give those reads two short recovery attempts. HTTP responses are returned as
 * received. A mutation is replayed only when its caller names the server-side
 * guarantee that makes an ambiguous retry safe. Convergent mutations need only
 * the short read budget; an idempotency-keyed mutation gets a longer backoff so
 * a real route change can settle. AbortSignal remains an immediate cancellation
 * boundary.
 */
export async function fetchWithNetworkRecovery(
  input: string,
  init: RequestInit = {},
  options: NetworkRecoveryOptions = {},
): Promise<Response> {
  const method = String(init.method || 'GET').toUpperCase();
  const retryDelays = SAFE_METHODS.has(method) || options.mutationReplay === 'convergent'
    ? SAFE_RETRY_DELAYS_MS
    : options.mutationReplay === 'idempotent'
      ? IDEMPOTENT_MUTATION_RETRY_DELAYS_MS
      : [];

  for (let attempt = 0; ; attempt += 1) {
    try {
      return await fetch(input, init);
    } catch (error) {
      if (isAbortError(error)) throw error;
      if (init.signal?.aborted) throw abortReason(init.signal);
      const delay = retryDelays[attempt];
      if (delay === undefined) throw error;
      await waitForRetry(delay, init.signal);
    }
  }
}
