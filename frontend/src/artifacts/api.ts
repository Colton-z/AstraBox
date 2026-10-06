import { runtimeDataPath, runtimeSegment } from '../../artifact-runtime/paths';
import { ApiError, MODULE_BASE } from '@/api';
import type { components, paths } from '@/api/schema';
import type {
  ArtifactPage,
  ArtifactRecord,
  ArtifactRuntime,
  ArtifactShare,
  ArtifactTarget,
  ArtifactVersion,
  ArtifactVersionSummary,
} from './types';
const base = String(import.meta.env.VITE_API_BASE ?? MODULE_BASE).replace(
  /\/$/,
  '',
);
export function artifactUrl(path: string) {
  return `${base}${path}`;
}
const runtimeRoutes = {
  runtime: [
    '/api/v1/artifacts/{artifact_id}/runtime',
    '/api/v1/artifact-share/{token}/runtime',
  ],
  live: [
    '/api/v1/artifacts/{artifact_id}/runtime/live',
    '/api/v1/artifact-share/{token}/runtime/live',
  ],
  publish: [
    '/api/v1/artifacts/{artifact_id}/runtime/publish',
    '/api/v1/artifact-share/{token}/runtime/publish',
  ],
  data: [
    '/api/v1/artifacts/{artifact_id}/runtime/data/{path}',
    '/api/v1/artifact-share/{token}/runtime/data/{path}',
  ],
  query: [
    '/api/v1/artifacts/{artifact_id}/runtime/data-query',
    '/api/v1/artifact-share/{token}/runtime/data-query',
  ],
  add: [
    '/api/v1/artifacts/{artifact_id}/runtime/data-add',
    '/api/v1/artifact-share/{token}/runtime/data-add',
  ],
  sample: [
    '/api/v1/artifacts/{artifact_id}/runtime/sample',
    '/api/v1/artifact-share/{token}/runtime/sample',
  ],
  emit: [
    '/api/v1/artifacts/{artifact_id}/runtime/room/{room}/emit',
    '/api/v1/artifact-share/{token}/runtime/room/{room}/emit',
  ],
  presence: [
    '/api/v1/artifacts/{artifact_id}/runtime/room/{room}/presence',
    '/api/v1/artifact-share/{token}/runtime/room/{room}/presence',
  ],
  viewers: [
    '/api/v1/artifacts/{artifact_id}/runtime/viewers',
    '/api/v1/artifact-share/{token}/runtime/viewers',
  ],
  agentRuns: [
    '/api/v1/artifacts/{artifact_id}/runtime/agent-runs',
    '/api/v1/artifact-share/{token}/runtime/agent-runs',
  ],
  agentStatus: [
    '/api/v1/artifacts/{artifact_id}/runtime/agent-runs/{run_id}',
    '/api/v1/artifact-share/{token}/runtime/agent-runs/{run_id}',
  ],
} as const satisfies Record<string, readonly [keyof paths, keyof paths]>;
export type RuntimeRoute = keyof typeof runtimeRoutes;
export function runtimePath(
  target: ArtifactTarget,
  route: RuntimeRoute = 'runtime',
  parameter = '',
  query: Record<string, string> = {},
) {
  const template = runtimeRoutes[route][target.token ? 1 : 0];
  const identity = runtimeSegment(target.token ?? target.id);
  if (route === 'data') parameter = runtimeDataPath(parameter, true);
  if (['emit', 'presence', 'agentStatus'].includes(route)) parameter = runtimeSegment(parameter);
  const prefix = runtimeRoutes.runtime[target.token ? 1 : 0]
    .replace(/\{artifact_id\}|\{token\}/, encodeURIComponent(identity));
  const path = template
    .replace('{artifact_id}', encodeURIComponent(target.id ?? ''))
    .replace('{token}', encodeURIComponent(target.token ?? ''))
    .replace('{path}', parameter.split('/').map(encodeURIComponent).join('/'))
    .replace(/\{room\}|\{run_id\}/, encodeURIComponent(parameter));
  const params = new URLSearchParams({
    ...query,
    ...(target.token ? { guest_id: target.guestId } : {}),
  });
  const result = `${path}${params.size ? `?${params}` : ''}`;
  const runtimePrefix = new URL(artifactUrl(prefix), window.location.href).pathname;
  const finalPath = new URL(artifactUrl(result), window.location.href).pathname;
  if (finalPath !== runtimePrefix && !finalPath.startsWith(`${runtimePrefix}/`))
    throw { code: 'invalid_argument', message: 'Runtime URL escaped its artifact.' };
  return result;
}
export class ArtifactApiError extends ApiError {
  constructor(
    code: string,
    message: string,
    status: number,
    readonly runtimeCode?: string,
  ) {
    super(code, message, status);
  }
}
export async function artifactRequest<T>(
  path: string,
  method = 'GET',
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(artifactUrl(path), {
    method,
    credentials: 'include',
    signal,
    ...(body !== undefined
      ? {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }
      : {}),
  });
  let payload: any;
  try {
    payload = await response.json();
  } catch {
    throw new ArtifactApiError(
      'MALFORMED_RESPONSE',
      `Artifact request returned HTTP ${response.status} without a JSON envelope.`,
      response.status,
    );
  }
  if (
    response.status === 401 &&
    (payload?.error?.code ?? payload?.code) === 'AUTH_REQUIRED'
  ) {
    const next = `${window.location.pathname}${window.location.search}`;
    window.location.assign(
      `${MODULE_BASE}/login?next=${encodeURIComponent(next)}`,
    );
  }
  if (!response.ok || payload?.code !== 'OK')
    throw new ArtifactApiError(
      payload?.error?.code ?? payload?.code ?? `HTTP_${response.status}`,
      payload?.error?.user_message ?? payload?.message ?? response.statusText,
      response.status,
      payload?.data?.runtime_code,
    );
  return payload.data as T;
}
export const artifactsApi = {
  list: (
    query: {
      session_id?: string;
      assistant_id?: string;
      cursor?: string;
      q?: string;
    } = {},
  ) =>
    artifactRequest<ArtifactPage>(
      `/api/v1/artifacts?${new URLSearchParams(Object.entries(query).filter(([, value]) => value !== undefined) as [string, string][]).toString()}`,
    ),
  get: (id: string) =>
    artifactRequest<ArtifactRecord>(`/api/v1/artifacts/${encodeURIComponent(id)}`),
  versions: (id: string) =>
    artifactRequest<ArtifactVersionSummary[]>(
      `/api/v1/artifacts/${encodeURIComponent(id)}/versions`,
    ),
  version: (id: string, version: number) =>
    artifactRequest<ArtifactVersion>(
      `/api/v1/artifacts/${encodeURIComponent(id)}/versions/${version}`,
    ),
  restore: (id: string, version: number) =>
    artifactRequest<ArtifactRecord>(
      `/api/v1/artifacts/${encodeURIComponent(id)}/versions/${version}/restore`,
      'POST',
    ),
  delete: (id: string) =>
    artifactRequest<{ deleted: boolean }>(
      `/api/v1/artifacts/${encodeURIComponent(id)}`,
      'DELETE',
    ),
  share: (id: string) =>
    artifactRequest<ArtifactShare>(
      `/api/v1/artifacts/${encodeURIComponent(id)}/share`,
    ),
  createShare: (
    id: string,
    settings: components['schemas']['ArtifactShareRequest'],
  ) =>
    artifactRequest<ArtifactShare>(
      `/api/v1/artifacts/${encodeURIComponent(id)}/share`,
      'POST',
      settings,
    ),
  revokeShare: (id: string) =>
    artifactRequest<ArtifactShare>(
      `/api/v1/artifacts/${encodeURIComponent(id)}/share`,
      'DELETE',
    ),
  runtime: async (target: ArtifactTarget) => {
    const runtime = await artifactRequest<ArtifactRuntime>(runtimePath(target));
    if (
      target.token &&
      (!runtime.artifact.share || !('version' in runtime.artifact.share))
    ) {
      throw new ApiError(
        'MALFORMED_RESPONSE',
        'Artifact share runtime did not include its share version state.',
        200,
      );
    }
    return runtime;
  },
};
export async function readArtifactSSE(
  response: Response,
  receive: (event: string, data: any, id?: string) => void,
) {
  if (!response.ok) {
    const payload = await response.json();
    throw new ArtifactApiError(
      payload?.error?.code ?? payload.code,
      payload?.error?.user_message ?? payload.message,
      response.status,
      payload?.data?.runtime_code,
    );
  }
  if (!response.body) throw new Error('Artifact stream has no response body.');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      buffer = buffer.replace(/\r\n/g, '\n');
      let end: number;
      while ((end = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        let event = 'message';
        let id: string | undefined;
        const data: string[] = [];
        for (const line of block.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          if (line.startsWith('id:')) id = line.slice(3).trim();
          if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
        }
        if (data.length) receive(event, JSON.parse(data.join('\n')), id);
      }
      if (done) return;
    }
  } finally {
    reader.releaseLock();
  }
}

