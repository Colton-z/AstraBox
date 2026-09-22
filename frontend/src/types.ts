import type { components } from './api/schema';

export type AgentPreparedRuntimeStatus = components['schemas']['AgentPreparedRuntimeStatus'];

/**
 * The wire shapes the server declares, re-exported from the generated client.
 *
 * `src/api/schema.d.ts` is generated from the OpenAPI snapshot the backend's own
 * response models produce (`npm run gen:api`; `make check-api-client` fails when
 * the committed file is not what regeneration yields). Naming those shapes here
 * keeps every `@/types` import valid while making the server the author: a field
 * the backend adds, drops or makes nullable moves this file by regeneration
 * rather than by somebody remembering to.
 *
 * The list is what the schema carries AND the console already agreed with. It is
 * not the whole set the schema could supply: a dozen more shapes are declared
 * truthfully upstream and are still written out below, because the truthful
 * version is nullable where a page reads it as a plain string (Vault.vault_id,
 * DeploymentRecord.scene) or carries a field the page's fixtures do not build
 * (SessionFileListing.session_kind, AdminSandboxSummary.endpoint). Each is a
 * page-side fix, not a type-side one, and belongs with whoever owns the page.
 *
 * What stays hand-written for good is what no route describes: the shapes the
 * console builds for itself — content blocks parsed out of tool events, outbox
 * and delivery bookkeeping, the form-schema grammar, file-change tracking — and
 * the payloads a route declares only as an open object, where the console's
 * stronger reading is its own claim rather than the server's.
 */
type Schemas = components['schemas'];

export type UserInfo = Schemas['CurrentUser'];
export type AgentExtensionCatalog = Schemas['AgentExtensionCatalog'];
export type AgentMcpCatalogItem = Schemas['AgentExtensionItem'];
export type AgentSkillCatalogItem = Schemas['AgentExtensionItem'];
export type AdminSandboxSecurity = Schemas['AdminSandboxSecurity'];
export type AdminSandboxDiagnostics = Schemas['AdminSandboxDiagnostics'];
export type AdminSandboxIdleAction = Schemas['AdminSandboxIdleAction'];
export type AdminLogsPage = Schemas['AdminLogsPage'];
export type SessionFileEntry = Schemas['SessionFileEntry'];
export type WebshellResult = Schemas['WebshellAccess'];
export type SandboxTerminateResult = Schemas['SandboxTermination'];
export type ConversationEndResult = Schemas['ConversationEndResult'];
export type BackgroundTaskState = Schemas['BackgroundTaskState'];
export type AgentRuntimeView = Schemas['AgentRuntimeView'];

export type SessionState =
  | 'CREATING'
  | 'READY'
  | 'BACKGROUND_RUNNING'
  | 'WAITING_INPUT'
  | 'SENDING'
  | 'BUSY'
  | 'PROCESSING'
  | 'INTERRUPTING'
  | 'TERMINATING'
  | 'TERMINATED'
  | 'RECOVERY_REQUIRED'
  | 'DELETED';

/** Engine-owned permission-mode name, carried verbatim from its manifest. */
export type PermissionMode = string;

export type RecoveryPolicy = 'auto' | 'manual';

export interface ApiResponse<T> {
  code: string;
  message: string;
  data: T;
  error?: ApiErrorEnvelope;
}

export interface ApiErrorEnvelope {
  code: string;
  category?: string;
  retryable?: boolean;
  owner?: string;
  user_message?: string;
  debug_message?: string;
  cause_code?: string;
  evidence?: Record<string, unknown>;
}

