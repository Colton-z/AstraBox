// Shared read helpers for the management-side Agent pages.
import type { AgentConfig, AgentDraft, FormSchema } from '@/types';

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
 * The model this agent names.
 *
 * Every stored Agent names one; the server refuses to run an Agent that does
 * not (`AGENT_MODEL_REQUIRED`), so an empty answer is a record to repair, not
 * a deployment default.
 */
export function agentModel(a: AgentConfig): string {
  return asString(a.model).trim();
}

export function agentUpdatedAt(a: AgentConfig): string {
  return asString(a.updated_at);
}

/**
 * The most tags one Agent carries, and the longest tag in characters — the
 * server's `MAX_AGENT_TAGS` and `MAX_TAG_LENGTH`
 * (astrabox/core/service/orchestrator/agent/agent_catalog.py), which refuses a
 * save past either.
 */
export const MAX_AGENT_TAGS = 5;
export const MAX_AGENT_TAG_LENGTH = 32;

/**
 * One tag in the form the server stores it (`normalize_tag` in the same
 * module): no leading `#`, lower case, and each run of whitespace joined into
 * one `-`, so one word is one tag.
 *
 * The server lower-cases with Python's `casefold`, which also folds a few
 * letters `toLowerCase` keeps (`ß` to `ss`); for those the saved record shows
 * the server's form.
 */
export function normalizeAgentTag(raw: string): string {
  return raw.trim().replace(/^#+/, '').trim().toLowerCase().replace(/\s+/g, '-');
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
// would only persist `{}` debris the schema-driven form prunes anyway. The one
// exception is a value the schema serves as this deployment's create-time
// default (`prepared_capacity`): the server stores it when the field is left
// alone, so the form starts from it rather than showing a value the saved
// record will contradict.
export function buildAgentDraft(schema: FormSchema): AgentDraft {
  const draft: AgentDraft = { name: '', enabled: true };
  for (const field of schema.fields) {
    if (field.default !== undefined) draft[field.key] = field.default;
  }
  return draft;
}
