import i18n from '@/i18n';
import type {
  PermissionMode,
  SessionRecord,
  SessionState,
  ToolUseBlockData,
  ContentBlock,
  TextBlockData,
  ToolResultBlockData,
} from '../types';

// Non-component module: read translations off the shared i18next instance. Every
// label function below is re-invoked on each render by its component callers, so
// switching language re-resolves them — there is no frozen-at-load-time snapshot.
const t = (key: string, opts?: Record<string, unknown>): string => i18n.t(key, opts) as string;

export const MANUAL_REFRESH_EVENT = 'astrabox:manual-refresh';
// Known labels are presentation aliases, never a capability whitelist. Engines
// may declare any non-empty mode name and the UI carries it verbatim.
export const PERMISSION_MODE_LABELS: Record<string, string> = {
  default: 'misc:permission_mode.default',
  acceptEdits: 'misc:permission_mode.accept_edits',
  plan: 'misc:permission_mode.plan',
  bypassPermissions: 'misc:permission_mode.bypass_permissions',
  dontAsk: 'misc:permission_mode.dont_ask',
  auto: 'misc:permission_mode.auto',
};
export const PERMISSION_MODE_TONE_CLASS: Record<string, string> = {
  default: 'mode-default',
  acceptEdits: 'mode-accept-edits',
  plan: 'mode-plan',
  bypassPermissions: 'mode-bypass',
};

export function getNextPermissionMode(
  value: PermissionMode,
  availableModes: readonly PermissionMode[],
): PermissionMode {
  if (availableModes.length === 0) return value;
  const index = availableModes.indexOf(value);
  return availableModes[(index + 1 + availableModes.length) % availableModes.length];
}

export function isEditableKeyboardTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  const tagName = target.tagName;
  return tagName === 'TEXTAREA' || tagName === 'INPUT' || target.isContentEditable;
}

export function stateLabel(state: SessionState | string | undefined): string {
  if (!state) return t('misc:state.unknown');
  if (state === 'CREATING') return t('misc:state.creating');
  if (state === 'SENDING') return t('misc:state.sending');
  if (state === 'READY') return t('misc:state.ready');
  if (state === 'WAITING_INPUT') return t('misc:state.waiting_input');
  if (state === 'PROCESSING') return t('misc:state.processing');
  if (state === 'PROVISIONING') return t('misc:state.provisioning');
  if (state === 'ACTIVE') return t('misc:state.active');
  if (state === 'HIBERNATING') return t('misc:state.hibernating');
  if (state === 'FAILED') return t('misc:state.failed');
  if (state === 'TERMINATING') return t('misc:state.terminating');
  if (state === 'TERMINATED') return t('misc:state.terminated');
  if (state === 'RECOVERY_REQUIRED') return t('misc:state.recovery_required');
  if (state === 'DELETED') return t('misc:state.deleted');
  return state;
}

type RuntimeAwareSession = {
  agent_id?: SessionRecord['agent_id'];
  engine_kind?: SessionRecord['engine_kind'];
  session_kind?: SessionRecord['session_kind'];
  source_type?: SessionRecord['source_type'];
  startup_progress?: SessionRecord['startup_progress'];
  state?: string | null;
  sandbox_id?: SessionRecord['sandbox_id'];
  runtime_unavailable?: SessionRecord['runtime_unavailable'];
  recovery_reason?: SessionRecord['recovery_reason'];
};

export function isAgentChatSession(session: RuntimeAwareSession | null | undefined): boolean {
  if (!session) return false;
  return session.session_kind === 'agent_chat' || session.source_type === 'agent' || !!session.agent_id;
}

export function isAssistantConversationSession(session: RuntimeAwareSession | null | undefined): boolean {
  if (!session) return false;
  return session.session_kind === 'assistant_chat';
}

// Whether the per-session SANDBOX is live and usable for resource access (files /
// terminal). Files live on the sandbox and stay available WHILE a response is in
// progress: during a reply the session state is PROCESSING / WAITING_INPUT (not
// READY), so gating file/terminal access on READY wrongly flips the files panel to
// "waiting for runtime ready" mid-reply on a fully-live sandbox. The sandbox is
// live in every non-terminal, non-creating, runtime-available state.
export function isSandboxLiveForSession(session: RuntimeAwareSession | null | undefined): boolean {
  if (!isAgentChatSession(session)) return true;
  if (session?.runtime_unavailable) return false;
  const state = String(session?.state ?? '').trim().toUpperCase();
  if (state === 'TERMINATED' || state === 'DELETED' || state === 'CREATING' || state === '') return false;
  return !!String(session?.sandbox_id ?? '').trim();
}

