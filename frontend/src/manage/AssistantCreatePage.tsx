import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { createAssistant } from '@/assistant/api';
import type { EnvironmentConfig } from '@/types';

import {
  ConsoleCreatePage,
  ConsoleErrorState,
  ConsoleRecordLoading,
  ConsoleRecordPage,
  useRecordCrumb,
} from './console';
import {
  buildAssistantDraft,
  loadAssistantEnvironments,
  type AssistantDraft,
} from './assistantConfig';
import { buildAssistantCreateSections } from './assistantEditConfig';

/**
 * A new Assistant, on its own page.
 *
 * Fewer fields than the record page it becomes, and that is not an
 * inconsistency with docs/frontend-design.md §4: engine and environment are
 * frozen identity, decided here and only reported there. Everything else a
 * create page offers is offered again as the record page's own controls once
 * the record exists.
 */
export default function AssistantCreatePage() {
  const navigate = useNavigate();
  const { t } = useTranslation();

  const [draft, setDraft] = useState<AssistantDraft | null>(null);
  const [environments, setEnvironments] = useState<EnvironmentConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [invalidKeys, setInvalidKeys] = useState<Set<string>>(new Set());

  useRecordCrumb(t('manage:assistants.create_title'));

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError('');
    try {
      // The list contains only environments backed by an installed adapter
      // that declares assistant_chat. No unverifiable free-text fallback.
      setEnvironments(await loadAssistantEnvironments());
      setDraft(buildAssistantDraft());
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
    () => buildAssistantCreateSections(environments),
    [environments, t],
  );

  if (loading) {
    return (
      <ConsoleRecordPage title={t('manage:assistants.create_title')}>
        <ConsoleRecordLoading />
      </ConsoleRecordPage>
    );
  }

  // A draft is built only once the environment list has landed, so its absence
  // here is the failed load — the same error card, with the same Retry, that
  // the record page this becomes renders.
  if (!draft) {
    return (
      <ConsoleRecordPage title={t('manage:assistants.create_title')}>
        <ConsoleErrorState detail={loadError} onRetry={() => void load()} />
      </ConsoleRecordPage>
    );
  }

  const name = draft.display_name.trim();
  const environment = draft.environment_name.trim();
  const blockedReason = !name
    ? t('manage:assistants.err_name_required')
    : !environment
      ? t('manage:assistants.err_environment_required')
      : undefined;

  return (
    <ConsoleCreatePage
      title={t('manage:assistants.create_title')}
      sections={sections}
      draft={draft as unknown as Record<string, unknown>}
      onDraftChange={(d) => setDraft(d as unknown as AssistantDraft)}
      idPrefix="assistant-new"
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
      onCancel={() => navigate('/manage/assistants')}
      onCreate={() => {
        setSaving(true);
        setError('');
        void createAssistant({
          display_name: name,
          description: draft.description.trim() || undefined,
          engine_kind: draft.engine_kind,
          environment_name: environment,
          permission_mode_default: draft.permission_mode_default,
        })
          .then((created) =>
            navigate(`/manage/assistants/${encodeURIComponent(created.assistant_id)}`),
          )
          .catch((e: unknown) => setError((e as Error).message))
          .finally(() => setSaving(false));
      }}
    />
  );
}
