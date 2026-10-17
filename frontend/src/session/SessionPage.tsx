import { useCallback, useMemo } from 'react';
import { useSessionUserAction } from './hooks/useSessionUserAction';
import { SessionUserAction } from './components/SessionUserAction';
import { useTranslation } from 'react-i18next';
import { useParams, useNavigate } from 'react-router-dom';
import { ResizablePanelGroup, ResizablePanel, ResizableHandle } from '@/components/ui/resizable';
import { useSessionLifecycle } from './hooks/useSessionLifecycle';
import { useMarkSessionRead } from './hooks/useMarkSessionRead';
import { useSessionHistory } from './hooks/useSessionHistory';
import { useSessionChat } from './hooks/useSessionChat';
import { useSessionBootstrapEffects } from './hooks/useSessionBootstrapEffects';
import { useComposerInput } from './hooks/useComposerInput';
import { usePendingInteractionDisplay } from './hooks/usePendingInteractionDisplay';
import { usePermissionModeControl } from './hooks/usePermissionModeControl';
import { useSessionTermination } from './hooks/useSessionTermination';
import { useSessionRightPanelState } from './hooks/useSessionRightPanelState';
import { useSessionRefreshEffects } from './hooks/useSessionRefreshEffects';
import { useSessionBanners } from './hooks/useSessionBanners';
import { isTransientHistoryLoadError } from './sessionPageLoading';
import { doesSessionDetailBelongToRoute } from './sessionBootstrapOwnership';
import type { HistoryWindow } from './historyPages';
import { inputFailure } from './sessionMessages';
import {
  SessionLoadingState,
  SessionUnavailableState,
  SessionHistoryBlockingState,
  SessionHistoryErrorState,
} from './components/SessionBootstrapStates';
import { stopSessionChildRun } from '../api';
import { computeSessionRunStatus } from './sessionRunStatus';
import { SubagentTranscriptDrawer } from './components/SubagentTranscriptDrawer';
import { SubagentNavigationProvider } from './components/subagentNavigation';
import { useSubagentDrawer } from './hooks/useSubagentDrawer';
import { SessionHeader } from './components/SessionHeader';
import { SessionRetryBanner } from './components/SessionRetryBanner';
import { SessionConversationView } from './components/SessionConversationView';
import { SessionPendingInteractionFooter } from './components/SessionPendingInteractionFooter';
import { SessionComposerBox } from './components/SessionComposerBox';
import { SessionRightPanel } from './components/SessionRightPanel';
import { SessionSendContext, type SessionSendState } from './components/UserMessageStatus';
import { ArtifactNavigationProvider } from '@/artifacts/navigation';
import { useArtifactPanelNavigation } from '@/artifacts/useArtifactPanelNavigation';
import { useFrontendReleaseHold } from '@/hooks/useFrontendReleaseHold';

// ── Outer component: bootstrap ─────────────────────────────────

/**
 * A conversation opens from its session detail and the newest window of its
 * history; the history's `output_seq` is where the page follows the session's
 * stream from, so nothing that happens after the history was read is missed
 * and nothing in it is shown twice.
 */
export function SessionPage({
  onSessionChanged,
}: {
  onSessionChanged: () => Promise<void>;
}) {
  const { sessionId = '' } = useParams();
  const navigate = useNavigate();

  const lifecycle = useSessionLifecycle(sessionId);
  useMarkSessionRead(sessionId, lifecycle.session?.last_output_at);
  const history = useSessionHistory(sessionId);
  const retryInitialLoad = useCallback(async () => {
    await Promise.allSettled([
      lifecycle.refresh({ force: true }),
      history.refetch(),
    ]);
  }, [history, lifecycle]);

  const detailBelongsToRoute = doesSessionDetailBelongToRoute(sessionId, lifecycle.session);
  const effectiveSession = detailBelongsToRoute ? lifecycle.session : null;
  const effectiveLifecycleState =
    detailBelongsToRoute || lifecycle.lifecycleState === 'error'
      ? lifecycle.lifecycleState
      : 'loading';

  useSessionBootstrapEffects({
    sessionId,
    effectiveSession,
    lifecycle,
    onSessionChanged,
  });

  if (effectiveLifecycleState === 'loading') {
    return <SessionLoadingState />;
  }

  if (!effectiveSession) {
    const loadError = String(lifecycle.error ?? '').trim();
    return (
      <SessionUnavailableState
        loadError={loadError}
        onRetry={() => { void lifecycle.refresh({ force: true }); }}
      />
    );
  }

  const window = history.ownerSessionId === sessionId ? history.window : null;
  if (!window) {
    if (history.error && !isTransientHistoryLoadError(history.error)) {
      return <SessionHistoryErrorState historyError={history.error} onRetry={() => { void retryInitialLoad(); }} />;
    }
    return <SessionHistoryBlockingState historyError={history.error} />;
  }
  if (window.outputSeq === null) {
    return (
      <SessionHistoryErrorState
        historyError="SESSION_HISTORY_POSITION_MISSING: the history page carried no output_seq"
        onRetry={() => { void retryInitialLoad(); }}
      />
    );
  }

  return (
    <SessionPageReady
      key={sessionId}
      sessionId={sessionId}
      history={{ ...window, outputSeq: window.outputSeq }}
      session={effectiveSession}
      lifecycleState={effectiveLifecycleState}
      lifecycleError={lifecycle.error}
      lifecycleDetailStale={lifecycle.detailStale}
      refresh={lifecycle.refresh}
      terminate={lifecycle.terminate}
      endConversation={lifecycle.endConversation}
      recover={lifecycle.recover}
      onSessionChanged={onSessionChanged}
      navigate={navigate}
    />
  );
}

