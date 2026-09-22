import { useCallback, useMemo } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { OutboxItem } from '../../types';
import type { QueuedMessageItem } from '../../utils/messages';
import type { TurnInputImage } from '../composerAttachments';

// Every Agent uses the platform's durable input outbox. Accepted rows remain
// visible until the adapter reports their exact consumption boundary.
export function useMessageQueue({
  outbox,
  setOutbox,
  canSendNow,
  canQueueMessage,
  sendClientMessageNow,
}: {
  outbox: OutboxItem[];
  setOutbox: Dispatch<SetStateAction<OutboxItem[]>>;
  canSendNow: boolean;
  canQueueMessage: boolean;
  sendClientMessageNow: (clientMessageId: string, text: string, images?: TurnInputImage[]) => void;
}) {
  const displayedQueue = useMemo<QueuedMessageItem[]>(() => outbox.map((item) => ({
      id: item.client_message_id,
      content: item.text,
      status: item.status === 'accepted' ? 'queued' : item.status,
      ...(item.failure_reason ? { error: item.failure_reason } : {}),
      source: item.status === 'failed'
        ? 'authoritative-delivery-failed'
        : item.command_id
          ? 'native'
          : 'native-submit',
    })), [outbox]);
  const canRetryQueued = canSendNow || canQueueMessage;

  const removeQueueItem = useCallback((id: string) => {
    setOutbox((current) => current.filter((item) => (
      item.client_message_id !== id
      || item.status !== 'failed'
    )));
  }, [setOutbox]);

  const retryQueueItem = useCallback((id: string) => {
    const item = outbox.find((candidate) => candidate.client_message_id === id);
    if (!item || (!item.text && !item.images?.length)) return;
    setOutbox((current) => current.filter((candidate) => candidate !== item));
    sendClientMessageNow(id, item.text, item.images);
  }, [outbox, sendClientMessageNow, setOutbox]);

  return {
    displayedQueue,
    canRetryQueued,
    removeQueueItem,
    retryQueueItem,
  };
}