// A cleared binding can borrow fresh compute on the next input. A failed attach
// to a retained box has no such recovery proof; keep its state and error visible.
export function isTransparentlyRecoverableAgentSession(
  session: RuntimeAwareSession | null | undefined,
): boolean {
  if (!isAgentChatSession(session)) return false;
  if (!session?.runtime_unavailable) return false;
  if (String(session.sandbox_id ?? '').trim() || String(session.recovery_reason ?? '').trim()) return false;
  const state = String(session?.state ?? '').trim().toUpperCase();
  // Only a wakeable conversation re-borrows transparently. TERMINATED/DELETED are
  // terminal; CREATING is actively provisioning and shows its own progress instead.
  return state !== 'TERMINATED' && state !== 'DELETED' && state !== 'CREATING';
}

const SESSION_STARTUP_PROGRESS_PHASES = new Set([
  'creating_sandbox',
  'mounting_nas',
  'starting_agent',
  'waiting_for_startup_lease',
]);

function sessionStartupProgressLabel(progress: string | null | undefined): string {
  const normalized = String(progress ?? '').trim();
  if (!SESSION_STARTUP_PROGRESS_PHASES.has(normalized)) return '';
  return progressLabel(normalized);
}

/** The state a session list row or header shows: a starting session names its startup step. */
export function sessionOperationalStatusLabel(session: RuntimeAwareSession | null | undefined): string {
  if (!session) return t('misc:state.unknown');
  const baseLabel = stateLabel(String(session.state ?? ''));
  if (session.state === 'CREATING') {
    return sessionStartupProgressLabel(session.startup_progress) || baseLabel;
  }
  return baseLabel;
}

export function progressLabel(progress: string | null | undefined): string {
  const normalized = String(progress ?? '').trim();
  if (!normalized) return '';
  if (normalized === 'creating_workspace') return t('misc:progress.creating_workspace');
  if (normalized === 'creating_sandbox') return t('misc:progress.creating_sandbox');
  if (normalized === 'mounting_nas') return t('misc:progress.mounting_nas');
  if (normalized === 'starting_agent') return t('misc:progress.starting_agent');
  if (normalized === 'waiting_for_startup_lease') return t('misc:progress.waiting_for_startup_lease');
  if (normalized === 'probing_conversation_profile') return t('misc:progress.probing_conversation_profile');
  if (normalized === 'mounting_conversation_storage') return t('misc:progress.mounting_conversation_storage');
  if (normalized === 'preparing_skill_cache') return t('misc:progress.preparing_skill_cache');
  if (normalized === 'preparing_conversation_bootstrap') return t('misc:progress.preparing_conversation_bootstrap');
  if (normalized === 'fetching_agent_metadata') return t('misc:progress.fetching_agent_metadata');
  return normalized;
}

/** The record store was unreachable; the request is retryable as sent. */
export function isPersistenceUnavailableError(message: string | undefined): boolean {
  return String(message || '').includes('PERSISTENCE_UNAVAILABLE');
}

export function toPrettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

export function mergeUniqueStrings(values: string[]): string[] {
  const merged: string[] = [];
  const seen = new Set<string>();
  for (const value of values) {
    const text = String(value || '').trim();
    if (!text || seen.has(text)) {
      continue;
    }
    seen.add(text);
    merged.push(text);
  }
  return merged;
}

export function normalizeAgentSlashCommands(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return mergeUniqueStrings(
    value
      .map((item) => String(item ?? '').trim().replace(/^\/+/, ''))
      .filter(Boolean),
  );
}

export function extractAgentInitMetadata(raw: unknown): { model: string; slashCommands: string[] } | null {
  if (!raw || typeof raw !== 'object') {
    return null;
  }
  let payload = raw as Record<string, unknown>;
  if (String(payload.subtype ?? '').trim().toLowerCase() !== 'init') {
    const nested = payload.data;
    if (!nested || typeof nested !== 'object') {
      return null;
    }
    payload = nested as Record<string, unknown>;
    if (String(payload.subtype ?? '').trim().toLowerCase() !== 'init') {
      return null;
    }
  }
  const model = String(payload.model ?? '').trim();
  const slashCommands = normalizeAgentSlashCommands(payload.slash_commands);
  if (!model && slashCommands.length === 0) {
    return null;
  }
  return { model, slashCommands };
}

/* ------------------------------------------------------------------ */
/*  Tool icons                                                         */
/* ------------------------------------------------------------------ */

