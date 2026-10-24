import type { UIMessage } from 'ai';
import type { TurnInputImage, UserAction } from '../types';
import { readUserAction } from './userAction';

/**
 * The session runtime's conversation as AI SDK UI messages.
 *
 * History and the session's stream deliver the same messages. A user message
 * is one accepted input: its id is the input's `client_message_id` and its
 * metadata the input's facts. An assistant message is one response: its id is
 * the response id and its metadata what the runtime reported about it. Both
 * ids are stable from the first moment the console sees a message, so every
 * merge below matches messages by id.
 */

/**
 * Where an imported message came from: the source's own id for it, the reply's
 * parent there, and whether the source recorded how a reply ended.
 */
export interface HistoryImport {
  source_id?: string;
  completion?: 'complete' | 'unknown';
  parent_source_id?: string;
}

/** What the runtime reported about one response (`session_runtime/ui_message.py`). */
export interface ResponseMetadata {
  response_id?: string;
  /** First native content position in the session's ordered output log. */
  conversation_order?: number | null;
  /** Open text/reasoning prefixes in a history snapshot, addressed by their output chunk ids. */
  continuation_parts?: Array<{ index: number; id: string }>;
  /** Preview chunks the adapter withdrew, addressed by providerMetadata.astrabox.part_id. */
  retracted_part_ids?: string[];
  /** The inputs this response answers; empty for one the runtime started itself. */
  input_ids?: string[];
  /** `input`, or `engine` for a response nobody's message asked for. */
  origin?: string;
  /** `running`, `waiting` (paused at an interaction) or `ended`. */
  status?: string;
  /** `completed`, `failed` or `cancelled` once ended. */
  outcome?: string | null;
  /** The engine's own word for how it ended, carried verbatim. */
  native_reason?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  /** The runtime's report of a failure. */
  error?: { code?: string; message?: string } | null;
  /** A reply imported with the conversation's history rather than produced here. */
  history_import?: HistoryImport | null;
}

/** Why the runtime or the deployment refused an input. */
export interface InputFailure {
  code?: string;
  message?: string;
  status_code?: number;
  user_action?: UserAction | null;
}

/** What the session recorded about one input. */
export interface InputMetadata {
  input_id?: string;
  /** Native conversation position; an input still held in a queue has none. */
  conversation_order?: number | null;
  created_at?: string | null;
  /** `pending`, `delivered`, `admitted`, `completed` or `failed`. */
  state?: string | null;
  author_user_id?: string | null;
  source?: string | null;
  failure?: InputFailure | null;
  /** A message imported with the conversation's history rather than sent here. */
  history_import?: HistoryImport | null;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? value as Record<string, unknown> : {};
}

export interface MessageOrder {
  kind: 'input' | 'response';
  id: string;
  order: number;
}

/** Native messages in recorded order, followed by inputs not yet observed there. */
export function orderConversationMessages(
  messages: readonly UIMessage[],
  observed: ReadonlyMap<string, number>,
): UIMessage[] {
  let updated = messages as UIMessage[];
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index];
    const metadata = record(message.metadata);
    const key = message.role === 'user' ? `input:${String(metadata.input_id || '')}` : `response:${message.id}`;
    const known = observed.get(key);
    const retained = metadata.conversation_order;
    if (known === undefined || (typeof retained === 'number' && retained <= known)) continue;
    if (updated === messages) updated = [...messages];
    updated[index] = withMetadata(message, { conversation_order: known });
  }
  const position = (message: UIMessage): [number, number] => {
    const metadata = record(message.metadata);
    const value = metadata.conversation_order;
    const known = typeof value === 'number' && Number.isSafeInteger(value);
    return [metadata.history_import ? 0 : known ? 1 : 2, known ? value : 0];
  };
  const compare = (left: UIMessage, right: UIMessage): number => {
    const a = position(left);
    const b = position(right);
    return a[0] - b[0] || a[1] - b[1];
  };
  // Most writes only append text to a message whose position is unchanged.
  // Sort only when an observed native position actually changes the order.
  for (let index = 1; index < updated.length; index += 1) {
    if (compare(updated[index - 1], updated[index]) > 0) {
      return [...updated].sort(compare);
    }
  }
  return updated;
}

export function responseMetadata(message: UIMessage): ResponseMetadata {
  return record(message.metadata) as ResponseMetadata;
}

/** Apply the adapter's withdrawals before a live message reaches any display. */
export function retractMessageParts(message: UIMessage): UIMessage {
  const retracted = new Set(responseMetadata(message).retracted_part_ids ?? []);
  if (retracted.size === 0) return message;
  return { ...message, parts: message.parts.filter((part) => {
    if (part.type !== 'text' && part.type !== 'reasoning') return true;
    const identity = record(part.providerMetadata?.astrabox).part_id;
    return typeof identity !== 'string' || !retracted.has(identity);
  }) };
}

export function inputMetadata(message: UIMessage): InputMetadata {
  return record(message.metadata) as InputMetadata;
}

/** A response that has not ended: still running, or paused at an interaction. */
export function isOpenResponse(message: UIMessage): boolean {
  if (message.role !== 'assistant') return false;
  const status = responseMetadata(message).status;
  return status === 'running' || status === 'waiting';
}

