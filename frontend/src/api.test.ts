import { describe, it, expect, beforeAll, afterEach, vi } from 'vitest';

import i18n from './i18n';
import type { FormSchema } from './types';
import {
  adminListSessions,
  adminReadSandboxDiagnostics,
  adminReadSandboxSecurity,
  appendTurnInput,
  createAgent,
  deleteSessionFiles,
  getCurrentUser,
  getAdminVaultCatalog,
  isRequestAbortError,
  revokeMcpClientToken,
  startAgentConversation,
  updateAgent,
  uploadSessionFiles,
} from './api';

// Behavioral tests for the send()/error-shaping chain in api.ts. None of
// normalizeLegacyApiError / appendStructuredApiErrorEvidence / compactApiErrorEvidence
// / buildUnexpectedResponseMessage are exported — they are private
// wire-formatting details — so they are exercised the way the app reaches
// them: through an exported call (getCurrentUser, the simplest GET) with
// global.fetch mocked to return representative (including malformed) payloads,
// and assertions on the resulting resolved value / thrown Error.
//
// This suite runs under this project's vitest `environment: 'node'` (see
// vitest.config.ts) — there is no `window`. requestPathForEvidence's
// `new URL(url, window.location.origin)` therefore always throws and falls
// back to the raw request path in its catch, which is what the assertions
// below encode; that equals the parsed pathname anyway, since every call here
// uses a root-relative, query-free URL.

beforeAll(async () => {
  await i18n.changeLanguage('en');
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetchResolved(response: Response): ReturnType<typeof vi.fn> {
  const mockFetch = vi.fn().mockResolvedValue(response);
  vi.stubGlobal('fetch', mockFetch);
  return mockFetch;
}

function stubFetchRejected(error: unknown): ReturnType<typeof vi.fn> {
  const mockFetch = vi.fn().mockRejectedValue(error);
  vi.stubGlobal('fetch', mockFetch);
  return mockFetch;
}

function stubFetchJson(body: unknown): ReturnType<typeof vi.fn> {
  const mockFetch = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse(body)));
  vi.stubGlobal('fetch', mockFetch);
  return mockFetch;
}

// The body reaches `fetch` as bytes, not as a JSON string: the generated client
// serializes into a `Request`, and the transport takes the body back off it as
// an ArrayBuffer so that replaying an upload resends binary unchanged. What
// these tests assert is the bytes sent, and that a replay sends them again;
// decoding is how the assertion reads them.
function sentBody(init: RequestInit): string {
  const body = init.body;
  if (body === undefined || body === null) return '';
  if (typeof body === 'string') return body;
  return new TextDecoder().decode(body as ArrayBuffer);
}

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
    ...init,
  });
}

const AGENT_SCHEMA: FormSchema = {
  version: 1,
  groups: [{ id: 'identity' }, { id: 'runtime' }],
  fields: [
    { key: 'name', type: 'string', group: 'identity' },
    { key: 'model', type: 'string', group: 'runtime' },
    { key: 'environment_name', type: 'env_ref', group: 'runtime' },
  ],
};

describe('request() success path', () => {
  it('resolves with payload.data and issues a same-origin, credentialed JSON request', async () => {
    const mockFetch = stubFetchResolved(
      jsonResponse({ code: 'OK', message: 'ok', data: { user_id: 'u1', display_name: 'Ada' } }),
    );

    await expect(getCurrentUser()).resolves.toEqual({ user_id: 'u1', display_name: 'Ada' });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/user/current');
    expect(init.credentials).toBe('include');
    expect((init.headers as Headers).get('content-type')).toBe('application/json');
  });

  it('is still treated as a failure when HTTP 200 carries a non-OK application code', async () => {
    stubFetchResolved(jsonResponse({ code: 'APP_FAIL', message: 'business rule violated', data: null }));
    await expect(getCurrentUser()).rejects.toThrow('APP_FAIL: business rule violated');
  });
});

