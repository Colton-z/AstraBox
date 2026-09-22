import React, { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, useNavigate } from 'react-router-dom';
import type { UIMessage as SDKUIMessage } from 'ai';
import { Queue, QueueItem, QueueItemIndicator, QueueItemContent } from '@/components/ai-elements/queue';
import { ResizablePanelGroup, ResizablePanel, ResizableHandle } from '@/components/ui/resizable';
import { useSessionLifecycle } from './hooks/useSessionLifecycle';
import { useFirstPageMessages } from './hooks/useFirstPageMessages';
import { useSessionChat } from './hooks/useSessionChat';
import { useSessionBootstrapEffects } from './hooks/useSessionBootstrapEffects';
import { useMessageQueue } from './hooks/useMessageQueue';
import { useComposerInput } from './hooks/useComposerInput';
import { usePendingInteractionDisplay } from './hooks/usePendingInteractionDisplay';
import { usePermissionModeControl } from './hooks/usePermissionModeControl';
import { useSessionTermination } from './hooks/useSessionTermination';
import { useSessionRightPanelState } from './hooks/useSessionRightPanelState';
import { useInitialMessagesCache } from './hooks/useInitialMessagesCache';
import { useSessionRefreshEffects } from './hooks/useSessionRefreshEffects';
import { useSessionBanners } from './hooks/useSessionBanners';
import { prepareInitialMessages } from './prepareInitialMessages';
import {
  shouldShowHistoryBlockingLoader,
  shouldShowInitialHistoryLoadError,
} from './sessionPageLoading';
import {
  doesFirstPageBelongToRoute,
  doesSessionDetailBelongToRoute,
} from './sessionBootstrapOwnership';
import {
  SessionLoadingState,
  SessionUnavailableState,
  SessionHistoryBlockingState,
  SessionHistoryErrorState,
} from './components/SessionBootstrapStates';
import type {
  InteractionResponse,
  PendingInteraction,
  SessionRecord,
} from '../types';
import { stateLabel } from '../utils/format';
import { stopSessionChildRun } from '../api';
import {
  getSdkMessageTurnId,
} from './utils/chatHelpers';
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
import { useFrontendReleaseHold } from '@/hooks/useFrontendReleaseHold';

const NO_DURABLE_RECORDS: ReturnType<typeof useFirstPageMessages>['durableRecords'] = [];

// ── Outer component: bootstrap ─────────────────────────────────

