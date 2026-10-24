import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Chat, useChat } from '@ai-sdk/react';
import { isToolUIPart, type UIMessage } from 'ai';
import { toast } from 'sonner';
import i18n from '@/i18n';
import { ApiError, answerInteraction, interruptSession, sendInput } from '../../api';
import type {
  InteractionResponse,
  PendingInteraction,
  SessionRecord,
  ToolPermissionInteractionResponse,
  TurnInputImage,
  UserAction,
} from '../../types';
import { turnInputContent, turnInputImages } from '../composerAttachments';
import { readHistoryPage, readLatestWindow, nextHistoryCursor, type HistoryWindow } from '../historyPages';
import { SessionFollower, type FollowerHost } from '../sessionFollower';
import {
  inputFailure,
  inputMetadata,
  orderConversationMessages,
  mergeLatestMessages,
  prependOlderMessages,
  updateMessages,
  upsertMessage,
  userMessage,
  userMessageText,
  withMetadata,
  type InputFailure,
} from '../sessionMessages';
import { useTranscriptAccess } from '../TranscriptAccess';
import { cardArtifacts } from '@/artifacts/marker';
import type { ArtifactMarker } from '@/artifacts/types';
import { createClientMessageId } from '../utils/chatHelpers';
import { hasRuntimeProtocolError } from '../sessionAuthority';

/**
 * How long a lost session stream is retried in silence before the page says
 * so. Reconnects that succeed inside this window are noise the reader need not
 * see; past it, a page that looks live while nothing arrives is a lie by
 * omission.
 */
const STREAM_TROUBLE_NOTICE_DELAY_MS = 10_000;
/** The index Virtuoso numbers the first loaded message from; older pages count down from it. */
const FIRST_ITEM_INDEX = 100_000;

/** A message this page sent that the session has not accepted (yet). */
export interface UnsentMessage {
  status: 'sending' | 'failed';
  error?: string;
  /** The session refused this message for good: sending it again is a new message. */
  resendAsNew?: boolean;
  userAction?: UserAction | null;
  text: string;
  images: TurnInputImage[];
}

export interface UseSessionChatProps {
  sessionId: string;
  session: SessionRecord;
  /** The history window the page opened with; its `outputSeq` is where the stream is followed from. */
  history: HistoryWindow & { outputSeq: number };
  /** Read the session's detail again; resolves with the detail read. */
  refresh: (options?: { force?: boolean }) => Promise<{ detail: SessionRecord | null }>;
  /** Read the session's entry in the conversation lists again. */
  onSessionChanged: () => void;
  onArtifactAppeared: (marker: ArtifactMarker, toolCallId: string) => void;
}

interface PendingStop {
  requestId: number;
  responseIds: readonly string[];
}

/**
 * The conversation as its owner sees it: history, the session's stream, and
 * what the owner does to it.
 *
 * The messages live in an AI SDK `Chat` (`useChat` publishes them, throttled):
 * the history window the page opened with, older pages as the reader scrolls
 * up, each response as the stream writes it (`SessionFollower`), and the
 * owner's own messages from the moment they are sent. Messages are matched by
 * id everywhere — a user message is its `client_message_id`, an assistant
 * message its response id — so a message is never shown twice, and the
 * session's history read after a reload shows the same messages in the same
 * places.
 */
