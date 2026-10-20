import i18n from '@/i18n';
import { readUserAction } from './session/userAction';
import { consoleFetch } from './consoleTransport';

import type { components, paths } from './api/schema';
import type { UIMessage } from 'ai';
import createClient from 'openapi-fetch';

import {
  fetchWithNetworkRecovery,
  isAbortError,
  NetworkRequestError,
  type NetworkRecoveryOptions,
} from './networkRecovery';

import type {
  UserAction,
  SessionUserActionState,
  AdminErrorsPage,
  AdminLogsPage,
  AdminProcessHealth,
  IssuedMcpClientToken,
  McpClientToken,
  AdminSandboxDiagnostics,
  AdminSandboxSecurity,
  AdminSandboxDiagnosticScope,
  AdminSandboxPage,
  AdminSandboxIdleAction,
  AdminSandboxQuota,
  AdminSessionDetail,
  AdminSessionFilters,
  AdminSessionPage,
  AdminSessionSummary,
  AdminSessionTrace,
  AdminIntegrations,
  AdminNavigationSummary,
  AdminSystemOverview,
  AdminAgentCategory,
  AgentAccess,
  AgentAccessPolicy,
  AgentCategory,
  AgentConfig,
  AgentFavorite,
  AgentListPage,
  AgentTag,
  DeletedAgentCategory,
  ListPageQuery,
  AgentPreparedRuntimeStatus,
  AgentExtensionCatalog,
  AgentDeployment,
  Connector,
  ChannelProviderDescriptor,
  ChannelAccountLink,
  ChannelExecutionMode,
  DeploymentRun,
  PersonalSchedule,
  AgentDraft,
  ApiResponse,
  EnvironmentConfig,
  ExtensionProviderView,
  FormSchema,
  InteractionResponse,
  PendingInteraction,
  PermissionMode,
  ProcessSummaryState,
  ConversationEndResult,
  CredentialBinding,
  SandboxTerminateResult,
  SessionFilesDeleteRequest,
  SessionFilesListRequest,
  SessionFilesListResponse,
  SessionFilesMkdirRequest,
  SessionFilesMoveRequest,
  SessionFilesMutationResult,
  SessionFilesUploadRequest,
  SessionListPage,
  SessionRecord,
  UserInfo,
  VaultCatalog,
  VaultBindingHolder,
  VaultSummary,
  WebshellResult,
} from './types';

type NormalizedApiError = {
  code: string;
  message: string;
};

/**
 * An error the server produced: a response arrived, and it was a refusal.
 *
 * The request path throws this only once it has an HTTP status in hand; a
 * request that never got an answer (DNS, TLS, a dropped connection, a proxy
 * eating it) still throws a plain `Error`. A caller that must not conflate
 * "the server said no" with "nobody said anything" — a 501 "this report does
 * not exist" against a failed fetch, say — can then tell them apart instead of
 * showing one sentence for both.
 *
 * `message` reads `CODE: text`, so a caller that only reads
 * `(e as Error).message` renders it the same way as any other error.
 */
export class ApiError extends Error {
  /** HTTP status of the response that carried the refusal. */
  readonly status: number;
  /** The envelope's error code (or `HTTP_<status>` when it carried none). */
  readonly code: string;
  /**
   * The envelope's `error.retryable`: whether the same request, sent again,
   * can succeed. `null` when the response carried no envelope to say.
   */
  readonly retryable: boolean | null;
  readonly userAction: UserAction | null;

  constructor(code: string, message: string, status: number, retryable: boolean | null = null, userAction: UserAction | null = null) {
    super(`${code}: ${message}`);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    this.userAction = readUserAction(userAction);
  }
}

/**
 * Whether a failure means "this is not yours to see" rather than "the request
 * broke".
 *
 * Ownership-scoped routes answer for a subject the caller does not manage with
 * a non-disclosing refusal — 404 rather than 403, so the response cannot reveal
 * that the subject exists. A caller that fans out over a list it does not own
 * needs to absorb those and keep the rest; anything else is a real failure and
 * must still surface.
 *
 * Decide from the status, never the message text: copy changes independently
 * of the access-control contract.
 */
export function isAccessRefusal(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 403);
}

function compactApiErrorEvidence(value: unknown): string {
  return String(value ?? '').replace(/\s+/g, ' ').trim().slice(0, 300);
}

function appendApiErrorEvidence(message: string, data: unknown): string {
  if (!data || typeof data !== 'object') {
    return message;
  }
  const obj = data as Record<string, unknown>;
  const detail = compactApiErrorEvidence(obj.detail);
  const traceId = compactApiErrorEvidence(obj.trace_id);
  const suffix = [
    detail && !message.includes(detail) ? `detail=${detail}` : '',
    traceId && !message.includes(traceId) ? `trace_id=${traceId}` : '',
  ].filter(Boolean).join('; ');
  return suffix ? `${message} (${suffix})` : message;
}

function appendStructuredApiErrorEvidence(message: string, payload: ApiResponse<unknown> | null): string {
  const error = payload?.error;
  if (!error || typeof error !== 'object') {
    return appendApiErrorEvidence(message, payload?.data);
  }
  const evidence = error.evidence && typeof error.evidence === 'object' ? error.evidence : {};
  const detail = compactApiErrorEvidence(error.debug_message || (evidence as Record<string, unknown>).detail);
  const traceId = compactApiErrorEvidence((evidence as Record<string, unknown>).trace_id);
  const category = compactApiErrorEvidence(error.category);
  const data = payload?.data;
  const dataEvidence = data && typeof data === 'object' && Object.keys(data).length > 0
    ? compactApiErrorEvidence(JSON.stringify(data))
    : '';
  const suffix = [
    category && !message.includes(category) ? `category=${category}` : '',
    detail && !message.includes(detail) ? `detail=${detail}` : '',
    traceId && !message.includes(traceId) ? `trace_id=${traceId}` : '',
    dataEvidence && !message.includes(dataEvidence) ? `data=${dataEvidence}` : '',
  ].filter(Boolean).join('; ');
  if (suffix) {
    return `${message} (${suffix})`;
  }
  return appendApiErrorEvidence(message, payload?.data);
}

function requestPathForEvidence(url: string): string {
  try {
    return new URL(url, window.location.origin).pathname;
  } catch {
    return url;
  }
}

function normalizeLegacyApiError(
  message: string,
  response: Response,
  url: string,
): NormalizedApiError | null {
  const raw = String(message || '').trim();
  if (!raw) {
    return null;
  }

  const path = requestPathForEvidence(url);
  const evidence = i18n.t('misc:api_error.evidence', { status: response.status, url: path });
  if (/BaseErrorCode\.UNKNOWN_ERROR/i.test(raw)) {
    return {
      code: 'PLATFORM_SERVER_ERROR',
      message: i18n.t('misc:api_error.platform_unknown', { evidence }),
    };
  }

  const baseErrorMatch = raw.match(/^BaseErrorCode\.([A-Z0-9_]+):\s*(.+)$/);
  if (baseErrorMatch) {
    return {
      code: `PLATFORM_${baseErrorMatch[1]}`,
      message: i18n.t('misc:api_error.platform_error', { message: baseErrorMatch[2], evidence }),
    };
  }

  const codeMatch = raw.match(/^([A-Z][A-Z0-9_]+):\s*(.+)$/);
  if (codeMatch) {
    return {
      code: codeMatch[1],
      message: codeMatch[2],
    };
  }

  return {
    code: `HTTP_${response.status}`,
    message: raw,
  };
}


/**
 * The path this console is published under: `''` at the root of the host, or a
 * path such as `/astrabox` with no trailing slash. It is the router's basename
 * and the base of every API and console URL the page composes.
 *
 * The AstraBox server writes it into the `astrabox-base-path` meta element on
 * every request (`ASTRABOX_PUBLIC_PATH_PREFIX`, astrabox/deploy/public_path.py),
 * and the Vite dev server writes `''`. It is not inferred from
 * `location.pathname`: the first segment there is as likely to be a console
 * route (`/agents`, `/manage`, `/share`) as a prefix, and taking a route for a
 * prefix would nest every route and API call under it. A page without the
 * value was not served by AstraBox, so loading stops with that reason rather
 * than guessing. Outside a document, as in the unit suite's node environment,
 * there is no page and therefore no prefix.
 */
function servedBasePath(): string {
  if (typeof document === 'undefined') return '';
  const value = document
    .querySelector<HTMLMetaElement>('meta[name="astrabox-base-path"]')
    ?.getAttribute('content');
  if (value === null || value === undefined || !/^(\/[A-Za-z0-9._~-]+)*$/.test(value)) {
    throw new Error(
      `This page carries no base path from the AstraBox server (astrabox-base-path is ${JSON.stringify(value ?? null)}). Open the console through the AstraBox server.`,
    );
  }
  return value;
}

export const MODULE_BASE = servedBasePath();
const API_BASE_ENV = String(import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '');

const API_BASE = API_BASE_ENV || MODULE_BASE;

function buildApiUrl(path: string): string {
  return API_BASE ? `${API_BASE}${path}` : path;
}

function compactErrorPreview(input: string, maxLength = 180): string {
  const normalized = String(input || '').replace(/\s+/g, ' ').trim();
  if (!normalized) {
    return '';
  }
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, maxLength - 3)}...`;
}

function buildUnexpectedResponseMessage(response: Response, body: unknown): string {
  const details: string[] = [];
  const contentType = String(response.headers.get('content-type') || '').trim();
  if (contentType) {
    details.push(`content-type: ${contentType}`);
  }

  let preview = '';
  if (typeof body === 'string') {
    preview = compactErrorPreview(body);
  } else if (body !== null && body !== undefined) {
    try {
      preview = compactErrorPreview(JSON.stringify(body));
    } catch {
      preview = compactErrorPreview(String(body));
    }
  }

  if (preview) {
    details.push(`body: ${preview}`);
  }

  if (details.length === 0) {
    return `HTTP ${response.status} unexpected response`;
  }
  return `HTTP ${response.status} unexpected response (${details.join('; ')})`;
}

/**
 * What the deployment's identity mode is, and whether this browser is signed in.
 *
 * Three identity answers, plus probe failure. A console that collapses
 * them guesses wrong in both directions. `GET /api/v1/auth/session` never
 * answers 401 — it exists to be asked. Its absence means the deployment has
 * no built-in OIDC login flow; local and external-auth deployments pass through.
 * Showing a sign-in page on a no-auth deployment would demand an account nobody
 * can have; skipping it on a signed-out one drops the reader at a blank console.
 */
export type AuthProbe =
  | { mode: 'no-auth' }
  | { mode: 'signed-in'; user: { user_id: string; display_name?: string | null } }
  | { mode: 'signed-out' }
  | { mode: 'error' };

/**
 * Asked with a bare fetch, not through the generated client, because the route
 * is not in the generated client: identity routes are registered by whichever
 * identity provider the deployment configures, and the schema snapshot is built
 * with `ASTRABOX_WEB_IDENTITY=local`, which registers none. Its absence is the
 * `no-auth` answer, so a client that could only call declared routes could not
 * ask this question at all.
 */
export async function probeAuthSession(signal?: AbortSignal): Promise<AuthProbe> {
  try {
    const response = await fetchWithNetworkRecovery(buildApiUrl('/api/v1/auth/session'), {
      credentials: 'include',
      cache: 'no-store',
      signal,
    });
    if (response.status === 404) return { mode: 'no-auth' };
    if (!response.ok) return { mode: 'error' };
    const payload = await response.json() as unknown;
    if (!payload || typeof payload !== 'object' || !('authenticated' in payload)) {
      return { mode: 'error' };
    }
    if (payload.authenticated === false) return { mode: 'signed-out' };
    if (payload.authenticated !== true || !('user' in payload)) return { mode: 'error' };
    const user = payload.user;
    if (!user || typeof user !== 'object' || !('user_id' in user)
      || typeof user.user_id !== 'string' || !user.user_id.trim()) {
      return { mode: 'error' };
    }
    return {
      mode: 'signed-in',
      user: {
        user_id: user.user_id,
        display_name: 'display_name' in user && typeof user.display_name === 'string'
          ? user.display_name : null,
      },
    };
  } catch (error) {
    if (signal?.aborted) throw error;
    return { mode: 'error' };
  }
}

/**
 * Where the browser goes to start the OIDC flow, preserving where it was headed.
 * *next* is a console route (see `currentAppPath`); the server adds the base
 * path when it sends the reader back.
 */
export function signInHref(next: string): string {
  return buildApiUrl(`/api/v1/auth/login?next=${encodeURIComponent(next)}`);
}

/** End the session or obtain the provider's browser navigation; never retry the POST. */
export async function endAuthSession(): Promise<string> {
  const response = await consoleFetch(buildApiUrl('/api/v1/auth/logout'), {
    method: 'POST', credentials: 'include', redirect: 'error',
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) throw new Error(`LOGOUT_FAILED: HTTP ${response.status}`);
  if (response.status === 204) {
    const probe = await probeAuthSession();
    if (probe.mode !== 'signed-out') throw new Error('LOGOUT_NOT_CONFIRMED');
    return `${MODULE_BASE}/`;
  }
  if (response.status !== 200) throw new Error(`LOGOUT_RESPONSE_INVALID: HTTP ${response.status}`);
  const payload: unknown = await response.json();
  if (!payload || typeof payload !== 'object' || !('redirect_url' in payload)
    || typeof payload.redirect_url !== 'string' || !payload.redirect_url.trim()) {
    throw new Error('LOGOUT_RESPONSE_INVALID: redirect_url is required');
  }
  const destination = new URL(payload.redirect_url, window.location.href);
  if (!['http:', 'https:'].includes(destination.protocol) || destination.username || destination.password) {
    throw new Error('LOGOUT_RESPONSE_INVALID: redirect_url must be an HTTP(S) navigation');
  }
  return destination.href;
}

/**
 * Where the reader is, as a console route with its query: the page's path
 * without `MODULE_BASE`. Sign-in carries it as `next` in this form, so every
 * redirect the server composes from it stays inside the base path.
 */
export function currentAppPath(): string {
  const { pathname, search } = window.location;
  if (pathname !== MODULE_BASE && !pathname.startsWith(`${MODULE_BASE}/`)) {
    throw new Error(`${pathname} is outside the console's base path ${MODULE_BASE}`);
  }
  return `${pathname.slice(MODULE_BASE.length) || '/'}${search}`;
}

