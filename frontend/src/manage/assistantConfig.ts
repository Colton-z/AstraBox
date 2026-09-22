// Shared read helpers for the management-side Assistant (shared workspace) pages.
// An assistant is a lifecycle object rather than a rich editable config: the record
// page shows facts and a workspace state, and Wake / Hibernate / Delete are
// operations beside the heading. Identity (engine/environment) is immutable after
// materialize, so only the mutable subset is editable there. These helpers mirror
// sessionConfig.ts so the two operational surfaces speak the same pill/format grammar.
import i18n from '@/i18n';
import { listAdminEnvironments } from '@/api';
import type { EnvironmentConfig } from '@/types';

import type { AssistantRecord } from '@/assistant/types';
import type { PillTone } from './console';

// Reuse the agent-side date helper verbatim — same semantics across manage pages.
export { formatDateTime } from './agentConfig';

function asString(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

export function assistantDisplayName(a: AssistantRecord): string {
  return asString(a.display_name).trim() || asString(a.assistant_id) || i18n.t('manage:value.unnamed_assistant');
}

/** engine_kind → branded engine label, matching the environment archetype voice. */
export function assistantEngineLabel(a: AssistantRecord): string {
  const kind = asString(a.engine_kind).trim();
  if (kind === 'claude_code') return 'Claude Code';
  if (kind === 'assistant') return 'Assistant';
  return kind || '—';
}

export function assistantUpdatedAt(a: AssistantRecord): string {
  return asString(a.updated_at);
}

// ── Workspace state → console grammar ───────────────────────────────────────
// The assistant workspace lifecycle, grouped by the same semantics as the
// instanceConfig badge classes but expressed as the console StatusPill tones:
//   READY            Ready → done (mint), settled-and-usable
//   MATERIALIZING    Initializing → running (astra + live pulse), actively provisioning
//   HIBERNATING      Hibernating → pending (citrine), dormant/transitioning
//   NOT_MATERIALIZED Not Materialized → idle (muted), cold/never woken
//   RECOVERY_REQUIRED Recovery Required → failed (crimson), needs attention
export function assistantStateLabel(state?: string): string {
  switch (String(state || '')) {
    case 'READY':
      return i18n.t('manage:assistant_state.ready');
    case 'MATERIALIZING':
      return i18n.t('manage:assistant_state.materializing');
    case 'HIBERNATING':
      return i18n.t('manage:assistant_state.hibernating');
    case 'RECOVERY_REQUIRED':
      return i18n.t('manage:assistant_state.recovery_required');
    case 'NOT_MATERIALIZED':
      return i18n.t('manage:assistant_state.not_materialized');
    default:
      return state ? String(state) : i18n.t('manage:assistant_state.unknown');
  }
}

export function assistantStateTone(state?: string): PillTone {
  switch (String(state || '')) {
    case 'READY':
      return 'done';
    case 'MATERIALIZING':
      return 'running';
    case 'HIBERNATING':
      return 'pending';
    case 'RECOVERY_REQUIRED':
      return 'failed';
    case 'NOT_MATERIALIZED':
    default:
      return 'idle';
  }
}

/** The one state that should breathe (a workspace mid-provision is live work). */
export function assistantStateIsLive(state?: string): boolean {
  return String(state || '') === 'MATERIALIZING';
}

// ── Lifecycle predicates ─────────────────────────────────────────────────────
// Wakeable: cold / dormant / recoverable workspaces can be materialized.
const WAKEABLE = new Set(['NOT_MATERIALIZED', 'HIBERNATING', 'RECOVERY_REQUIRED']);
export function assistantWakeable(state?: string): boolean {
  return WAKEABLE.has(String(state || 'NOT_MATERIALIZED'));
}
/** Hibernatable: only a fully-ready workspace can be put to sleep. */
export function assistantHibernatable(state?: string): boolean {
  return String(state || '') === 'READY';
}
/** Materializing rows are transient — show progress, offer no action. */
export function assistantMaterializing(state?: string): boolean {
  return String(state || '') === 'MATERIALIZING';
}

// ── Permission mode → label (canonical app vocabulary, mirrored from utils/format) ──
const PERMISSION_MODE_KEYS: Record<string, string> = {
  default: 'manage:permission_mode.default',
  acceptEdits: 'manage:permission_mode.accept_edits',
  plan: 'manage:permission_mode.plan',
  bypassPermissions: 'manage:permission_mode.bypass_permissions',
  dontAsk: 'manage:permission_mode.dont_ask',
  auto: 'manage:permission_mode.auto',
};
export function permissionModeLabel(mode?: string): string {
  const m = String(mode || 'default');
  const key = PERMISSION_MODE_KEYS[m];
  return key ? i18n.t(key) : m;
}

/** Blank create draft. The selected Environment supplies the engine identity. */
export interface AssistantDraft {
  display_name: string;
  description: string;
  engine_kind: string;
  environment_name: string;
  permission_mode_default: string;
}

export function buildAssistantDraft(): AssistantDraft {
  return {
    display_name: '',
    description: '',
    engine_kind: '',
    environment_name: '',
    permission_mode_default: 'default',
  };
}

/**
 * The environment presets an assistant may name.
 *
 * Eligibility comes from the installed adapter's declaration. A third-party
 * resident engine becomes selectable without teaching this page its name.
 */
export async function loadAssistantEnvironments(): Promise<EnvironmentConfig[]> {
  const envs = await listAdminEnvironments();
  return envs
    .filter(
      (e) =>
        e.enabled !== false &&
        e.engine_available !== false &&
        e.supported_session_kinds?.includes('assistant_chat') === true &&
        Boolean(String(e.name || '').trim()),
    );
}
