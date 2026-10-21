import type { SkillDescriptor } from '@/types';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import i18n from '@/i18n';
import {
  getAssistantDefaults,
  getAssistantDefaultsExtensions,
  setAssistantDefaults,
  setAssistantDefaultsExtensions,
} from '@/assistant/api';
import type { AssistantDefaults, AssistantDefaultsExtensionCatalog } from '@/assistant/types';
import { ErrorNote } from '@/components/shell';
import { getAdminVaultCatalog } from '@/api';
import { Checkbox } from '@/components/ui/checkbox';
import type { RuntimeProfileConfig, VaultSummary } from '@/types';
import { useFrontendReleaseHold } from '@/hooks/useFrontendReleaseHold';
import { useExtensionProvider } from '@/hooks/useExtensionProvider';

import { CatalogExtensionField, sameIds } from './CatalogExtensionField';
import {
  UnavailableCatalogSelection,
  catalogSelectionOf,
  hasCatalogSelection,
} from './UnavailableCatalogSelection';
import {
  ConsoleEditSections,
  ConsoleErrorState,
  ConsoleRecordLoading,
  ConsoleRecordPage,
  revertFields,
  sectionIsDirty,
  type EditFieldSpec,
  type EditSectionSpec,
} from './console';
import { loadAssistantRuntimeProfiles } from './assistantConfig';
import { buildAssistantExtensionSection } from './assistantEditConfig';

/** The select's value for "no RuntimeProfile named": the layer leaves the choice to the rule. */
const AUTOMATIC = '__automatic__';

interface DefaultsDraft {
  runtime_profile_name: string;
  model_name: string;
  instructions: string;
  skills: SkillDescriptor[];
  mcp_servers: Record<string, unknown> | undefined;
  packages: unknown[] | undefined;
  engine_options: Record<string, unknown>;
  credential_vault_ids: string[];
}

function draftOf(view: AssistantDefaults): DefaultsDraft {
  return {
    runtime_profile_name: view.runtime_profile_name || AUTOMATIC,
    model_name: view.model_name ?? '',
    instructions: view.instructions ?? '',
    skills: view.skills ?? [],
    mcp_servers: view.mcp_servers ?? undefined,
    packages: view.packages ?? undefined,
    engine_options: view.engine_options ?? {},
    credential_vault_ids: view.credential_vault_ids ?? [],
  };
}

function field<K extends keyof DefaultsDraft>(key: K) {
  return {
    get: (d: Record<string, unknown>) => (d as unknown as DefaultsDraft)[key],
    set: (d: Record<string, unknown>, value: unknown) => ({ ...d, [key]: value ?? '' }),
  };
}

function baseSection(view: AssistantDefaults): EditSectionSpec {
  const automatic = view.effective_runtime_profile_name && !view.runtime_profile_name
    ? i18n.t('manage:assistant_defaults.runtime_profile_automatic_named', { name: view.effective_runtime_profile_name })
    : i18n.t('manage:assistant_defaults.runtime_profile_automatic');
  return {
    label: i18n.t('manage:assistant_defaults.section_basic'),
    fields: [
      {
        key: 'runtime_profile_name',
        label: i18n.t('common:runtime_profile'),
        type: 'select',
        ...field('runtime_profile_name'),
        idTag: 'runtime_profile_name',
        options: [
          { value: AUTOMATIC, label: automatic },
          ...view.assistant_runtime_profile_names.map((name) => ({ value: name, label: name })),
        ],
        help: i18n.t('manage:assistant_defaults.runtime_profile_help'),
      },
      {
        key: 'model_name',
        label: i18n.t('manage:assistant_defaults.model_label'),
        type: 'text',
        ...field('model_name'),
        idTag: 'model_name',
        help: i18n.t('manage:assistant_defaults.model_help'),
      },
      {
        key: 'instructions',
        label: i18n.t('manage:assistant_defaults.instructions_label'),
        type: 'textarea',
        ...field('instructions'),
        idTag: 'instructions',
        rows: 8,
        mono: true,
        help: i18n.t('manage:assistant_defaults.instructions_help'),
      },
      {
        key: 'credential_vault_ids',
        label: i18n.t('manage:assistant_defaults.vaults_label'),
        type: 'list',
        get: (d) => (d as unknown as DefaultsDraft).credential_vault_ids,
        set: (d, value) => ({ ...d, credential_vault_ids: Array.isArray(value) ? value.map(String) : [] }),
        idTag: 'credential_vault_ids',
        help: i18n.t('manage:assistant_defaults.vaults_help'),
      },
    ],
  };
}

/**
 * The deployment's Assistant defaults: what every Assistant inherits.
 *
 * What an Assistant can do is decided here, by whoever runs the deployment:
 * the RuntimeProfile and model new Assistants use, instructions that come before
 * each owner's, and the Skills, MCP servers, packages and native options every
 * Assistant gets, the deployment's own or picked from the extension catalog.
 * Assistants read this live, so a save reaches each on its next start. The
 * extension fields are the ones the chosen RuntimeProfile's program reads, so
 * they wait for a RuntimeProfile. Without an extension catalog the layer's own
 * fields stand alone, and a selection it still holds is shown as unavailable.
 */
