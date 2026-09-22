/**
 * Typed API helper for the AstraBox backend. The spec-facing surface is four
 * entry points — create, wait, turn, oracle — over the `/api/v1` contract
 * (agents → conversations → sessions → ai-stream).
 *
 * Envelope: responses wrap payloads as `{ data: ... }`; `data()` unwraps and
 * fails loud on non-2xx with the body text in the error.
 */
import { randomUUID } from 'node:crypto';

import type { APIRequestContext } from '@playwright/test';

import { apiPath, parseTimeoutEnv } from './env';

export interface AgentRecord {
  agent_id: string;
  name: string;
  state?: string;
  [key: string]: unknown;
}

interface AgentAuthoringSchema {
  fields: Array<{ key: string; path?: string }>;
}

export interface AgentAccessPolicy {
  visibility: 'public' | 'private' | 'allowlist';
  admins: string[];
  allowed_user_ids: string[];
}

function agentAccessPolicy(config: Record<string, unknown>): AgentAccessPolicy {
  const rawVisibility = config.visibility == null ? 'private' : String(config.visibility);
  if (!['public', 'private', 'allowlist'].includes(rawVisibility)) {
    throw new Error(`invalid Agent visibility ${JSON.stringify(rawVisibility)}`);
  }
  const ids = (key: 'admins' | 'allowed_user_ids'): string[] => {
    const value = config[key];
    if (value == null) return [];
    if (
      !Array.isArray(value)
      || value.some((item) => typeof item !== 'string' || item.trim() === '')
    ) {
      throw new Error(`invalid Agent ${key}: expected non-empty user IDs`);
    }
    return value as string[];
  };
  return {
    visibility: rawVisibility as AgentAccessPolicy['visibility'],
    admins: ids('admins'),
    allowed_user_ids: ids('allowed_user_ids'),
  };
}

export interface AssistantRecord {
  assistant_id: string;
  display_name?: string;
  environment_name?: string;
  engine_kind?: string;
  [key: string]: unknown;
}

/**
 * What the workspace lifecycle endpoints answer with. Wake is asynchronous —
 * it returns MATERIALIZING with the bootstrap session that is driving it, and
 * the caller polls by waking again. Hibernate persists the workspace through
 * the storage seam and releases its old sandbox; wake materializes a new one.
 */
export interface WorkspaceState {
  assistant_id: string;
  state?: string;
  current_sandbox_id?: string | null;
  provisioning_session_id?: string | null;
  engine_kind?: string | null;
  hibernated?: boolean;
  released?: boolean;
  previous_sandbox_id?: string | null;
  sandbox_id?: string | null;
  hibernated_at?: string | null;
  recovery_pending_sandbox_id?: string | null;
  retryable?: boolean;
  [key: string]: unknown;
}

export interface SessionRecord {
  session_id: string;
  state?: string;
  sandbox_id?: string | null;
  conversation_state?: string | null;
  terminal_cwd?: string | null;
  last_error?: string | null;
  // Agent-conversation fields exposed by GET /sessions/{id} (verified against the
  // live projection): the session IS an agent's conversation, always ACTIVE.
  agent_id?: string | null;
  session_kind?: string | null;
  title?: string | null;
  // Permission mode the conversation runs under. Community conversations are
  // created with `bypassPermissions` (POST /conversations hardcodes it) — switch
  // with setPermissionMode(), or pass permission_mode per-turn via streamPrompt.
  permission_mode?: string | null;
  current_turn_id?: string | null;
  last_turn_id?: string | null;
  last_turn_status?: string | null;
  pending_interaction?: PendingInteraction | null;
  engine_capabilities?: {
    permission_modes?: string[];
    [key: string]: unknown;
  } | null;
  [key: string]: unknown;
}

/** Operator-only session detail. Native runtime coordinates never belong to getSession(). */
export interface AdminSessionRecord extends SessionRecord {
  engine_session_key?: string | null;
  runtime_identity?: Record<string, unknown> | null;
}

export interface SessionCreated {
  session_id: string;
  agent_id?: string;
  deployment_name?: string;
  [key: string]: unknown;
}

export interface MessageRecord {
  role: 'user' | 'assistant' | string;
  content?: unknown;
  blocks?: Array<Record<string, unknown>>;
  content_blocks?: Array<Record<string, unknown>>;
  parts?: Array<Record<string, unknown>>;
  turn_id: string;
  message_id: string;
  created_at?: string;
  [key: string]: unknown;
}

/**
 * The overlay the messages page carries for an in-flight turn (first page only).
 * `resume_cursor.frame_seq` is the replay cursor fed to GET ai-stream `after_seq`.
 */
export interface ActiveTurnOverlay {
  turn_id: string;
  message: MessageRecord;
  messages?: MessageRecord[];
  resume_cursor?: { turn_id?: string; frame_seq?: number | null };
}

export interface MessagePage {
  messages: MessageRecord[];
  has_more?: boolean;
  active_turn_overlay?: ActiveTurnOverlay | null;
  [key: string]: unknown;
}

/**
 * The page the console reads, with each settled response's tool work folded.
 *
 * Same records as `MessagePage`, paged by record instead of by timestamp:
 * `next_cursor` is the opaque token the next page is asked for with, and it
 * pins that page to the history this one saw.
 */
export interface HistoryBlockPage extends MessagePage {
  next_cursor?: string | null;
  block_count?: number;
  paging_mode?: string;
}

export interface ChildRunRecord {
  child_run_id: string;
  parent_child_run_id?: string | null;
  engine_kind: string;
  depth: number;
  engine_event: string;
  engine_status?: string | null;
  engine_reason?: string | null;
  closed: boolean;
  active: boolean;
  operations: string[];
  description?: string | null;
  task_type?: string | null;
  last_tool_name?: string | null;
  summary?: string | null;
  usage?: Record<string, unknown> | null;
}

