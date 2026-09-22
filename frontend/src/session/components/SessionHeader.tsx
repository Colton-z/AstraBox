import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { SidebarTrigger } from '@/components/ui/sidebar';
import { Ellipsis } from '@/components/shell';
import { RunId, StatusPill, type PillTone } from '../../components/AstraConsole';
import { localizeDisplayText } from '../../utils/format';
import type { SessionRecord } from '../../types';
import { ShareDialog } from './ShareDialog';

// Top page-chrome header: run id / model badge / title / status pill, the
// runtime-unavailable + last-error hints beneath it, and the terminate / end
// conversation / recover actions.
export function SessionHeader({
  session,
  headerIsLive,
  headerTone,
  headerStatusLabel,
  headerRunState,
  showRuntimeUnavailableBanner,
  showSessionLastError,
  isAssistantConversation,
  isAgentChat,
  isTerminated,
  lifecycleState,
  terminateLoading,
  handleEndConversation,
  handleTerminate,
  showRecoverButton,
  handleRecover,
}: {
  session: SessionRecord;
  headerIsLive: boolean;
  headerTone: PillTone;
  headerStatusLabel: string;
  headerRunState: string;
  showRuntimeUnavailableBanner: boolean;
  showSessionLastError: boolean;
  isAssistantConversation: boolean;
  isAgentChat: boolean;
  isTerminated: boolean;
  lifecycleState: string;
  terminateLoading: boolean;
  handleEndConversation: () => void;
  handleTerminate: () => void;
  showRecoverButton: boolean;
  handleRecover: () => void;
}) {
  const { t } = useTranslation();
  // This view renders no top bar, so the document title is set from the one
  // name it does show; without it every conversation shares one browser tab
  // label and one history entry.
  const pageName = session.title || session.template_name || session.session_id.slice(0, 8);
  useEffect(() => {
    document.title = pageName ? `${pageName} · AstraBox` : 'AstraBox';
  }, [pageName]);
  return (
    <header className="flex items-center justify-between gap-3 border-b border-border px-4 py-2.5 shrink-0">
      {/* The rail's only control lives in `AppShell`'s top bar, and this
          view replaces that bar rather than sitting under it — so without
          this the sidebar cannot be collapsed from the one screen a
          conversation is read on. */}
      <div className="flex min-w-0 items-center gap-2.5">
        <SidebarTrigger className="-ml-1 shrink-0 text-muted-foreground hover:text-foreground" />
        <div className="h-4 w-px shrink-0 bg-border" />
        <div className="min-w-0">
          <div className="mb-1 flex items-center gap-2.5">
            <RunId id={session.session_id} live={headerIsLive} />
            {session.model_name && (
              <span className="t-mono rounded-md border border-border px-1.5 py-0.5 text-10 text-muted-foreground">
                {session.model_name}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 min-w-0">
            <h1 className="t-h2-tight truncate text-15 tracking-[-0.01em]">{session.title || session.template_name || t('chat:page.session_title_fallback', { id: session.session_id.slice(0, 8) })}</h1>
            <StatusPill tone={headerTone} state={headerRunState}>{headerStatusLabel}</StatusPill>
          </div>
          {showRuntimeUnavailableBanner && <p className="mt-1 text-11 text-citrine-fg">{t('chat:status.runtime_disconnected')}</p>}
          {/* The deployment's own words, marked as quoted: `last_error` carries
              raw exception text and wire spellings straight from the API, and
              the rules about how this product writes do not reach text it did
              not write (§8). */}
          {showSessionLastError && (
            <p data-slot="verbatim" className="mt-1 max-w-md text-xs text-destructive">
              <Ellipsis>{localizeDisplayText(String(session.last_error ?? ''))}</Ellipsis>
            </p>
          )}
        </div>
      </div>
      <div className="flex items-center gap-1.5 shrink-0">
        <ShareDialog sessionId={session.session_id} />
        {/* One size for every action in this strip, taken from the band rather
            than written per button: `sm` is what the share trigger beside them
            and the retry banner under them already stand at (§9). */}
        {isAssistantConversation ? (
          <Button variant="outline" size="sm" onClick={handleEndConversation} disabled={isTerminated || lifecycleState === 'deleted' || terminateLoading}>
            {terminateLoading ? t('chat:header.ending') : t('chat:header.end_conversation')}
          </Button>
        ) : !isAgentChat && (
          <Button variant="outline" size="sm" onClick={handleTerminate} disabled={isTerminated || lifecycleState === 'deleted' || terminateLoading}>
            {terminateLoading ? t('chat:header.terminating') : t('chat:header.terminate')}
          </Button>
        )}
        {showRecoverButton && <Button size="sm" className="hover:bg-astra-2" onClick={handleRecover}>{t('chat:header.recover_session')}</Button>}
      </div>
    </header>
  );
}