// ── Inner component: orchestration ─────────────────────────────

function SessionPageReady({
  sessionId,
  history,
  session,
  lifecycleState,
  lifecycleError,
  lifecycleDetailStale,
  refresh,
  terminate,
  endConversation,
  recover,
  onSessionChanged,
  navigate,
}: {
  sessionId: string;
  history: HistoryWindow & { outputSeq: number };
  session: NonNullable<ReturnType<typeof useSessionLifecycle>['session']>;
  lifecycleState: string;
  lifecycleError: string | null;
  lifecycleDetailStale: boolean;
  refresh: ReturnType<typeof useSessionLifecycle>['refresh'];
  terminate: () => Promise<void>;
  endConversation: () => Promise<void>;
  recover: () => Promise<void>;
  onSessionChanged: () => Promise<void>;
  navigate: ReturnType<typeof useNavigate>;
}) {
  const { t } = useTranslation();
  // ── Permission mode ──────────────────────────────────────────
  const {
    permissionMode,
    modeSwitching,
    canChangePermissionMode,
    cyclePermissionMode,
    selectPermissionMode,
    hasPermissionModes,
  } = usePermissionModeControl({
    sessionId,
    initialPermissionMode: session.permission_mode ?? undefined,
    availableModes: session.engine_capabilities?.permission_modes ?? [],
    lifecycleState,
  });

  // ── The conversation ─────────────────────────────────────────
  const artifacts = useArtifactPanelNavigation(history.messages, () => setRightTab('artifacts'));
  const chat = useSessionChat({
    sessionId,
    session,
    history,
    refresh,
    onSessionChanged: () => { void onSessionChanged(); },
    onArtifactAppeared: artifacts.appeared,
  });
  const { messages, isWorking, isSending, isStopping } = chat;
  const isTerminated = session.state === 'TERMINATED' || lifecycleState === 'terminated' || lifecycleState === 'deleted'
    || session.agent_state === 'DELETING' || session.agent_state === 'DELETED';

  // ── Pending interaction ──────────────────────────────────────
  const {
    visiblePendingInteraction, hasPendingInteraction, pendingToolCallId,
    pendingConversationScrollKey,
  } = usePendingInteractionDisplay({
    messages,
    pendingInteraction: chat.pendingInteraction,
    isTerminated,
  });

  // ── Header and send capability ───────────────────────────────
  const {
    transparentlyRecoverable, canSendNow, canQueueMessage,
    canSend, headerStatusLabel, headerIsLive, headerTone, headerRunState,
  } = computeSessionRunStatus({
    session,
    lifecycleState,
    isTerminated,
    hasPendingInteraction,
    isSending,
    isWorking,
    isStopping,
    t,
  });
  // Runtime startup/recovery and prerequisite-related refusals ask the gate
  // again. Ordinary response activity does not invalidate this status read.
  const userActionInvalidation = useMemo(() => JSON.stringify([
    session.state === 'CREATING', session.runtime_unavailable === true, session.sandbox_id ?? null,
    ...[...chat.unsent].filter(([, entry]) => entry.userAction).map(([id]) => id),
    ...messages.filter((message) => inputFailure(message)?.user_action).map((message) => message.id),
  ]), [session.state, session.runtime_unavailable, session.sandbox_id, chat.unsent, messages]);
  const prerequisite = useSessionUserAction(sessionId, session.user_id ?? '', userActionInvalidation);
  const composerCanSend = prerequisite.ready && canSend;

  // ── Composer input (draft, slash commands, history, keydown) ─
  const {
    draft, setDraft, hasSlashCommands, showSlashPopup, filteredSlashCommands, slashPopupIndex,
    slashPopupRef, acceptSlashCommand, handleKeyDown, submitText,
  } = useComposerInput({
    sessionId,
    messages,
    slashCommandDetails: session.slash_command_details,
    canSend: composerCanSend,
    sendMessage: chat.sendMessage,
    canChangePermissionMode,
    cyclePermissionMode,
  });

  const stopGeneration = useCallback(async () => {
    await chat.handleStopGeneration();
  }, [chat]);

  const sendLabel = isTerminated
    ? t('chat:status.terminated')
    : canQueueMessage && !canSendNow ? t('chat:composer.queue') : t('chat:composer.send');

  const sendState = useMemo<SessionSendState>(() => ({
    unsent: chat.unsent,
    heldInputIds: chat.heldInputIds,
    canSend: composerCanSend,
    retryUnsent: chat.retryUnsent,
    removeUnsent: chat.removeUnsent,
    sendAgain: chat.sendAgain,
  }), [chat.heldInputIds, chat.removeUnsent, chat.retryUnsent, chat.sendAgain, chat.unsent, composerCanSend]);

  // ── Terminate / delete ───────────────────────────────────────
  const { isAgentChat, isAssistantConversation, terminateLoading, handleTerminate, handleEndConversation } = useSessionTermination({
    session,
    lifecycleState,
    terminate,
    endConversation,
    onSessionChanged,
    navigate,
  });

  // A prompt being typed is not something a release may take away.
  useFrontendReleaseHold(draft.trim().length > 0);

  // ── Auto-refresh (files panel when work ends; manual-refresh event) ─
  const { filesRefreshNonce } = useSessionRefreshEffects({
    working: isWorking || isSending,
    handleRehydrate: chat.rehydrate,
  });

  // ── Right-panel derived state (tabs, diff data, availability) ─
  const {
    rightPanelCaps, fileChanges, rightTab, setRightTab, selectedDiffFile, setSelectedDiffFile,
    uniqueChangedFiles, runtimeAccessReady, filesPanelEnabled, runtimeUnavailableMessage,
    terminalCwd, setTerminalCwd, fileChangesLoading, fileChangesError, refreshFileChanges,
  } = useSessionRightPanelState({
    session,
    messages,
    lifecycleState,
    working: isWorking,
    t,
  });

  // ── Subagent (Agent / Task tool) registry + overlay drawer ──
  const {
    subagentRegistry,
    projectionError: childRunProjectionError,
    refreshChildRuns,
    selectedChildRunId,
    setSelectedChildRunId,
    openSubagent,
  } = useSubagentDrawer({
    sessionId,
    childRunRevision: chat.childRunRevision,
    rightPanelCaps,
    setRightTab,
    working: isWorking,
    hasPendingInteraction,
  });

  // ── Banners ──────────────────────────────────────────────────
  const {
    showRuntimeUnavailableBanner, showSessionLastError, showRecoverButton,
    showBanner, bannerErrorMessage, showTransientBackendMessage,
  } = useSessionBanners({
    session,
    hasPendingInteraction,
    lifecycleDetailStale,
    isTerminated,
    transparentlyRecoverable,
    isAssistantConversation,
    lifecycleError,
    streamTrouble: chat.streamTrouble,
    isWorking,
    isSending,
  });

  // ── Render ───────────────────────────────────────────────────

  return (
    <ArtifactNavigationProvider open={artifacts.open} setComposer={setDraft}>
    <SubagentNavigationProvider openSubagent={openSubagent} agents={subagentRegistry.agents}>
    <SessionSendContext.Provider value={sendState}>
    {/* react-resizable-panels' Group overwrites a data-testid prop with its
        own `id ?? useId()` — the id prop is therefore the only way to get a
        stable data-testid="run-view" into the DOM (the e2e anchor). */}
    <ResizablePanelGroup orientation="horizontal" className="h-full" id="run-view">
      <ResizablePanel defaultSize={70} minSize={30} className="relative flex flex-col overflow-hidden">
        <SessionHeader
          session={session}
          headerIsLive={headerIsLive}
          headerTone={headerTone}
          headerStatusLabel={headerStatusLabel}
          headerRunState={headerRunState}
          showRuntimeUnavailableBanner={showRuntimeUnavailableBanner}
          showSessionLastError={showSessionLastError}
          isAssistantConversation={isAssistantConversation}
          isAgentChat={isAgentChat}
          isTerminated={isTerminated}
          lifecycleState={lifecycleState}
          terminateLoading={terminateLoading}
          handleEndConversation={handleEndConversation}
          handleTerminate={handleTerminate}
          showRecoverButton={showRecoverButton}
          handleRecover={() => { void recover(); }}
        />

        {showBanner && (
          <SessionRetryBanner
            showTransientBackendMessage={showTransientBackendMessage}
            bannerErrorMessage={bannerErrorMessage}
          />
        )}

        {/* `SubagentTranscriptDrawer` is positioned against this wrapper so it
            covers only `SessionConversationView`. The composer and pending-
            interaction footer remain outside the overlay and usable. */}
        <div className="relative flex min-h-0 flex-1 flex-col">
        <SessionConversationView
          sessionId={sessionId}
          messages={messages}
          isWorking={isWorking || isSending}
          pendingToolCallId={pendingToolCallId}
          writingMessageId={chat.writingMessageId}
          hasPendingInteraction={hasPendingInteraction}
          pendingConversationScrollKey={pendingConversationScrollKey}
          firstItemIndex={chat.firstItemIndex}
          hasMore={chat.hasMore}
          loadingMore={chat.loadingMore}
          loadMoreError={chat.loadMoreError}
          onLoadOlder={chat.loadOlder}
          assistantId={isAssistantConversation ? session.assistant_id : null}
        />
        <SubagentTranscriptDrawer
          sessionId={sessionId}
          refreshRevision={chat.childRunRevision}
          childRunId={selectedChildRunId}
          entry={
            selectedChildRunId
              ? subagentRegistry.getAgent(selectedChildRunId)
              : undefined
          }
          onClose={() => setSelectedChildRunId(null)}
          onStopChildRun={async (childRunId) => {
            await stopSessionChildRun(session.session_id, childRunId);
            void refreshChildRuns().catch(() => {});
          }}
        />
        </div>

        {visiblePendingInteraction ? (
          <SessionPendingInteractionFooter
            interaction={visiblePendingInteraction}
            submitting={chat.interactionSubmitting}
            onSubmit={chat.handleInteractionSubmit}
            isInterruptSettling={isStopping}
            onStop={stopGeneration}
            permissionMode={permissionMode}
            permissionModes={session.engine_capabilities?.permission_modes ?? []}
            showPermissionMode={hasPermissionModes}
            canChangePermissionMode={canChangePermissionMode}
            modeSwitching={modeSwitching}
            cyclePermissionMode={cyclePermissionMode}
            selectPermissionMode={selectPermissionMode}
          />
        ) : (
          <SessionComposerBox
            userAction={<SessionUserAction gate={prerequisite} />}
            sessionId={sessionId}
            hasSlashCommands={hasSlashCommands}
            showSlashPopup={showSlashPopup}
            slashPopupRef={slashPopupRef}
            filteredSlashCommands={filteredSlashCommands}
            slashPopupIndex={slashPopupIndex}
            acceptSlashCommand={acceptSlashCommand}
            submitText={submitText}
            acceptsImages={session.engine_capabilities?.input_content_types?.includes('image') === true}
            submitLabel={sendLabel}
            draft={draft}
            setDraft={setDraft}
            handleKeyDown={handleKeyDown}
            canSend={composerCanSend}
            isTerminated={isTerminated}
            isStreaming={isWorking}
            isSubmitted={isSending}
            hasPendingInteraction={hasPendingInteraction}
            permissionMode={permissionMode}
            permissionModes={session.engine_capabilities?.permission_modes ?? []}
            showPermissionMode={hasPermissionModes}
            canChangePermissionMode={canChangePermissionMode}
            modeSwitching={modeSwitching}
            cyclePermissionMode={cyclePermissionMode}
            selectPermissionMode={selectPermissionMode}
            isInterruptSettling={isStopping}
            wrappedStopGeneration={stopGeneration}
          />
        )}
      </ResizablePanel>

      <ResizableHandle withHandle />

      <SessionRightPanel
        artifactSelection={artifacts.selectedId}
        setArtifactSelection={artifacts.setSelectedId}
        artifactRevision={artifacts.revision}
        assistantId={session.assistant_id}
        assistantConversation={isAssistantConversation}
        rightTab={rightTab}
        setRightTab={setRightTab}
        rightPanelCaps={rightPanelCaps}
        uniqueChangedFiles={uniqueChangedFiles}
        subagentRegistry={subagentRegistry}
        childRunProjectionError={childRunProjectionError}
        sessionId={sessionId}
        filesPanelEnabled={filesPanelEnabled}
        runtimeUnavailableMessage={runtimeUnavailableMessage}
        filesRefreshNonce={filesRefreshNonce}
        runtimeAccessReady={runtimeAccessReady}
        lifecycleState={lifecycleState}
        terminalCwd={terminalCwd}
        setTerminalCwd={setTerminalCwd}
        selectedChildRunId={selectedChildRunId}
        setSelectedChildRunId={setSelectedChildRunId}
        fileChanges={fileChanges}
        fileChangesLoading={fileChangesLoading}
        fileChangesError={fileChangesError}
        refreshFileChanges={refreshFileChanges}
        selectedDiffFile={selectedDiffFile}
        setSelectedDiffFile={setSelectedDiffFile}
      />
    </ResizablePanelGroup>
    </SessionSendContext.Provider>
    </SubagentNavigationProvider>
    </ArtifactNavigationProvider>
  );
}