export interface ChildRunPage {
  session_id: string;
  child_runs: ChildRunRecord[];
}

export interface ChildRunMessagePage {
  session_id: string;
  child_run_id: string;
  messages: Array<{
    role: string;
    content: Array<Record<string, unknown>>;
    message_id?: string;
  }>;
}

/** Permission modes are engine-owned names carried verbatim by the platform. */
export type PermissionMode = string;

/** The session's pending tool/permission interaction (session.pending_interaction). */
export interface PendingInteraction {
  interaction_id: string;
  kind?: string;
  turn_id?: string;
  tool_name?: string;
  tool_call_id?: string;
  permission_suggestions?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

/** Result of POST /sessions/{id}/interaction-respond. */
export interface InteractionAnswerResult {
  interaction_id: string;
  answered: boolean;
  turn_id?: string;
  [key: string]: unknown;
}

/** Result of reclaiming a conversation's compute without ending the conversation. */
export interface SandboxReclaimResult {
  session_id: string;
  sandbox_id?: string | null;
  status: string;
  killed?: boolean;
  [key: string]: unknown;
}

/** Result of POST /sessions/{id}/permission-mode. */
export interface PermissionModeResult {
  session_id: string;
  permission_mode: string;
  applied: boolean;
  [key: string]: unknown;
}

/** Outcome of GET /sessions/{id}/ai-stream (durable-frame replay/resume). */
export interface ResumeStreamResult {
  /** 200 when frames were replayed; 204 when there is nothing to resume. */
  status: number;
  /** true on a 200 replay, false on a 204 (no resumable stream). */
  replayed: boolean;
  /** Frame `type` values in arrival order. */
  frameTypes: string[];
  /** Concatenated text-delta payloads. */
  text: string;
  /** errorText of the first `error` frame, if any. */
  errorText: string | null;
  /** Raw SSE body (empty on 204). */
  raw: string;
}

export interface TurnResult {
  /** Concatenated text-delta payloads. */
  text: string;
  /** Frame `type` values in arrival order (for shape assertions). */
  frameTypes: string[];
  /** errorText of the first `error` frame, if any. */
  errorText: string | null;
  /**
   * Wall-clock ms for the whole turn (request → stream closed). NOTE:
   * APIRequestContext buffers the SSE body, so per-frame timing is not
   * observable here — latency-shape assertions belong in UI specs, on the
   * status-pill `data-pulse` transition.
   */
  totalMs: number;
}

/**
 * Flatten a message's visible text. Structured `blocks` carry the same text as
 * `content` on assistant messages, so counting both would double it — prefer the
 * structured blocks and fall back to `content` only when no structured text
 * exists.
 */
export function messageText(message: MessageRecord): string {
  const chunks: string[] = [];
  const collect = (list?: Array<Record<string, unknown>>) => {
    for (const block of list || []) {
      const text = block.text ?? block.content;
      if (typeof text === 'string') {
        chunks.push(text);
      }
    }
  };
  collect(message.blocks);
  collect(message.content_blocks);
  collect(message.parts);
  if (chunks.length === 0 && typeof message.content === 'string') {
    chunks.push(message.content);
  }
  return chunks.join('');
}

/** Apply the first-page active-turn overlay exactly as the console does. */
export function visibleMessages(page: MessagePage): MessageRecord[] {
  const messages = [...(page.messages || [])];
  const overlay = page.active_turn_overlay;
  if (!overlay) return messages;

  const overlayMessages = overlay.messages?.length
    ? overlay.messages
    : [overlay.message];
  for (const overlayMessage of overlayMessages) {
    const overlayMessageId = String(overlayMessage.message_id ?? '').trim();
    if (!overlayMessageId) {
      throw new Error('active-turn overlay message is missing message_id');
    }
    const existingIndex = messages.findIndex((message) => {
      if (message.role !== overlayMessage.role) return false;
      const messageId = String(message.message_id ?? '').trim();
      if (!messageId) {
        throw new Error('durable message is missing message_id');
      }
      return messageId === overlayMessageId;
    });
    if (existingIndex >= 0) {
      messages[existingIndex] = overlayMessage;
    } else {
      messages.push(overlayMessage);
    }
  }
  return messages;
}

export class AstraApi {
  private agentAuthoringKeysPromise?: Promise<ReadonlySet<string>>;

  constructor(private readonly request: APIRequestContext) {}

  async data<T>(method: string, route: string, body?: unknown, timeoutMs?: number): Promise<T> {
    const response = await this.request.fetch(apiPath(route), {
      method,
      data: body === undefined ? undefined : body,
      timeout: timeoutMs ?? parseTimeoutEnv('ASTRABOX_E2E_API_TIMEOUT_MS', 60_000),
    });
    const text = await response.text();
    if (!response.ok()) {
      throw new Error(`${method} ${route} -> ${response.status()}: ${text.slice(0, 500)}`);
    }
    const parsed = JSON.parse(text) as { data?: T } & T;
    return (parsed.data ?? parsed) as T;
  }

  listAgents(): Promise<AgentRecord[]> {
    return this.data<AgentRecord[]>('GET', '/agents');
  }

  private agentAuthoringKeys(): Promise<ReadonlySet<string>> {
    if (!this.agentAuthoringKeysPromise) {
      this.agentAuthoringKeysPromise = this.data<AgentAuthoringSchema>(
        'GET',
        '/agent-configuration/schema',
      ).then((schema) => {
        const keys = new Set(
          schema.fields.map((field) => String(field.path || field.key).split('.', 1)[0]),
        );
        if (keys.size === 0) throw new Error('Agent authoring schema has no fields');
        return keys;
      });
    }
    return this.agentAuthoringKeysPromise;
  }

