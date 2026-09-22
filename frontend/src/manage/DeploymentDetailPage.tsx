import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import {
  deleteAgentDeployment,
  listChannelProviders,
  listDeployments,
  listDeploymentRuns,
  replayDeploymentRun,
  triggerDeploymentRun,
  updateAgentDeployment,
} from '@/api';
import type {
  AgentDeployment,
  ChannelProviderDescriptor,
  DeploymentRun,
} from '@/types';

import { ErrorNote } from '@/components/shell';
import {
  ConsoleCard,
  ConsoleDangerButton,
  ConsoleErrorState,
  ConsoleEmptyState,
  ConsoleEditSections,
  ConsoleFact,
  ConsoleFactRail,
  ConsoleFieldRow,
  ConsoleRecordLoading,
  ConsoleRecordPage,
  ConsoleTable,
  StatusPill,
  formatDateTime,
  missingRequiredFields,
  revertFields,
  sectionIsDirty,
  type ConsoleColumn,
  type EditSectionSpec,
  useRecordCrumb,
} from './console';
import {
  channelEditField,
  channelFieldValues,
  firstMissingChannelField,
  hasChannelFieldValue,
} from './channelProviderFields';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';
import { useFrontendReleaseHold } from '@/hooks/useFrontendReleaseHold';

function runTone(status: DeploymentRun['status']) {
  if (status === 'COMPLETED') return 'done' as const;
  if (status === 'FAILED' || status === 'CANCELLED') return 'failed' as const;
  if (status === 'WAITING_INPUT') return 'approval' as const;
  if (status === 'RUNNING') return 'running' as const;
  return 'pending' as const;
}

const RUN_TRIGGER_LABEL_KEYS: Readonly<Record<string, string>> = {
  schedule: 'manage:deployments.run_trigger_schedule',
  manual: 'manage:deployments.run_trigger_manual',
  replay: 'manage:deployments.run_trigger_replay',
};

