import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Moon, Power } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import {
  deleteAssistant,
  getAssistant,
  hibernateAssistantWorkspace,
  updateAssistant,
  wakeAssistantWorkspace,
} from '@/assistant/api';
import type { AssistantRecord } from '@/assistant/types';

import { ErrorNote } from '@/components/shell';
import {
  ConsoleEditSections,
  ConsoleDangerButton,
  ConsoleErrorState,
  ConsoleFact,
  ConsoleFactRail,
  ConsoleRecordLoading,
  ConsoleRecordPage,
  useRecordCrumb,
  sectionIsDirty,
  revertFields,
  type EditFieldSpec,
} from './console';
import {
  assistantDisplayName,
  assistantEngineLabel,
  assistantHibernatable,
  assistantMaterializing,
  assistantStateLabel,
  assistantStateTone,
  assistantWakeable,
  formatDateTime,
} from './assistantConfig';
import {
  buildAssistantEditDraft,
  buildAssistantEditSections,
  type AssistantEditDraft,
} from './assistantEditConfig';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';
import { useFrontendReleaseHold } from '@/hooks/useFrontendReleaseHold';

/** The edit helpers speak plain records; a typed draft is one. */
const draftRecordOf = (v: unknown) => (v ?? null) as Record<string, unknown> | null;

/**
 * One Assistant, on its own page.
 *
 * Same shape as the Environment and Agent records (docs/frontend-design.md §3):
 * the list hands the record over, each group of mutable fields is a card that
 * owns its save (§4), and the read-only facts the deployment reports —
 * workspace state, the sandbox it currently holds, when it last changed — sit
 * in the rail beside them rather than mixed in among the controls.
 *
 * The lifecycle buttons are operations on the record, not edits, so they live
 * with the heading and not in a card's footer.
 */
