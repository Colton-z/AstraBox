import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { ErrorNote, PageShell } from '@/components/shell';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { FieldError } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemGroup,
  ItemTitle,
} from '@/components/ui/item';
import { CodeBlock, CodeBlockCopyButton } from '@/components/ai-elements/code-block';
import {
  MODULE_BASE,
  deleteModelAccount,
  getModelAccountTeamPool,
  listModelAccounts,
  updateModelAccount,
} from '@/api';
import type { ModelAccount } from '@/types';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';

import {
  ConsoleCard,
  ConsoleDangerButton,
  ConsoleEmptyState,
  ConsoleErrorState,
  ConsolePageHeader,
  ConsoleRecordLoading,
  StatusPill,
} from './console';
import { formatDateTime } from './agentConfig';
import { ManageUsageLink } from './ModelAccountLinks';

/** The longest label the server stores. */
const LABEL_MAX_CHARS = 200;

/**
 * The command that links an account, pointed at this deployment.
 *
 * It runs on the reader's own computer: OpenAI sends the browser back to
 * `http://127.0.0.1:<port>/auth/callback`, an address only a program on the
 * computer running that browser can receive, so no page served from here can
 * finish a sign-in. With `accountId` it signs in to that account again.
 */
function loginCommand(accountId?: string): string {
  const endpoint = `${window.location.origin}${MODULE_BASE}`;
  const account = accountId ? ` --account ${accountId}` : '';
  return `astrabox model-account login chatgpt${account} --endpoint ${endpoint}`;
}

type Renaming = { id: string; label: string; error: string; saving: boolean };

/**
 * The signed-in user's ChatGPT accounts: the ones whose plans their
 * conversations on a ChatGPT plan RuntimeProfile spend.
 *
 * The page lists, renames, chooses the default, and removes. It does not link:
 * linking is the CLI's, for the reason {@link loginCommand} gives, so the page
 * hands the reader that command instead of a button that could not work.
 *
 * While the deployment's team pool is on, each account can be shared with the
 * team, and the page says when a teammate's conversation last spent it and
 * whether a reported usage limit holds it back. With the pool off nothing can
 * be shared, so the control is not shown.
 */
