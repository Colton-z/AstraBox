import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Plus, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { ErrorNote, PageShell } from '@/components/shell';
import { CodeBlock, CodeBlockCopyButton } from '@/components/ai-elements/code-block';
import {
  issueMcpClientToken,
  listMcpClientTokens,
  revokeMcpClientToken,
} from '@/api';
import type { IssuedMcpClientToken, McpClientToken } from '@/types';

import {
  ConsoleCard,
  ConsoleDangerButton,
  ConsoleEmptyState,
  ConsoleFieldRow,
  ConsoleTextField,
  ConsolePageHeader,
  ConsoleSelect,
  ConsoleTable,
  ConsoleTableSkeleton,
  type ConsoleColumn,
} from './console';
import { formatDateTime } from './agentConfig';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';

/** What the reader came here to paste. The endpoint is derived from where the
 *  console is served, so a copied block already points at this deployment. */
function clientConfig(secret: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        astrabox: {
          type: 'http',
          url: `${window.location.origin}/api/v1/mcp`,
          headers: { Authorization: `Bearer ${secret}` },
        },
      },
    },
    null,
    2,
  );
}

export default function McpTokensPage() {
  const { t } = useTranslation();

  const [tokens, setTokens] = useState<McpClientToken[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [name, setName] = useState('');
  const [scope, setScope] = useState<'read' | 'converse'>('converse');
  const [issuing, setIssuing] = useState(false);
  const [revoking, setRevoking] = useState('');
  // The whole of the secret's life on this page. Cleared when the reader is
  // done with it and never re-derived: nothing on this page can ask the server
  // for it again, because nothing on the server can answer.
  const [issued, setIssued] = useState<IssuedMcpClientToken | null>(null);

  // True after any successful read, an empty list included (see keepsLastRead).
  const loaded = useRef(false);

  const load = useCallback(async (context?: ReloadContext) => {
    const background = context?.background === true;
    if (!background) {
      setLoading(true);
      setError('');
    }
    try {
      setTokens(await listMcpClientTokens());
      setError('');
      loaded.current = true;
    } catch (e) {
      if (keepsLastRead(e, context, loaded.current)) return;
      setError((e as Error).message);
    } finally {
      if (!background) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useKeepCurrent(load);

  const issue = async () => {
    setIssuing(true);
    setError('');
    try {
      setIssued(await issueMcpClientToken({ name: name.trim(), scope }));
      setName('');
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setIssuing(false);
    }
  };

  const revoke = async (tokenId: string) => {
    setError('');
    setRevoking(tokenId);
    try {
      await revokeMcpClientToken(tokenId);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRevoking('');
    }
  };

  const columns = useMemo<ConsoleColumn<McpClientToken>[]>(
    () => [
      {
        key: 'name',
        header: t('manage:mcp_tokens.column_name'),
        intent: 'name',
        cell: (row) => row.name,
      },
      {
        key: 'scope',
        header: t('manage:mcp_tokens.column_scope'),
        intent: 'text',
        cell: (row) => t(`manage:mcp_tokens.scope_${row.scope}`),
      },
      {
        // The column a reader revokes by: a key nothing has used is the one
        // that can go without asking anybody.
        key: 'last_used_at',
        header: t('manage:mcp_tokens.column_last_used'),
        intent: 'timestamp',
        // Tested on the raw value, not the formatted one: `formatDateTime`
        // answers an em dash for an empty input, which is truthy, so a fallback
        // behind it never fires and the copy behind it is never seen.
        cell: (row) =>
          row.last_used_at
            ? formatDateTime(String(row.last_used_at))
            : t('manage:mcp_tokens.never_used'),
      },
      {
        key: 'created_at',
        header: t('manage:mcp_tokens.column_created'),
        intent: 'timestamp',
        cell: (row) => formatDateTime(row.created_at),
      },
      {
        key: 'actions',
        header: '',
        intent: 'compact',
        cell: (row) => (
          <ConsoleDangerButton
            size="sm"
            disabled={revoking === row.token_id}
            onClick={() => void revoke(row.token_id)}
          >
            {t('manage:mcp_tokens.revoke')}
          </ConsoleDangerButton>
        ),
      },
    ],
    [t, revoking],
  );

  return (
    <PageShell>
      <ConsolePageHeader
        title={t('manage:mcp_tokens.title')}
        description={t('manage:mcp_tokens.description')}
        actions={
          <Button variant="outline" onClick={() => void load()} disabled={loading}>
            <RefreshCw className="size-4" />
            {t('common:refresh')}
          </Button>
        }
      />
      <div className="flex flex-1 flex-col gap-4">
        {error && <ErrorNote>{error}</ErrorNote>}

        {issued ? (
          /* The only screen that has ever held this secret, and the last one that
             will. It replaces the issuing form rather than sitting beside it, so
             the reader finishes with it before anything else is offered. */
          <ConsoleCard
            title={t('manage:mcp_tokens.issued_title', { name: issued.name })}
            intro={t('manage:mcp_tokens.issued_intro')}
            note={t('manage:mcp_tokens.issued_once_note')}
          >
            <CodeBlock code={clientConfig(issued.secret)} language="json">
              <CodeBlockCopyButton />
            </CodeBlock>
            <div className="mt-4 flex justify-end">
              <Button size="sm" onClick={() => setIssued(null)}>
                {t('manage:mcp_tokens.issued_done')}
              </Button>
            </div>
          </ConsoleCard>
        ) : (
          <ConsoleCard
            title={t('manage:mcp_tokens.issue_title')}
            intro={t('manage:mcp_tokens.issue_intro')}
          >
            <ConsoleFieldRow
              label={t('manage:mcp_tokens.field_name')}
              htmlFor="mcp-token-name"
              help={t('manage:mcp_tokens.field_name_help')}
              required
            >
              <ConsoleTextField
                id="mcp-token-name"
                value={name}
                onChange={setName}
                mono={false}
                placeholder={t('manage:mcp_tokens.field_name_placeholder')}
              />
            </ConsoleFieldRow>
            <ConsoleFieldRow
              label={t('manage:mcp_tokens.field_scope')}
              htmlFor="mcp-token-scope"
              help={t('manage:mcp_tokens.field_scope_help')}
            >
              <ConsoleSelect
                id="mcp-token-scope"
                value={scope}
                onChange={(value) => setScope(value === 'read' ? 'read' : 'converse')}
                options={[
                  { value: 'converse', label: t('manage:mcp_tokens.scope_converse') },
                  { value: 'read', label: t('manage:mcp_tokens.scope_read') },
                ]}
              />
            </ConsoleFieldRow>
            <div className="mt-4 flex justify-end">
              <Button size="sm" onClick={() => void issue()} disabled={issuing || !name.trim()}>
                <Plus className="size-4" />
                {t('manage:mcp_tokens.issue')}
              </Button>
            </div>
          </ConsoleCard>
        )}

        <ConsoleCard title={t('manage:mcp_tokens.list_title')}>
          {loading ? (
            <ConsoleTableSkeleton columns={columns} rows={3} />
          ) : tokens.length === 0 ? (
            <ConsoleEmptyState
              title={t('manage:mcp_tokens.empty_title')}
              hint={t('manage:mcp_tokens.empty_hint')}
            />
          ) : (
            <ConsoleTable
              columns={columns}
              rows={tokens}
              rowKey={(row) => row.token_id}
              empty={t('manage:mcp_tokens.empty_title')}
            />
          )}
        </ConsoleCard>
      </div>
    </PageShell>
  );
}