export default function AssistantDetailPage() {
  const { assistantId = '' } = useParams();
  const navigate = useNavigate();
  const { t } = useTranslation();

  const [record, setRecord] = useState<AssistantRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [draft, setDraft] = useState<AssistantEditDraft | null>(null);
  const [savingSection, setSavingSection] = useState<number | null>(null);
  const [savedSection, setSavedSection] = useState<number | null>(null);
  const [saveError, setSaveError] = useState('');
  const [invalidKeys, setInvalidKeys] = useState<Set<string>>(new Set());
  const savedTimer = useRef<number | null>(null);

  const [actionBusy, setActionBusy] = useState(false);
  // Which action is running, kept apart from `actionBusy` because the heading
  // carries Delete beside the workspace buttons: a shared busy flag would put
  // "Deleting…" on screen while a workspace was starting.
  const [deleting, setDeleting] = useState(false);

  useRecordCrumb(record ? assistantDisplayName(record) : t('common:assistant'));

  // True after any successful read of the record (see keepsLastRead).
  const loaded = useRef(false);

  const load = useCallback(async (context?: ReloadContext) => {
    const background = context?.background === true;
    if (!background) setLoading(true);
    try {
      const found = await getAssistant(assistantId);
      setRecord(found);
      setError('');
      loaded.current = true;
    } catch (e) {
      if (keepsLastRead(e, context, loaded.current)) return;
      setRecord(null);
      setError((e as Error).message);
    } finally {
      if (!background) setLoading(false);
    }
  }, [assistantId]);

  useEffect(() => {
    void load();
  }, [load]);
  // Safe on an edit page: the draft below is seeded by record identity, so a
  // re-read while the reader waits on "Start workspace" does not wipe it.
  useKeepCurrent(load, {
    follow: !!record && assistantMaterializing(String(record.workspace_state || '')),
  });

  useEffect(
    () => () => {
      if (savedTimer.current) window.clearTimeout(savedTimer.current);
    },
    [],
  );

  // Re-seeded only when the record's identity changes: a save reloads it, and
  // re-seeding on every load would wipe edits pending in the other card.
  const seededFor = useRef<string | null>(null);
  useEffect(() => {
    if (seededFor.current === assistantId && draft) return;
    seededFor.current = assistantId;
    setDraft(record ? buildAssistantEditDraft(record) : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assistantId, record]);

  const sections = useMemo(() => buildAssistantEditSections(), [t]);
  const baseline = useMemo(
    () => (record ? buildAssistantEditDraft(record) : null),
    [record],
  );

  const isDirty = (fields: EditFieldSpec[]) =>
    sectionIsDirty(fields, draftRecordOf(draft), draftRecordOf(baseline));
  useFrontendReleaseHold(sections.some((section) => isDirty(section.fields)));

  const saveSection = async (index: number) => {
    if (!record || !draft) return;
    if (!draft.display_name.trim()) {
      setSaveError(t('manage:assistants.err_name_required'));
      return;
    }
    setSavingSection(index);
    setSaveError('');
    try {
      // The mutable subset only; '' clears an optional field rather than
      // storing an empty string the reader would see as a blank value.
      await updateAssistant(record.assistant_id, {
        display_name: draft.display_name.trim(),
        description: draft.description.trim() || null,
        icon: draft.icon.trim() || null,
        permission_mode_default: draft.permission_mode_default,
      });
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
    if (!draft || !baseline) return;
    setDraft(
      revertFields(
        fields,
        draftRecordOf(draft)!,
        draftRecordOf(baseline)!,
      ) as unknown as AssistantEditDraft,
    );
  };

  const runAction = async (action: () => Promise<unknown>) => {
    setActionBusy(true);
    setSaveError('');
    try {
      await action();
      await load();
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setActionBusy(false);
    }
  };

  if (loading && !record) {
    return (
      <ConsoleRecordPage title={t('common:assistant')}>
        <ConsoleRecordLoading />
      </ConsoleRecordPage>
    );
  }

  if (!record) {
    return (
      <ConsoleRecordPage title={t('common:assistant')}>
        <ConsoleErrorState
          title={t('manage:assistants.error_title')}
          detail={error}
          onRetry={() => void load()}
        />
      </ConsoleRecordPage>
    );
  }

  const state = String(record.workspace_state || 'NOT_MATERIALIZED');
  const notStarted = state === 'NOT_MATERIALIZED';
  const draftRecord = draft as unknown as Record<string, unknown> | undefined;

  return (
    <ConsoleRecordPage
      title={assistantDisplayName(record)}
      lede={String(record.description || '') || undefined}
      status={{ tone: assistantStateTone(state), label: assistantStateLabel(state) }}
      actions={
        <>
          {assistantMaterializing(state) && (
            <span className="t-label-sm mr-1 text-muted-foreground">
              {t('manage:assistants.materializing')}
            </span>
          )}
          {assistantWakeable(state) && (
            <Button
              disabled={actionBusy}
              onClick={() => void runAction(() => wakeAssistantWorkspace(record.assistant_id))}
            >
              <Power className="size-4" />
              {actionBusy
                ? t(notStarted ? 'manage:assistants.starting_workspace' : 'manage:assistants.resuming_workspace')
                : t(notStarted ? 'manage:assistants.start_workspace' : 'manage:assistants.resume_workspace')}
            </Button>
          )}
          {assistantHibernatable(state) && (
            <Button
              variant="outline"
              disabled={actionBusy}
              onClick={() => void runAction(() => hibernateAssistantWorkspace(record.assistant_id))}
            >
              <Moon className="size-4" />
              {actionBusy ? t('manage:assistants.hibernating_action') : t('manage:assistants.hibernate')}
            </Button>
          )}
          {/* The question is the button's (`confirm`), not this row's: arming in
              place swaps the action row for a confirm strip, which asks in the
              width left beside the heading and moves the workspace controls out
              from under the pointer that is on its way to answer. The reasoning
              lives on ConsoleDangerButton; this is the call site. */}
          <ConsoleDangerButton
            disabled={actionBusy}
            confirm={{
              title: t('manage:assistants.confirm_delete', {
                name: assistantDisplayName(record),
              }),
              action: t('common:confirm_delete'),
              onConfirm: () => {
                setDeleting(true);
                void runAction(async () => {
                  await deleteAssistant(record.assistant_id);
                  navigate('/manage/assistants');
                }).finally(() => setDeleting(false));
              },
            }}
          >
            {deleting ? t('manage:assistants.deleting') : t('common:delete')}
          </ConsoleDangerButton>
        </>
      }
      rail={
        <ConsoleFactRail>
          {/* Engine and environment are frozen identity, not settings — they
              are reported here rather than offered as fields nobody can change. */}
          <ConsoleFact label={t('common:engine')} value={assistantEngineLabel(record)} />
          <ConsoleFact
            label={t('manage:common_fields.environment')}
            value={record.environment_name || '—'}
          />
          <ConsoleFact
            label={t('manage:assistants.field_current_sandbox')}
            value={
              record.current_sandbox_id ? (
                <span className="font-mono">{record.current_sandbox_id}</span>
              ) : (
                '—'
              )
            }
          />
          <ConsoleFact
            label={t('common:updated_at')}
            value={formatDateTime(record.updated_at) || '—'}
          />
        </ConsoleFactRail>
      }
    >
      {saveError && <ErrorNote>{saveError}</ErrorNote>}
      <ConsoleEditSections
        sections={sections}
        draft={draftRecord ?? {}}
        onDraftChange={(d) => setDraft(d as unknown as AssistantEditDraft)}
        idPrefix="assistant"
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
          note: t('manage:assistants.card_note'),
          dirty: isDirty(section.fields),
          blocked: section.fields.some((f) => invalidKeys.has(f.key)),
          saving: savingSection === index,
          saved: savedSection === index,
          saveLabel: t('common:save'),
          revertLabel: t('common:revert'),
          onSave: () => void saveSection(index),
          onRevert: () => revertSection(section.fields),
        })}
      />
    </ConsoleRecordPage>
  );
}