/** Tail one artifact journal, resuming from the last delivered sequence after disconnection. */
export function subscribeArtifactLive(
  target: ArtifactTarget,
  receive: (event: string, payload: any) => void,
  reportError: (error: unknown) => void,
) {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout>;
  let seq = 0;
  let backoff = 1000;
  const live = async () => {
    try {
      const response = await fetch(
        artifactUrl(runtimePath(target, 'live', '', { after_seq: String(seq) })),
        {
          credentials: 'include',
          signal: controller.signal,
          headers: { Accept: 'text/event-stream' },
        },
      );
      await readArtifactSSE(response, (event, payload, id) => {
        if (event === 'error' || event === 'deleted') {
          const code = event === 'deleted' ? 'not_found' : payload.code;
          throw new ArtifactApiError(
            code,
            payload.message ?? 'Artifact was deleted.',
            code === 'not_found' ? 404 : code === 'not_granted' ? 403 : 503,
            code,
          );
        }
        const next = Number(id ?? payload.seq);
        if (Number.isFinite(next)) {
          if (next <= seq && event !== 'presence') return;
          seq = Math.max(seq, next);
        }
        backoff = 1000;
        receive(event, payload);
      });
    } catch (failure) {
      if (controller.signal.aborted) return;
      reportError(failure);
      if (
        failure instanceof ApiError &&
        [400, 401, 403, 404].includes(failure.status)
      )
        return;
    }
    if (!controller.signal.aborted) {
      timer = setTimeout(() => void live(), backoff);
      backoff = Math.min(backoff * 2, 30_000);
    }
  };
  void live();
  return () => {
    controller.abort();
    clearTimeout(timer);
  };
}
