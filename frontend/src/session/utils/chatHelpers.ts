import { isPersistenceUnavailableError } from '../../utils/format';

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
    isPersistenceUnavailableError(message ?? undefined) ||
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
