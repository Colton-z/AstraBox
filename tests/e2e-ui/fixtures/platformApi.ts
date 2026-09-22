/**
 * Typed API helper for the platform surfaces that are NOT the turn path — share
 * links, vaults, deployments, assistants, session files and the operator/admin
 * reads.
 *
 * Why a second helper rather than more methods on `astraApi.ts`: that file's
 * stated surface is the turn path (agents → conversations → sessions →
 * ai-stream) and its callers read it as such. These are the surrounding product
 * capabilities — each with its own route family, its own envelope quirks and its
 * own lifecycle — so they get their own file rather than doubling the size of
 * the one every turn spec imports.
 *
 * Envelope: the same `{ data: ... }` wrapper, with two documented exceptions
 * carried here verbatim rather than smoothed over — DELETE routes that answer
 * `204` with no body, and `GET /admin/sessions/{id}/transcript`, which is a raw
 * attachment. Smoothing those would hide the contract a spec is asserting.
 */
import { createHash, createHmac } from 'node:crypto';

import type { APIRequestContext } from '@playwright/test';

import { apiPath, parseTimeoutEnv } from './env';

// ── Share ───────────────────────────────────────────────────────────────────

/** Share configuration as `GET/POST /sessions/{id}/share` projects it. */
export interface ShareConfig {
  enabled: boolean;
  token?: string | null;
  expires_at?: string | null;
  allow_download?: boolean;
  created_at?: string | null;
}

// ── Vaults ──────────────────────────────────────────────────────────────────

export interface VaultRecord {
  vault_id: string;
  display_name: string;
  metadata?: Record<string, unknown>;
  archived_at?: string | null;
  [key: string]: unknown;
}

export interface CredentialRecord {
  credential_id: string;
  vault_id: string;
  display_name?: string | null;
  /** The OPEN half of the credential document. Secret fields are never here. */
  auth: Record<string, unknown>;
  archived_at?: string | null;
  [key: string]: unknown;
}

// ── Deployments ─────────────────────────────────────────────────────────────

export interface DeploymentRecord {
  deployment_id: string;
  agent_id: string;
  scene: string;
  enabled: boolean;
  name?: string;
  prompt_prefix?: string;
  channel_config?: Record<string, unknown>;
  credentials_configured?: boolean;
  callback_base_url?: string;
  schedule?: { cron: string; timezone: string };
  /** The trigger credential. Returned to its owner so a webhook can be signed. */
  secret?: string;
  [key: string]: unknown;
}

export interface ChannelFieldRecord {
  key: string;
  label: string;
  required: boolean;
  secret: boolean;
  kind: 'string' | 'number' | 'boolean' | 'select';
  options: string[];
  default?: unknown;
  placeholder?: string;
  help?: string;
}

export interface ChannelProviderRecord {
  name: string;
  label: string;
  scene: `channel:${string}`;
  config_fields: ChannelFieldRecord[];
  credential_fields: ChannelFieldRecord[];
  callback_path?: string | null;
  setup_url?: string | null;
  documentation_url?: string | null;
  supports_source: boolean;
  uses_trigger_secret: boolean;
}

export interface DeploymentRunRecord {
  run_id: string;
  deployment_id: string;
  agent_id: string;
  trigger: string;
  status: string;
  session_id?: string | null;
  turn_id?: string | null;
  replayed_from_run_id?: string | null;
  scheduled_for?: string | null;
  [key: string]: unknown;
}

export interface TriggerReceipt {
  deployment_id: string;
  session_id: string;
  status: string;
  [key: string]: unknown;
}

// ── Assistants ──────────────────────────────────────────────────────────────

export interface AssistantRecord {
  assistant_id: string;
  display_name: string;
  engine_kind?: string;
  environment_name?: string;
  workspace_state?: string | null;
  current_sandbox_id?: string | null;
  [key: string]: unknown;
}

// ── Session files ───────────────────────────────────────────────────────────

export interface SessionFileEntry {
  path?: string;
  name?: string;
  kind?: string;
  size?: number;
  modified_at?: string;
}

export interface SessionFileListing {
  root_path?: string;
  current_path?: string;
  parent_path?: string | null;
  entries?: SessionFileEntry[];
  session_kind?: string;
}

/** A non-2xx answer captured as data — for specs whose subject IS the refusal. */
export interface ApiRefusal {
  status: number;
  code: string;
  message: string;
}

