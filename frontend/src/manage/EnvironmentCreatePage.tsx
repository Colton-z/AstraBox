import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useSWRConfig } from 'swr';

import {
  adminDescribeSandboxIdleAction,
  getEnvironmentSchema,
  listAdminEnvironments,
  upsertAdminEnvironment,
} from '@/api';
import type { EnvironmentConfig, FormSchema } from '@/types';

import {
  ConsoleCreatePage,
  ConsoleErrorState,
  ConsoleRecordLoading,
  ConsoleRecordPage,
  useRecordCrumb,
} from './console';
import { buildEnvironmentDraft } from './environmentConfig';
import { buildEnvEditSections } from './environmentEditConfig';
import { MANAGE_NAV_COUNT_KEYS } from './navCounts';

/**
 * A new Environment, on its own page — the same fields the record page reads
 * (§4), with the name editable because that is what is being decided here.
 *
 * The name is also the environment's key, so a collision is refused before the
 * write rather than after: `upsertAdminEnvironment` would silently overwrite
 * the one already holding that name.
 */
export default function EnvironmentCreatePage() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { mutate } = useSWRConfig();

  const [draft, setDraft] = useState<EnvironmentConfig | null>(null);
  const [schema, setSchema] = useState<FormSchema | null>(null);
  const [taken, setTaken] = useState<string[]>([]);
  const [idleActions, setIdleActions] = useState<string[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [invalidKeys, setInvalidKeys] = useState<Set<string>>(new Set());

  useRecordCrumb(t('manage:environments.create_title'));

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError('');
    try {
      // The schema is the form, and the existing names are the whole of the
      // collision guard below. An unknown name list is not an empty one: an
      // empty one clears every name, so the create reaches
      // `upsertAdminEnvironment` and overwrites whichever environment already
      // holds it — which is why neither failure is absorbed here. The two
      // The idle-action probe is the declared exception: an unanswered probe
      // leaves the schema's supported choices on offer.
      const [sch, existing, idle] = await Promise.all([
        getEnvironmentSchema(),
        listAdminEnvironments(),
        adminDescribeSandboxIdleAction().catch(() => null),
      ]);
      setSchema(sch);
      setTaken(existing.map((env) => env.name));
      setIdleActions(idle?.supported_actions ?? null);
      setDraft(buildEnvironmentDraft());
    } catch (e) {
      setLoadError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const sections = useMemo(
    () =>
      buildEnvEditSections(schema, {
        nameEditable: true,
        idleActions,
      }).sections,
    [schema, idleActions, t],
  );

  if (loading) {
    return (
      <ConsoleRecordPage title={t('manage:environments.create_title')}>
        <ConsoleRecordLoading />
      </ConsoleRecordPage>
    );
  }

  // A draft is built only once the schema and the taken names have both landed,
  // so its absence here is the failed load — the same state, and the same
  // error card with a Retry, that the record page this becomes renders.
  if (!draft) {
    return (
      <ConsoleRecordPage title={t('manage:environments.create_title')}>
        <ConsoleErrorState detail={loadError} onRetry={() => void load()} />
      </ConsoleRecordPage>
    );
  }

  const name = String(draft.name || '').trim();
  const blockedReason = !name
    ? t('manage:environments.err_name_required')
    : taken.includes(name)
      ? t('manage:environments.err_name_exists', { name })
      : undefined;

  return (
    <ConsoleCreatePage
      title={t('manage:environments.create_title')}
      sections={sections}
      draft={draft as unknown as Record<string, unknown>}
      onDraftChange={(d) => setDraft(d as unknown as EnvironmentConfig)}
      idPrefix="environment-new"
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
      blockedReason={blockedReason}
      onCancel={() => navigate('/manage/environments')}
      onCreate={() => {
        setSaving(true);
        setError('');
        void upsertAdminEnvironment(name, { ...draft, name })
          .then(() => {
            void mutate(MANAGE_NAV_COUNT_KEYS.environments);
            navigate(`/manage/environments/${encodeURIComponent(name)}`);
          })
          .catch((e: unknown) => setError((e as Error).message))
          .finally(() => setSaving(false));
      }}
    />
  );
}