/** The newest response that has not ended, which a stream followed from history continues. */
export function lastOpenResponse(messages: readonly UIMessage[]): UIMessage | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (isOpenResponse(messages[index])) return messages[index];
  }
  return null;
}

/** The refusal recorded for an input, with its user action checked. */
export function inputFailure(message: UIMessage): InputFailure | null {
  if (message.role !== 'user') return null;
  const failure = inputMetadata(message).failure;
  if (!failure || typeof failure !== 'object') return null;
  return { ...failure, user_action: readUserAction(failure.user_action) };
}

export function withMetadata(message: UIMessage, metadata: Record<string, unknown>): UIMessage {
  return { ...message, metadata: { ...record(message.metadata), ...metadata } };
}

/** `message` in place of the one with its id, or after everything when there is none. */
export function upsertMessage(messages: readonly UIMessage[], message: UIMessage): UIMessage[] {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].id === message.id) {
      if (messages[index] === message) return messages as UIMessage[];
      const next = [...messages];
      next[index] = message;
      return next;
    }
  }
  return [...messages, message];
}

/** The messages with one of them changed; the same array when nothing matched or changed. */
export function updateMessages(
  messages: readonly UIMessage[],
  matches: (message: UIMessage) => boolean,
  update: (message: UIMessage) => UIMessage,
): UIMessage[] {
  let changed = false;
  const next = messages.map((message) => {
    if (!matches(message)) return message;
    const updated = update(message);
    if (updated !== message) changed = true;
    return updated;
  });
  return changed ? next : messages as UIMessage[];
}

/**
 * An older history page in front of the messages already shown.
 *
 * Pages are cut by the server's own cursor, so the older page ends where the
 * shown window begins; a message both carry is kept once, as shown.
 */
export function prependOlderMessages(
  existing: readonly UIMessage[],
  older: readonly UIMessage[],
): UIMessage[] {
  const shown = new Set(existing.map((message) => message.id));
  const added = older.filter((message) => !shown.has(message.id));
  return added.length === 0 ? existing as UIMessage[] : [...added, ...existing];
}

/**
 * The newest history window merged into the messages already shown.
 *
 * Messages older than the window stay as they are, and the window's own
 * messages older than the first one shown are left to paging while older
 * pages remain unloaded. Otherwise that prefix is retained: it can contain
 * an external input preceding the first response the stream showed.
 * Inside the window the history's version
 * wins, except for a message `keepShown` claims — one the stream is still
 * writing, which catches up on its own. Messages the window does not carry —
 * a reply that started after it was read, or a message this page could not
 * send — stay after it, in order.
 */
export function mergeLatestMessages(
  existing: readonly UIMessage[],
  latest: readonly UIMessage[],
  keepShown: (message: UIMessage) => boolean,
  hasOlderHistory: boolean,
): UIMessage[] {
  if (latest.length === 0) return existing as UIMessage[];
  if (existing.length === 0) return [...latest];
  const shownAt = new Map(existing.map((message, index) => [message.id, index]));
  const firstShown = hasOlderHistory ? latest.findIndex((message) => message.id === existing[0].id) : -1;
  const window = firstShown > 0 ? latest.slice(firstShown) : latest;
  const shared = window.findIndex((message) => shownAt.has(message.id));
  if (shared < 0) {
    // Nothing shown is history yet: the window goes before it.
    const ids = new Set(window.map((message) => message.id));
    return [...window, ...existing.filter((message) => !ids.has(message.id))];
  }
  const anchor = shownAt.get(window[shared].id) ?? 0;
  const windowIds = new Set(window.map((message) => message.id));
  return [
    ...existing.slice(0, anchor),
    ...window.map((message) => {
      const index = shownAt.get(message.id);
      const current = index === undefined ? undefined : existing[index];
      return current && keepShown(current) ? current : message;
    }),
    ...existing.slice(anchor).filter((message) => !windowIds.has(message.id)),
  ];
}

/**
 * Whether the newest window reaches the messages already shown, so merging it
 * leaves no gap: it carries one of them, or nothing is shown.
 */
export function latestPageJoins(existing: readonly UIMessage[], latest: readonly UIMessage[]): boolean {
  if (existing.length === 0 || latest.length === 0) return true;
  const shown = new Set(existing.map((message) => message.id));
  return latest.some((message) => shown.has(message.id));
}

/** The person's own message, as it is shown from the moment it is sent. */
export function userMessage(
  clientMessageId: string,
  text: string,
  images: readonly TurnInputImage[],
): UIMessage {
  return {
    id: clientMessageId,
    role: 'user',
    parts: [
      ...(text ? [{ type: 'text' as const, text }] : []),
      ...images.map((image) => ({
        type: 'file' as const,
        mediaType: image.source.media_type,
        url: `data:${image.source.media_type};base64,${image.source.data}`,
      })),
    ],
    metadata: {},
  };
}

/** The text a user message carries. */
export function userMessageText(message: UIMessage): string {
  return message.parts
    .filter((part): part is { type: 'text'; text: string } => part.type === 'text')
    .map((part) => part.text)
    .join('');
}
