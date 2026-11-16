import { useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { UIMessage as SDKUIMessage } from 'ai';
import { ArrowDownIcon } from 'lucide-react';
import { Virtuoso, type VirtuosoHandle } from 'react-virtuoso';
import { Button } from '@/components/ui/button';
import { Message, MessageContent } from '@/components/ai-elements/message';
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
// for the older page. Exactly zero is a transient position: prepending a page
// compensates scrollTop the moment it lands, so a reader who keeps pulling up
// sits a little below the edge, never on it.
const OLDER_HISTORY_BOUNDARY_PX = 160;

type PendingKind = 'sending' | 'working';
// An accepted input the runtime has not finished with.
const OWED_INPUT_STATES = new Set(['pending', 'delivered', 'admitted']);
type ConversationRow = ConversationItem | { key: string; pending: true };

interface VirtualizedMessageListProps {
  messages: SDKUIMessage[];
  sessionId?: string;
  pendingToolCallId?: string;
  /** The response the session's stream is writing now. */
  writingMessageId?: string | null;
  /** A reply is on its way: a message is being sent, or the runtime works. */
  responseExpected?: boolean;
  /** What the session is doing to get a runtime, while it has none to answer with. */
  startingLabel?: string;
  shouldAutoFollow: boolean;
  firstItemIndex: number;
  hasMore: boolean;
  loadingMore: boolean;
  loadMoreError: string | null;
  scrollToBottomKey: string;
  onLoadOlder: () => Promise<void> | void;
}

export function VirtualizedMessageList({
  messages,
  sessionId,
  pendingToolCallId,
  writingMessageId,
  responseExpected,
  startingLabel,
  shouldAutoFollow,
  firstItemIndex,
  hasMore,
  loadingMore,
  loadMoreError,
  scrollToBottomKey,
  onLoadOlder,
}: VirtualizedMessageListProps) {
  const { t } = useTranslation();
  const virtuosoRef = useRef<VirtuosoHandle | null>(null);
  const scrollerRef = useRef<HTMLElement | null>(null);
  const scrollerCleanupRef = useRef<(() => void) | null>(null);
  const userRequestedOlderRef = useRef(false);
  const loadOlderStateRef = useRef({ hasMore, loadingMore, onLoadOlder });
  const [isAtBottom, setIsAtBottom] = useState(true);
  const followingRef = useRef(false);
  // The reader is at the end and has not asked to leave it. Content growing
  // under them does not count: the list reports "not at the bottom" for the
  // frames before it follows, and taking that as the reader leaving is what
  // lets every new block push the latest line out of view.
  const pinnedRef = useRef(true);
  const pinObserverRef = useRef<ResizeObserver | null>(null);
  // Folded work a reader has already opened, kept here rather than in the row:
  // the list virtualizes, so the row that opened it is torn down when it
  // scrolls away and rebuilt when it comes back.
  const detailsCache = useRef<ProcessDetailsCacheValue>(new Map());
  const summaryCache = useRef<ProcessSummaryCacheValue>(new Map());
  const processOpenCache = useRef(new Map<string, boolean>());
  const [totalHeight, setTotalHeight] = useState(0);

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
  const pageAnchor = useRef({ sourceIndex: firstItemIndex, index: firstItemIndex, key: items[0]?.key });
  if (pageAnchor.current.sourceIndex !== firstItemIndex) {
    const retained = items.findIndex((item) => item.key === pageAnchor.current.key);
    if (retained < 0) throw new Error('Conversation pagination lost its first displayed item');
    pageAnchor.current.index -= retained;
    pageAnchor.current.sourceIndex = firstItemIndex;
  }
  pageAnchor.current.key = items[0]?.key;
  const displayFirstItemIndex = pageAnchor.current.index;
  const itemContext = useMemo(() => ({
    pendingToolCallId,
    writingMessageId,
  }), [pendingToolCallId, writingMessageId]);

  loadOlderStateRef.current = { hasMore, loadingMore, onLoadOlder };
  followingRef.current = shouldAutoFollow && Boolean(writingMessageId || responseExpected);

  const lastItemIndex = displayFirstItemIndex + Math.max(0, items.length - 1);

  const requestOlder = useCallback(() => {
    const state = loadOlderStateRef.current;
    if (!state.hasMore || state.loadingMore || !userRequestedOlderRef.current) return;
    userRequestedOlderRef.current = false;
    void state.onLoadOlder();
  }, []);

  const bindScroller = useCallback((element: HTMLElement | Window | null) => {
    scrollerCleanupRef.current?.();
    scrollerCleanupRef.current = null;
    if (!(element instanceof HTMLElement)) return;
    scrollerRef.current = element;
    let previousTouchY: number | null = null;
    const markOlderIntent = () => {
      pinnedRef.current = false;
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
    // Home targets the first loaded item through Virtuoso so its measured
    // heights participate in the jump. Relative keys keep native scrolling.
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.target !== element) return;
      if (event.key === 'Home') {
        event.preventDefault();
        pinnedRef.current = false;
        clearOlderIntent();
        virtuosoRef.current?.scrollIntoView({
          index: 0,
          align: 'start',
          behavior: 'auto',
          done: () => {
            if (!element.isConnected) return;
            markOlderIntent();
            if (atOlderBoundary()) requestOlder();
          },
        });
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
      const fromEnd = element.scrollHeight - element.scrollTop - element.clientHeight;
      if (fromEnd < 2) {
        pinnedRef.current = true;
      } else if (followingRef.current && pinnedRef.current) {
        // The reader did not move: every gesture that leaves the end clears
        // the pin before its scroll arrives. This is the list scrolling to
        // where the end was a measurement ago, so it is put back before paint.
        element.scrollTop = element.scrollHeight;
      }
      if (atOlderBoundary()) requestOlder();
    };
    // Opening a block while the reader follows a running response grows the
    // list under them, which would otherwise leave them above the new content
    // and end the following. They asked to see more of what is being written,
    // so the view stays on its latest line.
    const onClick = (event: MouseEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      if (!target?.closest('[data-slot="collapsible-trigger"]')) return;
      if (!followingRef.current || !pinnedRef.current) return;
      const settle = () => element.scrollTo({ top: element.scrollHeight });
      window.requestAnimationFrame(() => {
        settle();
        window.requestAnimationFrame(settle);
      });
    };
    element.addEventListener('click', onClick, { capture: true });
    // Content that grows while the reader follows is pinned before it paints.
    // The list's own following lands a few frames after the growth, and for
    // those frames the newest line sits below the fold: every new card would
    // make the activity line drop out of view and come back.
    const pin = new ResizeObserver(() => {
      if (followingRef.current && pinnedRef.current) element.scrollTop = element.scrollHeight;
    });
    pinObserverRef.current = pin;
    element.addEventListener('pointerdown', onPointerDown);
    element.addEventListener('wheel', onWheel, { passive: true });
    element.addEventListener('keydown', onKeyDown);
    element.addEventListener('touchstart', onTouchStart, { passive: true });
    element.addEventListener('touchmove', onTouchMove, { passive: true });
    element.addEventListener('touchend', clearTouch, { passive: true });
    element.addEventListener('touchcancel', clearTouch, { passive: true });
    element.addEventListener('scroll', onScroll, { passive: true });
    scrollerCleanupRef.current = () => {
      element.removeEventListener('pointerdown', onPointerDown);
      element.removeEventListener('wheel', onWheel);
      element.removeEventListener('keydown', onKeyDown);
      element.removeEventListener('touchstart', onTouchStart);
      element.removeEventListener('touchmove', onTouchMove);
      element.removeEventListener('touchend', clearTouch);
      element.removeEventListener('touchcancel', clearTouch);
      element.removeEventListener('scroll', onScroll);
      element.removeEventListener('click', onClick, { capture: true });
      pin.disconnect();
      pinObserverRef.current = null;
    };
  }, [requestOlder]);

  useEffect(() => () => scrollerCleanupRef.current?.(), []);

  // The list's own elements are created with its first rows and replaced when
  // it remounts them, after the scroller was bound. Observing again is free for
  // an element already observed.
  useEffect(() => {
    const element = scrollerRef.current;
    const pin = pinObserverRef.current;
    if (!element || !pin) return;
    for (let node = element.firstElementChild; node; node = node.firstElementChild) {
      pin.observe(node);
      if (node.getAttribute('data-testid') === 'virtuoso-item-list') break;
    }
  });

  useEffect(() => {
    if (loadingMore) userRequestedOlderRef.current = false;
  }, [loadingMore]);

  // A first page of folded headers can be shorter than the viewport, and a
  // list that cannot scroll never reaches the boundary the gestures above
  // ask at. Ask for the next page directly until the list fills or history
  // runs out; a failed page stops it, so the retry stays the reader's.
  useEffect(() => {
    const element = scrollerRef.current;
    if (!element || totalHeight <= 0 || totalHeight > element.clientHeight) return;
    if (!hasMore || loadingMore || loadMoreError) return;
    void onLoadOlder();
  }, [totalHeight, hasMore, loadingMore, loadMoreError, onLoadOlder]);

  useEffect(() => {
    if (!scrollToBottomKey) return;
    const scroll = () => virtuosoRef.current?.scrollToIndex({ index: 'LAST', align: 'end', behavior: 'auto' });
    scroll();
    const frame = window.requestAnimationFrame(scroll);
    return () => window.cancelAnimationFrame(frame);
  }, [lastItemIndex, scrollToBottomKey]);

  const scrollToBottom = useCallback(() => {
    virtuosoRef.current?.scrollToIndex({
      index: 'LAST',
      align: 'end',
      behavior: 'auto',
    });
  }, [lastItemIndex]);

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
      <Virtuoso
        ref={virtuosoRef}
        data-testid="session-conversation"
        role="log"
        className="h-full"
        data={items}
        firstItemIndex={displayFirstItemIndex}
        initialTopMostItemIndex={{ index: 'LAST', align: 'end' }}
        alignToBottom
        skipAnimationFrameInResizeObserver
        increaseViewportBy={{ top: 600, bottom: 900 }}
        context={itemContext}
        totalListHeightChanged={setTotalHeight}
        atBottomStateChange={setIsAtBottom}
        followOutput={(atBottom) => (shouldAutoFollow && atBottom ? 'auto' : false)}
        startReached={requestOlder}
        scrollerRef={bindScroller}
        // Ids are stable from the moment a message is shown: a user message
        // is its client_message_id, a response its response id.
        computeItemKey={(_index, item) => item.key}
        itemContent={(index, item, context) => ('pending' in item ? (
          <div className="mx-auto w-full max-w-reading px-4 py-2" data-testid="response-pending-row">
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
            className="mx-auto w-full max-w-reading px-4 py-2"
            data-message-virtual-index={index}
            data-message-id={item.message.id}
            data-response-segment={item.responseSegment}
          >
            <MessageBubble
              message={item.message}
              sessionId={sessionId}
              pendingToolCallId={context.pendingToolCallId}
              isStreaming={item.message.role === 'assistant' && item.message.id === context.writingMessageId
                && item.responseSegment !== 'preceding'}
              includedInResponse={item.acceptedBy}
              responseSegment={item.responseSegment}
              replacesPending={continuesPending.current.has(item.key)}
            />
          </div>
        ))}
      />
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
          onClick={scrollToBottom}
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
