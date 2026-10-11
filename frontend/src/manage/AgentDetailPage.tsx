import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import i18n from '@/i18n';
import {
  agentAccessPayload,
  createAgentExtensionConsoleSession,
  getAgentAccess,
  getAgentExtensions,
  getAgentSchema,
  listAgentEnvironmentModels,
  listAgentEnvironments,
  listAgents,
  setAgentAccess,
  setAgentExtensions,
  updateAgent,
} from '@/api';
import type {
  AgentConfig,
  AgentDraft,
  AgentExtensionCatalog,
  EnvironmentConfig,
  FormSchema,
} from '@/types';

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
import { CatalogExtensionField, sameIds } from './CatalogExtensionField';
import { ConnectorsCard } from './ConnectorsCard';
import {
  UnavailableCatalogSelection,
  catalogSelectionOf,
  hasCatalogSelection,
} from './UnavailableCatalogSelection';
import { agentDisplayName, agentModelLabel, formatDateTime } from './agentConfig';
import { buildAgentEditSections } from './agentEditConfig';
import { useFrontendReleaseHold } from '@/hooks/useFrontendReleaseHold';
import { useExtensionProvider } from '@/hooks/useExtensionProvider';
import { AgentPrewarmStatus } from './AgentPrewarmStatus';

/** The edit helpers speak plain records; a typed draft is one. */
const draftRecordOf = (v: unknown) => (v ?? null) as Record<string, unknown> | null;

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

  const [record, setRecord] = useState<AgentConfig | null>(null);
  const [schema, setSchema] = useState<FormSchema | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [draft, setDraft] = useState<AgentConfig | null>(null);
  const [savingSection, setSavingSection] = useState<number | null>(null);
  const [savedSection, setSavedSection] = useState<number | null>(null);
  const [saveError, setSaveError] = useState('');
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

  const [environments, setEnvironments] = useState<EnvironmentConfig[]>([]);
  const [models, setModels] = useState<string[]>([]);

  useRecordCrumb(record ? agentDisplayName(record) : t('common:agent'));

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [list, sch, envs] = await Promise.all([
        listAgents(),
        getAgentSchema().catch(() => null),
        listAgentEnvironments().catch(() => [] as EnvironmentConfig[]),
      ]);
      const found = list.find((a) => a.agent_id === agentId) ?? null;
      const access = found?.can_manage ? await getAgentAccess(found.agent_id) : null;
      setRecord(found && access ? { ...found, ...access } : found);
      setSchema(sch);
      setEnvironments(envs);
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, record]);

  const sections: EditSectionSpec[] = useMemo(
    () =>
      buildAgentEditSections(schema, draft as Record<string, unknown> | null, {
        nameEditable: false,
        environments,
        engineOptionsSchema: environments.find((e) => e.name === environmentName)
          ?.engine_options_schema,
        models,
        catalogName: extensionProvider?.display_name,
      }).sections,
    [schema, draft, environments, models, extensionProvider, t],
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
    setSaveError('');
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
      setSaveError((e as Error).message);
      await load();
    } finally {
      setSavingSection(null);
    }
  };

  const revertSection = (fields: EditFieldSpec[]) => {
    if (!draft || !record) return;
    setDraft(
      revertFields(fields, draftRecordOf(draft)!, draftRecordOf(record)!) as unknown as AgentConfig,
    );
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
      rail={
        <ConsoleFactRail>
          <ConsoleFact label={t('common:model')} value={agentModelLabel(record)} />
          <ConsoleFact
            label={t('manage:common_fields.environment')}
            value={String(record.environment_name || '—')}
          />
          <ConsoleFact
            label={t('common:updated_at')}
            value={formatDateTime(String(record.updated_at || '')) || '—'}
          />
        </ConsoleFactRail>
      }
    >
      {saveError && <ErrorNote>{saveError}</ErrorNote>}
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
          onDraftChange={(d) => setDraft(d as unknown as AgentConfig)}
          idPrefix="agent"
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
