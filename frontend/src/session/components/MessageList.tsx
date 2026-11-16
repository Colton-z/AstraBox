import { useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import type { UIMessage as SDKUIMessage } from 'ai';
import { ArrowDownIcon } from 'lucide-react';
import { useStickToBottom } from 'use-stick-to-bottom';
import { Message, MessageContent } from '@/components/ai-elements/message';
import { Button } from '@/components/ui/button';
import { MessageBubble } from './MessageParts';
import { PendingResponseContext, ResponsePending } from './ResponsePending';
import { SessionSendContext } from './UserMessageStatus';
import { conversationTimeline, type ConversationItem } from '../conversationTimeline';
import { inputFailure, inputMetadata } from '../sessionMessages';
import {
  ProcessDetailsCache,
  ProcessOpenCache,
  ProcessSummaryCache,
  type ProcessDetailsCacheValue,
  type ProcessSummaryCacheValue,
} from './LazyProcessBlock';

// How close to the top the scroller has to be for an upward gesture to ask
// for the older page. Exactly zero is a transient position: a page landing
// above moves the scroll position the moment it does, so a reader who keeps
// pulling up sits a little below the edge, never on it.
const OLDER_HISTORY_BOUNDARY_PX = 160;

type PendingKind = 'sending' | 'working';
// An accepted input the runtime has not finished with.
const OWED_INPUT_STATES = new Set(['pending', 'delivered', 'admitted']);
type ConversationRow = ConversationItem | { key: string; pending: true };

interface MessageListProps {
  messages: SDKUIMessage[];
  sessionId?: string;
  pendingToolCallId?: string;
  /** The response the session's stream is writing now. */
  writingMessageId?: string | null;
  /** A reply is on its way: a message is being sent, or the runtime works. */
  responseExpected?: boolean;
  /** What the session is doing to get a runtime, while it has none to answer with. */
  startingLabel?: string;
  firstItemIndex: number;
  hasMore: boolean;
  loadingMore: boolean;
  loadMoreError: string | null;
  scrollToBottomKey: string;
  onLoadOlder: () => Promise<void> | void;
}

/**
 * The conversation, in one scrolling column held at its end.
 *
 * The browser keeps the end in place: `use-stick-to-bottom` sets the scroll
 * position inside the resize notification of the content, which runs before
 * the frame paints, so a reply that grows never shows a frame with its newest
 * line below the fold. A list that measures a row after rendering it and then
 * scrolls cannot do that, which is why this one renders its rows plainly.
 * History arrives a page at a time, so the rows on the page are the pages a
 * reader asked for.
 */
export function MessageList({
  messages,
  sessionId,
  pendingToolCallId,
  writingMessageId,
  responseExpected,
  startingLabel,
  firstItemIndex,
  hasMore,
  loadingMore,
  loadMoreError,
  scrollToBottomKey,
  onLoadOlder,
}: MessageListProps) {
  const { t } = useTranslation();
  const { scrollRef, contentRef, isAtBottom, scrollToBottom } = useStickToBottom({
    initial: 'instant',
    resize: 'instant',
  });
  const userRequestedOlderRef = useRef(false);
  const loadOlderStateRef = useRef({ hasMore, loadingMore, onLoadOlder });
  // Folded work a reader has already opened, and the labels already fetched,
  // kept for the life of the list rather than of a row.
  const detailsCache = useRef<ProcessDetailsCacheValue>(new Map());
  const summaryCache = useRef<ProcessSummaryCacheValue>(new Map());
  const processOpenCache = useRef(new Map<string, boolean>());

  const sendState = useContext(SessionSendContext);
  const timeline = useMemo(() => conversationTimeline(messages), [messages]);
  // Between a message leaving and the reply's first content there is no
  // response to show, so the list holds the reply's place with one line.
  const lastMessage = timeline.at(-1)?.message;
  // A response being written above a message that waits behind it is already
  // on screen; one whose first frame has not reached the list is not.
  const writingShown = Boolean(writingMessageId) && timeline.some((item) => item.message.id === writingMessageId);
  const awaited = !writingShown && lastMessage?.role === 'user' ? lastMessage : null;
  const sending = awaited ? sendState?.unsent.get(awaited.id)?.status === 'sending' : false;
  // A message this page sent stays owed a reply from its acceptance on: the
  // session's own state arrives a read later, and the line must not blink out
  // in between.
  const sentHere = useRef(new Set<string>());
  if (awaited && sending) sentHere.current.add(awaited.id);
  const owed = awaited && !sendState?.unsent.has(awaited.id) && !inputFailure(awaited)
    && OWED_INPUT_STATES.has(String(inputMetadata(awaited).state));
  const pending: PendingKind | null = sending
    ? 'sending'
    : owed && (responseExpected || sentHere.current.has(awaited.id)) ? 'working' : null;
  const pendingFor = pending ? awaited!.id : null;
  const items = useMemo<ConversationRow[]>(
    () => (pendingFor ? [...timeline, { key: `pending:${pendingFor}`, pending: true }] : timeline),
    [timeline, pendingFor],
  );
  // The reply that takes the pending line's place is the same line continuing,
  // not a new block arriving.
  const hadPending = useRef(false);
  const continuesPending = useRef(new Set<string>());
  const lastRow = items.at(-1);
  if (hadPending.current && !pending && lastRow && !('pending' in lastRow) && lastRow.message.role === 'assistant') {
    continuesPending.current.add(lastRow.key);
  }
  hadPending.current = pending !== null;
  // The message keeps its one line until the reply's row has settled in: its
  // own status must not flash in the frame between the two.
  const lastPendingFor = useRef<string | null>(null);
  if (pendingFor) lastPendingFor.current = pendingFor;
  const quietMessageId = pendingFor
    ?? (lastRow && !('pending' in lastRow) && continuesPending.current.has(lastRow.key) ? lastPendingFor.current : null);

  // Rows keep the number they were first given: a page loaded above counts
  // down from the first row already shown.
  const pageAnchor = useRef({ sourceIndex: firstItemIndex, index: firstItemIndex, key: items[0]?.key });
  if (pageAnchor.current.sourceIndex !== firstItemIndex) {
    const retained = items.findIndex((item) => item.key === pageAnchor.current.key);
    if (retained < 0) throw new Error('Conversation pagination lost its first displayed item');
    pageAnchor.current.index -= retained;
    pageAnchor.current.sourceIndex = firstItemIndex;
  }
  pageAnchor.current.key = items[0]?.key;
  const displayFirstItemIndex = pageAnchor.current.index;

  loadOlderStateRef.current = { hasMore, loadingMore, onLoadOlder };

  const requestOlder = useCallback(() => {
    const state = loadOlderStateRef.current;
    if (!state.hasMore || state.loadingMore || !userRequestedOlderRef.current) return;
    userRequestedOlderRef.current = false;
    void state.onLoadOlder();
  }, []);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    let previousTouchY: number | null = null;
    const markOlderIntent = () => {
      if (loadOlderStateRef.current.loadingMore) return;
      userRequestedOlderRef.current = true;
    };
    const clearOlderIntent = () => {
      userRequestedOlderRef.current = false;
    };
    const atOlderBoundary = () => element.scrollTop <= OLDER_HISTORY_BOUNDARY_PX;
    const onWheel = (event: WheelEvent) => {
      if (event.deltaY < 0) {
        markOlderIntent();
        if (atOlderBoundary()) requestOlder();
      } else if (event.deltaY > 0) {
        clearOlderIntent();
      }
    };
    // A native scrollbar drag targets the scroller itself, not a row inside
    // it; it is the one pointer gesture the wheel and touch handlers never see.
    const onPointerDown = (event: PointerEvent) => {
      if (event.target === element) markOlderIntent();
    };
    const onTouchStart = (event: TouchEvent) => {
      previousTouchY = event.touches.item(0)?.clientY ?? null;
    };
    const onTouchMove = (event: TouchEvent) => {
      const currentTouchY = event.touches.item(0)?.clientY ?? null;
      if (previousTouchY !== null && currentTouchY !== null) {
        if (currentTouchY > previousTouchY) {
          markOlderIntent();
          if (atOlderBoundary()) requestOlder();
        } else if (currentTouchY < previousTouchY) {
          clearOlderIntent();
        }
      }
      previousTouchY = currentTouchY;
    };
    const clearTouch = () => {
      previousTouchY = null;
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.target !== element) return;
      if (event.key === 'Home') {
        event.preventDefault();
        element.scrollTop = 0;
        markOlderIntent();
        requestOlder();
      } else if (
        event.key === 'ArrowUp' || event.key === 'PageUp'
        || (event.key === ' ' && event.shiftKey)
      ) {
        markOlderIntent();
        if (atOlderBoundary()) requestOlder();
      } else if (
        event.key === 'ArrowDown' || event.key === 'PageDown' || event.key === 'End' || event.key === ' '
      ) {
        clearOlderIntent();
      }
    };
    const onScroll = () => {
      if (atOlderBoundary()) requestOlder();
    };
    element.addEventListener('pointerdown', onPointerDown);
    element.addEventListener('wheel', onWheel, { passive: true });
    element.addEventListener('keydown', onKeyDown);
    element.addEventListener('touchstart', onTouchStart, { passive: true });
    element.addEventListener('touchmove', onTouchMove, { passive: true });
    element.addEventListener('touchend', clearTouch, { passive: true });
    element.addEventListener('touchcancel', clearTouch, { passive: true });
    element.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      element.removeEventListener('pointerdown', onPointerDown);
      element.removeEventListener('wheel', onWheel);
      element.removeEventListener('keydown', onKeyDown);
      element.removeEventListener('touchstart', onTouchStart);
      element.removeEventListener('touchmove', onTouchMove);
      element.removeEventListener('touchend', clearTouch);
      element.removeEventListener('touchcancel', clearTouch);
      element.removeEventListener('scroll', onScroll);
    };
  }, [requestOlder, scrollRef]);

  useEffect(() => {
    if (loadingMore) userRequestedOlderRef.current = false;
  }, [loadingMore]);

  // A page loaded above must not move what the reader is looking at. The
  // browser's scroll anchoring does this where it exists; where it does not,
  // the height the page added is added to the scroll position before paint.
  const heightBeforePage = useRef<number | null>(null);
  if (loadingMore && heightBeforePage.current === null) {
    heightBeforePage.current = scrollRef.current?.scrollHeight ?? null;
  }
  useLayoutEffect(() => {
    if (loadingMore) return;
    const element = scrollRef.current;
    const before = heightBeforePage.current;
    heightBeforePage.current = null;
    if (!element || before === null || CSS.supports('overflow-anchor', 'auto')) return;
    element.scrollTop += element.scrollHeight - before;
  }, [loadingMore, displayFirstItemIndex, scrollRef]);

  // A first page of folded headers can be shorter than the viewport, and a
  // list that cannot scroll never reaches the boundary the gestures above
  // ask at. Ask for the next page directly until the list fills or history
  // runs out; a failed page stops it, so the retry stays the reader's.
  useEffect(() => {
    const element = scrollRef.current;
    if (!element || element.scrollHeight > element.clientHeight) return;
    if (!hasMore || loadingMore || loadMoreError) return;
    void onLoadOlder();
  }, [items.length, hasMore, loadingMore, loadMoreError, onLoadOlder, scrollRef]);

  // A question the response asks is at the end, and answering it is why the
  // reader is here, wherever they had scrolled to.
  useEffect(() => {
    if (!scrollToBottomKey) return;
    void scrollToBottom('instant');
  }, [scrollToBottom, scrollToBottomKey]);

  return (
    <div
      className="relative flex-1 min-h-0"
      data-testid="session-conversation-shell"
      data-pending-tool-call-id={pendingToolCallId || undefined}
    >
      <ProcessDetailsCache.Provider value={detailsCache.current}>
      <ProcessSummaryCache.Provider value={summaryCache.current}>
      <ProcessOpenCache.Provider value={processOpenCache.current}>
      <PendingResponseContext.Provider value={quietMessageId}>
      <div
        ref={scrollRef}
        data-testid="session-conversation"
        role="log"
        tabIndex={0}
        className="console-scroll h-full overflow-y-auto"
      >
        <div ref={contentRef} className="flex min-h-full flex-col justify-end">
          {items.map((item, offset) => ('pending' in item ? (
            <div key={item.key} className="mx-auto w-full max-w-reading px-4 py-2" data-testid="response-pending-row">
              <Message from="assistant" className="astra-enter max-w-full">
                <MessageContent className="clip-content w-full gap-2.5 overflow-clip">
                  <ResponsePending
                    announce
                    label={pending === 'sending'
                      ? t('misc:composer.queue_sending')
                      : startingLabel || t('chat:process.thinking')}
                  />
                </MessageContent>
              </Message>
            </div>
          ) : (
            <div
              // Ids are stable from the moment a message is shown: a user message
              // is its client_message_id, a response its response id.
              key={item.key}
              className="mx-auto w-full max-w-reading px-4 py-2"
              data-message-virtual-index={displayFirstItemIndex + offset}
              data-message-id={item.message.id}
              data-response-segment={item.responseSegment}
            >
              <MessageBubble
                message={item.message}
                sessionId={sessionId}
                pendingToolCallId={pendingToolCallId}
                isStreaming={item.message.role === 'assistant' && item.message.id === writingMessageId
                  && item.responseSegment !== 'preceding'}
                includedInResponse={item.acceptedBy}
                responseSegment={item.responseSegment}
                replacesPending={continuesPending.current.has(item.key)}
              />
            </div>
          )))}
        </div>
      </div>
      </PendingResponseContext.Provider>
      </ProcessOpenCache.Provider>
      </ProcessSummaryCache.Provider>
      </ProcessDetailsCache.Provider>
      {(loadingMore || loadMoreError) && (
        <div className="absolute left-1/2 top-2 z-10 -translate-x-1/2 rounded-md border border-border bg-background/95 px-3 py-1.5 text-xs shadow-sm">
          {loadingMore ? (
            <span className="text-muted-foreground">{t('chat:history.loading_older')}</span>
          ) : (
            <div className="flex items-center gap-2 text-destructive">
              <span>{t('chat:history.load_older_failed')}</span>
              <Button
                size="sm"
                variant="secondary"
                onClick={() => {
                  userRequestedOlderRef.current = true;
                  requestOlder();
                }}
              >
                {t('common:retry')}
              </Button>
            </div>
          )}
        </div>
      )}
      {!isAtBottom && (
        <Button
          aria-label={t('chat:history.scroll_to_bottom')}
          className="absolute bottom-4 left-1/2 -translate-x-1/2 rounded-full dark:bg-background dark:hover:bg-muted"
          onClick={() => { void scrollToBottom('instant'); }}
          size="icon"
          type="button"
          variant="outline"
        >
          <ArrowDownIcon className="size-4" />
        </Button>
      )}
    </div>
  );
}
