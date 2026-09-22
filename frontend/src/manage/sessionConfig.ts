import i18n from '@/i18n';
import { toneForState } from '@/components/AstraConsole';
import type { AdminSessionSummary } from '@/types';

/** Display name for a session's user, falling back to the raw user id. */
export function sessionUserDisplay(row: Partial<AdminSessionSummary>): string {
  const display = String(row?.display_name || '').trim();
  if (display) return display;
  return String(row?.user_id || '').trim() || '-';
}

export function shortId(id?: string | null): string {
  const s = String(id || '');
  if (!s) return '-';
  return s.length > 14 ? `${s.slice(0, 8)}…${s.slice(-4)}` : s;
}

export function formatDuration(seconds?: number): string {
  if (seconds == null || !Number.isFinite(seconds)) return '-';
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/**
 * SessionState → a short translated label, mirroring assistantStateLabel in
 * assistantConfig.ts so the two operational surfaces speak the same vocabulary.
 * Covers the canonical SessionState union (types.ts) plus runtime states that
 * can appear in persisted records (PROVISIONING / HIBERNATING / …). Unknown
 * states fall back to the raw token rather than rendering blank. Color and
 * pulse stay with sessionStateTone / sessionStateIsLive.
 */
export function sessionStateLabel(state?: string): string {
  switch (String(state || '').toUpperCase()) {
    case 'CREATING':
      return i18n.t('manage:session_state.creating');
    case 'PROVISIONING':
      return i18n.t('manage:session_state.provisioning');
    case 'STARTING':
      return i18n.t('manage:session_state.starting');
    case 'PENDING':
      return i18n.t('manage:session_state.pending');
    case 'READY':
      return i18n.t('manage:session_state.ready');
    case 'BACKGROUND_RUNNING':
      return i18n.t('manage:session_state.background_running');
    case 'PROCESSING':
      return i18n.t('manage:session_state.processing');
    case 'BUSY':
      return i18n.t('manage:session_state.busy');
    case 'SENDING':
      return i18n.t('manage:session_state.sending');
    case 'ACTIVE':
    case 'RUNNING':
      return i18n.t('manage:session_state.running');
    case 'WAITING_INPUT':
      return i18n.t('manage:session_state.waiting_input');
    case 'INTERRUPTING':
      return i18n.t('manage:session_state.interrupting');
    case 'TERMINATING':
      return i18n.t('manage:session_state.terminating');
    case 'HIBERNATING':
      return i18n.t('manage:session_state.hibernating');
    case 'IDLE':
      return i18n.t('manage:session_state.idle');
    case 'PAUSED':
      return i18n.t('manage:session_state.paused');
    case 'TERMINATED':
      return i18n.t('manage:session_state.terminated');
    case 'DELETED':
      return i18n.t('manage:session_state.deleted');
    case 'RECOVERY_REQUIRED':
      return i18n.t('manage:session_state.recovery_required');
    case 'FAILED':
    case 'ERROR':
      return i18n.t('manage:session_state.failed');
    default:
      return state ? String(state) : i18n.t('manage:session_state.unknown');
  }
}

export type SessionPillTone = 'running' | 'pending' | 'failed' | 'idle' | 'done';

/**
 * SessionState → console StatusPill tone, grouped by the same lifecycle
 * semantics as {@link sessionStateBadgeClass}. healthy→running (live halo),
 * starting→pending, waiting/dormant→pending, needs-attention→failed,
 * terminal/unknown→idle.
 */
// Re-export the shared classifier so every surface uses one state-to-tone map.
export function sessionStateTone(state?: string): SessionPillTone {
  return toneForState(state) as SessionPillTone;
}

/** States that should breathe (actively serving). */
export function sessionStateIsLive(state?: string): boolean {
  return ['BACKGROUND_RUNNING', 'PROCESSING', 'BUSY', 'SENDING', 'ACTIVE', 'RUNNING'].includes(
    String(state || '').toUpperCase(),
  );
}