describe('request() structured error envelope', () => {
  it('prefers error.user_message and appends category/detail/trace_id evidence exactly once each', async () => {
    stubFetchResolved(
      jsonResponse(
        {
          code: 'ERR',
          message: 'ignored top-level message',
          data: null,
          error: {
            code: 'SOME_CODE',
            category: 'validation',
            user_message: 'Something went wrong',
            debug_message: 'stack trace info',
            evidence: { trace_id: 'abc123' },
          },
        },
        { status: 400 },
      ),
    );

    await expect(getCurrentUser()).rejects.toThrow(
      'SOME_CODE: Something went wrong (category=validation; detail=stack trace info; trace_id=abc123)',
    );
  });

  it('keeps cleanup data actionable when a structured category is also present', async () => {
    stubFetchResolved(
      jsonResponse(
        {
          code: 'SESSION_SANDBOX_DESTRUCTION_UNCONFIRMED',
          message: 'ignored top-level message',
          data: {
            failed_operations: ['destroy sandbox'],
            sandbox_id: 'sandbox-1',
            destruction_outcome: 'UNCONFIRMED',
          },
          error: {
            code: 'SESSION_SANDBOX_DESTRUCTION_UNCONFIRMED',
            category: 'runtime.cleanup',
            retryable: true,
            owner: 'runtime',
            user_message: 'Sandbox destruction could not be confirmed; retry session deletion.',
          },
        },
        { status: 502 },
      ),
    );

    await expect(getCurrentUser()).rejects.toThrow(
      'SESSION_SANDBOX_DESTRUCTION_UNCONFIRMED: Sandbox destruction could not be confirmed; '
      + 'retry session deletion. (category=runtime.cleanup; '
      + 'data={"failed_operations":["destroy sandbox"],"sandbox_id":"sandbox-1",'
      + '"destruction_outcome":"UNCONFIRMED"})',
    );
  });

  it('normalizes a structured UNKNOWN_ERROR code to PLATFORM_SERVER_ERROR with a templated evidence message', async () => {
    stubFetchResolved(
      jsonResponse({ code: 'UNKNOWN_ERROR', message: 'BaseErrorCode.UNKNOWN_ERROR', data: null }, { status: 500 }),
    );

    await expect(getCurrentUser()).rejects.toThrow(
      'PLATFORM_SERVER_ERROR: The platform returned an unclassified error (HTTP 500, /api/v1/user/current). '
      + 'Check the backend logs to pin down the exact failure.',
    );
  });
});

describe('request() legacy err_message payloads', () => {
  it('splits a "CODE: message" legacy err_message into a typed code + message', async () => {
    stubFetchResolved(jsonResponse({ err_message: 'AUTH_FAILED: Invalid session token' }, { status: 401 }));
    await expect(getCurrentUser()).rejects.toThrow('AUTH_FAILED: Invalid session token');
  });
});

describe('request() malformed / non-JSON responses', () => {
  it('falls back to a content-type + truncated body preview when the response body is not valid JSON', async () => {
    const html = '<html><body>502 Bad Gateway</body></html>';
    stubFetchResolved(new Response(html, { status: 502, headers: { 'content-type': 'text/html' } }));

    await expect(getCurrentUser()).rejects.toThrow(
      `HTTP_502: HTTP 502 unexpected response (content-type: text/html; body: ${html})`,
    );
  });
});

describe('sandbox report response validation', () => {
  it('rejects a null security report before the UI can dereference it', async () => {
    stubFetchResolved(jsonResponse({ code: 'OK', message: 'ok', data: null }));

    await expect(adminReadSandboxSecurity('sandbox-1')).rejects.toThrow(
      'MALFORMED_RESPONSE: The server answered /admin/sandboxes/{id}/security with null instead of a record.',
    );
  });

  it('rejects an incomplete diagnostics report before rendering it', async () => {
    stubFetchResolved(jsonResponse({
      code: 'OK',
      message: 'ok',
      data: { text: 'partial', truncated: false },
    }));

    await expect(adminReadSandboxDiagnostics('sandbox-1', 'summary')).rejects.toThrow(
      'MALFORMED_RESPONSE: The server answered /admin/sandboxes/{id}/diagnostics/{scope} with an invalid record',
    );
  });
});

