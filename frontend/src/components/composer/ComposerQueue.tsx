import { useTranslation } from 'react-i18next';
import { RefreshCw, Trash2 } from 'lucide-react';
import type { QueuedMessageItem } from '../../utils/messages';
import { Button } from '@/components/ui/button';
import {
  Queue,
  QueueItem,
  QueueItemAction,
  QueueItemActions,
  QueueItemContent,
  QueueItemDescription,
  QueueList,
} from '@/components/ai-elements/queue';
import { ErrorNote } from '@/components/shell';

export function ComposerQueue({
  queuedMessages,
  onRemoveQueuedMessage,
  onRetryQueuedMessage,
  canRetryQueuedMessages,
}: {
  queuedMessages: QueuedMessageItem[];
  onRemoveQueuedMessage: (id: string) => void;
  onRetryQueuedMessage: (id: string) => void;
  canRetryQueuedMessages: boolean;
}) {
  const { t } = useTranslation();
  if (queuedMessages.length === 0) {
    return null;
  }

  return (
    // The queue is the top of the composer's stack, so it takes the ground,
    // radius and (absent) shadow of the input directly below it rather than the
    // kit's free-standing card.
    <Queue data-testid="composer-queue" className="mb-2 bg-card shadow-none">
      {/* The kit gives QueueList `mt-2 -mb-1`, the gap a QueueSection header
          leaves above its list. This queue has no header, so that margin has
          nothing above it: it stacks onto the card's own `pt-2` for 20px over
          the first row against 8px under the last. Cancelling both leaves the
          card's `py-2` and the row's `py-1` even on either edge. */}
      <QueueList className="my-0">
        {queuedMessages.map((item, index) => (
          <QueueItem
            key={item.id}
            // Every row reserves the border the failed one paints, so a send
            // that fails does not move the rows around it by a pixel.
            className={`flex-row items-start gap-3 border ${item.status === 'failed' ? 'border-destructive/30' : 'border-transparent'}`}
          >
            <div className="min-w-0 flex-1">
              <QueueItemDescription className="ml-0 flex items-center gap-2">
                <span className="font-medium">{t('misc:composer.queue_n', { index: index + 1 })}</span>
                <span className={item.status === 'failed' ? 'text-destructive' : item.status === 'sending' ? 'text-teal-fg' : ''}>
                  {item.status === 'sending' ? t('misc:composer.queue_sending') : item.status === 'failed' ? t('misc:composer.queue_failed') : t('misc:composer.queue_waiting')}
                </span>
              </QueueItemDescription>
              <QueueItemContent className="mt-1 text-foreground">{item.content}</QueueItemContent>
              {item.status === 'failed' && item.error && (
                <ErrorNote className="mt-1">{item.error}</ErrorNote>
              )}
            </div>
            <QueueItemActions className="shrink-0 items-center">
              {item.status === 'failed' && (
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={!canRetryQueuedMessages}
                  onClick={() => onRetryQueuedMessage(item.id)}
                  aria-label={t('misc:composer.retry_queued')}
                  title={canRetryQueuedMessages ? t('misc:composer.retry_queued') : t('misc:composer.retry_disabled')}
                >
                  <RefreshCw />
                  <span>{t('misc:composer.retry')}</span>
                </Button>
              )}
              {(
                item.status === 'failed'
                || item.source === 'authoritative-delivery-failed'
                || item.source === 'local'
              ) && (
                <QueueItemAction
                  // Always painted: the kit reveals a row action on hover, and a
                  // hover is nothing a touch reader has. Dropping a message is
                  // the only way out of a failed send.
                  className="opacity-100"
                  onClick={() => onRemoveQueuedMessage(item.id)}
                  aria-label={t('misc:composer.remove_queued')}
                  title={t('misc:composer.remove_queued')}
                >
                  <Trash2 />
                </QueueItemAction>
              )}
            </QueueItemActions>
          </QueueItem>
        ))}
      </QueueList>
    </Queue>
  );
}
