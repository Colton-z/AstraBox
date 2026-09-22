import { isAssistantConversationSession } from '../utils/format';

export type SessionRightPanelTab = 'files' | 'terminal' | 'diff' | 'agents';

export interface SessionRightPanelCapabilities {
  tabs: readonly SessionRightPanelTab[];
  defaultTab: SessionRightPanelTab;
}

// Product projection for which right-side panel tabs apply to a given session.
//
// Two questions decide it, and they are not the same question. The product
// shape (session_kind) says which surfaces can exist at all: an Assistant
// conversation shares a sandbox without an AstraBox sidecar, so it has no
// per-user terminal to expose. The Agent's own declaration then says which of
// those a person actually needs to inspect THIS Agent's work — AstraBox runs
// Agent programs of any purpose, and a diff view serves only the ones that
// edit files. A research or support Agent showing a permanently empty Diff tab
// is a promise the product does not keep.
//
// Neither question is `engine_kind`. Reading the engine is what put Claude's
// tool names in the diff panel's filter and left Codex's writes invisible with
// nothing said about why.
//
// Accepts a structural shape rather than a concrete SessionRecord so callers
// can pass any object that carries the discriminating fields without dragging
// in heavier types.
type SessionCapabilityInput =
  | {
      session_kind?: string | null;
      agent_id?: string | null;
      source_type?: string | null;
      workspace_panels?: { terminal?: boolean; diff?: boolean } | null;
    }
  | null
  | undefined;

// Always present, for every product shape. Files is the sandbox's own, and the
// Agents tab stays visible when the wire has no subagents so its empty state
// explains the capability instead of changing the panel shape per session.
const ALWAYS_TABS: readonly SessionRightPanelTab[] = ['files', 'agents'];

// The surfaces the product shape can offer at all. An Agent may decline one it
// is offered; it cannot claim one this shape does not have.
const AGENT_CHAT_OFFERS: readonly SessionRightPanelTab[] = ['terminal', 'diff'];
const ASSISTANT_OFFERS: readonly SessionRightPanelTab[] = [];

export function getSessionRightPanelCapabilities(
  session: SessionCapabilityInput,
): SessionRightPanelCapabilities {
  // SessionCapabilityInput accepts nullable wire fields; the helper consumes
  // the platform product kind without interpreting an engine identity.
  const probe = session
    ? {
        session_kind: session.session_kind ?? undefined,
      }
    : session;
  const offered = isAssistantConversationSession(probe)
    ? ASSISTANT_OFFERS
    : AGENT_CHAT_OFFERS;
  const declared = session?.workspace_panels ?? null;
  const optional = offered.filter((tab) => declared?.[tab as 'terminal' | 'diff'] === true);
  // Order is the reader's, not the filter's: files, terminal, diff, agents.
  const order: readonly SessionRightPanelTab[] = ['files', 'terminal', 'diff', 'agents'];
  const enabled = new Set<SessionRightPanelTab>([...ALWAYS_TABS, ...optional]);
  return { tabs: order.filter((tab) => enabled.has(tab)), defaultTab: 'files' };
}

// i18n keys (chat namespace) for the localizable tab labels. Diff/Agents are
// brand/tech identifiers kept verbatim and never go through i18n.
const TAB_LABEL_KEYS: Record<'files' | 'terminal', string> = {
  files: 'chat:panel.tab.files',
  terminal: 'chat:panel.tab.terminal',
};

// Minimal translate signature so this engine-capability module stays free of a
// direct react-i18next import; callers pass their component's t().
type TranslateFn = (key: string) => string;

export function getRightPanelTabLabel(
  tab: SessionRightPanelTab,
  t: TranslateFn,
  context: { changedFileCount?: number; liveSubagentCount?: number } = {},
): string {
  if (tab === 'diff') {
    const count = context.changedFileCount ?? 0;
    return count > 0 ? `Diff (${count})` : 'Diff';
  }
  if (tab === 'agents') {
    const count = context.liveSubagentCount ?? 0;
    return count > 0 ? `Agents (${count})` : 'Agents';
  }
  return t(TAB_LABEL_KEYS[tab]);
}