export class PlatformApi {
  constructor(private readonly request: APIRequestContext) {}

  private timeout(ms?: number): number {
    return ms ?? parseTimeoutEnv('ASTRABOX_E2E_API_TIMEOUT_MS', 60_000);
  }

  /** Unwrap `{ data }`; throw with the body on non-2xx. */
  async data<T>(method: string, route: string, body?: unknown, timeoutMs?: number): Promise<T> {
    const response = await this.request.fetch(apiPath(route), {
      method,
      data: body === undefined ? undefined : body,
      timeout: this.timeout(timeoutMs),
    });
    const text = await response.text();
    if (!response.ok()) {
      throw new Error(`${method} ${route} -> ${response.status()}: ${text.slice(0, 500)}`);
    }
    if (!text) {
      // 204 (the DELETE routes). An empty body is the answer, not a parse failure.
      return undefined as unknown as T;
    }
    const parsed = JSON.parse(text) as { data?: T } & T;
    return (parsed.data ?? parsed) as T;
  }

  /** The status a route answers, without unwrapping — for 204/404 assertions. */
  async status(method: string, route: string, body?: unknown, timeoutMs?: number): Promise<number> {
    const response = await this.request.fetch(apiPath(route), {
      method,
      data: body === undefined ? undefined : body,
      timeout: this.timeout(timeoutMs),
    });
    return response.status();
  }

  /**
   * Call a route that is EXPECTED to refuse, and return the refusal as data.
   * A capability the deployment cannot serve must say so with a code and a
   * reason (the repo's no-silent-degradation rule); a spec asserting that needs
   * the refusal, not an exception.
   */
  async refusal(method: string, route: string, body?: unknown, timeoutMs?: number): Promise<ApiRefusal> {
    const response = await this.request.fetch(apiPath(route), {
      method,
      data: body === undefined ? undefined : body,
      timeout: this.timeout(timeoutMs),
    });
    const text = await response.text();
    let code = '';
    let message = text.slice(0, 300);
    try {
      const parsed = JSON.parse(text) as { code?: string; message?: string };
      code = String(parsed.code || '');
      message = String(parsed.message || message);
    } catch {
      /* a non-JSON body is itself the evidence; keep the text */
    }
    return { status: response.status(), code, message };
  }

  // ── Share ─────────────────────────────────────────────────────────────────

  createShare(sessionId: string, opts: { allowDownload?: boolean; expiresInSeconds?: number } = {}): Promise<ShareConfig> {
    return this.data<ShareConfig>('POST', `/sessions/${sessionId}/share`, {
      allow_download: Boolean(opts.allowDownload),
      ...(opts.expiresInSeconds ? { expires_in_seconds: opts.expiresInSeconds } : {}),
    });
  }

  getShare(sessionId: string): Promise<ShareConfig> {
    return this.data<ShareConfig>('GET', `/sessions/${sessionId}/share`);
  }

  revokeShare(sessionId: string): Promise<ShareConfig> {
    return this.data<ShareConfig>('DELETE', `/sessions/${sessionId}/share`);
  }