// Under the oidc identity mode the backend answers 401 AUTH_REQUIRED when no
// session cookie is presented. The console sends the reader to its own sign-in
// page rather than straight to the identity provider: a person who has never
// seen AstraBox should not have their first impression be somebody else's login
// screen, and a deployment can name which account to use there. The timestamp
// guard keeps a misconfigured deployment (cookie never sticks) from
// redirect-looping on every failed request.
function redirectToSignIn(): void {
  const last = Number(window.sessionStorage.getItem('astrabox:signin-redirect') || 0);
  if (Date.now() - last < 10_000) return;
  window.sessionStorage.setItem('astrabox:signin-redirect', String(Date.now()));
  window.location.assign(`${MODULE_BASE}/login?next=${encodeURIComponent(currentAppPath())}`);
}

// ── The generated client ─────────────────────────────────────────────────────
// Paths, path parameters and response shapes come from tests/data/openapi_snapshot.json
// through src/api/schema.d.ts (`npm run gen:api`; `make check-api-client` fails
// if the committed file is not what the snapshot regenerates). What this file
// still owns is everything the document does not describe: the success envelope
// every handler wraps its payload in, the error vocabulary, and the transport.

/**
 * An origin that exists only so a URL can be parsed.
 *
 * openapi-fetch assembles a `Request`, and `new Request()` demands an absolute
 * URL. The console talks to its own origin over root-relative paths and has no
 * origin to name at module load (`window` is absent under the unit suite's node
 * environment), so a fixed placeholder stands in during assembly and
 * `originRelative()` removes it again before the request is issued, so what
 * goes on the wire is the root-relative path.
 * A `VITE_API_BASE` that names a real origin is used as-is and never stripped.
 */
const URL_ASSEMBLY_ORIGIN = 'http://api-base.invalid';
const API_BASE_IS_ABSOLUTE = /^https?:\/\//i.test(API_BASE);
const CLIENT_BASE_URL = API_BASE_IS_ABSOLUTE ? API_BASE : `${URL_ASSEMBLY_ORIGIN}${API_BASE}`;

function originRelative(url: string): string {
  return url.startsWith(URL_ASSEMBLY_ORIGIN) ? url.slice(URL_ASSEMBLY_ORIGIN.length) : url;
}

/**
 * The one place a response body is read, so the raw text survives a parse
 * failure.
 *
 * openapi-fetch parses the body itself and throws when a 2xx is not JSON — the
 * case an API path serving the SPA produces, reported as
 * "Load failed HTTP_200: <!doctype". Reading the text here and handing the
 * client a response rebuilt from it keeps the count of reads at one while
 * leaving the exact bytes available to build the failure message from.
 */
interface Transport {
  (request: Request): Promise<Response>;
  /** The response as received, present once one arrived at all. */
  response: Response | null;
  /** Its body, verbatim. */
  text: string;
  /**
   * The path asked for. Read from the request rather than from
   * `response.url`, which is empty on a response that was constructed rather
   * than fetched — and which does not exist at all when nobody answered, the
   * case whose message needs the path most.
   */
  url: string;
}

