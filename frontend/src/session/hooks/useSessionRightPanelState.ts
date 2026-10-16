import { useEffect, useMemo, useState } from 'react';
import type { UIMessage as SDKUIMessage } from 'ai';
import type { SessionRecord } from '../../types';
import { isSandboxLiveForSession } from '../../utils/format';
import { changedFilePaths, computeFileChanges } from '../fileChanges';
import { useSessionFileChanges } from './useSessionFileChanges';
import { getSessionRightPanelCapabilities, type SessionRightPanelTab } from '../sessionCapabilities';

type TranslateFn = (key: string) => string;

// Derives the right-panel tabs and selection, diff data, agents empty-state
// copy, and files/terminal availability. The result feeds SessionRightPanel's
// props.
export function useSessionRightPanelState({
  session,
  messages,
  lifecycleState,
  working,
  t,
}: {
  session: SessionRecord;
  messages: SDKUIMessage[];
  lifecycleState: string;
  working: boolean;
  t: TranslateFn;
}) {
  // ── Right-panel capabilities (single source of truth for tab visibility) ─
  const rightPanelCaps = useMemo(
    () => getSessionRightPanelCapabilities(session),
    [session],
  );
  const diffEnabled = rightPanelCaps.tabs.includes('diff');
  const [rightTab, setRightTab] = useState<SessionRightPanelTab>(rightPanelCaps.defaultTab);

  const durableChanges = useSessionFileChanges({
    sessionId: session.session_id, enabled: diffEnabled, working,
    lastResponseId: session.last_response?.response_id, messages,
  });

  // ── File changes for diff panel ──────────────────────────────
  const fileChanges = useMemo(
    () => computeFileChanges(durableChanges.messages, diffEnabled && rightTab === 'diff'),
    [diffEnabled, durableChanges.messages, rightTab],
  );

  useEffect(() => {
    if (!rightPanelCaps.tabs.includes(rightTab)) {
      setRightTab(rightPanelCaps.defaultTab);
    }
  }, [rightPanelCaps, rightTab]);
  const [selectedDiffFile, setSelectedDiffFile] = useState<string | null>(null);
  const uniqueChangedFiles = useMemo(
    () => changedFilePaths(durableChanges.messages, diffEnabled && !durableChanges.error).length,
    [diffEnabled, durableChanges.messages, durableChanges.error],
  );

  const [terminalCwd, setTerminalCwd] = useState<string | null>(session.terminal_cwd ?? null);
  useEffect(() => {
    setTerminalCwd(session.terminal_cwd ?? null);
  }, [session.session_id, session.terminal_cwd]);

  // File/terminal access depends on the sandbox being LIVE, not on the session being
  // idle (READY). During a reply the state is PROCESSING/WAITING_INPUT but the
  // sandbox and its workspace files are fully available; gating on READY made the files
  // panel show "waiting for runtime ready" mid-reply.
  const runtimeAccessReady = isSandboxLiveForSession(session);
  const filesPanelEnabled = runtimeAccessReady && (lifecycleState === 'ready' || lifecycleState === 'busy');
  const runtimeUnavailableMessage = runtimeAccessReady ? undefined : t('chat:files.unavailable');

  return {
    rightPanelCaps,
    fileChanges,
    fileChangesLoading: durableChanges.loading,
    fileChangesError: durableChanges.error,
    refreshFileChanges: durableChanges.refresh,
    rightTab,
    setRightTab,
    selectedDiffFile,
    setSelectedDiffFile,
    uniqueChangedFiles,
    runtimeAccessReady,
    filesPanelEnabled,
    runtimeUnavailableMessage,
    terminalCwd,
    setTerminalCwd,
  };
}
