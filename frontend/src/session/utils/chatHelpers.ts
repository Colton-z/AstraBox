import type { UIMessage as SDKUIMessage } from 'ai';
import { MODULE_BASE } from '../../api';
import type { MessageRecord, SessionRecord } from '../../types';
import { isMongoTransientError } from '../../utils/format';
import { isTurnSettledInSessionSnapshot } from '../messageSettlement';
import { getMessageTurnId } from '../messageIdentity';

const API_BASE_ENV = String(import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '');
export const API_BASE = API_BASE_ENV || MODULE_BASE;

/** One terminal-bounded response from the session output subscription. */
export function buildSessionStreamUrl(sessionId: string, afterSeq?: number | null): string {
  const url = new URL(`${API_BASE}/api/v1/sessions/${sessionId}/ai-stream`, window.location.origin);
  url.searchParams.set('follow', 'session');
  if (typeof afterSeq === 'number' && Number.isFinite(afterSeq) && afterSeq >= 0) {
    url.searchParams.set('after_seq', String(afterSeq));
  }
  return url.toString();
}

export interface ResumeCursorChunk {
  type: 'data-resume-cursor';
  data?: {
    frameSeq?: unknown;
    turnId?: unknown;
  };
  transient?: boolean;
}

export interface TurnAcceptedChunk {
  type: 'data-turn-accepted';
  data?: {
    clientMessageId?: unknown;
    turnId?: unknown;
  };
}

export type RetryIntent =
  | { kind: 'auto-recover'; summary: string }
  | { kind: 'recover'; label: string; summary: string }
  | { kind: 'delivery-failed'; label: string; summary: string; text: string; clientMessageId?: string }
  | { kind: 'turn-failed'; summary: string; failureDetail?: string }
  | { kind: 'budget-exhausted'; summary: string }
  | { kind: 'none' };

export function extractUserMessageText(message: SDKUIMessage | null | undefined): string {
  if (!message || message.role !== 'user') {
    return '';
  }
  return message.parts
    .filter((part): part is { type: 'text'; text: string } => part.type === 'text')
    .map((part) => part.text)
    .join('')
    .trim();
}

export function getSdkMessageTurnId(message: SDKUIMessage | null | undefined): string {
  return getMessageTurnId(message);
}

export function suppressNotReceivedUserMessages(
  messages: SDKUIMessage[],
  session: Pick<SessionRecord, 'delivery_state' | 'delivery_failure' | 'last_turn_id'>,
): SDKUIMessage[] {
  if (String(session.delivery_state ?? '').trim() !== 'NOT_RECEIVED') {
    return messages;
  }
  const suppressTurnId = String(
    session.delivery_failure?.turn_id ?? session.last_turn_id ?? '',
  ).trim();
  const suppressClientMessageId = String(session.delivery_failure?.client_message_id ?? '').trim();
  if (!suppressTurnId && !suppressClientMessageId) {
    return messages;
  }
  return messages.filter((msg) => {
    if (msg.role !== 'user') return true;
    const turnId = getMessageTurnId(msg);
    const messageId = String(msg.id ?? '').trim();
    if (suppressTurnId && turnId === suppressTurnId) {
      return false;
    }
    if (suppressClientMessageId && messageId === suppressClientMessageId) {
      return false;
    }
    return true;
  });
}

export function hasLocalAssistantForTurn(messages: SDKUIMessage[], turnId: string): boolean {
  const normalizedTurnId = String(turnId ?? '').trim();
  if (!normalizedTurnId) {
    return false;
  }
  return messages.some(
    (message) => message.role === 'assistant' && getSdkMessageTurnId(message) === normalizedTurnId,
  );
}

export function isTurnDurablySettled(session: SessionRecord, turnId: string): boolean {
  return isTurnSettledInSessionSnapshot(session, turnId);
}

// Transient stream errors can be retried without surfacing a terminal failure.
export function isTransientStreamError(err: unknown): boolean {
  const msg = String((err as Error)?.message ?? err ?? '').toLowerCase();
  return (
    msg.includes('mongodb') ||
    msg.includes('failed to fetch') ||
    msg.includes('load failed') ||
    msg.includes('network') ||
    msg.includes('econnrefused') ||
    msg.includes('econnreset') ||
    msg.includes('timeout')
  );
}

export function isNotFoundOrPermissionError(message: string | null | undefined): boolean {
  const normalized = String(message ?? '').trim().toLowerCase();
  return (
    normalized.includes('session_not_found') ||
    normalized.includes('not found') ||
    normalized.includes('permission_denied') ||
    normalized.includes('unauthorized')
  );
}

export function isTransientSessionLoadError(message: string | null | undefined): boolean {
  const normalized = String(message ?? '').trim().toLowerCase();
  return (
    isMongoTransientError(message ?? undefined) ||
    normalized.includes('network_error') ||
    normalized.includes('failed to fetch') ||
    normalized.includes('network error') ||
    normalized.includes('load failed') ||
    normalized.includes('econnrefused') ||
    normalized.includes('econnreset') ||
    normalized.includes('session_detail_timeout') ||
    normalized.includes('session detail request timed out')
  );
}

export function isStalePendingInteractionError(err: unknown): boolean {
  const normalized = String((err as Error)?.message ?? err ?? '').trim().toLowerCase();
  return (
    normalized.includes('there is no pending interaction to answer')
    || normalized.includes('the specified interaction is no longer pending')
  );
}

export function createClientMessageId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `client-message-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function formatOutput(output: unknown): string {
  if (typeof output === 'string') return output.length > 2000 ? output.slice(0, 2000) + '...' : output;
  try {
    const s = JSON.stringify(output, null, 2);
    return s.length > 2000 ? s.slice(0, 2000) + '...' : s;
  } catch { return String(output); }
}

export function parseTodoString(raw: unknown): Array<Record<string, unknown>> {
  if (Array.isArray(raw)) return raw as Array<Record<string, unknown>>;
  if (typeof raw !== 'string' || !raw.trim()) return [];
  return raw
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const match = line.match(/^\d+\.\s*\[(\w+)\]\s*(.*)$/);
      if (match) {
        return { status: match[1], content: match[2].trim() };
      }
      return { status: 'pending', content: line.replace(/^\d+\.\s*/, '').trim() };
    })
    .filter((t) => t.content);
}