export function SessionPage({
  onSessionChanged,
}: {
  onSessionChanged: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const { sessionId = '' } = useParams();
  const navigate = useNavigate();

  const lifecycle = useSessionLifecycle(sessionId);
  const firstPage = useFirstPageMessages(
    sessionId,
    lifecycle.bootstrapPendingInteraction,
  );
  const retryInitialLoad = useCallback(async () => {
    await Promise.allSettled([
      lifecycle.refresh({ force: true }),
      firstPage.refetch(),
    ]);
  }, [firstPage, lifecycle]);

  const detailBelongsToRoute = doesSessionDetailBelongToRoute(sessionId, lifecycle.session);
  const historyBelongsToRoute = doesFirstPageBelongToRoute(sessionId, firstPage.ownerSessionId);
  const effectiveSession = detailBelongsToRoute ? lifecycle.session : null;
  const effectivePendingInteraction = detailBelongsToRoute ? lifecycle.pendingInteraction : null;
  const effectiveLifecycleState =
    detailBelongsToRoute || lifecycle.lifecycleState === 'error'
      ? lifecycle.lifecycleState
      : 'loading';
  // A route-mismatched history stands in as empty, and that placeholder is a
  // module constant on purpose: everything downstream keys effects on this
  // array's identity, so a fresh literal here re-runs them on every render for
  // as long as the mismatch lasts.
  const effectiveHistory = historyBelongsToRoute
    ? firstPage
    : {
        ...firstPage,
        durableRecords: NO_DURABLE_RECORDS,
        overlay: null,
        sessionFrameSeq: null,
        loading: true,
        error: null,
        loadedOnce: false,
      };

  const initialMessagesRef = useInitialMessagesCache({
    sessionId,
    effectiveLifecycleState,
    effectiveSession,
    effectiveHistory,
  });

  useSessionBootstrapEffects({
    sessionId,
    effectiveSession,
    effectiveLifecycleState,
    effectiveHistory,
    lifecycle,
    firstPage,
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

  if (shouldShowHistoryBlockingLoader({
    historyLoading: effectiveHistory.loading,
    lifecycleState: effectiveLifecycleState,
    messageCount: effectiveHistory.durableRecords.length,
    historyLoadedOnce: effectiveHistory.loadedOnce,
    historyError: effectiveHistory.error,
  })) {
    return <SessionHistoryBlockingState historyError={effectiveHistory.error} />;
  }

  if (shouldShowInitialHistoryLoadError({
    historyLoading: effectiveHistory.loading,
    lifecycleState: effectiveLifecycleState,
    messageCount: effectiveHistory.durableRecords.length,
    historyLoadedOnce: effectiveHistory.loadedOnce,
    historyError: effectiveHistory.error,
  })) {
    return <SessionHistoryErrorState historyError={effectiveHistory.error} onRetry={() => { void retryInitialLoad(); }} />;
  }

  const needsResume = effectiveLifecycleState === 'busy';
  const initialMessages =
    initialMessagesRef.current?.sessionId === sessionId
      ? initialMessagesRef.current.messages
      : prepareInitialMessages(effectiveHistory.durableRecords, effectiveHistory.overlay, effectiveSession);

  return (
    <SessionPageReady
      key={sessionId}
      sessionId={sessionId}
      durableRecords={effectiveHistory.durableRecords}
      initialMessages={initialMessages}
      overlay={effectiveHistory.overlay}
      sessionFrameSeq={effectiveHistory.sessionFrameSeq}
      needsResume={needsResume}
      session={effectiveSession}
      pendingInteraction={effectivePendingInteraction}
      lifecycleState={effectiveLifecycleState}
      lifecycleError={lifecycle.error}
      lifecycleDetailStale={lifecycle.detailStale}
      historyError={effectiveHistory.error}
      historyHasMore={effectiveHistory.hasMore}
      historyLoadingMore={effectiveHistory.loadingMore}
      historyLoadMoreError={effectiveHistory.loadMoreError}
      loadOlderHistory={effectiveHistory.loadOlder}
      refresh={lifecycle.refresh}
      refreshAuthoritativeHistory={effectiveHistory.refetch}
      observePendingInteraction={lifecycle.observePendingInteraction}
      clearPendingInteraction={lifecycle.clearPendingInteraction}
      interrupt={lifecycle.interrupt}
      terminate={lifecycle.terminate}
      endConversation={lifecycle.endConversation}
      recover={lifecycle.recover}
      deleteSession={lifecycle.deleteSession}
      onSessionChanged={onSessionChanged}
      navigate={navigate}
    />
  );
}

// ── Inner component: orchestration ─────────────────────────────

function SessionPageReady({
  sessionId,
  durableRecords,
  initialMessages,
  overlay,
  sessionFrameSeq,
  needsResume,
  session,
  pendingInteraction: sessionPendingInteraction,
  lifecycleState,
  lifecycleError,
  lifecycleDetailStale,
  historyError,
  historyHasMore,
  historyLoadingMore,
  historyLoadMoreError,
  loadOlderHistory,
  refresh,
  refreshAuthoritativeHistory,
  observePendingInteraction,
  clearPendingInteraction,
  interrupt,
  terminate,
  endConversation,
  recover,
  deleteSession,
  onSessionChanged,
  navigate,
}: {
  sessionId: string;
  durableRecords: ReturnType<typeof useFirstPageMessages>['durableRecords'];
  initialMessages: SDKUIMessage[];
  overlay: ReturnType<typeof useFirstPageMessages>['overlay'];
  sessionFrameSeq: ReturnType<typeof useFirstPageMessages>['sessionFrameSeq'];
  needsResume: boolean;
  session: NonNullable<ReturnType<typeof useSessionLifecycle>['session']>;
  pendingInteraction: PendingInteraction | null;
  lifecycleState: string;
  lifecycleError: string | null;
  lifecycleDetailStale: boolean;
  historyError: string | null;
  historyHasMore: boolean;
  historyLoadingMore: boolean;
  historyLoadMoreError: string | null;
  loadOlderHistory: () => Promise<void>;
  refresh: (options?: { force?: boolean }) => Promise<any>;
  refreshAuthoritativeHistory: ReturnType<typeof useFirstPageMessages>['refetch'];
  observePendingInteraction: ReturnType<
    typeof useSessionLifecycle
  >['observePendingInteraction'];
  clearPendingInteraction: ReturnType<
    typeof useSessionLifecycle
  >['clearPendingInteraction'];
  interrupt: () => Promise<void>;
  terminate: () => Promise<void>;
  endConversation: () => Promise<void>;
  recover: () => Promise<void>;
  deleteSession: () => Promise<void>;
  onSessionChanged: () => Promise<void>;
  navigate: ReturnType<typeof useNavigate>;
}) {
  const { t } = useTranslation();
  // ── Permission mode ──────────────────────────────────────────
  const {
    permissionMode,
    permissionModeRef,
    modeSwitching,
    canChangePermissionMode,
    cyclePermissionMode,
    selectPermissionMode,
    hasPermissionModes,
  } = usePermissionModeControl({
    sessionId,
    initialPermissionMode: session.permission_mode,
    availableModes: session.engine_capabilities?.permission_modes ?? [],
    lifecycleState,
  });

  // ── Core chat hook ───────────────────────────────────────────
  const chat = useSessionChat({
    sessionId,
    session,
    durableRecords,
    initialMessages,
    overlay,
    sessionFrameSeq,
    needsResume,
    lifecycleState,
    liveSubscriptionEnabled: (
      lifecycleState === 'ready'
      || lifecycleState === 'background'
      || lifecycleState === 'busy'
    ),
    pendingInteraction: sessionPendingInteraction,
    permissionModeRef,
    refresh,
    refreshAuthoritativeHistory,
    observePendingInteraction,
    clearPendingInteraction,
    interrupt,
    recover,
  });

  const {
    messages, childRunRevision, status, chatError, clearError, historyFirstItemIndex,
    isSubmitted, isStreaming, outbox, setOutbox,
    isInterruptSettling,
    retryIntent, transientBannerVisible,
    showRetryBanner, showRetryButton, interactionSubmitting,
    sendClientMessageNow, handleInteractionSubmit, handleStopGeneration,
    handleRehydrate, handleRetry, handleRecover,
  } = chat;

  const activeTurnId = String(session.current_turn_id ?? '').trim() || null;
  const isTerminated = session.state === 'TERMINATED' || lifecycleState === 'terminated' || lifecycleState === 'deleted';

  // ── Pending interaction ──────────────────────────────────────
  const {
    visiblePendingInteraction, hasPendingInteraction, pendingToolCallId,
    pendingConversationScrollKey, handleVisibleInteractionSubmit,
  } = usePendingInteractionDisplay({
    messages,
    sessionPendingInteraction,
    isTerminated,
    interactionSubmitting,
    handleInteractionSubmit,
  });

  // ── Effective lifecycle + send capability ─────────────────────
  const {
    isAgentRuntimeDeleted, transparentlyRecoverable, canSendNow, canQueueMessage,
    canSend, headerStatusLabel, headerIsLive, headerTone, headerRunState,
  } = computeSessionRunStatus({
    session,
    lifecycleState,
    isTerminated,
    hasPendingInteraction,
    isSubmitted,
    isStreaming,
    isInterruptSettling,
    t,
  });
  // A creating runtime has no engine FIFO to receive an input yet. Once
  // created, both idle and busy Agents accept into the same durable queue.
  const composerCanSend = canSend && lifecycleState !== 'creating';
  const { displayedQueue, canRetryQueued, removeQueueItem, retryQueueItem } = useMessageQueue({
    outbox,
    setOutbox,
    canSendNow,
    canQueueMessage,
    sendClientMessageNow,
  });

  // ── Composer input (draft, slash commands, history, keydown) ─
  const {
    draft, setDraft, hasSlashCommands, showSlashPopup, filteredSlashCommands, slashPopupIndex,
    slashPopupRef, acceptSlashCommand, handleKeyDown, submitText,
  } = useComposerInput({
    sessionId,
    messages,
    slashCommandDetails: session.slash_command_details,
    canSend: composerCanSend,
    sendClientMessageNow,
    canChangePermissionMode,
    cyclePermissionMode,
  });

  // ── Interrupt loading ────────────────────────────────────────
  const wrappedStopGeneration = useCallback(async () => {
    if (isInterruptSettling) return;
    await handleStopGeneration();
  }, [isInterruptSettling, handleStopGeneration]);

  // ── Send label ───────────────────────────────────────────────
  const sendLabel = isTerminated || isAgentRuntimeDeleted ? t('chat:status.terminated') : canQueueMessage && !canSendNow ? t('chat:composer.queue') : t('chat:composer.send');

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
  const { filesRefreshNonce } = useSessionRefreshEffects({ isStreaming, isSubmitted, lifecycleState, handleRehydrate });

  // ── Right-panel derived state (tabs, diff data, availability) ─
  const {
    rightPanelCaps, fileChanges, rightTab, setRightTab, selectedDiffFile, setSelectedDiffFile,
    uniqueChangedFiles, runtimeAccessReady, filesPanelEnabled, runtimeUnavailableMessage,
    terminalCwd, setTerminalCwd,
  } = useSessionRightPanelState({
    session,
    messages,
    lifecycleState,
    isAgentRuntimeDeleted,
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
    childRunRevision,
    rightPanelCaps,
    setRightTab,
    lifecycleState,
    isSubmitted,
    isStreaming,
    hasPendingInteraction,
  });

  // ── Banners ──────────────────────────────────────────────────
  const {
    showRuntimeUnavailableBanner, showSessionLastError, showRecoverButton,
    combinedShowRetryBanner, bannerErrorMessage, showTransientBackendMessage,
  } = useSessionBanners({
    session,
    hasPendingInteraction,
    lifecycleDetailStale,
    isTerminated,
    transparentlyRecoverable,
    isAssistantConversation,
    isAgentRuntimeDeleted,
    lifecycleState,
    showRetryBanner,
    lifecycleError,
    historyError,
    chatErrorMessage: chatError?.message,
    transientBannerVisible,
  });

  // ── Render ───────────────────────────────────────────────────

  return (
    <SubagentNavigationProvider openSubagent={openSubagent} agents={subagentRegistry.agents}>
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
          handleRecover={handleRecover}
        />

        {combinedShowRetryBanner && (
          <SessionRetryBanner
            showTransientBackendMessage={showTransientBackendMessage}
            retryIntent={retryIntent}
            bannerErrorMessage={bannerErrorMessage}
            showRetryButton={showRetryButton}
            handleRetry={handleRetry}
          />
        )}

        {/* `SubagentTranscriptDrawer` is positioned against this wrapper so it
            covers only `SessionConversationView`. The composer and pending-
            interaction footer remain outside the overlay and usable. */}
        <div className="relative flex min-h-0 flex-1 flex-col">
        <SessionConversationView
          sessionId={sessionId}
          messages={messages}
          displayedQueueLength={displayedQueue.length}
          isStreaming={isStreaming}
          isSubmitted={isSubmitted}
          pendingToolCallId={pendingToolCallId}
          activeTurnId={activeTurnId}
          hasPendingInteraction={hasPendingInteraction}
          pendingConversationScrollKey={pendingConversationScrollKey}
          firstItemIndex={historyFirstItemIndex}
          hasMore={historyHasMore}
          loadingMore={historyLoadingMore}
          loadMoreError={historyLoadMoreError}
          onLoadOlder={loadOlderHistory}
        />
        <SubagentTranscriptDrawer
          sessionId={sessionId}
          refreshRevision={childRunRevision}
          backgroundTasksPending={Boolean(session.background_task_state)}
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
            displayedQueue={displayedQueue}
            removeQueueItem={removeQueueItem}
            retryQueueItem={retryQueueItem}
            canRetryQueued={canRetryQueued}
            interaction={visiblePendingInteraction as PendingInteraction}
            submitting={interactionSubmitting}
            onSubmit={handleVisibleInteractionSubmit}
            isInterruptSettling={isInterruptSettling}
            onStop={wrappedStopGeneration}
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
            displayedQueue={displayedQueue}
            removeQueueItem={removeQueueItem}
            retryQueueItem={retryQueueItem}
            canRetryQueued={canRetryQueued}
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
            isAgentRuntimeDeleted={isAgentRuntimeDeleted}
            isTerminated={isTerminated}
            lifecycleState={lifecycleState}
            isStreaming={isStreaming}
            isSubmitted={isSubmitted}
            hasPendingInteraction={hasPendingInteraction}
            permissionMode={permissionMode}
            permissionModes={session.engine_capabilities?.permission_modes ?? []}
            showPermissionMode={hasPermissionModes}
            canChangePermissionMode={canChangePermissionMode}
            modeSwitching={modeSwitching}
            cyclePermissionMode={cyclePermissionMode}
            selectPermissionMode={selectPermissionMode}
            isInterruptSettling={isInterruptSettling}
            wrappedStopGeneration={wrappedStopGeneration}
          />
        )}
      </ResizablePanel>

      <ResizableHandle withHandle />

      <SessionRightPanel
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
        selectedDiffFile={selectedDiffFile}
        setSelectedDiffFile={setSelectedDiffFile}
      />
    </ResizablePanelGroup>
    </SubagentNavigationProvider>
  );
}
