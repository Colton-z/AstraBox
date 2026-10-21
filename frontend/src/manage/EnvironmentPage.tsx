import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  getAdminEnvironment, getCurrentEnvironment, setAdminEnvironment,
  type AdminEnvironment, type CurrentEnvironment,
} from '@/api';
import { useFrontendReleaseHold } from '@/hooks/useFrontendReleaseHold';
import {
  ConsoleEditSections, ConsoleErrorState, ConsoleFact, ConsoleFactRail,
  ConsoleRecordLoading, ConsoleRecordPage, sectionIsDirty, type EditSectionSpec,
} from './console';

function draftOf(view: AdminEnvironment): Record<string, unknown> {
  return {
    name: view.name, display_name: view.display_name,
    description: view.description ?? '', member_user_ids: view.member_user_ids,
  };
}

export default function EnvironmentPage() {
  const { t } = useTranslation();
  const [view, setView] = useState<AdminEnvironment | null>(null);
  const [current, setCurrent] = useState<CurrentEnvironment | null>(null);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [error, setError] = useState('');
  const [saveError, setSaveError] = useState('');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [invalidKeys, setInvalidKeys] = useState<Set<string>>(new Set());
  const load = useCallback(async () => {
    setError('');
    try {
      const environment = await getCurrentEnvironment();
      const record = await getAdminEnvironment(environment.name);
      setCurrent(environment);
      setView(record);
      setDraft(draftOf(record));
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const sections: EditSectionSpec[] = useMemo(() => [{
    label: t('manage:environment.section'),
    fields: [
      {
        key: 'name', label: t('manage:environment.name'), type: 'text',
        editable: false, get: (d) => d.name, set: (d) => d,
        help: t('manage:environment.name_help'),
      },
      {
        key: 'display_name', label: t('common:name'), type: 'text', required: true,
        get: (d) => d.display_name, set: (d, value) => ({ ...d, display_name: value }),
      },
      {
        key: 'description', label: t('manage:environment.description'), type: 'textarea',
        get: (d) => d.description, set: (d, value) => ({ ...d, description: value }),
      },
      {
        key: 'member_user_ids', label: t('manage:environment.members'), type: 'list',
        get: (d) => d.member_user_ids,
        set: (d, value) => ({ ...d, member_user_ids: Array.isArray(value) ? value.map(String) : [] }),
        placeholder: t('manage:agent_form.user_id_placeholder'),
        help: t('manage:environment.members_help'),
      },
    ],
  }], [t]);
  const dirty = sectionIsDirty(sections[0].fields, draft, view ? draftOf(view) : null);
  useFrontendReleaseHold(dirty || saving);

  const save = async () => {
    if (!view) return;
    setSaving(true);
    setSaveError('');
    setSaved(false);
    try {
      const record = await setAdminEnvironment(view.name, {
        display_name: String(draft.display_name ?? ''),
        description: String(draft.description ?? '') || null,
        member_user_ids: (draft.member_user_ids as string[]), version: view.version,
      });
      setView(record);
      setDraft(draftOf(record));
      setSaved(true);
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const title = t('manage:environment.title');
  if (error) return (
    <ConsoleRecordPage title={title}>
      <ConsoleErrorState title={t('manage:environment.load_failed')} detail={error} onRetry={() => void load()} />
    </ConsoleRecordPage>
  );
  if (!view || !current) return (
    <ConsoleRecordPage title={title}><ConsoleRecordLoading /></ConsoleRecordPage>
  );
  const policy = current.runtime_policy;
  return (
    <ConsoleRecordPage title={title} lede={t('manage:environment.lede')} rail={
      <ConsoleFactRail>
        <ConsoleFact label={t('manage:environment.prewarm')} value={t(policy.agent_prewarm_enabled ? 'common:enabled' : 'common:disabled')} />
        <ConsoleFact label={t('manage:environment.background')} value={t(policy.background_execution_enabled ? 'common:enabled' : 'common:disabled')} />
      </ConsoleFactRail>
    }>
      <ConsoleEditSections
        sections={sections} draft={draft} idPrefix="environment" invalidKeys={invalidKeys}
        onDraftChange={(value) => { setDraft(value); setSaved(false); }}
        onInvalidChange={(key, invalid) => setInvalidKeys((previous) => {
          const next = new Set(previous);
          if (invalid) next.add(key); else next.delete(key);
          return next;
        })}
        cardProps={() => ({
          dirty, saving, saved, blocked: invalidKeys.size > 0, error: saveError,
          saveLabel: t('common:save'), revertLabel: t('common:revert'),
          onSave: () => void save(),
          onRevert: () => { setDraft(draftOf(view)); setSaveError(''); setSaved(false); },
        })}
      />
    </ConsoleRecordPage>
  );
}
