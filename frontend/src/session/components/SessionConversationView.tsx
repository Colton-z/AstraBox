import { useTranslation } from 'react-i18next';
import type { UIMessage as SDKUIMessage } from 'ai';
import { EmptyState } from '@/components/shell';
import { AstraMark } from '../../components/AstraConsole';
import { VirtualizedMessageList } from './VirtualizedMessageList';

// The scrollable message list and empty state for the main pane.
export function SessionConversationView({
  sessionId, messages, displayedQueueLength, isStreaming, isSubmitted, pendingToolCallId,
  activeTurnId, hasPendingInteraction, pendingConversationScrollKey,
  firstItemIndex, hasMore, loadingMore, loadMoreError, onLoadOlder,
}: {
  sessionId: string;
  messages: SDKUIMessage[];
  displayedQueueLength: number;
  isStreaming: boolean;
  isSubmitted: boolean;
  pendingToolCallId: string | undefined;
  activeTurnId: string | null;
  hasPendingInteraction: boolean;
  pendingConversationScrollKey: string;
  firstItemIndex: number;
  hasMore: boolean;
  loadingMore: boolean;
  loadMoreError: string | null;
  onLoadOlder: () => Promise<void>;
}) {
  const { t } = useTranslation();
  if (messages.length === 0 && displayedQueueLength === 0 && !isStreaming && !isSubmitted) {
    return (
      <div className="relative flex-1 min-h-0" role="log">
        <EmptyState
          icon={<AstraMark size={20} />}
          title={t('chat:empty.start_conversation')}
          hint={t('chat:empty.start_hint')}
        >
          <div className="bg-starfield pointer-events-none absolute inset-0 opacity-50" aria-hidden />
        </EmptyState>
      </div>
    );
  }
  return (
    <VirtualizedMessageList
      messages={messages}
      sessionId={sessionId}
      pendingToolCallId={pendingToolCallId}
      activeTurnId={activeTurnId}
      isStreaming={isStreaming}
      shouldAutoFollow={isStreaming || isSubmitted || hasPendingInteraction}
      firstItemIndex={firstItemIndex}
      hasMore={hasMore}
      loadingMore={loadingMore}
      loadMoreError={loadMoreError}
      scrollToBottomKey={hasPendingInteraction ? pendingConversationScrollKey : ''}
      onLoadOlder={onLoadOlder}
    />
  );
}
