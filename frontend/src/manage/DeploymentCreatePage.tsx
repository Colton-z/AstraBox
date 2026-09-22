import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { createAgentDeployment, listAgents, listChannelProviders } from '@/api';
import type { AgentConfig, ChannelProviderDescriptor } from '@/types';

import {
  ConsoleCreatePage,
  ConsoleErrorState,
  ConsoleRecordLoading,
  ConsoleRecordPage,
  useRecordCrumb,
  type EditSectionSpec,
} from './console';
import {
  channelEditField,
  channelFieldValues,
  firstMissingChannelField,
} from './channelProviderFields';

const BUILTIN_SCENES = ['schedule', 'hmac', 'scheduler'] as const;

/** A new built-in schedule, authenticated trigger, or concrete chat binding. */
export default function DeploymentCreatePage() {
  const navigate = useNavigate();
  const { t } = useTranslation();

  const [agents, setAgents] = useState<AgentConfig[]>([]);
  const [channelProviders, setChannelProviders] = useState<ChannelProviderDescriptor[]>([]);
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [invalidKeys, setInvalidKeys] = useState<Set<string>>(new Set());

  useRecordCrumb(t('manage:deployments.create_title'));

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError('');
    try {
      const [agentList, providers] = await Promise.all([
        listAgents(),
        listChannelProviders(),
      ]);
      setAgents(agentList);
      setChannelProviders(providers);
      setDraft({
        agent_id: agentList[0]?.agent_id ?? '',
        scene: BUILTIN_SCENES[0],
        name: '',
        prompt_prefix: '',
        cron: '0 9 * * *',
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
        channel_config: {},
        credentials: {},
      });
    } catch (loadFailure) {
      setLoadError((loadFailure as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const draftScene = String(draft?.scene ?? '');
  const selectedProvider = channelProviders.find((provider) => provider.scene === draftScene);
  const sections = useMemo<EditSectionSpec[]>(() => {
    const fields: EditSectionSpec['fields'] = [
      {
        key: 'agent_id',
        label: t('manage:deployments.field_agent'),
        type: 'select',
        required: true,
        idTag: 'agent_id',
        placeholder: t('manage:deployments.select_agent'),
        options: agents.map((agent) => ({ value: agent.agent_id, label: agent.name })),
        get: (value) => value.agent_id,
        set: (value, next) => ({ ...value, agent_id: next }),
        help: t('manage:deployments.agent_help'),
      },
      {
        key: 'scene',
        label: t('manage:deployments.field_scene'),
        type: 'select',
        required: true,
        idTag: 'scene',
        options: [
          ...BUILTIN_SCENES.map((scene) => ({
            value: scene,
            label: t(`manage:deployments.scene_${scene}`),
          })),
          ...channelProviders.map((provider) => ({
            value: provider.scene,
            label: provider.label,
          })),
        ],
        get: (value) => value.scene,
        set: (value, next) => ({
          ...value,
          scene: next,
          channel_config: {},
          credentials: {},
        }),
        help: t('manage:deployments.scene_help'),
      },
    ];
    if (draftScene === 'schedule') {
      fields.push({
        key: 'name',
        label: t('manage:deployments.field_name'),
        type: 'text',
        required: true,
        idTag: 'name',
        get: (value) => value.name,
        set: (value, next) => ({ ...value, name: next }),
        help: t('manage:deployments.name_help'),
      });
    }
    fields.push({
      key: 'prompt_prefix',
      label:
        draftScene === 'schedule'
          ? t('manage:deployments.field_prompt')
          : t('manage:deployments.field_prompt_prefix'),
      type: 'textarea',
      required: draftScene === 'schedule',
      idTag: 'prompt_prefix',
      rows: 3,
      get: (value) => value.prompt_prefix,
      set: (value, next) => ({ ...value, prompt_prefix: next }),
      help:
        draftScene === 'schedule'
          ? t('manage:deployments.prompt_help')
          : t('manage:deployments.prompt_prefix_help'),
    });
    if (draftScene === 'schedule') {
      fields.push(
        {
          key: 'cron',
          label: t('manage:deployments.field_cron'),
          type: 'text',
          required: true,
          idTag: 'cron',
          placeholder: '0 9 * * *',
          get: (value) => value.cron,
          set: (value, next) => ({ ...value, cron: next }),
          help: t('manage:deployments.cron_help'),
        },
        {
          key: 'timezone',
          label: t('manage:deployments.field_timezone'),
          type: 'text',
          required: true,
          idTag: 'timezone',
          placeholder: 'America/Los_Angeles',
          get: (value) => value.timezone,
          set: (value, next) => ({ ...value, timezone: next }),
          help: t('manage:deployments.timezone_help'),
        },
      );
    }

    const output: EditSectionSpec[] = [
      { label: t('manage:deployments.create_section'), fields },
    ];
    if (selectedProvider) {
      const providerFields = [
        ...selectedProvider.config_fields.map((field) =>
          channelEditField(field, 'channel_config'),
        ),
        ...selectedProvider.credential_fields.map((field) =>
          channelEditField(field, 'credentials'),
        ),
      ];
      if (providerFields.length) {
        output.push({
          label: (
            <span className="inline-flex flex-wrap items-center gap-x-3 gap-y-1">
              <span>{selectedProvider.label}</span>
              {selectedProvider.setup_url && (
                <a
                  className="t-link text-xs font-normal"
                  href={selectedProvider.setup_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  {t('manage:deployments.open_provider_console')}
                </a>
              )}
            </span>
          ),
          fields: providerFields,
        });
      }
    }
    return output;
  }, [agents, channelProviders, draftScene, selectedProvider, t]);

  if (loading) {
    return (
      <ConsoleRecordPage title={t('manage:deployments.create_title')}>
        <ConsoleRecordLoading />
      </ConsoleRecordPage>
    );
  }

  if (!draft) {
    return (
      <ConsoleRecordPage title={t('manage:deployments.create_title')}>
        <ConsoleErrorState detail={loadError} onRetry={() => void load()} />
      </ConsoleRecordPage>
    );
  }

  const agentId = String(draft.agent_id ?? '').trim();
  const scene = String(draft.scene ?? '').trim();
  const name = String(draft.name ?? '').trim();
  const prompt = String(draft.prompt_prefix ?? '').trim();
  const cron = String(draft.cron ?? '').trim();
  const timezone = String(draft.timezone ?? '').trim();
  const missingChannelField = selectedProvider
    ? firstMissingChannelField(draft, 'channel_config', selectedProvider.config_fields) ??
      firstMissingChannelField(draft, 'credentials', selectedProvider.credential_fields)
    : undefined;

  let blockedReason: string | undefined;
  if (!agents.length) blockedReason = t('manage:deployments.empty_hint_no_agents');
  else if (!agentId) blockedReason = t('manage:deployments.err_agent_required');
  else if (!scene) blockedReason = t('manage:deployments.err_scene_required');
  else if (scene.startsWith('channel:') && !selectedProvider)
    blockedReason = t('manage:deployments.err_channel_provider_unavailable');
  else if (scene === 'schedule' && !name)
    blockedReason = t('manage:deployments.err_name_required');
  else if (scene === 'schedule' && !prompt)
    blockedReason = t('manage:deployments.err_prompt_required');
  else if (scene === 'schedule' && !cron)
    blockedReason = t('manage:deployments.err_cron_required');
  else if (scene === 'schedule' && !timezone)
    blockedReason = t('manage:deployments.err_timezone_required');
  else if (missingChannelField)
    blockedReason = t('manage:deployments.err_channel_field_required', {
      field: missingChannelField.label,
    });

  return (
    <ConsoleCreatePage
      title={t('manage:deployments.create_title')}
      lede={t('manage:deployments.create_lede')}
      sections={sections}
      draft={draft}
      onDraftChange={setDraft}
      idPrefix="deployment-new"
      invalidKeys={invalidKeys}
      onInvalidChange={(key, invalid) =>
        setInvalidKeys((previous) => {
          const next = new Set(previous);
          if (invalid) next.add(key);
          else next.delete(key);
          return next;
        })
      }
      error={error}
      saving={saving}
      createLabel={t('common:create')}
      blockedReason={blockedReason}
      onCancel={() => navigate('/manage/deployments')}
      onCreate={() => {
        setSaving(true);
        setError('');
        void createAgentDeployment(agentId, {
          scene,
          name: scene === 'schedule' ? name : undefined,
          prompt_prefix: prompt || undefined,
          channel_config: selectedProvider
            ? channelFieldValues(draft, 'channel_config', selectedProvider.config_fields)
            : undefined,
          credentials: selectedProvider?.credential_fields.length
            ? channelFieldValues(draft, 'credentials', selectedProvider.credential_fields)
            : undefined,
          schedule: scene === 'schedule' ? { cron, timezone } : undefined,
        })
          .then((created) =>
            navigate(`/manage/deployments/${created.deployment_id}`, {
              state: { secret: created.secret },
            }),
          )
          .catch((saveFailure: unknown) => setError((saveFailure as Error).message))
          .finally(() => setSaving(false));
      }}
    />
  );
}
