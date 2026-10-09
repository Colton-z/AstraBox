import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Plug } from 'lucide-react';

import { authorizeConnector, listConnectors } from '@/api';
import { useArtifactNavigation } from '@/artifacts/navigation';
import { StatusPill } from '@/components/AstraConsole';
import { ErrorNote } from '@/components/shell';
import { Button } from '@/components/ui/button';

/**
 * The card an agent shows when it asks the person to connect a work app.
 *
 * The agent decides when to ask; the person signs in at the vendor in a new
 * tab. The card reads the viewer's own connection, again whenever the tab
 * regains focus, so it says Connected once they come back. A shared transcript
 * shows only what was asked: its reader is not the person asked.
 */
export function ConnectionRequestCard({ connectorId, name }: { connectorId: string; name: string }) {
  const { t } = useTranslation();
  const { sharedTranscript } = useArtifactNavigation();
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const read = useCallback(async () => {
    try {
      const row = (await listConnectors()).find((connector) => connector.connector_id === connectorId);
      // Only an app the deployment offers now can be connected from here.
      setStatus(row?.available ? String(row.status) : 'unavailable');
    } catch (failure) {
      setError((failure as Error).message);
    }
  }, [connectorId]);

  useEffect(() => {
    if (sharedTranscript) return undefined;
    void read();
    const onFocus = () => void read();
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [read, sharedTranscript]);

  const connect = async () => {
    // Opened in the click itself: a tab opened after an await is a pop-up.
    const tab = window.open('', '_blank');
    if (!tab) {
      setError(t('misc:connections.popup_blocked'));
      return;
    }
    setBusy(true);
    setError('');
    try {
      tab.location.href = (await authorizeConnector(connectorId)).authorize_url;
    } catch (failure) {
      tab.close();
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const connected = status === 'connected';
  return (
    <div
      className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-card p-3"
      data-testid="connection-request-card"
      data-connector-id={connectorId}
    >
      <Plug className="size-5 shrink-0 text-muted-foreground" />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="truncate text-sm font-medium">{t('misc:connections.card_title', { name })}</span>
        <span className="text-xs text-muted-foreground">
          {sharedTranscript
            ? t('misc:connections.card_shared', { name })
            : connected
              ? t('misc:connections.connected_now', { name })
              : status === 'unavailable'
                ? t('misc:connections.card_unavailable', { name })
                : t('misc:connections.card_waiting', { name })}
        </span>
        {error && <ErrorNote className="text-xs">{error}</ErrorNote>}
      </div>
      {!sharedTranscript && connected && (
        <StatusPill tone="done">{t('manage:connectors.status_connected')}</StatusPill>
      )}
      {!sharedTranscript && status !== null && !connected && status !== 'unavailable' && (
        <Button size="sm" disabled={busy} onClick={() => void connect()}>
          {busy
            ? t('manage:connectors.connecting')
            : status === 'needs_reauth'
              ? t('manage:connectors.reconnect')
              : t('manage:connectors.connect')}
        </Button>
      )}
    </div>
  );
}