/** One authenticated external binding or built-in schedule and its Runs. */
export default function DeploymentDetailPage() {
  const { deploymentId = '' } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const { t } = useTranslation();

  const oneTimeSecret = (location.state as { secret?: string } | null)?.secret ?? '';

  const [deployment, setDeployment] = useState<AgentDeployment | null>(null);
  const [channelProviders, setChannelProviders] = useState<ChannelProviderDescriptor[]>([]);
  const [agentName, setAgentName] = useState('');
  const [runs, setRuns] = useState<DeploymentRun[]>([]);
  const [scheduleDraft, setScheduleDraft] = useState<Record<string, unknown>>({});
  const [channelDraft, setChannelDraft] = useState<Record<string, unknown>>({});
  const [invalidKeys, setInvalidKeys] = useState<Set<string>>(new Set());
  const [runsError, setRunsError] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [actionError, setActionError] = useState('');
  const [busy, setBusy] = useState(false);

  const crumbProvider = channelProviders.find(
    (provider) => provider.scene === deployment?.scene,
  );
  useRecordCrumb(
    deployment
      ? deployment.name || crumbProvider?.label || deployment.scene
      : t('common:deployment'),
  );

  // True after any successful read of the runs, an empty table included (see
  // keepsLastRead).
  const runsLoaded = useRef(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [deployments, providers] = await Promise.all([
        listDeployments(),
        listChannelProviders(),
      ]);
      setChannelProviders(providers);
      const found = deployments.find((binding) => binding.deployment_id === deploymentId);
      if (found) {
        if (found.scene === 'schedule') {
          try {
            setRuns(await listDeploymentRuns(found.agent_id, found.deployment_id));
            setRunsError('');
            runsLoaded.current = true;
          } catch (e) {
            setRuns([]);
            setRunsError((e as Error).message);
          }
        } else {
          setRuns([]);
          setRunsError('');
        }
        setDeployment(found);
        setScheduleDraft({
          name: found.name ?? '',
          prompt_prefix: found.prompt_prefix ?? '',
          cron: found.schedule?.cron ?? '',
          timezone: found.schedule?.timezone ?? '',
        });
        setChannelDraft({
          channel_config: { ...(found.channel_config ?? {}) },
          credentials: {},
        });
        setAgentName(found.agent_name || found.agent_id);
        setError('');
        return;
      }
      setDeployment(null);
      setError('');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [deploymentId]);

  useEffect(() => {
    void load();
  }, [load]);

  // A schedule's runs are written by the cron, not by this page, so the table
  // follows them for as long as the record is open: a run the cron starts
  // after the page loaded is otherwise never seen. Only the runs are re-read;
  // the drafts above belong to the reader.
  const reloadRuns = useCallback(async (context?: ReloadContext) => {
    if (!deployment || deployment.scene !== 'schedule') return;
    try {
      setRuns(await listDeploymentRuns(deployment.agent_id, deployment.deployment_id));
      setRunsError('');
      runsLoaded.current = true;
    } catch (pollError) {
      if (keepsLastRead(pollError, context, runsLoaded.current)) return;
      setRunsError((pollError as Error).message);
    }
  }, [deployment]);
  useKeepCurrent(reloadRuns, { follow: deployment?.scene === 'schedule' });
  useFrontendReleaseHold(Boolean(deployment) && (
    scheduleDraft.name !== (deployment?.name ?? '')
    || scheduleDraft.prompt_prefix !== (deployment?.prompt_prefix ?? '')
    || scheduleDraft.cron !== (deployment?.schedule?.cron ?? '')
    || scheduleDraft.timezone !== (deployment?.schedule?.timezone ?? '')
    || JSON.stringify(channelDraft) !== JSON.stringify({
      channel_config: { ...(deployment?.channel_config ?? {}) },
      credentials: {},
    })
  ));

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setActionError('');
    try {
      await action();
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (loading && !deployment) {
    return (
      <ConsoleRecordPage title={t('common:deployment')}>
        <ConsoleRecordLoading />
      </ConsoleRecordPage>
    );
  }

  if (!deployment) {
    return (
      <ConsoleRecordPage title={t('common:deployment')}>
        <ConsoleErrorState
          title={t('manage:deployments.error_title')}
          detail={error || t('manage:deployments.not_found', { id: deploymentId })}
          onRetry={() => void load()}
        />
      </ConsoleRecordPage>
    );
  }

  const enabled = deployment.enabled !== false;
  const isSchedule = deployment.scene === 'schedule';
  const isChannel = deployment.scene.startsWith('channel:');
  const channelProvider = channelProviders.find(
    (provider) => provider.scene === deployment.scene,
  );
  const triggerUrl = `/api/v1/deployments/${deployment.deployment_id}/trigger`;
  const callbackUrl =
    channelProvider?.callback_path && deployment.callback_base_url
      ? `${deployment.callback_base_url}${channelProvider.callback_path}`
      : '';
  const showTriggerUrl = !isChannel || channelProvider?.uses_trigger_secret === true;
  const scheduleBaseline = {
    name: deployment.name ?? '',
    prompt_prefix: deployment.prompt_prefix ?? '',
    cron: deployment.schedule?.cron ?? '',
    timezone: deployment.schedule?.timezone ?? '',
  };
  const scheduleSections: EditSectionSpec[] = [
    {
      label: t('manage:deployments.section_schedule'),
      fields: [
        {
          key: 'name',
          label: t('manage:deployments.field_name'),
          type: 'text',
          required: true,
          get: (draft) => draft.name,
          set: (draft, value) => ({ ...draft, name: value }),
          help: t('manage:deployments.name_help'),
        },
        {
          key: 'prompt_prefix',
          label: t('manage:deployments.field_prompt'),
          type: 'textarea',
          required: true,
          rows: 3,
          get: (draft) => draft.prompt_prefix,
          set: (draft, value) => ({ ...draft, prompt_prefix: value }),
          help: t('manage:deployments.prompt_help'),
        },
        {
          key: 'cron',
          label: t('manage:deployments.field_cron'),
          type: 'text',
          required: true,
          get: (draft) => draft.cron,
          set: (draft, value) => ({ ...draft, cron: value }),
          help: t('manage:deployments.cron_help'),
        },
        {
          key: 'timezone',
          label: t('manage:deployments.field_timezone'),
          type: 'text',
          required: true,
          get: (draft) => draft.timezone,
          set: (draft, value) => ({ ...draft, timezone: value }),
          help: t('manage:deployments.timezone_help'),
        },
      ],
    },
  ];
  const channelSections: EditSectionSpec[] = channelProvider
    ? [
        {
          label: t('manage:deployments.section_channel_configuration', {
            provider: channelProvider.label,
          }),
          fields: [
            ...channelProvider.config_fields.map((field) =>
              channelEditField(field, 'channel_config'),
            ),
            ...channelProvider.credential_fields.map((field) =>
              channelEditField(field, 'credentials'),
            ),
          ],
        },
      ].filter((section) => section.fields.length > 0)
    : [];
  const missingScheduleFields = missingRequiredFields(scheduleSections, scheduleDraft);
  const replacingCredentials = channelProvider
    ? hasChannelFieldValue(
        channelDraft,
        'credentials',
        channelProvider.credential_fields,
      )
    : false;
  const missingChannelField = channelProvider
    ? firstMissingChannelField(
        channelDraft,
        'channel_config',
        channelProvider.config_fields,
      ) ??
      (replacingCredentials || deployment.credentials_configured === false
        ? firstMissingChannelField(
            channelDraft,
            'credentials',
            channelProvider.credential_fields,
          )
        : undefined)
    : undefined;
  const channelBaseline = {
    channel_config: { ...(deployment.channel_config ?? {}) },
    credentials: {},
  };
  const refreshRuns = async () => {
    setRuns(await listDeploymentRuns(deployment.agent_id, deployment.deployment_id));
    setRunsError('');
  };
  const runColumns: ConsoleColumn<DeploymentRun>[] = [
    {
      key: 'run',
      intent: 'name',
      header: t('manage:deployments.run_id'),
      title: (item) => item.run_id,
      cell: (item) => (
        <span className="block truncate font-mono text-xs text-foreground">
          {item.run_id}
        </span>
      ),
    },
    {
      key: 'trigger',
      intent: 'compact',
      header: t('manage:deployments.run_trigger'),
      title: (item) => {
        const labelKey = RUN_TRIGGER_LABEL_KEYS[String(item.trigger)];
        return labelKey
          ? t(labelKey)
          : `${t('manage:deployments.run_trigger_unknown')}: ${String(item.trigger)}`;
      },
      cell: (item) => {
        const labelKey = RUN_TRIGGER_LABEL_KEYS[String(item.trigger)];
        return (
          <span className="text-muted-foreground">
            {labelKey ? (
              t(labelKey)
            ) : (
              <>
                {t('manage:deployments.run_trigger_unknown')}{' '}
                <span data-slot="verbatim" className="font-mono text-xs">
                  {String(item.trigger)}
                </span>
              </>
            )}
          </span>
        );
      },
    },
    {
      key: 'status',
      intent: 'status',
      header: t('common:status'),
      cell: (item) => (
        <StatusPill tone={runTone(item.status)} live={item.status === 'RUNNING'}>
          {item.status}
        </StatusPill>
      ),
    },
    {
      key: 'started',
      intent: 'timestamp',
      header: t('manage:deployments.run_time'),
      cell: (item) => (
        <span className="text-muted-foreground">
          {formatDateTime(item.scheduled_for || item.created_at || '') || '—'}
        </span>
      ),
    },
    {
      key: 'replay',
      intent: 'compact',
      header: t('manage:deployments.run_action'),
      cell: (item) => (
        <Button
          variant="ghost"
          size="sm"
          disabled={busy}
          onClick={(event) => {
            event.stopPropagation();
            void run(async () => {
              await replayDeploymentRun(
                deployment.agent_id,
                deployment.deployment_id,
                item.run_id,
              );
              await refreshRuns();
            });
          }}
        >
          {t('manage:deployments.replay')}
        </Button>
      ),
    },
  ];

  return (
    <ConsoleRecordPage
      title={deployment.name || channelProvider?.label || deployment.scene}
      lede={t(
        isSchedule
          ? 'manage:deployments.record_schedule_lede'
          : isChannel
            ? 'manage:deployments.record_channel_lede'
            : 'manage:deployments.record_lede',
        { agent: agentName },
      )}
      status={
        enabled
          ? { tone: 'done', label: t('common:enabled') }
          : { tone: 'idle', label: t('common:disabled') }
      }
      actions={
        <>
          {isSchedule && (
            <Button
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  await triggerDeploymentRun(
                    deployment.agent_id,
                    deployment.deployment_id,
                  );
                  await refreshRuns();
                })
              }
            >
              {t('manage:deployments.run_now')}
            </Button>
          )}
          <Button
            variant="outline"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await updateAgentDeployment(deployment.agent_id, deployment.deployment_id, {
                  enabled: !enabled,
                });
                await load();
              })
            }
          >
            {enabled ? t('common:disable') : t('common:enable')}
          </Button>
          {/* The console's own dialog rather than `confirm()`: the browser's
              dialog cannot say which binding is going, and an outside system is
              pointed at it. The question is the button's (`confirm`) rather than
              this row's — see ConsoleDangerButton for why arming in place is
              the wrong shape for it. */}
          <ConsoleDangerButton
            disabled={busy}
            confirm={{
              title: t('manage:deployments.confirm_delete', { scene: deployment.scene }),
              action: t('common:confirm_delete'),
              onConfirm: () =>
                void run(async () => {
                  await deleteAgentDeployment(deployment.agent_id, deployment.deployment_id);
                  navigate('/manage/deployments');
                }),
            }}
          >
            {t('common:delete')}
          </ConsoleDangerButton>
        </>
      }
      rail={
        <ConsoleFactRail>
          <ConsoleFact label={t('manage:deployments.field_agent')} value={agentName || '—'} />
          <ConsoleFact
            label={t('manage:deployments.field_deployment_id')}
            value={<span className="font-mono">{deployment.deployment_id}</span>}
          />
          <ConsoleFact
            label={t('common:created_at')}
            value={formatDateTime(deployment.created_at || '') || '—'}
          />
          <ConsoleFact
            label={t('common:updated_at')}
            value={formatDateTime(deployment.updated_at || '') || '—'}
          />
        </ConsoleFactRail>
      }
    >
      {actionError && <ErrorNote>{actionError}</ErrorNote>}

      {isSchedule ? (
        <ConsoleEditSections
          sections={scheduleSections}
          draft={scheduleDraft}
          onDraftChange={setScheduleDraft}
          idPrefix="deployment-schedule"
          invalidKeys={invalidKeys}
          onInvalidChange={(key, invalid) =>
            setInvalidKeys((previous) => {
              const next = new Set(previous);
              if (invalid) next.add(key);
              else next.delete(key);
              return next;
            })
          }
          cardProps={(section) => ({
            intro: t('manage:deployments.record_schedule_intro'),
            dirty: sectionIsDirty(section.fields, scheduleDraft, scheduleBaseline),
            blocked:
              section.fields.some((field) => invalidKeys.has(field.key)) ||
              missingScheduleFields.length > 0,
            blockedReason:
              missingScheduleFields.length > 0
                ? t('manage:console.blocked_by_required', {
                    field: String(missingScheduleFields[0].label),
                  })
                : undefined,
            saving: busy,
            saveLabel: t('common:save'),
            revertLabel: t('common:revert'),
            onSave: () =>
              void run(async () => {
                const cron = String(scheduleDraft.cron ?? '').trim();
                const timezone = String(scheduleDraft.timezone ?? '').trim();
                const update: {
                  name: string;
                  prompt_prefix: string;
                  schedule?: { cron: string; timezone: string };
                } = {
                  name: String(scheduleDraft.name ?? '').trim(),
                  prompt_prefix: String(scheduleDraft.prompt_prefix ?? '').trim(),
                };
                if (
                  cron !== scheduleBaseline.cron ||
                  timezone !== scheduleBaseline.timezone
                ) {
                  update.schedule = { cron, timezone };
                }
                await updateAgentDeployment(
                  deployment.agent_id,
                  deployment.deployment_id,
                  update,
                );
                await load();
              }),
            onRevert: () =>
              setScheduleDraft(
                revertFields(section.fields, scheduleDraft, scheduleBaseline),
              ),
          })}
        />
      ) : (
        <ConsoleCard
          title={t('manage:deployments.section_binding')}
          intro={t('manage:deployments.record_intro')}
        >
          {showTriggerUrl && (
            <ConsoleFieldRow label={t('manage:deployments.field_trigger_url')}>
              <p className="console-input console-input--fixed font-mono select-text">
                {triggerUrl}
              </p>
            </ConsoleFieldRow>
          )}
          {channelProvider && (
            <ConsoleFieldRow
              label={t('manage:deployments.field_provider')}
              help={t('manage:deployments.provider_console_help')}
            >
              <div className="flex min-h-9 flex-wrap items-center gap-3">
                <span className="t-label">{channelProvider.label}</span>
                {channelProvider.setup_url && (
                  <a
                    className="t-link text-sm"
                    href={channelProvider.setup_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {t('manage:deployments.open_provider_console')}
                  </a>
                )}
                {channelProvider.documentation_url && (
                  <a
                    className="t-link text-sm"
                    href={channelProvider.documentation_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {t('manage:deployments.provider_documentation')}
                  </a>
                )}
              </div>
            </ConsoleFieldRow>
          )}
          {callbackUrl && (
            <ConsoleFieldRow
              label={t('manage:deployments.field_callback_url')}
              help={t('manage:deployments.callback_url_help')}
            >
              <p className="console-input console-input--fixed font-mono select-text">
                {callbackUrl}
              </p>
            </ConsoleFieldRow>
          )}
          {channelProvider?.credential_fields.length ? (
            <ConsoleFieldRow
              label={t('manage:deployments.field_credentials')}
              help={t('manage:deployments.credentials_write_only_help')}
            >
              <p className="t-copy">
                {deployment.credentials_configured
                  ? t('manage:deployments.credentials_configured')
                  : t('manage:deployments.credentials_missing')}
              </p>
            </ConsoleFieldRow>
          ) : null}
          {deployment.prompt_prefix && (
            <ConsoleFieldRow label={t('manage:deployments.field_prompt_prefix')}>
              <p className="t-copy whitespace-pre-wrap">{deployment.prompt_prefix}</p>
            </ConsoleFieldRow>
          )}
          {(oneTimeSecret || deployment.secret) && (
            <ConsoleFieldRow
              label={t('manage:deployments.field_secret')}
              help={t('manage:deployments.secret_once')}
            >
              <p className="console-input console-input--fixed font-mono select-text">
                {oneTimeSecret || deployment.secret}
              </p>
            </ConsoleFieldRow>
          )}
        </ConsoleCard>
      )}

      {!isSchedule && channelSections.length > 0 && (
        <ConsoleEditSections
          sections={channelSections}
          draft={channelDraft}
          onDraftChange={setChannelDraft}
          idPrefix="deployment-channel"
          invalidKeys={invalidKeys}
          onInvalidChange={(key, invalid) =>
            setInvalidKeys((previous) => {
              const next = new Set(previous);
              if (invalid) next.add(key);
              else next.delete(key);
              return next;
            })
          }
          cardProps={(section) => ({
            intro: t('manage:deployments.channel_configuration_intro'),
            note: t('manage:deployments.credentials_write_only_help'),
            dirty: sectionIsDirty(section.fields, channelDraft, channelBaseline),
            blocked:
              section.fields.some((field) => invalidKeys.has(field.key)) ||
              missingChannelField !== undefined,
            blockedReason: missingChannelField
              ? t('manage:deployments.err_channel_field_required', {
                  field: missingChannelField.label,
                })
              : undefined,
            saving: busy,
            saveLabel: t('common:save'),
            revertLabel: t('common:revert'),
            onSave: () =>
              void run(async () => {
                if (!channelProvider) return;
                await updateAgentDeployment(
                  deployment.agent_id,
                  deployment.deployment_id,
                  {
                    channel_config: channelFieldValues(
                      channelDraft,
                      'channel_config',
                      channelProvider.config_fields,
                    ),
                    ...(replacingCredentials
                      ? {
                          credentials: channelFieldValues(
                            channelDraft,
                            'credentials',
                            channelProvider.credential_fields,
                          ),
                        }
                      : {}),
                  },
                );
                await load();
              }),
            onRevert: () => setChannelDraft(channelBaseline),
          })}
        />
      )}

      {isSchedule && (
        <ConsoleCard
          title={t('manage:deployments.runs_title')}
          intro={t('manage:deployments.runs_intro')}
        >
          {runsError && <ErrorNote>{runsError}</ErrorNote>}
          <ConsoleTable
            columns={runColumns}
            rows={runs}
            rowKey={(item) => item.run_id}
            isRowClickable={(item) => Boolean(item.session_id)}
            onRowClick={(item) => {
              if (item.session_id) navigate(`/manage/sessions/${item.session_id}`);
            }}
            empty={
              <ConsoleEmptyState
                title={t('manage:deployments.runs_empty_title')}
                hint={t('manage:deployments.runs_empty_hint')}
              />
            }
          />
        </ConsoleCard>
      )}
    </ConsoleRecordPage>
  );
}
