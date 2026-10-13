import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ExternalLink, RefreshCw } from 'lucide-react';
import { ApiError, openSessionBrowser } from '@/api';
import { ErrorNote } from '@/components/shell';
import { Button } from '@/components/ui/button';

export default function SessionBrowserPanel({ sessionId, enabled, disabledMessage }: {
  sessionId: string;
  enabled: boolean;
  disabledMessage?: string;
}) {
  const { t } = useTranslation();
  const [revision, setRevision] = useState(0);
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setUrl(null);
    setError(null);
    setLoading(enabled);
    if (enabled) {
      void openSessionBrowser(sessionId, controller.signal).then((result) => {
        if (!controller.signal.aborted) setUrl(result.url);
      }).catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(cause instanceof ApiError && cause.code === 'BROWSER_TENANCY_UNSUPPORTED'
          ? t('chat:browser.unsupported')
          : cause instanceof Error ? cause.message : t('chat:browser.failed'));
      }).finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    }
    return () => controller.abort();
  }, [sessionId, enabled, revision, t]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1 border-b border-border p-2">
        <Button variant="ghost" size="sm" disabled={!enabled || loading} onClick={() => setRevision((value) => value + 1)}>
          <RefreshCw className="size-3.5" />{t('chat:browser.reconnect')}
        </Button>
        {url && <Button variant="ghost" size="sm" onClick={() => window.open(url, '_blank', 'noopener,noreferrer')}>
          <ExternalLink className="size-3.5" />{t('chat:browser.open_tab')}
        </Button>}
      </div>
      {!enabled ? <ErrorNote>{disabledMessage ?? t('chat:browser.failed')}</ErrorNote>
        : error ? <ErrorNote>{error}</ErrorNote>
          : loading ? <p className="p-4 text-sm text-muted-foreground">{t('chat:browser.loading')}</p>
            : url && <iframe
              key={url}
              title={t('chat:panel.tab.browser')}
              src={url}
              className="min-h-0 w-full flex-1 border-0"
              sandbox="allow-scripts allow-same-origin"
              referrerPolicy="no-referrer"
              allowFullScreen
            />}
    </div>
  );
}