  private async projectAgentAuthoringConfig(
    config: Record<string, unknown>,
    includeVersion: boolean,
  ): Promise<Record<string, unknown>> {
    const keys = new Set(await this.agentAuthoringKeys());
    if (includeVersion) keys.add('version');
    return Object.fromEntries(Object.entries(config).filter(([key]) => keys.has(key)));
  }

  /** The deployment-proven Agent selected by the runner, or the local seed. */
  async defaultAgent(name = process.env.ASTRABOX_E2E_AGENT_NAME || 'Claude Code'): Promise<AgentRecord> {
    const agents = await this.listAgents();
    const match = agents.find((a) => a.name === name);
    if (!match) {
      throw new Error(`agent ${JSON.stringify(name)} not found; have: ${agents.map((a) => a.name).join(', ')}`);
    }
    return match;
  }

  /**
   * Create a temporary Agent whose conversations own cold, isolated sandboxes.
   *
   * The campaign Agent uses an Agent-tenancy Environment backed exclusively by
   * the shared Pool. Tests that require conversation tenancy must not inherit
   * that placement policy. The deployment's cold Environment is explicitly
   * conversation-tenancy with prewarming disabled, so node capacity is the
   * admission authority and every conversation owns its sandbox.
   */
  async createColdTestAgent(name: string): Promise<AgentRecord> {
    const resolvedName = String(name || '').trim();
    if (!resolvedName) throw new Error('test Agent name must not be empty');

    const base = await this.defaultAgent();
    const model = String(base.model || '').trim();
    const environmentName = String(
      process.env.ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT || '',
    ).trim();
    if (!model || !environmentName) {
      throw new Error(
        'cold test Agent requires the deployment-proven default model and ' +
          'ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT',
      );
    }
    const models = await this.listEnvironmentModels(environmentName);
    if (!models.includes(model)) {
      throw new Error(
        `cold test Environment ${JSON.stringify(environmentName)} does not expose ` +
          `the deployment-proven model ${JSON.stringify(model)}; have: ${models.join(', ')}`,
      );
    }
    return this.createAgent({
      name: resolvedName,
      model,
      environment_name: environmentName,
      prewarm_enabled: false,
    });
  }

  /** Reuse a deployed Agent's proven route when a test creates another Agent. */
  async configuredAgentModel(agentName: string, environmentName: string): Promise<string> {
    const name = String(agentName || '').trim();
    const environment = String(environmentName || '').trim();
    if (!name || !environment) {
      throw new Error('configured Agent model requires an Agent and Environment name');
    }
    const agent = await this.defaultAgent(name);
    const model = String(agent.model || '').trim();
    if (!model) throw new Error(`agent ${JSON.stringify(name)} has no model route`);
    const models = await this.listEnvironmentModels(environment);
    if (!models.includes(model)) {
      throw new Error(
        `agent ${JSON.stringify(name)} model ${JSON.stringify(model)} is not exposed by `
        + `Environment ${JSON.stringify(environment)}; have: ${models.join(', ')}`,
      );
    }
    return model;
  }

  startConversation(agentId: string): Promise<SessionCreated> {
    return this.data<SessionCreated>('POST', `/agents/${agentId}/conversations`);
  }

  getSession(sessionId: string): Promise<SessionRecord> {
    return this.data<SessionRecord>('GET', `/sessions/${sessionId}`);
  }

  // ── assistants ─────────────────────────────────────────────────────────────

  createAssistant(payload: {
    display_name: string;
    environment_name: string;
    [key: string]: unknown;
  }): Promise<AssistantRecord> {
    return this.data<AssistantRecord>('POST', '/assistants', payload);
  }

  getAssistant(assistantId: string): Promise<AssistantRecord> {
    return this.data<AssistantRecord>('GET', `/assistants/${assistantId}`);
  }

  deleteAssistant(assistantId: string): Promise<unknown> {
    return this.data<unknown>('DELETE', `/assistants/${assistantId}`, undefined, 60_000);
  }

  /**
   * The first enabled environment running the resident `assistant` engine.
   *
   * An Assistant cannot borrow an Agent's environment: the engine is a property
   * of the environment, and `POST /assistants` rejects `claude_code` outright
   * ("assistants run the resident assistant engine"). Selecting by engine
   * rather than by name keeps this working on any deployment that has one, and
   * says what is missing on one that does not.
   */
  async assistantEnvironmentName(): Promise<string> {
    const environments = await this.data<Array<Record<string, unknown>>>(
      'GET',
      '/admin/environments',
    );
    const match = (environments || []).find(
      (e) => String(e.engine_kind || '').trim() === 'assistant' && e.enabled !== false,
    );
    if (!match) {
      throw new Error(
        // `enabled` is listed because it is what disqualifies a candidate here:
        // a message that shows the engine alone reads as self-contradictory when
        // a disabled assistant environment exists, and sends the reader after
        // the matching logic instead of at the row.
        'no enabled environment runs the assistant engine; an Assistant cannot use an ' +
          `agent environment. Have: ${(environments || [])
            .map((e) => `${e.name}(engine=${e.engine_kind}, enabled=${e.enabled})`)
            .join(', ')}`,
      );
    }
    return String(match.name || '');
  }

  /** The OpenAI Chat model proven against the dedicated Assistant environment. */
  async assistantModelName(environmentName: string): Promise<string> {
    const environment = String(environmentName || '').trim();
    const model = String(process.env.ASTRABOX_E2E_ASSISTANT_MODEL || '').trim();
    if (!environment || !model) {
      throw new Error(
        'Assistant fixture requires an Environment and ASTRABOX_E2E_ASSISTANT_MODEL',
      );
    }
    const models = await this.listEnvironmentModels(environment);
    if (!models.includes(model)) {
      throw new Error(
        `Assistant Environment ${JSON.stringify(environment)} does not expose the `
        + `configured model ${JSON.stringify(model)}; have: ${models.join(', ')}`,
      );
    }
    return model;
  }

