import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import i18n from '@/i18n';
import {
  agentAccessPayload,
  createAgentExtensionConsoleSession,
  deleteAgent,
  getAgentAccess,
  getAgentExtensions,
  getAgentSchema,
  listAgentCategories,
  listAgentRuntimeProfileModels,
  listAgentRuntimeProfiles,
  listAgentTags,
  listAgents,
  setAgentAccess,
  setAgentExtensions,
  updateAgent,
} from '@/api';
import type {
  AgentCategory,
  AgentConfig,
  AgentDraft,
  AgentExtensionCatalog,
  RuntimeProfileConfig,
  FormSchema,
} from '@/types';

import { ErrorNote } from '@/components/shell';
import {
  ConsoleEditSections,
  ConsoleDangerButton,
  ConsoleFact,
  ConsoleFactRail,
  ConsoleRecordLoading,
  ConsoleRecordPage,
  ConsoleErrorState,
  useRecordCrumb,
  sectionIsDirty,
  missingRequiredFields,
  fieldNamedByRefusal,
  revertFields,
  type EditFieldSpec,
  type EditSectionSpec,
} from './console';
import { CatalogExtensionField, sameIds } from './CatalogExtensionField';
import { ConnectorsCard } from './ConnectorsCard';
import {
  UnavailableCatalogSelection,
  catalogSelectionOf,
  hasCatalogSelection,
} from './UnavailableCatalogSelection';
import { agentDisplayName, agentModel, formatDateTime } from './agentConfig';
import { buildAgentEditSections } from './agentEditConfig';
import { useFrontendReleaseHold } from '@/hooks/useFrontendReleaseHold';
import { useExtensionProvider } from '@/hooks/useExtensionProvider';
import { AgentPrewarmStatus } from './AgentPrewarmStatus';

/** The edit helpers speak plain records; a typed draft is one. */
const draftRecordOf = (v: unknown) => (v ?? null) as Record<string, unknown> | null;

/** A refused save: the card whose Save was pressed, the reason, and the field it names. */
type SaveRefusal = { section: number; message: string; field: string | null };

/**
 * One Agent on its own page.
 *
 * The list hands the record over completely (docs/frontend-design.md §3): this
 * is a form, and a form wants its labels beside its controls and its controls
 * wide enough to read a value in — which a panel sharing the width with a table
 * cannot give it.
 *
 * Each schema group is a card that owns its own save (§4). Nothing here has a
 * view mode: the fields are the controls from the moment the page opens.
 *
 * The extension catalog is whatever the deployment's provider declares, read
 * before anything asks a catalog. A deployment that runs none gets the Agent's
 * own MCP, Skills and package fields alone, sends no catalog request, and sees
 * a selection the Agent still holds from an earlier catalog as unavailable.
 */