// The editable body of an Agent. The authoritative field list is the server's
// (`agent_schema.py`), which the form reads at runtime from
// `GET /api/v1/admin/agent-schema`; the named fields here are only the ones this
// code reads directly, and the index signature carries the rest untouched.
export interface AgentDraft {
  name: string;
  // One line on what this agent is for. It is the only field that tells two
  // agents apart at a glance, so the pick-an-agent cards read it.
  description?: string | null;
  enabled?: boolean;
  // First-class Agent fields; there is no separate template resource.
  model?: string;
  environment_name?: string;
  system?: string;
  display_meta?: Record<string, unknown>;
  skills?: string[];
  mcp_servers?: Record<string, unknown>;
  engine_options?: Record<string, unknown>;
  default_repo?: Record<string, unknown>;
  plugin_repos?: Record<string, unknown>[];
  // Access control (per-agent). created_by is server-stamped (user id) and read-only.
  created_by?: string;
  admins?: string[];
  visibility?: 'public' | 'private' | 'allowlist';
  allowed_user_ids?: string[];
  [key: string]: unknown;
}

/** The complete policy owned by the dedicated Agent access endpoint. */
export interface AgentAccess {
  created_by?: string;
  visibility: 'public' | 'private' | 'allowlist';
  admins: string[];
  allowed_user_ids: string[];
}

export type AgentAccessPolicy = Omit<AgentAccess, 'created_by'>;

// A STORED Agent, as `GET /api/v1/agents` returns it. `agent_id` is the identity
// every write addresses (never the name); `version` is the optimistic-concurrency
// stamp the server bumps whenever a runtime-affecting Agent field changes.
export interface AgentConfig extends AgentDraft {
  agent_id: string;
  can_manage?: boolean;
  version?: number;
  state?: string;
  updated_at?: string;
}

/** Non-secret metadata for one saved credential. */
export interface VaultCredentialSummary {
  credential_id: string;
  vault_id: string;
  display_name?: string | null;
  auth: {
    type: 'mcp_oauth' | 'mcp_static_header' | 'static_bearer' | 'environment_variable' | 'http_basic';
    url?: string;
    username?: string;
    mcp_server_url?: string;
    header_name?: string;
    secret_name?: string;
    expires_at?: string | null;
    networking?: {
      type?: 'limited' | 'unrestricted';
      allowed_hosts?: string[];
    };
    injection_location?: { header?: boolean; body?: boolean };
  };
  archived_at?: string | null;
}

/** An administrator-managed group of credentials bound to runtimes. */
export interface VaultSummary {
  vault_id: string;
  display_name: string;
  metadata?: Record<string, unknown>;
  archived_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  credentials?: VaultCredentialSummary[];
}

export interface CredentialDeliveryOverview {
  deployment_mode: 'local_development' | 'trusted_private' | 'team';
  model_credentials: 'egress_placeholder' | 'sandbox_environment';
  mcp_credentials: 'egress_injection' | 'unavailable';
  environment_credentials: 'egress_placeholder' | 'unavailable';
}

export interface VaultCatalog {
  vaults: VaultSummary[];
  credential_delivery: CredentialDeliveryOverview;
}

export interface CredentialBinding {
  target_type: 'agent' | 'assistant';
  target_id: string;
  vault_ids: string[];
  vaults: VaultSummary[];
}

export interface VaultBindingHolder {
  target_type: 'agent' | 'assistant';
  target_id: string;
  target_name: string;
  vault_ids: string[];
}

/** One API key an MCP client authenticates with. `secret` appears in the
 *  issuing response only — the list carries no secret field at all, so a
 *  reader cannot mistake an absent one for an empty one. */
export interface McpClientToken {
  token_id: string;
  name: string;
  scope: 'read' | 'converse';
  expires_at?: string | null;
  created_at?: string | null;
  last_used_at?: string | null;
}

export interface IssuedMcpClientToken extends McpClientToken {
  secret: string;
}