function transport(recovery: NetworkRecoveryOptions = {}, multipartBody?: FormData): Transport {
  const wire: Transport = (async (request: Request): Promise<Response> => {
    const url = originRelative(request.url);
    wire.url = url;
    const headers = new Headers(request.headers);
    if (multipartBody) {
      // Fetch frames the original files without reading them into JS memory.
      // Its boundary must be generated with that body, not copied from Request.
      headers.delete('Content-Type');
    } else if (!headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
    const body = multipartBody
      ?? (request.body === null ? undefined : await request.arrayBuffer());
    let response: Response;
    try {
      response = await fetchWithNetworkRecovery(url, {
        method: request.method,
        headers,
        credentials: 'include',
        signal: request.signal,
        ...(body === undefined ? {} : { body }),
      }, recovery);
      if (response.status !== 204 && response.status !== 205 && response.status !== 304) {
        wire.text = await response.text();
      }
    } catch (error) {
      if (isRequestAbortError(error)) {
        throw error;
      }
      const detail = error instanceof Error ? error.message : String(error || 'request failed');
      throw new NetworkRequestError(i18n.t('misc:api_error.network', { url, detail }), { cause: error });
    }
    wire.response = response;
    // A status defined to carry no body cannot be rebuilt with one.
    if (response.status === 204 || response.status === 205 || response.status === 304) {
      return response;
    }
    return new Response(wire.text, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  }) as Transport;
  wire.response = null;
  wire.text = '';
  wire.url = '';
  return wire;
}

/** The payload an operation's success envelope carries at `data`. */
type PayloadOf<R> = R extends { data?: infer Envelope }
  ? Envelope extends { data: infer Payload }
    ? Payload
    : never
  : never;

/**
 * Query strings and the bodies of the routes that read the raw request.
 *
 * The document declares two query parameters across 153 routes, and a body on
 * 42 of the 74 write routes, because these handlers take `request: Request` and
 * read `request.query_params` / `await request.json()` themselves — FastAPI has
 * nothing to declare on their behalf, so the generated client types them as
 * `never`. Sending one is therefore an assertion, and it is made here, once,
 * rather than at each call site: the day a route declares its parameters, its
 * call site gains a checked `params.query` and stops coming through here.
 *
 * `never` is the return type because that is what makes the value assignable
 * wherever the generated client says the route accepts nothing — it is a claim
 * about the wire, and naming it in one function is what keeps it countable.
 */
function undeclared(value: Record<string, unknown>): never {
  const present: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    if (item !== undefined) present[key] = item;
  }
  return present as never;
}

/** The same assertion for a query string, dropping absent and empty terms. */
function undeclaredQuery(values: Record<string, string | number | boolean | null | undefined>): never {
  const query: Record<string, string> = {};
  for (const [key, value] of Object.entries(values)) {
    if (value !== null && value !== undefined && value !== '') query[key] = String(value);
  }
  return query as never;
}

/**
 * Issue one generated call and hand back what its envelope carried.
 *
 * The wire is created per call because the recovery budget is per call and the
 * captured response belongs to this request alone.
 */
async function send<R extends { response: Response }>(
  run: (wire: { fetch: Transport }) => Promise<R>,
  recovery: NetworkRecoveryOptions = {},
  multipartBody?: FormData,
): Promise<PayloadOf<R>> {
  const wire = transport(recovery, multipartBody);
  let response: Response;
  let typed: unknown;
  try {
    const result = await run({ fetch: wire }) as { data?: unknown; response: Response };
    response = result.response;
    typed = result.data;
  } catch (error) {
    // No response means nobody answered: the transport already shaped that as a
    // network failure, and an abort is the caller's own cancellation. A throw
    // with a response in hand is a body the client could not parse, which the
    // refusal path below reports from the bytes that arrived.
    if (!wire.response) throw error;
    response = wire.response;
  }

  if (response.status === 204 && response.ok) {
    return undefined as PayloadOf<R>;
  }

  let rawPayload: unknown = null;
  let unexpectedResponseMessage = '';
  try {
    rawPayload = wire.text ? JSON.parse(wire.text) : null;
  } catch {
    unexpectedResponseMessage = buildUnexpectedResponseMessage(response, wire.text);
  }
  const payload = rawPayload as ApiResponse<unknown> | null;
  const url = wire.url;

  if (!response.ok || !payload || payload.code !== 'OK') {
    const legacyMessage =
      rawPayload && typeof rawPayload === 'object' && 'err_message' in rawPayload
        ? String((rawPayload as Record<string, unknown>).err_message ?? '')
        : '';

    const normalizedLegacy = normalizeLegacyApiError(legacyMessage, response, url);
    const rawCode = String(payload?.error?.code ?? payload?.code ?? normalizedLegacy?.code ?? `HTTP_${response.status}`);
    const code = rawCode === 'UNKNOWN_ERROR' ? 'PLATFORM_SERVER_ERROR' : rawCode;
    const rawMessage =
      payload?.error?.user_message ??
      payload?.message ??
      normalizedLegacy?.message ??
      (legacyMessage || unexpectedResponseMessage || buildUnexpectedResponseMessage(response, rawPayload));
    const normalizedPayload =
      rawCode === 'UNKNOWN_ERROR'
        ? normalizeLegacyApiError(rawMessage, response, url)
        : null;
    const message = normalizedPayload?.message ?? rawMessage;
    if (response.status === 401 && rawCode === 'AUTH_REQUIRED') {
      redirectToSignIn();
    }
    const retryable = payload?.error?.retryable;
    throw new ApiError(
      normalizedPayload?.code ?? code,
      appendStructuredApiErrorEvidence(message, payload as ApiResponse<unknown> | null),
      response.status,
      typeof retryable === 'boolean' ? retryable : null,
      code === 'TURN_PREPARATION_USER_ACTION_REQUIRED'
        ? readUserAction(payload?.error?.evidence?.user_action) : null,
    );
  }

  // The typed value is the client's; `payload.data` is the same field read off
  // the same bytes, and is what a 201 or any other 2xx the operation does not
  // declare still resolves to.
  return ((typed as { data?: unknown } | undefined)?.data ?? payload.data) as PayloadOf<R>;
}

const client = createClient<paths>({ baseUrl: CLIENT_BASE_URL, credentials: 'include' });

type Schemas = components['schemas'];

/**
 * The console's own reading of a payload the document leaves open.
 *
 * A dozen routes declare `dict[str, Any]`, or a list of them: the handler builds
 * the object out of a store record and the response model says only that it is
 * an object. The console still has to name the fields it renders, so the
 * stronger type is a claim about the wire rather than a check on it, written
 * down here so it can be counted and removed one route at a time as the
 * server's models get specific. Where the value goes straight into render, the
 * runtime guards below still stand in front of it, because a claim is not a
 * check.
 */
function asDeclared<T>(payload: unknown): T {
  return payload as T;
}

/**
 * Assert that an endpoint promising a list actually returned one.
 *
 * The generated schema says what the server declares; it is not a check on what
 * arrived. An unchecked wrong claim flows into component state and fails later,
 * inside render, as "Cannot read properties of null (reading 'find')". React
 * unmounts the tree on a render error, so the page goes white: no path taken,
 * and no failure reported either.
 *
 * Failing here instead puts the error where every one of these calls already
 * has a `catch` that shows it. The list is not defaulted to `[]` — an empty
 * list is a real answer meaning "none", and substituting it for "the server
 * said something unrecognized" is the kind of quiet degradation this
 * repository does not allow.
 */
function expectPage<T>(value: unknown, endpoint: string, key: string): T {
  if (value && typeof value === 'object' && key in (value as Record<string, unknown>)) {
    return value as T;
  }
  throw new ApiError(
    'MALFORMED_RESPONSE',
    i18n.t('misc:api_error.malformed_record', {
      endpoint,
      got: value === null ? 'null' : typeof value,
    }),
    200,
  );
}

function expectList<T>(value: unknown, endpoint: string): T[] {
  if (Array.isArray(value)) return value as T[];
  throw new ApiError(
    'MALFORMED_RESPONSE',
    i18n.t('misc:api_error.malformed_list', {
      endpoint,
      got: value === null ? 'null' : typeof value,
    }),
    200,
  );
}

function malformedRecord(endpoint: string): never {
  throw new ApiError(
    'MALFORMED_RESPONSE',
    i18n.t('misc:api_error.malformed_fields', { endpoint }),
    200,
  );
}

export function isRequestAbortError(error: unknown): boolean {
  return isAbortError(error);
}

export async function getCurrentUser(): Promise<UserInfo> {
  return send((wire) => client.GET('/api/v1/user/current', wire));
}

// ── Agents ──────────────────────────────────────────────────────────────────
// One Agent resource carries its model, prompt, extensions, and Environment.
// Every write addresses it by
// `agent_id` — the name is a label the operator can change.

export async function listAgents(): Promise<AgentConfig[]> {
  return expectList<AgentConfig>(await send((wire) => client.GET('/api/v1/agents', wire)), '/agents');
}

export async function getAgent(agentId: string): Promise<AgentConfig> {
  return asDeclared<AgentConfig>(await send((wire) => client.GET('/api/v1/agents/{agent_id}', {
    ...wire,
    params: { path: { agent_id: agentId } },
  })));
}

/** One page of the Agent list, narrowed and counted by the server. */
export async function listAgentsPage(query: ListPageQuery = {}): Promise<AgentListPage> {
  return expectPage<AgentListPage>(
    await send((wire) => client.GET('/api/v1/agents', {
      ...wire,
      params: { query: listPageQuery(query) },
    })),
    '/agents',
    'agents',
  );
}

/**
 * The query of a paged list read (`page=1`, the list routes' page mode). The
 * Agent and Assistant list routes read these from the raw request, so this is
 * the `undeclaredQuery` assertion for both.
 */
export function listPageQuery(query: ListPageQuery): never {
  return undeclaredQuery({
    page: 1,
    limit: query.limit ?? 50,
    cursor: query.cursor,
    q: query.q?.trim(),
    status: query.status === 'all' ? undefined : query.status,
  });
}

/** Admin-only credential groups plus the active delivery summary. */
export async function getAdminVaultCatalog(): Promise<VaultCatalog> {
  const page = expectPage<{ vaults: unknown; credential_delivery?: unknown }>(
    await send((wire) => client.GET('/api/v1/admin/vaults', wire)),
    '/admin/vaults',
    'vaults',
  );
  const vaults = expectList<VaultSummary>(page.vaults, '/admin/vaults');
  if (!page.credential_delivery || typeof page.credential_delivery !== 'object') {
    throw new Error('GET /admin/vaults returned no credential_delivery summary');
  }
  return {
    vaults,
    credential_delivery: page.credential_delivery as VaultCatalog['credential_delivery'],
  };
}

export async function createAdminVault(
  displayName: string,
  metadata?: Record<string, unknown>,
): Promise<VaultSummary> {
  return asDeclared<VaultSummary>(await send((wire) => client.POST('/api/v1/admin/vaults', {
    ...wire,
    body: { display_name: displayName, metadata },
  })));
}

export async function archiveAdminVault(vaultId: string): Promise<VaultSummary> {
  return asDeclared<VaultSummary>(await send((wire) => client.POST('/api/v1/admin/vaults/{vault_id}/archive', {
    ...wire,
    params: { path: { vault_id: vaultId } },
  })));
}

export async function deleteAdminVault(vaultId: string): Promise<void> {
  await send((wire) => client.DELETE('/api/v1/admin/vaults/{vault_id}', {
    ...wire,
    params: { path: { vault_id: vaultId } },
  }));
}

export async function createAdminVaultCredential(
  vaultId: string,
  payload: { display_name?: string; auth: Record<string, unknown> },
): Promise<import('./types').VaultCredentialSummary> {
  return send((wire) => client.POST('/api/v1/admin/vaults/{vault_id}/credentials', {
    ...wire,
    params: { path: { vault_id: vaultId } },
    body: { display_name: payload.display_name, auth: payload.auth },
  })) as Promise<import('./types').VaultCredentialSummary>;
}

export async function archiveAdminVaultCredential(
  vaultId: string,
  credentialId: string,
): Promise<import('./types').VaultCredentialSummary> {
  return send((wire) => client.POST(
    '/api/v1/admin/vaults/{vault_id}/credentials/{credential_id}/archive',
    { ...wire, params: { path: { vault_id: vaultId, credential_id: credentialId } } },
  )) as Promise<import('./types').VaultCredentialSummary>;
}

export async function deleteAdminVaultCredential(
  vaultId: string,
  credentialId: string,
): Promise<void> {
  await send((wire) => client.DELETE(
    '/api/v1/admin/vaults/{vault_id}/credentials/{credential_id}',
    { ...wire, params: { path: { vault_id: vaultId, credential_id: credentialId } } },
  ));
}

export async function getAgentCredentialBinding(agentId: string): Promise<CredentialBinding> {
  return send((wire) => client.GET('/api/v1/admin/agents/{agent_id}/credential-vaults', {
    ...wire,
    params: { path: { agent_id: agentId } },
  })) as Promise<CredentialBinding>;
}

export async function listAdminVaultBindings(
  vaultId: string,
): Promise<VaultBindingHolder[]> {
  return expectList<VaultBindingHolder>(
    await send((wire) => client.GET('/api/v1/admin/vaults/{vault_id}/bindings', {
      ...wire,
      params: { path: { vault_id: vaultId } },
    })),
    '/admin/vaults/{vault_id}/bindings',
  );
}

export async function setAgentCredentialBinding(
  agentId: string,
  vaultIds: string[],
): Promise<CredentialBinding> {
  return send((wire) => client.PUT('/api/v1/admin/agents/{agent_id}/credential-vaults', {
    ...wire,
    params: { path: { agent_id: agentId } },
    body: { vault_ids: vaultIds },
  })) as Promise<CredentialBinding>;
}

export async function getAssistantCredentialBinding(
  assistantId: string,
): Promise<CredentialBinding> {
  return send((wire) => client.GET('/api/v1/admin/assistants/{assistant_id}/credential-vaults', {
    ...wire,
    params: { path: { assistant_id: assistantId } },
  })) as Promise<CredentialBinding>;
}

export async function setAssistantCredentialBinding(
  assistantId: string,
  vaultIds: string[],
): Promise<CredentialBinding> {
  return send((wire) => client.PUT('/api/v1/admin/assistants/{assistant_id}/credential-vaults', {
    ...wire,
    params: { path: { assistant_id: assistantId } },
    body: { vault_ids: vaultIds },
  })) as Promise<CredentialBinding>;
}

/**
 * The authoritative editable shape of an Agent.
 *
 * The form is schema-driven on purpose: `agent_schema.py` calls itself the
 * single source for this shape, so a field added there reaches the console
 * without a frontend change. Only the field's copy is maintained in the
 * frontend (i18n, keyed by `key`); the structure is never hardcoded here.
 */
export async function getAgentSchema(): Promise<FormSchema> {
  return asDeclared<FormSchema>(await send((wire) => client.GET('/api/v1/agent-configuration/schema', wire)));
}

/** Secret-free Environment choices available to any signed-in Agent author. */
export async function listAgentEnvironments(): Promise<EnvironmentConfig[]> {
  return expectList<EnvironmentConfig>(
    await send((wire) => client.GET('/api/v1/agent-configuration/environments', wire)),
    '/agent-configuration/environments',
  );
}

/** Model routing ids advertised by the selected Environment's gateway. */
export async function listAgentEnvironmentModels(environmentName: string): Promise<string[]> {
  if (!environmentName.trim()) return [];
  const result = await send((wire) => client.GET(
    '/api/v1/agent-configuration/environments/{name}/models',
    { ...wire, params: { path: { name: environmentName } } },
  ));
  return Array.isArray(result.models)
    ? [...new Set(result.models.map(String).filter(Boolean))].sort((a, b) => a.localeCompare(b))
    : [];
}

/**
 * Project a form draft onto the server-advertised authoring wire shape.
 *
 * Stored Agent records also carry identity, lifecycle, access, and derived
 * capability fields. A fetched record is therefore never a valid PUT body.
 * The schema is already loaded by both Agent forms; deriving the top-level keys
 * from it keeps this projection on the same contract the server validates.
 */
export function projectAgentAuthoringPayload(
  payload: Record<string, unknown>,
  schema: FormSchema,
  options: { includeVersion?: boolean } = {},
): Record<string, unknown> {
  const allowed = new Set(
    schema.fields.map((field) => String(field.path || field.key).split('.', 1)[0]),
  );
  if (allowed.size === 0) {
    throw new Error('Agent authoring schema has no fields');
  }
  if (options.includeVersion) allowed.add('version');
  return Object.fromEntries(Object.entries(payload).filter(([key]) => allowed.has(key)));
}

/** Build the complete access request, including the secure create default. */
export function agentAccessPayload(payload: Record<string, unknown>): AgentAccessPolicy {
  const visibility = payload.visibility == null ? 'private' : String(payload.visibility);
  if (!['public', 'private', 'allowlist'].includes(visibility)) {
    throw new Error(`Invalid Agent visibility: ${visibility}`);
  }
  const ids = (key: 'admins' | 'allowed_user_ids') => {
    const value = payload[key];
    if (value == null) return [];
    if (
      !Array.isArray(value)
      || value.some((item) => typeof item !== 'string' || item.trim() === '')
    ) {
      throw new Error(`Invalid Agent ${key}: expected non-empty user IDs`);
    }
    return value as string[];
  };
  return {
    visibility: visibility as AgentAccessPolicy['visibility'],
    admins: ids('admins'),
    allowed_user_ids: ids('allowed_user_ids'),
  };
}

export async function getAgentAccess(agentId: string): Promise<AgentAccess> {
  return send((wire) => client.GET('/api/v1/agents/{agent_id}/access', {
    ...wire,
    params: { path: { agent_id: agentId } },
  })) as Promise<AgentAccess>;
}

export async function getAgentPreparedRuntime(agentId: string): Promise<AgentPreparedRuntimeStatus> {
  return send((wire) => client.GET('/api/v1/agents/{agent_id}/prepared-runtime', {
    ...wire,
    params: { path: { agent_id: agentId } },
  }));
}

export async function refreshAgentPreparedRuntime(agentId: string): Promise<AgentPreparedRuntimeStatus> {
  return send((wire) => client.POST('/api/v1/agents/{agent_id}/prepared-runtime/refresh', {
    ...wire,
    params: { path: { agent_id: agentId } },
  }));
}

export async function setAgentAccess(
  agentId: string,
  access: AgentAccessPolicy,
): Promise<AgentAccess> {
  return send((wire) => client.PUT('/api/v1/agents/{agent_id}/access', {
    ...wire,
    params: { path: { agent_id: agentId } },
    body: access,
  })) as Promise<AgentAccess>;
}

export async function createAgent(
  payload: AgentDraft,
  schema: FormSchema,
): Promise<AgentConfig> {
  const requestedAccess = agentAccessPayload(payload);
  const created = await send((wire) => client.POST('/api/v1/agents', {
    ...wire,
    body: undeclared(projectAgentAuthoringPayload(payload, schema)),
  })) as AgentConfig;
  // Access is a separate authorization boundary. Always make the second phase
  // explicit so a create with access fields cannot silently discard them; an
  // untouched form writes the secure private policy.
  const access = await setAgentAccess(created.agent_id, requestedAccess);
  return { ...created, ...access };
}

/**
 * Update one Agent by id.
 *
 * Send the `version` the draft was read at and the server fences the write
 * (409 AGENT_VERSION_CONFLICT if someone else saved in between); omit it and
 * the write applies unconditionally. The console sends it — a silent
 * last-writer-wins overwrite of an Agent is the failure worth refusing.
 */
export async function updateAgent(
  agentId: string,
  payload: AgentDraft & { version?: number },
  schema: FormSchema,
): Promise<AgentConfig> {
  return send((wire) => client.PUT('/api/v1/agents/{agent_id}', {
    ...wire,
    params: { path: { agent_id: agentId } },
    body: undeclared(projectAgentAuthoringPayload(payload, schema, { includeVersion: true })),
  })) as Promise<AgentConfig>;
}

/**
 * The extension provider whose catalog this deployment offers, or null when it
 * runs none (`ASTRABOX_EXTENSION_PROVIDER=none`). Reads no catalog.
 */
export async function getExtensionProvider(): Promise<ExtensionProviderView | null> {
  const page = expectPage<{ provider: ExtensionProviderView | null }>(
    await send((wire) => client.GET('/api/v1/extension-provider', wire)),
    '/extension-provider',
    'provider',
  );
  return page.provider;
}

export async function createAgentExtensionConsoleSession(
  agentId: string,
  section: 'mcp' | 'skills',
): Promise<{ open_url: string; expires_in: number }> {
  return send((wire) => client.POST('/api/v1/agents/{agent_id}/extension-console/session', {
    ...wire,
    params: { path: { agent_id: agentId } },
    body: { section },
  }));
}

export async function getAgentExtensions(
  agentId: string,
): Promise<AgentExtensionCatalog> {
  return send((wire) => client.GET('/api/v1/agents/{agent_id}/extensions', {
    ...wire,
    params: { path: { agent_id: agentId } },
  })) as Promise<AgentExtensionCatalog>;
}

export async function setAgentExtensions(
  agentId: string,
  selection: { mcp_server_ids: string[]; skill_ids: string[] },
): Promise<AgentExtensionCatalog> {
  return send((wire) => client.PUT('/api/v1/agents/{agent_id}/extensions', {
    ...wire,
    params: { path: { agent_id: agentId } },
    body: selection,
  })) as Promise<AgentExtensionCatalog>;
}

const CONVERSATION_CREATE_TIMEOUT_MS = 8_000;
const CONVERSATION_CREATE_RETRY_DELAYS_MS = [0, 250, 750] as const;

function newConversationIdempotencyKey(): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  return `conversation-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function retryDelay(milliseconds: number): Promise<void> {
  if (milliseconds <= 0) return Promise.resolve();
  return new Promise((resolve) => globalThis.setTimeout(resolve, milliseconds));
}

/**
 * Create one conversation across a transient network change.
 *
 * POST is normally unsafe to retry: the first request may have committed even
 * when its response never reached the browser.  The one generated key is sent
 * on every attempt so the server converges all attempts onto the same Session.
 * HTTP refusals are never retried; only a request that received no response is.
 *
 * Takes the attempt rather than a path so both conversation routes reach it
 * through their own generated call, each with its own path parameters and its
 * own declared result.
 */
export async function startConversationRequest<T>(
  attempt: (headers: Record<string, string>, signal: AbortSignal) => Promise<T>,
  endpoint: string,
): Promise<T> {
  const idempotencyKey = newConversationIdempotencyKey();
  let lastTransportError: unknown = null;

  for (let index = 0; index < CONVERSATION_CREATE_RETRY_DELAYS_MS.length; index += 1) {
    await retryDelay(CONVERSATION_CREATE_RETRY_DELAYS_MS[index]);
    const controller = new AbortController();
    const timeout = globalThis.setTimeout(
      () => controller.abort(),
      CONVERSATION_CREATE_TIMEOUT_MS,
    );
    try {
      return await attempt({ 'Idempotency-Key': idempotencyKey }, controller.signal);
    } catch (error) {
      if (error instanceof ApiError) throw error;
      lastTransportError = isRequestAbortError(error)
        ? new Error(i18n.t('misc:api_error.network', {
            url: endpoint,
            detail: `request timed out after ${CONVERSATION_CREATE_TIMEOUT_MS}ms`,
          }))
        : error;
    } finally {
      globalThis.clearTimeout(timeout);
    }
  }

  if (lastTransportError instanceof Error) throw lastTransportError;
  throw new Error(i18n.t('misc:api_error.network', {
    url: endpoint,
    detail: String(lastTransportError || 'request timed out'),
  }));
}

/**
 * Open a new conversation on one agent.
 *
 * The conversation owns its own sandbox; the agent record is metadata, so there
 * is nothing to wake first and no shared box to contend for.
 */
export async function startAgentConversation(
  agentId: string,
): Promise<{ session_id: string; agent_id: string }> {
  const path = `/api/v1/agents/${encodeURIComponent(agentId)}/conversations`;
  return startConversationRequest(
    async (headers, signal) => await send((wire) => client.POST('/api/v1/agents/{agent_id}/conversations', {
      ...wire,
      params: { path: { agent_id: agentId } },
      headers,
      signal,
    })) as { session_id: string; agent_id: string },
    path,
  );
}

export async function deleteAgent(agentId: string): Promise<AgentConfig> {
  return send((wire) => client.DELETE('/api/v1/agents/{agent_id}', {
    ...wire,
    params: { path: { agent_id: agentId } },
  })) as Promise<AgentConfig>;
}

// ── Agent catalog ───────────────────────────────────────────────────────────
// How people find Agents: the categories a platform administrator defines, the
// tags an Agent's authors write, and each user's own favorites.

/** Every Agent category, in the administrator's order. */
export async function listAgentCategories(): Promise<AgentCategory[]> {
  return expectList<AgentCategory>(
    await send((wire) => client.GET('/api/v1/agent-categories', wire)),
    '/agent-categories',
  );
}

/** The tags of the Agents the caller may see, most used first. */
export async function listAgentTags(): Promise<AgentTag[]> {
  return expectList<AgentTag>(
    await send((wire) => client.GET('/api/v1/agent-tags', wire)),
    '/agent-tags',
  );
}

/** Favorite or unfavorite one Agent for the caller; repeating either is a no-op. */
export async function setAgentFavorite(agentId: string, favorited: boolean): Promise<AgentFavorite> {
  const path = { agent_id: agentId };
  return send((wire) =>
    favorited
      ? client.PUT('/api/v1/agents/{agent_id}/favorite', { ...wire, params: { path } })
      : client.DELETE('/api/v1/agents/{agent_id}/favorite', { ...wire, params: { path } }),
  );
}

/** Every category with the number of Agents it holds (platform administrators). */
export async function listAdminAgentCategories(): Promise<AdminAgentCategory[]> {
  return expectList<AdminAgentCategory>(
    await send((wire) => client.GET('/api/v1/admin/agent-categories', wire)),
    '/admin/agent-categories',
  );
}

/**
 * Add a category at the end of the order.
 *
 * Refused with 409 `AGENT_CATEGORY_NAME_TAKEN` when another category has the
 * same name regardless of case.
 */
export async function createAdminAgentCategory(name: string): Promise<AgentCategory> {
  return send((wire) => client.POST('/api/v1/admin/agent-categories', {
    ...wire,
    body: { name },
  }));
}

/** Rename one category; Agents name it by id, so they follow without a write. */
export async function renameAdminAgentCategory(
  categoryId: string,
  name: string,
): Promise<AgentCategory> {
  return send((wire) => client.PUT('/api/v1/admin/agent-categories/{category_id}', {
    ...wire,
    params: { path: { category_id: categoryId } },
    body: { name },
  }));
}

/**
 * Put the categories in a new order.
 *
 * `categoryIds` must name every category exactly once; the server refuses a
 * partial list rather than guessing where the missing ones go.
 */
export async function reorderAdminAgentCategories(categoryIds: string[]): Promise<AgentCategory[]> {
  return expectList<AgentCategory>(
    await send((wire) => client.PUT('/api/v1/admin/agent-categories/order', {
      ...wire,
      body: { category_ids: categoryIds },
    })),
    '/admin/agent-categories/order',
  );
}

/** Delete one category after taking it off every Agent that holds it. */
export async function deleteAdminAgentCategory(categoryId: string): Promise<DeletedAgentCategory> {
  return send((wire) => client.DELETE('/api/v1/admin/agent-categories/{category_id}', {
    ...wire,
    params: { path: { category_id: categoryId } },
  }));
}

export async function listAdminEnvironments(): Promise<EnvironmentConfig[]> {
  return expectList<EnvironmentConfig>(
    await send((wire) => client.GET('/api/v1/admin/environments', wire)),
    '/admin/environments',
  );
}

export async function getEnvironmentSchema(): Promise<FormSchema> {
  return asDeclared<FormSchema>(await send((wire) => client.GET('/api/v1/admin/environment-schema', wire)));
}

export async function deleteAdminEnvironment(name: string): Promise<void> {
  await send((wire) => client.DELETE('/api/v1/admin/environments/{name}', {
    ...wire,
    params: { path: { name } },
  }));
}

export async function upsertAdminEnvironment(
  name: string,
  payload: EnvironmentConfig
): Promise<EnvironmentConfig> {
  return send((wire) => client.PUT('/api/v1/admin/environments/{name}', {
    ...wire,
    params: { path: { name } },
    body: undeclared(payload),
  })) as Promise<EnvironmentConfig>;
}

// ── Admin session console (management surface) ──────────────────────────────
/**
 * One page of sessions, narrowed by the server.
 *
 * The filters are query terms, not a pass over the result: a deployment with a
 * hundred agents and a thousand conversations a day each cannot be listed by
 * fetching a few hundred rows and sieving them in the browser, and the count
 * beside the page has to come from the collection rather than from the page.
 */
export async function adminListSessions(
  opts: AdminSessionFilters & { page?: number; pageSize?: number } = {},
): Promise<AdminSessionPage> {
  const endpoint = '/api/v1/admin/sessions/all';
  // Guarded like its peer listSessionsPage: an answer without `items` is "the
  // server said something unrecognized", not "no sessions" — substituting the
  // empty state for it is the quiet degradation this repository does not allow.
  return expectPage<AdminSessionPage>(
    await send((wire) => client.GET('/api/v1/admin/sessions/all', {
      ...wire,
      params: {
        query: undeclaredQuery({
          page: opts.page ?? 1,
          page_size: opts.pageSize ?? 50,
          agent_id: opts.agent_id,
          since: opts.since,
          until: opts.until,
        }),
      },
    })),
    endpoint,
    'items',
  );
}

/**
 * One agent's conversations, newest-first (server-side agent_id query, its own
 * `limit` — a busy deployment's other agents cannot page these out of reach).
 * Requires the caller to manage the agent; the server 403s otherwise.
 */
export async function listAgentSessions(agentId: string, limit = 500): Promise<AdminSessionSummary[]> {
  return expectList<AdminSessionSummary>(
    await send((wire) => client.GET('/api/v1/agents/{agent_id}/sessions', {
      ...wire,
      params: { path: { agent_id: agentId }, query: undeclaredQuery({ limit }) },
    })),
    '/agents/{agent_id}/sessions',
  );
}

export async function adminGetSessionDetail(sessionId: string): Promise<AdminSessionDetail> {
  return asDeclared<AdminSessionDetail>(await send((wire) => client.GET('/api/v1/admin/sessions/{session_id}/detail', {
    ...wire,
    params: { path: { session_id: sessionId } },
  })));
}

/**
 * A session's latest messages beside one response's stored parts: the newest
 * response still running, else the newest on the page, unless `responseId`
 * names another.
 */
export async function adminGetSessionTrace(
  sessionId: string,
  opts: { responseId?: string; messageLimit?: number; partLimit?: number } = {}
): Promise<AdminSessionTrace> {
  return asDeclared<AdminSessionTrace>(await send((wire) => client.GET('/api/v1/admin/sessions/{session_id}/trace', {
    ...wire,
    params: {
      path: { session_id: sessionId },
      query: undeclaredQuery({
        response_id: opts.responseId,
        message_limit: opts.messageLimit,
        part_limit: opts.partLimit,
      }),
    },
  })));
}

export async function adminKillSession(sessionId: string): Promise<Record<string, unknown>> {
  return send((wire) => client.POST('/api/v1/admin/sessions/{session_id}/kill', {
    ...wire,
    params: { path: { session_id: sessionId } },
  }));
}

/**
 * Drop this process's in-memory runtime for a session, leaving the session and
 * its sandbox alone.
 *
 * Distinct from kill, which ends the session. This is the narrower move for a
 * runtime that is wedged — held lock, stopped owner loop — where the session
 * itself is still wanted. The next turn reconnects and builds a new one.
 */
export async function adminEvictSessionRuntime(sessionId: string): Promise<Record<string, unknown>> {
  return send((wire) => client.POST('/api/v1/admin/sessions/{session_id}/evict-runtime', {
    ...wire,
    params: { path: { session_id: sessionId } },
  }));
}

/**
 * The session's transcript in the Claude Agent SDK's own format.
 *
 * `<sdk-session-id>.jsonl` — one SessionStoreEntry per line, exactly what the
 * SDK wrote. Dropped into any directory under `~/.claude/projects/`,
 * `claude --resume <sdk-session-id>` continues the conversation on the
 * operator's own machine. A session that also has subagent transcripts answers
 * with a `.tar.gz` holding the SDK's directory layout, because those are
 * separate files.
 *
 * A URL rather than a fetch: the browser takes the response straight to disk,
 * so a long transcript never lands in the tab's memory.
 */
export function buildAdminSessionTranscriptUrl(sessionId: string): string {
  return buildApiUrl(`/api/v1/admin/sessions/${encodeURIComponent(sessionId)}/transcript`);
}

/**
 * Every transcript the caller can reach, as one archive.
 *
 * The same files as the single-session URL, one directory per session in the
 * SDK's layout, so untarring under `~/.claude/projects/` makes each of them
 * resumable. Owner-scoped by the server: the listing it walks is already
 * filtered to agents the caller administers.
 */
export function buildAdminBatchTranscriptsUrl(filters: AdminSessionFilters = {}): string {
  const params = new URLSearchParams();
  if (filters.agent_id) params.set('agent_id', filters.agent_id);
  if (filters.since) params.set('since', filters.since);
  if (filters.until) params.set('until', filters.until);
  const suffix = params.toString();
  return buildApiUrl(`/api/v1/admin/sessions/transcripts${suffix ? `?${suffix}` : ''}`);
}

/** Resource totals for the management rail, scoped to the current user. */
export async function adminNavigationSummary(): Promise<AdminNavigationSummary> {
  return send((wire) => client.GET('/api/v1/admin/navigation-summary', wire)) as Promise<AdminNavigationSummary>;
}

// ── Admin process console (this server, right now) ───────────────────────────
// These read the process answering the call, not the database. Two consequences
// the UI has to respect: every reading is of one replica (a multi-replica
// deployment shows whichever answered), and nothing here is retrospective —
// there is no history to page back through, only what is true at this instant.

export async function adminSystemOverview(): Promise<AdminSystemOverview> {
  return send((wire) => client.GET('/api/v1/admin/system/overview', wire)) as Promise<AdminSystemOverview>;
}

/** Browser management surfaces enabled by this deployment's live configuration. */
export async function adminListIntegrations(): Promise<AdminIntegrations> {
  return send((wire) => client.GET('/api/v1/admin/integrations', wire)) as Promise<AdminIntegrations>;
}

export async function adminProcessHealth(): Promise<AdminProcessHealth> {
  return send((wire) => client.GET('/api/v1/admin/process/health', wire)) as Promise<AdminProcessHealth>;
}

/** Sessions carrying an error plus failed turn checkpoints, newest-first (server clamps to 500). */
export async function adminListErrors(limit = 200): Promise<AdminErrorsPage> {
  return send((wire) => client.GET('/api/v1/admin/errors', {
    ...wire,
    params: { query: undeclaredQuery({ limit }) },
  })) as Promise<AdminErrorsPage>;
}

/**
 * A window onto one log file on the host that answers.
 *
 * `file` names one of the `available_files` the server discovered; omit it and
 * the server picks its preferred file. A deployment that logs to stderr has no
 * files at all, and answers with an empty list — that is an answer, not a
 * failure, and the page says so rather than showing an empty log.
 */
export async function adminReadLogs(
  opts: { lines?: number; level?: string; keyword?: string; file?: string } = {}
): Promise<AdminLogsPage> {
  return send((wire) => client.GET('/api/v1/admin/logs', {
    ...wire,
    params: {
      query: undeclaredQuery({
        lines: opts.lines ?? 200,
        level: opts.level,
        keyword: opts.keyword,
        file: opts.file,
      }),
    },
  })) as Promise<AdminLogsPage>;
}

// ── Admin sandbox inventory (read-only ops face) ─────────────────────────────
// Sandboxes are created and destroyed by a session's lifecycle; this face only
// asks what a backend is running. There is deliberately no kill/renew here —
// that authority stays on the session.

export async function adminListSandboxes(
  params: { page?: number; pageSize?: number; backend?: string } = {}
): Promise<AdminSandboxPage> {
  return send((wire) => client.GET('/api/v1/admin/sandboxes', {
    ...wire,
    params: {
      query: undeclaredQuery({
        page: params.page,
        page_size: params.pageSize,
        backend: params.backend,
      }),
    },
  })) as Promise<AdminSandboxPage>;
}

/**
 * Each enabled backend's quota ceilings, what is held against them, and its
 * boxes. Usage is the backend's last inventory (taken at `inventory_at`) plus
 * the reservations for boxes that inventory does not show yet, counted the way
 * the quota itself counts them when it judges the next create.
 */
export async function adminSandboxQuota(): Promise<AdminSandboxQuota> {
  return send((wire) => client.GET('/api/v1/admin/sandbox-quota', wire)) as Promise<AdminSandboxQuota>;
}

/**
 * One plain-text diagnostic report. Rejections are meaningful here: a backend
 * (or its server) that cannot produce the report answers 501 with the reason,
 * which the caller should show as-is rather than as an empty report.
 */
export async function adminReadSandboxDiagnostics(
  sandboxId: string,
  scope: AdminSandboxDiagnosticScope
): Promise<AdminSandboxDiagnostics> {
  const endpoint = '/admin/sandboxes/{id}/diagnostics/{scope}';
  const report = expectPage<AdminSandboxDiagnostics>(
    await send((wire) => client.GET('/api/v1/admin/sandboxes/{sandbox_id}/diagnostics/{scope}', {
      ...wire,
      params: { path: { sandbox_id: sandboxId, scope } },
    })),
    endpoint,
    'text',
  );
  if (
    typeof report.text !== 'string'
    || typeof report.truncated !== 'boolean'
    || !Array.isArray(report.known_scopes)
  ) {
    return malformedRecord(endpoint);
  }
  return report;
}

/**
 * What a sandbox reports about its own containment — its egress policy and the
 * names in its credential vault.
 *
 * A box with no egress sidecar answers `available: false` with the reason; that
 * is a 200, not a rejection, because "nothing contains this box" is an answer an
 * operator needs rather than an error.
 */
export async function adminReadSandboxSecurity(
  sandboxId: string
): Promise<AdminSandboxSecurity> {
  const endpoint = '/admin/sandboxes/{id}/security';
  const posture = expectPage<AdminSandboxSecurity>(
    await send((wire) => client.GET('/api/v1/admin/sandboxes/{sandbox_id}/security', {
      ...wire,
      params: { path: { sandbox_id: sandboxId } },
    })),
    endpoint,
    'available',
  );
  if (
    typeof posture.available !== 'boolean'
    || !Array.isArray(posture.egress_rules)
    || !Array.isArray(posture.credential_names)
    || !Array.isArray(posture.binding_names)
  ) {
    return malformedRecord(endpoint);
  }
  return posture;
}

export async function adminDescribeSandboxIdleAction(
  backend?: string
): Promise<AdminSandboxIdleAction> {
  return send((wire) => client.GET('/api/v1/admin/sandbox-idle-action', {
    ...wire,
    // Whether a box can be parked rather than destroyed is the backend's property,
    // so an environment that names its own backend must be answered about that one.
    params: { query: undeclaredQuery({ backend }) },
  })) as Promise<AdminSandboxIdleAction>;
}

// ── Agent deployments: schedules and external trigger bindings ──────────────
export async function listMcpClientTokens(): Promise<McpClientToken[]> {
  const payload = await send((wire) => client.GET('/api/v1/mcp-tokens', wire));
  return expectList<McpClientToken>(payload?.tokens, '/mcp-tokens');
}

export async function issueMcpClientToken(payload: {
  name: string;
  scope: 'read' | 'converse';
}): Promise<IssuedMcpClientToken> {
  return send((wire) => client.POST('/api/v1/mcp-tokens', {
    ...wire,
    body: payload,
  })) as Promise<IssuedMcpClientToken>;
}

export async function revokeMcpClientToken(tokenId: string): Promise<void> {
  await send((wire) => client.DELETE('/api/v1/mcp-tokens/{token_id}', {
    ...wire,
    params: { path: { token_id: tokenId } },
  }));
}

export async function listAgentDeployments(agentId: string): Promise<AgentDeployment[]> {
  return expectList<AgentDeployment>(
    await send((wire) => client.GET('/api/v1/admin/agents/{agent_id}/deployments', {
      ...wire,
      params: { path: { agent_id: agentId } },
    })),
    '/admin/agents/{id}/deployments',
  );
}

export async function listDeployments(): Promise<AgentDeployment[]> {
  return expectList<AgentDeployment>(
    await send((wire) => client.GET('/api/v1/admin/deployments', wire)),
    '/admin/deployments',
  );
}

export async function listPersonalSchedules(): Promise<PersonalSchedule[]> {
  return expectList<PersonalSchedule>(
    await send((wire) => client.GET('/api/v1/me/schedules', wire)),
    '/me/schedules',
  );
}

export async function updatePersonalSchedule(
  deploymentId: string,
  patch: Partial<Pick<PersonalSchedule, 'name' | 'prompt_prefix' | 'schedule' | 'enabled'>>,
): Promise<PersonalSchedule> {
  return await send((wire) => client.PUT('/api/v1/me/schedules/{deployment_id}', {
    ...wire,
    params: { path: { deployment_id: deploymentId } },
    body: patch,
  })) as unknown as PersonalSchedule;
}

export async function deletePersonalSchedule(deploymentId: string): Promise<void> {
  await send((wire) => client.DELETE('/api/v1/me/schedules/{deployment_id}', {
    ...wire,
    params: { path: { deployment_id: deploymentId } },
  }));
}

export async function listPersonalScheduleRuns(deploymentId: string): Promise<DeploymentRun[]> {
  return expectList<DeploymentRun>(
    await send((wire) => client.GET('/api/v1/me/schedules/{deployment_id}/runs', {
      ...wire,
      params: { path: { deployment_id: deploymentId }, query: { limit: 50 } },
    })),
    '/me/schedules/{deployment_id}/runs',
  );
}

export async function triggerPersonalSchedule(deploymentId: string): Promise<DeploymentRun> {
  return send((wire) => client.POST('/api/v1/me/schedules/{deployment_id}/runs', {
    ...wire,
    params: { path: { deployment_id: deploymentId } },
  })) as Promise<DeploymentRun>;
}

export async function replayPersonalScheduleRun(deploymentId: string, runId: string): Promise<DeploymentRun> {
  return send((wire) => client.POST('/api/v1/me/schedules/{deployment_id}/runs/{run_id}/replay', {
    ...wire,
    params: { path: { deployment_id: deploymentId, run_id: runId } },
  })) as Promise<DeploymentRun>;
}

export async function listChannelProviders(): Promise<ChannelProviderDescriptor[]> {
  return expectList<ChannelProviderDescriptor>(
    await send((wire) => client.GET('/api/v1/channel-providers', { ...wire })),
    '/channel-providers',
  );
}

type DeploymentPayload = {
  scene: string;
  name?: string;
  prompt_prefix?: string;
  secret?: string;
  execution_mode?: ChannelExecutionMode;
  execution_user_id?: string;
  allowed_participants?: string[];
  attention_policy?: 'all' | 'mentions' | 'participated' | 'proactive';
  response_instruction?: string | null;
  channel_config?: Record<string, unknown>;
  credentials?: Record<string, unknown>;
  schedule?: { cron: string; timezone: string };
};

type DeploymentPatch = Partial<Omit<DeploymentPayload, 'secret'>> & { enabled?: boolean };

/** The Agent or Assistant a Deployment belongs to; an Assistant's are owner routes. */
export type DeploymentOwner = Pick<AgentDeployment, 'agent_id' | 'assistant_id'>;

function deploymentOwner(owner: DeploymentOwner):
  | { kind: 'assistant'; id: string }
  | { kind: 'agent'; id: string } {
  if (owner.assistant_id) return { kind: 'assistant', id: owner.assistant_id };
  if (owner.agent_id) return { kind: 'agent', id: owner.agent_id };
  throw new Error('deployment names no Agent or Assistant');
}

export async function createAgentDeployment(
  agentId: string,
  payload: DeploymentPayload,
): Promise<AgentDeployment> {
  return send((wire) => client.POST('/api/v1/admin/agents/{agent_id}/deployments', {
    ...wire,
    params: { path: { agent_id: agentId } },
    body: undeclared(payload),
  })) as Promise<AgentDeployment>;
}

export async function createAssistantDeployment(
  assistantId: string,
  payload: DeploymentPayload,
): Promise<AgentDeployment> {
  return send((wire) => client.POST('/api/v1/assistants/{assistant_id}/deployments', {
    ...wire,
    params: { path: { assistant_id: assistantId } },
    body: undeclared(payload),
  })) as Promise<AgentDeployment>;
}

export async function updateDeployment(
  owner: DeploymentOwner,
  deploymentId: string,
  patch: DeploymentPatch,
): Promise<AgentDeployment> {
  const target = deploymentOwner(owner);
  if (target.kind === 'assistant') {
    return send((wire) => client.PUT('/api/v1/assistants/{assistant_id}/deployments/{deployment_id}', {
      ...wire,
      params: { path: { assistant_id: target.id, deployment_id: deploymentId } },
      body: undeclared(patch),
    })) as Promise<AgentDeployment>;
  }
  return send((wire) => client.PUT('/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}', {
    ...wire,
    params: { path: { agent_id: target.id, deployment_id: deploymentId } },
    body: undeclared(patch),
  })) as Promise<AgentDeployment>;
}

/** The participants an Agent trigger runs as their own linked accounts. */
export async function listDeploymentAccountLinks(
  agentId: string,
  deploymentId: string,
): Promise<ChannelAccountLink[]> {
  return expectList<ChannelAccountLink>(
    await send((wire) => client.GET('/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/account-links', {
      ...wire,
      params: { path: { agent_id: agentId, deployment_id: deploymentId } },
    })),
    '/admin/agents/{agent_id}/deployments/{deployment_id}/account-links',
  );
}

export async function linkDeploymentAccount(
  agentId: string,
  deploymentId: string,
  link: { participant: string; user_id: string },
): Promise<ChannelAccountLink> {
  return send((wire) => client.PUT('/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/account-links', {
    ...wire,
    params: { path: { agent_id: agentId, deployment_id: deploymentId } },
    body: undeclared(link),
  })) as Promise<ChannelAccountLink>;
}

export async function unlinkDeploymentAccount(
  agentId: string,
  deploymentId: string,
  participant: string,
): Promise<void> {
  await send((wire) => client.DELETE('/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/account-links', {
    ...wire,
    params: { path: { agent_id: agentId, deployment_id: deploymentId }, query: { participant } },
  }));
}

export async function listDeploymentRuns(
  owner: DeploymentOwner,
  deploymentId: string,
  limit = 50,
): Promise<DeploymentRun[]> {
  const target = deploymentOwner(owner);
  if (target.kind === 'assistant') {
    return expectList<DeploymentRun>(
      await send((wire) => client.GET('/api/v1/assistants/{assistant_id}/deployments/{deployment_id}/runs', {
        ...wire,
        params: { path: { assistant_id: target.id, deployment_id: deploymentId }, query: { limit } },
      })),
      '/assistants/{assistant_id}/deployments/{deployment_id}/runs',
    );
  }
  return expectList<DeploymentRun>(
    await send((wire) => client.GET('/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/runs', {
      ...wire,
      params: { path: { agent_id: target.id, deployment_id: deploymentId }, query: { limit } },
    })),
    '/admin/agents/{agent_id}/deployments/{deployment_id}/runs',
  );
}

export async function triggerDeploymentRun(
  owner: DeploymentOwner,
  deploymentId: string,
): Promise<DeploymentRun> {
  const target = deploymentOwner(owner);
  if (target.kind === 'assistant') {
    return send((wire) => client.POST('/api/v1/assistants/{assistant_id}/deployments/{deployment_id}/runs', {
      ...wire,
      params: { path: { assistant_id: target.id, deployment_id: deploymentId } },
    })) as Promise<DeploymentRun>;
  }
  return send((wire) => client.POST('/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/runs', {
    ...wire,
    params: { path: { agent_id: target.id, deployment_id: deploymentId } },
  })) as Promise<DeploymentRun>;
}

export async function replayDeploymentRun(
  owner: DeploymentOwner,
  deploymentId: string,
  runId: string,
): Promise<DeploymentRun> {
  const target = deploymentOwner(owner);
  if (target.kind === 'assistant') {
    return send((wire) => client.POST('/api/v1/assistants/{assistant_id}/deployments/{deployment_id}/runs/{run_id}/replay', {
      ...wire,
      params: { path: { assistant_id: target.id, deployment_id: deploymentId, run_id: runId } },
    })) as Promise<DeploymentRun>;
  }
  return send((wire) => client.POST('/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/runs/{run_id}/replay', {
    ...wire,
    params: { path: { agent_id: target.id, deployment_id: deploymentId, run_id: runId } },
  })) as Promise<DeploymentRun>;
}

export async function deleteDeployment(
  owner: DeploymentOwner,
  deploymentId: string,
): Promise<Record<string, unknown>> {
  const target = deploymentOwner(owner);
  if (target.kind === 'assistant') {
    return send((wire) => client.DELETE('/api/v1/assistants/{assistant_id}/deployments/{deployment_id}', {
      ...wire,
      params: { path: { assistant_id: target.id, deployment_id: deploymentId } },
    }));
  }
  return send((wire) => client.DELETE('/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}', {
    ...wire,
    params: { path: { agent_id: target.id, deployment_id: deploymentId } },
  }));
}

// ── Connectors: work apps an Assistant uses through the connector hub ──
export async function listConnectors(): Promise<Connector[]> {
  return expectList<Connector>(
    await send((wire) => client.GET('/api/v1/connectors', wire)),
    '/connectors',
  );
}

export async function authorizeConnector(connectorId: string): Promise<{ authorize_url: string }> {
  return send((wire) => client.POST('/api/v1/connectors/{connector_id}/authorize', {
    ...wire,
    params: { path: { connector_id: connectorId } },
  })) as Promise<{ authorize_url: string }>;
}

/** Turn a just-connected app on in the caller's Assistants that have not turned it off. */
export async function offerConnectorInAssistants(
  connectorId: string,
): Promise<{ connector_id: string; assistant_ids: string[] }> {
  return send((wire) => client.POST('/api/v1/connectors/{connector_id}/assistants', {
    ...wire,
    params: { path: { connector_id: connectorId } },
  })) as Promise<{ connector_id: string; assistant_ids: string[] }>;
}

export async function disconnectConnector(connectorId: string): Promise<void> {
  await send((wire) => client.DELETE('/api/v1/connectors/{connector_id}/connection', {
    ...wire,
    params: { path: { connector_id: connectorId } },
  }));
}

export async function listAssistantConnectors(assistantId: string): Promise<string[]> {
  const state = (await send((wire) => client.GET('/api/v1/assistants/{assistant_id}/connectors', {
    ...wire,
    params: { path: { assistant_id: assistantId } },
  }))) as { enabled?: string[] };
  return state.enabled ?? [];
}

export async function setAssistantConnector(
  assistantId: string,
  connectorId: string,
  enabled: boolean,
): Promise<string[]> {
  const path = { assistant_id: assistantId, connector_id: connectorId };
  const state = (await send((wire) =>
    enabled
      ? client.PUT('/api/v1/assistants/{assistant_id}/connectors/{connector_id}', {
          ...wire,
          params: { path },
        })
      : client.DELETE('/api/v1/assistants/{assistant_id}/connectors/{connector_id}', {
          ...wire,
          params: { path },
        }),
  )) as { enabled?: string[] };
  return state.enabled ?? [];
}

export async function listAgentConnectors(agentId: string): Promise<string[]> {
  const state = (await send((wire) => client.GET('/api/v1/agents/{agent_id}/connectors', {
    ...wire,
    params: { path: { agent_id: agentId } },
  }))) as { enabled?: string[] };
  return state.enabled ?? [];
}

export async function setAgentConnector(
  agentId: string,
  connectorId: string,
  enabled: boolean,
): Promise<string[]> {
  const path = { agent_id: agentId, connector_id: connectorId };
  const state = (await send((wire) =>
    enabled
      ? client.PUT('/api/v1/agents/{agent_id}/connectors/{connector_id}', { ...wire, params: { path } })
      : client.DELETE('/api/v1/agents/{agent_id}/connectors/{connector_id}', { ...wire, params: { path } }),
  )) as { enabled?: string[] };
  return state.enabled ?? [];
}

export async function listAdminConnectors(): Promise<Connector[]> {
  return expectList<Connector>(
    await send((wire) => client.GET('/api/v1/admin/connectors', wire)),
    '/admin/connectors',
  );
}

export async function configureConnector(
  connectorId: string,
  payload: { client_id?: string; client_secret?: string },
): Promise<Connector> {
  return send((wire) => client.PUT('/api/v1/admin/connectors/{connector_id}', {
    ...wire,
    params: { path: { connector_id: connectorId } },
    body: payload,
  })) as Promise<Connector>;
}

export async function verifyConnector(connectorId: string): Promise<{ authorize_url: string }> {
  return send((wire) => client.POST('/api/v1/admin/connectors/{connector_id}/verify', {
    ...wire,
    params: { path: { connector_id: connectorId } },
  })) as Promise<{ authorize_url: string }>;
}

export async function publishConnector(connectorId: string): Promise<Connector> {
  return send((wire) => client.POST('/api/v1/admin/connectors/{connector_id}/publish', {
    ...wire,
    params: { path: { connector_id: connectorId } },
  })) as Promise<Connector>;
}

export async function removeConnector(connectorId: string): Promise<void> {
  await send((wire) => client.DELETE('/api/v1/admin/connectors/{connector_id}', {
    ...wire,
    params: { path: { connector_id: connectorId } },
  }));
}

export async function listSessions(): Promise<SessionRecord[]> {
  return expectList<SessionRecord>(await send((wire) => client.GET('/api/v1/sessions', wire)), '/sessions');
}

export async function listSessionsPage(cursor?: string | null, limit = 50): Promise<SessionListPage> {
  return expectPage<SessionListPage>(
    await send((wire) => client.GET('/api/v1/sessions', {
      ...wire,
      params: { query: undeclaredQuery({ page: 1, limit, cursor }) },
    })),
    '/sessions',
    'sessions',
  );
}

/** Record that the owner has read a conversation up to now. */
export async function markSessionRead(sessionId: string): Promise<{ session_id: string; read_at: string }> {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/read', {
    ...wire,
    params: { path: { session_id: sessionId } },
  })) as Promise<{ session_id: string; read_at: string }>;
}

export async function getSession(sessionId: string, init?: RequestInit): Promise<SessionRecord> {
  return send((wire) => client.GET('/api/v1/sessions/{session_id}', {
    ...wire,
    params: { path: { session_id: sessionId } },
    signal: init?.signal ?? undefined,
  })) as Promise<SessionRecord>;
}

/**
 * One page of a session's history as AI SDK UI messages, oldest first.
 *
 * A user message is an accepted input (id = its `client_message_id`); an
 * assistant message is one response (id = the response id), its parts folded
 * the way the AI SDK folds them live. Ended responses carry their work folded
 * behind `data-process-block` parts. `next_cursor` asks for the next older
 * page and pins it to the output position of the first one. The first page
 * also carries `output_seq`, the position its parts were read through and the
 * one the session's stream is followed from, and the interaction waiting on
 * the owner.
 */
export interface HistoryBlockPage {
  messages: UIMessage[];
  has_more: boolean;
  output_seq?: number | null;
  pending_interaction?: PendingInteraction | null;
  paging_mode: 'blocks';
  next_cursor?: string | null;
  block_count: number;
}

/** The parts one folded header stands for, as one assistant message. */
export interface HistoryBlockDetails {
  messages: UIMessage[];
  has_more: boolean;
}

export interface SessionChildRun {
  child_run_id: string;
  engine_kind: string;
  depth: number;
  engine_event?: string | null;
  engine_status?: string | null;
  engine_reason?: string | null;
  closed: boolean;
  active: boolean;
  operations: string[];
  tool_call_ids: string[];
  parent_child_run_id?: string | null;
  description?: string | null;
  task_type?: string | null;
  last_tool_name?: string | null;
  summary?: string | null;
  model?: string | null;
  usage?: { total_tokens?: number; tool_uses?: number; duration_ms?: number } | null;
}

export interface SessionChildRunPage {
  session_id: string;
  child_runs: SessionChildRun[];
}

export interface ChildRunTranscriptMessage {
  role: 'assistant' | 'user';
  content: Array<Record<string, unknown>>;
  message_id?: string;
}

export interface ChildRunMessagePage {
  session_id: string;
  child_run_id: string;
  messages: ChildRunTranscriptMessage[];
}

/** One ended response and every `data-file-changes` part it recorded. */
export interface SessionFileChangeResponse {
  response_id: string;
  parts: Array<{ type: 'data-file-changes'; id?: string; data: import('./types').FileChangesResult }>;
}

/** The responses that ended after `after_seq`, through `through_seq` (the next `after_seq`). */
export interface SessionFileChanges {
  through_seq: number;
  responses: SessionFileChangeResponse[];
}

export async function getSessionFileChanges(
  sessionId: string, afterSeq: number, signal?: AbortSignal,
): Promise<SessionFileChanges> {
  return asDeclared<SessionFileChanges>(await send((wire) => client.GET('/api/v1/sessions/{session_id}/file-changes', {
    ...wire,
    params: { path: { session_id: sessionId }, query: { after_seq: afterSeq } },
    signal,
  })));
}

/**
 * One page of the conversation with each ended response's work folded.
 *
 * Omitting `before` reads the newest page; `next_cursor` from a page is the
 * `before` for the one older than it.
 */
export async function getHistoryBlocks(
  sessionId: string,
  before?: string,
  limit = 50,
  init?: RequestInit,
): Promise<HistoryBlockPage> {
  return asDeclared<HistoryBlockPage>(await send((wire) => client.GET('/api/v1/sessions/{session_id}/history-blocks', {
    ...wire,
    params: {
      path: { session_id: sessionId },
      query: undeclaredQuery({ limit, before }),
    },
    signal: init?.signal ?? undefined,
  })));
}

/**
 * The parts one folded header stands for, read at the header's own cursor.
 */
export async function getHistoryBlockDetails(
  sessionId: string,
  blockId: string,
  cursor: string,
  init?: RequestInit,
): Promise<HistoryBlockDetails> {
  return asDeclared<HistoryBlockDetails>(await send((wire) => client.GET('/api/v1/sessions/{session_id}/history-blocks/{block_id}', {
    ...wire,
    params: {
      path: { session_id: sessionId, block_id: blockId },
      query: undeclaredQuery({ cursor }),
    },
    signal: init?.signal ?? undefined,
  })));
}

/**
 * Ask for the label of one response's folded work.
 *
 * The server answers with the stored label when there is one, so a reader who
 * arrives after the response ended gets the label it already has rather than
 * a second one. `retry` reopens a label whose generation failed.
 */
export async function generateProcessSummary(
  sessionId: string,
  responseId: string,
  retry = false,
): Promise<ProcessSummaryState> {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/messages/{message_id}/process-summary', {
    ...wire,
    params: {
      path: { session_id: sessionId, message_id: responseId },
      query: undeclaredQuery({ retry: retry ? 'true' : '' }),
    },
  }));
}

export async function listSessionChildRuns(
  sessionId: string,
  init?: RequestInit,
): Promise<SessionChildRunPage> {
  return asDeclared<SessionChildRunPage>(await send((wire) => client.GET('/api/v1/sessions/{session_id}/child-runs', {
    ...wire,
    params: { path: { session_id: sessionId } },
    signal: init?.signal ?? undefined,
  })));
}

export async function getSessionChildRunMessages(
  sessionId: string,
  childRunId: string,
  init?: RequestInit,
): Promise<ChildRunMessagePage> {
  return asDeclared<ChildRunMessagePage>(await send((wire) => client.GET('/api/v1/sessions/{session_id}/child-runs/{child_run_id}/messages', {
    ...wire,
    params: { path: { session_id: sessionId, child_run_id: childRunId } },
    signal: init?.signal ?? undefined,
  })));
}

// ── Session sharing ───────────────────────────────────────────────────────
export interface ShareConfig {
  enabled: boolean;
  token: string;
  expires_at?: string | null;
  allow_download: boolean;
  created_at?: string | null;
}

export async function getSessionShare(sessionId: string): Promise<ShareConfig> {
  return send((wire) => client.GET('/api/v1/sessions/{session_id}/share', {
    ...wire,
    params: { path: { session_id: sessionId } },
  })) as Promise<ShareConfig>;
}

export async function createSessionShare(
  sessionId: string,
  opts: { expires_in_seconds?: number | null; allow_download?: boolean } = {},
): Promise<ShareConfig> {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/share', {
    ...wire,
    params: { path: { session_id: sessionId } },
    body: undeclared(opts),
  })) as Promise<ShareConfig>;
}

export async function revokeSessionShare(sessionId: string): Promise<{ enabled: boolean }> {
  return send((wire) => client.DELETE('/api/v1/sessions/{session_id}/share', {
    ...wire,
    params: { path: { session_id: sessionId } },
  })) as Promise<{ enabled: boolean }>;
}

/**
 * What a share link shows around its transcript: the title, the interaction
 * the conversation waits on (without the tool input it asks about), and
 * whether files may be downloaded.
 */
export interface SharedSession {
  title?: string | null;
  pending_interaction?: PendingInteraction | null;
  share_allow_download: boolean;
}

// Viewer side: the share token grants read-only access without a login.
export async function getSharedSession(token: string): Promise<SharedSession> {
  return asDeclared<SharedSession>(await send((wire) => client.GET('/api/v1/share/{token}', {
    ...wire,
    params: { path: { token } },
  })));
}

export async function listSharedFiles(
  token: string,
  path?: string,
): Promise<SessionFilesListResponse> {
  return send((wire) => client.GET('/api/v1/share/{token}/files/list', {
    ...wire,
    params: { path: { token }, query: undeclaredQuery({ path }) },
  })) as Promise<SessionFilesListResponse>;
}

/** The shared conversation's history; a share link has no stream, so its first page carries no `output_seq`. */
export async function getSharedHistoryBlocks(
  token: string, before?: string, limit = 50, init?: RequestInit,
): Promise<HistoryBlockPage> {
  return asDeclared<HistoryBlockPage>(await send((wire) => client.GET('/api/v1/share/{token}/history-blocks', {
    ...wire,
    params: { path: { token }, query: { limit, before } },
    signal: init?.signal ?? undefined,
  })));
}

export async function getSharedHistoryBlockDetails(
  token: string, blockId: string, cursor: string, init?: RequestInit,
): Promise<HistoryBlockDetails> {
  return asDeclared<HistoryBlockDetails>(await send((wire) => client.GET('/api/v1/share/{token}/history-blocks/{block_id}', {
    ...wire,
    params: { path: { token, block_id: blockId }, query: { cursor } },
    signal: init?.signal ?? undefined,
  })));
}

export function buildSharedFileDownloadUrl(token: string, path: string): string {
  const params = new URLSearchParams({ path });
  return buildApiUrl(`/api/v1/share/${encodeURIComponent(token)}/files/download?${params}`);
}

export async function setSessionPermissionMode(
  sessionId: string,
  permissionMode: PermissionMode,
): Promise<{ session_id: string; permission_mode: PermissionMode; applied: boolean }> {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/permission-mode', {
    ...wire,
    params: { path: { session_id: sessionId } },
    body: { permission_mode: permissionMode },
  })) as Promise<{ session_id: string; permission_mode: PermissionMode; applied: boolean }>;
}

export type InteractionAnswerReceipt = Schemas['InteractionAnswerReceipt'];

/**
 * Answer an interaction the runtime opened. The answer is stored for the
 * runtime and the response continues on the session's stream.
 *
 * The outcome of a request that outlives its deadline is unknown, so the
 * deadline is reported as that rather than as a refusal.
 */
export async function answerInteraction(
  sessionId: string,
  interactionId: string,
  answer: InteractionResponse,
): Promise<InteractionAnswerReceipt> {
  const controller = new AbortController();
  const timeoutId = globalThis.setTimeout(() => controller.abort(), 15_000);
  try {
    return await send((wire) => client.POST('/api/v1/sessions/{session_id}/interactions/{interaction_id}/answer', {
      ...wire,
      params: { path: { session_id: sessionId, interaction_id: interactionId } },
      body: undeclared({ answer }),
      signal: controller.signal,
    }));
  } catch (error) {
    // Headers can arrive before an aborted body read is shaped by send(). The
    // caller's deadline, not the resulting error class, owns this unknown outcome.
    if (controller.signal.aborted) {
      throw new Error(`INTERACTION_RESPONSE_TIMEOUT: ${i18n.t('chat:interaction.response_timeout')}`);
    }
    throw error;
  }
  finally {
    globalThis.clearTimeout(timeoutId);
  }
}

export type InputReceipt = Schemas['InputReceipt'];

/**
 * One input's content: its text, or the engine's content blocks when the
 * message carries more than text.
 */
export type InputContent =
  | string
  | Array<
      | { type: 'text'; text: string }
      | {
          type: 'image';
          source: { type: 'base64'; media_type: string; data: string };
        }
    >;

/**
 * Send a message. The receipt says the session accepted it; its response
 * arrives on the session's stream.
 *
 * `client_message_id` is the message's identity: the server answers a repeat
 * of the same id with the earlier receipt (`duplicate`), so replaying a
 * request whose answer was lost cannot accept the message twice.
 */
export async function sendInput(
  sessionId: string,
  body: {
    content: InputContent;
    client_message_id: string;
  },
): Promise<InputReceipt> {
  return send(
    (wire) => client.POST('/api/v1/sessions/{session_id}/inputs', {
      ...wire,
      params: { path: { session_id: sessionId } },
      body: undeclared({
        content: body.content,
        client_message_id: body.client_message_id,
      }),
    }),
    { mutationReplay: 'idempotent' },
  );
}

/** The session's stream after `afterSeq`: what it missed, then live, through the end of one response. */
export function buildSessionStreamUrl(sessionId: string, afterSeq: number): string {
  const params = new URLSearchParams({ after_seq: String(afterSeq) });
  return buildApiUrl(`/api/v1/sessions/${encodeURIComponent(sessionId)}/stream?${params}`);
}

function isEventStream(response: Response): boolean {
  const ct = String(response.headers.get('content-type') ?? '').toLowerCase();
  return ct.includes('text/event-stream');
}

function findSseDelimiter(buffer: string): { index: number; length: number } | null {
  const idxLf = buffer.indexOf('\n\n');
  const idxCrlf = buffer.indexOf('\r\n\r\n');
  if (idxLf < 0 && idxCrlf < 0) return null;
  if (idxLf < 0) return { index: idxCrlf, length: 4 };
  if (idxCrlf < 0) return { index: idxLf, length: 2 };
  return idxLf < idxCrlf ? { index: idxLf, length: 2 } : { index: idxCrlf, length: 4 };
}

async function parseErrorFromResponse(response: Response): Promise<never> {
  // Try JSON first (standard API error format), fall back to text/html.
  let rawPayload: unknown = null;
  try {
    rawPayload = await response.json();
  } catch {
    try {
      rawPayload = await response.text();
    } catch {
      rawPayload = null;
    }
  }

  if (typeof rawPayload === 'object' && rawPayload !== null) {
    const obj = rawPayload as Record<string, unknown>;
    const code = String(obj.code ?? 'UNKNOWN_ERROR');
    const message = String(obj.message ?? `HTTP ${response.status}`);
    throw new Error(`${code}: ${message}`);
  }

  const text = typeof rawPayload === 'string' ? rawPayload : '';
  const preview = text.trim() ? text.trim().slice(0, 200) : `HTTP ${response.status}`;
  throw new Error(`STREAM_ERROR: ${preview}`);
}

async function streamSseBody<T>(response: Response, onEvent: (event: T) => void): Promise<void> {
  if (!response.body) {
    throw new Error('STREAM_ERROR: empty response body');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      while (true) {
        const delim = findSseDelimiter(buffer);
        if (!delim) break;

        const rawEvent = buffer.slice(0, delim.index);
        buffer = buffer.slice(delim.index + delim.length);

        if (!rawEvent.trim() || rawEvent.trimStart().startsWith(':')) continue;

        const lines = rawEvent.split(/\r?\n/);
        const dataLines: string[] = [];
        for (const line of lines) {
          if (line.startsWith('data:')) {
            dataLines.push(line.slice(5).trimStart());
          }
        }

        if (dataLines.length === 0) continue;
        const data = dataLines.join('\n');
        const event = JSON.parse(data) as T;
        onEvent(event);
      }
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      // Ignore.
    }
  }
}

export type InterruptReceipt = Schemas['InterruptReceipt'];

/**
 * Ask the conversation's runtime to stop the response it is producing.
 * `accepted` means a reader will pass the interrupt on; `idle` that nothing ran.
 */
export async function interruptSession(sessionId: string): Promise<InterruptReceipt> {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/interrupt', {
    ...wire,
    params: { path: { session_id: sessionId } },
  }));
}

export async function recoverSession(sessionId: string): Promise<SessionRecord> {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/recover', {
    ...wire,
    params: { path: { session_id: sessionId } },
  })) as Promise<SessionRecord>;
}

export async function stopSessionChildRun(
  sessionId: string,
  childRunId: string,
): Promise<{ status: string }> {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/child-runs/{child_run_id}/stop', {
    ...wire,
    params: { path: { session_id: sessionId, child_run_id: childRunId } },
  })) as Promise<{ status: string }>;
}

export async function terminateSandbox(sessionId: string): Promise<SandboxTerminateResult> {
  // Sandbox termination can legitimately take tens of seconds while the
  // backend tears down the remote runtime. A short client-side abort produces
  // a false UI failure even when the server successfully completes termination.
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/sandbox/terminate', {
    ...wire,
    params: { path: { session_id: sessionId } },
  })) as Promise<SandboxTerminateResult>;
}

export async function endConversation(sessionId: string): Promise<ConversationEndResult> {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/conversation/end', {
    ...wire,
    params: { path: { session_id: sessionId } },
  })) as Promise<ConversationEndResult>;
}

export async function getWebshellUrl(sessionId: string): Promise<WebshellResult> {
  return send((wire) => client.GET('/api/v1/sessions/{session_id}/webshell', {
    ...wire,
    params: { path: { session_id: sessionId } },
  })) as Promise<WebshellResult>;
}

export async function deleteSession(sessionId: string): Promise<{ deleted: boolean }> {
  return send((wire) => client.DELETE('/api/v1/sessions/{session_id}', {
    ...wire,
    params: { path: { session_id: sessionId } },
  })) as Promise<{ deleted: boolean }>;
}

export async function archiveSession(sessionId: string): Promise<{ archived: boolean }> {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/archive', {
    ...wire,
    params: { path: { session_id: sessionId } },
  })) as Promise<{ archived: boolean }>;
}

export async function listSessionFiles(
  sessionId: string,
  payload: SessionFilesListRequest = {},
): Promise<SessionFilesListResponse> {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/files/list', {
    ...wire,
    params: { path: { session_id: sessionId } },
    body: payload,
  })) as Promise<SessionFilesListResponse>;
}

export async function uploadSessionFiles(
  sessionId: string,
  payload: SessionFilesUploadRequest,
): Promise<SessionFilesMutationResult> {
  const formData = new FormData();
  formData.append('path', payload.path);
  payload.files.forEach((file) => {
    formData.append('files', file);
  });

  return send((wire) => client.POST('/api/v1/sessions/{session_id}/files/upload', {
    ...wire,
    params: { path: { session_id: sessionId } },
    body: formData as never,
  }), {}, formData) as Promise<SessionFilesMutationResult>;
}

export async function getSpeechInputOptions(sessionId: string, signal?: AbortSignal) {
  return send((wire) => client.GET('/api/v1/sessions/{session_id}/speech-input', {
    ...wire,
    params: { path: { session_id: sessionId } },
    signal,
  }));
}

export async function transcribeSpeechInput(
  sessionId: string,
  model: string,
  audio: Blob,
  signal: AbortSignal,
): Promise<string> {
  const formData = new FormData();
  formData.append('model', model);
  formData.append('file', audio, 'recording');
  const result = await send((wire) => client.POST('/api/v1/sessions/{session_id}/speech-input', {
    ...wire,
    params: { path: { session_id: sessionId } },
    body: formData as never,
    signal,
  }), {}, formData);
  return result.text;
}

export async function createSessionDirectory(
  sessionId: string,
  payload: SessionFilesMkdirRequest,
): Promise<SessionFilesMutationResult> {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/files/mkdir', {
    ...wire,
    params: { path: { session_id: sessionId } },
    body: payload,
  })) as Promise<SessionFilesMutationResult>;
}

export async function moveSessionFiles(
  sessionId: string,
  payload: SessionFilesMoveRequest,
): Promise<SessionFilesMutationResult> {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/files/move', {
    ...wire,
    params: { path: { session_id: sessionId } },
    body: payload,
  })) as Promise<SessionFilesMutationResult>;
}

export async function deleteSessionFiles(
  sessionId: string,
  payload: SessionFilesDeleteRequest,
): Promise<SessionFilesMutationResult> {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/files/delete', {
    ...wire,
    params: { path: { session_id: sessionId } },
    body: payload,
  }), {
    // The server defines this route as convergence on "all targets absent", so
    // replay is safe when the browser loses the first response.
    mutationReplay: 'convergent',
  }) as Promise<SessionFilesMutationResult>;
}

export function buildSessionFileDownloadUrl(sessionId: string, path: string): string {
  const params = new URLSearchParams({ path });
  return buildApiUrl(`/api/v1/sessions/${sessionId}/files/download?${params.toString()}`);
}

export async function openSessionBrowser(sessionId: string, signal?: AbortSignal) {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/browser', {
    ...wire,
    signal,
    params: { path: { session_id: sessionId } },
  })) as Promise<{ url: string; expires_at: string }>;
}

export async function previewSessionFile(sessionId: string, path: string, signal?: AbortSignal) {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/files/preview', {
    ...wire,
    signal,
    params: { path: { session_id: sessionId } },
    body: { path },
  })) as Promise<{ url: string; expires_at: string }>;
}

/** Bound the reader's memory even if the file grows after its directory listing. */
export async function readSessionFile(
  sessionId: string, path: string, limit: number, signal: AbortSignal,
): Promise<Blob> {
  const response = await fetchWithNetworkRecovery(buildSessionFileDownloadUrl(sessionId, path), {
    credentials: 'include', signal, cache: 'no-store',
  });
  if (!response.ok) {
    const payload = await response.json();
    throw new Error(payload.message || `HTTP ${response.status}`);
  }
  const reader = response.body?.getReader();
  if (!reader) throw new Error(i18n.t('chat:files.preview_failed'));
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) throw new Error(i18n.t('chat:files.preview_too_large'));
      chunks.push(new Uint8Array(value));
    }
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
  return new Blob(chunks);
}

/**
 * One event of a terminal command: its `ack` (with the `command_id`), output,
 * the directory the shell ended in, and a final `exit`. Events read back from
 * the command's record carry their `seq`.
 */
export interface TerminalEvent {
  type: 'ack' | 'stdout' | 'stderr' | 'cwd' | 'exit';
  seq?: number;
  command_id?: string;
  text?: string;
  path?: string;
  command?: string;
  working_directory?: string | null;
  /** `null` when the platform could not observe the command's status. */
  exit_code?: number | null;
}

/**
 * Run one command in the session's terminal, its events read as they happen.
 *
 * Read on a raw fetch, not the generated client: the client parses a response
 * body before handing it back, and this one is an event stream that has to be
 * consumed frame by frame while the command is still running. Closing the
 * stream leaves the command running; `followTerminalCommand` reads it again.
 */
export async function runTerminalCommandStream(
  sessionId: string,
  command: string,
  onEvent: (event: TerminalEvent) => void,
  signal?: AbortSignal,
  cwd?: string
): Promise<void> {
  const response = await consoleFetch(buildApiUrl(`/api/v1/sessions/${sessionId}/terminal/stream`), {
    method: 'POST',
    credentials: 'include',
    headers: {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ command, working_directory: cwd }),
    signal,
  });

  if (!isEventStream(response)) {
    await parseErrorFromResponse(response);
  }

  await streamSseBody<TerminalEvent>(response, onEvent);
}

/**
 * Follow a terminal command again: its recorded events after `afterSeq`, then
 * live, through its `exit`. `afterSeq` -1 reads it from its first event.
 *
 * Without `commandId` it follows the command the terminal runs now, and
 * answers `false` when none runs.
 */
export async function followTerminalCommand(
  sessionId: string,
  target: { commandId?: string; afterSeq: number },
  onEvent: (event: TerminalEvent) => void,
  signal?: AbortSignal,
): Promise<boolean> {
  const params = new URLSearchParams({ after_seq: String(target.afterSeq) });
  if (target.commandId) params.set('command_id', target.commandId);
  const response = await consoleFetch(buildApiUrl(`/api/v1/sessions/${sessionId}/terminal/stream?${params}`), {
    method: 'GET',
    credentials: 'include',
    headers: { Accept: 'text/event-stream' },
    signal,
  });
  if (response.status === 204) return false;
  if (!isEventStream(response)) {
    await parseErrorFromResponse(response);
  }
  await streamSseBody<TerminalEvent>(response, onEvent);
  return true;
}

export type TerminalInterruptResult = Schemas['TerminalInterruptResult'];

/**
 * Stop the command the session's terminal runs. Answers once the command has
 * ended (`completed`), or `idle` when none ran.
 */
export async function interruptTerminal(sessionId: string): Promise<TerminalInterruptResult> {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/terminal/interrupt', {
    ...wire,
    params: { path: { session_id: sessionId } },
  }));
}

// The assistant module speaks to the same routes, through the same success
// envelope and the same error vocabulary, so it shares this client rather than
// keeping a second one. Two shapings of a refusal drift: one appends the
// response body as evidence and the other does not, and the same failure then
// reads differently depending on which page the reader is standing on.
export { client as apiClient, send as sendApi };

export async function getSessionUserAction(sessionId: string): Promise<SessionUserActionState> {
  return send((wire) => client.GET('/api/v1/sessions/{session_id}/user-action', {
    ...wire, params: { path: { session_id: sessionId } },
  })) as Promise<SessionUserActionState>;
}

export async function performSessionUserAction(sessionId: string, actionId: string): Promise<SessionUserActionState> {
  return send((wire) => client.POST('/api/v1/sessions/{session_id}/user-action', {
    ...wire, params: { path: { session_id: sessionId } }, body: { action_id: actionId },
  })) as Promise<SessionUserActionState>;
}
