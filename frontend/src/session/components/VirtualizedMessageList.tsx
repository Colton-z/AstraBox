import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { UIMessage as SDKUIMessage } from 'ai';
import { ArrowDownIcon } from 'lucide-react';
import { Virtuoso, type VirtuosoHandle } from 'react-virtuoso';
import { Button } from '@/components/ui/button';
import { MessageBubble } from './MessageParts';
import {
  ProcessDetailsCache,
  ProcessSummaryCache,
  type ProcessDetailsCacheValue,
  type ProcessSummaryCacheValue,
} from './LazyProcessBlock';
import { conversationMessageKeys } from '../messageIdentity';

// How close to the top the scroller has to be for an upward gesture to ask
// for the older page. Exactly zero is a transient position: prepending a page
// compensates scrollTop the moment it lands, so a reader who keeps pulling up
// sits a little below the edge, never on it.
const OLDER_HISTORY_BOUNDARY_PX = 160;

interface VirtualizedMessageListProps {
  messages: SDKUIMessage[];
  sessionId?: string;
  pendingToolCallId?: string;
  activeTurnId?: string | null;
  isStreaming: boolean;
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
  activeTurnId,
  isStreaming,
  shouldAutoFollow,
  firstItemIndex,
  hasMore,
  loadingMore,
  loadMoreError,
  scrollToBottomKey,
  onLoadOlder,
}: VirtualizedMessageListProps) {
  const { t } = useTranslation();
  const messageKeys = conversationMessageKeys(messages);
  const virtuosoRef = useRef<VirtuosoHandle | null>(null);
  const scrollerRef = useRef<HTMLElement | null>(null);
  const scrollerCleanupRef = useRef<(() => void) | null>(null);
  const userRequestedOlderRef = useRef(false);
  const loadOlderStateRef = useRef({ hasMore, loadingMore, onLoadOlder });
  const [isAtBottom, setIsAtBottom] = useState(true);
  // Folded work a reader has already opened, kept here rather than in the row:
  // the list virtualizes, so the row that opened it is torn down when it
  // scrolls away and rebuilt when it comes back.
  const detailsCache = useRef<ProcessDetailsCacheValue>(new Map());
  const summaryCache = useRef<ProcessSummaryCacheValue>(new Map());
  const [totalHeight, setTotalHeight] = useState(0);

  const itemContext = useMemo(() => ({
    pendingToolCallId,
    activeTurnId,
    isStreaming,
    lastMessage: messages[messages.length - 1],
  }), [activeTurnId, isStreaming, messages, pendingToolCallId]);

  loadOlderStateRef.current = { hasMore, loadingMore, onLoadOlder };

  const lastItemIndex = firstItemIndex + Math.max(0, messages.length - 1);

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
    // The keyboard is the third way a reader moves through the transcript:
    // the scroller is in the tab order, and the browser scrolls a focused
    // scroll container on these keys before the scroll event that asks.
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.target !== element) return;
      if (
        event.key === 'ArrowUp' || event.key === 'PageUp' || event.key === 'Home'
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
    scrollerCleanupRef.current = () => {
      element.removeEventListener('pointerdown', onPointerDown);
      element.removeEventListener('wheel', onWheel);
      element.removeEventListener('keydown', onKeyDown);
      element.removeEventListener('touchstart', onTouchStart);
      element.removeEventListener('touchmove', onTouchMove);
      element.removeEventListener('touchend', clearTouch);
      element.removeEventListener('touchcancel', clearTouch);
      element.removeEventListener('scroll', onScroll);
    };
  }, [requestOlder]);

  useEffect(() => () => scrollerCleanupRef.current?.(), []);

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
      <Virtuoso
        ref={virtuosoRef}
        data-testid="session-conversation"
        role="log"
        className="h-full"
        data={messages}
        firstItemIndex={firstItemIndex}
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
        computeItemKey={(index) => messageKeys[index - firstItemIndex]}
        itemContent={(index, message, context) => (
          <div
            className="mx-auto w-full max-w-reading px-4 py-2"
            data-message-virtual-index={index}
            data-message-id={message.id}
          >
            <MessageBubble
              message={message}
              sessionId={sessionId}
              pendingToolCallId={context.pendingToolCallId}
              activeTurnId={context.activeTurnId}
              isStreaming={
                context.isStreaming
                && message === context.lastMessage
                && message.role === 'assistant'
              }
            />
          </div>
        )}
      />
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
