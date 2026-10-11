import { useCallback, useEffect, useMemo, useState } from 'react';
import { Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { ErrorNote } from '@/components/shell';
import { linkDeploymentAccount, listDeploymentAccountLinks, unlinkDeploymentAccount } from '@/api';
import type { ChannelAccountLink } from '@/types';

import {
  ConsoleCard,
  ConsoleDangerButton,
  ConsoleEmptyState,
  ConsoleFieldRow,
  ConsoleTable,
  ConsoleTableSkeleton,
  ConsoleTextField,
  formatDateTime,
  type ConsoleColumn,
} from './console';

/**
 * The participants of a trigger that runs as linked accounts, and the account
 * each one's messages run as. A participant without a link is refused.
 */
export default function DeploymentAccountLinks({
  agentId,
  deploymentId,
}: {
  agentId: string;
  deploymentId: string;
}) {
  const { t } = useTranslation();
  const [links, setLinks] = useState<ChannelAccountLink[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [participant, setParticipant] = useState('');
  const [userId, setUserId] = useState('');
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState('');

  const load = useCallback(async () => {
    try {
      setLinks(await listDeploymentAccountLinks(agentId, deploymentId));
      setError('');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [agentId, deploymentId]);

  useEffect(() => {
    void load();
  }, [load]);

  const link = async () => {
    setSaving(true);
    setError('');
    try {
      await linkDeploymentAccount(agentId, deploymentId, {
        participant: participant.trim(),
        user_id: userId.trim(),
      });
      setParticipant('');
      setUserId('');
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const unlink = async (name: string) => {
    setRemoving(name);
    setError('');
    try {
      await unlinkDeploymentAccount(agentId, deploymentId, name);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRemoving('');
    }
  };

  const columns = useMemo<ConsoleColumn<ChannelAccountLink>[]>(
    () => [
      {
        key: 'participant',
        header: t('manage:deployments.account_links_column_participant'),
        intent: 'name',
        cell: (row) => <span className="font-mono text-xs">{row.participant}</span>,
      },
      {
        key: 'user_id',
        header: t('manage:deployments.account_links_column_user'),
        intent: 'text',
        cell: (row) => <span className="font-mono text-xs">{row.user_id}</span>,
      },
      {
        key: 'linked_at',
        header: t('manage:deployments.account_links_column_linked'),
        intent: 'timestamp',
        cell: (row) => formatDateTime(row.linked_at),
      },
      {
        key: 'actions',
        header: '',
        intent: 'compact',
        cell: (row) => (
          <ConsoleDangerButton
            size="sm"
            disabled={removing === row.participant}
            onClick={() => void unlink(row.participant)}
          >
            {t('manage:deployments.account_links_remove')}
          </ConsoleDangerButton>
        ),
      },
    ],
    [t, removing],
  );

  return (
    <ConsoleCard
      title={t('manage:deployments.account_links_title')}
      intro={t('manage:deployments.account_links_intro')}
    >
      {error && <ErrorNote>{error}</ErrorNote>}
      <ConsoleFieldRow
        label={t('manage:deployments.account_links_field_participant')}
        htmlFor="deployment-account-link-participant"
        help={t('manage:deployments.account_links_field_participant_help')}
        required
      >
        <ConsoleTextField
          id="deployment-account-link-participant"
          value={participant}
          onChange={setParticipant}
          placeholder="telegram:123456789"
        />
      </ConsoleFieldRow>
      <ConsoleFieldRow
        label={t('manage:deployments.account_links_field_user')}
        htmlFor="deployment-account-link-user"
        help={t('manage:deployments.account_links_field_user_help')}
        required
      >
        <ConsoleTextField
          id="deployment-account-link-user"
          value={userId}
          onChange={setUserId}
        />
      </ConsoleFieldRow>
      <div className="mt-4 flex justify-end">
        <Button
          size="sm"
          onClick={() => void link()}
          disabled={saving || !participant.trim() || !userId.trim()}
        >
          <Plus className="size-4" />
          {t('manage:deployments.account_links_add')}
        </Button>
      </div>
      <div className="mt-4">
        {loading ? (
          <ConsoleTableSkeleton columns={columns} rows={2} />
        ) : (
          <ConsoleTable
            columns={columns}
            rows={links}
            rowKey={(row) => row.participant}
            empty={
              <ConsoleEmptyState
                title={t('manage:deployments.account_links_empty_title')}
                hint={t('manage:deployments.account_links_empty_hint')}
              />
            }
          />
        )}
      </div>
    </ConsoleCard>
  );
}
