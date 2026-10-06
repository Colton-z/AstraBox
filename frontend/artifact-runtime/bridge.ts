import { runtimeDataPath, runtimeSegment } from './paths';
import {
  CHANNEL,
  CONTRACT,
  STORAGE_LIMIT,
  documentSnapshot,
  querySnapshot,
  storageBytes,
} from './types';
import type { BridgeMessage, Row, RuntimeError, Viewer } from './types';

type Rpc = (
  capability: string,
  method: string,
  args?: unknown[],
  signal?: AbortSignal,
) => Promise<any>;
type Listener = (payload: any) => void;
type SampleTool = {
  name: string;
  description?: string;
  input_schema?: unknown;
  execute: (input: any) => unknown | Promise<unknown>;
};
type SampleOptions = {
  onText?: (text: string) => void;
  signal?: AbortSignal;
  tools?: SampleTool[];
  maxTokens?: number;
  cache?: boolean;
  modelTier?: string;
};
export type PageWindow = Window &
  typeof globalThis & {
    astrabox?: {
      use: (name: string) => Promise<Readonly<Record<string, any>> | null>;
    };
    __artifactReady?: Promise<void>;
    __artifactReportError?: (error: unknown) => void;
  };

export function installBridge(page: PageWindow) {
  const send = (message: Omit<BridgeMessage, 'channel'>) =>
    page.parent.postMessage({ channel: CHANNEL, ...message }, '*');
  const pending = new Map<
    string,
    {
      resolve: (value: any) => void;
      reject: (error: RuntimeError) => void;
      cleanup: () => void;
    }
  >();
  const listeners = new Map<string, Set<Listener>>();
  const uses = new Map<string, Promise<Readonly<Record<string, any>> | null>>();
  let serial = 0;
  let granted: string[] = [];
  let viewer: Viewer = { level: 'none', id: null, guest: true };
  let version = 0;
  let initialized = false;
  let resolveReady!: () => void;
  const ready = new Promise<void>((resolve) => {
    resolveReady = resolve;
  });
  page.__artifactReady = ready;
  const timeout = page.setTimeout(resolveReady, 10_000);
  const failure = (code: string, message: string): RuntimeError => ({
    code,
    message,
  });
  const rpc: Rpc = async (capability, method, args = [], signal) => {
    await ready;
    if (!granted.includes(capability))
      throw failure('not_granted', `Capability ${capability} is not granted.`);
    if (signal?.aborted) throw failure('cancelled', 'Request cancelled.');
    const id = String(++serial);
    return new Promise((resolve, reject) => {
      const abort = () => {
        send({
          type: 'call',
          id: `${id}-cancel`,
          capability,
          method: 'cancel',
          args: [id],
        });
        pending.delete(id);
        cleanup();
        reject(failure('cancelled', 'Request cancelled.'));
      };
      const timer = page.setTimeout(() => {
        pending.delete(id);
        cleanup();
        reject(
          failure('unavailable', 'The viewer did not answer the request.'),
        );
      }, 300_000);
      const cleanup = () => {
        page.clearTimeout(timer);
        signal?.removeEventListener('abort', abort);
      };
      pending.set(id, { resolve, reject, cleanup });
      signal?.addEventListener('abort', abort, { once: true });
      send({ type: 'call', id, capability, method, args });
    });
  };
  const listen = (subscription: string, fn: Listener) => {
    const set = listeners.get(subscription) ?? new Set<Listener>();
    set.add(fn);
    listeners.set(subscription, set);
    return () => {
      set.delete(fn);
      if (!set.size) {
        listeners.delete(subscription);
        send({ type: 'unsubscribe', subscription });
      }
    };
  };
  const storage = (persist: boolean) => {
    let values: Record<string, string> = Object.create(null);
    const write = (next: Record<string, string>) => {
      if (storageBytes(next) > STORAGE_LIMIT)
        throw new DOMException(
          'Artifact storage exceeds 5 MiB.',
          'QuotaExceededError',
        );
      values = next;
      if (persist) send({ type: 'storage', payload: values });
    };
    const api = {
      get length() {
        return Object.keys(values).length;
      },
      key: (index: number) => Object.keys(values)[index] ?? null,
      getItem: (key: string) => values[String(key)] ?? null,
      setItem: (key: string, value: string) =>
        write(
          Object.assign(Object.create(null), values, {
            [String(key)]: String(value),
          }),
        ),
      removeItem: (key: string) => {
        const next = Object.assign(Object.create(null), values);
        delete next[String(key)];
        write(next);
      },
      clear: () => write(Object.create(null)),
    };
    const proxy = new Proxy(api, {
      get: (target, key) =>
        typeof key === 'string' && !(key in target)
          ? (api.getItem(key) ?? undefined)
          : Reflect.get(target, key),
      set: (_target, key, value) => {
        if (typeof key === 'string') api.setItem(key, value);
        return true;
      },
      deleteProperty: (_target, key) => {
        if (typeof key === 'string') api.removeItem(key);
        return true;
      },
      ownKeys: () => Object.keys(values),
      getOwnPropertyDescriptor: (_target, key) =>
        typeof key === 'string' && key in values
          ? {
              configurable: true,
              enumerable: true,
              value: values[key],
              writable: true,
            }
          : undefined,
    });
    return {
      api: proxy,
      seed: (seed: Record<string, string>) => {
        values = Object.assign(Object.create(null), seed);
      },
    };
  };
  const local = storage(true);
  Object.defineProperty(page, 'localStorage', {
    configurable: false,
    value: local.api,
  });
  Object.defineProperty(page, 'sessionStorage', {
    configurable: false,
    value: storage(false).api,
  });
  page.__artifactReportError = (error) => {
    const details =
      error && typeof error === 'object'
        ? (error as { message?: unknown; stack?: unknown })
        : null;
    const message = details?.message ? String(details.message) : String(error);
    send({
      type: 'error',
      message,
      stack: typeof details?.stack === 'string' ? details.stack : undefined,
    });
  };
  page.onerror = (message, _source, _line, _column, error) =>
    page.__artifactReportError!(error ?? message);
  page.addEventListener('error', (event) =>
    page.__artifactReportError!(event.error ?? event.message),
  );
  page.addEventListener('unhandledrejection', (event) =>
    page.__artifactReportError!(event.reason),
  );
  page.addEventListener('message', (event) => {
    if (event.source !== page.parent || event.data?.channel !== CHANNEL) return;
    const message = event.data as BridgeMessage;
    if (message.type === 'init' && !initialized) {
      initialized = true;
      granted = message.granted ?? [];
      viewer = message.viewer ?? viewer;
      version = message.version ?? 0;
      local.seed(message.storage ?? {});
      page.clearTimeout(timeout);
      resolveReady();
    } else if (message.type === 'grants') {
      granted = message.granted ?? [];
    } else if (message.type === 'result' && message.id) {
      const call = pending.get(message.id);
      if (!call) return;
      pending.delete(message.id);
      call.cleanup();
      if (message.ok) call.resolve(message.value);
      else
        call.reject(message.error ?? failure('unavailable', 'Request failed.'));
    } else if (message.type === 'event' && message.subscription) {
      listeners.get(message.subscription)?.forEach((fn) => fn(message.payload));
    }
  });
  const path = (value: string, document: boolean) => {
    if (value.includes('{self}') && (viewer.guest || !viewer.id))
      throw failure('not_granted', 'Guests have no private document path.');
    const expanded = value.replaceAll('{self}', viewer.id ?? '');
    return runtimeDataPath(expanded, document);
  };
  const subscribeData = (
    value: string,
    read: () => Promise<any>,
    next: Listener,
    error?: Listener,
  ) => {
    let active = true;
    let running = false;
    let dirty = false;
    let timer: number | undefined;
    const refresh = async () => {
      if (!active) return;
      if (running) {
        dirty = true;
        return;
      }
      running = true;
      try {
        const snapshot = await read();
        if (active) next(snapshot);
      } catch (e) {
        if (active) {
          if (error) error(e);
          else page.__artifactReportError!(e);
        }
      } finally {
        running = false;
        if (dirty) {
          dirty = false;
          void refresh();
        }
      }
    };
    const off = listen('data', (payload) => {
      const changed = payload?.path;
      if (changed && changed !== value && !changed.startsWith(`${value}/`))
        return;
      page.clearTimeout(timer);
      timer = page.setTimeout(() => void refresh(), 50);
    });
    void refresh();
    return () => {
      active = false;
      off();
      page.clearTimeout(timer);
    };
  };
  const doc = (value: string) => {
    const get = async () => {
      await ready;
      const result = await rpc('db', 'get', [path(value, true)]);
      return documentSnapshot(
        value,
        result?.exists === false ? undefined : result?.data,
      );
    };
    return Object.freeze({
      get,
      id: value.split('/').at(-1),
      path: value.replaceAll('{self}', viewer.id ?? ''),
      set: async (data: unknown) => {
        await ready;
        await rpc('db', 'set', [path(value, true), data]);
      },
      update: async (data: unknown) => {
        await ready;
        await rpc('db', 'update', [path(value, true), data]);
      },
      delete: async () => {
        await ready;
        await rpc('db', 'delete', [path(value, true)]);
      },
      onSnapshot: (next: Listener, error?: Listener) =>
        subscribeData(
          value.replaceAll('{self}', viewer.id ?? ''),
          get,
          next,
          error,
        ),
    });
  };
  type Query = {
    where: { field: string; op: string; value: unknown }[];
    order_by: { field: string; direction: string }[];
    limit?: number;
  };
  const collection = (
    value: string,
    query: Query = { where: [], order_by: [] },
  ): any => {
    const read = async () => {
      await ready;
      const result = await rpc('db', 'query', [
        { collection: path(value, false), ...query },
      ]);
      return result.items as Row[];
    };
    return Object.freeze({
      doc: (id: string) => doc(`${value}/${id}`),
      add: async (data: unknown) => {
        await ready;
        const result = await rpc('db', 'add', [path(value, false), data]);
        return doc(result.path);
      },
      where: (field: string, op: string, match: unknown) =>
        collection(value, {
          ...query,
          where: [...query.where, { field, op, value: match }],
        }),
      orderBy: (field: string, direction = 'asc') =>
        collection(value, {
          ...query,
          order_by: [...query.order_by, { field, direction }],
        }),
      limit: (limit: number) => collection(value, { ...query, limit }),
      get: async () => querySnapshot(await read()),
      onSnapshot: (next: Listener, error?: Listener) => {
        let previous: Row[] = [];
        return subscribeData(
          value,
          read,
          (rows) => {
            const snapshot = querySnapshot(rows, previous);
            previous = structuredClone(rows);
            next(snapshot);
          },
          error,
        );
      },
    });
  };
  const cache = new Map<string, { at: number; result: unknown }>();
  const sample = async (
    input: string | unknown[],
    options: SampleOptions = {},
  ) => {
    const tools = options.tools ?? [];
    const definitions = tools.map(({ name, description, input_schema }) => ({
      name, description: description ?? '', input_schema: input_schema ?? { type: 'object', properties: {} },
    }));
    const key = JSON.stringify({
      input,
      tools: definitions,
      maxTokens: options.maxTokens,
    });
    const cached = cache.get(key);
    if (options.cache && cached && Date.now() - cached.at < 300_000) {
      options.onText?.((cached.result as { text: string }).text);
      return structuredClone(cached.result);
    }
    let turns: unknown[] =
      typeof input === 'string'
        ? [{ role: 'user', content: input }]
        : [...input];
    for (let round = 0; round < 8; round++) {
      const subscription = `sample-${++serial}`;
      const off = listen(subscription, (payload) =>
        options.onText?.(payload.text),
      );
      let result: any;
      try {
        result = await rpc(
          'sample',
          'sample',
          [
            { input: turns, tools: definitions, max_tokens: options.maxTokens, modelTier: options.modelTier },
            subscription,
          ],
          options.signal,
        );
      } finally {
        off();
      }
      if (!result.tool_calls?.length) {
        if (options.cache) cache.set(key, { at: Date.now(), result });
        return result;
      }
      turns.push({
        role: 'assistant',
        content: result.text,
        tool_calls: result.tool_calls,
      });
      for (const call of result.tool_calls) {
        if (options.signal?.aborted)
          throw failure('cancelled', 'Request cancelled.');
        const name = call.function.name;
        const tool = tools.find((tool) => tool.name === name);
        if (!tool) throw failure('invalid_argument', `Unknown tool ${name}.`);
        const raw = call.function.arguments;
        const output = await tool.execute(
          typeof raw === 'string' ? JSON.parse(raw) : raw,
        );
        turns.push({
          role: 'tool',
          tool_call_id: call.id,
          content: typeof output === 'string' ? output : JSON.stringify(output),
        });
      }
    }
    throw failure('too_large', 'Sample tool loop exceeds eight rounds.');
  };
  sample.json = async (
    input: string | unknown[],
    options: SampleOptions = {},
  ) => {
    const result = await sample(input, options);
    try {
      return JSON.parse(result.text.replace(/^```(?:json)?\s*|\s*```$/g, ''));
    } catch {
      throw {
        code: 'invalid_argument',
        message: 'The model did not return JSON.',
        text: result.text,
      };
    }
  };
  const room = (name = 'default') => {
    runtimeSegment(name);
    let presence: unknown = null;
    let heartbeat: number | undefined;
    const offs = new Set<() => void>();
    const subscribe = (topic: string, fn: Listener) => {
      const off = listen(`room:${name}:${topic}`, fn);
      offs.add(off);
      return () => {
        off();
        offs.delete(off);
      };
    };
    const setPresence = async (state: unknown) => {
      presence = state;
      page.clearInterval(heartbeat);
      if (state !== null)
        heartbeat = page.setInterval(
          () =>
            void rpc('room', 'presence', [name, presence]).catch(
              page.__artifactReportError,
            ),
          15_000,
        );
      return rpc('room', 'presence', [name, state]);
    };
    return Object.freeze({
      emit: (topic: string, data: unknown) =>
        rpc('room', 'emit', [name, topic, data]),
      on: (topic: string, fn: Listener) => subscribe(topic, fn),
      presence: setPresence,
      onPeers: (fn: Listener) => subscribe('presence', fn),
      leave: () => {
        offs.forEach((off) => off());
        offs.clear();
        return setPresence(null);
      },
    });
  };
  const defaultRoom = room();
  const namespaces: Record<string, Record<string, any>> = {
    artifact: {
      publish: (html: string) => rpc('artifact', 'publish', [html, version]),
      version: () => version,
    },
    db: { doc, collection },
    sample: { sample: Object.freeze(sample), json: sample.json },
    user: {
      isOwner: () => viewer.level === 'owner',
      canEdit: () =>
        !viewer.guest && ['owner', 'interact'].includes(viewer.level),
      can: (name: string) => granted.includes(name),
      id: () => viewer.id,
      me: () => Object.freeze({ ...viewer }),
      profiles: (ids: string[]) => rpc('user', 'profiles', [ids]),
    },
    room: { ...defaultRoom, join: (name: string) => room(name) },
    downloads: {
      save: (value: { filename: string; data: string | Blob | ArrayBuffer }) =>
        rpc('downloads', 'save', [value]),
    },
    agent: {
      run: async (input: { prompt: string }) => {
        const result = await rpc('agent', 'run', [input]);
        return {
          runId: result.run_id,
          sessionId: result.session_id,
          url: result.url,
        };
      },
      status: async (runId: string) => {
        runtimeSegment(runId);
        const result = await rpc('agent', 'status', [runId]);
        return {
          state: result.state,
          sessionId: result.session_id,
          ...(result.error ? { error: result.error } : {}),
        };
      },
      watch: (runId: string, fn: Listener) => {
        runtimeSegment(runId);
        const subscription = `agent:${runId}`;
        const off = listen(subscription, (result) =>
          fn({
            state: result.state,
            sessionId: result.session_id,
            ...(result.error ? { error: result.error } : {}),
          }),
        );
        let active = true;
        void rpc('agent', 'watch', [runId, subscription])
          .then(() => {
            if (!active) send({ type: 'unsubscribe', subscription });
          })
          .catch(page.__artifactReportError);
        return () => {
          active = false;
          off();
        };
      },
    },
    permissions: {
      state: (name?: string) => rpc('permissions', 'state', [name]),
      request: (names?: string[]) => rpc('permissions', 'request', [names]),
      manage: () => rpc('permissions', 'manage'),
    },
  };
  const use = (name: string) => {
    if (!uses.has(name))
      uses.set(
        name,
        ready.then(() =>
          granted.includes(name) && namespaces[name]
            ? Object.freeze(namespaces[name])
            : null,
        ),
      );
    return uses.get(name)!;
  };
  page.astrabox = Object.freeze({ use });
  send({ type: 'hello', contract: CONTRACT });
  void ready
    .then(async () => {
      // Author scripts wait for storage seeding before they read Web Storage.
      for (const original of page.document.querySelectorAll<HTMLScriptElement>(
        'script[type="application/astrabox-script"]',
      )) {
        const script = page.document.createElement('script');
        for (const attribute of original.attributes)
          if (!['type', 'data-artifact-type'].includes(attribute.name))
            script.setAttribute(attribute.name, attribute.value);
        const type = original.getAttribute('data-artifact-type');
        if (type) script.type = type;
        script.textContent = original.textContent;
        const loaded = new Promise<void>((resolve, reject) => {
          script.onload = () => resolve();
          script.onerror = () =>
            reject(new Error(`Could not load ${script.src}`));
        });
        original.replaceWith(script);
        if (script.src) await loaded;
      }
    })
    .catch(page.__artifactReportError);
  return { use, ready };
}