export default function ModelAccountsPage() {
  const { t } = useTranslation();
  const [accounts, setAccounts] = useState<ModelAccount[] | null>(null);
  const [poolEnabled, setPoolEnabled] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [actionError, setActionError] = useState('');
  // OpenAI did not confirm that a removed account's session ended.
  const [unrevoked, setUnrevoked] = useState('');
  const [busy, setBusy] = useState('');
  const [renaming, setRenaming] = useState<Renaming | null>(null);
  const loaded = useRef(false);

  const load = useCallback(async (context?: ReloadContext) => {
    const background = context?.background === true;
    if (!background) setRefreshing(true);
    try {
      const [listed, pool] = await Promise.all([listModelAccounts(), getModelAccountTeamPool()]);
      setAccounts(listed);
      setPoolEnabled(pool.enabled);
      loaded.current = true;
      setLoadError('');
    } catch (e) {
      if (keepsLastRead(e, context, loaded.current)) return;
      if (loaded.current) setActionError((e as Error).message);
      else setLoadError((e as Error).message);
    } finally {
      if (!background) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useKeepCurrent(load);

  const makeDefault = async (account: ModelAccount) => {
    setActionError('');
    setBusy(account.account_id);
    try {
      await updateModelAccount(account.account_id, { is_default: true });
      await load();
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const share = async (account: ModelAccount, shared: boolean) => {
    setActionError('');
    setBusy(account.account_id);
    try {
      const saved = await updateModelAccount(account.account_id, { shared });
      setAccounts((prev) =>
        prev && prev.map((item) => (item.account_id === saved.account_id ? saved : item)),
      );
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const saveRename = async () => {
    if (!renaming || !accounts) return;
    const label = renaming.label.trim();
    const current = accounts.find((account) => account.account_id === renaming.id);
    if (!current || !label) return;
    if (label === current.label) {
      setRenaming(null);
      return;
    }
    setRenaming({ ...renaming, saving: true, error: '' });
    try {
      const saved = await updateModelAccount(renaming.id, { label });
      setAccounts((prev) =>
        prev && prev.map((account) => (account.account_id === saved.account_id ? saved : account)),
      );
      setRenaming(null);
    } catch (e) {
      setRenaming((prev) => prev && { ...prev, saving: false, error: (e as Error).message });
    }
  };

  const remove = async (account: ModelAccount) => {
    setActionError('');
    setUnrevoked('');
    setBusy(account.account_id);
    try {
      const removed = await deleteModelAccount(account.account_id);
      if (removed.revocation_confirmed === false) {
        setUnrevoked(t('manage:model_accounts.revocation_unconfirmed', { account: account.label }));
      }
      await load();
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const header = (
    <ConsolePageHeader
      title={t('manage:model_accounts.title')}
      meta={accounts ? t('manage:model_accounts.meta', { count: accounts.length }) : undefined}
      description={t('manage:model_accounts.description')}
      actions={
        <Button
          variant="outline"
          size="icon"
          onClick={() => void load()}
          disabled={refreshing}
          aria-label={t('common:refresh')}
        >
          <RefreshCw className={refreshing ? 'size-4 animate-spin' : 'size-4'} />
        </Button>
      }
    />
  );

  if (!accounts) {
    return (
      <PageShell>
        {header}
        <div className="flex flex-1 flex-col gap-4">
          {loadError ? (
            <ConsoleErrorState
              title={t('manage:model_accounts.error_title')}
              detail={loadError}
              onRetry={() => void load()}
            />
          ) : (
            <ConsoleRecordLoading />
          )}
        </div>
      </PageShell>
    );
  }

  return (
    <PageShell>
      {header}
      <div className="flex flex-1 flex-col gap-4">
        {actionError && <ErrorNote>{actionError}</ErrorNote>}
        {unrevoked && <ErrorNote>{unrevoked}</ErrorNote>}
        <ConsoleCard
          title={t('manage:model_accounts.list_title')}
          intro={t('manage:model_accounts.list_intro')}
          actions={<ManageUsageLink />}
          note={poolEnabled ? t('manage:model_accounts.pool_note') : undefined}
        >
          {accounts.length === 0 ? (
            <ConsoleEmptyState
              title={t('manage:model_accounts.empty_title')}
              hint={t('manage:model_accounts.empty_hint')}
            />
          ) : (
            <ItemGroup className="gap-0">
              {accounts.map((account) => {
                const editing = renaming?.id === account.account_id ? renaming : null;
                const inputId = `model-account-${account.account_id}-label`;
                const signInRequired = account.status === 'sign_in_required';
                const working = busy === account.account_id;
                return (
                  <Item
                    key={account.account_id}
                    role="listitem"
                    data-testid="model-account-row"
                    className="items-start rounded-none border-0 border-b border-border px-0 first:pt-0 last:border-b-0 last:pb-0"
                  >
                    {editing ? (
                      <form
                        className="flex min-w-0 flex-1 flex-wrap items-start gap-2"
                        onSubmit={(event) => {
                          event.preventDefault();
                          void saveRename();
                        }}
                      >
                        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                          <Input
                            id={inputId}
                            className="h-7"
                            autoFocus
                            maxLength={LABEL_MAX_CHARS}
                            value={editing.label}
                            aria-label={t('manage:model_accounts.rename_label', { account: account.label })}
                            aria-invalid={editing.error ? true : undefined}
                            aria-describedby={editing.error ? `${inputId}-error` : undefined}
                            readOnly={editing.saving}
                            onChange={(event) =>
                              setRenaming({ ...editing, label: event.target.value, error: '' })
                            }
                            onKeyDown={(event) => {
                              if (event.key === 'Escape') setRenaming(null);
                            }}
                          />
                          {editing.error && (
                            <FieldError id={`${inputId}-error`} className="text-11 leading-snug">
                              {editing.error}
                            </FieldError>
                          )}
                        </div>
                        <div className="flex items-center gap-2">
                          <Button
                            type="button"
                            size="sm"
                            variant="ghost"
                            disabled={editing.saving}
                            onClick={() => setRenaming(null)}
                          >
                            {t('common:cancel')}
                          </Button>
                          <Button
                            type="submit"
                            size="sm"
                            disabled={editing.saving || !editing.label.trim()}
                          >
                            {t('common:save')}
                          </Button>
                        </div>
                      </form>
                    ) : (
                      <>
                        <ItemContent className="min-w-0 gap-2">
                          <div className="flex min-w-0 flex-col gap-0.5">
                            <div className="flex min-w-0 flex-wrap items-center gap-2">
                              <ItemTitle className="t-label block truncate text-13">{account.label}</ItemTitle>
                              {account.is_default && (
                                <Badge variant="secondary">{t('manage:model_accounts.default_badge')}</Badge>
                              )}
                              {poolEnabled && account.shared && (
                                <Badge variant="secondary">{t('manage:model_accounts.shared_badge')}</Badge>
                              )}
                              {signInRequired ? (
                                <StatusPill tone="approval">{t('manage:model_accounts.status_sign_in_required')}</StatusPill>
                              ) : (
                                <StatusPill tone="done">{t('manage:model_accounts.status_active')}</StatusPill>
                              )}
                            </div>
                            <ItemDescription className="t-copy-sm text-xs">
                              {[
                                account.email && account.email !== account.label ? account.email : '',
                                t('manage:model_accounts.linked_at', { at: formatDateTime(account.created_at) }),
                              ].filter(Boolean).join(' · ')}
                            </ItemDescription>
                            {poolEnabled && (account.shared || account.usage_limited_until) && (
                              <ItemDescription className="t-copy-sm text-xs" data-testid="model-account-pool-state">
                                {[
                                  account.shared
                                    ? account.pool_last_used_at
                                      ? t('manage:model_accounts.pool_last_used', {
                                          at: formatDateTime(account.pool_last_used_at),
                                        })
                                      : t('manage:model_accounts.pool_never_used')
                                    : '',
                                  account.usage_limited_until
                                    ? t('manage:model_accounts.usage_limited', {
                                        at: formatDateTime(account.usage_limited_until),
                                      })
                                    : '',
                                ].filter(Boolean).join(' · ')}
                              </ItemDescription>
                            )}
                          </div>
                          {poolEnabled && (
                            <label className="t-label-sm inline-flex items-center gap-2">
                              <Switch
                                checked={account.shared}
                                disabled={working}
                                onCheckedChange={(next: boolean) => void share(account, next)}
                                aria-label={t('manage:model_accounts.share_button', { account: account.label })}
                              />
                              {t('manage:model_accounts.share')}
                            </label>
                          )}
                          {signInRequired && (
                            <div className="flex flex-col gap-2">
                              <p className="t-copy-sm text-muted-foreground">
                                {t('manage:model_accounts.sign_in_again_intro', {
                                  reason: account.last_error_code ?? '—',
                                })}
                              </p>
                              <CodeBlock code={loginCommand(account.account_id)} language="bash">
                                <CodeBlockCopyButton aria-label={t('common:copy')} />
                              </CodeBlock>
                            </div>
                          )}
                        </ItemContent>
                        <ItemActions>
                          {!account.is_default && (
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={working}
                              onClick={() => void makeDefault(account)}
                            >
                              {t('manage:model_accounts.make_default')}
                            </Button>
                          )}
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={working}
                            aria-label={t('manage:model_accounts.rename_button', { account: account.label })}
                            onClick={() =>
                              setRenaming({ id: account.account_id, label: account.label, error: '', saving: false })
                            }
                          >
                            {t('manage:model_accounts.rename')}
                          </Button>
                          <ConsoleDangerButton
                            size="sm"
                            disabled={working}
                            aria-label={t('manage:model_accounts.remove_button', { account: account.label })}
                            confirm={{
                              title: t('manage:model_accounts.remove_title', { account: account.label }),
                              description: [
                                t('manage:model_accounts.remove_description'),
                                account.is_default ? t('manage:model_accounts.remove_default') : '',
                              ].filter(Boolean).join(' '),
                              action: t('manage:model_accounts.remove_action'),
                              onConfirm: () => void remove(account),
                            }}
                          >
                            {t('manage:model_accounts.remove')}
                          </ConsoleDangerButton>
                        </ItemActions>
                      </>
                    )}
                  </Item>
                );
              })}
            </ItemGroup>
          )}
        </ConsoleCard>
        <ConsoleCard
          title={t('manage:model_accounts.link_title')}
          intro={t('manage:model_accounts.link_intro')}
          note={t('manage:model_accounts.link_note')}
        >
          <CodeBlock code={loginCommand()} language="bash">
            <CodeBlockCopyButton aria-label={t('common:copy')} />
          </CodeBlock>
        </ConsoleCard>
      </div>
    </PageShell>
  );
}