  /** Viewer side: the shared session document, by token alone (no login). */
  sharedSession(token: string): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>('GET', `/share/${token}`);
  }

  sharedMessages(token: string, limit = 20): Promise<{ messages?: Array<Record<string, unknown>> }> {
    return this.data<{ messages?: Array<Record<string, unknown>> }>(
      'GET',
      `/share/${token}/messages?limit=${limit}`,
    );
  }

  sharedFiles(token: string, path?: string): Promise<SessionFileListing> {
    const query = path ? `?path=${encodeURIComponent(path)}` : '';
    return this.data<SessionFileListing>('GET', `/share/${token}/files/list${query}`);
  }

  // ── Administrator-managed credentials ───────────────────────────────────

  createVault(displayName: string, metadata?: Record<string, unknown>): Promise<VaultRecord> {
    return this.data<VaultRecord>('POST', '/admin/vaults', {
      display_name: displayName,
      ...(metadata ? { metadata } : {}),
    });
  }

  listVaults(): Promise<VaultRecord[]> {
    return this.data<{ vaults: VaultRecord[] }>('GET', '/admin/vaults').then((d) => d.vaults || []);
  }

  getVault(vaultId: string): Promise<VaultRecord> {
    return this.data<VaultRecord>('GET', `/admin/vaults/${vaultId}`);
  }

  archiveVault(vaultId: string): Promise<VaultRecord> {
    return this.data<VaultRecord>('POST', `/admin/vaults/${vaultId}/archive`);
  }

  deleteVault(vaultId: string): Promise<number> {
    return this.status('DELETE', `/admin/vaults/${vaultId}`);
  }

  /**
   * Add a credential. `auth` is the credential document; its secret fields
   * (`token` / `access_token` / `refresh_token` / `client_secret` /
   * `secret_value`) are write-only and must never come back on any read.
   */
  createCredential(
    vaultId: string,
    auth: Record<string, unknown>,
    displayName?: string,
  ): Promise<CredentialRecord> {
    return this.data<CredentialRecord>('POST', `/admin/vaults/${vaultId}/credentials`, {
      auth,
      ...(displayName ? { display_name: displayName } : {}),
    });
  }

  listCredentials(vaultId: string): Promise<CredentialRecord[]> {
    return this.data<{ credentials: CredentialRecord[] }>(
      'GET',
      `/admin/vaults/${vaultId}/credentials`,
    ).then((d) => d.credentials || []);
  }

  updateCredential(
    vaultId: string,
    credentialId: string,
    patch: Record<string, unknown>,
  ): Promise<CredentialRecord> {
    return this.data<CredentialRecord>('PATCH', `/admin/vaults/${vaultId}/credentials/${credentialId}`, patch);
  }

  archiveCredential(vaultId: string, credentialId: string): Promise<CredentialRecord> {
    return this.data<CredentialRecord>('POST', `/admin/vaults/${vaultId}/credentials/${credentialId}/archive`);
  }

  deleteCredential(vaultId: string, credentialId: string): Promise<number> {
    return this.status('DELETE', `/admin/vaults/${vaultId}/credentials/${credentialId}`);
  }

  // ── Deployments (trigger bindings) ────────────────────────────────────────

  listDeployments(agentId: string): Promise<DeploymentRecord[]> {
    return this.data<DeploymentRecord[]>('GET', `/admin/agents/${agentId}/deployments`);
  }

  listChannelProviders(): Promise<ChannelProviderRecord[]> {
    return this.data<ChannelProviderRecord[]>('GET', '/admin/channel-providers');
  }

  createDeployment(agentId: string, body: Record<string, unknown>): Promise<DeploymentRecord> {
    return this.data<DeploymentRecord>('POST', `/admin/agents/${agentId}/deployments`, body);
  }

  updateDeployment(
    agentId: string,
    deploymentId: string,
    patch: Record<string, unknown>,
  ): Promise<DeploymentRecord> {
    return this.data<DeploymentRecord>(
      'PUT',
      `/admin/agents/${agentId}/deployments/${deploymentId}`,
      patch,
    );
  }

  deleteDeployment(agentId: string, deploymentId: string): Promise<{ deleted?: boolean }> {
    return this.data<{ deleted?: boolean }>(
      'DELETE',
      `/admin/agents/${agentId}/deployments/${deploymentId}`,
    );
  }

  listDeploymentRuns(
    agentId: string,
    deploymentId: string,
  ): Promise<DeploymentRunRecord[]> {
    return this.data<DeploymentRunRecord[]>(
      'GET',
      `/admin/agents/${agentId}/deployments/${deploymentId}/runs`,
    );
  }

  triggerDeploymentRun(
    agentId: string,
    deploymentId: string,
  ): Promise<DeploymentRunRecord> {
    return this.data<DeploymentRunRecord>(
      'POST',
      `/admin/agents/${agentId}/deployments/${deploymentId}/runs`,
    );
  }

  replayDeploymentRun(
    agentId: string,
    deploymentId: string,
    runId: string,
  ): Promise<DeploymentRunRecord> {
    return this.data<DeploymentRunRecord>(
      'POST',
      `/admin/agents/${agentId}/deployments/${deploymentId}/runs/${runId}/replay`,
    );
  }

  /**
   * Fire an `hmac`-scene webhook the way a real sender does. The signature is
   * Base64(HMAC-SHA256(secret, "<timestamp>.<sha256(body)>")) over the EXACT
   * bytes sent, so it is computed here from the same buffer that is posted —
   * re-serializing the body would produce a signature for a different payload.
   * A Buffer carries sender-authored bytes; other values are serialized as JSON.
   */
  async triggerHmac(
    deploymentId: string,
    secret: string,
    payload: unknown,
    overrides: { timestamp?: string; signature?: string; signOverPayload?: unknown } = {},
  ): Promise<{ status: number; body: string }> {
    const raw = Buffer.isBuffer(payload) ? payload : Buffer.from(JSON.stringify(payload), 'utf-8');
    const timestamp = overrides.timestamp ?? String(Math.floor(Date.now() / 1000));
    // `signOverPayload` signs a DIFFERENT body than the one posted — the shape of
    // a captured signature aimed at new bytes, which the body digest must defeat.
    const signedBytes = overrides.signOverPayload === undefined
      ? raw
      : Buffer.isBuffer(overrides.signOverPayload)
        ? overrides.signOverPayload
        : Buffer.from(JSON.stringify(overrides.signOverPayload), 'utf-8');
    const digest = createHash('sha256').update(signedBytes).digest('hex');
    const signature =
      overrides.signature
      ?? createHmac('sha256', secret).update(`${timestamp}.${digest}`).digest('base64');
    const response = await this.request.fetch(apiPath(`/deployments/${deploymentId}/trigger`), {
      method: 'POST',
      data: raw,
      headers: {
        'content-type': Buffer.isBuffer(payload) ? 'text/plain; charset=utf-8' : 'application/json',
        'x-webhook-timestamp': timestamp,
        'x-webhook-signature': signature,
      },
      timeout: parseTimeoutEnv('ASTRABOX_E2E_API_TIMEOUT_MS', 60_000),
    });
    return { status: response.status(), body: await response.text() };
  }

  /** The accepted receipt of a signed trigger (throws when it was refused). */
  async triggerHmacAccepted(
    deploymentId: string,
    secret: string,
    payload: unknown,
  ): Promise<TriggerReceipt> {
    const { status, body } = await this.triggerHmac(deploymentId, secret, payload);
    if (status !== 200) {
      throw new Error(`trigger ${deploymentId} -> ${status}: ${body.slice(0, 400)}`);
    }
    return (JSON.parse(body) as { data: TriggerReceipt }).data;
  }

  // ── Assistants ────────────────────────────────────────────────────────────

  createAssistant(body: Record<string, unknown>): Promise<AssistantRecord> {
    return this.data<AssistantRecord>('POST', '/assistants', body);
  }

  listAssistants(): Promise<AssistantRecord[]> {
    return this.data<AssistantRecord[]>('GET', '/assistants');
  }

  getAssistant(assistantId: string): Promise<AssistantRecord> {
    return this.data<AssistantRecord>('GET', `/assistants/${assistantId}`);
  }

  patchAssistant(assistantId: string, patch: Record<string, unknown>): Promise<AssistantRecord> {
    return this.data<AssistantRecord>('PATCH', `/assistants/${assistantId}`, patch);
  }

  deleteAssistant(assistantId: string): Promise<{ deleted?: boolean }> {
    return this.data<{ deleted?: boolean }>('DELETE', `/assistants/${assistantId}`);
  }

  wakeWorkspace(assistantId: string, timeoutMs = 180_000): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>(
      'POST',
      `/assistants/${assistantId}/workspace/wake`,
      {},
      timeoutMs,
    );
  }

  hibernateWorkspace(assistantId: string, timeoutMs = 120_000): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>(
      'POST',
      `/assistants/${assistantId}/workspace/hibernate`,
      {},
      timeoutMs,
    );
  }

  deleteWorkspace(assistantId: string, timeoutMs = 120_000): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>(
      'DELETE',
      `/assistants/${assistantId}/workspace`,
      undefined,
      timeoutMs,
    );
  }

  // ── Session files ─────────────────────────────────────────────────────────

  listFiles(sessionId: string, path?: string, timeoutMs = 120_000): Promise<SessionFileListing> {
    return this.data<SessionFileListing>(
      'POST',
      `/sessions/${sessionId}/files/list`,
      path ? { path } : {},
      timeoutMs,
    );
  }

  mkdir(sessionId: string, path: string, timeoutMs = 120_000): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>(
      'POST',
      `/sessions/${sessionId}/files/mkdir`,
      { path },
      timeoutMs,
    );
  }

  moveFile(sessionId: string, srcPath: string, destPath: string, timeoutMs = 120_000): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>(
      'POST',
      `/sessions/${sessionId}/files/move`,
      { src_path: srcPath, dest_path: destPath },
      timeoutMs,
    );
  }

  deleteFiles(sessionId: string, paths: string[], timeoutMs = 120_000): Promise<{ deleted_count?: number }> {
    return this.data<{ deleted_count?: number }>(
      'POST',
      `/sessions/${sessionId}/files/delete`,
      { paths },
      timeoutMs,
    );
  }

  // ── Operator / admin reads ────────────────────────────────────────────────

  systemOverview(): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>('GET', '/admin/system/overview');
  }

  adminErrors(): Promise<{ errors?: Array<Record<string, unknown>> }> {
    return this.data<{ errors?: Array<Record<string, unknown>> }>('GET', '/admin/errors');
  }

  adminLogs(): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>('GET', '/admin/logs');
  }

  listSandboxes(): Promise<{ backend?: string; items?: Array<Record<string, unknown>> }> {
    return this.data<{ backend?: string; items?: Array<Record<string, unknown>> }>(
      'GET',
      '/admin/sandboxes',
    );
  }

  getSandbox(sandboxId: string): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>('GET', `/admin/sandboxes/${sandboxId}`);
  }

  sandboxSecurity(sandboxId: string): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>('GET', `/admin/sandboxes/${sandboxId}/security`);
  }

  sandboxDiagnostics(sandboxId: string, scope: string): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>(
      'GET',
      `/admin/sandboxes/${sandboxId}/diagnostics/${scope}`,
      undefined,
      120_000,
    );
  }

  preparedRuntime(agentId: string): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>(
      'GET',
      `/agents/${encodeURIComponent(agentId)}/prepared-runtime`,
    );
  }

  idleAction(): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>('GET', '/admin/sandbox-idle-action');
  }

  idleSweep(): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>('POST', '/admin/sandbox-idle-sweep', {}, 120_000);
  }

  sessionTrace(sessionId: string): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>('GET', `/admin/sessions/${sessionId}/trace`);
  }

  // ── Environments ──────────────────────────────────────────────────────────

  listEnvironments(): Promise<Array<Record<string, unknown>>> {
    return this.data<Array<Record<string, unknown>>>('GET', '/admin/environments');
  }

  putEnvironment(name: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>(
      'PUT',
      `/admin/environments/${encodeURIComponent(name)}`,
      body,
    );
  }

  environmentSchema(): Promise<{ fields?: Array<Record<string, unknown>> }> {
    return this.data<{ fields?: Array<Record<string, unknown>> }>('GET', '/admin/environment-schema');
  }

  agentSchema(): Promise<{ fields?: Array<Record<string, unknown>> }> {
    return this.data<{ fields?: Array<Record<string, unknown>> }>('GET', '/admin/agent-schema');
  }

  // ── Agent MCP catalogue ───────────────────────────────────────────────────

  /**
   * One JSON-RPC call against `POST /mcp`. The route answers either
   * JSON or an SSE frame depending on the Accept negotiation, so the raw text
   * comes back with the status and the caller decides.
   */
  async agentMcp(
    method: string,
    params: Record<string, unknown> = {},
    id = 1,
  ): Promise<{ status: number; body: string }> {
    const response = await this.request.fetch(apiPath('/mcp'), {
      method: 'POST',
      data: { jsonrpc: '2.0', id, method, params },
      headers: { Accept: 'application/json, text/event-stream' },
      timeout: parseTimeoutEnv('ASTRABOX_E2E_API_TIMEOUT_MS', 60_000),
    });
    return { status: response.status(), body: await response.text() };
  }

  /** The `result` of a JSON-RPC call, from either a JSON or an SSE body. */
  async agentMcpResult(
    method: string,
    params: Record<string, unknown> = {},
  ): Promise<Record<string, unknown>> {
    const { status, body } = await this.agentMcp(method, params);
    if (status !== 200) {
      throw new Error(`agent mcp ${method} -> ${status}: ${body.slice(0, 400)}`);
    }
    const jsonText = body.trimStart().startsWith('{')
      ? body
      : (body.split('\n').find((line) => line.startsWith('data:')) || '').slice(5).trim();
    const parsed = JSON.parse(jsonText) as { result?: Record<string, unknown>; error?: unknown };
    if (!parsed.result) {
      throw new Error(`agent mcp ${method} returned no result: ${JSON.stringify(parsed).slice(0, 400)}`);
    }
    return parsed.result;
  }
}
