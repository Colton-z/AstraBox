import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import type { TFunction } from 'i18next';
import { Archive, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { listAssistants } from '@/assistant/api';
import type { AssistantRecord } from '@/assistant/types';
import { StatusPill } from '@/components/AstraConsole';
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
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemGroup,
  ItemTitle,
} from '@/components/ui/item';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  archiveAdminVault,
  archiveAdminVaultCredential,
  createAdminVaultCredential,
  deleteAdminVault,
  deleteAdminVaultCredential,
  getAdminVaultCatalog,
  getAgentCredentialBinding,
  getAssistantCredentialBinding,
  listAdminVaultBindings,
  listAgents,
  setAgentCredentialBinding,
  setAssistantCredentialBinding,
} from '@/api';
import type {
  AgentConfig,
  CredentialDeliveryOverview,
  VaultBindingHolder,
  VaultCredentialSummary,
  VaultSummary,
} from '@/types';

import {
  ConsoleCard,
  ConsoleDangerButton,
  ConsoleEmptyState,
  ConsoleErrorState,
  ConsoleFact,
  ConsoleFactRail,
  ConsoleRecordLoading,
  ConsoleRecordPage,
  formatDateTime,
  useRecordCrumb,
} from './console';
import { credentialDeliveryLabel, credentialTypeLabel } from './credentialPresentation';

type CredentialType =
  | 'static_bearer'
  | 'mcp_oauth'
  | 'mcp_static_header'
  | 'http_basic'
  | 'environment_variable';
type TargetType = 'agent' | 'assistant';

type Assignment = VaultBindingHolder;

function credentialExpired(expiresAt: string): boolean {
  const at = Date.parse(expiresAt);
  return Number.isFinite(at) && at <= Date.now();
}

function credentialTarget(credential: VaultCredentialSummary): string {
  return credential.auth.url || credential.auth.mcp_server_url || credential.auth.secret_name || '—';
}

/**
 * What a credential is called here: its own name, or the kind of thing it is.
 *
 * One function because it is read twice — the row's heading, and the question
 * about deleting that row — and those two have to be the same string for the
 * question to be about the record the reader pressed.
 */
function credentialLabel(credential: VaultCredentialSummary, t: TFunction): string {
  return credential.display_name || credentialTypeLabel(credential.auth.type, t);
}

/**
 * One credential vault, on its own page.
 *
 * A vault is not a form: it is a container of two lists — the credentials it
 * holds and the agents and assistants bound to it — each row of which has its
 * own actions. Reading one is a job of its own, so it gets a page rather than a
 * panel beside the list (§3 of docs/frontend-design.md); a label/value grid
 * would render each of those lists as if it were a single fact.
 */
