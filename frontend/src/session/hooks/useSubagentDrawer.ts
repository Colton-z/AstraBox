import { useCallback, useEffect, useRef, useState } from 'react';

import { listSessionChildRuns, type SessionChildRun } from '../../api';
import type { SessionRightPanelCapabilities, SessionRightPanelTab } from '../sessionCapabilities';
import { shouldPollBackgroundSubagentHistory } from '../sessionPolling';
import { useSubagentRegistry } from './useSubagentRegistry';
import { keepsLastRead } from '../../hooks/useKeepCurrent';

// Owns the Session-scoped child-run read model and transcript-drawer selection.
// Root messages are deliberately absent: the backend projection is the sole
// lifecycle authority for both live updates and reload recovery.
export function useSubagentDrawer({
  sessionId,
  childRunRevision,
  rightPanelCaps,
  setRightTab,
  lifecycleState,
  isSubmitted,
  isStreaming,
  hasPendingInteraction,
}: {
  sessionId: string;
  childRunRevision: number;
  rightPanelCaps: SessionRightPanelCapabilities;
  setRightTab: (tab: SessionRightPanelTab) => void;
  lifecycleState: string;
  isSubmitted: boolean;
  isStreaming: boolean;
  hasPendingInteraction: boolean;
}) {
  const [childRuns, setChildRuns] = useState<SessionChildRun[]>([]);
  const [projectionError, setProjectionError] = useState<string | null>(null);
  const requestGeneration = useRef(0);
  const loadedSession = useRef<string | null>(null);
  const lifecycleBoundaryRef = useRef({ sessionId, state: lifecycleState });
  const subagentRegistry = useSubagentRegistry(childRuns);
  const [selectedChildRunId, setSelectedChildRunId] = useState<string | null>(null);

  const refreshChildRuns = useCallback(async () => {
    const generation = ++requestGeneration.current;
    try {
      const page = await listSessionChildRuns(sessionId);
      if (generation !== requestGeneration.current) return;
      setChildRuns(page.child_runs);
      loadedSession.current = sessionId;
      setProjectionError(null);
    } catch (error) {
      if (generation !== requestGeneration.current) return;
      const message = error instanceof Error ? error.message : String(error);
      if (!keepsLastRead(error, { background: true }, loadedSession.current === sessionId)) {
        setProjectionError(message);
      }
      throw error;
    }
  }, [sessionId]);

  useEffect(() => {
    const reconnect = () => { void refreshChildRuns().catch(() => {}); };
    window.addEventListener('online', reconnect);
    return () => window.removeEventListener('online', reconnect);
  }, [refreshChildRuns]);

  useEffect(() => {
    requestGeneration.current += 1;
    loadedSession.current = null;
    setChildRuns([]);
    setProjectionError(null);
    setSelectedChildRunId(null);
  }, [sessionId]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void refreshChildRuns().catch(() => {});
    }, 100);
    return () => window.clearTimeout(timer);
  }, [childRunRevision, refreshChildRuns]);

  useEffect(() => {
    const previous = lifecycleBoundaryRef.current;
    lifecycleBoundaryRef.current = { sessionId, state: lifecycleState };
    // Native child activity clears through the durable child-run projection.
    // Consume that boundary before the background poller stops.
    if (
      previous.sessionId === sessionId
      && previous.state === 'background'
      && lifecycleState === 'ready'
    ) {
      void refreshChildRuns().catch(() => {});
    }
  }, [lifecycleState, refreshChildRuns, sessionId]);

  const openSubagent = useCallback(
    (childRunId: string) => {
      setSelectedChildRunId(childRunId);
      if (rightPanelCaps.tabs.includes('agents')) {
        setRightTab('agents');
      }
    },
    [rightPanelCaps, setRightTab],
  );

  useEffect(() => {
    if (!shouldPollBackgroundSubagentHistory({
      lifecycleState,
      liveSubagentCount: subagentRegistry.liveCount,
      isSubmitted,
      isStreaming,
      hasPendingInteraction,
    })) {
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      if (cancelled) return;
      await refreshChildRuns().catch(() => {});
      if (cancelled) return;
      timer = window.setTimeout(() => { void poll(); }, 2500);
    };
    timer = window.setTimeout(() => { void poll(); }, 1000);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [
    hasPendingInteraction,
    isStreaming,
    isSubmitted,
    lifecycleState,
    refreshChildRuns,
    subagentRegistry.liveCount,
  ]);

  return {
    subagentRegistry,
    projectionError,
    refreshChildRuns,
    selectedChildRunId,
    setSelectedChildRunId,
    openSubagent,
  };
}
