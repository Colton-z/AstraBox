import { useTranslation } from 'react-i18next';
import type { UIMessage as SDKUIMessage } from 'ai';
import { EmptyState } from '@/components/shell';
import { AstraMark } from '../../components/AstraConsole';
import { VirtualizedMessageList } from './VirtualizedMessageList';
import { AssistantWelcome } from '../../assistant/AssistantWelcome';

// The scrollable message list and empty state for the main pane.
export function SessionConversationView({
  sessionId, messages, isWorking, pendingToolCallId,
  writingMessageId, hasPendingInteraction, pendingConversationScrollKey,
  firstItemIndex, hasMore, loadingMore, loadMoreError, onLoadOlder, assistantId,
}: {
  sessionId: string;
  messages: SDKUIMessage[];
  isWorking: boolean;
  pendingToolCallId: string | undefined;
  writingMessageId: string | null;
  hasPendingInteraction: boolean;
  pendingConversationScrollKey: string;
  firstItemIndex: number;
  hasMore: boolean;
  loadingMore: boolean;
  loadMoreError: string | null;
  onLoadOlder: () => Promise<void>;
  /** Set for an Assistant's conversation, whose empty state says what it can use. */
  assistantId?: string | null;
}) {
  const { t } = useTranslation();
  if (messages.length === 0 && !isWorking) {
    if (assistantId) return <AssistantWelcome assistantId={assistantId} />;
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
      writingMessageId={writingMessageId}
      shouldAutoFollow
      firstItemIndex={firstItemIndex}
      hasMore={hasMore}
      loadingMore={loadingMore}
      loadMoreError={loadMoreError}
      scrollToBottomKey={hasPendingInteraction ? pendingConversationScrollKey : ''}
      onLoadOlder={onLoadOlder}
    />
  );
}