export const TOOL_ICONS: Record<string, string> = {
  AskUserQuestion: '\u2753',
  Bash: '\u2318',
  Edit: '\u270E',
  EnterPlanMode: '\u{1F4CB}',
  ExitPlanMode: '\u{1F4CB}',
  Write: '\u270D',
  Read: '\u{1F4C4}',
  Glob: '\u{1F50D}',
  Grep: '\u{1F50E}',
  WebFetch: '\u{1F310}',
  WebSearch: '\u{1F310}',
  Task: '\u{1F4CB}',
  NotebookEdit: '\u{1F4D3}',
  TodoRead: '\u{2611}',
  TodoWrite: '\u{2611}',
};

export function toolIcon(name: string): string {
  return TOOL_ICONS[name] || '\u{1F527}';
}

export function truncateToolPreview(value: string, maxLength = 60): string {
  const text = value.trim();
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, maxLength - 3)}...`;
}

export function firstNonEmptyLine(value: string): string {
  return value
    .split('\n')
    .map((line) => line.trim())
    .find(Boolean) ?? '';
}

export function summarizeInlineValue(value: unknown): string {
  if (value == null) {
    return '';
  }
  if (typeof value === 'string') {
    return value;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  if (Array.isArray(value)) {
    const items = value
      .map((item) => summarizeInlineValue(item))
      .map((item) => item.trim())
      .filter(Boolean);
    if (items.length > 0) {
      return items.join(' / ');
    }
    return t('misc:summary.items', { count: value.length });
  }
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>;
    for (const key of ['question', 'header', 'label', 'activeForm', 'content', 'prompt', 'plan']) {
      const summary = summarizeInlineValue(record[key]);
      if (summary) {
        return summary;
      }
    }
    const keys = Object.keys(record);
    return keys.length > 0 ? t('misc:summary.fields', { count: keys.length }) : '';
  }
  return '';
}

export function formatAskUserQuestionPreview(input: Record<string, unknown>): string {
  const questions = Array.isArray(input.questions)
    ? (input.questions as Array<Record<string, unknown>>)
    : [];
  if (questions.length === 0) {
    return '';
  }

  const firstQuestion = questions[0] || {};
  const header = summarizeInlineValue(firstQuestion.header);
  const question = summarizeInlineValue(firstQuestion.question);
  const summary = header && question ? `${header}: ${question}` : question || header;
  const prefix = questions.length > 1 ? t('misc:summary.questions_prefix', { count: questions.length }) : '';
  return truncateToolPreview(`${prefix}${summary}`.trim());
}

function parseTodoItems(raw: unknown): Array<Record<string, unknown>> {
  if (Array.isArray(raw)) return raw as Array<Record<string, unknown>>;
  if (typeof raw !== 'string' || !raw.trim()) return [];
  return raw
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const match = line.match(/^\d+\.\s*\[(\w+)\]\s*(.*)$/);
      if (match) return { status: match[1], content: match[2].trim() };
      return { status: 'pending', content: line.replace(/^\d+\.\s*/, '').trim() };
    })
    .filter((t) => t.content);
}

export function formatTodoWritePreview(input: Record<string, unknown>): string {
  const todos = parseTodoItems(input.todos);
  if (todos.length === 0) {
    return '';
  }

  const activeTodo =
    todos.find((todo) => String(todo.status ?? '') === 'in_progress') ??
    todos.find((todo) => String(todo.status ?? '') === 'pending') ??
    todos[0];
  const focus = summarizeInlineValue(activeTodo?.activeForm ?? activeTodo?.content);
  const prefix = t('misc:summary.todos_count', { count: todos.length });
  return truncateToolPreview(focus ? `${prefix} · ${focus}` : prefix);
}

export function formatExitPlanModePreview(input: Record<string, unknown>): string {
  const plan = String(input.plan ?? '');
  const firstLine = firstNonEmptyLine(plan).replace(/^#+\s*/, '');
  if (firstLine) {
    return truncateToolPreview(firstLine);
  }

  const prompts = Array.isArray(input.allowedPrompts)
    ? (input.allowedPrompts as Array<Record<string, unknown>>)
    : [];
  if (prompts.length > 0) {
    const firstPrompt = summarizeInlineValue(prompts[0]?.prompt);
    return truncateToolPreview(firstPrompt ? t('misc:summary.allow_operation_prefix', { prompt: firstPrompt }) : t('misc:summary.allowed_operations', { count: prompts.length }));
  }
  return '';
}

export function toolPreview(name: string, input: Record<string, unknown>): string {
  if (name === 'Bash') {
    const cmd = String(input.command ?? '');
    const firstLine = cmd.split('\n')[0];
    return firstLine.length > 80 ? firstLine.slice(0, 77) + '...' : firstLine;
  }
  if (name === 'Edit' || name === 'Write' || name === 'Read') {
    return String(input.file_path ?? input.path ?? '');
  }
  if (name === 'Glob') {
    return String(input.pattern ?? '');
  }
  if (name === 'Grep') {
    return String(input.pattern ?? '');
  }
  if (name === 'AskUserQuestion') {
    return formatAskUserQuestionPreview(input);
  }
  if (name === 'TodoWrite') {
    return formatTodoWritePreview(input);
  }
  if (name === 'ExitPlanMode') {
    return formatExitPlanModePreview(input);
  }
  const keys = Object.keys(input);
  if (keys.length > 0) {
    const first = summarizeInlineValue(input[keys[0]]);
    if (first) {
      return truncateToolPreview(first);
    }
    return truncateToolPreview(t('misc:summary.params_count', { count: keys.length }));
  }
  return '';
}

export function formatAskUserQuestionInput(input: Record<string, unknown>): string {
  const questions = Array.isArray(input.questions)
    ? (input.questions as Array<Record<string, unknown>>)
    : [];
  if (questions.length === 0) {
    return JSON.stringify(input, null, 2);
  }

  return questions.map((question, index) => {
    const lines: string[] = [];
    const header = summarizeInlineValue(question.header);
    const prompt = summarizeInlineValue(question.question);
    const options = Array.isArray(question.options)
      ? (question.options as Array<Record<string, unknown>>)
      : [];

    lines.push(t('misc:summary.question_n', { index: index + 1 }));
    if (header) {
      lines.push(t('misc:summary.question_header', { header }));
    }
    if (prompt) {
      lines.push(t('misc:summary.question_content', { content: prompt }));
    }
    if (options.length > 0) {
      lines.push(t('misc:summary.options_label'));
      options.forEach((option, optionIndex) => {
        const label = summarizeInlineValue(option.label) || t('misc:summary.option_n', { index: optionIndex + 1 });
        const description = summarizeInlineValue(option.description);
        lines.push(
          description
            ? `${optionIndex + 1}. ${label} - ${description}`
            : `${optionIndex + 1}. ${label}`,
        );
      });
    }

    return lines.join('\n');
  }).join('\n\n');
}

export function formatTodoWriteInput(input: Record<string, unknown>): string {
  const todos = parseTodoItems(input.todos);
  if (todos.length === 0) {
    return JSON.stringify(input, null, 2);
  }

  return todos.map((todo, index) => {
    const status = String(todo.status ?? 'pending');
    const activeForm = summarizeInlineValue(todo.activeForm);
    const content = summarizeInlineValue(todo.content);
    const summary = activeForm || content || t('misc:summary.todo_n', { index: index + 1 });
    return `${index + 1}. [${status}] ${summary}`;
  }).join('\n');
}

export function formatToolInputBody(name: string, input: Record<string, unknown>): string {
  if (name === 'Bash') {
    return String(input.command ?? '');
  }
  if (name === 'Edit') {
    return `${input.file_path ?? ''}\n---old---\n${input.old_string ?? ''}\n---new---\n${input.new_string ?? ''}`;
  }
  if (name === 'Write') {
    return `${input.file_path ?? ''}\n${String(input.content ?? '').slice(0, 2000)}`;
  }
  if (name === 'Read') {
    return String(input.file_path ?? '');
  }
  if (name === 'AskUserQuestion') {
    return formatAskUserQuestionInput(input);
  }
  if (name === 'TodoWrite') {
    return formatTodoWriteInput(input);
  }
  if (name === 'ExitPlanMode') {
    return String(input.plan ?? JSON.stringify(input, null, 2));
  }
  return JSON.stringify(input, null, 2);
}

// Maps an English wire/source string to its i18n key. `localizeDisplayText`
// resolves the key at call time so the substituted display text follows the
// active language. The order here governs which substring wins under the
// sequential split/join (longer, more specific strings precede the generic ones
// they contain). Backend wire strings are English.
export const DISPLAY_TEXT_REPLACEMENTS: Array<[string, string]> = [
  ['Answer questions?', 'misc:wire.answer_questions'],
  ['Exit plan mode?', 'misc:wire.exit_plan_mode'],
  ['[Request interrupted by user for tool use]', 'misc:wire.interrupted_for_tool_use'],
  ['Response to your pending questions:', 'misc:wire.response_to_questions'],
  ['User declined to answer questions.', 'misc:wire.declined_questions'],
  ["User rejected Claude's plan.", 'misc:wire.rejected_plan'],
  ['Response to your pending plan confirmation:', 'misc:wire.response_to_plan'],
  // Persisted `last_error` values can contain these English wire strings.
  ['sandbox expired', 'misc:wire.sandbox_expired'],
  ['sandbox terminated by user', 'misc:wire.sandbox_terminated_by_user'],
  ['runtime reconnect failed, sandbox expired — create a new session', 'misc:wire.reconnect_failed_expired_new_session'],
  ['runtime reconnect failed, sandbox expired', 'misc:wire.reconnect_failed_expired'],
  ['runtime reconnect failed, retrying may help', 'misc:wire.reconnect_failed_retry'],
  ['runtime reconnect failed, recover and retry', 'misc:wire.reconnect_failed_recover'],
  ['interrupt requested', 'misc:wire.interrupt_requested'],
  ['stream recovery incomplete', 'misc:wire.stream_recovery_incomplete'],
  ['turn lock expired', 'misc:wire.turn_lock_expired'],
  ['killed by admin', 'misc:wire.killed_by_admin'],
  ['stale creating session cleaned up on bootstrap', 'misc:wire.stale_creating_cleaned'],
  [
    'cannot terminate sandbox while a conversation turn is active or durable finalization is pending',
    'misc:wire.cannot_terminate_active_turn',
  ],
  // Runtime/error wire strings emitted by turn_service / session_service / sandbox_lifecycle /
  // platform_service. The longer "sandbox terminated *" variants MUST precede the generic
  // "sandbox terminated" below, since the sequential split/join lets the first match win.
  ['the agent sandbox is restarting, please retry', 'misc:wire.agent_sandbox_restarting'],
  ['the agent sandbox was recycled, rebuilding the runtime, please retry', 'misc:wire.agent_sandbox_recycled'],
  ['session creation timed out', 'misc:wire.session_creation_timed_out'],
  ['sandbox terminated abnormally', 'misc:wire.sandbox_terminated_abnormally'],
  ['sandbox unavailable', 'misc:wire.sandbox_unavailable'],
  ['sandbox terminated', 'misc:wire.sandbox_terminated'],
  ['the sandbox did not receive this message', 'misc:wire.sandbox_did_not_receive'],
  ['sandbox TTL expired; workspace hibernated, will be rebuilt on next access', 'misc:wire.workspace_hibernated'],
];

export function localizeDisplayText(text: string): string {
  let localized = text;
  DISPLAY_TEXT_REPLACEMENTS.forEach(([source, key]) => {
    localized = localized.split(source).join(t(key));
  });
  return localized;
}

export function isToolWaitResult(blockName: string, resultBlock?: ToolResultBlockData): boolean {
  if (!resultBlock || resultBlock.is_error !== true) {
    return false;
  }

  const content = String(resultBlock.content ?? '').trim();
  // Accept the English wire literal or content localized for display before it
  // reached this classifier.
  return (
    (blockName === 'AskUserQuestion' && (content === 'Answer questions?' || content === t('misc:wire.answer_questions'))) ||
    (blockName === 'ExitPlanMode' && (content === 'Exit plan mode?' || content === t('misc:wire.exit_plan_mode')))
  );
}

export function isAwaitingUserConfirmation(
  blockName: string,
  resultBlock: ToolResultBlockData | undefined,
  isActivePendingTool: boolean,
): boolean {
  return isActivePendingTool && isToolWaitResult(blockName, resultBlock);
}

export function isInterruptedToolWaitText(block: TextBlockData, blocks: ContentBlock[]): boolean {
  const text = String(block.text ?? '').trim();
  if (text !== '[Request interrupted by user for tool use]') {
    return false;
  }

  const resultMap = new Map<string, ToolResultBlockData>();
  blocks.forEach((candidate) => {
    if (candidate.type === 'tool_result') {
      resultMap.set(candidate.tool_use_id, candidate);
    }
  });

  return blocks.some(
    (candidate) =>
      candidate.type === 'tool_use' &&
      isToolWaitResult(candidate.name, resultMap.get(candidate.id)),
  );
}

export function todoStatusLabel(status: string): string {
  if (status === 'completed') return t('misc:todo.completed');
  if (status === 'in_progress') return t('misc:todo.in_progress');
  if (status === 'cancelled') return t('misc:todo.cancelled');
  return t('misc:todo.pending');
}

export function todoStatusClass(status: string): string {
  if (status === 'completed') return 'tool-todo-item-done';
  if (status === 'in_progress') return 'tool-todo-item-active';
  if (status === 'cancelled') return 'tool-todo-item-cancelled';
  return 'tool-todo-item-pending';
}
