// Shared read helpers for the management-side Environment (runtime preset) pages.
import type { EnvironmentConfig } from '@/types';

// Reuse the agent-side date/empty helpers verbatim — same semantics.
export { formatDateTime, isEmptyValue } from './agentConfig';

function asString(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

export function envDisplayName(e: EnvironmentConfig): string {
  const dn = asString(e.display_name).trim();
  return dn || e.name;
}

const ENGINE_WORD_LABELS: Readonly<Record<string, string>> = {
  claude: 'Claude',
  codex: 'Codex',
  deepseek: 'DeepSeek',
  pi: 'Pi',
};

/** Render an engine's identifier without requiring a per-engine UI branch. */
export function engineKindLabel(value: unknown): string {
  const words = asString(value)
    .trim()
    .split(/[._-]+/)
    .filter(Boolean);
  if (words.length === 0) return '—';
  return words
    .map((word) => ENGINE_WORD_LABELS[word.toLowerCase()] ?? `${word[0].toUpperCase()}${word.slice(1)}`)
    .join(' ');
}

// An environment may carry its own product name in display_name; this column
// shows the underlying engine archetype instead.
export function envEngineLabel(e: EnvironmentConfig): string {
  return engineKindLabel(e.engine_kind);
}

export function envSandbox(e: EnvironmentConfig): string {
  const value = asString(e.sandbox_backend).trim() || 'open_sandbox';
  return value === 'open_sandbox' ? 'OpenSandbox' : engineKindLabel(value);
}

export function envUpdatedAt(e: EnvironmentConfig): string {
  return asString(e.updated_at);
}

// Blank starting draft for the create flow. engine_kind is required, so seed a
// sensible default the admin can change.
export function buildEnvironmentDraft(): EnvironmentConfig {
  return {
    name: '',
    enabled: true,
    engine_kind: 'claude_code',
    networking: {
      type: 'limited',
      allowed_hosts: [],
      allow_mcp_servers: false,
    },
  };
}
