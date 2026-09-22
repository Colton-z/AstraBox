import { useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Download, FileText, Lock } from 'lucide-react';

import { VirtualizedMessageList } from '@/session/components/VirtualizedMessageList';
import { useFirstPageMessages } from '@/session/hooks/useFirstPageMessages';
import { TranscriptAccessContext, sharedTranscriptAccess } from '@/session/TranscriptAccess';
import { prepareInitialMessages } from '@/session/prepareInitialMessages';
import {
  buildSharedFileDownloadUrl,
  getSharedSession,
  listSharedFiles,
} from '@/api';
import type { SessionFileEntry, SessionRecord } from '@/types';

// Read-only conversation view reached through /share/:token. The token is the
// viewer credential; owner authentication is required only to create or revoke
// it. Chat rendering is reused without send or lifecycle controls.
export default function SharePage() {
  const { token = '' } = useParams();
  const access = useMemo(() => sharedTranscriptAccess(token), [token]);
  return (
    <TranscriptAccessContext.Provider value={access}>
      <SharedConversation key={token} token={token} />
    </TranscriptAccessContext.Provider>
  );
}

function SharedConversation({ token }: { token: string }) {
  const { t } = useTranslation();
  const history = useFirstPageMessages(token);
  const [session, setSession] = useState<(SessionRecord & { share_allow_download?: boolean }) | null>(null);
  const [files, setFiles] = useState<SessionFileEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let alive = true;
    void (async () => {
      setLoading(true);
      try {
        const sess = await getSharedSession(token);
        if (!alive) return;
        setSession(sess);
        // Files are best-effort: only when the share allows download.
        if (sess.share_allow_download) {
          try {
            const fl = await listSharedFiles(token);
            if (alive) setFiles(fl.entries || []);
          } catch {
            /* file listing optional; ignore */
          }
        }
        setError('');
      } catch (e) {
        if (alive) setError((e as Error).message);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [token]);

  const messages = useMemo(() => session ? prepareInitialMessages(
    history.durableRecords, history.overlay, session,
  ) : [], [history.durableRecords, history.overlay, session]);

  const title = useMemo(
    () => String(session?.title || '').trim() || t('misc:share.title_fallback'),
    [session, t],
  );

  if (loading || (!history.loadedOnce && history.loading)) {
    return <div className="flex h-screen items-center justify-center text-sm text-muted-foreground">{t('common:loading')}</div>;
  }
  if (error || history.error || !session) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-3 px-6 text-center">
        <Lock className="size-8 text-muted-foreground" />
        <div className="text-base font-medium">{t('misc:share.cannot_open')}</div>
        <p className="max-w-sm text-sm text-muted-foreground">
          {error || history.error || t('misc:share.link_invalid')}
        </p>
      </div>
    );
  }

  const downloadable = Boolean(session.share_allow_download);

  return (
    <div className="flex h-screen flex-col bg-background text-foreground">
      <header className="flex shrink-0 items-center justify-between gap-4 border-b px-6 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h1 className="t-h2-tight truncate text-15 tracking-[-0.01em]">{title}</h1>
            <span className="rounded bg-muted px-1.5 py-0.5 text-10 text-muted-foreground">{t('misc:share.readonly_badge')}</span>
          </div>
          <p className="text-xs text-muted-foreground">{t('misc:share.readonly_notice')}</p>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          {messages.length === 0 ? (
            <div className="py-20 text-center text-sm text-muted-foreground">{t('misc:share.no_messages')}</div>
          ) : (
            <VirtualizedMessageList
              messages={messages}
              isStreaming={false}
              shouldAutoFollow={false}
              firstItemIndex={100_000 - messages.length}
              hasMore={history.hasMore}
              loadingMore={history.loadingMore}
              loadMoreError={history.loadMoreError}
              scrollToBottomKey=""
              onLoadOlder={history.loadOlder}
            />
          )}
        </main>

        {downloadable && files.length > 0 && (
          <aside className="hidden w-72 shrink-0 overflow-y-auto border-l p-4 lg:block">
            <div className="mb-3 flex items-center gap-2 text-sm font-medium">
              <FileText className="size-4" />
              {t('common:files')}
            </div>
            <ul className="space-y-1">
              {files
                .filter((f) => f.kind === 'file')
                .map((f) => (
                  <li key={f.path}>
                    <a
                      href={buildSharedFileDownloadUrl(token, f.path)}
                      className="flex items-center justify-between gap-2 rounded-md px-2 py-1.5 text-xs hover:bg-muted"
                    >
                      <span className="truncate">{f.name}</span>
                      <Download className="size-3.5 shrink-0 text-muted-foreground" />
                    </a>
                  </li>
                ))}
            </ul>
          </aside>
        )}
      </div>
    </div>
  );
}