  /** Materialize the workspace's box. Asynchronous — see waitForWorkspaceReady. */
  wakeWorkspace(assistantId: string): Promise<WorkspaceState> {
    return this.data<WorkspaceState>(
      'POST',
      `/assistants/${assistantId}/workspace/wake`,
      undefined,
      120_000,
    );
  }

  /** Commit the workspace to storage and release its box. */
  hibernateWorkspace(assistantId: string): Promise<WorkspaceState> {
    // The commit is synchronous in this call and takes tens of seconds on a
    // real backend, so this needs a lifecycle-sized budget rather than the
    // ordinary API one.
    return this.data<WorkspaceState>(
      'POST',
      `/assistants/${assistantId}/workspace/hibernate`,
      undefined,
      parseTimeoutEnv('ASTRABOX_E2E_WORKSPACE_HIBERNATE_TIMEOUT_MS', 240_000),
    );
  }

  startAssistantConversation(assistantId: string): Promise<SessionCreated> {
    return this.data<SessionCreated>('POST', `/assistants/${assistantId}/conversations`);
  }

  /**
   * Poll wake until the workspace is READY, and return the state that said so.
   *
   * Waking again IS the poll: there is no workspace GET, and wake is written to
   * be re-entrant — it reports READY when the box is up, re-drives a
   * materialization whose bootstrap died, and releases a committed old box
   * whose destruction was interrupted. A wake that
   * keeps answering MATERIALIZING is a bootstrap still running, not a stall.
   *
   * RECOVERY_REQUIRED is not a terminal answer for the same reason: wake IS the
   * destruction retry, so it is polled through rather than failed on. What ends
   * the wait unsuccessfully is the deadline, and the message carries the last
   * state and the sandbox id so a failure names something diagnosable.
   */
  async waitForWorkspaceReady(
    assistantId: string,
    timeoutMs = parseTimeoutEnv('ASTRABOX_E2E_WORKSPACE_READY_TIMEOUT_MS', 300_000),
  ): Promise<WorkspaceState> {
    const deadline = Date.now() + timeoutMs;
    let last: WorkspaceState | null = null;
    let lastError: unknown = null;
    while (Date.now() < deadline) {
      try {
        last = await this.wakeWorkspace(assistantId);
        lastError = null;
        if (String(last.state || '') === 'READY' && last.current_sandbox_id) {
          return last;
        }
      } catch (error) {
        // A poll that fails to reach the API is not an answer about the
        // workspace. Waking a parked box takes tens of seconds, and a hop in
        // front of the server answered one of these polls 502 while the server
        // itself answered every wake it saw 200 and the box came up four
        // seconds later — one lost poll ended a wait that was about to
        // succeed. The deadline is what fails this wait, as above; a failed
        // poll is carried into its message so the failure still names what
        // went wrong.
        lastError = error;
      }
      await new Promise((r) => setTimeout(r, 3_000));
    }
    throw new Error(
      `assistant ${assistantId} workspace not READY after ${timeoutMs}ms ` +
        `(state=${last?.state} sandbox=${last?.current_sandbox_id ?? null} ` +
        `provisioning_session=${last?.provisioning_session_id ?? null}` +
        (lastError ? `; last poll failed: ${String(lastError).slice(0, 200)}` : '') +
        ')',
    );
  }

  getMessages(sessionId: string, limit = 50): Promise<MessagePage> {
    return this.data<MessagePage>('GET', `/sessions/${sessionId}/messages?limit=${limit}`);
  }

  /** The folded read the console pages through. `before` is a `next_cursor`. */
  getHistoryBlocks(
    sessionId: string,
    limit = 50,
    before?: string | null,
  ): Promise<HistoryBlockPage> {
    const cursor = before ? `&before=${encodeURIComponent(before)}` : '';
    return this.data<HistoryBlockPage>(
      'GET',
      `/sessions/${sessionId}/history-blocks?limit=${limit}${cursor}`,
    );
  }

  listChildRuns(sessionId: string): Promise<ChildRunPage> {
    return this.data<ChildRunPage>('GET', `/sessions/${sessionId}/child-runs`);
  }

  getChildRunMessages(sessionId: string, childRunId: string): Promise<ChildRunMessagePage> {
    return this.data<ChildRunMessagePage>(
      'GET',
      `/sessions/${sessionId}/child-runs/${encodeURIComponent(childRunId)}/messages`,
    );
  }

  async waitForChildRuns(
    sessionId: string,
    matches: (childRuns: ChildRunRecord[]) => boolean,
    timeoutMs = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000),
  ): Promise<ChildRunRecord[]> {
    const deadline = Date.now() + timeoutMs;
    let last: ChildRunRecord[] = [];
    while (Date.now() < deadline) {
      last = (await this.listChildRuns(sessionId)).child_runs;
      if (matches(last)) return last;
      await new Promise((resolve) => setTimeout(resolve, 1_500));
    }
    throw new Error(
      `session ${sessionId} child-run projection did not match within ${timeoutMs}ms: ` +
        JSON.stringify(last.map((row) => ({
          child_run_id: row.child_run_id,
          parent_child_run_id: row.parent_child_run_id,
          engine_event: row.engine_event,
          engine_status: row.engine_status,
          engine_reason: row.engine_reason,
          closed: row.closed,
          operations: row.operations,
        }))),
    );
  }

  deleteSession(sessionId: string): Promise<unknown> {
    return this.data<unknown>('DELETE', `/sessions/${sessionId}`, undefined, 30_000);
  }

  interruptSession(sessionId: string): Promise<unknown> {
    return this.data<unknown>('POST', `/sessions/${sessionId}/interrupt`, undefined, 20_000);
  }

  terminateSandbox(sessionId: string): Promise<SandboxReclaimResult> {
    return this.data<SandboxReclaimResult>(
      'POST',
      `/sessions/${sessionId}/sandbox/terminate`,
      undefined,
      60_000,
    );
  }