export interface ChannelFieldDescriptor {
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

export interface ChannelProviderDescriptor {
  name: string;
  label: string;
  scene: `channel:${string}`;
  config_fields: ChannelFieldDescriptor[];
  credential_fields: ChannelFieldDescriptor[];
  callback_path?: string | null;
  setup_url?: string | null;
  documentation_url?: string | null;
  supports_source: boolean;
  uses_trigger_secret: boolean;
}

// A Deployment is one internal or external trigger binding on an Agent.
export interface AgentDeployment {
  deployment_id: string;
  agent_id: string;
  agent_name?: string;
  name?: string;
  created_by?: string;
  enabled?: boolean;
  scene: 'hmac' | 'scheduler' | 'schedule' | `channel:${string}`;
  prompt_prefix?: string;
  secret?: string;
  attention_policy?: 'all' | 'mentions';
  channel_config?: {
    [key: string]: unknown;
  };
  credentials_configured?: boolean;
  callback_base_url?: string;
  schedule?: {
    cron: string;
    timezone: string;
  };
  created_at?: string;
  updated_at?: string;
}

export interface DeploymentRun {
  run_id: string;
  deployment_id: string;
  agent_id: string;
  trigger: 'schedule' | 'manual' | 'replay';
  status:
    | 'QUEUED'
    | 'RUNNING'
    | 'WAITING_INPUT'
    | 'COMPLETED'
    | 'FAILED'
    | 'CANCELLED'
    | 'UNKNOWN';
  scheduled_for?: string | null;
  replayed_from_run_id?: string | null;
  session_id?: string | null;
  turn_id?: string | null;
  error?: string | null;
  created_at?: string | null;
}

// Admin-authored runtime environment preset. Open shape (validated server-side
// by environment_schema). Reuses FormSchema/FormFieldSchema for its form.
export interface EnvironmentConfig {
  name: string;
  enabled?: boolean;
  engine_kind?: string;
  engine_available?: boolean;
  supported_session_kinds?: string[];
  sandbox_backend?: string;
  networking?: {
    type: 'unrestricted' | 'limited';
    allowed_hosts?: string[];
    allow_mcp_servers?: boolean;
  };
  // Capability flag a sandbox backend may declare on its environment payload:
  // repos are cloned over https with a platform-injected token, so SSH repo
  // settings have no effect (the form hides them).
  git_over_https?: boolean;
  // The environment's engine declares what an Agent's engine_options bag may
  // hold; the Agent form renders these fields for the chosen environment.
  engine_options_schema?: FormFieldSchema[];
  [key: string]: unknown;
}

// ── Schema-driven forms ─────────────────────────────────────────────────────
// One grammar, two producers: `GET /api/v1/admin/agent-schema` (agent_schema.py)
// and `GET /api/v1/admin/environment-schema` (environment_schema.py).
export type FormFieldType =
  | 'string'
  | 'text'
  | 'boolean'
  | 'integer'
  | 'enum'
  | 'env_ref'
  | 'string_list'
  | 'key_value'
  | 'object'
  | 'object_list';

// Schema = STRUCTURE only. Human-facing copy (label/help/description) is owned by
// the frontend i18n catalogue, keyed by `key`/`id`, so language switching covers
// schema-driven forms too and the backend API never carries UI strings.
export interface FormFieldSchema {
  key: string;
  type: FormFieldType;
  group?: string;
  required?: boolean;
  enum?: string[];
  path?: string;
  complex?: boolean;
  advanced?: boolean;
  item_schema?: FormFieldSchema[];
  // Engine-declared fields (an Agent's engine_options bag) carry their OWN
  // copy: the strings are the engine author's voice, carried verbatim like
  // any other vendor vocabulary — the platform catalogue cannot know a
  // third-party engine's keys. Platform-owned schemas keep copy in i18n.
  label?: string;
  help?: string;
  placeholder?: string;
}

export interface FormSchemaGroup {
  id: string;
}

export interface FormSchema {
  version: number;
  groups: FormSchemaGroup[];
  fields: FormFieldSchema[];
}

export interface MessageRecord {
  session_id: string;
  message_id: string;
  turn_id: string;
  client_message_id?: string | null;
  role: 'user' | 'assistant';
  user_id?: string;
  content: string;
  blocks?: ContentBlock[];
  created_at?: string;
  source_frame_seq_applied?: number | null;
  // Present on the history-block read: the record's identity as that page
  // folded it. The reader's identity for a record stays `role:message_id`.
  history_block_id?: string;
}

export interface DeliveryFailure {
  turn_id: string;
  client_message_id: string;
  text: string;
  summary: string;
}

export interface OutboxItem {
  client_message_id: string;
  text: string;
  status: 'sending' | 'accepted' | 'failed';
  accepted_turn_id?: string;
  command_id?: string;
  input_id?: string;
  failure_reason?: string;
  /**
   * The images this message was sent with. Held because retrying a failed row
   * has to resend the message, and resending only its text would quietly
   * deliver a different message from the one the user wrote.
   */
  images?: TurnInputImage[];
}

/** The image encodings this console can send. Mirrors the server's list. */
export const COMPOSER_IMAGE_MEDIA_TYPES = [
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
] as const;

export type ComposerImageMediaType = (typeof COMPOSER_IMAGE_MEDIA_TYPES)[number];

/** An image on its way out: narrowed, because this console chose it. */
export interface TurnInputImage {
  type: 'image';
  source: { type: 'base64'; media_type: ComposerImageMediaType; data: string };
}

/**
 * An image on its way in. The media type stays open: it is whatever the
 * server sent, and narrowing a value that arrived over the wire without
 * checking it would only make the type a claim nobody verified.
 */
export interface ImageBlockData {
  type: 'image';
  source: { type: 'base64'; media_type: string; data: string };
}

export interface PendingEngineInput {
  command_id: string;
  input_id: string;
  client_message_id: string;
  content: string;
  sequence: number;
  status: 'pending' | 'delivered';
}

export interface SlashCommandDetail {
  name?: string;
  command?: string;
  description?: string | null;
  aliases?: string[];
}

export interface EngineCapabilityManifest {
  engine_kind: string;
  tools: string[];
  input_content_types: string[];
  permission_modes: string[];
  supports_interaction: boolean;
  supports_child_run_control: boolean;
  supports_server_info: boolean;
  extra: Record<string, unknown>;
}

export interface SessionRecord {
  /** Which workspace surfaces this session's Agent declares. Platform's, not the engine's. */
  workspace_panels?: { terminal?: boolean; diff?: boolean } | null;
  session_id: string;
  user_id: string;
  template_name: string;
  state: SessionState;
  permission_mode?: PermissionMode;
  model_name?: string | null;
  slash_commands?: Array<string | SlashCommandDetail>;
  slash_command_details?: SlashCommandDetail[];
  sandbox_id?: string;
  engine_session_key?: string;
  terminal_cwd?: string | null;
  title?: string;
  source_type?: string;
  agent_id?: string;
  agent_runtime?: AgentRuntimeView | null;
  session_kind?: string;
  engine_kind?: string | null;
  /** The live engine client's declaration, carried by session detail without
   *  translating its vocabulary. Null means this API process has no live
   *  client from which to ask; list responses omit the detail-only field. */
  engine_capabilities?: EngineCapabilityManifest | null;
  expires_at?: string;
  created_at?: string;
  updated_at?: string;
  deleted?: boolean;
  runtime_unavailable?: boolean;
  last_error?: string;
  startup_progress?: string;
  current_turn_id?: string | null;
  last_turn_id?: string | null;
  last_turn_status?: 'COMPLETED' | 'FAILED' | null;
  last_turn_error?: string | null;
  last_turn_command_id?: string | null;
  delivery_state?: 'PENDING' | 'RECEIVED' | 'NOT_RECEIVED' | null;
  last_turn_failure_phase?: string | null;
  /** The engine's own account of why its loop ended (the agent SDK's
   *  `terminal_reason`, e.g. completed / max_turns / aborted_tools) —
   *  carried verbatim, so the value set belongs to the engine, not this platform. */
  last_turn_terminal_reason?: string | null;
  recovery_policy?: RecoveryPolicy | null;
  recovery_reason?: string | null;
  delivery_failure?: DeliveryFailure | null;
  background_task_state?: BackgroundTaskState | null;
  partial_response?: {
    turn_id: string;
    text: string;
    chunk_count: number;
    last_seq: number;
    blocks?: ContentBlock[];
  };
  pending_interaction?: PendingInteraction | null;
  pending_inputs?: PendingEngineInput[];
}

export interface SessionListPage {
  sessions: SessionRecord[];
  has_more: boolean;
  next_cursor?: string | null;
}

// ── Admin session console (management surface) ──────────────────────────────
// Shapes mirror /api/v1/admin/sessions/{all,detail,trace}; see the admin session
// endpoints and manage/SessionDetailPage.tsx.
export interface AdminSessionVersions {
  engine_kind?: string | null;
  runtime_identity_status?: string | null;
  bootstrap_transport?: string | null;
  has_local_runtime?: boolean;
  [key: string]: unknown;
}

export interface AdminSessionSummary {
  session_id: string;
  template_name?: string;
  state: string;
  sandbox_id?: string;
  created_at?: string;
  updated_at?: string;
  expires_at?: string;
  duration_seconds?: number;
  last_error?: string;
  has_local_runtime?: boolean;
  user_id: string;
  display_name?: string;
  agent_id?: string;
  title?: string;
  runtime_versions?: AdminSessionVersions;
}

/** The narrowing the console and the export share: one agent, one time window. */
export interface AdminSessionFilters {
  agent_id?: string;
  since?: string;
  until?: string;
}

export interface AdminSessionPage {
  items: AdminSessionSummary[];
  pagination: {
    page: number;
    page_size: number;
    total_items: number;
    total_pages: number;
  };
}

export interface AdminNavigationSummary {
  agents: number;
  environments: number;
  sessions: number;
}

export interface AdminSessionMcpConnection {
  server_name?: string;
  service_code?: string;
  transport?: string;
  connected?: boolean;
}

export interface AdminSessionDetail extends AdminSessionSummary {
  template_skills?: string[];
  template_mcp_servers?: string[];
  template_mcp_config?: unknown;
  mcp_connections?: AdminSessionMcpConnection[];
}

export interface AdminTraceTurn {
  turn_id: string;
  latest_created_at?: string;
  message_count?: number;
}

export interface AdminTraceMessage {
  created_at?: string;
  role?: string;
  turn_id?: string;
  content_preview?: string;
  raw?: unknown;
}

export interface AdminTraceFrame {
  seq?: number;
  type?: string;
  text_preview?: string;
  text?: string;
  [key: string]: unknown;
}

export interface AdminSessionTrace {
  current_turn_id?: string;
  selected_turn_id?: string;
  turns?: AdminTraceTurn[];
  messages?: AdminTraceMessage[];
  frames?: AdminTraceFrame[];
  message_limit?: number;
  frame_limit?: number;
  truncated_frame_count?: number;
}

/**
 * One sandbox as its backend's control plane describes it
 * (`GET /api/v1/admin/sandboxes`). Every field is the backend's own value —
 * nothing here is inferred or defaulted, so a surprising value is the backend's
 * report, not a rendering artifact.
 *
 * `session_id` is the AstraBox session the box was created for, read from its
 * own create metadata. It is null for a box that carries none (one this
 * deployment did not create); no session is ever inferred from the id.
 */
export interface AdminSandboxSummary {
  sandbox_id: string;
  backend: string;
  state: string;
  created_at?: string | null;
  expires_at?: string | null;
  image?: string | null;
  entrypoint?: string[];
  metadata?: Record<string, string>;
  session_id?: string | null;
}

export interface AdminSandboxPagination {
  page: number;
  page_size: number;
  total_items: number;
  total_pages: number;
  has_next_page: boolean;
}

export interface AdminSandboxPage {
  backend: string;
  items: AdminSandboxSummary[];
  pagination: AdminSandboxPagination;
}

/** The four diagnostic reports a backend may be able to render about one box. */
export type AdminSandboxDiagnosticScope = 'summary' | 'inspect' | 'events' | 'logs';

// ── Admin process console (this server, right now) ───────────────────────────
// Everything above describes stored resources. These three describe the PROCESS
// answering the request — its threads, its file descriptors, the runtimes it
// holds open — so every field is a live reading and none of it is persisted.
// Shapes mirror AdminService.admin_{system_overview,process_health,list_errors}.

export interface AdminProcessRuntimeRow {
  session_id: string;
  sandbox_id?: string | null;
  /** `false` while a turn is in flight, `true` once it settled, absent if unknown. */
  current_task_done?: boolean | null;
  watchdog_done?: boolean | null;
  lock_locked?: boolean | null;
  disconnect_set?: boolean | null;
  owner_loop_running?: boolean | null;
}

export interface AdminProcessHealth {
  /** 'ok' | 'warning' | 'error' — the server's own read, not recomputed here. */
  severity?: string;
  machine_id?: string;
  pid?: number;
  captured_at?: string;
  threads?: { count?: number; non_daemon_count?: number; items?: Array<{ name?: string; daemon?: boolean; alive?: boolean; ident?: number | null; native_id?: number | null }> };
  file_descriptors?: { count?: number | null; soft_limit?: number | null; hard_limit?: number | null; usage_ratio?: number | null };
  asyncio?: { task_count?: number; pending_task_count?: number; sample_pending_tasks?: Array<{ name?: string; coro?: string }> };
  memory?: { rss_mb?: number | null; max_rss_mb?: number | null };
  /** 1/5/15-minute load; empty on a platform without getloadavg. */
  load_avg?: number[];
  runtimes?: {
    count?: number;
    current_task_count?: number;
    watchdog_task_count?: number;
    locked_count?: number;
    items?: AdminProcessRuntimeRow[];
  };
}

export interface AdminSystemOverview {
  machine_id?: string;
  server_env?: string;
  active_runtimes?: number;
  mcp_proxy_base_url?: string;
  runtime_session_ids?: string[];
  /** Counted over the caller's own sessions, so it moves with who is asking. */
  session_state_counts?: Record<string, number>;
  total_sessions?: number;
  process_health?: { severity?: string; thread_count?: number; fd_count?: number | null; pending_task_count?: number };
}

export interface AdminIntegratedService {
  id: string;
  name: string;
  category: 'identity' | 'api_access' | 'model_gateway';
  admin_url: string;
}

export interface AdminIntegrations {
  services: AdminIntegratedService[];
}

export interface AdminErrorItem {
  /** 'session' or 'turn_checkpoint' — which record the row was summarised from. */
  source?: string;
  severity?: string;
  session_id?: string;
  sandbox_id?: string;
  turn_id?: string;
  state?: string;
  user_id?: string;
  display_name?: string;
  message?: string;
  last_error?: string;
  updated_at?: string;
  failed_at?: string;
  blocked_at?: string;
}

export interface AdminErrorsPage {
  errors: AdminErrorItem[];
  counts?: { total?: number; session?: number; turn_checkpoint?: number };
}

export interface SessionEvent {
  type: string;
  turn_id?: string;
  state?: SessionState;
  permission_mode?: PermissionMode;
  text?: string;
  code?: string;
  message?: string;
  data?: Record<string, unknown>;
  interaction_id?: string;
  tool_name?: string;
  pending_interaction?: PendingInteraction;
  at?: string;
}

export type SessionFileEntryKind = 'file' | 'directory';

export interface SessionFilesListRequest {
  path?: string;
}

export interface SessionFilesListResponse {
  root_path: string;
  current_path: string;
  parent_path: string | null;
  entries: SessionFileEntry[];
}

export interface SessionFilesUploadRequest {
  path: string;
  files: File[];
}

export interface SessionFilesMkdirRequest {
  path: string;
}

export interface SessionFilesMoveRequest {
  src_path: string;
  dest_path: string;
}

export interface SessionFilesDeleteRequest {
  paths: string[];
}

export interface SessionFilesMutationResult {
  root_path?: string;
  current_path?: string;
  parent_path?: string | null;
  entries?: SessionFileEntry[];
  path?: string;
  paths?: string[];
  src_path?: string;
  dest_path?: string;
  deleted_count?: number;
}

// Content block types derived from engine-emitted AI SDK frames.
export interface TextBlockData {
  type: 'text';
  text: string;
}

export interface ThinkingBlockData {
  type: 'thinking';
  thinking: string;
}

export interface ToolUseBlockData {
  type: 'tool_use';
  id: string;
  name: string;
  input: Record<string, unknown>;
}

export interface ToolResultBlockData {
  type: 'tool_result';
  tool_use_id: string;
  content: string | Array<Record<string, unknown>> | null;
  is_error: boolean;
  tool_result_state?: 'output-available' | 'output-error' | 'output-denied';
}

export interface ResultBlockData {
  type: 'result';
  result: string;
  duration_ms?: number;
  duration_api_ms?: number;
  total_cost_usd?: number;
  num_turns?: number;
  stop_reason?: string;
  usage?: { input_tokens?: number; output_tokens?: number };
}

export interface TurnFailureBlockData {
  type: 'turn_failure';
  error: string;
  failure_phase: 'post_dispatch';
}

export interface ApiRetryBlockData {
  type: 'api_retry';
  // The live frame's id, so a reload reconciles with the stream instead of
  // laying a restored copy beside the live one (active_turn_projection).
  id?: string;
  attempt?: number;
  max_retries?: number;
  error_status?: number;
  error?: string;
}

/**
 * The generated label for one response's folded work.
 *
 * `status` is `generating` while a writer holds the claim, `completed` once the
 * label exists, `failed` when the model call did not produce one, and
 * `disabled` when label generation is switched off. The
 * status is the server's word and is never rewritten here.
 */
export interface ProcessSummaryState {
  status: string;
  summary?: string | null;
  error?: string | null;
  turn_completed?: boolean | null;
}

/**
 * The header a settled response's folded work is shown behind.
 *
 * `cursor` is the checkpoint the page that folded this work was read at, and
 * has to be sent back to reopen it: without it the blocks would come from the
 * record as it stands now rather than as this page saw it. `summarize` says
 * whether this header is the one a label describes.
 */
export interface ProcessDetails {
  block_id: string;
  cursor: string;
  session_id: string;
  message_id: string;
  turn_id: string;
  tool_count: number;
  summarize: boolean;
  summary?: ProcessSummaryState | null;
}

export interface ProcessBlockData {
  type: 'process_block';
  process_details: ProcessDetails;
}

export interface UIDataBlockData {
  type: 'ui_data';
  part: {
    type: `data-${string}`;
    id?: string;
    data: unknown;
    [key: string]: unknown;
  };
}

export type ContentBlock =
  | TextBlockData
  | ImageBlockData
  | ThinkingBlockData
  | ToolUseBlockData
  | ToolResultBlockData
  | ResultBlockData
  | TurnFailureBlockData
  | ApiRetryBlockData
  | ProcessBlockData
  | UIDataBlockData;

export interface PendingInteractionOption {
  label: string;
  description?: string | null;
}

export interface PendingInteractionQuestion {
  id: string;
  header?: string | null;
  question?: string | null;
  multi_select?: boolean | null;
  allow_free_text: boolean;
  allow_empty_text: boolean;
  options: PendingInteractionOption[];
}

// A pending interaction is discriminated by `presentation` — the structural
// answer contract its engine adapter declared — never by the vendor's name for
// it. `tool_name` carries that native name verbatim for display and matching
// only: two engines may raise the same native name through different
// contracts, so no consumer may branch on it.
export interface PendingQuestionnaireInteraction {
  interaction_id: string;
  session_id?: string;
  turn_id: string;
  tool_call_id?: string;
  tool_name: string;
  presentation: 'form';
  prompt?: string;
  created_at?: string;
  questions: PendingInteractionQuestion[];
  preset_answers?: Record<string, string> | null;
  raw_input?: Record<string, unknown>;
}

export interface PendingPlanPrompt {
  prompt: string;
  tool?: string | null;
}

// The decision-option fields the browser reads. An option also carries the
// model-facing reply copy the adapter authored; that half settles the
// transcript on the backend and never reaches the UI.
export interface PendingDecisionOption {
  id: string;
  denial: boolean;
  permission_mode_choices?: string[] | null;
  default_permission_mode?: string | null;
  applies_permission_mode?: string | null;
}

export interface PendingPlanConfirmationInteraction {
  interaction_id: string;
  session_id?: string;
  turn_id: string;
  tool_call_id?: string;
  tool_name: string;
  presentation: 'decision';
  prompt?: string;
  created_at?: string;
  body?: string | null;
  options: PendingDecisionOption[];
  raw_input?: Record<string, unknown>;
}

export interface PendingToolPermissionInteraction {
  interaction_id: string;
  session_id?: string;
  turn_id: string;
  tool_call_id?: string;
  tool_name: string;
  presentation: 'tool_approval';
  prompt?: string;
  created_at?: string;
  raw_input?: Record<string, unknown>;
}

export type PendingInteraction =
  | PendingQuestionnaireInteraction
  | PendingPlanConfirmationInteraction
  | PendingToolPermissionInteraction;

export interface InteractionQuestionAnswer {
  question_id: string;
  option_label?: string;
  option_labels?: string[];
  free_text?: string;
}

export interface QuestionnaireInteractionResponse {
  interaction_id: string;
  answers?: InteractionQuestionAnswer[];
  decline?: boolean;
  notes?: string;
}

export interface PlanInteractionResponse {
  interaction_id: string;
  decision: 'approve' | 'revise' | 'reject';
  comment?: string;
  permission_mode?: PermissionMode;
}

export interface ToolPermissionInteractionResponse {
  interaction_id: string;
  decision: 'approve' | 'reject';
  suggestion_id?: string;
  comment?: string;
}

/**
 * The answer to a decision whose options are the engine's own.
 *
 * `decision` is the id the engine declared, carried back verbatim. It is not
 * narrowed to a set of literals because the set belongs to the engine:
 * Claude's exit-plan confirmation answers `approve`/`reject`, Codex answers
 * `accept`/`acceptForSession`/`decline`/`cancel`, and an engine the platform
 * has not met yet answers with words neither of them has. A decision the
 * engine cannot parse is not a refusal it reports — the tool simply never
 * runs and the turn ends with nothing said about why.
 */
export interface DecisionInteractionResponse {
  interaction_id: string;
  decision: string;
  comment?: string;
}

export type InteractionResponse =
  | QuestionnaireInteractionResponse
  | PlanInteractionResponse
  | DecisionInteractionResponse
  | ToolPermissionInteractionResponse;

// File change tracking for diff panel
export type FileChangeDiff =
  | { format: 'contents'; before: string; after: string }
  | { format: 'unified'; patch: string }
  | { format: 'hunks'; hunks: Array<{
    oldStart: number; oldLines: number; newStart: number; newLines: number; lines: string[];
  }> }
  | { format: 'excerpts'; excerpts: Array<{ before: string; after: string }> };

export interface FileChangesResult {
  toolCallId: string;
  toolName: string;
  files: Array<{ path: string; diff: FileChangeDiff | null }>;
}

export interface FileChange {
  filePath: string;
  /** The engine's own name for the tool that changed this file. */
  toolName: string;
  diff: FileChangeDiff | null;
  linesAdded: number;
  linesRemoved: number;
  timestamp: number;
}
