import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import i18n from '@/i18n';
import {
  adminDescribeSandboxIdleAction,
  getEnvironmentSchema,
  listAdminEnvironments,
  upsertAdminEnvironment,
} from '@/api';
import type { EnvironmentConfig, FormSchema } from '@/types';

import { ErrorNote } from '@/components/shell';
import {
  ConsoleEditSections,
  ConsoleFact,
  ConsoleFactRail,
  ConsoleRecordLoading,
  ConsoleRecordPage,
  ConsoleErrorState,
  useRecordCrumb,
  sectionIsDirty,
  missingRequiredFields,
  revertFields,
  type EditFieldSpec,
  type EditSectionSpec,
} from './console';
import { envDisplayName, formatDateTime } from './environmentConfig';
import { buildEnvEditSections } from './environmentEditConfig';
import { useFrontendReleaseHold } from '@/hooks/useFrontendReleaseHold';

/** The edit helpers speak plain records; a typed draft is one. */
const draftRecordOf = (v: unknown) => (v ?? null) as Record<string, unknown> | null;

/**
 * One Environment, on its own page.
 *
 * The list hands the record over completely (docs/frontend-design.md §3): this
 * is a form, and a form wants its labels beside its controls and its controls
 * wide enough to read a value in — which a panel sharing the width with a table
 * cannot give it.
 *
 * Each schema group is a card that owns its own save (§4). Nothing here has a
 * view mode: the fields are the controls from the moment the page opens.
 */
export default function EnvironmentDetailPage() {
  const { name = '' } = useParams();
  const { t } = useTranslation();

  const [record, setRecord] = useState<EnvironmentConfig | null>(null);
  const [schema, setSchema] = useState<FormSchema | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [draft, setDraft] = useState<EnvironmentConfig | null>(null);
  const [savingSection, setSavingSection] = useState<number | null>(null);
  const [savedSection, setSavedSection] = useState<number | null>(null);
  const [saveError, setSaveError] = useState('');
  const [invalidKeys, setInvalidKeys] = useState<Set<string>>(new Set());
  const savedTimer = useRef<number | null>(null);

  useRecordCrumb(record ? envDisplayName(record) : t('common:environment'));

  const [idleActions, setIdleActions] = useState<string[] | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [list, sch] = await Promise.all([
        listAdminEnvironments(),
        getEnvironmentSchema().catch(() => null),
      ]);
      const found = list.find((e) => e.name === name) ?? null;
      setRecord(found);
      setSchema(sch);
      setError(found ? '' : t('manage:environments.not_found', { name }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [name, t]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(
    () => () => {
      if (savedTimer.current) window.clearTimeout(savedTimer.current);
    },
    [],
  );

  // Seeded from the record, and re-seeded only when the record's identity
  // changes — a save reloads the list, and re-seeding on every load would wipe
  // edits pending in another card.
  const seededFor = useRef<string | null>(null);
  useEffect(() => {
    if (seededFor.current === name && draft) return;
    seededFor.current = name;
    setDraft(record ? { ...record } : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name, record]);

  const activeBackend = String((draft ?? record)?.sandbox_backend || '').trim();

  useEffect(() => {
    let alive = true;
    setIdleActions(null);
    void adminDescribeSandboxIdleAction(activeBackend || undefined)
      .then((state) => alive && setIdleActions(state.supported_actions ?? null))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [activeBackend]);

  const sections: EditSectionSpec[] = useMemo(
    () =>
      buildEnvEditSections(schema, {
        nameEditable: false,
        idleActions,
      }).sections,
    [schema, idleActions, t],
  );

  const isDirty = (fields: EditFieldSpec[]) =>
    sectionIsDirty(fields, draftRecordOf(draft), draftRecordOf(record));
  useFrontendReleaseHold(sections.some((section) => isDirty(section.fields)));

  const missingRequired = useMemo(
    () => missingRequiredFields(sections, draftRecordOf(draft)),
    [sections, draft],
  );

  const saveSection = async (fields: EditFieldSpec[], index: number) => {
    if (!draft || !record) return;
    setSavingSection(index);
    setSaveError('');
    try {
      const payload = fields.reduce(
        (acc, f) =>
          f.editable === false
            ? acc
            : f.set(acc, f.get(draft as unknown as Record<string, unknown>)),
        { ...record } as Record<string, unknown>,
      ) as EnvironmentConfig;
      await upsertAdminEnvironment(record.name, { ...payload, name: record.name });
      await load();
      setSavedSection(index);
      if (savedTimer.current) window.clearTimeout(savedTimer.current);
      savedTimer.current = window.setTimeout(() => setSavedSection(null), 2600);
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSavingSection(null);
    }
  };

  const revertSection = (fields: EditFieldSpec[]) => {
    if (!draft || !record) return;
    setDraft(
      revertFields(
        fields,
        draftRecordOf(draft)!,
        draftRecordOf(record)!,
      ) as unknown as EnvironmentConfig,
    );
  };

  if (loading && !record) {
    return (
      <ConsoleRecordPage title={t('common:environment')}>
        <ConsoleRecordLoading />
      </ConsoleRecordPage>
    );
  }

  if (!record) {
    return (
      <ConsoleRecordPage title={t('common:environment')}>
        <ConsoleErrorState
          title={t('manage:environments.error_title')}
          detail={error}
          onRetry={() => void load()}
        />
      </ConsoleRecordPage>
    );
  }

  const draftRecord = draft as unknown as Record<string, unknown> | undefined;
  return (
    <ConsoleRecordPage
      title={envDisplayName(record)}
      lede={String(record.description || '') || undefined}
      status={
        record.engine_available === false
          ? { tone: 'failed', label: t('manage:environments.engine_missing') }
          : record.enabled !== false
            ? { tone: 'done', label: t('common:enabled') }
            : { tone: 'idle', label: t('common:disabled') }
      }
      rail={
        <ConsoleFactRail>
          <ConsoleFact
            label={t('common:updated_at')}
            value={formatDateTime(String(record.updated_at || '')) || '—'}
          />
        </ConsoleFactRail>
      }
    >
      {saveError && <ErrorNote>{saveError}</ErrorNote>}
      <ConsoleEditSections
        sections={sections}
        draft={draftRecord ?? {}}
        onDraftChange={(d) => setDraft(d as unknown as EnvironmentConfig)}
        idPrefix="environment"
        invalidKeys={invalidKeys}
        onInvalidChange={(key, invalid) =>
          setInvalidKeys((prev) => {
            const next = new Set(prev);
            if (invalid) next.add(key);
            else next.delete(key);
            return next;
          })
        }
        cardProps={(section, index) => ({
          intro: schema?.groups[index]?.id
            ? i18n.t(`manage:env_form.groups.${schema.groups[index].id}.description`, { defaultValue: '' })
            : undefined,
          note: t('manage:environments.card_note'),
          dirty: isDirty(section.fields),
          blocked:
            section.fields.some((f) => invalidKeys.has(f.key)) || missingRequired.length > 0,
          blockedReason:
            missingRequired.length > 0
              ? t('manage:console.blocked_by_required', {
                  field: String(missingRequired[0].label),
                })
              : undefined,
          saving: savingSection === index,
          saved: savedSection === index,
          saveLabel: t('common:save'),
          revertLabel: t('common:revert'),
          onSave: () => void saveSection(section.fields, index),
          onRevert: () => revertSection(section.fields),
        })}
      />
    </ConsoleRecordPage>
  );
}