  async waitForSessionReady(
    sessionId: string,
    timeoutMs = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 180_000),
  ): Promise<SessionRecord> {
    const deadline = Date.now() + timeoutMs;
    let last: SessionRecord | null = null;
    while (Date.now() < deadline) {
      last = await this.getSession(sessionId);
      const state = String(last.state || '');
      if (state === 'READY' && last.sandbox_id) {
        return last;
      }
      if (state === 'FAILED' || state === 'TERMINATED' || state === 'DELETED') {
        throw new Error(
          `session ${sessionId} reached terminal state ${state} while waiting for READY` +
            (last.last_error ? `: ${last.last_error}` : ''),
        );
      }
      await new Promise((r) => setTimeout(r, 2_000));
    }
    throw new Error(
      `session ${sessionId} not READY after ${timeoutMs}ms (state=${last?.state} sandbox=${last?.sandbox_id})`,
    );
  }

  /**
   * Drive one turn over the ai-stream SSE endpoint and collect the frame tail.
   * This supports API-focused specs and UI specs that need a second,
   * out-of-band turn.
   */
  /**
   * Admit a turn and return, without reading its stream.
   *
   * `sendTurn` reads the SSE body to completion, so a turn that pauses on a
   * tool permission blocks the caller until somebody answers it — which makes
   * it useless for nudging a model that did NOT reach for a tool. This is the
   * product's own separation: `turn-inputs` answers with a receipt and nothing
   * about the turn's lifetime, so the caller can go on to watch for the state
   * it actually wants.
   */
  async postTurnInput(sessionId: string, content: string): Promise<void> {
    const response = await this.request.fetch(
      apiPath(`/sessions/${sessionId}/turn-inputs`),
      {
        method: 'POST',
        data: { content, client_message_id: randomUUID() },
        timeout: 30_000,
      },
    );
    if (!response.ok()) {
      throw new Error(
        `turn-inputs -> ${response.status()}: ${(await response.text()).slice(0, 500)}`,
      );
    }
  }

  async sendTurn(
    sessionId: string,
    content: string,
    timeoutMs = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 150_000),
  ): Promise<TurnResult> {
    const started = Date.now();
    const response = await this.request.fetch(apiPath(`/sessions/${sessionId}/ai-stream`), {
      method: 'POST',
      data: { content, client_message_id: randomUUID() },
      headers: { Accept: 'text/event-stream' },
      timeout: timeoutMs,
    });
    if (!response.ok()) {
      throw new Error(`ai-stream -> ${response.status()}: ${(await response.text()).slice(0, 500)}`);
    }
    const body = await response.text();
    const result: TurnResult = { text: '', frameTypes: [], errorText: null, totalMs: Date.now() - started };
    for (const rawLine of body.split('\n')) {
      const line = rawLine.trim();
      if (!line.startsWith('data:')) continue;
      const payload = line.slice(5).trim();
      if (!payload || payload === '[DONE]') continue;
      let frame: Record<string, unknown>;
      try {
        frame = JSON.parse(payload) as Record<string, unknown>;
      } catch {
        continue;
      }
      const type = String(frame.type || '');
      result.frameTypes.push(type);
      if (type === 'text-delta' && typeof frame.delta === 'string') {
        result.text += frame.delta;
      } else if (type === 'error') {
        result.errorText = String(frame.errorText ?? 'unknown error');
      }
    }
    return result;
  }

  /** Parse an SSE body's `data:` frames into type list / text / first error. */
  private parseFrames(raw: string): { frameTypes: string[]; text: string; errorText: string | null } {
    const frameTypes: string[] = [];
    let text = '';
    let errorText: string | null = null;
    for (const rawLine of raw.split('\n')) {
      const line = rawLine.trim();
      if (!line.startsWith('data:')) continue;
      const payload = line.slice(5).trim();
      if (!payload || payload === '[DONE]') continue;
      let frame: Record<string, unknown>;
      try {
        frame = JSON.parse(payload) as Record<string, unknown>;
      } catch {
        continue;
      }
      const type = String(frame.type || '');
      frameTypes.push(type);
      if (type === 'text-delta' && typeof frame.delta === 'string') {
        text += frame.delta;
      } else if (type === 'error') {
        errorText = String(frame.errorText ?? 'unknown error');
      }
    }
    return { frameTypes, text, errorText };
  }

  // ── Agents (get / create / delete + environment model catalog) ──────────

  getAgent(agentId: string): Promise<AgentRecord> {
    return this.data<AgentRecord>('GET', `/agents/${agentId}`);
  }

  /**
   * Create one Agent through POST /api/v1/agents. The community
   * validator (agent_schema.validate_agent_payload) requires non-blank `name`,
   * `model`, and `environment_name`, so a throwaway agent needs at least
   * `{ name, model, environment_name }` — source a valid `model` from
   * `listEnvironmentModels(environment_name)`. Returns the sanitized agent doc.
   */
  async createAgent(config: Record<string, unknown>): Promise<AgentRecord> {
    const requestedAccess = agentAccessPolicy(config);
    const created = await this.data<AgentRecord>(
      'POST',
      '/agents',
      await this.projectAgentAuthoringConfig(config, false),
      parseTimeoutEnv('ASTRABOX_E2E_AGENT_CREATE_API_TIMEOUT_MS', 120_000),
    );
    const access = await this.setAgentAccess(created.agent_id, requestedAccess);
    return { ...created, ...access };
  }

  /** Update one Agent with its optimistic-concurrency version. */
  async updateAgent(agentId: string, config: Record<string, unknown>): Promise<AgentRecord> {
    return this.data<AgentRecord>(
      'PUT',
      `/agents/${agentId}`,
      await this.projectAgentAuthoringConfig(config, true),
      parseTimeoutEnv('ASTRABOX_E2E_AGENT_CREATE_API_TIMEOUT_MS', 120_000),
    );
  }

  getAgentAccess(agentId: string): Promise<AgentAccessPolicy> {
    return this.data<AgentAccessPolicy>('GET', `/agents/${agentId}/access`);
  }

  setAgentAccess(agentId: string, access: AgentAccessPolicy): Promise<AgentAccessPolicy> {
    return this.data<AgentAccessPolicy>('PUT', `/agents/${agentId}/access`, access);
  }

  /** Soft-delete an agent (DELETE /api/v1/agents/{id}); returns the doc with state=DELETED. */
  deleteAgent(agentId: string): Promise<AgentRecord> {
    return this.data<AgentRecord>('DELETE', `/agents/${agentId}`, undefined, 60_000);
  }

  /** Hibernate an Agent. */
  hibernateAgent(agentId: string): Promise<AgentRecord> {
    return this.data<AgentRecord>('POST', `/agents/${agentId}/hibernate`);
  }

  /** Wake an Agent. */
  wakeAgent(agentId: string): Promise<AgentRecord> {
    return this.data<AgentRecord>('POST', `/agents/${agentId}/wake`);
  }

  /** Model ids the environment's gateway offers (GET /admin/environments/{name}/models). */
  async listEnvironmentModels(environmentName: string): Promise<string[]> {
    const d = await this.data<{ models?: string[] }>(
      'GET',
      `/admin/environments/${encodeURIComponent(environmentName)}/models`,
    );
    return d.models ?? [];
  }

  // ── Permission mode ─────────────────────────────────────────────────────

  /**
   * Change a conversation's permission mode (POST /sessions/{id}/permission-mode).
   * Community conversations are created with `bypassPermissions` (the create route
   * hardcodes it — there is NO permission_mode on POST /conversations), so a spec
   * that needs a tool-permission interaction to surface must switch to `default`
   * here (or pass permissionMode per-turn to `streamPrompt`) BEFORE that turn.
   */
  setPermissionMode(sessionId: string, permissionMode: PermissionMode | string): Promise<PermissionModeResult> {
    return this.data<PermissionModeResult>(
      'POST',
      `/sessions/${sessionId}/permission-mode`,
      { permission_mode: permissionMode },
    );
  }

  // ── Pending interactions (list / answer / stop) ─────────────────────────

  /** The session's currently-pending interaction, or null (read from GET /sessions/{id}). */
  async getPendingInteraction(sessionId: string): Promise<PendingInteraction | null> {
    const session = await this.getSession(sessionId);
    const pending = session.pending_interaction;
    if (pending && String(pending.interaction_id || '').trim()) {
      return pending;
    }
    return null;
  }

  /** Poll until a pending interaction appears or fail at the fixed timeout. */
  async waitForPendingInteraction(
    sessionId: string,
    timeoutMs = parseTimeoutEnv('ASTRABOX_E2E_PENDING_TIMEOUT_MS', 180_000),
  ): Promise<PendingInteraction> {
    const deadline = Date.now() + timeoutMs;
    let last: PendingInteraction | null = null;
    while (Date.now() < deadline) {
      last = await this.getPendingInteraction(sessionId);
      if (last) return last;
      await new Promise((r) => setTimeout(r, 1_000));
    }
    throw new Error(`session ${sessionId} did not expose a pending interaction within ${timeoutMs}ms`);
  }

  /**
   * Wait for a pending interaction, or for the turn to end without one.
   *
   * `waitForPendingInteraction` can only time out, so a model that answered
   * without reaching for a gated tool costs the caller its whole probe budget
   * before it learns anything — long enough that asking again does not fit
   * inside the lane's per-test wall. A turn that has SETTLED with no
   * interaction is the answer already: the model is not going to gate a tool it
   * has stopped working on. Returning null there turns a several-minute silence
   * into a verdict in the seconds the turn actually took.
   */
  async waitForPendingInteractionOrSettledTurn(
    sessionId: string,
    timeoutMs: number,
  ): Promise<PendingInteraction | null> {
    const deadline = Date.now() + timeoutMs;
    let sawTurn = false;
    while (Date.now() < deadline) {
      const pending = await this.getPendingInteraction(sessionId);
      if (pending) return pending;
      const session = await this.getSession(sessionId);
      const turnId = String(session.current_turn_id || '').trim();
      if (turnId) {
        sawTurn = true;
      } else if (sawTurn) {
        // It ran and it is over, and nothing was gated.
        return null;
      }
      await new Promise((r) => setTimeout(r, 1_000));
    }
    return null;
  }

  /**
   * Answer a pending interaction (POST /sessions/{id}/interaction-respond). `answer`
   * is the raw interaction_response object — e.g. `{ decision: 'approve' }` /
   * `{ decision: 'reject' }` for a tool-permission prompt, `{ decline: true }` for
   * AskUserQuestion, `{ decision: 'approve'|'revise'|'reject' }` for ExitPlanMode.
   */
  answerPendingInteraction(
    sessionId: string,
    interactionId: string,
    answer: Record<string, unknown>,
  ): Promise<InteractionAnswerResult> {
    return this.data<InteractionAnswerResult>(
      'POST',
      `/sessions/${sessionId}/interaction-respond`,
      { interaction_id: interactionId, answer },
    );
  }

  /** Approve the pending tool-permission interaction (decision:'approve'). Resolves interaction_id from the session when not supplied. */
  async approvePendingInteraction(
    sessionId: string,
    opts: { interactionId?: string; comment?: string; suggestionId?: string } = {},
  ): Promise<InteractionAnswerResult> {
    const interactionId = opts.interactionId ?? (await this.waitForPendingInteraction(sessionId)).interaction_id;
    const answer: Record<string, unknown> = { decision: 'approve' };
    if (opts.comment) answer.comment = opts.comment;
    if (opts.suggestionId) answer.suggestion_id = opts.suggestionId;
    return this.answerPendingInteraction(sessionId, interactionId, answer);
  }

  /** Reject the pending tool-permission interaction (decision:'reject') — the "stop this tool use" answer. */
  async denyPendingInteraction(
    sessionId: string,
    opts: { interactionId?: string; comment?: string } = {},
  ): Promise<InteractionAnswerResult> {
    const interactionId = opts.interactionId ?? (await this.waitForPendingInteraction(sessionId)).interaction_id;
    const answer: Record<string, unknown> = { decision: 'reject' };
    if (opts.comment) answer.comment = opts.comment;
    return this.answerPendingInteraction(sessionId, interactionId, answer);
  }

  // ── Turns (raw stream / replay-resume / terminal) ───────────────────────

  /**
   * Drive one turn over POST /sessions/{id}/ai-stream and return the RAW SSE body.
   * `permissionMode` applies to THIS turn only. Use `sendTurn` for a parsed
   * frame tail.
   */
  async streamPrompt(
    sessionId: string,
    content: string,
    permissionMode?: string,
    timeoutMs = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000),
  ): Promise<string> {
    const response = await this.request.fetch(apiPath(`/sessions/${sessionId}/ai-stream`), {
      method: 'POST',
      data: {
        content,
        client_message_id: randomUUID(),
        ...(permissionMode ? { permission_mode: permissionMode } : {}),
      },
      headers: { Accept: 'text/event-stream' },
      timeout: timeoutMs,
    });
    const raw = await response.text();
    if (!response.ok()) {
      throw new Error(`ai-stream -> ${response.status()}: ${raw.slice(0, 500)}`);
    }
    return raw;
  }

  /**
   * Replay/resume a turn's durable frames (GET /sessions/{id}/ai-stream?after_seq=N).
   * The replay cursor is the `frame_seq` from the messages page
   * `active_turn_overlay.resume_cursor`; passing the last-applied seq replays only
   * newer frames. Returns `{ status: 204, replayed: false }` when nothing is
   * resumable — the endpoint's no-active-stream signal.
   */
  async resumeStream(
    sessionId: string,
    afterSeq = -1,
    timeoutMs = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 60_000),
  ): Promise<ResumeStreamResult> {
    const response = await this.request.fetch(
      apiPath(`/sessions/${sessionId}/ai-stream?after_seq=${afterSeq}`),
      { method: 'GET', headers: { Accept: 'text/event-stream' }, timeout: timeoutMs },
    );
    const status = response.status();
    if (status === 204) {
      return { status, replayed: false, frameTypes: [], text: '', errorText: null, raw: '' };
    }
    const raw = await response.text();
    if (!response.ok()) {
      throw new Error(`resume ai-stream -> ${status}: ${raw.slice(0, 500)}`);
    }
    return { status, replayed: true, raw, ...this.parseFrames(raw) };
  }

  /** Run one terminal command (POST /sessions/{id}/terminal/stream); returns raw SSE. For filesystem probes/assertions inside the sandbox. */
  async runTerminalCommand(
    sessionId: string,
    command: string,
    cwd?: string,
    timeoutMs = parseTimeoutEnv('ASTRABOX_E2E_TERMINAL_TIMEOUT_MS', 60_000),
  ): Promise<string> {
    const response = await this.request.fetch(apiPath(`/sessions/${sessionId}/terminal/stream`), {
      method: 'POST',
      data: { command, ...(cwd ? { working_directory: cwd } : {}) },
      headers: { Accept: 'text/event-stream' },
      timeout: timeoutMs,
    });
    const raw = await response.text();
    if (!response.ok()) {
      throw new Error(`terminal/stream -> ${response.status()}: ${raw.slice(0, 500)}`);
    }
    return raw;
  }

  // ── Conversation lifecycle ──────────────────────────────────────────────

  endConversation(sessionId: string): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>('POST', `/sessions/${sessionId}/conversation/end`, undefined, 30_000);
  }

  archiveSession(sessionId: string): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>('POST', `/sessions/${sessionId}/archive`, undefined, 30_000);
  }

  recoverSession(sessionId: string): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>('POST', `/sessions/${sessionId}/recover`, undefined, 30_000);
  }

  stopChildRun(sessionId: string, childRunId: string): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>(
      'POST',
      `/sessions/${sessionId}/child-runs/${encodeURIComponent(childRunId)}/stop`,
    );
  }

  // ── Messages ────────────────────────────────────────────────────────────

  /** Count assistant messages currently visible on the first messages page. */
  async assistantCount(sessionId: string): Promise<number> {
    const page = await this.getMessages(sessionId, 50);
    return visibleMessages(page).filter((m) => m.role === 'assistant').length;
  }

  /**
   * Wait until the assistant-message count EXCEEDS `minAssistantCount` (pass the
   * pre-turn count to wait for the next reply) and return the newest assistant
   * message.
   */
  async waitForAssistantMessageCount(
    sessionId: string,
    minAssistantCount: number,
    timeoutMs = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000),
  ): Promise<MessageRecord> {
    return this.waitForAssistantMessageMatching(
      sessionId,
      minAssistantCount,
      () => true,
      timeoutMs,
    );
  }

  /** Wait for a new assistant message whose current projection satisfies `matches`. */
  async waitForAssistantMessageMatching(
    sessionId: string,
    minAssistantCount: number,
    matches: (message: MessageRecord) => boolean,
    timeoutMs = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000),
  ): Promise<MessageRecord> {
    const deadline = Date.now() + timeoutMs;
    let lastMessages: MessageRecord[] = [];
    while (Date.now() < deadline) {
      const page = await this.getMessages(sessionId, 50);
      lastMessages = visibleMessages(page);
      const assistants = lastMessages.filter((m) => m.role === 'assistant');
      if (assistants.length > minAssistantCount) {
        for (let index = assistants.length - 1; index >= minAssistantCount; index -= 1) {
          const candidate = assistants[index];
          if (candidate && matches(candidate)) return candidate;
        }
      }
      await new Promise((r) => setTimeout(r, 1_500));
    }
    const assistants = lastMessages.filter((m) => m.role === 'assistant');
    throw new Error(
      `session ${sessionId} did not expose a matching assistant message after count ` +
        `${minAssistantCount} within ${timeoutMs}ms; have ${assistants.length}; ` +
        `new assistant text=${JSON.stringify(assistants.slice(minAssistantCount).map(messageText))}`,
    );
  }

  // ── Admin (session detail / runtime eviction / export / JSONL) ──────────

  /** Operator detail for runtime fault injection and private identity assertions. */
  adminSessionDetail(sessionId: string): Promise<AdminSessionRecord> {
    return this.data<AdminSessionRecord>('GET', `/admin/sessions/${sessionId}/detail`);
  }

  /** Evict the cached in-memory runtime for a session (POST /admin/sessions/{id}/evict-runtime) — simulates the next request landing on a cold process. */
  adminEvictRuntime(sessionId: string): Promise<{ evicted: string }> {
    return this.data<{ evicted: string }>('POST', `/admin/sessions/${sessionId}/evict-runtime`);
  }

  /** Provider-backed sandbox detail used for lifecycle truth assertions. */
  getSandbox(sandboxId: string): Promise<Record<string, unknown>> {
    return this.data<Record<string, unknown>>(
      'GET',
      `/admin/sandboxes/${encodeURIComponent(sandboxId)}`,
    );
  }

  /**
   * The session transcript in the SDK's own format (GET /admin/sessions/{id}/transcript).
   * A raw attachment, not the `{ data }` envelope: JSONL for a session with only
   * its main conversation, tar.gz when it also has subagent transcripts.
   */
  async adminSessionTranscript(sessionId: string, timeoutMs = 60_000): Promise<string> {
    const response = await this.request.fetch(apiPath(`/admin/sessions/${sessionId}/transcript`), {
      method: 'GET',
      timeout: timeoutMs,
    });
    const raw = await response.text();
    if (!response.ok()) {
      throw new Error(`session transcript -> ${response.status()}: ${raw.slice(0, 500)}`);
    }
    return raw;
  }

  /**
   * Batch-download all sessions' JSONL for a template/agent as one tar.gz
   * (GET /admin/batch-jsonl). `templateName` is the REQUIRED query param (the
   * community route still keys the export by the session's `template_name`, which
   * for an agent conversation is the agent's name).
   */
  /**
   * Write one text file into the sandbox (POST /sessions/{id}/files/upload).
   *
   * A deterministic way to place a marker a later assertion can look for. The
   * alternative — asking the model to write it — makes the artifact depend on
   * the model choosing to call a tool, so a durability assertion built on it
   * would fail for a reason that has nothing to do with durability.
   */
  async uploadFileText(
    sessionId: string,
    directory: string,
    fileName: string,
    content: string,
    timeoutMs = 60_000,
  ): Promise<void> {
    const response = await this.request.fetch(apiPath(`/sessions/${sessionId}/files/upload`), {
      method: 'POST',
      multipart: {
        path: directory,
        files: { name: fileName, mimeType: 'text/plain', buffer: Buffer.from(content, 'utf-8') },
      },
      timeout: timeoutMs,
    });
    if (!response.ok()) {
      throw new Error(
        `files/upload -> ${response.status()}: ${(await response.text()).slice(0, 500)}`,
      );
    }
  }

  /** Download a sandbox file as text (GET /sessions/{id}/files/download?path=). */
  async downloadFileText(sessionId: string, path: string, timeoutMs = 60_000): Promise<string> {
    const response = await this.request.fetch(
      apiPath(`/sessions/${sessionId}/files/download?path=${encodeURIComponent(path)}`),
      { method: 'GET', timeout: timeoutMs },
    );
    const raw = await response.text();
    if (!response.ok()) {
      throw new Error(`files/download -> ${response.status()}: ${raw.slice(0, 500)}`);
    }
    return raw;
  }

  // ── Session waiters (state / predicate) ─────────────────────────────────

  async waitForSessionState(
    sessionId: string,
    expected: string,
    timeoutMs = parseTimeoutEnv('ASTRABOX_E2E_SESSION_TIMEOUT_MS', 180_000),
  ): Promise<SessionRecord> {
    return this.waitForSession(sessionId, (s) => String(s.state || '') === expected, timeoutMs);
  }

  async waitForSession(
    sessionId: string,
    predicate: (s: SessionRecord) => boolean,
    timeoutMs = parseTimeoutEnv('ASTRABOX_E2E_SESSION_TIMEOUT_MS', 180_000),
  ): Promise<SessionRecord> {
    const deadline = Date.now() + timeoutMs;
    let last: SessionRecord | null = null;
    while (Date.now() < deadline) {
      last = await this.getSession(sessionId);
      if (predicate(last)) return last;
      await new Promise((r) => setTimeout(r, 1_500));
    }
    throw new Error(
      `session ${sessionId} did not satisfy predicate within ${timeoutMs}ms; last state=${last?.state}`,
    );
  }

  async waitForAdminSession(
    sessionId: string,
    predicate: (s: AdminSessionRecord) => boolean,
    timeoutMs = parseTimeoutEnv('ASTRABOX_E2E_SESSION_TIMEOUT_MS', 180_000),
  ): Promise<AdminSessionRecord> {
    const deadline = Date.now() + timeoutMs;
    let last: AdminSessionRecord | null = null;
    while (Date.now() < deadline) {
      last = await this.adminSessionDetail(sessionId);
      if (predicate(last)) return last;
      await new Promise((r) => setTimeout(r, 1_500));
    }
    throw new Error(
      `admin session ${sessionId} did not satisfy predicate within ${timeoutMs}ms; ` +
        `last state=${last?.state}`,
    );
  }
}
