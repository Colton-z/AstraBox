import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  authorizeConnector,
  disconnectConnector,
  listAgentConnectors,
  listAssistantConnectors,
  listConnectors,
  setAgentConnector,
  setAssistantConnector,
} from '@/api';
import { StatusPill } from '@/components/AstraConsole';
import { ErrorNote } from '@/components/shell';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import type { Connector } from '@/types';

import { ConsoleCard } from './console';

/**
 * The work apps one Agent or Assistant offers, and the viewer's own connections.
 *
 * Connecting is a person's consent at the vendor, kept for that person; offering
 * a connector is per Agent or Assistant. An Assistant uses its owner's
 * connections, so its switch needs the owner connected. An Agent's callers each
 * use their own, so its author offers a connector without connecting.
 */
export function ConnectorsCard({ kind, targetId }: { kind: 'agent' | 'assistant'; targetId: string }) {
  const { t } = useTranslation();
  const [connectors, setConnectors] = useState<Connector[] | null>(null);
  const [enabled, setEnabled] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      const [all, active] = await Promise.all([
        listConnectors(),
        kind === 'agent' ? listAgentConnectors(targetId) : listAssistantConnectors(targetId),
      ]);
      setConnectors(all);
      setEnabled(new Set(active));
      setError('');
    } catch (failure) {
      setError((failure as Error).message);
    }
  }, [kind, targetId]);

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

  const connect = (connectorId: string) =>
    run(connectorId, async () => {
      const { authorize_url } = await authorizeConnector(connectorId);
      window.location.assign(authorize_url);
    });

  return (
    <ConsoleCard
      title={t('manage:connectors.card_title')}
      intro={t(kind === 'agent' ? 'manage:connectors.agent_card_intro' : 'manage:connectors.card_intro')}
      note={t(kind === 'agent' ? 'manage:connectors.agent_card_note' : 'manage:connectors.card_note')}
    >
      {error && <ErrorNote>{error}</ErrorNote>}
      <ul className="flex flex-col divide-y" data-testid={`${kind}-connectors`}>
        {(connectors ?? []).map((connector) => {
          const connected = connector.status === 'connected';
          const expired = connector.status === 'needs_reauth';
          const using = enabled.has(connector.connector_id);
          return (
            <li
              key={connector.connector_id}
              className="flex flex-wrap items-center gap-3 py-3"
              data-connector-id={connector.connector_id}
            >
              <div className="min-w-0 flex-1">
                <div className="t-label font-medium">{connector.name}</div>
                {!connector.available && (
                  <div className="t-label-sm text-muted-foreground">
                    {t('manage:connectors.not_set_up')}
                  </div>
                )}
              </div>
              {connected && (
                <StatusPill tone="done">{t('manage:connectors.status_connected')}</StatusPill>
              )}
              {expired && (
                <StatusPill tone="approval">{t('manage:connectors.status_needs_reauth')}</StatusPill>
              )}
              {(kind === 'agent' ? connector.available : connected) && (
                <label className="t-label-sm inline-flex items-center gap-2">
                  <Switch
                    checked={using}
                    disabled={busy !== null}
                    onCheckedChange={(next: boolean) =>
                      void run(connector.connector_id, () =>
                        kind === 'agent'
                          ? setAgentConnector(targetId, connector.connector_id, next)
                          : setAssistantConnector(targetId, connector.connector_id, next),
                      )
                    }
                    aria-label={t(
                      kind === 'agent' ? 'manage:connectors.offer_in_agent' : 'manage:connectors.use_in_assistant',
                      { name: connector.name },
                    )}
                  />
                  {t(kind === 'agent' ? 'manage:connectors.offer_here' : 'manage:connectors.use_here')}
                </label>
              )}
              {connected ? (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={busy !== null}
                  onClick={() =>
                    void run(connector.connector_id, () =>
                      disconnectConnector(connector.connector_id),
                    )
                  }
                >
                  {t('manage:connectors.disconnect')}
                </Button>
              ) : (
                <Button
                  size="sm"
                  disabled={busy !== null || !connector.available}
                  onClick={() => void connect(connector.connector_id)}
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
    </ConsoleCard>
  );
}
