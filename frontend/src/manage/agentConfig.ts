// Shared read helpers for the management-side Agent pages.
import i18n from '@/i18n';
import type { AgentConfig, AgentDraft } from '@/types';

// Re-export the console's locale-neutral mono date formatter so every manage
// module importing `formatDateTime` from './agentConfig' (and the
// environmentConfig / assistantConfig re-exports) renders one compact-ISO
// `YYYY-MM-DD HH:mm` shape across every list and record page.
export { formatDateTime, formatDateTimeSeconds } from './console/format';

function asString(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

export function agentDisplayName(a: AgentConfig): string {
  const meta = a.display_meta as Record<string, unknown> | undefined;
  const dn = asString(meta?.display_name).trim();
  return dn || a.name;
}

// `model` is a first-class required Agent field, so callers do not need to
// inspect the runtime-only `model_config` object (agent_schema.py).
/**
 * The model this agent pins, or '' when it pins none.
 *
 * Empty is not missing — it means the deployment's default is used, which is a
 * different fact from "unknown" and reads differently to the reader. The
 * placeholder is not this function's business: a sentinel here forces every
 * caller to either test for it (`=== '—'`) or print a dash that contradicts the
 * list beside it. {@link agentModelLabel} owns the wording.
 */
export function agentModel(a: AgentConfig): string {
  return asString(a.model).trim();
}

/** What the reader is shown for {@link agentModel} — never a bare dash. */
export function agentModelLabel(a: AgentConfig): string {
  return agentModel(a) || i18n.t('common:model_deployment_default');
}

export function agentUpdatedAt(a: AgentConfig): string {
  return asString(a.updated_at);
}

export function isEmptyValue(value: unknown): boolean {
  if (value == null || value === '') return true;
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === 'object') return Object.keys(value as Record<string, unknown>).length === 0;
  return false;
}

// Blank starting draft for the single-step create page. Deliberately almost
// empty: `model` and `environment_name` are required choices the operator makes
// (the server rejects a payload missing either), and seeding empty containers
// would only persist `{}` debris the schema-driven form prunes anyway.
export function buildAgentDraft(): AgentDraft {
  return { name: '', enabled: true };
}
