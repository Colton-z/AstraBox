import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { DefaultChatTransport, UIMessageStreamError } from 'ai';
import { useChat } from '@ai-sdk/react';
import { toast } from 'sonner';
import type { UIMessage as SDKUIMessage } from 'ai';
import type {
  ContentBlock,
  InteractionResponse,
  MessageRecord,
  OutboxItem,
  PendingInteraction,
  SessionRecord,
  ToolPermissionInteractionResponse,
} from '../../types';
import { answerPendingInteraction, appendTurnInput } from '../../api';
import type { ActiveTurnOverlay } from '../../api';
import {
  attachPendingInteractionToExistingTool,
  convertRecordToSDKMessage,
} from './useInitialMessages';
import type { FetchAuthoritativeHistoryOptions } from './useFirstPageMessages';
import {
  getMessagePendingInteraction,
  getPendingInteractionPart,
} from '../pendingToolMatch';
import {
  shouldAdoptAuthoritativeMessagesForLocalPendingInteraction,
  withoutAssistantStreamSlots,
} from '../messageSettlement';
import { prepareInitialMessages } from '../prepareInitialMessages';
import { didPrependDurableRecords, prependAuthoritativeMessages } from '../historyWindow';
import { turnInputContent, type TurnInputImage } from '../composerAttachments';
import {
  buildSessionStreamUrl,
  suppressNotReceivedUserMessages,
  isTransientStreamError,
  isStalePendingInteractionError,
  createClientMessageId,
  type ResumeCursorChunk,
  type TurnAcceptedChunk,
  type RetryIntent,
} from '../utils/chatHelpers';
import { getInteractionPermissionMode } from '../../utils/format';
import {
  getDurableUserTurnIdForClientMessage,
  markOutboxItemFailed,
  reconcileNativeInputOutbox,
  reconcileOutboxWithSession,
  retainQueuedOutbox,
} from '../outboxAuthority';
import { isUserInterruptedTurnFailure } from '../turnFailureIntent';
import {
  projectNativeInputs,
  retireAdoptedNativeInputs,
  type NativeInputProjectionBoundary,
} from '../nativeInputProjection';
import {
  bootstrapStreamCursor,
  decideAutoResume,
  initialTurnStreamState,
  reduceTurnStream,
  shouldOpenSessionSubscription,
  type TurnStreamEvent,
  type TurnStreamState,
} from '../turnStream';
import { withStreamLiveness } from '../streamLiveness';

/**
 * How long a lost session stream is retried in silence before the page says
 * so. Reconnects that succeed inside this window are noise the reader need
 * not see; past it, "Processing" with nothing arriving is a lie by omission,
 * and the reader is told the backend is unreachable while retries continue.
 * The clock starts at the first failure and is cleared by the first frame
 * that lands, so a flapping link does not restart it.
 */
const TRANSIENT_STREAM_NOTICE_DELAY_MS = 10_000;

// ── Types ──────────────────────────────────────────────────────

export interface UseSessionChatProps {
  sessionId: string;
  session: SessionRecord;
  durableRecords: MessageRecord[];
  initialMessages: SDKUIMessage[];
  overlay: ActiveTurnOverlay | null;
  sessionFrameSeq: number | null;
  needsResume: boolean;
  lifecycleState: string;
  liveSubscriptionEnabled: boolean;
  pendingInteraction: PendingInteraction | null;
  permissionModeRef: React.MutableRefObject<string | null>;
  refresh: (options?: { force?: boolean }) => Promise<any>;
  refreshAuthoritativeHistory: (options?: FetchAuthoritativeHistoryOptions) => Promise<{
    ownerSessionId: string;
    durableRecords: MessageRecord[];
    overlay: ActiveTurnOverlay | null;
    sessionFrameSeq: number | null;
    pendingInteraction: PendingInteraction | null;
  }>;
  observePendingInteraction: (interaction: PendingInteraction) => void;
  clearPendingInteraction: (interactionId: string) => boolean;
  interrupt: () => Promise<void>;
  recover: () => Promise<void>;
}

export interface UseSessionChatReturn {
  messages: SDKUIMessage[];
  historyFirstItemIndex: number;
  childRunRevision: number;
  status: string;
  chatError: Error | null;
  clearError: () => void;
  setMessages: (messages: SDKUIMessage[] | ((messages: SDKUIMessage[]) => SDKUIMessage[])) => void;
  outbox: OutboxItem[];
  setOutbox: React.Dispatch<React.SetStateAction<OutboxItem[]>>;
  retryIntent: RetryIntent;
  isSubmitted: boolean;
  isStreaming: boolean;
  isInterruptSettling: boolean;
  autoResumeBudgetExhausted: boolean;
  transientBannerVisible: boolean;
  showRetryBanner: boolean;
  showRetryButton: boolean;
  interactionSubmitting: boolean;
  pendingClientMessageRef: React.MutableRefObject<{
    id: string; text: string; turnId: string | null; accepted: boolean; startedAt: number;
  } | null>;
  sendClientMessageNow: (clientMessageId: string, text: string) => void;
  handleInteractionSubmit: (response: InteractionResponse) => Promise<void>;
  handleStopGeneration: () => Promise<void>;
  handleRehydrate: () => Promise<void>;
  handleRetry: () => void;
  handleRecover: () => void;
}

type AuthoritativeHistoryData = {
  ownerSessionId: string;
  durableRecords: MessageRecord[];
  overlay: ActiveTurnOverlay | null;
  sessionFrameSeq: number | null;
  pendingInteraction: PendingInteraction | null;
};

type PendingEngineInput = {
  clientMessageId: string;
  inputId: string;
  content: string;
};

type EngineInputConsumedChunk = {
  type: 'data-input-consumed';
  data?: {
    inputId?: unknown;
    responseMessageId?: unknown;
    clientMessageId?: unknown;
    content?: unknown;
    contentBlocks?: unknown;
  };
};

// ── Helpers ────────────────────────────────────────────────────

function consumedContentBlocks(value: unknown): ContentBlock[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value) || value.length === 0) {
    throw new Error('engine input boundary content blocks are malformed');
  }
  for (const block of value) {
    if (!block || typeof block !== 'object') {
      throw new Error('engine input boundary content block is malformed');
    }
    const candidate = block as Record<string, unknown>;
    if (candidate.type === 'text' && typeof candidate.text === 'string') continue;
    const source = candidate.source;
    if (
      candidate.type === 'image'
      && source
      && typeof source === 'object'
      && (source as Record<string, unknown>).type === 'base64'
      && typeof (source as Record<string, unknown>).media_type === 'string'
      && typeof (source as Record<string, unknown>).data === 'string'
    ) continue;
    throw new Error('engine input boundary content block is malformed');
  }
  return value as ContentBlock[];
}

function rehydrateDeliveryFailure(
  outbox: OutboxItem[],
  failure: SessionRecord['delivery_failure'],
): OutboxItem[] {
  if (!failure) return outbox;
  if (outbox.some((item) => item.client_message_id === failure.client_message_id)) return outbox;
  return [{
    client_message_id: failure.client_message_id,
    text: failure.text,
    status: 'failed' as const,
    failure_reason: failure.summary,
  }, ...outbox];
}

function rehydratePendingEngineInputs(session: SessionRecord): OutboxItem[] {
  return (session.pending_inputs ?? []).map((input) => ({
    client_message_id: input.client_message_id || input.input_id,
    text: input.content,
    status: 'accepted' as const,
    command_id: input.command_id,
    input_id: input.input_id,
  }));
}

function pendingEngineInputs(session: SessionRecord): PendingEngineInput[] {
  return (session.pending_inputs ?? []).map((input) => ({
    clientMessageId: input.client_message_id || input.input_id,
    inputId: input.input_id,
    content: input.content,
  }));
}

function normalizeAuthoritativeHistory(
  pageData: AuthoritativeHistoryData,
  sessionId: string,
) {
  return {
    ownerSessionId: String(pageData.ownerSessionId ?? '').trim() || sessionId,
    records: Array.isArray(pageData.durableRecords) ? pageData.durableRecords : [],
    overlay: pageData.overlay ?? null,
    sessionFrameSeq: pageData.sessionFrameSeq,
    pendingInteraction: pageData.pendingInteraction ?? null,
  };
}

const UNCONFIRMED_SEND_RECONCILE_TIMEOUT_MS = 2_000;

class SessionStoreReloadBoundary extends Error {
  constructor() {
    super('SessionStore history reload reached its durable resume cursor');
    this.name = 'SessionStoreReloadBoundary';
  }
}

