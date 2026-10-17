import { runtimeDataPath, runtimeSegment } from '../../artifact-runtime/paths';
import { ApiError } from '@/api';
import { consoleFetch } from '../consoleTransport';
import {
  CHANNEL,
  CONTRACT,
  STORAGE_LIMIT,
  storageBytes,
} from '../../artifact-runtime/types';
import type { BridgeMessage, RuntimeError } from '../../artifact-runtime/types';
import {
  artifactRequest,
  ArtifactApiError,
  artifactUrl,
  readArtifactSSE,
  runtimePath,
  subscribeArtifactLive,
} from './api';
import type { RuntimeRoute } from './api';
import type { components } from '@/api/schema';
import { computeGrants } from './grants';
import type { ArtifactRuntime, ArtifactTarget } from './types';
export class ArtifactPageError extends Error {}
export type Consent = {
  kind: 'sample' | 'agent' | 'downloads' | 'permissions';
  prompt?: string;
  filename?: string;
  names?: string[];
  selected?: string[];
};
export type ConsentAnswer = boolean | string[];
export function runtimeError(error: unknown): RuntimeError {
  const details =
    typeof error === 'object' && error
      ? (error as { code?: string; message?: string; text?: string })
      : null;
  const code =
    error instanceof ArtifactApiError && error.runtimeCode
      ? error.runtimeCode
      : details?.code ?? '';
  if (
    [
      'not_granted',
      'not_writer',
      'conflict',
      'invalid_argument',
      'not_found',
      'rate_limited',
      'unavailable',
      'too_large',
      'cancelled',
    ].includes(code)
  )
    return {
      code,
      message: details?.message ?? String(error),
      ...(details?.text ? { text: details.text } : {}),
    };
  const mappings: Record<string, string> = {
    ARTIFACT_NOT_FOUND: 'not_found',
    ARTIFACT_VERSION_CONFLICT: 'conflict',
    ARTIFACT_CAPABILITY_NOT_DECLARED: 'not_granted',
    ARTIFACT_NOT_WRITER: 'not_writer',
    ARTIFACT_AGENT_RUN_LIMITED: 'rate_limited',
    ARTIFACT_SAMPLE_RATE_LIMITED: 'rate_limited',
    ARTIFACT_SIGN_IN_REQUIRED: 'not_granted',
  };
  const normalized =
    mappings[code] ??
    (code.includes('TOO_LARGE')
      ? 'too_large'
      : code.includes('INVALID')
        ? 'invalid_argument'
        : code.includes('FORBIDDEN')
          ? 'not_granted'
          : 'unavailable');
  return {
    code:
      error instanceof DOMException && error.name === 'AbortError'
        ? 'cancelled'
        : normalized,
    message: details?.message ?? String(error),
    ...(details?.text ? { text: details.text } : {}),
  };
}
export function artifactStorageKey(
  runtime: ArtifactRuntime,
  target: ArtifactTarget,
) {
  return `astrabox.artifact.${runtime.artifact.artifact_id}.${runtime.viewer.id ?? 'guest'}${target.token ? `.${target.token}` : ''}`;
}
export class BridgeHost {
  private granted: string[];
  private storageKey: string;
  private stopped = false;
  private initialized = false;
  private sampleConsent: Promise<ConsentAnswer> | null = null;
  private controllers = new Map<string, AbortController>();
  private watches = new Map<
    string,
    { stopped: boolean; timer?: ReturnType<typeof setTimeout> }
  >();
  private liveController = new AbortController();
  private stopLive?: () => void;
  private pendingPresence = new Set<string>();
  private bufferedEvents = new Map<string, unknown>();
  private peers = new Map<string, Map<string, { id: string; state: unknown; guest: boolean }>>();
  private onMessage = (event: MessageEvent) => {
    if (
      event.source !== this.frame.contentWindow ||
      event.data?.channel !== CHANNEL
    )
      return;
    const message = event.data as BridgeMessage;
    if (message.type === 'hello') {
      if (message.contract !== CONTRACT) {
        this.error(new Error('Unsupported artifact bridge contract.'));
        return;
      }
      let storage: Record<string, string> = {};
      try {
        const raw = localStorage.getItem(this.storageKey);
        if (raw) {
          const value = JSON.parse(raw);
          if (
            value &&
            typeof value === 'object' &&
            !Array.isArray(value) &&
            Object.values(value).every((item) => typeof item === 'string') &&
            storageBytes(value) <= STORAGE_LIMIT
          )
            storage = value;
          else throw new Error('Invalid artifact storage.');
        }
      } catch (error) {
        this.error(error);
        return;
      }
      this.send({
        type: 'init',
        granted: this.granted,
        viewer: this.runtime.viewer,
        version: this.runtime.version,
        storage,
      });
      this.initialized = true;
      this.bufferedEvents.forEach((payload, subscription) =>
        this.event(subscription, payload),
      );
      this.bufferedEvents.clear();
    } else if (message.type === 'error') {
      const error = new ArtifactPageError(message.message ?? 'Artifact page failed.');
      error.stack = message.stack;
      this.error(error);
    } else if (message.type === 'storage' && this.initialized) {
      const values = message.payload;
      if (
        !values ||
        typeof values !== 'object' ||
        Array.isArray(values) ||
        !Object.values(values).every((value) => typeof value === 'string')
      )
        return;
      if (storageBytes(values as Record<string, string>) > STORAGE_LIMIT) {
        this.error(new Error('Artifact storage exceeds 5 MiB.'));
        return;
      }
      try {
        localStorage.setItem(this.storageKey, JSON.stringify(values));
      } catch (error) {
        this.error(error);
      }
    } else if (
      message.type === 'call' &&
      this.initialized &&
      typeof message.id === 'string' &&
      Array.isArray(message.args)
    )
      void this.call(message);
    else if (message.type === 'unsubscribe' && message.subscription) {
      const watch = this.watches.get(message.subscription);
      if (watch) {
        watch.stopped = true;
        clearTimeout(watch.timer);
        this.watches.delete(message.subscription);
      }
    }
  };
  constructor(
    private frame: HTMLIFrameElement,
    private runtime: ArtifactRuntime,
    private target: ArtifactTarget,
    private consent: (request: Consent) => Promise<ConsentAnswer>,
    private error: (error: unknown) => void,
    private versionChanged: () => void,
    private historical = false,
    private pinned = false,
  ) {
    this.storageKey = artifactStorageKey(runtime, target);
    this.granted = computeGrants(
      runtime.manifest,
      runtime.viewer,
      historical,
      pinned,
      Boolean(runtime.artifact.origin.assistant_id),
    );
    window.addEventListener('message', this.onMessage);
    if (!historical)
      this.stopLive = subscribeArtifactLive(
        target,
        (type, payload) => {
          if (type === 'version') {
            if (!this.pinned && Number(payload.version) > this.runtime.version)
              this.versionChanged();
          } else if (type === 'data') this.event('data', payload);
          else if (type === 'room')
            this.event(
              `room:${payload.room ?? 'default'}:${payload.topic}`,
              payload.data,
            );
          else if (type === 'presence') {
            const room = payload.room;
            const peers = this.peers.get(room) ?? new Map();
            if (payload.state === null) peers.delete(payload.viewer_id);
            else peers.set(payload.viewer_id, {
              id: payload.viewer_id, state: payload.state, guest: payload.guest,
            });
            this.peers.set(room, peers);
            this.event(`room:${room}:presence`, [...peers.values()]);
          }
        },
        error,
      );
  }
  private send(message: Omit<BridgeMessage, 'channel'>) {
    if (!this.stopped)
      this.frame.contentWindow?.postMessage(
        { channel: CHANNEL, ...message },
        '*',
      );
  }
  private event(subscription: string, payload: unknown) {
    if (!this.initialized) {
      this.bufferedEvents.set(subscription, payload);
      return;
    }
    this.send({ type: 'event', subscription, payload });
  }
  private request<T>(
    route: RuntimeRoute,
    method = 'GET',
    body?: unknown,
    signal?: AbortSignal,
    parameter?: string,
    query?: Record<string, string>,
  ) {
    return artifactRequest<T>(
      runtimePath(this.target, route, parameter, query), method, body, signal,
    );
  }
  private async call(message: BridgeMessage) {
    const id = message.id!;
    const capability = message.capability!;
    const method = message.method!;
    const args = message.args!;
    if (method === 'cancel') {
      this.controllers.get(String(args[0]))?.abort();
      return;
    }
    if (this.controllers.has(id)) return;
    const controller = new AbortController();
    this.controllers.set(id, controller);
    try {
      if (!this.granted.includes(capability))
        throw {
          code: 'not_granted',
          message: 'This capability is not granted.',
        };
      const value = await this.dispatch(
        capability,
        method,
        args,
        controller.signal,
      );
      this.send({ type: 'result', id, ok: true, value });
    } catch (error) {
      this.send({ type: 'result', id, ok: false, error: runtimeError(error) });
    } finally {
      this.controllers.delete(id);
    }
  }
  private async dispatch(
    capability: string,
    method: string,
    args: any[],
    signal: AbortSignal,
  ): Promise<unknown> {
    const denied = () => {
      throw { code: 'cancelled', message: 'The viewer declined this request.' };
    };
    if (capability === 'artifact' && method === 'publish') {
      if (this.runtime.viewer.level !== 'owner')
        throw { code: 'not_writer', message: 'Only the owner can publish.' };
      return this.request(
        'publish',
        'POST',
        { content: args[0], base_version: args[1] },
        signal,
      );
    }
    if (capability === 'db') {
      if (
        ['set', 'update', 'delete', 'add'].includes(method) &&
        this.runtime.viewer.guest
      )
        throw {
          code: 'not_writer',
          message: 'This viewer cannot write artifact data.',
        };
      const path = ['get', 'set', 'update', 'delete'].includes(method)
        ? runtimeDataPath(args[0], true) : '';
      if (method === 'query') runtimeDataPath(args[0]?.collection, false);
      if (method === 'add') runtimeDataPath(args[0], false);
      if (method === 'get')
        return this.request('data', 'GET', undefined, signal, path);
      if (method === 'set' || method === 'update')
        return this.request(
          'data',
          method === 'set' ? 'PUT' : 'PATCH',
          { data: args[1] },
          signal,
          path,
        );
      if (method === 'delete')
        return this.request('data', 'DELETE', undefined, signal, path);
      if (method === 'query')
        return this.request('query', 'POST', args[0] as components['schemas']['ArtifactQueryRequest'], signal);
      if (method === 'add')
        return this.request(
          'add',
          'POST',
          { collection: args[0], data: args[1] },
          signal,
        );
    }
    if (capability === 'sample' && method === 'sample') {
      this.sampleConsent ??= this.consent({ kind: 'sample' });
      if (!(await this.sampleConsent)) denied();
      if (signal.aborted)
        throw new DOMException('Request cancelled.', 'AbortError');
      const response = await consoleFetch(artifactUrl(runtimePath(this.target, 'sample')), {
        method: 'POST',
        credentials: 'include',
        signal,
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
        },
        body: JSON.stringify(args[0]),
      });
      let result: unknown;
      await readArtifactSSE(response, (event, payload) => {
        if (event === 'delta')
          this.event(args[1], { text: payload.text });
        else if (event === 'done') result = payload;
        else if (event === 'error') throw payload;
      });
      if (result === undefined)
        throw new Error('Model stream ended without a final result.');
      return result;
    }
    if (capability === 'agent') {
      if (method === 'run') {
        if (typeof args[0]?.prompt !== 'string' || !args[0].prompt.trim())
          throw {
            code: 'invalid_argument',
            message: 'An Agent run needs a prompt.',
          };
        if (!(await this.consent({ kind: 'agent', prompt: args[0].prompt })))
          denied();
        return this.request<components['schemas']['ArtifactAgentRun']>('agentRuns', 'POST', args[0], signal);
      }
      if (method === 'status')
        return this.request<components['schemas']['ArtifactAgentStatus']>(
          'agentStatus',
          'GET',
          undefined,
          signal,
          args[0],
        );
      if (method === 'watch') {
        runtimeSegment(args[0]);
        const subscription = args[1];
        if (this.watches.has(subscription)) return null;
        const watch = {
          stopped: false,
          timer: undefined as ReturnType<typeof setTimeout> | undefined,
        };
        this.watches.set(subscription, watch);
        const poll = async () => {
          if (watch.stopped || this.stopped) return;
          try {
            const status = await this.request<components['schemas']['ArtifactAgentStatus']>(
              'agentStatus',
              'GET',
              undefined,
              this.liveController.signal,
              args[0],
            );
            if (watch.stopped) return;
            this.event(subscription, status);
            if (['completed', 'failed'].includes(status.state)) {
              this.watches.delete(subscription);
              return;
            }
            watch.timer = setTimeout(() => void poll(), 3000);
          } catch (error) {
            if (!this.stopped && !watch.stopped) {
              this.error(error);
              if (error instanceof ApiError && error.status < 500)
                this.watches.delete(subscription);
              else watch.timer = setTimeout(() => void poll(), 3000);
            }
          }
        };
        void poll();
        return null;
      }
    }
    if (capability === 'user' && method === 'profiles')
      return this.request<{ profiles: { id: string; display_name: string }[] }>(
        'viewers',
        'GET',
        undefined,
        signal,
        '',
        { ids: args[0].join(',') },
      ).then((result) => result.profiles);
    if (capability === 'room') {
      const room = runtimeSegment(args[0]);
      if (method === 'emit') {
        const declaration = this.runtime.manifest.room as {
          topics?: Record<string, string>;
        };
        const required = declaration.topics?.[args[1]] ?? 'owner';
        const levels = ['none', 'view', 'interact', 'owner'];
        if (
          levels.indexOf(this.runtime.viewer.level) < levels.indexOf(required)
        )
          throw {
            code: 'not_writer',
            message: 'This viewer cannot send to that room topic.',
          };
        return this.request(
          'emit',
          'POST',
          { topic: args[1], data: args[2] },
          signal,
          room,
        );
      }
      if (method === 'presence') {
        if (args[1] === null) this.pendingPresence.delete(room);
        else this.pendingPresence.add(room);
        return this.request(
          'presence',
          'POST',
          { state: args[1] },
          signal,
          room,
        );
      }
    }
    if (capability === 'downloads' && method === 'save') {
      const { filename, data } = args[0];
      if (
        typeof filename !== 'string' ||
        !(
          typeof data === 'string' ||
          data instanceof Blob ||
          data instanceof ArrayBuffer
        )
      )
        throw {
          code: 'invalid_argument',
          message:
            'A download needs a filename and string, Blob or ArrayBuffer data.',
        };
      if (!(await this.consent({ kind: 'downloads', filename }))) denied();
      saveArtifactFile(filename, data);
      return null;
    }
    if (capability === 'permissions') {
      if (method === 'state')
        return args[0]
          ? { name: args[0], granted: this.granted.includes(args[0]) }
          : { granted: [...this.granted] };
      if (method === 'request' || method === 'manage') {
        const eligible = computeGrants(
          this.runtime.manifest,
          this.runtime.viewer,
          this.historical,
          this.pinned,
          Boolean(this.runtime.artifact.origin.assistant_id),
        );
        const names: string[] = args[0] ?? eligible;
        if (names.some((name) => !eligible.includes(name)))
          throw {
            code: 'not_granted',
            message: 'A requested capability is unavailable to this viewer.',
          };
        const selected =
          method === 'request'
            ? [
                ...new Set([
                  ...this.granted,
                  ...names.filter((name) => eligible.includes(name)),
                ]),
              ]
            : this.granted;
        const answer = await this.consent({
          kind: 'permissions',
          names: eligible.filter((name) => name !== 'permissions'),
          selected,
        });
        if (!answer) denied();
        const allowed = Array.isArray(answer) ? answer : selected;
        this.granted = [
          'permissions',
          ...allowed.filter(
            (name) => eligible.includes(name) && name !== 'permissions',
          ),
        ];
        this.send({ type: 'grants', granted: this.granted });
        return { granted: [...this.granted] };
      }
    }
    throw {
      code: 'invalid_argument',
      message: `Unknown capability method ${capability}.${method}.`,
    };
  }
  stop() {
    this.stopped = true;
    window.removeEventListener('message', this.onMessage);
    this.controllers.forEach((controller) => controller.abort());
    this.liveController.abort();
    this.stopLive?.();
    this.watches.forEach((watch) => {
      watch.stopped = true;
      clearTimeout(watch.timer);
    });
    for (const room of this.pendingPresence) {
      void consoleFetch(artifactUrl(runtimePath(this.target, 'presence', room)), {
        method: 'POST',
        credentials: 'include',
        keepalive: true,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ state: null }),
      }).catch(() => {});
    }
  }
}
export function saveArtifactFile(
  filename: string,
  data: string | Blob | ArrayBuffer,
) {
  const blob = data instanceof Blob ? data : new Blob([data]);
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