export default function CredentialVaultPage() {
  const { vaultId = '' } = useParams();
  const navigate = useNavigate();
  const { t } = useTranslation();

  const [vault, setVault] = useState<VaultSummary | null>(null);
  const [delivery, setDelivery] = useState<CredentialDeliveryOverview | null>(null);
  const [agents, setAgents] = useState<AgentConfig[]>([]);
  const [assistants, setAssistants] = useState<AssistantRecord[]>([]);
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [actionError, setActionError] = useState('');
  const [busy, setBusy] = useState(false);

  const [credentialOpen, setCredentialOpen] = useState(false);
  const [credentialType, setCredentialType] = useState<CredentialType>('static_bearer');
  const [credentialName, setCredentialName] = useState('');
  const [credentialTargetValue, setCredentialTargetValue] = useState('');
  const [credentialHeaderName, setCredentialHeaderName] = useState('');
  const [credentialUsername, setCredentialUsername] = useState('');
  const [credentialSecret, setCredentialSecret] = useState('');
  const [allowedHosts, setAllowedHosts] = useState('');
  const [expiresAt, setExpiresAt] = useState('');

  const [bindingOpen, setBindingOpen] = useState(false);
  const [targetType, setTargetType] = useState<TargetType>('agent');
  const [targetId, setTargetId] = useState('');

  useRecordCrumb(vault ? vault.display_name : t('common:credential_vault'));

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [catalog, nextAgents, nextAssistants, nextAssignments] = await Promise.all([
        getAdminVaultCatalog(),
        listAgents(),
        listAssistants(),
        listAdminVaultBindings(vaultId),
      ]);
      const found = catalog.vaults.find((v) => v.vault_id === vaultId) || null;
      setVault(found);
      setDelivery(catalog.credential_delivery);
      setAgents(nextAgents);
      setAssistants(nextAssistants);
      setAssignments(nextAssignments);
      setError(found ? '' : t('manage:credentials.not_found', { id: vaultId }));
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setLoading(false);
    }
  }, [vaultId, t]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setActionError('');
    try {
      await action();
    } catch (reason) {
      setActionError((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const saveCredential = () =>
    vault &&
    void run(async () => {
      const target = credentialTargetValue.trim();
      const secret = credentialType === 'http_basic' ? credentialSecret : credentialSecret.trim();
      if (!target || !secret) throw new Error(t('manage:credentials.credential_required'));
      let auth: Record<string, unknown>;
      if (credentialType === 'http_basic') {
        const username = credentialUsername.trim();
        if (!username) throw new Error(t('manage:credentials.username_required'));
        auth = { type: credentialType, url: target, username, password: secret };
      } else if (credentialType === 'environment_variable') {
        const hosts = allowedHosts.split(',').map((host) => host.trim()).filter(Boolean);
        if (hosts.length === 0) throw new Error(t('manage:credentials.host_required'));
        auth = {
          type: credentialType,
          secret_name: target,
          secret_value: secret,
          networking: { type: 'limited', allowed_hosts: hosts },
          injection_location: { header: true, body: true },
        };
      } else if (credentialType === 'mcp_oauth') {
        auth = {
          type: credentialType,
          mcp_server_url: target,
          access_token: secret,
          ...(expiresAt.trim() ? { expires_at: expiresAt.trim() } : {}),
        };
      } else if (credentialType === 'mcp_static_header') {
        const headerName = credentialHeaderName.trim();
        if (!headerName) throw new Error(t('manage:credentials.header_required'));
        auth = {
          type: credentialType,
          mcp_server_url: target,
          header_name: headerName,
          value: secret,
        };
      } else {
        auth = { type: credentialType, mcp_server_url: target, token: secret };
      }
      await createAdminVaultCredential(vault.vault_id, {
        display_name: credentialName.trim() || undefined,
        auth,
      });
      setCredentialOpen(false);
      setCredentialName('');
      setCredentialTargetValue('');
      setCredentialHeaderName('');
      setCredentialUsername('');
      setCredentialSecret('');
      setAllowedHosts('');
      setExpiresAt('');
      await refresh();
    });

  const bind = () =>
    vault &&
    targetId &&
    void run(async () => {
      const current =
        targetType === 'agent'
          ? await getAgentCredentialBinding(targetId)
          : await getAssistantCredentialBinding(targetId);
      const next = [...current.vault_ids.filter((id) => id !== vault.vault_id), vault.vault_id];
      if (targetType === 'agent') await setAgentCredentialBinding(targetId, next);
      else await setAssistantCredentialBinding(targetId, next);
      setBindingOpen(false);
      setTargetId('');
      await refresh();
    });

  const unbind = (assignment: Assignment) =>
    vault &&
    void run(async () => {
      const next = assignment.vault_ids.filter((id) => id !== vault.vault_id);
      if (assignment.target_type === 'agent') {
        await setAgentCredentialBinding(assignment.target_id, next);
      } else {
        await setAssistantCredentialBinding(assignment.target_id, next);
      }
      await refresh();
    });

  // Each select's vocabulary, written once and read twice: the options in the
  // popup, and the label the closed trigger shows. `<SelectValue>` renders the
  // raw value unless the root carries the same list as its `items`
  // (@base-ui/react/select), so without these the triggers would read
  // `static_bearer`, `agent`, and a target's bare id — wire spellings on
  // screen (§6), in a dialog whose whole job is to name what is being made.
  const credentialTypeOptions = useMemo(
    () => [
      { value: 'static_bearer' as const, label: t('manage:credentials.type_static') },
      { value: 'mcp_oauth' as const, label: t('manage:credentials.type_oauth') },
      { value: 'mcp_static_header' as const, label: t('manage:credentials.type_header') },
      { value: 'environment_variable' as const, label: t('manage:credentials.type_environment') },
      { value: 'http_basic' as const, label: t('manage:credentials.type_http_basic') },
    ],
    [t],
  );

  const targetTypeOptions = useMemo(
    () => [
      { value: 'agent' as const, label: t('manage:nav.agents') },
      { value: 'assistant' as const, label: t('manage:nav.assistants') },
    ],
    [t],
  );

  // Which of the two collections is being offered, and which field of it names
  // a row, is one decision — resolved here rather than once per option.
  const targetOptions = useMemo(
    () =>
      targetType === 'agent'
        ? agents.map((agent) => ({ value: agent.agent_id, label: agent.name }))
        : assistants.map((assistant) => ({
            value: assistant.assistant_id,
            label: assistant.display_name,
          })),
    [targetType, agents, assistants],
  );

  if (loading && !vault) {
    return (
      <ConsoleRecordPage title={t('common:credential_vault')}>
        <ConsoleRecordLoading />
      </ConsoleRecordPage>
    );
  }

  if (!vault) {
    return (
      <ConsoleRecordPage title={t('common:credential_vault')}>
        <ConsoleErrorState
          title={t('manage:credentials.load_failed')}
          detail={error}
          onRetry={() => void refresh()}
        />
      </ConsoleRecordPage>
    );
  }

  const credentials = vault.credentials || [];

  return (
    <ConsoleRecordPage
      title={vault.display_name}
      status={
        vault.archived_at
          ? { tone: 'idle', label: t('manage:credentials.archived') }
          : { tone: 'done', label: t('manage:credentials.active') }
      }
      actions={
        <>
          {!vault.archived_at && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => void run(async () => { await archiveAdminVault(vault.vault_id); await refresh(); })}
            >
              <Archive className="size-4" />
              {t('manage:credentials.archive')}
            </Button>
          )}
          {/* The console's own dialog rather than `window.confirm()`, which is
              the browser's — a different typeface, a different place on screen,
              and no way to name which vault is going. The question belongs to
              the button (`confirm`); see ConsoleDangerButton for why arming in
              place is the wrong shape for it.

              The test id names the button that deletes, not the one that asks:
              the credentials below carry a Delete with the same label, and the
              end-to-end specs under e2e/specs/ arm by label and then confirm by
              this id, so arming the wrong control fails there instead of
              deleting the wrong record. `ConsoleDangerConfirm` takes the armed
              button's words, so the id rides on them. */}
          <ConsoleDangerButton
            disabled={busy}
            confirm={{
              title: t('manage:credentials.confirm_delete_vault', { name: vault.display_name }),
              action: (
                <span data-testid="credential-vault-delete">{t('common:confirm_delete')}</span>
              ),
              onConfirm: () =>
                void run(async () => {
                  await deleteAdminVault(vault.vault_id);
                  navigate('/manage/credentials');
                }),
            }}
          >
            {t('common:delete')}
          </ConsoleDangerButton>
        </>
      }
      rail={
        <ConsoleFactRail>
          <ConsoleFact
            label={t('manage:credentials.vault_id')}
            value={<span className="font-mono">{vault.vault_id}</span>}
          />
          {/* How a secret in this vault actually reaches a sandbox. Deployment
              policy, not a vault setting — reported, not offered. */}
          <ConsoleFact
            label={t('manage:credentials.model_delivery')}
            value={credentialDeliveryLabel(delivery, 'model_credentials', t)}
          />
          <ConsoleFact
            label={t('manage:credentials.mcp_delivery')}
            value={credentialDeliveryLabel(delivery, 'mcp_credentials', t)}
          />
          <ConsoleFact
            label={t('manage:credentials.environment_delivery')}
            value={credentialDeliveryLabel(delivery, 'environment_credentials', t)}
          />
          {vault.archived_at && (
            <ConsoleFact
              label={t('manage:credentials.archived')}
              value={formatDateTime(vault.archived_at) || '—'}
            />
          )}
        </ConsoleFactRail>
      }
    >
      {actionError && <ErrorNote>{actionError}</ErrorNote>}

      <ConsoleCard
        title={t('manage:credentials.section_credentials')}
        intro={t('manage:credentials.secret_write_only')}
        actions={
          !vault.archived_at && (
            <Button data-testid="credential-add" size="sm" disabled={busy} onClick={() => setCredentialOpen(true)}>
              <Plus className="size-4" />
              {t('manage:credentials.add_credential')}
            </Button>
          )
        }
      >
        {credentials.length === 0 ? (
          <ConsoleEmptyState title={t('manage:credentials.no_credentials')} />
        ) : (
          <ul className="divide-y divide-border">
            {credentials.map((credential) => (
              <li
                key={credential.credential_id}
                data-testid="managed-credential-row"
                className="flex items-center justify-between gap-4 py-2.5 first:pt-0 last:pb-0"
              >
                <div className="min-w-0">
                  <div className="t-label truncate">{credentialLabel(credential, t)}</div>
                  <div className="t-copy-sm truncate text-muted-foreground">
                    {credentialTypeLabel(credential.auth.type, t)} ·{' '}
                    <span className="font-mono">{credentialTarget(credential)}</span>
                  </div>
                  {/* An expiry is the one fact about a credential that changes
                      with nobody touching it: a conversation that reaches for
                      an expired one fails in provisioning, so the record says
                      it first. */}
                  {credential.auth.expires_at && (
                    <div className="t-copy-sm mt-1 truncate">
                      {credentialExpired(credential.auth.expires_at) ? (
                        <StatusPill tone="failed">
                          {t('manage:credentials.expired_on', {
                            when: formatDateTime(credential.auth.expires_at),
                          })}
                        </StatusPill>
                      ) : (
                        <span className="text-muted-foreground">
                          {t('manage:credentials.expires_on', {
                            when: formatDateTime(credential.auth.expires_at),
                          })}
                        </span>
                      )}
                    </div>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {!credential.archived_at && (
                    <Button
                      size="xs"
                      variant="outline"
                      disabled={busy}
                      onClick={() => void run(async () => {
                        await archiveAdminVaultCredential(vault.vault_id, credential.credential_id);
                        await refresh();
                      })}
                    >
                      {t('manage:credentials.archive')}
                    </Button>
                  )}
                  {/* `xs`, like Archive beside it: a row is a band, and a band
                      has one height (docs/frontend-design.md §9). The dialog it
                      opens is sized by its own foot, not by this control.

                      A vault's Delete carries the same word a few lines above,
                      so the question has to name the credential — which is the
                      argument for asking it in a dialog rather than in the strip
                      of space left at the end of a row. */}
                  <ConsoleDangerButton
                    size="xs"
                    disabled={busy}
                    confirm={{
                      title: t('manage:credentials.confirm_delete_credential', {
                        name: credentialLabel(credential, t),
                      }),
                      action: t('common:confirm_delete'),
                      onConfirm: () => void run(async () => {
                        await deleteAdminVaultCredential(vault.vault_id, credential.credential_id);
                        await refresh();
                      }),
                    }}
                  >
                    {t('common:delete')}
                  </ConsoleDangerButton>
                </div>
              </li>
            ))}
          </ul>
        )}
      </ConsoleCard>

      <ConsoleCard
        title={t('manage:credentials.section_assignments')}
        intro={t('manage:credentials.bind_help')}
        actions={
          !vault.archived_at && (
            <Button
              data-testid="credential-binding-open"
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => setBindingOpen(true)}
            >
              {t('manage:credentials.bind')}
            </Button>
          )
        }
      >
        {assignments.length === 0 ? (
          <ConsoleEmptyState title={t('manage:credentials.no_assignments')} />
        ) : (
          /* The rule between rows is each row's own bottom border, not the
             group's `divide-y`: `divide-*` puts every declaration it writes
             inside `:where()`, which has no specificity, so `Item`'s `border`
             and `border-transparent` win and the rules never paint. */
          <ItemGroup className="gap-0">
            {assignments.map((assignment) => (
              /* `role="listitem"` because `ItemGroup` declares a list and `Item`
                 claims no role of its own: a list whose children carry none
                 reports as empty to a reader who cannot see the rows
                 (docs/frontend-design.md §10). The kit's inset and rounded box
                 go back out so this band matches the credentials list above. */
              <Item
                key={`${assignment.target_type}:${assignment.target_id}`}
                role="listitem"
                data-testid="credential-assignment-row"
                className="rounded-none border-0 border-b border-border px-0 first:pt-0 last:border-b-0 last:pb-0"
              >
                <ItemContent className="min-w-0 gap-0">
                  {/* The type role and the step it names, because the two live in
                      different layers: `.t-label` is @layer components and the
                      kit's own `text-sm` is a utility, so only a utility takes
                      the size back. Same for `.t-copy-sm` below. `block` because
                      `text-overflow` does not reach the anonymous flex item a
                      bare string becomes inside the kit's flex title. */}
                  <ItemTitle className="t-label block truncate text-13">
                    {assignment.target_name}
                  </ItemTitle>
                  <ItemDescription className="t-copy-sm text-xs">
                    {assignment.target_type === 'agent'
                      ? t('manage:nav.agents')
                      : t('manage:nav.assistants')}
                  </ItemDescription>
                </ItemContent>
                <ItemActions>
                  <Button size="xs" variant="ghost" disabled={busy} onClick={() => unbind(assignment)}>
                    {t('manage:credentials.unbind')}
                  </Button>
                </ItemActions>
              </Item>
            ))}
          </ItemGroup>
        )}
      </ConsoleCard>

      <Dialog open={credentialOpen} onOpenChange={setCredentialOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{t('manage:credentials.add_credential')}</DialogTitle>
            <DialogDescription>{t('manage:credentials.secret_write_only')}</DialogDescription>
          </DialogHeader>
          <Label className="block space-y-1.5 text-sm font-normal">
            <span>{t('manage:credentials.type')}</span>
            <Select
              items={credentialTypeOptions}
              value={credentialType}
              // Base UI reports a cleared select as `null`. There is no
              // untyped credential to build, so a null names nothing to
              // change — the previous choice stands.
              onValueChange={(value) => { if (value !== null) setCredentialType(value); }}
            >
              <SelectTrigger data-testid="credential-type" className="w-full"><SelectValue /></SelectTrigger>
              {/* The menu hangs off the trigger's box; it does not sit on top
                  of it. Base UI's default `alignItemWithTrigger` is the macOS
                  native-select behaviour, which places the popup so the
                  selected item's text lands on the trigger's text: the text
                  lines up and the boxes do not, which reads as a menu that
                  missed rather than as one that opened. `align="start"` keeps
                  the left edges together. */}
              <SelectContent align="start" alignItemWithTrigger={false}>
                {credentialTypeOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>
          <Label className="block space-y-1.5 text-sm font-normal">
            <span>{t('manage:credentials.credential_name')}</span>
            <Input value={credentialName} onChange={(event) => setCredentialName(event.target.value)} />
          </Label>
          <Label className="block space-y-1.5 text-sm font-normal">
            <span>{credentialType === 'environment_variable'
              ? t('manage:credentials.variable_name')
              : credentialType === 'http_basic'
                ? t('manage:credentials.http_basic_url')
                : t('manage:credentials.mcp_url')}</span>
            <Input
              data-testid="credential-target"
              value={credentialTargetValue}
              onChange={(event) => setCredentialTargetValue(event.target.value)}
              placeholder={credentialType === 'http_basic' ? 'https://github.com/your-org/private-skills.git' : undefined}
              aria-describedby={credentialType === 'http_basic' ? 'credential-http-basic-help' : undefined}
            />
          </Label>
          {credentialType === 'http_basic' && (
            <>
              <p id="credential-http-basic-help" className="text-sm text-muted-foreground">
                {t('manage:credentials.http_basic_help')}
              </p>
              <Label className="block space-y-1.5 text-sm font-normal">
                <span>{t('manage:credentials.username')}</span>
                <Input
                  data-testid="credential-username"
                  value={credentialUsername}
                  onChange={(event) => setCredentialUsername(event.target.value)}
                  placeholder={t('manage:credentials.username_placeholder')}
                  autoComplete="off"
                />
              </Label>
            </>
          )}
          {credentialType === 'mcp_static_header' && (
            <Label className="block space-y-1.5 text-sm font-normal">
              <span>{t('manage:credentials.header_name')}</span>
              <Input
                data-testid="credential-header-name"
                value={credentialHeaderName}
                onChange={(event) => setCredentialHeaderName(event.target.value)}
                placeholder="apikey"
              />
            </Label>
          )}
          <Label className="block space-y-1.5 text-sm font-normal">
            <span>{t(credentialType === 'http_basic' ? 'manage:credentials.password_token' : 'manage:credentials.secret')}</span>
            <Input
              data-testid="credential-secret"
              type="password"
              value={credentialSecret}
              onChange={(event) => setCredentialSecret(event.target.value)}
              placeholder={credentialType === 'http_basic' ? t('manage:credentials.password_token_placeholder') : undefined}
              autoComplete="new-password"
            />
          </Label>
          {credentialType === 'environment_variable' && (
            <Label className="block space-y-1.5 text-sm font-normal">
              <span>{t('manage:credentials.allowed_hosts')}</span>
              <Input data-testid="credential-allowed-hosts" value={allowedHosts} onChange={(event) => setAllowedHosts(event.target.value)} placeholder="api.example.com, uploads.example.com" />
            </Label>
          )}
          {credentialType === 'mcp_oauth' && (
            <Label className="block space-y-1.5 text-sm font-normal">
              <span>{t('manage:credentials.expires_at')}</span>
              <Input value={expiresAt} onChange={(event) => setExpiresAt(event.target.value)} placeholder="2099-12-31T23:59:59Z" />
            </Label>
          )}
          {actionError && <ErrorNote>{actionError}</ErrorNote>}
          <DialogFooter>
            <Button variant="outline" onClick={() => setCredentialOpen(false)}>{t('common:cancel')}</Button>
            <Button data-testid="credential-save" onClick={saveCredential} disabled={busy}>{t('common:create')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={bindingOpen} onOpenChange={setBindingOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('manage:credentials.bind')}</DialogTitle>
            <DialogDescription>{t('manage:credentials.bind_help')}</DialogDescription>
          </DialogHeader>
          <Label className="block space-y-1.5 text-sm font-normal">
            <span>{t('manage:credentials.target_type')}</span>
            <Select
              items={targetTypeOptions}
              value={targetType}
              // A null is the cleared select, which neither of the two kinds
              // of target is; changing kinds drops the chosen row, because an
              // agent's id names no assistant.
              onValueChange={(value) => {
                if (value === null) return;
                setTargetType(value);
                setTargetId('');
              }}
            >
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              {/* Anchored like the type select above. */}
              <SelectContent align="start" alignItemWithTrigger={false}>
                {targetTypeOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>
          <Label className="block space-y-1.5 text-sm font-normal">
            <span>{t('manage:credentials.target')}</span>
            <Select
              items={targetOptions}
              value={targetId}
              // `''` is this field's own "nothing chosen" — the Bind button is
              // disabled on it — and Base UI shows the placeholder for it. A
              // null from a cleared select means the same thing, so it lands
              // on the same value rather than on a second empty state.
              onValueChange={(value) => setTargetId(value ?? '')}
            >
              <SelectTrigger data-testid="credential-binding-target" className="w-full">
                <SelectValue placeholder={t('manage:credentials.choose_target')} />
              </SelectTrigger>
              {/* Anchored like the type select above. */}
              <SelectContent align="start" alignItemWithTrigger={false}>
                {targetOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>
          {actionError && <ErrorNote>{actionError}</ErrorNote>}
          <DialogFooter>
            <Button variant="outline" onClick={() => setBindingOpen(false)}>{t('common:cancel')}</Button>
            <Button data-testid="credential-binding-save" onClick={bind} disabled={busy || !targetId}>{t('manage:credentials.bind')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </ConsoleRecordPage>
  );
}
