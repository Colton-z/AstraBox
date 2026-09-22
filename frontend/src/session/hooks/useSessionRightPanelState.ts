import { useEffect, useMemo, useState } from 'react';
import type { UIMessage as SDKUIMessage } from 'ai';
import type { SessionRecord } from '../../types';
import { isSandboxLiveForSession } from '../../utils/format';
import { changedFilePaths, computeFileChanges } from '../fileChanges';
import { getSessionRightPanelCapabilities, type SessionRightPanelTab } from '../sessionCapabilities';

type TranslateFn = (key: string) => string;

// Derives the right-panel tabs and selection, diff data, agents empty-state
// copy, and files/terminal availability. The result feeds SessionRightPanel's
// props.
export function useSessionRightPanelState({
  session,
  messages,
  lifecycleState,
  isAgentRuntimeDeleted,
  t,
}: {
  session: SessionRecord;
  messages: SDKUIMessage[];
  lifecycleState: string;
  isAgentRuntimeDeleted: boolean;
  t: TranslateFn;
}) {
  // ── Right-panel capabilities (single source of truth for tab visibility) ─
  const rightPanelCaps = useMemo(
    () => getSessionRightPanelCapabilities(session),
    [session],
  );
  const diffEnabled = rightPanelCaps.tabs.includes('diff');
  const [rightTab, setRightTab] = useState<SessionRightPanelTab>(rightPanelCaps.defaultTab);

  // ── File changes for diff panel ──────────────────────────────
  const fileChanges = useMemo(
    () => computeFileChanges(messages, diffEnabled && rightTab === 'diff'),
    [diffEnabled, messages, rightTab],
  );

  useEffect(() => {
    if (!rightPanelCaps.tabs.includes(rightTab)) {
      setRightTab(rightPanelCaps.defaultTab);
    }
  }, [rightPanelCaps, rightTab]);
  const [selectedDiffFile, setSelectedDiffFile] = useState<string | null>(null);
  const uniqueChangedFiles = useMemo(
    () => changedFilePaths(messages, diffEnabled).length,
    [diffEnabled, messages],
  );

  const [terminalCwd, setTerminalCwd] = useState<string | null>(session.terminal_cwd ?? null);
  useEffect(() => {
    setTerminalCwd(session.terminal_cwd ?? null);
  }, [session.session_id, session.terminal_cwd]);

  // File/terminal access depends on the sandbox being LIVE, not on the session being
  // idle (READY). During a streaming reply the state is BUSY/WAITING_INPUT but the
  // sandbox + its workspace files are fully available; gating on READY made the files
  // panel show "waiting for runtime ready" mid-reply. Liveness allows ready/busy/background here.
  const runtimeAccessReady = isSandboxLiveForSession(session);
  const filesPanelEnabled = runtimeAccessReady && (lifecycleState === 'ready' || lifecycleState === 'busy' || lifecycleState === 'background');
  const runtimeUnavailableMessage = runtimeAccessReady
    ? undefined
    : isAgentRuntimeDeleted
      ? t('chat:files.unavailable_agent_terminated')
      : t('chat:files.unavailable');

  return {
    rightPanelCaps,
    fileChanges,
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
