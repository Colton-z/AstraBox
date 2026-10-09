import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  configureConnector,
  listAdminConnectors,
  publishConnector,
  removeConnector,
  verifyConnector,
} from '@/api';
import { StatusPill } from '@/components/AstraConsole';
import { ErrorNote } from '@/components/shell';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import type { Connector } from '@/types';

import {
  ConsoleCard,
  ConsoleErrorState,
  ConsoleRecordLoading,
  ConsoleRecordPage,
  useRecordCrumb,
} from './console';

/**
 * Setting up each connector with the connector hub.
 *
 * An app is set up once: register it with the hub (with this deployment's
 * OAuth app where the vendor issues clients only to registered apps), connect
 * the administrator's own account so the hub can list the app's tools, and
 * publish them. The redirect URI shown is the one to register with the vendor.
 */
export default function ConnectorsPage() {
  const { t } = useTranslation();
  const [connectors, setConnectors] = useState<Connector[] | null>(null);
  const [loadError, setLoadError] = useState('');
  const [drafts, setDrafts] = useState<Record<string, { id: string; secret: string }>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  useRecordCrumb(t('manage:connectors.admin_title'));

  const load = useCallback(async () => {
    try {
      setConnectors(await listAdminConnectors());
      setLoadError('');
    } catch (failure) {
      setLoadError((failure as Error).message);
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

  if (connectors === null) {
    return (
      <ConsoleRecordPage title={t('manage:connectors.admin_title')}>
        {loadError ? (
          <ConsoleErrorState detail={loadError} onRetry={() => void load()} />
        ) : (
          <ConsoleRecordLoading />
        )}
      </ConsoleRecordPage>
    );
  }

  const redirectUri = connectors.find((connector) => connector.redirect_uri)?.redirect_uri ?? '';

  return (
    <ConsoleRecordPage
      title={t('manage:connectors.admin_title')}
      lede={t('manage:connectors.admin_lede', { callback: redirectUri })}
    >
      {error && <ErrorNote>{error}</ErrorNote>}
      {connectors.map((connector) => {
        const id = connector.connector_id;
        const draft = drafts[id] ?? { id: '', secret: '' };
        const setDraft = (next: Partial<typeof draft>) =>
          setDrafts((previous) => ({ ...previous, [id]: { ...draft, ...next } }));
        const operator = connector.registration === 'operator';
        return (
          <ConsoleCard
            key={id}
            title={connector.name}
            intro={operator ? t('manage:connectors.operator_intro') : t('manage:connectors.dynamic_intro')}
            actions={
              !connector.deployed ? (
                <StatusPill tone="idle">{t('manage:connectors.not_deployed')}</StatusPill>
              ) : connector.published ? (
                <StatusPill tone="done">{t('manage:connectors.published')}</StatusPill>
              ) : connector.configured ? (
                <StatusPill tone="approval">{t('manage:connectors.unpublished')}</StatusPill>
              ) : (
                <StatusPill tone="idle">{t('manage:connectors.not_configured')}</StatusPill>
              )
            }
          >
            {connector.docs_url && (
              <a className="t-link t-label-sm" href={connector.docs_url} target="_blank" rel="noreferrer">
                {t('manage:connectors.vendor_guide')}
              </a>
            )}
            {connector.deployed && (
              <div className="flex flex-col gap-2" data-connector-id={id}>
                {operator && (
                  <>
                    <Input
                      placeholder={t('manage:connectors.client_id')}
                      aria-label={t('manage:connectors.client_id')}
                      value={draft.id}
                      onChange={(event) => setDraft({ id: event.target.value })}
                    />
                    <Input
                      type="password"
                      placeholder={t('manage:connectors.client_secret')}
                      aria-label={t('manage:connectors.client_secret')}
                      value={draft.secret}
                      onChange={(event) => setDraft({ secret: event.target.value })}
                    />
                  </>
                )}
                <div className="flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    disabled={busy !== null || (operator && !draft.id.trim())}
                    onClick={() =>
                      void run(id, async () => {
                        await configureConnector(
                          id,
                          operator
                            ? { client_id: draft.id.trim(), client_secret: draft.secret.trim() || undefined }
                            : {},
                        );
                        setDraft({ id: '', secret: '' });
                      })
                    }
                  >
                    {t('manage:connectors.set_up')}
                  </Button>
                  {connector.configured && (
                    <>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy !== null}
                        onClick={() =>
                          void run(id, async () => {
                            const { authorize_url } = await verifyConnector(id);
                            window.location.assign(authorize_url);
                          })
                        }
                      >
                        {t('manage:connectors.verify')}
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy !== null}
                        onClick={() => void run(id, () => publishConnector(id))}
                      >
                        {t('manage:connectors.publish')}
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy !== null}
                        onClick={() => void run(id, () => removeConnector(id))}
                      >
                        {t('manage:connectors.remove')}
                      </Button>
                    </>
                  )}
                </div>
              </div>
            )}
          </ConsoleCard>
        );
      })}
    </ConsoleRecordPage>
  );
}