function withTimeout<T>(promise: Promise<T>, timeoutMs: number, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      reject(new Error(`${label} timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    promise.then(
      (value) => {
        window.clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        window.clearTimeout(timer);
        reject(error);
      },
    );
  });
}

// ── Hook ───────────────────────────────────────────────────────

export function useSessionChat(props: UseSessionChatProps): UseSessionChatReturn {
  const {
    sessionId, session, durableRecords, initialMessages, overlay, sessionFrameSeq, needsResume,
    lifecycleState, liveSubscriptionEnabled, pendingInteraction, permissionModeRef,
    refresh, refreshAuthoritativeHistory, interrupt, recover,
    observePendingInteraction, clearPendingInteraction,
  } = props;

  const { t } = useTranslation();
  const activeTurnId = String(session.current_turn_id ?? '').trim() || null;
  const activeTurnIdRef = useRef<string | null>(activeTurnId);
  activeTurnIdRef.current = activeTurnId;
  const [localTurnId, setLocalTurnId] = useState<string | null>(activeTurnId);
  const [childRunRevision, setChildRunRevision] = useState(0);
  const latestAcceptedTurnIdRef = useRef<string | null>(null);
  const messagesRef = useRef<SDKUIMessage[]>(initialMessages);
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
  const sessionSnapshotRef = useRef(session);
  // Everything about "is this turn still streaming, and is a stream owed?"
  // lives in one place (see session/turnStream.ts). Mutated in render the way
  // activeTurnIdRef above already is; the reducer is pure, so the only side
  // effect is the assignment.
  const turnStreamRef = useRef<TurnStreamState>(
    initialTurnStreamState(
      activeTurnId,
      bootstrapStreamCursor(activeTurnId, overlay?.resume_cursor ?? null, sessionFrameSeq),
    ),
  );
  const dispatchTurnStream = useCallback((event: TurnStreamEvent) => {
    turnStreamRef.current = reduceTurnStream(turnStreamRef.current, event);
  }, []);
  const pendingClientMessageRef = useRef<{
    id: string; text: string; turnId: string | null; accepted: boolean; startedAt: number;
  } | null>(null);
  const markAcceptedRef = useRef<(cid: string, tid?: string | null) => void>(() => {});

  useEffect(() => { sessionSnapshotRef.current = session; }, [session]);

  // Transient stream errors remain retryable until the retry budget expires.
  const errorStartTimeRef = useRef<number | null>(null);
  const silentRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [transientBannerVisible, setTransientBannerVisible] = useState(false);
  const isInTransientRecoveryRef = useRef(false);
  const resetTransientState = useCallback(() => {
    errorStartTimeRef.current = null;
    isInTransientRecoveryRef.current = false;
    setTransientBannerVisible(false);
    if (silentRetryTimerRef.current !== null) {
      clearTimeout(silentRetryTimerRef.current);
      silentRetryTimerRef.current = null;
    }
  }, []);
  useEffect(() => () => {
    if (silentRetryTimerRef.current !== null) clearTimeout(silentRetryTimerRef.current);
  }, []);

  // Outbox
  const [outbox, setOutbox] = useState<OutboxItem[]>(() =>
    rehydrateDeliveryFailure(
      rehydratePendingEngineInputs(session),
      session.delivery_failure ?? null,
    ),
  );
  const pendingInputsRef = useRef(new Map(
    pendingEngineInputs(session).map((input) => [input.clientMessageId, input]),
  ));
  const consumedInputIdsRef = useRef(new Set<string>());
  const [nativeInputProjectionState, setNativeInputProjectionState] = useState<{
    ownerSessionId: string;
    boundaries: NativeInputProjectionBoundary[];
  }>({ ownerSessionId: sessionId, boundaries: [] });
  const [residentMessages, setResidentMessages] = useState<{
    ownerSessionId: string;
    entries: { message: SDKUIMessage; afterId: string | null }[];
  }>({ ownerSessionId: sessionId, entries: [] });
  const [interactionSubmitting, setInteractionSubmitting] = useState(false);
  //: This hook's own delivery-in-flight state. Session activity is independent
  //: of whether one terminal-bounded output response is currently open.
  const [deliveryInFlight, setDeliveryInFlight] = useState(false);
  const ensureSessionStreamRef = useRef<() => void>(() => {});
  const subscriptionTaskRef = useRef<Promise<void> | null>(null);
  const subscriptionNeedsHistoryRef = useRef(false);
  const subscriptionFinishRef = useRef<'clean' | 'disconnected' | 'aborted' | 'error' | null>(null);
  const clearAssistantStreamSlotRef = useRef<() => void>(() => {});
  const subscriptionMountedRef = useRef(true);
  const liveSubscriptionEnabledRef = useRef(liveSubscriptionEnabled);
  liveSubscriptionEnabledRef.current = liveSubscriptionEnabled;
  const sessionStoreRebuildRef = useRef<(resumeSequence: number) => Promise<void>>(
    async () => { throw new Error('SessionStore rebuild handler is not ready'); },
  );
  const authoritativeRehydrateRef = useRef<(receivedCursor?: TurnStreamState['cursor']) => Promise<void>>(
    async () => { throw new Error('Authoritative stream rehydrate handler is not ready'); },
  );
  const sessionStoreReloadRef = useRef<{
    key: string;
    promise: Promise<{ ok: true } | { ok: false; error: unknown }>;
    closeAfterCursor: boolean;
  } | null>(null);
  const materializePendingSendFailureRef = useRef<(id: string, reason: string) => void>(() => {});
  const adoptAuthoritativeMessagesRef = useRef<(
    messages: SDKUIMessage[],
    completedPlatformTurnId?: string | null,
  ) => void>(() => {});
  const [isInterruptSettling, setIsInterruptSettling] = useState(false);
  const interruptedTurnIdRef = useRef<string | null>(null);
  const interruptRequestInFlightRef = useRef(false);

  // Auto-resume budget
  const [autoResumeBudgetExhausted, setAutoResumeBudgetExhausted] = useState(false);
  if (turnStreamRef.current.serverTurnId !== activeTurnId) {
    // A phase carries the turn it describes, so one belonging to the turn that
    // just ended is ignored from here without anyone deciding to clear it.
    turnStreamRef.current = reduceTurnStream(turnStreamRef.current, {
      type: 'server-turn-observed', turnId: activeTurnId,
    });
  }

  useEffect(() => {
    if (!activeTurnId) {
      return;
    }
    setLocalTurnId((prev) => (prev === activeTurnId ? prev : activeTurnId));
  }, [activeTurnId]);

  // ── Transport ────────────────────────────────────────────────
  // One receive subscription, separate from the input route. Each HTTP
  // response carries one FIFO reply, and reconnect uses the session-wide
  // durable frame cursor.
  const transport = useMemo(
    () => new DefaultChatTransport({
      api: buildSessionStreamUrl(sessionId),
      credentials: 'include',
      prepareReconnectToStreamRequest: () => ({
        api: buildSessionStreamUrl(sessionId, turnStreamRef.current.cursor.frameSeq),
        credentials: 'include' as RequestCredentials,
      }),
      // Response headers are where a reconnect proves it worked. Reporting it
      // here rather than on the first frame matters because a resumed stream
      // can stay silent for as long as the engine is thinking, and a
      // disconnect in that silence belongs to the new connection.
      fetch: async (input, init) => {
        const response = await globalThis.fetch(input, init);
        if (
          response.ok
          && String(init?.method ?? 'GET').toUpperCase() === 'GET'
        ) {
          dispatchTurnStream({ type: 'stream-established' });
          // A socket that dies under the browser stays pending forever;
          // ending it on silence is what lets the disconnect path resume.
          return withStreamLiveness(response);
        }
        return response;
      },
    }),
    [dispatchTurnStream, sessionId],
  );

  // ── useChat ──────────────────────────────────────────────────
  const {
    messages, resumeStream, setMessages,
    status, stop: stopStream, error: chatError, clearError,
    addToolApprovalResponse, addToolOutput,
  } = useChat({
    id: sessionId,
    transport,
    messages: initialMessages,
    resume: false,
    experimental_throttle: 50,

    // Transport finished: refresh session truth, but do not settle transcript here.
    onFinish: ({ message, isAbort, isDisconnect, isError }) => {
      // Unmount stops the SDK stream, which still calls onFinish. Its old
      // refresh closure must not cancel the next route's detail request.
      if (!subscriptionMountedRef.current) return;
      clearAssistantStreamSlotRef.current();
      const rebuildingSessionStore = sessionStoreReloadRef.current !== null;
      const cleanFinish = !rebuildingSessionStore && !isAbort && !isDisconnect && !isError;
      const finishTurnId = String(
        turnStreamRef.current.cursor.turnId
        ?? activeTurnIdRef.current
        ?? localTurnId
        ?? latestAcceptedTurnIdRef.current
        ?? pendingClientMessageRef.current?.turnId
        ?? '',
      ).trim();
      const finishClientMessageId = String(pendingClientMessageRef.current?.id ?? '').trim();
      const finishedTurnId = finishTurnId
        || String(activeTurnIdRef.current ?? '').trim()
        || String(latestAcceptedTurnIdRef.current ?? '').trim()
        || String(pendingClientMessageRef.current?.turnId ?? '').trim()
        || null;
      let finishOutcome: 'clean' | 'disconnected' | 'aborted' | 'error' = 'error';
      if (cleanFinish) finishOutcome = 'clean';
      else if (rebuildingSessionStore || isDisconnect) finishOutcome = 'disconnected';
      else if (isAbort) finishOutcome = 'aborted';
      subscriptionFinishRef.current = finishOutcome;
      dispatchTurnStream({
        type: 'stream-finished',
        outcome: finishOutcome,
        turnId: finishedTurnId,
      });
      if (cleanFinish) resetTransientState();
      if ((message.metadata as { response_boundary?: boolean } | undefined)?.response_boundary) {
        return;
      }
      void (async () => {
        let detail = sessionSnapshotRef.current;
        try {
          const result = await refresh({ force: true });
          detail = result?.detail ?? detail;
        } catch (err) {
          console.error('[useSessionChat] onFinish refresh failed:', err);
        }
        // The finish frame reaches the client a beat before the terminal
        // snapshot CAS frees the turn slot server-side, so a single read here
        // can race it and freeze this page — and the sidebar its signature
        // effect signals — on PROCESSING until the slow idle poll. Re-read on
        // a short backoff until the session has left the mid-turn states;
        // when the first read already settled this costs nothing.
        const MID_TURN_STATES = new Set(['PROCESSING', 'STREAMING', 'INTERRUPTING', 'BUSY', 'SENDING']);
        for (const delayMs of [150, 350, 700, 1400]) {
          if (!MID_TURN_STATES.has(String(detail?.state ?? '').toUpperCase())) break;
          await new Promise((resolve) => setTimeout(resolve, delayMs));
          if (!subscriptionMountedRef.current) return;
          try {
            const again = await refresh({ force: true });
            detail = again?.detail ?? detail;
          } catch {
            // Transient read failure — the next backoff tick retries.
          }
        }
        if (!cleanFinish) {
          return;
        }
        const nextActiveTurnId = String(detail?.current_turn_id ?? '').trim();
        if (nextActiveTurnId && nextActiveTurnId !== finishTurnId) {
          return;
        }
        const liveActiveTurnId = String(activeTurnIdRef.current ?? '').trim();
        if (liveActiveTurnId && liveActiveTurnId !== finishTurnId) {
          return;
        }
        if (statusRef.current === 'submitted' || statusRef.current === 'streaming') {
          return;
        }
        const pending = pendingClientMessageRef.current;
        if (pending) {
          const pendingTurnId = String(pending.turnId ?? '').trim();
          const sameFinishedPending = Boolean(
            finishClientMessageId
            && pending.id === finishClientMessageId
            && (!pendingTurnId || pendingTurnId === finishTurnId),
          );
          const sameFinishedTurn = Boolean(finishTurnId && pendingTurnId === finishTurnId);
          if (!pending.accepted || (!sameFinishedPending && !sameFinishedTurn)) {
            return;
          }
        }
        if (nextActiveTurnId) {
          return;
        }
        dispatchTurnStream({ type: 'turn-settled' });
        const completedTurnId = String(detail?.last_turn_id ?? latestAcceptedTurnIdRef.current ?? '').trim();
        if (pending?.accepted && (!pending.turnId || pending.turnId === completedTurnId)) {
          // Only the send's own bookkeeping settles here. The queue row is not
          // swept by a turn ending: it leaves when the transcript carries the
          // message (see the subtraction below) and at no other moment, so a
          // message the engine has not taken cannot fall off both surfaces.
          pendingClientMessageRef.current = null;
        }
      })();
    },

    // onData: accept + interaction + resume cursor + authoritative store rebuild
    onData: (part) => {
      if (isInTransientRecoveryRef.current) resetTransientState();
      const reload = part as {
        type?: string;
        data?: { resumeSequence?: unknown };
      };
      if (reload.type === 'data-session-store-reload') {
        const resumeSequence = reload.data?.resumeSequence;
        if (!Number.isInteger(resumeSequence) || Number(resumeSequence) < 0) {
          throw new Error(`malformed SessionStore reload cursor: ${String(resumeSequence)}`);
        }
        const normalizedSequence = Number(resumeSequence);
        const reloadTurnId = String(
          activeTurnIdRef.current
          ?? localTurnId
          ?? latestAcceptedTurnIdRef.current
          ?? '',
        ).trim() || null;
        const key = `${sessionId}:${reloadTurnId ?? ''}:${normalizedSequence}`;
        if (sessionStoreReloadRef.current?.key === key) return;
        // resumeSequence is the runner journal's retained boundary. The page's
        // cursor is a different coordinate (durable platform frame_seq), so it
        // advances only on the data-resume-cursor that follows this frame.
        dispatchTurnStream({
          type: 'session-store-rebuild-required',
          turnId: reloadTurnId,
          resumeSequence: normalizedSequence,
        });
        const promise = sessionStoreRebuildRef.current(normalizedSequence).then(
          () => ({ ok: true as const }),
          (error: unknown) => ({ ok: false as const, error }),
        );
        sessionStoreReloadRef.current = { key, promise, closeAfterCursor: true };
        return;
      }
      const type = String((part as { type?: unknown }).type ?? '');
      if (type === 'data-session-changed') {
        const changedSessionId = (part as { data?: { sessionId?: unknown } }).data?.sessionId;
        if (changedSessionId !== sessionId) {
          throw new Error('malformed session invalidation');
        }
        void refresh({ force: true }).catch((err) => {
          console.error('[useSessionChat] session invalidation refresh failed:', err);
        });
        return;
      }
      if (type === 'data-session-message') {
        const record = (part as { data?: MessageRecord }).data;
        if (
          !record || record.session_id !== sessionId || record.turn_id !== ''
          || record.role !== 'assistant' || !record.message_id?.trim()
          || typeof record.content !== 'string' || !record.content.trim()
        ) throw new Error('malformed resident session message');
        const message = convertRecordToSDKMessage(record);
        const visible = withoutAssistantStreamSlots(messagesRef.current, sessionId);
        const afterId = visible.at(-1)?.id ?? null;
        setResidentMessages((current) => {
          const entries = current.ownerSessionId === sessionId ? current.entries : [];
          if (entries.some((entry) => entry.message.id === message.id)) return current;
          return { ownerSessionId: sessionId, entries: [...entries, { message, afterId }] };
        });
        return;
      }
      if (type === 'data-child-runs-changed') {
        const frameSeq = (part as { data?: { frameSeq?: unknown } }).data?.frameSeq;
        if (!Number.isInteger(frameSeq) || Number(frameSeq) < 0) {
          throw new Error(`malformed child-run invalidation cursor: ${String(frameSeq)}`);
        }
        setChildRunRevision((revision) => Math.max(revision, Number(frameSeq) + 1));
        return;
      }
      if (type === 'data-input-consumed') {
        const consumed = part as EngineInputConsumedChunk;
        const inputId = String(consumed.data?.inputId ?? '').trim();
        const responseMessageId = String(
          consumed.data?.responseMessageId ?? '',
        ).trim();
        const boundaryClientMessageId = String(
          consumed.data?.clientMessageId ?? '',
        ).trim();
        const content = consumed.data?.content;
        if (!inputId || !responseMessageId || typeof content !== 'string') {
          throw new Error('engine input boundary is malformed');
        }
        const contentBlocks = consumedContentBlocks(consumed.data?.contentBlocks);
        const pending = Array.from(pendingInputsRef.current.values()).find(
          (candidate) => (
            candidate.inputId === inputId
            || candidate.clientMessageId === inputId
            || candidate.clientMessageId === boundaryClientMessageId
          ),
        );
        const clientMessageId = boundaryClientMessageId || pending?.clientMessageId || inputId;
        pendingInputsRef.current.delete(clientMessageId);
        consumedInputIdsRef.current.add(clientMessageId);
        consumedInputIdsRef.current.add(inputId);
        const platformTurnId = String(
          latestAcceptedTurnIdRef.current
          ?? activeTurnIdRef.current
          ?? localTurnId
          ?? '',
        ).trim();
        // A backend-driven handoff consumes a queued input without this client
        // ever accepting a turn for it, so no ref holds a turn id when the
        // boundary arrives. The message still moves from the queue to the
        // transcript here — consumption is the handover, and clearing the
        // queue row without projecting the message leaves it on neither
        // surface until the turn's authoritative history lands, which reads
        // as the message vanishing and returning with the answer. An empty
        // turn id costs only the metadata: the row appends at the end of the
        // transcript instead of anchoring ahead of its response, and the
        // authoritative history places it exactly when it arrives.
        const consumedBoundary: NativeInputProjectionBoundary = {
          clientMessageId,
          inputId,
          platformTurnId,
          responseMessageId,
          content,
          contentBlocks,
        };
        setNativeInputProjectionState((current) => {
          const boundaries = current.ownerSessionId === sessionId
            ? current.boundaries
            : [];
          const index = boundaries.findIndex((candidate) => (
            candidate.inputId === inputId
            || candidate.clientMessageId === clientMessageId
          ));
          return {
            ownerSessionId: sessionId,
            boundaries: index < 0
              ? [...boundaries, consumedBoundary]
              : boundaries.map((candidate, candidateIndex) => (
                  candidateIndex === index ? consumedBoundary : candidate
                )),
          };
        });
        // A new engine input can only be consumed after the interrupted foreground
        // turn has reached its terminal boundary. Do not carry that turn's local
        // stopping state into the newly active queued turn.
        setIsInterruptSettling(false);
        setOutbox((current) => current.filter((item) => (
          item.client_message_id !== clientMessageId
          && item.client_message_id !== inputId
          && item.input_id !== inputId
        )));
        // The SDK exclusively owns the live assistant/tool tree. This boundary
        // projects only the consumed user row until an authoritative message
        // array adopts it, so approval frames always extend the same tool tree.
        const foregroundPending = pendingClientMessageRef.current;
        if (
          foregroundPending
          && (foregroundPending.id === clientMessageId || foregroundPending.id === inputId)
        ) {
          pendingClientMessageRef.current = null;
        }
        return;
      }
      if (type === 'data-result') {
        // Confirm durable history after the response ends, before reopening.
        // The stream, not a newer history window, owns the reader's live tree.
        subscriptionNeedsHistoryRef.current = true;
        return;
      }
      const acc = part as TurnAcceptedChunk;
      if (acc.type === 'data-turn-accepted') {
        clearAssistantStreamSlotRef.current();
        const cid = String(acc.data?.clientMessageId ?? '').trim();
        const tid = String(acc.data?.turnId ?? '').trim();
        if (cid && tid) markAcceptedRef.current(cid, tid);
        // Acceptance means the backend dispatched this turn to a live sandbox:
        // for a conversation whose sandbox was reclaimed, the per-session
        // sandbox has just been re-borrowed. Refreshing the lifecycle session
        // here rather than at onFinish flips the runtime-ready gating (files /
        // terminal panels) to available the moment the sandbox is back,
        // instead of holding "files temporarily unavailable" until the turn
        // ends.
        void refresh({ force: true });
        return;
      }
      if ((part as { type?: unknown }).type === 'data-interaction') {
        const nextPendingInteraction = getPendingInteractionPart(part);
        if (!nextPendingInteraction) {
          throw new Error('SDK data-interaction is malformed');
        }
        observePendingInteraction(nextPendingInteraction);
        return;
      }
      const cur = part as ResumeCursorChunk;
      if (cur.type !== 'data-resume-cursor') return;
      const nextSeq = Number(cur.data?.frameSeq);
      const tid = String(cur.data?.turnId ?? '').trim() || null;
      if (tid) clearAssistantStreamSlotRef.current();
      const changedTurn = Boolean(tid && turnStreamRef.current.cursor.turnId !== tid);
      dispatchTurnStream({ type: 'cursor-advanced', turnId: tid, frameSeq: nextSeq });
      if (changedTurn && tid) setLocalTurnId((current) => (current === tid ? current : tid));
      const reloadBoundary = sessionStoreReloadRef.current;
      if (reloadBoundary?.closeAfterCursor) {
        if (!Number.isInteger(nextSeq) || nextSeq < 0) {
          throw new Error(`malformed durable resume cursor after SessionStore reload: ${String(nextSeq)}`);
        }
        reloadBoundary.closeAfterCursor = false;
        throw new SessionStoreReloadBoundary();
      }
    },

    // Record transient failures without settling the turn prematurely.
    onError: (err) => {
      if (err instanceof SessionStoreReloadBoundary) return;
      const pending = pendingClientMessageRef.current;
      dispatchTurnStream({ type: 'stream-error', hasActiveTurn: Boolean(activeTurnIdRef.current) });
      console.error('[useSessionChat] stream error:', err);
      if (UIMessageStreamError.isInstance(err)) {
        // AI SDK parsers intentionally keep request-local state. If a stream
        // reconnect ever starts after a structural dependency (for example a
        // tool-input-start), continuing with another fresh parser only repeats
        // the same error. Rebuild the complete assistant/tool prefix and its
        // cursor from one authoritative history response, then let the standing
        // next subscription request from that boundary.
        const cursor = turnStreamRef.current.cursor;
        const key = [
          'ui-message-stream',
          sessionId,
          cursor.turnId ?? '',
          cursor.frameSeq,
          err.chunkType,
          err.chunkId,
        ].join(':');
        if (sessionStoreReloadRef.current === null) {
          const promise = authoritativeRehydrateRef.current().then(
            () => ({ ok: true as const }),
            (error: unknown) => ({ ok: false as const, error }),
          );
          sessionStoreReloadRef.current = { key, promise, closeAfterCursor: false };
        }
        return;
      }
      if (isTransientStreamError(err)) {
        if ((activeTurnIdRef.current || pending) && errorStartTimeRef.current === null) {
          errorStartTimeRef.current = Date.now();
          if (silentRetryTimerRef.current !== null) clearTimeout(silentRetryTimerRef.current);
          silentRetryTimerRef.current = setTimeout(
            () => setTransientBannerVisible(true),
            TRANSIENT_STREAM_NOTICE_DELAY_MS,
          );
        }
        isInTransientRecoveryRef.current = true;
      } else {
        isInTransientRecoveryRef.current = false;
      }
      if (pending && !pending.accepted && !activeTurnIdRef.current) {
        return;
      }
      void refresh();
    },
  });

  clearAssistantStreamSlotRef.current = () => {
    setMessages((current) => withoutAssistantStreamSlots(current, sessionId));
  };

  // Post-useChat wiring
  markAcceptedRef.current = (cid: string, tid?: string | null) => {
    const normTid = String(tid ?? '').trim() || null;
    const pending = pendingClientMessageRef.current;
    if (pending && pending.id === cid) {
      pendingClientMessageRef.current = { ...pending, accepted: true, turnId: normTid ?? pending.turnId };
    }
    if (normTid) {
      dispatchTurnStream({ type: 'turn-accepted', turnId: normTid });
      latestAcceptedTurnIdRef.current = normTid;
      setLocalTurnId(normTid);
      setNativeInputProjectionState((current) => ({
        ...current,
        boundaries: current.ownerSessionId !== sessionId
          ? current.boundaries
          : current.boundaries.map((boundary) => (
              boundary.clientMessageId === cid || boundary.inputId === cid
                ? { ...boundary, platformTurnId: normTid }
                : boundary
            )),
      }));
    }
    setOutbox((prev) => prev.map((item) =>
      item.client_message_id === cid
        ? { ...item, status: 'accepted' as const, accepted_turn_id: normTid ?? undefined }
        : item,
    ));
  };

  const adoptAuthoritativeMessages = useCallback((
    nextMessages: SDKUIMessage[],
    completedPlatformTurnId: string | null = null,
  ) => {
    setMessages(nextMessages);
    setNativeInputProjectionState((current) => {
      if (current.ownerSessionId !== sessionId || current.boundaries.length === 0) {
        return current;
      }
      const boundaries = retireAdoptedNativeInputs(
        current.boundaries,
        nextMessages,
        completedPlatformTurnId,
      );
      return boundaries.length === current.boundaries.length
        ? current
        : { ...current, boundaries };
    });
  }, [sessionId, setMessages]);
  adoptAuthoritativeMessagesRef.current = adoptAuthoritativeMessages;

  const [historyFirstItemIndex, setHistoryFirstItemIndex] = useState(100_000);
  const previousDurableRecordsRef = useRef(durableRecords);
  useLayoutEffect(() => {
    const previous = previousDurableRecordsRef.current;
    previousDurableRecordsRef.current = durableRecords;
    if (!didPrependDurableRecords(previous, durableRecords)) return;
    const detail = sessionSnapshotRef.current;
    const previousMessages = prepareInitialMessages(previous, null, detail);
    const authoritative = prepareInitialMessages(durableRecords, null, detail);
    const prependedCount = authoritative.length - previousMessages.length;
    if (prependedCount < 0) throw new Error('SESSION_HISTORY_PREPEND_PROJECTION_REGRESSED');
    if (prependedCount > 0) setHistoryFirstItemIndex((current) => current - prependedCount);
    setMessages((current) => prependAuthoritativeMessages(current, authoritative));
  }, [durableRecords, setMessages]);

  const statusRef = useRef(status);
  statusRef.current = status;
  const nativeInputProjectionBoundaries = nativeInputProjectionState.ownerSessionId === sessionId
    ? nativeInputProjectionState.boundaries
    : [];
  const projectedMessages = useMemo(() => {
    const projected = [...projectNativeInputs(
      withoutAssistantStreamSlots(messages, sessionId),
      nativeInputProjectionBoundaries,
    )];
    if (residentMessages.ownerSessionId !== sessionId) return projected;
    const residentIds = new Set(residentMessages.entries.map((entry) => entry.message.id));
    for (const { message, afterId } of residentMessages.entries) {
      if (projected.some((item) => item.id === message.id)) continue;
      const anchor = afterId === null ? -1 : projected.findIndex((item) => item.id === afterId);
      if (afterId !== null && anchor < 0) continue;
      let index = anchor + 1;
      while (index < projected.length && residentIds.has(projected[index].id)) index += 1;
      projected.splice(index, 0, message);
    }
    return projected;
  }, [messages, nativeInputProjectionBoundaries, residentMessages, sessionId]);
  useEffect(() => { messagesRef.current = projectedMessages; }, [projectedMessages]);

  /**
   * The queue holds what the transcript does not.
   *
   * Every other rule for emptying it — a turn ending, a receipt arriving, a
   * boundary frame — is a guess about when the message became visible
   * elsewhere, and a wrong guess drops it off both surfaces. This states the
   * invariant instead: a queued row leaves exactly when a user message
   * carrying its identity is on screen. It is also the repair for a row whose
   * clearing signal never arrives.
   */
  const outboxRef = useRef(outbox);
  outboxRef.current = outbox;
  useEffect(() => {
    // This runs on every streamed change to the transcript — a few times a
    // second for the length of a turn — so it decides before it writes. A
    // setter call that lands on an identical value still schedules a render
    // for React to unwind, and while a stream keeps the root busy those never
    // get the quiet moment that resets the nested-update count; fifty in a row
    // is reported as an exceeded update depth and takes the turn's stream down
    // with it. Reading the committed outbox through a ref is what makes the
    // decision available here at all.
    const current = outboxRef.current;
    const remaining = retainQueuedOutbox(current, projectedMessages);
    if (remaining === current) return;
    setOutbox(remaining);
  }, [projectedMessages, setOutbox]);

  const clearPendingClientMessage = useCallback((clientMessageId: string) => {
    const normalizedClientMessageId = String(clientMessageId ?? '').trim();
    if (!normalizedClientMessageId) {
      return;
    }
    const pending = pendingClientMessageRef.current;
    if (pending && pending.id === normalizedClientMessageId) {
      pendingClientMessageRef.current = null;
    }
    dispatchTurnStream({ type: 'went-idle' });
  }, [dispatchTurnStream]);

  const materializePendingSendFailure = useCallback((
    clientMessageId: string,
    failureReason: string,
  ) => {
    const normalizedClientMessageId = String(clientMessageId ?? '').trim();
    if (!normalizedClientMessageId) {
      return;
    }
    clearPendingClientMessage(normalizedClientMessageId);
    resetTransientState();
    clearError();
    setOutbox((prev) => markOutboxItemFailed(prev, normalizedClientMessageId, failureReason));
    setNativeInputProjectionState((current) => {
      if (current.ownerSessionId !== sessionId) return current;
      const boundaries = current.boundaries.filter((boundary) => (
        boundary.clientMessageId !== normalizedClientMessageId
        && boundary.inputId !== normalizedClientMessageId
      ));
      return boundaries.length === current.boundaries.length
        ? current
        : { ...current, boundaries };
    });
    setMessages((prev) => prev.filter((message) => String(message.id ?? '').trim() !== normalizedClientMessageId));
  }, [clearError, clearPendingClientMessage, resetTransientState, sessionId, setMessages]);

  materializePendingSendFailureRef.current = materializePendingSendFailure;

  useEffect(() => {
    const authoritative = pendingEngineInputs(session).filter((input) => (
      !consumedInputIdsRef.current.has(input.clientMessageId)
      && !consumedInputIdsRef.current.has(input.inputId)
    ));
    authoritative.forEach((input) => {
      pendingInputsRef.current.set(input.clientMessageId, input);
    });
    const authoritativeOutbox = rehydratePendingEngineInputs(session).filter((item) => (
      !consumedInputIdsRef.current.has(item.client_message_id)
      && !consumedInputIdsRef.current.has(String(item.input_id ?? ''))
    ));
    setOutbox((current) => reconcileNativeInputOutbox(
      current,
      authoritativeOutbox,
      messagesRef.current,
    ));
  }, [session.pending_inputs]);

  // These refs and projections are session-owned; carrying them across a route
  // change would mix one session's pending inputs into another session.
  useEffect(() => {
    pendingClientMessageRef.current = null;
    pendingInputsRef.current.clear();
    pendingEngineInputs(session).forEach((input) => {
      pendingInputsRef.current.set(input.clientMessageId, input);
    });
    consumedInputIdsRef.current.clear();
    setNativeInputProjectionState({ ownerSessionId: sessionId, boundaries: [] });
    latestAcceptedTurnIdRef.current = null;
    sessionStoreReloadRef.current = null;
    setLocalTurnId(activeTurnIdRef.current);
    messagesRef.current = initialMessages;
    setOutbox(rehydrateDeliveryFailure(
      rehydratePendingEngineInputs(session),
      session.delivery_failure ?? null,
    ));
    dispatchTurnStream({
      type: 'session-changed',
      serverTurnId: activeTurnIdRef.current,
      cursor: bootstrapStreamCursor(
        activeTurnIdRef.current,
        overlay?.resume_cursor ?? null,
        sessionFrameSeq,
      ),
    });
  }, [sessionId]);

  const pendingSendErrorKeyRef = useRef<string | null>(null);
  useEffect(() => {
    const pending = pendingClientMessageRef.current;
    if (!chatError || !pending || pending.accepted) {
      pendingSendErrorKeyRef.current = null;
      return;
    }
    if (activeTurnIdRef.current) {
      pendingSendErrorKeyRef.current = null;
      return;
    }
    const key = `${pending.id}|${String(chatError.message ?? chatError).trim()}`;
    if (pendingSendErrorKeyRef.current === key) {
      return;
    }
    pendingSendErrorKeyRef.current = key;

    let cancelled = false;
    void (async () => {
      let detail: SessionRecord = sessionSnapshotRef.current;
      let durableMessages: MessageRecord[] = [];
      const [refreshResult, authoritativeHistoryResult] = await Promise.allSettled([
        withTimeout(
          refresh({ force: true }),
          UNCONFIRMED_SEND_RECONCILE_TIMEOUT_MS,
          'pending send refresh',
        ),
        withTimeout(
          refreshAuthoritativeHistory({ mode: 'acceptance', commit: false }),
          UNCONFIRMED_SEND_RECONCILE_TIMEOUT_MS,
          'pending send acceptance check',
        ),
      ]);
      if (cancelled) {
        return;
      }
      if (refreshResult.status === 'fulfilled') {
        detail = refreshResult.value.detail ?? sessionSnapshotRef.current;
      } else {
        console.error('[useSessionChat] pending send refresh failed:', refreshResult.reason);
      }
      if (authoritativeHistoryResult.status === 'fulfilled') {
        durableMessages = normalizeAuthoritativeHistory(
          authoritativeHistoryResult.value,
          sessionId,
        ).records;
      } else {
        console.error('[useSessionChat] pending send authoritative history refresh failed:', authoritativeHistoryResult.reason);
      }

      const latestPending = pendingClientMessageRef.current;
      if (!latestPending || latestPending.id !== pending.id || latestPending.accepted) {
        return;
      }

      const authoritativeTurnId = String(detail.current_turn_id ?? '').trim();
      if (authoritativeTurnId) {
        clearError();
        markAcceptedRef.current(latestPending.id, authoritativeTurnId);
        return;
      }

      const durableTurnId = getDurableUserTurnIdForClientMessage(durableMessages, latestPending.id);
      if (durableTurnId) {
        clearError();
        markAcceptedRef.current(latestPending.id, durableTurnId);
        return;
      }

      const authoritativeFailureClientId = String(detail.delivery_failure?.client_message_id ?? '').trim();
      if (authoritativeFailureClientId === latestPending.id) {
        clearPendingClientMessage(latestPending.id);
        resetTransientState();
        clearError();
        return;
      }

      materializePendingSendFailure(latestPending.id, t('chat:send.unconfirmed_failure'));
    })();

    return () => {
      cancelled = true;
    };
  }, [
    chatError,
    clearPendingClientMessage,
    clearError,
    materializePendingSendFailure,
    refresh,
    resetTransientState,
    refreshAuthoritativeHistory,
    sessionId,
    t,
  ]);

  const messagePendingInteraction = useMemo(
    () => getMessagePendingInteraction(messages),
    [messages],
  );
  const authoritativeMessages = useMemo(
    () => prepareInitialMessages(durableRecords, overlay, session),
    [durableRecords, overlay, session],
  );
  const authoritativeMessagePendingInteraction = useMemo(
    () => getMessagePendingInteraction(authoritativeMessages),
    [authoritativeMessages],
  );
  // Session detail owns pending interaction correctness, but not message
  // chronology. Only attach its metadata after the matching tool part already
  // exists in the local message tree; the SDK stream alone owns live message
  // and tool chronology.
  useEffect(() => {
    if (!pendingInteraction) return;
    setMessages((prev) => attachPendingInteractionToExistingTool(prev, pendingInteraction));
  }, [pendingInteraction, setMessages]);

  useEffect(() => {
    if (!shouldAdoptAuthoritativeMessagesForLocalPendingInteraction(
      session,
      messagePendingInteraction,
      authoritativeMessagePendingInteraction,
      !!overlay,
    )) {
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const pageData = normalizeAuthoritativeHistory(
          await refreshAuthoritativeHistory(),
          sessionId,
        );
        if (cancelled) {
          return;
        }
        if (statusRef.current === 'submitted' || statusRef.current === 'streaming') {
          return;
        }
        if (activeTurnIdRef.current || pendingClientMessageRef.current) {
          return;
        }
        const detail = sessionSnapshotRef.current;
        const freshMessages = prepareInitialMessages(pageData.records, pageData.overlay, detail);
        const currentLocalPendingInteraction = getMessagePendingInteraction(messagesRef.current);
        const freshAuthoritativePendingInteraction = getMessagePendingInteraction(freshMessages);
        if (!shouldAdoptAuthoritativeMessagesForLocalPendingInteraction(
          detail,
          currentLocalPendingInteraction,
          freshAuthoritativePendingInteraction,
          !!pageData.overlay,
        )) {
          return;
        }
        adoptAuthoritativeMessages(freshMessages);
      } catch (err) {
        console.error('[useSessionChat] pending interaction authoritative adoption failed:', err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    authoritativeMessagePendingInteraction,
    messagePendingInteraction,
    overlay,
    refreshAuthoritativeHistory,
    session,
    sessionId,
    adoptAuthoritativeMessages,
  ]);

  // ── Sending→failed watchdog ──────────────────────────────────
  // An outbox item that stays 'sending' for 45s without being accepted, and
  // without authoritative evidence that the backend received it, rolls back to
  // 'failed' so it appears in the retry queue. The optimistic user message is
  // removed from the transcript at the same time: the queue is its only home.
  useEffect(() => {
    const SEND_TIMEOUT_MS = 45_000;
    const timer = window.setInterval(() => {
      // Evidence the backend is processing: useChat is active, or the session
      // carries a current_turn_id.
      if (statusRef.current === 'submitted' || statusRef.current === 'streaming') return;
      if (activeTurnIdRef.current) return;

      const pending = pendingClientMessageRef.current;
      if (!pending || pending.accepted) return;
      if (Date.now() - pending.startedAt < SEND_TIMEOUT_MS) return;

      materializePendingSendFailure(pending.id, t('chat:send.unconfirmed_timeout'));
    }, 5_000);
    return () => window.clearInterval(timer);
  }, [materializePendingSendFailure, t]);

  // ── Delivery failure materialization ─────────────────────────
  useEffect(() => {
    if (String(session.delivery_state ?? '').trim() !== 'NOT_RECEIVED') return;
    if (!session.delivery_failure) return;
    if (status === 'submitted' || status === 'streaming') return;
    if (session.current_turn_id) return;
    if (session.delivery_failure.turn_id !== session.last_turn_id) return;
    let cancelled = false;
    void (async () => {
      try {
        const pageData = normalizeAuthoritativeHistory(
          await refreshAuthoritativeHistory(),
          sessionId,
        );
        if (cancelled) return;
        // Commit-time revalidation: if user retried during fetch, abort
        if (statusRef.current === 'submitted' || statusRef.current === 'streaming') return;
        const msgs = suppressNotReceivedUserMessages(
          prepareInitialMessages(pageData.records, pageData.overlay, session),
          session,
        );
        adoptAuthoritativeMessages(msgs);
        setOutbox((prev) => rehydrateDeliveryFailure(prev, session.delivery_failure));
      } catch (err) {
        console.error('[useSessionChat] delivery failure materialization error:', err);
      }
    })();
    return () => { cancelled = true; };
  }, [adoptAuthoritativeMessages, refreshAuthoritativeHistory, session.delivery_state, session.delivery_failure?.turn_id, status, session.current_turn_id, session.last_turn_id, sessionId, session]);

  // Clear local outbox shadows once authoritative durable user rows prove the
  // backend has already accepted the message.
  useEffect(() => {
    setOutbox((prev) => reconcileOutboxWithSession(prev, session, durableRecords));
  }, [durableRecords, session, setOutbox]);

  // ── Manual / structural-error rehydrate ──────────────────────
  const rehydrateFromAuthoritativeSnapshot = useCallback(async (receivedCursor?: TurnStreamState['cursor']) => {
    const [refreshResult, historyResult] = await Promise.all([
      refresh({ force: true }),
      refreshAuthoritativeHistory(),
    ]);
    const detail = refreshResult.detail ?? sessionSnapshotRef.current;
    if (sessionIdRef.current !== sessionId) {
      throw new Error('Session changed while authoritative stream state was rebuilding');
    }
    const pageData = normalizeAuthoritativeHistory(historyResult, sessionId);
    const snapshotCursor = bootstrapStreamCursor(
      String(detail.current_turn_id ?? '').trim() || null,
      pageData.overlay?.resume_cursor ?? null,
      pageData.sessionFrameSeq,
    );
    // A reply handoff has already received its durable terminal cursor. An
    // empty next-reply overlay must not rewind that subscription to the start.
    dispatchTurnStream({
      type: 'cursor-rebased',
      cursor: receivedCursor && (receivedCursor.frameSeq ?? -1) > (snapshotCursor.frameSeq ?? -1)
        ? receivedCursor
        : snapshotCursor,
    });
    if (!pageData.overlay) {
      setIsInterruptSettling(false);
    }
    adoptAuthoritativeMessages(prepareInitialMessages(pageData.records, pageData.overlay, {
      ...detail,
      pending_interaction: pageData.pendingInteraction,
    }));
    clearError();
  }, [sessionId, refresh, refreshAuthoritativeHistory, adoptAuthoritativeMessages, clearError, dispatchTurnStream]);
  authoritativeRehydrateRef.current = rehydrateFromAuthoritativeSnapshot;

  const handleRehydrate = useCallback(async () => {
    try {
      await rehydrateFromAuthoritativeSnapshot();
    } catch (err) {
      console.error('[useSessionChat] rehydrate failed:', err);
    }
  }, [rehydrateFromAuthoritativeSnapshot]);

  const rebuildFromSessionStore = useCallback(async (_resumeSequence: number) => {
    const [refreshResult, historyResult] = await Promise.all([
      refresh({ force: true }),
      refreshAuthoritativeHistory(),
    ]);
    const detail = refreshResult.detail;
    if (!detail) throw new Error('SessionStore rebuild returned no session detail');
    if (sessionIdRef.current !== sessionId) {
      throw new Error('Session changed while SessionStore history was rebuilding');
    }
    const pageData = normalizeAuthoritativeHistory(historyResult, sessionId);
    adoptAuthoritativeMessages(prepareInitialMessages(pageData.records, pageData.overlay, detail));
    clearError();
  }, [adoptAuthoritativeMessages, clearError, refresh, refreshAuthoritativeHistory, sessionId]);
  sessionStoreRebuildRef.current = rebuildFromSessionStore;

  // Stale pending interaction error recovery
  const handledStalePendingRef = useRef<string | null>(null);
  useEffect(() => {
    if (!chatError || !isStalePendingInteractionError(chatError)) {
      handledStalePendingRef.current = null;
      return;
    }
    const key = String(chatError.message ?? chatError).trim();
    if (handledStalePendingRef.current === key) return;
    handledStalePendingRef.current = key;
    setInteractionSubmitting(false);
    dispatchTurnStream({ type: 'went-idle' });
    resetTransientState();
    void (async () => {
      await Promise.allSettled([refresh({ force: true }), handleRehydrate()]);
      clearError();
    })();
  }, [chatError, clearError, refresh, handleRehydrate, resetTransientState]);

  // ── Auto-resume ──────────────────────────────────────────────
  // One question, one answer: session/turnStream.ts decides, this wires it to
  // React and executes. Keeping the decision in this one place matters: a
  // gate chain duplicated across call sites risks the copies disagreeing on
  // whether a turn needs to resume.
  const autoResumeDecision = useCallback(() => decideAutoResume(turnStreamRef.current, {
    transportBusy: statusRef.current === 'submitted' || statusRef.current === 'streaming',
    pendingInteraction: Boolean(pendingInteraction),
    needsResume,
  }), [pendingInteraction, needsResume]);

  /** Ensure exactly one response owns the session output subscription. */
  const ensureSessionStream = useCallback(() => {
    if (!navigator.onLine) return;
    if (subscriptionTaskRef.current) return;
    if (sessionStoreReloadRef.current !== null) return;
    const decision = autoResumeDecision();
    if (decision.kind === 'exhausted') { setAutoResumeBudgetExhausted(true); return; }
    if (!shouldOpenSessionSubscription(liveSubscriptionEnabled, decision)) return;
    setAutoResumeBudgetExhausted(false);
    subscriptionFinishRef.current = null;
    let knownTurnId: string | null = null;
    if (decision.kind === 'resume') {
      knownTurnId = decision.turnId;
    } else if (
      decision.kind === 'idle'
      && decision.reason !== 'no-turn'
      && decision.reason !== 'turn-finished'
    ) {
      knownTurnId = turnStreamRef.current.serverTurnId;
    }
    // The SDK starts resume with a fresh parser; the server supplies the
    // complete reply and its stable id. A seeded empty message is not consumed.
    dispatchTurnStream({ type: 'resume-started', turnId: knownTurnId });
    let reopenAfterSettle = false;
    const task = (async () => {
      try {
        await resumeStream();
      } catch (err) {
        console.error('[useSessionChat] session stream failed:', err);
      } finally {
        const reload = sessionStoreReloadRef.current;
        if (reload !== null) {
          const result = await reload.promise;
          const stillCurrent = sessionStoreReloadRef.current === reload;
          if (stillCurrent && result.ok) {
            sessionStoreReloadRef.current = null;
            clearError();
          }
          if (result.ok && stillCurrent) {
            reopenAfterSettle = true;
          } else if (!result.ok && stillCurrent) {
            dispatchTurnStream({
              type: 'stream-error',
              hasActiveTurn: Boolean(activeTurnIdRef.current),
            });
            setTransientBannerVisible(true);
            console.error('[useSessionChat] SessionStore rebuild failed:', result.error);
          }
          return;
        }
        if (subscriptionFinishRef.current === 'clean' && subscriptionNeedsHistoryRef.current) {
          subscriptionNeedsHistoryRef.current = false;
          try {
            await Promise.all([
              refresh({ force: true }),
              refreshAuthoritativeHistory({ mode: 'acceptance', commit: false }),
            ]);
          } catch (err) {
            dispatchTurnStream({ type: 'stream-error', hasActiveTurn: Boolean(activeTurnIdRef.current) });
            setTransientBannerVisible(true);
            console.error('[useSessionChat] completed reply history refresh failed:', err);
            return;
          }
        }
        reopenAfterSettle = subscriptionFinishRef.current === 'clean';
      }
    })();
    subscriptionTaskRef.current = task;
    void task.then(() => {
      if (subscriptionTaskRef.current !== task) return;
      subscriptionTaskRef.current = null;
      if (!subscriptionMountedRef.current || !liveSubscriptionEnabledRef.current) return;
      if (reopenAfterSettle) {
        ensureSessionStreamRef.current();
        return;
      }
      // A subscription that ended without a clean finish — the reopen's fetch
      // failed, or its body died — is reopened on the dropped phase's backoff.
      // Nothing else re-arms it: the transport's status settles on 'error' and
      // stays there, so a page that waited for the next status change waited
      // for a recovery it could not see. The decision is asked again when the
      // timer fires. Turn recovery keeps its budget; idle subscriptions keep
      // reconnecting because they also carry future turns and title updates.
      const decision = autoResumeDecision();
      if (decision.kind !== 'resume' && decision.kind !== 'idle') return;
      window.setTimeout(() => {
        if (subscriptionMountedRef.current && liveSubscriptionEnabledRef.current) {
          ensureSessionStreamRef.current();
        }
      }, decision.kind === 'resume' ? decision.delayMs : 5000);
    });
  }, [autoResumeDecision, clearError, dispatchTurnStream, liveSubscriptionEnabled, refresh, refreshAuthoritativeHistory, resumeStream, sessionId]);
  ensureSessionStreamRef.current = ensureSessionStream;

  useEffect(() => {
    if (!liveSubscriptionEnabled) return;
    const delayMs = turnStreamRef.current.attempts > 1
      ? Math.min(1000 * turnStreamRef.current.attempts, 5000)
      : 0;
    const timer = window.setTimeout(ensureSessionStream, delayMs);
    return () => window.clearTimeout(timer);
  }, [ensureSessionStream, liveSubscriptionEnabled, status]);

  useEffect(() => {
    if (liveSubscriptionEnabled) return;
    void stopStream();
  }, [liveSubscriptionEnabled, stopStream]);

  useEffect(() => {
    subscriptionMountedRef.current = true;
    return () => {
      subscriptionMountedRef.current = false;
      void stopStream();
    };
  }, [sessionId, stopStream]);

  // Reconnect when a hidden tab becomes visible again.
  useEffect(() => {
    const handler = () => {
      if (document.visibilityState !== 'visible') return;
      ensureSessionStream();
    };
    document.addEventListener('visibilitychange', handler);
    window.addEventListener('online', handler);
    return () => {
      document.removeEventListener('visibilitychange', handler);
      window.removeEventListener('online', handler);
    };
  }, [ensureSessionStream]);

  useEffect(() => { setInteractionSubmitting(false); }, [pendingInteraction?.interaction_id]);

  useEffect(() => {
    if (session.state !== 'TERMINATED' && lifecycleState !== 'deleted') return;
    clearError(); resetTransientState(); dispatchTurnStream({ type: 'went-idle' });
  }, [clearError, dispatchTurnStream, lifecycleState, resetTransientState, session.state]);

  useEffect(() => {
    if (!pendingInteraction) return;
    clearError(); resetTransientState(); dispatchTurnStream({ type: 'went-idle' });
  }, [clearError, dispatchTurnStream, pendingInteraction, resetTransientState]);

  useEffect(() => {
    if (lifecycleState !== 'busy' && status !== 'submitted' && status !== 'streaming') {
      dispatchTurnStream({ type: 'went-idle' });
    }
  }, [dispatchTurnStream, lifecycleState, status]);

  // ── Retry intent ─────────────────────────────────────────────
  const latestDurableUnansweredUser = useMemo(() => {
    for (let i = durableRecords.length - 1; i >= 0; i -= 1) {
      if (durableRecords[i].role === 'assistant') return null;
      if (durableRecords[i].role === 'user') return durableRecords[i];
    }
    return null;
  }, [durableRecords]);

  const retryIntent = useMemo<RetryIntent>(() => {
    const rp = String(session.recovery_policy ?? '').trim().toLowerCase();
    const rr = String(session.recovery_reason ?? '').trim();
    if (lifecycleState === 'recovery') {
      if (rp === 'auto') return { kind: 'auto-recover', summary: t('chat:retry.auto_recover_summary') };
      return { kind: 'recover', label: t('chat:retry.recover_label'), summary: rr === 'sandbox_reattach_failed' || rr === 'sandbox_missing' || rr === 'sandbox_expired' ? t('chat:retry.recover_summary_sandbox_lost') : t('chat:retry.recover_summary') };
    }
    if (autoResumeBudgetExhausted) return { kind: 'budget-exhausted', summary: t('chat:retry.budget_exhausted_summary') };
    const ds = String(session.delivery_state ?? '').trim();
    const fp = String(session.last_turn_failure_phase ?? '').trim();
    if (ds === 'NOT_RECEIVED' && lifecycleState === 'ready' && !activeTurnId && latestDurableUnansweredUser && !!String(latestDurableUnansweredUser.content || '').trim()) {
      return { kind: 'delivery-failed', label: t('chat:retry.resend_label'), summary: t('chat:retry.delivery_failed_summary'), text: String(latestDurableUnansweredUser.content || '').trim(), clientMessageId: latestDurableUnansweredUser.client_message_id || undefined };
    }
    if (fp === 'pre_dispatch' && lifecycleState === 'ready' && !activeTurnId && session.last_turn_status === 'FAILED' && latestDurableUnansweredUser && !!String(latestDurableUnansweredUser.content || '').trim() && session.last_turn_id === latestDurableUnansweredUser.turn_id) {
      return { kind: 'delivery-failed', label: t('chat:retry.resend_label'), summary: t('chat:retry.delivery_failed_summary'), text: String(latestDurableUnansweredUser.content || '').trim(), clientMessageId: latestDurableUnansweredUser.client_message_id || undefined };
    }
    if (fp === 'post_dispatch' && lifecycleState === 'ready' && !activeTurnId && session.last_turn_status === 'FAILED' && session.last_turn_id) {
      if (isUserInterruptedTurnFailure(session)) return { kind: 'none' };
      return { kind: 'turn-failed', summary: t('chat:retry.turn_failed_summary'), failureDetail: String(session.last_turn_error ?? '').trim() || undefined };
    }
    return { kind: 'none' };
  }, [activeTurnId, autoResumeBudgetExhausted, latestDurableUnansweredUser, lifecycleState, session, t]);

  // Auto-recover
  const handleRecover = useCallback(() => { clearError(); void recover(); }, [clearError, recover]);
  const autoRecoverMarker = useMemo(() => {
    if (lifecycleState !== 'recovery' || retryIntent.kind !== 'auto-recover') return null;
    return [session.last_turn_command_id, session.last_turn_id, session.recovery_reason, session.last_error].map((v) => String(v ?? '').trim()).join('|');
  }, [lifecycleState, retryIntent.kind, session.last_error, session.last_turn_command_id, session.last_turn_id, session.recovery_reason]);
  const autoRecoverAttemptRef = useRef<string | null>(null);
  useEffect(() => {
    if (!autoRecoverMarker) { autoRecoverAttemptRef.current = null; return; }
    if (autoRecoverAttemptRef.current === autoRecoverMarker) return;
    autoRecoverAttemptRef.current = autoRecoverMarker;
    handleRecover();
  }, [autoRecoverMarker, handleRecover]);

  // ── Derived banners ──────────────────────────────────────────
  // The session is working, or a send is waiting to be accepted. Neither is
  // "the connection is open", which is all `status` reports: one stream stays
  // open for the session's whole life.
  const isSubmitted = deliveryInFlight;
  const isStreaming = lifecycleState === 'busy';
  // An idle subscription also follows future turns and title updates. Its
  // transport error is not a failed user operation, regardless of HTTP status.
  const isTransientSilent = (!activeTurnId && !deliveryInFlight)
    || (isInTransientRecoveryRef.current && !transientBannerVisible);
  const showTransientBanner = transientBannerVisible && !isTransientSilent;
  const suppressBanner = session.state === 'TERMINATED' || lifecycleState === 'deleted';
  const showRetryBanner = Boolean(!suppressBanner && (
    (((chatError && !isTransientSilent) || retryIntent.kind === 'turn-failed' || retryIntent.kind === 'auto-recover' || retryIntent.kind === 'delivery-failed' || retryIntent.kind === 'budget-exhausted') && !pendingInteraction)
      || showTransientBanner
  ));
  const showRetryButton = !showTransientBanner && (retryIntent.kind === 'recover' || retryIntent.kind === 'delivery-failed');

  // ── Callbacks ────────────────────────────────────────────────
  const sendClientMessageNow = useCallback((
    clientMessageId: string,
    text: string,
    images: TurnInputImage[] = [],
  ) => {
    const t = text.trim();
    const cid = String(clientMessageId ?? '').trim() || createClientMessageId();
    // A picture with no caption is still a message.
    if (!t && images.length === 0) return;
    setIsInterruptSettling(false);
    pendingClientMessageRef.current = { id: cid, text: t, turnId: null, accepted: false, startedAt: Date.now() };
    pendingInputsRef.current.set(cid, {
      clientMessageId: cid,
      inputId: cid,
      content: t,
    });
    // No projection boundary here. The two surfaces divide by ownership: the
    // queue shows what the platform is holding — a message the outbox has
    // accepted and the engine has not taken — while the transcript is the
    // agent's own record, one row per input the engine actually consumed.
    // Projecting on submit would put the same message in both at once, which
    // reads as two messages and promises the engine has it while the outbox is
    // still the only thing holding it. `data-input-consumed` creates the
    // boundary and drops the queue row in the same handler, so the message is
    // never missing from both surfaces at once.
    if (!activeTurnIdRef.current) {
      dispatchTurnStream({ type: 'send-started' });
    }
    clearError();
    setOutbox((current) => {
      const sending = {
        client_message_id: cid,
        text: t,
        status: 'sending' as const,
        ...(images.length > 0 ? { images } : {}),
      };
      const index = current.findIndex((item) => item.client_message_id === cid);
      if (index < 0) return [...current, sending];
      return current.map((item, itemIndex) => (itemIndex === index ? sending : item));
    });
    setDeliveryInFlight(true);
    void (async () => {
      try {
        const receipt = await appendTurnInput(sessionId, {
          content: turnInputContent(t, images),
          client_message_id: cid,
          permission_mode: permissionModeRef.current,
        });
        if (
          !('status' in receipt)
          || receipt.status !== 'delivered'
          || receipt.client_message_id !== cid
          || !String(receipt.command_id ?? '').trim()
          || !String(receipt.input_id ?? '').trim()
        ) {
          throw new Error('Agent FIFO returned an invalid delivery acknowledgement');
        }
        if (consumedInputIdsRef.current.has(receipt.input_id)) {
          pendingInputsRef.current.delete(cid);
          consumedInputIdsRef.current.add(cid);
          setOutbox((current) => current.filter((item) => (
            item.client_message_id !== cid
            && item.client_message_id !== receipt.input_id
            && item.input_id !== receipt.input_id
          )));
          setNativeInputProjectionState((current) => ({
            ...current,
            boundaries: current.ownerSessionId !== sessionId
              ? current.boundaries
              : current.boundaries.map((boundary) => (
                  boundary.inputId === receipt.input_id
                    ? { ...boundary, clientMessageId: cid }
                    : boundary
                )),
          }));
        } else {
          const stillPending = pendingInputsRef.current.get(cid);
          if (stillPending) stillPending.inputId = receipt.input_id;
          setNativeInputProjectionState((current) => ({
            ...current,
            boundaries: current.ownerSessionId !== sessionId
              ? current.boundaries
              : current.boundaries.map((boundary) => (
                  boundary.clientMessageId === cid
                    ? { ...boundary, inputId: receipt.input_id }
                    : boundary
                )),
          }));
          setOutbox((current) => current.map((item) => (
            item.client_message_id === cid
              ? {
                  ...item,
                  status: 'accepted' as const,
                  command_id: receipt.command_id,
                  input_id: receipt.input_id,
                }
              : item
          )));
        }
        // The receipt proves the platform holds this message — that is what it
        // says, and all it says. Which turn will carry it is a separate fact:
        // a queued input has no turn of its own yet, and the one that is
        // running belongs to an earlier message. Binding it to that turn makes
        // that turn's end look like this message's own, so everything keyed on
        // "this message's turn settled" fires for a message the engine has not
        // taken. The binding comes from the data-turn-accepted frame that names
        // this message; the read below stands in only when no turn is running,
        // where the next turn can only carry this accepted input.
        const runningTurnId = String(activeTurnIdRef.current ?? '').trim();
        markAcceptedRef.current(cid, null);
        try {
          const refreshed = await refresh({ force: true });
          const acceptedTurnId = String(
            refreshed?.detail?.current_turn_id ?? '',
          ).trim();
          if (acceptedTurnId && acceptedTurnId !== runningTurnId) {
            markAcceptedRef.current(cid, acceptedTurnId);
          }
        } catch (reason) {
          console.warn('[useSessionChat] accepted input refresh failed:', reason);
        }
        // Nothing is owed after this: the frames land on a stream that is
        // already open, or on the one the ensure below opens.
        ensureSessionStreamRef.current();
      } catch (err) {
        pendingInputsRef.current.delete(cid);
        console.error('[useSessionChat] send failed:', err);
        if (!consumedInputIdsRef.current.has(cid)) {
          materializePendingSendFailureRef.current(cid, String((err as Error)?.message ?? err));
        }
      } finally {
        setDeliveryInFlight(false);
      }
    })();
  }, [clearError, dispatchTurnStream, permissionModeRef, refresh, sessionId, setOutbox]);

  const handleStopGeneration = useCallback(async () => {
    if (interruptRequestInFlightRef.current) return;
    interruptRequestInFlightRef.current = true;
    interruptedTurnIdRef.current = activeTurnIdRef.current;
    setIsInterruptSettling(true);
    clearError();
    try {
      await interrupt();
      // The continuation after a stop lands on the session's stream whether
      // the stop parked an interaction or handed the state to a queued next
      // input — the cancelled turn's finish closes the stream segment, and
      // without a reader the successor's frames (its consumed boundary
      // included) reach nobody: the queue chip then survives a turn that is
      // already running. The ensure is a no-op when the subscription is
      // already open, and a stop with nothing queued just closes again at
      // the settled boundary.
      await refresh({ force: true });
      ensureSessionStream();
    } catch (err) {
      setIsInterruptSettling(false);
      console.error('[useSessionChat] interrupt failed:', err);
      throw err;
    } finally {
      interruptRequestInFlightRef.current = false;
    }
  }, [clearError, interrupt, refresh, ensureSessionStream]);

  useEffect(() => {
    setIsInterruptSettling(false);
  }, [sessionId]);

  useEffect(() => {
    // Cleared from the authoritative projection, not from a transport edge.
    // Depending on [isSubmitted, isStreaming] alone would deadlock the
    // composer: for a stop pressed while the turn is parked at an interaction
    // both flags are already false and stay false, so the edge this effect
    // waits for never arrives and "Stopping…" outlives a turn the server
    // settles within about two seconds — while the only other clearer is
    // sending a message, which the stuck flag itself disables. The projection
    // deps below re-run the effect when the interrupt's outcome lands (pending
    // cleared, lifecycle leaves 'interaction'); the body's condition still
    // keeps "Stopping…" for a stop that is genuinely still busy.
    if (!isSubmitted && !isStreaming && !pendingInteraction) {
      setIsInterruptSettling(false);
      return;
    }
    // A stop can also settle into a successor: the interrupted turn hands the
    // session to a queued next input, so the projection goes straight from the
    // stopped turn to a new busy one and the idle condition above never holds.
    // The successor's consumed boundary also clears this flag, but that frame
    // rides the live stream and a stop closes the stream segment, so it can
    // fire into the reader gap. The projection's turn moving off the stopped
    // turn is the durable form of the same outcome.
    if (activeTurnId && activeTurnId !== interruptedTurnIdRef.current) {
      setIsInterruptSettling(false);
    }
  }, [activeTurnId, isSubmitted, isStreaming, lifecycleState, pendingInteraction]);

  const handleInteractionSubmit = useCallback(async (response: InteractionResponse) => {
    if (!pendingInteraction || interactionSubmitting) return;
    const pi = pendingInteraction;
    const newMode = getInteractionPermissionMode(pi, response);
    if (newMode) permissionModeRef.current = newMode;
    setInteractionSubmitting(true);
    // Answering an interaction continues the turn: the backend closes the
    // segment at a park by design and resumes the same turn under a new command
    // id. The reducer cannot leave a "finished" mark standing behind this.
    dispatchTurnStream({ type: 'interaction-answered', turnId: pi.turn_id ?? activeTurnIdRef.current });
    clearError();
    try {
      await answerPendingInteraction(sessionId, pi.interaction_id, response);
      clearPendingInteraction(pi.interaction_id);
      const toolCallId = String(pi.tool_call_id ?? pi.interaction_id ?? '').trim();
      try {
        if (pi.presentation === 'tool_approval') {
          const d = (response as ToolPermissionInteractionResponse).decision;
          await addToolApprovalResponse({ id: pi.interaction_id, approved: d === 'approve', reason: d === 'reject' ? (response as ToolPermissionInteractionResponse).comment || undefined : undefined });
      } else {
          await addToolOutput({ tool: pi.tool_name, toolCallId, output: JSON.stringify(response) });
        }
      } catch (e) { console.warn('[useSessionChat] optimistic interaction UI update failed:', e); }
      // Turn/cursor bookkeeping is best-effort: it sharpens where the resume
      // starts, but the resume itself is owed either way. Letting a failed
      // refresh throw past the resume loses the continuation entirely — no
      // GET ai-stream?after_seq=N is issued at all — so the failure is caught
      // and the resume below still runs.
      try {
        const refreshed = await refresh({ force: true });
        const refreshedSession = refreshed?.detail ?? sessionSnapshotRef.current;
        const activeInteractionTurnId = String(
          refreshedSession?.current_turn_id ?? pi.turn_id ?? '',
        ).trim() || null;
        if (activeInteractionTurnId) {
          activeTurnIdRef.current = activeInteractionTurnId;
          setLocalTurnId((current) => (
            current === activeInteractionTurnId ? current : activeInteractionTurnId
          ));
          if (turnStreamRef.current.cursor.turnId !== activeInteractionTurnId) {
            // The refresh identifies the turn, but it does not establish a new
            // replay boundary. Frame sequence belongs to the Session, so only
            // retag the existing boundary; rewinding here would replay earlier
            // turns before continuing this interaction.
            dispatchTurnStream({
              type: 'cursor-rebased',
              cursor: {
                turnId: activeInteractionTurnId,
                frameSeq: turnStreamRef.current.cursor.frameSeq,
              },
            });
          }
        }
      } catch (e) {
        console.warn('[useSessionChat] post-answer session refresh failed; resuming anyway:', e);
      }
      // Nothing is owed here. The continuation lands on the session's stream,
      // which is already open — and if it is not, this ensures it. Waiting
      // before resuming would leave a gap in which the continuation has no
      // reader.
      ensureSessionStream();
    } catch (err) {
      setInteractionSubmitting(false);
      dispatchTurnStream({ type: 'went-idle' });
      console.error('[useSessionChat] interaction submit failed:', err);
      toast.error(err instanceof Error ? err.message : String(err));
      await Promise.allSettled([refresh({ force: true }), handleRehydrate()]);
    }
  }, [pendingInteraction, interactionSubmitting, clearError, sessionId, addToolApprovalResponse, addToolOutput, refresh, handleRehydrate, resumeStream, permissionModeRef, clearPendingInteraction]);

  const handleRetry = useCallback(() => {
    if (retryIntent.kind === 'recover') { handleRecover(); return; }
    if (retryIntent.kind === 'delivery-failed' && retryIntent.text.trim()) {
      sendClientMessageNow(retryIntent.clientMessageId || createClientMessageId(), retryIntent.text.trim());
      return;
    }
    clearError();
  }, [clearError, handleRecover, retryIntent, sendClientMessageNow]);

  return {
    messages: projectedMessages, childRunRevision,
    status, chatError: chatError ?? null, clearError, setMessages, historyFirstItemIndex,
    outbox, setOutbox, retryIntent,
    isSubmitted, isStreaming, isInterruptSettling,
    autoResumeBudgetExhausted, transientBannerVisible: showTransientBanner,
    showRetryBanner, showRetryButton, interactionSubmitting, pendingClientMessageRef,
    sendClientMessageNow, handleInteractionSubmit, handleStopGeneration, handleRehydrate, handleRetry, handleRecover,
  };
}