describe('request() transport failures', () => {
  it('wraps a network-level fetch rejection into a NETWORK_ERROR with the url and underlying detail', async () => {
    stubFetchRejected(new TypeError('Failed to fetch'));
    await expect(getCurrentUser()).rejects.toThrow(
      "NETWORK_ERROR: can't reach the backend (/api/v1/user/current): Failed to fetch",
    );
  });

  it('passes an AbortError straight through unwrapped', async () => {
    const abortError = new DOMException('The operation was aborted.', 'AbortError');
    stubFetchRejected(abortError);
    await expect(getCurrentUser()).rejects.toBe(abortError);
  });

  it('replays convergent file deletion after a lost response', async () => {
    const mockFetch = vi.fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(jsonResponse({
        code: 'OK',
        message: 'success',
        data: { paths: ['/workspace/report'], deleted_count: 0 },
      }));
    vi.stubGlobal('fetch', mockFetch);

    await expect(deleteSessionFiles('session-1', { paths: ['report'] })).resolves.toMatchObject({
      deleted_count: 0,
    });
    expect(mockFetch).toHaveBeenCalledTimes(2);
    for (const [url, init] of mockFetch.mock.calls as Array<[string, RequestInit]>) {
      expect(url).toBe('/api/v1/sessions/session-1/files/delete');
      expect(init.method).toBe('POST');
      expect(sentBody(init)).toBe(JSON.stringify({ paths: ['report'] }));
    }
  });

  it('replays a lost turn-input response with the same durable client message id', async () => {
    const receipt = {
      session_id: 'session-1',
      command_id: 'session-1:client-1',
      client_message_id: 'client-1',
      input_id: 'input-1',
      status: 'delivered',
    };
    const mockFetch = vi.fn()
      .mockRejectedValueOnce(new TypeError('network changed'))
      .mockResolvedValueOnce(jsonResponse({ code: 'OK', message: 'success', data: receipt }));
    vi.stubGlobal('fetch', mockFetch);

    await expect(appendTurnInput('session-1', {
      content: 'hello',
      client_message_id: 'client-1',
    })).resolves.toEqual(receipt);

    expect(mockFetch).toHaveBeenCalledTimes(2);
    const bodies = mockFetch.mock.calls.map(([, init]) => JSON.parse(sentBody(init)));
    expect(bodies).toEqual([
      { content: 'hello', client_message_id: 'client-1' },
      { content: 'hello', client_message_id: 'client-1' },
    ]);
  });
});

describe('query strings the document does not declare', () => {
  it('sends the paging and filter terms, and drops the ones that are absent', async () => {
    const mockFetch = stubFetchResolved(jsonResponse({
      code: 'OK',
      message: 'ok',
      data: { items: [], pagination: { page: 2, page_size: 25, total_items: 0, total_pages: 0 } },
    }));

    await adminListSessions({ page: 2, pageSize: 25, agent_id: 'agent-1', since: '' });

    const [url] = mockFetch.mock.calls[0] as [string];
    // These handlers read `request.query_params` themselves, so the schema
    // declares no query parameters and the client cannot type them. The terms
    // still have to arrive, which is the half a typecheck cannot cover: an
    // empty `since` is not a filter on the empty string, and a page that never
    // left the browser is the whole page of results.
    const query = new URLSearchParams(url.split('?')[1] ?? '');
    expect(url.split('?')[0]).toBe('/api/v1/admin/sessions/all');
    expect(query.get('page')).toBe('2');
    expect(query.get('page_size')).toBe('25');
    expect(query.get('agent_id')).toBe('agent-1');
    expect(query.has('since')).toBe(false);
    expect(query.has('until')).toBe(false);
  });
});

describe('a route that answers 204', () => {
  it('resolves rather than reporting the absent envelope as a malformed one', async () => {
    const mockFetch = stubFetchResolved(new Response(null, { status: 204 }));

    await expect(revokeMcpClientToken('token-1')).resolves.toBeUndefined();

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/mcp-tokens/token-1');
    expect(init.method).toBe('DELETE');
  });
});

