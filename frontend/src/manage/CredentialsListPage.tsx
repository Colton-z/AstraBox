import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { KeyRound, Plus, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { ErrorNote } from '@/components/shell';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { PageShell } from '@/components/shell';
import {
  createAdminVault,
  getAdminVaultCatalog,
} from '@/api';
import type {
  CredentialBinding,
  VaultCredentialSummary,
  VaultSummary,
} from '@/types';

import {
  ConsoleEmptyState,
  ConsoleErrorState,
  ConsolePageHeader,
  ConsoleSearch,
  ConsoleTable,
  ConsoleTableNote,
  ConsoleTableSkeleton,
  ConsoleToolbar,
  NameCell,
  StatusPill,
  formatDateTime,
  type ConsoleColumn,
} from './console';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';

type CredentialType = 'static_bearer' | 'mcp_oauth' | 'environment_variable';
type TargetType = 'agent' | 'assistant';

type Assignment = {
  target_type: TargetType;
  target_id: string;
  target_name: string;
  binding: CredentialBinding;
};

function credentialTarget(credential: VaultCredentialSummary): string {
  return credential.auth.url || credential.auth.mcp_server_url || credential.auth.secret_name || '—';
}

export default function CredentialsListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [vaults, setVaults] = useState<VaultSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [drawerError, setDrawerError] = useState('');
  const [search, setSearch] = useState('');
  const [busy, setBusy] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [vaultName, setVaultName] = useState('');



  // True after any successful read, an empty catalog included (see keepsLastRead).
  const loaded = useRef(false);

  const refresh = useCallback(async (context?: ReloadContext) => {
    const background = context?.background === true;
    if (!background) setLoading(true);
    try {
      const catalog = await getAdminVaultCatalog();
      setVaults(catalog.vaults);
      setError('');
      loaded.current = true;
    } catch (reason) {
      if (keepsLastRead(reason, context, loaded.current)) return;
      setError((reason as Error).message);
    } finally {
      if (!background) setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  useKeepCurrent(refresh);

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return vaults;
    return vaults.filter((vault) => (
      vault.display_name.toLowerCase().includes(query)
      || vault.vault_id.toLowerCase().includes(query)
      || (vault.credentials || []).some((credential) => (
        String(credential.display_name || '').toLowerCase().includes(query)
        || credentialTarget(credential).toLowerCase().includes(query)
      ))
    ));
  }, [search, vaults]);



  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setDrawerError('');
    try {
      await action();
    } catch (reason) {
      setDrawerError((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const saveVault = () => void run(async () => {
    const name = vaultName.trim();
    if (!name) throw new Error(t('manage:credentials.name_required'));
    const created = await createAdminVault(name);
    setCreateOpen(false);
    setVaultName('');
    await refresh();
    navigate(`/manage/credentials/${created.vault_id}`);
  });








  const columns: ConsoleColumn<VaultSummary>[] = [
    {
      key: 'name',
      intent: 'name',
      header: t('manage:credentials.name'),
      cell: (vault) => (
        <span data-testid="credential-vault-row">
          <NameCell name={vault.display_name} sub={vault.vault_id} />
        </span>
      ),
    },
    {
      key: 'credentials',
      intent: 'compact',
      header: t('manage:credentials.credentials'),
      cell: (vault) => String((vault.credentials || []).filter((item) => !item.archived_at).length),
    },
    {
      key: 'status',
      intent: 'status',
      header: t('common:status'),
      cell: (vault) => vault.archived_at
        ? <StatusPill tone="idle">{t('manage:credentials.archived')}</StatusPill>
        : <StatusPill tone="done">{t('manage:credentials.active')}</StatusPill>,
    },
    {
      key: 'updated',
      intent: 'timestamp',
      header: t('common:updated_at'),
      cell: (vault) => formatDateTime(vault.updated_at || vault.created_at),
    },
  ];



  return (
    <div data-testid="credential-vault-page" className="contents">
      <PageShell
    >
        <ConsolePageHeader
          title={t('manage:credentials.title')}
          meta={t('manage:credentials.meta', { count: vaults.length })}
          description={t('manage:credentials.description')}
          actions={(
            <div className="flex items-center gap-2">
              <Button variant="outline" size="icon" onClick={() => void refresh()} disabled={loading} aria-label={t('common:refresh')}>
                <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
              </Button>
              <Button data-testid="credential-vault-create" onClick={() => setCreateOpen(true)}>
                <Plus className="size-4" />
                {t('manage:credentials.create')}
              </Button>
            </div>
          )}
        />

        <div className="flex flex-1 flex-col gap-4">
          <Alert className="border-teal/25 bg-teal/5">
            <KeyRound className="size-4" />
            <AlertTitle>{t('manage:credentials.admin_only_title')}</AlertTitle>
            <AlertDescription>{t('manage:credentials.admin_only_description')}</AlertDescription>
          </Alert>

          <ConsoleToolbar>
            <ConsoleSearch value={search} onChange={setSearch} total={vaults.length} placeholder={t('manage:credentials.search')} />
          </ConsoleToolbar>

          <ConsoleTable
            columns={columns}
            rows={loading || error ? [] : filtered}
            rowKey={(vault) => vault.vault_id}
            onRowClick={(vault) => navigate(`/manage/credentials/${vault.vault_id}`)}
            empty={loading ? (
              <ConsoleTableSkeleton columns={columns} />
            ) : error ? (
              <ConsoleErrorState title={t('manage:credentials.load_failed')} detail={error} onRetry={() => void refresh()} />
            ) : vaults.length === 0 ? (
              <ConsoleEmptyState
                title={t('manage:credentials.empty_title')}
                hint={t('manage:credentials.empty_hint')}
                action={<Button size="sm" onClick={() => setCreateOpen(true)}><Plus className="size-4" />{t('manage:credentials.create')}</Button>}
              />
            ) : (
              <ConsoleTableNote>{t('manage:credentials.no_match', { query: search })}</ConsoleTableNote>
            )}
          />


          <Dialog open={createOpen} onOpenChange={setCreateOpen}>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>{t('manage:credentials.create')}</DialogTitle>
                <DialogDescription>{t('manage:credentials.create_help')}</DialogDescription>
              </DialogHeader>
              <Label className="flex-col items-stretch gap-1.5 text-sm leading-5 font-normal">
                <span>{t('manage:credentials.name')}</span>
                <Input data-testid="credential-vault-name" value={vaultName} onChange={(event) => setVaultName(event.target.value)} autoFocus />
              </Label>
              {drawerError && <ErrorNote>{drawerError}</ErrorNote>}
              <DialogFooter>
                <Button variant="outline" onClick={() => setCreateOpen(false)}>{t('common:cancel')}</Button>
                <Button data-testid="credential-vault-save" onClick={saveVault} disabled={busy}>{t('common:create')}</Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>

      </PageShell>
    </div>
  );
}