export default function AgentDetailPage() {
  const { agentId = '' } = useParams();
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [record, setRecord] = useState<AgentConfig | null>(null);
  const [schema, setSchema] = useState<FormSchema | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [archiving, setArchiving] = useState(false);
  const [archiveError, setArchiveError] = useState('');

  const [draft, setDraft] = useState<AgentConfig | null>(null);
  const [savingSection, setSavingSection] = useState<number | null>(null);
  const [savedSection, setSavedSection] = useState<number | null>(null);
  // Cleared only when the reader edits the draft or saves again; the reload
  // that follows a refused save leaves it beside the Save that was pressed.
  const [refusal, setRefusal] = useState<SaveRefusal | null>(null);
  const [invalidKeys, setInvalidKeys] = useState<Set<string>>(new Set());
  const [extensionOpening, setExtensionOpening] = useState<'mcp' | 'skills' | null>(null);
  const [extensionError, setExtensionError] = useState('');
  const [extensionCatalog, setExtensionCatalog] = useState<AgentExtensionCatalog | null>(null);
  const [extensionMcpIds, setExtensionMcpIds] = useState<string[]>([]);
  const [extensionSkillIds, setExtensionSkillIds] = useState<string[]>([]);
  const [extensionLoading, setExtensionLoading] = useState(false);
  const [removingSelection, setRemovingSelection] = useState(false);
  const savedTimer = useRef<number | null>(null);
  const {
    provider: extensionProvider,
    error: extensionProviderError,
    retry: retryExtensionProvider,
  } = useExtensionProvider();

  const [runtime_profiles, setRuntimeProfiles] = useState<RuntimeProfileConfig[]>([]);
  const [categories, setCategories] = useState<AgentCategory[]>([]);
  const [tagSuggestions, setTagSuggestions] = useState<string[]>([]);
  const [models, setModels] = useState<string[]>([]);

  useRecordCrumb(record ? agentDisplayName(record) : t('common:agent'));

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // The categories and tags are read with the record, not absorbed: an
      // Agent's stored categories render by name from this list, and an empty
      // one would show every category it holds as unavailable.
      const [list, sch, envs, cats, tags] = await Promise.all([
        listAgents(),
        getAgentSchema().catch(() => null),
        listAgentRuntimeProfiles().catch(() => [] as RuntimeProfileConfig[]),
        listAgentCategories(),
        listAgentTags(),
      ]);
      const found = list.find((a) => a.agent_id === agentId) ?? null;
      const access = found?.can_manage ? await getAgentAccess(found.agent_id) : null;
      setRecord(found && access ? { ...found, ...access } : found);
      setSchema(sch);
      setRuntimeProfiles(envs);
      setCategories(cats);
      setTagSuggestions(tags.map((entry) => entry.tag));
      setError(found ? '' : t('manage:agents.not_found', { name: agentId }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [agentId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const runtimeProfileName = String(draft?.runtime_profile_name || '').trim();
  useEffect(() => {
    let current = true;
    if (!runtimeProfileName) {
      setModels([]);
      return () => {
        current = false;
      };
    }
    void listAgentRuntimeProfileModels(runtimeProfileName)
      .then((items) => {
        if (current) setModels(items);
      })
      .catch(() => {
        if (current) setModels([]);
      });
    return () => {
      current = false;
    };
  }, [runtimeProfileName]);

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
    if (seededFor.current === agentId && draft) return;
    seededFor.current = agentId;
    setDraft(record ? { ...record } : null);
    setRefusal(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, record]);

  const sections: EditSectionSpec[] = useMemo(
    () =>
      buildAgentEditSections(schema, draft as Record<string, unknown> | null, {
        nameEditable: false,
        runtime_profiles,
        engineOptionsSchema: runtime_profiles.find((e) => e.name === runtimeProfileName)
          ?.engine_options_schema,
        models,
        catalogName: extensionProvider?.display_name,
        categories,
        tagSuggestions,
      }).sections,
    [schema, draft, runtime_profiles, models, extensionProvider, categories, tagSuggestions, t],
  );

  const isDirty = (fields: EditFieldSpec[]) =>
    sectionIsDirty(fields, draftRecordOf(draft), draftRecordOf(record));

  const missingRequired = useMemo(
    () => missingRequiredFields(sections, draftRecordOf(draft)),
    [sections, draft],
  );

  // Authorization is computed by the server against its current ownership and
  // role facts. Re-deriving that decision in the browser drifts as soon as the
  // server adds another manager class.
  const canManage = record?.can_manage === true;

  const loadExtensions = useCallback(async () => {
    if (!agentId) return;
    setExtensionLoading(true);
    setExtensionError('');
    try {
      const catalog = await getAgentExtensions(agentId);
      setExtensionCatalog(catalog);
      setExtensionMcpIds(catalog.selected_mcp_server_ids);
      setExtensionSkillIds(catalog.selected_skill_ids);
    } catch (e) {
      setExtensionError((e as Error).message);
    } finally {
      setExtensionLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    if (canManage && extensionProvider) void loadExtensions();
  }, [canManage, extensionProvider, loadExtensions]);

  /** Drop a selection no catalog can serve; the server accepts only this then. */
  const removeCatalogSelection = async () => {
    if (!record) return;
    setRemovingSelection(true);
    setExtensionError('');
    try {
      await setAgentExtensions(record.agent_id, { mcp_server_ids: [], skill_ids: [] });
      await load();
    } catch (e) {
      setExtensionError((e as Error).message);
    } finally {
      setRemovingSelection(false);
    }
  };

  const extensionsDirty = Boolean(extensionCatalog) && (
    !sameIds(extensionMcpIds, extensionCatalog!.selected_mcp_server_ids)
    || !sameIds(extensionSkillIds, extensionCatalog!.selected_skill_ids)
  );
  useFrontendReleaseHold(extensionsDirty || sections.some((section) => isDirty(section.fields)));

  const revertExtensions = () => {
    if (!extensionCatalog) return;
    setExtensionMcpIds(extensionCatalog.selected_mcp_server_ids);
    setExtensionSkillIds(extensionCatalog.selected_skill_ids);
  };

  /**
   * Opens the extension catalogue in a tab of its own, which is what the
   * button's outward arrow announces.
   *
   * The catalogue is a different application — the gateway's own UI, wearing
   * none of this console's chrome and offering no way back into it. Replacing
   * this page with it would discard whatever is half-typed in the cards below,
   * on a surface whose fields are editable where they are read.
   */
  const openExtensions = async (section: 'mcp' | 'skills') => {
    if (!record || extensionOpening) return;
    setExtensionOpening(section);
    setExtensionError('');
    // Claimed while the click is still on the stack. A tab asked for after the
    // await is a tab asked for without a user gesture, which a pop-up blocker
    // refuses — and the round trip below is what puts the await there. The
    // target is same-origin (the address is checked to be a path), so the
    // handle is kept rather than dropped for `noopener`.
    const tab = window.open('', '_blank');
    try {
      const session = await createAgentExtensionConsoleSession(record.agent_id, section);
      if (!session.open_url.startsWith('/')) {
        throw new Error(t('manage:agents.extension_url_invalid'));
      }
      if (!tab) throw new Error(t('manage:agents.extension_tab_blocked'));
      // replace, so the blank placeholder is not a history entry the reader
      // has to press Back through twice.
      tab.location.replace(session.open_url);
    } catch (e) {
      tab?.close();
      setExtensionError((e as Error).message);
    } finally {
      setExtensionOpening(null);
    }
  };

  const saveSection = async (fields: EditFieldSpec[], index: number) => {
    if (!draft || !record) return;
    setSavingSection(index);
    setRefusal(null);
    try {
      const payload = fields.reduce(
        (acc, f) =>
          f.editable === false
            ? acc
            : f.set(acc, f.get(draft as unknown as Record<string, unknown>)),
        { ...record } as Record<string, unknown>,
      );
      const isAccessSection = fields.every((field) =>
        ['visibility', 'admins', 'allowed_user_ids'].includes(field.key),
      );
      if (isAccessSection) {
        await setAgentAccess(record.agent_id, agentAccessPayload(payload));
      } else if (isDirty(fields)) {
        if (!schema) throw new Error('Agent authoring schema is unavailable');
        // The version the record was read at: the server refuses the write
        // (409) if somebody else saved this agent in between. updateAgent then
        // projects this fetched record onto the schema's authoring keys.
        await updateAgent(
          record.agent_id,
          {
            ...(payload as unknown as AgentDraft),
            version: record.version,
          },
          schema,
        );
      }
      if (fields.some((field) => field.key === 'mcp_servers') && extensionsDirty) {
        const saved = await setAgentExtensions(record.agent_id, {
          mcp_server_ids: extensionMcpIds,
          skill_ids: extensionSkillIds,
        });
        setExtensionCatalog(saved);
        setExtensionMcpIds(saved.selected_mcp_server_ids);
        setExtensionSkillIds(saved.selected_skill_ids);
      }
      await load();
      setSavedSection(index);
      if (savedTimer.current) window.clearTimeout(savedTimer.current);
      savedTimer.current = window.setTimeout(() => setSavedSection(null), 2600);
    } catch (e) {
      setRefusal({
        section: index,
        message: (e as Error).message,
        field: fieldNamedByRefusal(e, sections),
      });
      await load();
    } finally {
      setSavingSection(null);
    }
  };

  const editDraft = (next: AgentConfig) => {
    // A control can report a value it did not change (a JSON field does on
    // blur); only a change is the edit that retires the refusal.
    if (JSON.stringify(next) !== JSON.stringify(draft)) setRefusal(null);
    setDraft(next);
  };

  const revertSection = (fields: EditFieldSpec[]) => {
    if (!draft || !record) return;
    editDraft(
      revertFields(fields, draftRecordOf(draft)!, draftRecordOf(record)!) as unknown as AgentConfig,
    );
  };

  const archive = async () => {
    if (archiving) return;
    setArchiving(true);
    setArchiveError('');
    try {
      await deleteAgent(agentId);
      navigate('/manage/agents');
    } catch (e) {
      setArchiveError((e as Error).message);
      setArchiving(false);
    }
  };

  if (loading && !record) {
    return (
      <ConsoleRecordPage title={t('common:agent')}>
        <ConsoleRecordLoading />
      </ConsoleRecordPage>
    );
  }

  if (!record) {
    return (
      <ConsoleRecordPage title={t('common:agent')}>
        <ConsoleErrorState
          title={t('manage:agents.error_title')}
          detail={error}
          onRetry={() => void load()}
        />
      </ConsoleRecordPage>
    );
  }

  // Which catalog the fields stand beside decides how they are laid out, so the
  // page waits for that answer rather than redrawing the card once it lands.
  if (extensionProvider === undefined) {
    return (
      <ConsoleRecordPage title={agentDisplayName(record)}>
        {extensionProviderError ? (
          <ConsoleErrorState
            title={t('manage:agents.error_title')}
            detail={extensionProviderError.message}
            onRetry={retryExtensionProvider}
          />
        ) : (
          <ConsoleRecordLoading />
        )}
      </ConsoleRecordPage>
    );
  }

  const draftRecord = draft as unknown as Record<string, unknown> | undefined;
  const staleSelection = extensionProvider === null ? catalogSelectionOf(record) : null;

  const extensionField = (field: EditFieldSpec, customField: React.ReactNode) => {
    if (field.key !== 'mcp_servers' && field.key !== 'skills') return customField;
    if (!extensionProvider) return customField;
    const isMcp = field.key === 'mcp_servers';
    const kind = isMcp ? 'mcp' : 'skills';
    return (
      <CatalogExtensionField
        kind={kind}
        id={`agent-extension-${kind}`}
        provider={extensionProvider}
        catalog={extensionCatalog}
        value={isMcp ? extensionMcpIds : extensionSkillIds}
        onChange={isMcp ? setExtensionMcpIds : setExtensionSkillIds}
        loading={extensionLoading}
        disabled={savingSection !== null}
        manage={{
          opening: extensionOpening === kind,
          busy: extensionOpening !== null,
          onOpen: () => void openExtensions(kind),
        }}
      >
        {customField}
      </CatalogExtensionField>
    );
  };

  return (
    <ConsoleRecordPage
      title={agentDisplayName(record)}
      lede={String(record.description || '') || undefined}
      status={
        record.enabled !== false
          ? { tone: 'done', label: t('common:enabled') }
          : { tone: 'idle', label: t('common:disabled') }
      }
      actions={canManage && (
        <ConsoleDangerButton
          disabled={archiving || savingSection !== null || extensionOpening !== null || removingSelection}
          confirm={{
            title: t('manage:agents.archive_title', { name: agentDisplayName(record) }),
            description: t('manage:agents.archive_description'),
            action: t('manage:agents.archive'),
            onConfirm: () => void archive(),
          }}
        >
          {t(archiving ? 'manage:agents.archiving' : 'manage:agents.archive')}
        </ConsoleDangerButton>
      )}
      rail={
        <ConsoleFactRail>
          <ConsoleFact label={t('common:model')} value={agentModel(record) || '—'} />
          <ConsoleFact
            label={t('manage:common_fields.runtime_profile')}
            value={String(record.runtime_profile_name || '—')}
          />
          <ConsoleFact
            label={t('common:updated_at')}
            value={formatDateTime(String(record.updated_at || '')) || '—'}
          />
        </ConsoleFactRail>
      }
    >
      {archiveError && <ErrorNote>{archiveError}</ErrorNote>}
      {extensionError && <ErrorNote>{extensionError}</ErrorNote>}
      {canManage && staleSelection && hasCatalogSelection(staleSelection) && (
        <UnavailableCatalogSelection
          selection={staleSelection}
          removing={removingSelection}
          onRemove={() => void removeCatalogSelection()}
        />
      )}
      {canManage && (
        <AgentPrewarmStatus key={agentId} agentId={agentId}
          savedAt={String(record.updated_at || '')}
          enabled={Number(record.prepared_capacity ?? 0) > 0 && record.enabled !== false}
          dirty={extensionsDirty || sections.some((section) => isDirty(section.fields))} />
      )}
      {canManage && (
        <ConsoleEditSections
          sections={sections}
          renderField={extensionField}
          draft={draftRecord ?? {}}
          onDraftChange={(d) => editDraft(d as unknown as AgentConfig)}
          idPrefix="agent"
          invalidKeys={invalidKeys}
          refusedKey={refusal?.field}
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
              ? i18n.t(`misc:agent_form.groups.${schema.groups[index].id}.description`, {
                  defaultValue: '',
                })
              : undefined,
            note: t('manage:agents.card_note'),
            dirty: isDirty(section.fields) || (section.fields.some((f) => f.key === 'mcp_servers') && extensionsDirty),
            blocked:
              section.fields.some((f) => invalidKeys.has(f.key)) || missingRequired.length > 0,
            blockedReason:
              missingRequired.length > 0
                ? t('manage:console.blocked_by_required', {
                    field: String(missingRequired[0].label),
                  })
                : undefined,
            error: refusal?.section === index ? refusal.message : undefined,
            saving: savingSection === index,
            saved: savedSection === index,
            saveLabel: t('common:save'),
            revertLabel: t('common:revert'),
            onSave: () => void saveSection(section.fields, index),
            onRevert: () => {
              revertSection(section.fields);
              if (section.fields.some((f) => f.key === 'mcp_servers')) revertExtensions();
            },
          })}
        />
      )}
      {record?.can_manage && <ConnectorsCard kind="agent" targetId={record.agent_id} />}
    </ConsoleRecordPage>
  );
}
