import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { SearchXIcon } from 'lucide-react';
import { isNotFoundOrPermissionError, isTransientSessionLoadError } from '../utils/chatHelpers';
import { Button, buttonVariants } from '@/components/ui/button';
import { EmptyState, ErrorNote } from '@/components/shell';
import { SidebarTrigger } from '@/components/ui/sidebar';
import { isTransientHistoryLoadError } from '../sessionPageLoading';

// Presentational early-return states for SessionPage's bootstrap phase: the
// outer component keeps the control flow (which state to show, and when) and
// the JSX bodies live here.

/**
 * The frame a conversation stands in before it has arrived.
 *
 * The heading is the record's type — not the session's title, which no session
 * carries until it has loaded, and not a progress word, which says what the
 * page is doing rather than which page it is. The state goes in the body under
 * it, the same division `manage/SessionDetailPage.tsx` makes for the same
 * record (§1).
 *
 * It has to be the heading and not the trail: `App.tsx` gives a session route
 * no topbar, so until `SessionHeader` renders there is nothing else on screen
 * that names the surface.
 */
function SessionBootstrapFrame({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  return (
    <section className="flex h-full flex-col text-sm">
      {/* A session that has not loaded still stands where the session view
          does, and that view replaces `AppShell`'s top bar. Without this row
          the reader of a failed load has no control over the rail and no way
          back except the browser's own. */}
      <div className="flex shrink-0 items-center gap-2.5 border-b border-border px-4 py-2.5">
        <SidebarTrigger className="-ml-1 shrink-0 text-muted-foreground hover:text-foreground" />
        <div className="h-4 w-px shrink-0 bg-border" />
        <h1 className="t-h2-tight text-15">{t('common:session')}</h1>
      </div>
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3">
        {children}
      </div>
    </section>
  );
}

export function SessionLoadingState() {
  const { t } = useTranslation();
  return (
    <SessionBootstrapFrame>
      <p className="text-muted-foreground">{t('chat:page.loading_session')}</p>
    </SessionBootstrapFrame>
  );
}

export function SessionUnavailableState({
  loadError,
  onRetry,
}: {
  loadError: string;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  if (loadError && !isNotFoundOrPermissionError(loadError)) {
    return (
      <SessionBootstrapFrame>
        <p className="text-muted-foreground">{isTransientSessionLoadError(loadError) ? t('chat:page.session_detail_transient') : t('chat:page.load_session_failed')}</p>
        <ErrorNote>{loadError}</ErrorNote>
        <div className="flex items-center gap-2">
          <Button variant="secondary" size="sm" onClick={onRetry}>{t('chat:page.retry_now')}</Button>
          {/* Home is a destination, so it borrows the button's shape through
              `buttonVariants` instead of being a `Button`:
              `@base-ui/react/button` imposes button semantics on whatever it
              renders, which its own documentation rules out for an `<a>`.
              `SessionBootstrapStates.test.tsx` asks for `role="link"`. */}
          <Link to="/" className={buttonVariants({ variant: 'outline', size: 'sm' })}>{t('chat:page.back_home')}</Link>
        </div>
      </SessionBootstrapFrame>
    );
  }
  // A refusal and an empty answer are one state for the reader: the session is
  // either missing or not theirs to see, and neither has anything to retry.
  return (
    <SessionBootstrapFrame>
      <EmptyState
        icon={<SearchXIcon className="size-5" />}
        title={t('chat:page.not_found')}
        action={<Link to="/" className={buttonVariants({ variant: 'outline', size: 'sm' })}>{t('chat:page.back_home')}</Link>}
      />
    </SessionBootstrapFrame>
  );
}

export function SessionHistoryBlockingState({ historyError }: { historyError: string | null }) {
  const { t } = useTranslation();
  return (
    <SessionBootstrapFrame>
      <p className="text-muted-foreground">{isTransientHistoryLoadError(historyError) ? t('chat:page.messages_transient') : t('chat:page.syncing_messages')}</p>
      {historyError && isTransientHistoryLoadError(historyError) && (
        <ErrorNote>{historyError}</ErrorNote>
      )}
    </SessionBootstrapFrame>
  );
}

export function SessionHistoryErrorState({
  historyError,
  onRetry,
}: {
  historyError: string | null;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  return (
    <SessionBootstrapFrame>
      <p className="text-muted-foreground">{t('chat:page.messages_load_failed')}</p>
      <ErrorNote>{historyError}</ErrorNote>
      <div className="flex items-center gap-2">
        <Button variant="secondary" size="sm" onClick={onRetry}>{t('chat:page.retry_now')}</Button>
        <Link to="/" className={buttonVariants({ variant: 'outline', size: 'sm' })}>{t('chat:page.back_home')}</Link>
      </div>
    </SessionBootstrapFrame>
  );
}