describe('a multipart upload', () => {
  it('keeps the boundary the platform generated, and the file bytes it framed', async () => {
    const mockFetch = stubFetchResolved(jsonResponse({
      code: 'OK',
      message: 'ok',
      data: { path: '/workspace', paths: ['note.bin'] },
    }));
    // A byte no UTF-8 decode survives intact. Reading the body back off the
    // request as text rather than as bytes corrupts exactly this, and the
    // corruption is invisible at every other layer — the request still has a
    // body, the server still answers, and the file that lands is wrong.
    const raw = new Uint8Array([0x00, 0xff, 0xfe, 0x41]);
    const file = new File([raw], 'note.bin', { type: 'application/octet-stream' });

    await uploadSessionFiles('session-1', { path: '/workspace', files: [file] });

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/sessions/session-1/files/upload');
    const contentType = String((init.headers as Headers).get('content-type'));
    expect(contentType).toMatch(/^multipart\/form-data; boundary=/);

    // The body is parsed back with the header that framed it: the boundary has
    // to still describe these bytes, and the bytes have to still be the file's.
    const sent = new Response(init.body as ArrayBuffer, {
      headers: { 'content-type': contentType },
    });
    const form = await sent.formData();
    expect(form.get('path')).toBe('/workspace');
    const received = form.get('files') as File;
    expect(new Uint8Array(await received.arrayBuffer())).toEqual(raw);
  });
});

describe('isRequestAbortError', () => {
  it('recognizes any object whose name is exactly "AbortError"', () => {
    expect(isRequestAbortError(new DOMException('aborted', 'AbortError'))).toBe(true);
    expect(isRequestAbortError({ name: 'AbortError' })).toBe(true);
  });

  it('rejects everything else, including near-misses', () => {
    expect(isRequestAbortError(new Error('AbortError'))).toBe(false); // message, not name
    expect(isRequestAbortError(null)).toBe(false);
    expect(isRequestAbortError(undefined)).toBe(false);
    expect(isRequestAbortError('AbortError')).toBe(false);
    expect(isRequestAbortError({ name: 'abortError' })).toBe(false);
  });
});