export function useSessionChat({
  sessionId,
  session,
  history,
  refresh,
  onSessionChanged,
  onArtifactAppeared,
}: UseSessionChatProps) {
  const { readHistory } = useTranscriptAccess();
  // The page is keyed by session, so the chat is created once per page with
  // the history it opened with.
  const [chat] = useState(() => new Chat<UIMessage>({ id: sessionId, messages: history.messages }));
  const { messages, setMessages: writeMessages, addToolApprovalResponse } = useChat({ chat, throttle: 50 });
  const nativeOrderRef = useRef(new Map<string, number>());
  const setMessages = useCallback((update: Parameters<typeof writeMessages>[0]) => {
    writeMessages((current) => orderConversationMessages(
      typeof update === 'function' ? update(current) : update, nativeOrderRef.current,
    ));
  }, [writeMessages]);

  const [followFrom, setFollowFrom] = useState(() => ({ seq: history.outputSeq }));
  const followerRef = useRef<SessionFollower | null>(null);
  const [writing, setWriting] = useState<string | null>(null);
  const [unsent, setUnsent] = useState<ReadonlyMap<string, UnsentMessage>>(() => new Map());
  const sendsInFlightRef = useRef(0);
  const [sendsInFlight, setSendsInFlight] = useState(0);
  const [liveInteraction, setLiveInteraction] = useState<PendingInteraction | null>(history.pendingInteraction);
  const [closedInteractions, setClosedInteractions] = useState<ReadonlySet<string>>(() => new Set());
  const [interactionSubmitting, setInteractionSubmitting] = useState(false);
  const [stopping, setStopping] = useState<PendingStop | null>(null);
  const stopRequestIdRef = useRef(0);
  const [streamTrouble, setStreamTrouble] = useState(false);
  const troubleTimerRef = useRef<number | null>(null);
  const [childRunRevision, setChildRunRevision] = useState(0);
  const [paging, setPaging] = useState(() => ({
    firstItemIndex: FIRST_ITEM_INDEX,
    hasMore: history.hasMore,
    nextCursor: history.nextCursor,
    loadingMore: false,
    loadMoreError: null as string | null,
  }));
  const pagingRef = useRef(paging);
  pagingRef.current = paging;
  /** Refusals that arrived before the receipt naming their input. */
  const earlyFailuresRef = useRef(new Map<string, InputFailure>());
  /** Inputs a response answered that this page has no message for, waiting on receipts in flight. */
  const unknownInputsRef = useRef(false);
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const liveInteractionRef = useRef(liveInteraction);
  liveInteractionRef.current = liveInteraction;
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  const onSessionChangedRef = useRef(onSessionChanged);
  onSessionChangedRef.current = onSessionChanged;
  const mountedRef = useRef(true);
  const historyControllerRef = useRef<AbortController | null>(null);
  const historyReadEpochRef = useRef(0);
  useEffect(() => {
    const controller = new AbortController();
    historyControllerRef.current = controller;
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      controller.abort();
    };
  }, []);

  const refreshDetail = useCallback(() => {
    void refreshRef.current({ force: true }).catch((error: unknown) => {
      console.warn('[useSessionChat] session detail refresh failed:', error);
    });
  }, []);

  // ── History ──────────────────────────────────────────────────────────

  /**
   * Read the newest history window again and merge it in.
   *
   * `restart` follows the stream again from the window's position, with
   * everything shown taken from history: the page does this when what the
   * stream delivered could not be folded, or when the reader asks for a
   * refresh. Without it, responses being written and messages changed while
   * the read was pending keep their shown version. The merge adds what the
   * stream does not carry — the
   * messages someone sent from another tab, a channel or a schedule.
   */
  const readHistoryAgain = useCallback(async ({ restart }: { restart: boolean }) => {
    const signal = historyControllerRef.current?.signal;
    if (!signal || signal.aborted) return;
    // Restarting follows from the history's position, so nothing the old
    // follower would still write may land on top of what history says.
    if (restart) {
      historyReadEpochRef.current += 1;
      followerRef.current?.stop();
    }
    const epoch = historyReadEpochRef.current;
    const shown = new Map(chat.messages.map((message) => [message.id, message]));
    const writingAtRead = followerRef.current?.writing();
    try {
      const window = await readLatestWindow(readHistory, sessionId, chat.messages, signal);
      if (signal.aborted || !mountedRef.current || epoch !== historyReadEpochRef.current) return;
      if (restart && window.outputSeq === null) {
        throw new Error('SESSION_HISTORY_POSITION_MISSING: the history page carried no output_seq');
      }
      const keepShown = (message: UIMessage) => (
        !restart && (
          message.id === writingAtRead
          || message.id === followerRef.current?.writing()
          || shown.get(message.id) !== message
        )
      );
      let addedBeforeFirst = 0;
      setMessages((current) => {
        const next = mergeLatestMessages(current, window.messages, keepShown, pagingRef.current.hasMore);
        if (current.length > 0) addedBeforeFirst = next.findIndex((message) => message.id === current[0].id);
        return next;
      });
      if (addedBeforeFirst > 0) {
        setPaging((current) => ({ ...current, firstItemIndex: current.firstItemIndex - addedBeforeFirst }));
      }
      if (pagingRef.current.nextCursor === null && !pagingRef.current.hasMore) {
        setPaging((current) => ({ ...current, hasMore: window.hasMore, nextCursor: window.nextCursor }));
      }
      if (restart && window.outputSeq !== null) {
        // The stream follows from here, so an interaction opened before this
        // read arrives on it only if history does not already say so.
        setLiveInteraction(window.pendingInteraction);
        setWriting(null);
        setFollowFrom({ seq: window.outputSeq });
      }
    } catch (error) {
      if (signal.aborted) return;
      console.error('[useSessionChat] history read failed:', error);
      if (restart && mountedRef.current && epoch === historyReadEpochRef.current) {
        setStreamTrouble(true);
        window.setTimeout(() => {
          if (mountedRef.current && epoch === historyReadEpochRef.current) {
            void readHistoryAgainRef.current({ restart: true });
          }
        }, 5_000);
      }
    }
  }, [chat, readHistory, sessionId, setMessages]);
  const readHistoryAgainRef = useRef(readHistoryAgain);
  readHistoryAgainRef.current = readHistoryAgain;

  const loadOlder = useCallback(async () => {
    const signal = historyControllerRef.current?.signal;
    const current = pagingRef.current;
    const before = current.nextCursor;
    if (!signal || signal.aborted || !current.hasMore || !before || current.loadingMore) return;
    setPaging((state) => ({ ...state, loadingMore: true, loadMoreError: null }));
    try {
      const page = await readHistoryPage(readHistory, sessionId, before, signal);
      const nextCursor = nextHistoryCursor(page, before);
      if (signal.aborted || !mountedRef.current) return;
      let added = 0;
      setMessages((shown) => {
        const next = prependOlderMessages(shown, page.messages);
        added = next.length - shown.length;
        return next;
      });
      setPaging((state) => ({
        ...state,
        firstItemIndex: state.firstItemIndex - added,
        hasMore: page.has_more,
        nextCursor,
        loadingMore: false,
      }));
    } catch (error) {
      if (signal.aborted || !mountedRef.current) return;
      setPaging((state) => ({
        ...state,
        loadingMore: false,
        loadMoreError: error instanceof Error ? error.message : String(error),
      }));
    }
  }, [readHistory, sessionId, setMessages]);

  // ── The stream ───────────────────────────────────────────────────────

  const closeInteractionsOf = useCallback((responseId: string) => {
    const owned = [liveInteractionRef.current, sessionRef.current.pending_interaction]
      .filter((interaction): interaction is PendingInteraction => (
        Boolean(interaction) && interaction?.response_id === responseId
      ))
      .map((interaction) => interaction.interaction_id);
    if (owned.length === 0) return;
    setClosedInteractions((current) => new Set([...current, ...owned]));
  }, []);

  /**
   * A paused response went on, so an interaction it waited on was answered —
   * here or anywhere else. Which one, when two were open at once, only the
   * session knows: its read names the oldest interaction still unanswered (an
   * answer is recorded before the runtime is handed it, so before the response
   * goes on). The interaction this page shows for the response is closed
   * unless that read still names it, and kept if a newer one replaced it in
   * the meantime, since the read may predate the newer one.
   */
  const settleInteractionOf = useCallback((responseId: string) => {
    const shown = liveInteractionRef.current;
    void refreshRef.current({ force: true }).then(({ detail }) => {
      if (!mountedRef.current || !detail || !shown || shown.response_id !== responseId) return;
      if (liveInteractionRef.current?.interaction_id !== shown.interaction_id) return;
      if (detail.pending_interaction?.interaction_id === shown.interaction_id) return;
      setClosedInteractions((current) => new Set(current).add(shown.interaction_id));
    }).catch((error: unknown) => {
      console.warn('[useSessionChat] session detail refresh failed:', error);
    });
  }, []);

  const markInputs = useCallback((
    inputIds: readonly string[],
    metadata: (message: UIMessage) => Record<string, unknown> | null,
  ): string[] => {
    const wanted = new Set(inputIds);
    const found = new Set<string>();
    setMessages((current) => updateMessages(
      current,
      (message) => {
        const inputId = message.role === 'user' ? inputMetadata(message).input_id : undefined;
        if (!inputId || !wanted.has(inputId)) return false;
        found.add(inputId);
        return true;
      },
      (message) => {
        const next = metadata(message);
        return next ? withMetadata(message, next) : message;
      },
    ));
    return inputIds.filter((inputId) => !found.has(inputId));
  }, [setMessages]);

  const noteUnknownInputs = useCallback(() => {
    if (sendsInFlightRef.current > 0) {
      unknownInputsRef.current = true;
      return;
    }
    unknownInputsRef.current = false;
    void readHistoryAgainRef.current({ restart: false });
  }, []);

  const finishStopping = useCallback((responseId: string) => {
    setStopping((current) => {
      if (!current) return current;
      const remaining = current.responseIds.filter((id) => id !== responseId);
      if (remaining.length === 0) return null;
      return remaining.length === current.responseIds.length ? current : { ...current, responseIds: remaining };
    });
  }, []);

  const hostRef = useRef<FollowerHost>(null as unknown as FollowerHost);
  hostRef.current = {
    messages: () => chat.messages,
    write: (message) => {
      setMessages((current) => upsertMessage(current, message));
      for (const part of message.parts) {
        const toolCallId = String((part as { data?: { toolCallId?: unknown } }).data?.toolCallId ?? '');
        for (const marker of cardArtifacts(part)) onArtifactAppeared(marker, toolCallId);
      }
    },
    responseStarted: (messageId, metadata, reopened) => {
      setWriting(messageId);
      setStopping((current) => {
        if (!current) return current;
        if (current.responseIds.length === 0) return { ...current, responseIds: [messageId] };
        return current.responseIds.includes(messageId) ? current : null;
      });
      const inputIds = Array.isArray(metadata.input_ids) ? metadata.input_ids.map(String) : [];
      if (inputIds.length > 0) {
        const unknown = markInputs(inputIds, (message) => (
          inputMetadata(message).state === 'failed' ? null : { state: 'admitted' }
        ));
        if (unknown.length > 0) noteUnknownInputs();
      }
      if (reopened) settleInteractionOf(messageId);
      else refreshDetail();
    },
    responsePaused: () => {
      setWriting(null);
      refreshDetail();
    },
    responseEnded: (messageId) => {
      setWriting((current) => current === messageId ? null : current);
      finishStopping(messageId);
      closeInteractionsOf(messageId);
      void (async () => {
        const read = await refreshRef.current({ force: true }).catch(() => null);
        const last = read?.detail?.last_response;
        await followerRef.current?.written(messageId);
        if (!mountedRef.current) return;
        if (last?.response_id === messageId && last.status === 'ended') {
          setMessages((current) => updateMessages(
            current,
            (message) => message.id === messageId,
            (message) => withMetadata(message, {
              outcome: last.outcome,
              native_reason: last.native_reason,
              ended_at: last.ended_at,
              ...(last.error ? { error: last.error } : {}),
            }),
          ));
          return;
        }
        // Another response ended after this one before the detail was read
        // (a held message answered at once after a stop), and the detail names
        // that one instead. History records how every response ended.
        void readHistoryAgainRef.current({ restart: false });
      })();
    },
    inputFailed: ({ input_id: inputId, ...failure }) => {
      const unknown = markInputs([inputId], () => ({ state: 'failed', failure }));
      if (unknown.length > 0) {
        if (sendsInFlightRef.current > 0) earlyFailuresRef.current.set(inputId, failure);
        else noteUnknownInputs();
      }
      refreshDetail();
    },
    messageOrdered: (position) => {
      const key = `${position.kind}:${position.id}`;
      const retained = nativeOrderRef.current.get(key);
      nativeOrderRef.current.set(key, retained === undefined ? position.order : Math.min(retained, position.order));
      setMessages((current) => current);
      if (position.kind === 'input' && !chat.messages.some((message) => (
        message.role === 'user' && inputMetadata(message).input_id === position.id
      ))) noteUnknownInputs();
    },
    interactionOpened: (interaction) => {
      setLiveInteraction(interaction);
      refreshDetail();
    },
    interactionClosed: (interactionId) => {
      setClosedInteractions((current) => new Set(current).add(interactionId));
      refreshDetail();
    },
    childRunsChanged: () => {
      setChildRunRevision((revision) => revision + 1);
    },
    sessionChanged: () => {
      refreshDetail();
      onSessionChangedRef.current();
    },
    connected: () => {
      if (troubleTimerRef.current !== null) {
        window.clearTimeout(troubleTimerRef.current);
        troubleTimerRef.current = null;
      }
      setStreamTrouble(false);
    },
    disconnected: (error) => {
      console.warn('[useSessionChat] session stream lost:', error);
      if (troubleTimerRef.current !== null) return;
      troubleTimerRef.current = window.setTimeout(() => {
        troubleTimerRef.current = null;
        if (mountedRef.current) setStreamTrouble(true);
      }, STREAM_TROUBLE_NOTICE_DELAY_MS);
    },
    broken: (error) => {
      console.error('[useSessionChat] the session stream could not be applied; reading history again:', error);
      void readHistoryAgainRef.current({ restart: true });
    },
  };

  useEffect(() => {
    const host: FollowerHost = {
      messages: () => hostRef.current.messages(),
      write: (message) => hostRef.current.write(message),
      responseStarted: (...args) => hostRef.current.responseStarted(...args),
      responsePaused: (...args) => hostRef.current.responsePaused(...args),
      responseEnded: (...args) => hostRef.current.responseEnded(...args),
      inputFailed: (...args) => hostRef.current.inputFailed(...args),
      messageOrdered: (...args) => hostRef.current.messageOrdered(...args),
      interactionOpened: (...args) => hostRef.current.interactionOpened(...args),
      interactionClosed: (...args) => hostRef.current.interactionClosed(...args),
      childRunsChanged: () => hostRef.current.childRunsChanged(),
      sessionChanged: () => hostRef.current.sessionChanged(),
      connected: () => hostRef.current.connected(),
      disconnected: (...args) => hostRef.current.disconnected(...args),
      broken: (...args) => hostRef.current.broken(...args),
    };
    const follower = new SessionFollower(sessionId, followFrom.seq, host);
    followerRef.current = follower;
    follower.start();
    return () => {
      follower.stop();
      if (followerRef.current === follower) followerRef.current = null;
    };
  }, [followFrom, sessionId]);

  useEffect(() => () => {
    if (troubleTimerRef.current !== null) window.clearTimeout(troubleTimerRef.current);
  }, []);

  // ── Sending ──────────────────────────────────────────────────────────

  const deliver = useCallback(async (clientMessageId: string, text: string, images: TurnInputImage[]) => {
    sendsInFlightRef.current += 1;
    setSendsInFlight(sendsInFlightRef.current);
    setUnsent((current) => new Map(current).set(clientMessageId, { status: 'sending', text, images }));
    try {
      const receipt = await sendInput(sessionId, {
        content: turnInputContent(text, images),
        client_message_id: clientMessageId,
      });
      if (!mountedRef.current) return;
      setUnsent((current) => {
        const next = new Map(current);
        next.delete(clientMessageId);
        return next;
      });
      const earlyFailure = earlyFailuresRef.current.get(receipt.input_id);
      earlyFailuresRef.current.delete(receipt.input_id);
      setMessages((current) => updateMessages(
        current,
        (message) => message.id === clientMessageId,
        (message) => {
          const failure = inputFailure({ ...message, metadata: receipt }) ?? earlyFailure;
          return withMetadata(message, {
            input_id: receipt.input_id,
            created_at: receipt.accepted_at,
            state: failure ? 'failed' : receipt.state,
            ...(failure ? { failure } : {}),
          });
        },
      ));
      refreshDetail();
    } catch (error) {
      if (!mountedRef.current) return;
      console.error('[useSessionChat] send failed:', error);
      setUnsent((current) => new Map(current).set(clientMessageId, {
        status: 'failed',
        error: error instanceof Error ? error.message : String(error),
        // The session answers a refused id with the same refusal.
        resendAsNew: error instanceof ApiError && error.retryable === false,
        userAction: error instanceof ApiError ? error.userAction : null,
        text,
        images,
      }));
    } finally {
      sendsInFlightRef.current -= 1;
      if (mountedRef.current) setSendsInFlight(sendsInFlightRef.current);
      if (sendsInFlightRef.current === 0) {
        earlyFailuresRef.current.clear();
        if (unknownInputsRef.current) noteUnknownInputs();
      }
    }
  }, [noteUnknownInputs, refreshDetail, sessionId, setMessages]);

  /** Send a message: shown at once, after everything already shown. */
  const sendMessage = useCallback((text: string, images: TurnInputImage[] = []) => {
    const content = text.trim();
    if (!content && images.length === 0) return;
    const clientMessageId = createClientMessageId();
    setMessages((current) => [...current, userMessage(clientMessageId, content, images)]);
    void deliver(clientMessageId, content, images);
  }, [deliver, setMessages]);

  /** Take back a message the session never accepted. */
  const removeUnsent = useCallback((clientMessageId: string) => {
    setUnsent((current) => {
      if (current.get(clientMessageId)?.status !== 'failed') return current;
      const next = new Map(current);
      next.delete(clientMessageId);
      return next;
    });
    setMessages((current) => current.filter((message) => message.id !== clientMessageId));
  }, [setMessages]);

  /** Send a message this page could not send again; a refused one goes as a new message. */
  const retryUnsent = useCallback((clientMessageId: string) => {
    const entry = unsent.get(clientMessageId);
    if (!entry || entry.status !== 'failed') return;
    if (entry.resendAsNew) {
      removeUnsent(clientMessageId);
      sendMessage(entry.text, entry.images);
      return;
    }
    void deliver(clientMessageId, entry.text, entry.images);
  }, [deliver, removeUnsent, sendMessage, unsent]);

  /** Send what a refused message said again, as a new message. */
  const sendAgain = useCallback((message: UIMessage) => {
    const files = message.parts.filter((part) => part.type === 'file');
    sendMessage(userMessageText(message), turnInputImages(files));
  }, [sendMessage]);

  // ── Interactions ─────────────────────────────────────────────────────

  const pendingInteraction = useMemo(() => {
    for (const candidate of [liveInteraction, session.pending_interaction ?? null]) {
      if (candidate && !closedInteractions.has(candidate.interaction_id)) return candidate;
    }
    return null;
  }, [closedInteractions, liveInteraction, session.pending_interaction]);

  const handleInteractionSubmit = useCallback(async (response: InteractionResponse) => {
    const interaction = pendingInteraction;
    if (!interaction || interactionSubmitting) return;
    setInteractionSubmitting(true);
    try {
      await answerInteraction(sessionId, interaction.interaction_id, response);
      setClosedInteractions((current) => new Set(current).add(interaction.interaction_id));
      if (interaction.presentation === 'tool_approval') {
        const answer = response as ToolPermissionInteractionResponse;
        const approved = answer.decision === 'approve';
        const reason = approved ? undefined : answer.comment || undefined;
        const responseId = interaction.response_id ?? '';
        // The approval is shown on the tool call it asked about, when the
        // response carries one; the SDK finds that part by its approval id.
        const asked = chat.messages.some((message) => message.id === responseId && message.parts.some((part) => (
          isToolUIPart(part) && part.approval?.id === interaction.interaction_id
        )));
        const follower = followerRef.current;
        if (asked && !follower?.recordApproval(responseId, interaction.interaction_id, approved, reason)) {
          await follower?.written(responseId);
          await addToolApprovalResponse({ id: interaction.interaction_id, approved, reason });
        }
      }
    } catch (error) {
      if (error instanceof ApiError && (error.code === 'INTERACTION_NOT_OPEN' || error.code === 'INTERACTION_NOT_FOUND')) {
        // It was answered or closed elsewhere; the session's read says what is current.
        setClosedInteractions((current) => new Set(current).add(interaction.interaction_id));
      }
      console.error('[useSessionChat] interaction answer failed:', error);
      toast.error(error instanceof Error ? error.message : String(error));
    } finally {
      if (mountedRef.current) setInteractionSubmitting(false);
      refreshDetail();
    }
  }, [addToolApprovalResponse, chat, interactionSubmitting, pendingInteraction, refreshDetail, sessionId]);

  // ── Interrupting ─────────────────────────────────────────────────────

  const handleStopGeneration = useCallback(async () => {
    if (stopping) return;
    const writingId = followerRef.current?.writing();
    const request: PendingStop = {
      requestId: ++stopRequestIdRef.current,
      responseIds: [...new Set([
        ...(sessionRef.current.current_responses ?? []).map((response) => response.response_id),
        ...(writingId ? [writingId] : []),
      ])],
    };
    setStopping(request);
    const clearRequest = () => setStopping((current) => current?.requestId === request.requestId ? null : current);
    try {
      const receipt = await interruptSession(sessionId);
      if (receipt.status !== 'accepted') clearRequest();
    } catch (error) {
      clearRequest();
      console.error('[useSessionChat] interrupt failed:', error);
      toast.error(error instanceof ApiError && error.code === 'PENDING_INPUTS_NOT_CANCELLED'
        ? i18n.t('chat:page.pending_inputs_not_cancelled')
        : error instanceof Error ? error.message : String(error));
    } finally {
      refreshDetail();
    }
  }, [refreshDetail, sessionId, stopping]);

  const runtimeWorking = session.state === 'PROCESSING'
    || session.state === 'WAITING_INPUT'
    || (session.current_responses?.length ?? 0) > 0;
  useEffect(() => {
    if (!stopping) return;
    const ended = session.last_response;
    if (ended?.status === 'ended' && stopping.responseIds.includes(ended.response_id)
      && !session.current_responses?.some((response) => response.response_id === ended.response_id)) {
      finishStopping(ended.response_id);
      // A missed finish leaves the follower's segment open; canonical history
      // closes it without assuming that a same-id start is a different response.
      if (writing === ended.response_id && (session.current_responses?.length ?? 0) === 0) {
        void readHistoryAgainRef.current({ restart: true });
      }
    } else if (!runtimeWorking && writing === null) {
      setStopping((current) => current?.requestId === stopping.requestId ? null : current);
    }
  }, [finishStopping, runtimeWorking, session.current_responses, session.last_response, stopping, writing]);

  // ── What the page shows ─────────────────────────────────────────────

  const heldInputIds = useMemo(
    () => new Set((session.held_inputs ?? []).map((input) => input.input_id)),
    [session.held_inputs],
  );

  return {
    messages,
    /** The response the stream is writing now. */
    writingMessageId: hasRuntimeProtocolError(session) ? null : writing,
    /** A response is running: on this page's stream, or by the session's own account. */
    isWorking: !hasRuntimeProtocolError(session) && (writing !== null || session.state === 'PROCESSING'),
    /** A message is on its way to the session. */
    isSending: sendsInFlight > 0,
    isStopping: stopping !== null,
    streamTrouble,
    childRunRevision,
    firstItemIndex: paging.firstItemIndex,
    hasMore: paging.hasMore,
    loadingMore: paging.loadingMore,
    loadMoreError: paging.loadMoreError,
    loadOlder,
    unsent,
    heldInputIds,
    pendingInteraction,
    interactionSubmitting,
    sendMessage,
    retryUnsent,
    removeUnsent,
    sendAgain,
    handleInteractionSubmit,
    handleStopGeneration,
    rehydrate: () => readHistoryAgain({ restart: true }),
  };
}
