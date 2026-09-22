import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useSWRConfig } from 'swr';

import {
  createAgent,
  getAgentSchema,
  listAgentEnvironmentModels,
  listAgentEnvironments,
} from '@/api';
import type { AgentDraft, EnvironmentConfig, FormSchema } from '@/types';

import {
  ConsoleCreatePage,
  ConsoleErrorState,
  ConsoleRecordLoading,
  ConsoleRecordPage,
  useRecordCrumb,
} from './console';
import { buildAgentDraft } from './agentConfig';
import { buildAgentEditSections } from './agentEditConfig';
import { MANAGE_NAV_COUNT_KEYS } from './navCounts';

/**
 * A new Agent, on its own page.
 *
 * Same fields and same sections as the record page it becomes (§4). The name is
 * editable here and nowhere else: it is the record's identity once the record
 * exists.
 */
export default function AgentCreatePage() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { mutate } = useSWRConfig();

  const [draft, setDraft] = useState<AgentDraft | null>(null);
  const [schema, setSchema] = useState<FormSchema | null>(null);
  const [environments, setEnvironments] = useState<EnvironmentConfig[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [invalidKeys, setInvalidKeys] = useState<Set<string>>(new Set());

  useRecordCrumb(t('manage:agents.create_title'));

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError('');
    try {
      // Neither of these is optional to the form: without the schema
      // `buildAgentEditSections` has no sections to return and the page renders
      // a heading over nothing, and without the environment list the
      // environment picker offers no environment. Absorbing either would report
      // a fetch that failed as a product that has nothing to show.
      const [sch, envs] = await Promise.all([getAgentSchema(), listAgentEnvironments()]);
      setSchema(sch);
      setEnvironments(envs);
      setDraft(buildAgentDraft());
    } catch (e) {
      setLoadError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const environmentName = String(draft?.environment_name || '').trim();
  useEffect(() => {
    let current = true;
    if (!environmentName) {
      setModels([]);
      return () => {
        current = false;
      };
    }
    void listAgentEnvironmentModels(environmentName)
      .then((items) => {
        if (current) setModels(items);
      })
      .catch(() => {
        if (current) setModels([]);
      });
    return () => {
      current = false;
    };
  }, [environmentName]);

  // Only `visibility` changes which fields the config produces (it toggles the
  // allowlist), so the memo keys on that value rather than the whole draft —
  // otherwise every keystroke rebuilds every section's closures.
  const draftVisibility = (draft as Record<string, unknown> | null)?.visibility;
  // The engine_options fields come from the engine declaration of whichever
  // environment is selected, so the memo also keys on the chosen environment
  // name.
  const { sections, advancedKeys } = useMemo(
    () =>
      buildAgentEditSections(schema, { visibility: draftVisibility }, {
        nameEditable: true,
        environments,
        models,
        engineOptionsSchema: environments.find((e) => e.name === environmentName)
          ?.engine_options_schema,
      }),
    [schema, draftVisibility, environments, models, environmentName, t],
  );

  if (loading) {
    return (
      <ConsoleRecordPage title={t('manage:agents.create_title')}>
        <ConsoleRecordLoading />
      </ConsoleRecordPage>
    );
  }

  // A draft is built only once the schema has landed, so its absence here is
  // the failed load — the same error card, with the same Retry, that the record
  // page this becomes renders.
  if (!draft || !schema) {
    return (
      <ConsoleRecordPage title={t('manage:agents.create_title')}>
        <ConsoleErrorState detail={loadError} onRetry={() => void load()} />
      </ConsoleRecordPage>
    );
  }

  const name = String(draft.name || '').trim();

  return (
    <ConsoleCreatePage
      title={t('manage:agents.create_title')}
      sections={sections}
      // Creating an Agent is where someone meets this form for the first time;
      // the Environment it names has already settled the sandbox, the gateway
      // and the tracing destination, so most of what is advanced here is a
      // question they do not have to answer yet. The record page folds nothing:
      // there each section owns its own save.
      advancedKeys={advancedKeys}
      draft={draft as unknown as Record<string, unknown>}
      onDraftChange={(d) => setDraft(d as unknown as AgentDraft)}
      idPrefix="agent-new"
      invalidKeys={invalidKeys}
      onInvalidChange={(key, invalid) =>
        setInvalidKeys((prev) => {
          const next = new Set(prev);
          if (invalid) next.add(key);
          else next.delete(key);
          return next;
        })
      }
      error={error}
      saving={saving}
      createLabel={t('common:create')}
      blockedReason={name ? undefined : t('manage:agents.err_name_required')}
      onCancel={() => navigate('/manage/agents')}
      onCreate={() => {
        setSaving(true);
        setError('');
        void createAgent({ ...draft, name }, schema)
          // Straight to the record: what the reader wanted was the agent, and
          // the list they came from cannot show them the one they just made
          // any better than its own page can.
          .then((created) => {
            void mutate(MANAGE_NAV_COUNT_KEYS.agents);
            navigate(`/manage/agents/${encodeURIComponent(created.agent_id)}`);
          })
          .catch((e: unknown) => setError((e as Error).message))
          .finally(() => setSaving(false));
      }}
    />
  );
}
