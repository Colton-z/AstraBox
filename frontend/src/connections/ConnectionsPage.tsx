import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import {
  authorizeConnector,
  disconnectConnector,
  listConnectors,
  offerConnectorInAssistants,
} from '@/api';
import { StatusPill } from '@/components/AstraConsole';
import { AppTopbar, PageShell } from '@/components/shell';
import { ErrorNote } from '@/components/shell/ErrorNote';
import { Button } from '@/components/ui/button';
import type { Connector } from '@/types';

/**
 * The signed-in person's own connections to work apps.
 *
 * A connection is this person's consent at the vendor. An Assistant they own
 * uses it, and so does an Agent that offers the app when they start a
 * conversation with it. The vendor returns the browser here after a consent,
 * and an app just connected is turned on in the person's Assistants.
 */
export function ConnectionsPage() {
  const { t } = useTranslation();
  const [params] = useSearchParams();
  const [connectors, setConnectors] = useState<Connector[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      setConnectors(await listConnectors());
      setError('');
    } catch (failure) {
      setError((failure as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async (key: string, action: () => Promise<unknown>) => {
    setBusy(key);
    setError('');
    try {
      await action();
      await load();
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const justConnected = connectors?.find((connector) => connector.connector_id === params.get('connected'));
  const available = (connectors ?? []).filter((connector) => connector.available);

  // Once per return from the vendor: the app is offered by the person's
  // Assistants, except those where they turned it off.
  const [adoptedIn, setAdoptedIn] = useState<number | null>(null);
  const adopted = useRef<string | null>(null);
  useEffect(() => {
    if (justConnected?.status !== 'connected' || adopted.current === justConnected.connector_id) return;
    adopted.current = justConnected.connector_id;
    offerConnectorInAssistants(justConnected.connector_id).then(
      (answer) => setAdoptedIn(answer.assistant_ids.length),
      (failure) => setError((failure as Error).message),
    );
  }, [justConnected]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <AppTopbar
        crumbs={[{ label: t('misc:connections.title') }]}
        trailLabel={t('shell:breadcrumb_navigation')}
      />
      <PageShell measure="wide">
        <h1 className="text-2xl font-medium">{t('misc:connections.title')}</h1>
        <p className="t-label text-muted-foreground">{t('misc:connections.intro')}</p>
        {justConnected?.status === 'connected' && (
          <StatusPill tone="done">{t('misc:connections.connected_now', { name: justConnected.name })}</StatusPill>
        )}
        {justConnected?.status === 'connected' && adoptedIn !== null && adoptedIn > 0 && (
          <p className="t-label text-muted-foreground" data-testid="connection-adopted">
            {t('misc:connections.adopted', { count: adoptedIn, name: justConnected.name })}
          </p>
        )}
        {error && <ErrorNote>{error}</ErrorNote>}
        {connectors !== null && available.length === 0 && (
          <p className="t-label text-muted-foreground">{t('misc:connections.none')}</p>
        )}
        <ul className="flex flex-col divide-y" data-testid="connections">
          {available.map((connector) => {
            const connected = connector.status === 'connected';
            const expired = connector.status === 'needs_reauth';
            return (
              <li
                key={connector.connector_id}
                className="flex flex-wrap items-center gap-3 py-3"
                data-connector-id={connector.connector_id}
              >
                <div className="t-label min-w-0 flex-1 font-medium">{connector.name}</div>
                {connected && (
                  <StatusPill tone="done">{t('manage:connectors.status_connected')}</StatusPill>
                )}
                {expired && (
                  <StatusPill tone="approval">{t('manage:connectors.status_needs_reauth')}</StatusPill>
                )}
                {connected ? (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={busy !== null}
                    onClick={() =>
                      void run(connector.connector_id, () => disconnectConnector(connector.connector_id))
                    }
                  >
                    {t('manage:connectors.disconnect')}
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    disabled={busy !== null}
                    onClick={() =>
                      void run(connector.connector_id, async () => {
                        const { authorize_url } = await authorizeConnector(connector.connector_id);
                        window.location.assign(authorize_url);
                      })
                    }
                  >
                    {busy === connector.connector_id
                      ? t('manage:connectors.connecting')
                      : expired
                        ? t('manage:connectors.reconnect')
                        : t('manage:connectors.connect')}
                  </Button>
                )}
              </li>
            );
          })}
        </ul>
      </PageShell>
    </div>
  );
}