describe('managed credential requests', () => {
  it('reads the admin Vault catalog from its named response field', async () => {
    stubFetchResolved(jsonResponse({
      code: 'OK',
      message: 'ok',
      data: {
        vaults: [{
          vault_id: 'vault-1',
          display_name: 'Production',
          archived_at: null,
          credentials: [{
            credential_id: 'credential-1',
            vault_id: 'vault-1',
            display_name: 'Issue tracker',
            auth: { type: 'static_bearer', mcp_server_url: 'https://mcp.example.test' },
          }],
        }],
        credential_delivery: {
          deployment_mode: 'local_development',
          model_credentials: 'egress_placeholder',
          mcp_credentials: 'egress_injection',
          environment_credentials: 'egress_placeholder',
        },
      },
    }));

    await expect(getAdminVaultCatalog()).resolves.toMatchObject({
      vaults: [{
        vault_id: 'vault-1',
        display_name: 'Production',
        archived_at: null,
        credentials: [{
          credential_id: 'credential-1',
          vault_id: 'vault-1',
          display_name: 'Issue tracker',
          auth: { type: 'static_bearer', mcp_server_url: 'https://mcp.example.test' },
        }],
      }],
    });
    expect(fetch).toHaveBeenCalledWith('/api/v1/admin/vaults', expect.any(Object));
  });

  it('starts an Agent conversation without exposing Vault selection', async () => {
    const mockFetch = stubFetchResolved(jsonResponse({
      code: 'OK',
      message: 'ok',
      data: { session_id: 'session-1', agent_id: 'agent/1' },
    }));

    await startAgentConversation('agent/1');

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/agents/agent%2F1/conversations');
    expect(init.method).toBe('POST');
    expect(init.body).toBeUndefined();
    expect((init.headers as Headers).get('idempotency-key')).toMatch(/\S+/);
  });

  it('retries a transport-lost create with the same idempotency key', async () => {
    const mockFetch = vi.fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(jsonResponse({
        code: 'OK',
        message: 'ok',
        data: { session_id: 'session-1', agent_id: 'agent/1' },
      }));
    vi.stubGlobal('fetch', mockFetch);

    await expect(startAgentConversation('agent/1')).resolves.toMatchObject({
      session_id: 'session-1',
    });

    expect(mockFetch).toHaveBeenCalledTimes(2);
    const first = mockFetch.mock.calls[0][1] as RequestInit;
    const second = mockFetch.mock.calls[1][1] as RequestInit;
    const firstKey = (first.headers as Headers).get('idempotency-key');
    expect(firstKey).toMatch(/\S+/);
    expect((second.headers as Headers).get('idempotency-key')).toBe(firstKey);
  });

  it('does not retry an HTTP refusal from conversation create', async () => {
    const mockFetch = stubFetchResolved(jsonResponse({
      code: 'FORBIDDEN',
      message: 'not allowed',
      data: null,
    }, { status: 403 }));

    await expect(startAgentConversation('agent/1')).rejects.toThrow('FORBIDDEN: not allowed');
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
});

describe('Agent authoring wire boundary', () => {
  it('creates the Agent first, then writes access without leaking it into POST', async () => {
    const mockFetch = stubFetchJson({
      code: 'OK',
      message: 'ok',
      data: { agent_id: 'agent/1', name: 'Operator', version: 1 },
    });

    await createAgent(
      {
        name: 'Operator',
        model: 'model-1',
        environment_name: 'default',
        visibility: 'allowlist',
        admins: ['manager'],
        allowed_user_ids: ['member'],
      },
      AGENT_SCHEMA,
    );

    expect(mockFetch).toHaveBeenCalledTimes(2);
    const [createUrl, createInit] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(createUrl).toBe('/api/v1/agents');
    expect(JSON.parse(sentBody(createInit))).toEqual({
      name: 'Operator',
      model: 'model-1',
      environment_name: 'default',
    });
    const [accessUrl, accessInit] = mockFetch.mock.calls[1] as [string, RequestInit];
    expect(accessUrl).toBe('/api/v1/agents/agent%2F1/access');
    expect(JSON.parse(sentBody(accessInit))).toEqual({
      visibility: 'allowlist',
      admins: ['manager'],
      allowed_user_ids: ['member'],
    });
  });

  it('makes the secure private access default an explicit second create phase', async () => {
    const mockFetch = stubFetchJson({
      code: 'OK',
      message: 'ok',
      data: { agent_id: 'agent-1', name: 'Private', version: 1 },
    });

    await createAgent(
      { name: 'Private', model: 'model-1', environment_name: 'default' },
      AGENT_SCHEMA,
    );

    expect(mockFetch).toHaveBeenCalledTimes(2);
    const [accessUrl, accessInit] = mockFetch.mock.calls[1] as [string, RequestInit];
    expect(accessUrl).toBe('/api/v1/agents/agent-1/access');
    expect(JSON.parse(sentBody(accessInit))).toEqual({
      visibility: 'private',
      admins: [],
      allowed_user_ids: [],
    });
  });

  it('validates access before creating so a malformed second phase cannot orphan a harness', async () => {
    const mockFetch = stubFetchJson({ code: 'OK', message: 'ok', data: {} });

    await expect(
      createAgent(
        {
          name: 'Invalid access',
          model: 'model-1',
          environment_name: 'default',
          admins: [''],
        },
        AGENT_SCHEMA,
      ),
    ).rejects.toThrow('Invalid Agent admins: expected non-empty user IDs');
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('fails the create when the dedicated access write is refused', async () => {
    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        code: 'OK',
        message: 'ok',
        data: { agent_id: 'agent-1', name: 'Private', version: 1 },
      }))
      .mockResolvedValueOnce(jsonResponse(
        { code: 'FORBIDDEN', message: 'access denied', data: null },
        { status: 403 },
      ));
    vi.stubGlobal('fetch', mockFetch);

    await expect(
      createAgent(
        { name: 'Private', model: 'model-1', environment_name: 'default' },
        AGENT_SCHEMA,
      ),
    ).rejects.toThrow('FORBIDDEN: access denied');
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it('projects a fetched Agent record to authoring fields before PUT', async () => {
    const mockFetch = stubFetchResolved(jsonResponse({
      code: 'OK',
      message: 'ok',
      data: { agent_id: 'agent-1', name: 'Operator', version: 4 },
    }));

    await updateAgent(
      'agent-1',
      {
        agent_id: 'agent-1',
        name: 'Operator',
        model: 'model-1',
        environment_name: 'default',
        version: 3,
        created_by: 'owner',
        state: 'ACTIVE',
        visibility: 'private',
        can_manage: true,
      },
      AGENT_SCHEMA,
    );

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/agents/agent-1');
    expect(JSON.parse(sentBody(init))).toEqual({
      name: 'Operator',
      model: 'model-1',
      environment_name: 'default',
      version: 3,
    });
  });
});
