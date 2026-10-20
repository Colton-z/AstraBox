import { createContext, useContext } from 'react';
import { useTranslation } from 'react-i18next';
import type { UIMessage } from 'ai';
import { RefreshCw, Trash2 } from 'lucide-react';
import { ErrorNote } from '@/components/shell';
import { Button } from '@/components/ui/button';
import type { UnsentMessage } from '../hooks/useSessionChat';
import { inputFailure, inputMetadata } from '../sessionMessages';
import { UserActionNotice } from './SessionUserAction';

/**
 * What the owner's page knows about its own messages beyond the transcript:
 * the ones still on their way, the ones the session holds back while a
 * response runs, and what to do with one that did not go through. A read-only
 * transcript provides none of it.
 */
export interface SessionSendState {
  unsent: ReadonlyMap<string, UnsentMessage>;
  /** Inputs the platform holds: accepted, not yet with the runtime. */
  heldInputIds: ReadonlySet<string>;
  canSend: boolean;
  retryUnsent: (clientMessageId: string) => void;
  removeUnsent: (clientMessageId: string) => void;
  sendAgain: (message: UIMessage) => void;
}

export const SessionSendContext = createContext<SessionSendState | null>(null);

/**
 * The line under a user message that has not simply arrived: still sending,
 * waiting behind the response that runs, or not delivered — with the way to
 * send it again.
 */
export function UserMessageStatus({ message, includedInResponse }: { message: UIMessage; includedInResponse?: string }) {
  const { t } = useTranslation();
  const state = useContext(SessionSendContext);
  const included = includedInResponse ? (
    <p data-testid="input-response-association" data-response-id={includedInResponse} className="text-xs text-muted-foreground">
      {t('chat:message.included_in_response')}
    </p>
  ) : null;
  if (!state) return included;
  const unsent = state.unsent.get(message.id);
  if (unsent?.status === 'sending') {
    return <p role="status" className="text-xs text-teal-fg">{t('misc:composer.queue_sending')}</p>;
  }
  if (unsent?.status === 'failed') {
    return (
      <div className="flex w-full max-w-[85%] flex-col gap-2" data-testid="user-message-failure">
        {unsent.userAction
          ? <UserActionNotice action={unsent.userAction} />
          : <ErrorNote>{unsent.error || t('misc:composer.queue_failed')}</ErrorNote>}
        <div className="flex items-center justify-end gap-1">
          <Button
            variant="ghost"
            size="sm"
            disabled={!state.canSend}
            onClick={() => state.retryUnsent(message.id)}
            title={state.canSend ? undefined : t('misc:composer.retry_disabled')}
          >
            <RefreshCw />
            <span>{t(unsent.resendAsNew ? 'misc:composer.send_again' : 'misc:composer.retry')}</span>
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => state.removeUnsent(message.id)}
            aria-label={t('misc:composer.remove_queued')}
            title={t('misc:composer.remove_queued')}
          >
            <Trash2 />
          </Button>
        </div>
      </div>
    );
  }
  const failure = inputFailure(message);
  if (failure) {
    return (
      <div className="flex w-full max-w-[85%] flex-col gap-2" data-testid="user-message-failure">
        {failure.user_action
          ? <UserActionNotice action={failure.user_action} />
          : <ErrorNote>{failure.message || t('misc:composer.queue_failed')}</ErrorNote>}
        <div className="flex items-center justify-end">
          <Button
            variant="ghost"
            size="sm"
            disabled={!state.canSend}
            onClick={() => state.sendAgain(message)}
            title={state.canSend ? undefined : t('misc:composer.retry_disabled')}
          >
            <RefreshCw />
            <span>{t('misc:composer.send_again')}</span>
          </Button>
        </div>
      </div>
    );
  }
  const inputId = inputMetadata(message).input_id;
  if (!included && inputId && state.heldInputIds.has(inputId)) {
    return <p role="status" className="text-xs text-muted-foreground">{t('misc:composer.queue_waiting')}</p>;
  }
  return included;
}