export default function AssistantDefaultsPage() {
  const { t } = useTranslation();
  const [view, setView] = useState<AssistantDefaults | null>(null);
  const [draft, setDraft] = useState<DefaultsDraft | null>(null);
  const [error, setError] = useState('');
  const [saveError, setSaveError] = useState('');
  const [saving, setSaving] = useState<number | null>(null);
  const [saved, setSaved] = useState<number | null>(null);
  const [invalidKeys, setInvalidKeys] = useState<Set<string>>(new Set());
  const [runtime_profiles, setRuntimeProfiles] = useState<RuntimeProfileConfig[]>([]);
  const [catalog, setCatalog] = useState<AssistantDefaultsExtensionCatalog | null>(null);
  const [mcpIds, setMcpIds] = useState<string[]>([]);
  const [skillIds, setSkillIds] = useState<string[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [removingSelection, setRemovingSelection] = useState(false);
  const {
    provider: extensionProvider,
    error: extensionProviderError,
    retry: retryExtensionProvider,
  } = useExtensionProvider();
  const [vaults, setVaults] = useState<VaultSummary[] | null>(null);
  useEffect(() => {
    void getAdminVaultCatalog().then((found) => setVaults(found.vaults), (e: Error) => setSaveError(e.message));
  }, []);

  const load = useCallback(async () => {
    try {
      const found = await getAssistantDefaults();
      setView(found);
      setDraft(draftOf(found));
      setError('');
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  const loadCatalog = useCallback(async () => {
    setCatalogLoading(true);
    try {
      const found = await getAssistantDefaultsExtensions();
      setCatalog(found);
      setMcpIds(found.selected_mcp_server_ids);
      setSkillIds(found.selected_skill_ids);
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setCatalogLoading(false);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (extensionProvider) void loadCatalog();
  }, [extensionProvider, loadCatalog]);
  useEffect(() => {
    void loadAssistantRuntimeProfiles().then(setRuntimeProfiles, (e: Error) => setSaveError(e.message));
  }, []);

  const chosen = draft && draft.runtime_profile_name !== AUTOMATIC
    ? draft.runtime_profile_name
    : view?.effective_runtime_profile_name ?? null;
  const environment = runtime_profiles.find((candidate) => candidate.name === chosen);
  const sections = useMemo(() => {
    if (!view) return [];
    const extensions = buildAssistantExtensionSection(environment);
    return extensions ? [baseSection(view), extensions] : [baseSection(view)];
  }, [environment, view, t]);
  const baseline = useMemo(() => (view ? draftOf(view) : null), [view]);
  const catalogDirty = Boolean(catalog) && (
    !sameIds(mcpIds, catalog!.selected_mcp_server_ids) || !sameIds(skillIds, catalog!.selected_skill_ids)
  );
  const holdsCatalog = (fields: EditFieldSpec[]) =>
    fields.some((f) => f.key === 'mcp_servers' || f.key === 'skills');
  const record = (d: DefaultsDraft | null) => (d ?? null) as unknown as Record<string, unknown> | null;
  const isDirty = (fields: EditFieldSpec[]) =>
    sectionIsDirty(fields, record(draft), record(baseline)) || (holdsCatalog(fields) && catalogDirty);
  useFrontendReleaseHold(sections.some((section) => isDirty(section.fields)));

  const save = async (index: number) => {
    if (!view || !draft) return;
    setSaving(index);
    setSaveError('');
    try {
      const next = await setAssistantDefaults({
        runtime_profile_name: draft.runtime_profile_name === AUTOMATIC ? null : draft.runtime_profile_name,
        model_name: draft.model_name.trim() || null,
        instructions: draft.instructions.trim() || null,
        skills: draft.skills,
        mcp_servers: draft.mcp_servers ?? {},
        packages: (draft.packages ?? []) as Record<string, unknown>[],
        engine_options: draft.engine_options,
        credential_vault_ids: draft.credential_vault_ids,
      });
      if (holdsCatalog(sections[index]?.fields ?? []) && catalogDirty) {
        const selected = await setAssistantDefaultsExtensions({ mcp_server_ids: mcpIds, skill_ids: skillIds });
        setCatalog(selected);
        setMcpIds(selected.selected_mcp_server_ids);
        setSkillIds(selected.selected_skill_ids);
      }
      setView(next);
      setDraft(draftOf(next));
      setSaved(index);
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(null);
    }
  };

  /** Drop a selection no catalog can serve; the server accepts only this then. */
  const removeCatalogSelection = async () => {
    setRemovingSelection(true);
    setSaveError('');
    try {
      await setAssistantDefaultsExtensions({ mcp_server_ids: [], skill_ids: [] });
      // The view only: re-reading through `load` would reset the draft and
      // discard edits pending in the cards.
      setView(await getAssistantDefaults());
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setRemovingSelection(false);
    }
  };

  const title = t('manage:assistant_defaults.title');
  if (error) {
    return (
      <ConsoleRecordPage title={title}>
        <ConsoleErrorState title={t('manage:assistant_defaults.load_failed')} detail={error} onRetry={() => { void load(); }} />
      </ConsoleRecordPage>
    );
  }
  if (extensionProviderError && extensionProvider === undefined) {
    return (
      <ConsoleRecordPage title={title}>
        <ConsoleErrorState
          title={t('manage:assistant_defaults.load_failed')}
          detail={extensionProviderError.message}
          onRetry={retryExtensionProvider}
        />
      </ConsoleRecordPage>
    );
  }
  // Which catalog the fields stand beside decides how they are laid out, so the
  // page waits for that answer rather than redrawing the card once it lands.
  if (!view || !draft || extensionProvider === undefined) {
    return (
      <ConsoleRecordPage title={title}>
        <ConsoleRecordLoading />
      </ConsoleRecordPage>
    );
  }
  const staleSelection = extensionProvider === null
    ? catalogSelectionOf(view as unknown as Record<string, unknown>)
    : null;
  return (
    <ConsoleRecordPage title={title} lede={t('manage:assistant_defaults.lede')}>
      {saveError && <ErrorNote>{saveError}</ErrorNote>}
      {staleSelection && hasCatalogSelection(staleSelection) && (
        <UnavailableCatalogSelection
          selection={staleSelection}
          defaults
          removing={removingSelection}
          onRemove={() => void removeCatalogSelection()}
        />
      )}
      {view.assistant_runtime_profile_names.length === 0 && (
        <ErrorNote title={t('manage:assistant_defaults.no_runtime_profile_title')}>
          {t('manage:assistant_defaults.no_runtime_profile')}
        </ErrorNote>
      )}
      <ConsoleEditSections
        sections={sections}
        renderField={(spec, customField) => {
          if (spec.key === 'credential_vault_ids') {
            const chosen = new Set(draft.credential_vault_ids);
            const choices = (vaults ?? []).filter((vault) => !vault.archived_at || chosen.has(vault.vault_id));
            if (choices.length === 0) {
              return <p className="text-13 text-muted-foreground">{t('manage:assistant_defaults.vaults_none')}</p>;
            }
            return (
              <ul className="flex flex-col gap-2" data-testid="assistant-defaults-vaults">
                {choices.map((vault) => (
                  <li key={vault.vault_id}>
                    <label className="flex items-center gap-2 text-13">
                      <Checkbox
                        checked={chosen.has(vault.vault_id)}
                        disabled={saving !== null}
                        onCheckedChange={(checked) => {
                          setSaved(null);
                          setDraft({
                            ...draft,
                            credential_vault_ids: checked
                              ? [...draft.credential_vault_ids, vault.vault_id]
                              : draft.credential_vault_ids.filter((id) => id !== vault.vault_id),
                          });
                        }}
                      />
                      {vault.display_name}
                    </label>
                  </li>
                ))}
              </ul>
            );
          }
          if (spec.key !== 'mcp_servers' && spec.key !== 'skills') return customField;
          if (!extensionProvider) return customField;
          const isMcp = spec.key === 'mcp_servers';
          const kind = isMcp ? 'mcp' : 'skills';
          return (
            <CatalogExtensionField
              kind={kind}
              id={`assistant-defaults-extension-${kind}`}
              provider={extensionProvider}
              catalog={catalog}
              value={isMcp ? mcpIds : skillIds}
              onChange={isMcp ? setMcpIds : setSkillIds}
              loading={catalogLoading}
              disabled={saving !== null}
            >
              {customField}
            </CatalogExtensionField>
          );
        }}
        draft={record(draft) ?? {}}
        onDraftChange={(d) => { setSaved(null); setDraft(d as unknown as DefaultsDraft); }}
        idPrefix="assistant-defaults"
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
          note: t('manage:assistant_defaults.card_note'),
          dirty: isDirty(section.fields),
          blocked: section.fields.some((f) => invalidKeys.has(f.key)),
          saving: saving === index,
          saved: saved === index,
          saveLabel: t('common:save'),
          revertLabel: t('common:revert'),
          onSave: () => void save(index),
          onRevert: () => {
            if (!baseline) return;
            if (holdsCatalog(section.fields) && catalog) {
              setMcpIds(catalog.selected_mcp_server_ids);
              setSkillIds(catalog.selected_skill_ids);
            }
            setDraft(revertFields(section.fields, record(draft)!, record(baseline)!) as unknown as DefaultsDraft);
          },
        })}
      />
    </ConsoleRecordPage>
  );
}
