import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import {
  createAgentDeployment,
  createAssistantDeployment,
  listAgents,
  listChannelProviders,
} from '@/api';
import { listAssistants } from '@/assistant/api';
import type { AssistantRecord } from '@/assistant/types';
import type { AgentConfig, AgentDeployment, ChannelExecutionMode, ChannelProviderDescriptor } from '@/types';

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
  channelParticipantHelp,
  channelExecutionModeField,
  channelExecutionUserField,
  channelFieldValues,
  defaultExecutionMode,
  firstMissingChannelField,
} from './channelProviderFields';

const BUILTIN_SCENES = ['schedule', 'hmac', 'scheduler'] as const;

/** A target select value: ``agent:<id>`` or ``assistant:<id>``. */
function parseTarget(value: unknown): { kind: 'agent' | 'assistant'; id: string } | null {
  const [kind, ...rest] = String(value ?? '').split(':');
  const id = rest.join(':').trim();
  if (!id || (kind !== 'agent' && kind !== 'assistant')) return null;
  return { kind, id };
}

function participantList(value: unknown): string[] {
  return String(value ?? '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

/** A new built-in schedule, authenticated trigger, or concrete chat binding. */
export default function DeploymentCreatePage() {
  const navigate = useNavigate();
  const { t } = useTranslation();

  const [agents, setAgents] = useState<AgentConfig[]>([]);
  const [assistants, setAssistants] = useState<AssistantRecord[]>([]);
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
      const [visibleAgents, assistantList] = await Promise.all([
        listAgents(),
        listAssistants(),
      ]);
      const agentList = visibleAgents.filter((agent) => agent.can_manage);
      setAgents(agentList);
      setAssistants(assistantList);
      const firstTarget = agentList[0]
        ? `agent:${agentList[0].agent_id}`
        : assistantList[0]
          ? `assistant:${assistantList[0].assistant_id}`
          : '';
      setDraft({
        target: firstTarget,
        allowed_participants: '',
        scene: BUILTIN_SCENES[0],
        name: '',
        prompt_prefix: '',
        attention_policy: 'all',
        response_instruction: '',
        cron: '0 9 * * *',
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
        execution_mode: '',
        execution_user_id: '',
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
  const draftExecutionMode = String(draft?.execution_mode ?? '');
  const draftAttentionPolicy = String(draft?.attention_policy ?? 'all');
  const usesResponseInstruction =
    draftAttentionPolicy === 'participated' || draftAttentionPolicy === 'proactive';
  const draftTarget = parseTarget(draft?.target);
  const onAssistant = draftTarget?.kind === 'assistant';
  const providerAgentId = draftTarget?.kind === 'agent' ? draftTarget.id : undefined;
  useEffect(() => {
    let cancelled = false;
    setChannelProviders([]);
    void listChannelProviders(providerAgentId).then((providers) => {
      if (!cancelled) setChannelProviders(providers);
    }).catch((failure: Error) => {
      if (!cancelled) setLoadError(failure.message);
    });
    return () => { cancelled = true; };
  }, [providerAgentId]);
  const selectedProvider = channelProviders.find((provider) => provider.scene === draftScene);
  const sections = useMemo<EditSectionSpec[]>(() => {
    const fields: EditSectionSpec['fields'] = [
      {
        key: 'target',
        label: t('manage:deployments.field_target'),
        type: 'select',
        required: true,
        idTag: 'target',
        placeholder: t('manage:deployments.select_agent'),
        options: [
          ...agents.map((agent) => ({ value: `agent:${agent.agent_id}`, label: agent.name })),
          ...assistants.map((assistant) => ({
            value: `assistant:${assistant.assistant_id}`,
            label: t('manage:deployments.target_assistant_label', {
              name: assistant.display_name || assistant.assistant_id,
            }),
          })),
        ],
        get: (value) => value.target,
        set: (value, next) => ({ ...value, target: next }),
        help: t('manage:deployments.target_help'),
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
          execution_mode: defaultExecutionMode(
            channelProviders.find((provider) => provider.scene === next)?.execution_modes,
          ),
          execution_user_id: '',
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
      const canConfigure = selectedProvider.can_configure_execution_user === true;
      const providerFields = [
        {
          key: 'attention_policy',
          label: t('manage:deployments.field_attention_policy'),
          type: 'select' as const,
          options: ['all', 'mentions', 'participated', 'proactive'].map((policy) => ({
            value: policy,
            label: `${t(`manage:deployments.attention_policy_${policy}`)} — ${t(`manage:deployments.attention_policy_${policy}_help`)}`,
          })),
          get: (value: Record<string, unknown>) => value.attention_policy,
          set: (value: Record<string, unknown>, next: unknown) => ({
            ...value,
            attention_policy: next,
          }),
        },
        ...(usesResponseInstruction
          ? [{
              key: 'response_instruction',
              label: t('manage:deployments.field_response_instruction'),
              type: 'textarea' as const,
              rows: 3,
              get: (value: Record<string, unknown>) => value.response_instruction,
              set: (value: Record<string, unknown>, next: unknown) => ({
                ...value,
                response_instruction: next,
              }),
              help: t('manage:deployments.response_instruction_help'),
            }]
          : []),
        ...(onAssistant
          ? []
          : [
              channelExecutionModeField(t, selectedProvider.execution_modes, canConfigure),
              ...(draftExecutionMode === 'account'
                ? [channelExecutionUserField(t, canConfigure)]
                : []),
            ]),
        {
          key: 'allowed_participants',
          label: t('manage:deployments.field_allowed_participants'),
          type: 'textarea' as const,
          required: onAssistant,
          idTag: 'allowed_participants',
          rows: 2,
          placeholder: selectedProvider?.participant_example || undefined,
          get: (value: Record<string, unknown>) => value.allowed_participants,
          set: (value: Record<string, unknown>, next: unknown) => ({
            ...value,
            allowed_participants: next,
          }),
          help: `${t('manage:deployments.allowed_participants_help')} ${channelParticipantHelp(t, selectedProvider)}`,
        },
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
  }, [
    agents, assistants, channelProviders, draftExecutionMode, draftScene, onAssistant,
    selectedProvider, t, usesResponseInstruction,
  ]);

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

  const target = parseTarget(draft.target);
  const scene = String(draft.scene ?? '').trim();
  const participants = participantList(draft.allowed_participants);
  const name = String(draft.name ?? '').trim();
  const prompt = String(draft.prompt_prefix ?? '').trim();
  const cron = String(draft.cron ?? '').trim();
  const timezone = String(draft.timezone ?? '').trim();
  const responseInstruction = String(draft.response_instruction ?? '').trim();
  const missingChannelField = selectedProvider
    ? firstMissingChannelField(draft, 'channel_config', selectedProvider.config_fields) ??
      firstMissingChannelField(draft, 'credentials', selectedProvider.credential_fields)
    : undefined;

  let blockedReason: string | undefined;
  if (!agents.length && !assistants.length)
    blockedReason = t('manage:deployments.empty_hint_no_targets');
  else if (!target) blockedReason = t('manage:deployments.err_agent_required');
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
  else if (
    selectedProvider && target?.kind === 'agent' && draftExecutionMode === 'account'
    && !String(draft.execution_user_id ?? '').trim()
  )
    blockedReason = t('manage:deployments.err_execution_user_required');
  else if (missingChannelField)
    blockedReason = t('manage:deployments.err_channel_field_required', {
      field: missingChannelField.label,
    });
  else if (selectedProvider && target?.kind === 'assistant' && !participants.length)
    blockedReason = t('manage:deployments.err_allowed_participants_required');
  else if (selectedProvider && usesResponseInstruction && [...responseInstruction].length > 4000)
    blockedReason = t('manage:deployments.err_response_instruction_too_long');

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
        if (!target) return;
        const payload = {
          scene,
          ...(selectedProvider?.can_configure_execution_user && target.kind === 'agent'
            ? {
                execution_mode: (draftExecutionMode || undefined) as ChannelExecutionMode | undefined,
                execution_user_id: draftExecutionMode === 'account'
                  ? String(draft.execution_user_id ?? '').trim() : undefined,
              }
            : {}),
          allowed_participants: selectedProvider && participants.length ? participants : undefined,
          name: scene === 'schedule' ? name : undefined,
          prompt_prefix: prompt || undefined,
          ...(selectedProvider
            ? {
                attention_policy: draftAttentionPolicy as AgentDeployment['attention_policy'],
                response_instruction: usesResponseInstruction ? responseInstruction || null : null,
              }
            : {}),
          channel_config: selectedProvider
            ? channelFieldValues(draft, 'channel_config', selectedProvider.config_fields)
            : undefined,
          credentials: selectedProvider?.credential_fields.length
            ? channelFieldValues(draft, 'credentials', selectedProvider.credential_fields)
            : undefined,
          schedule: scene === 'schedule' ? { cron, timezone } : undefined,
        };
        void (target.kind === 'assistant'
          ? createAssistantDeployment(target.id, payload)
          : createAgentDeployment(target.id, payload))
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
