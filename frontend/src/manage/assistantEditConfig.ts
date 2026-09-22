// The create-flow and edit-flow edit configs for an Assistant, in the same
// declarative grammar environmentEditConfig.ts produces (EditSectionSpec[]), so
// the create page and the record page's edit cards both render console-grammar
// inputs from config rather than a bespoke form.
//
// Identity vs mutable: an assistant's identity (engine_kind / environment_name) is
// frozen after the workspace materializes (the backend rejects changes with 409),
// so those fields are locked — present in create, absent from edit. The mutable
// set the backend PATCH accepts is display_name / icon / description /
// permission_mode_default; only those drive the edit sections. The create form's
// environment options are injected from the live list of assistant-capable
// environments, with no free-text fallback for an environment nothing backs.
import i18n from '@/i18n';
import type { AssistantRecord } from '@/assistant/types';
import type { EnvironmentConfig } from '@/types';

import type { AssistantDraft } from './assistantConfig';
import type { EditSectionSpec } from './console';

// Built per-call (not module-load) so labels resolve in the active language.
const permissionOptions = () => [
  { value: 'default', label: i18n.t('manage:permission_mode.default') },
  { value: 'acceptEdits', label: i18n.t('manage:permission_mode.accept_edits') },
  { value: 'plan', label: i18n.t('manage:permission_mode.plan') },
  { value: 'bypassPermissions', label: i18n.t('manage:permission_mode.bypass_permissions') },
];

/** Typed get/set over the AssistantDraft so the form stays agnostic of the shape. */
function field<K extends keyof AssistantDraft>(key: K) {
  return {
    get: (d: Record<string, unknown>) => (d as unknown as AssistantDraft)[key],
    set: (d: Record<string, unknown>, value: unknown) => ({
      ...d,
      [key]: value ?? '',
    }),
  };
}

/**
 * Build the create-page edit sections. `environments` is the live list of
 * assistant-capable environment presets — an assistant is not an agent: it names
 * an environment (engine + provider access) and layers its own overrides, so the
 * environment is the thing being chosen here. The engine identity is derived
 * from that choice and shown read-only. `essentials` (name/engine/environment)
 * sits above `Runtime preferences` (description + permission default), matching the
 * environment form's two-group rhythm.
 */
export function buildAssistantCreateSections(
  environments: EnvironmentConfig[],
): EditSectionSpec[] {
  const environmentField = {
    key: 'environment_name',
    label: i18n.t('manage:assistant_form.environment_label'),
    type: 'select' as const,
    get: (d: Record<string, unknown>) => (d as unknown as AssistantDraft).environment_name,
    set: (d: Record<string, unknown>, value: unknown) => {
      const name = String(value || '');
      const environment = environments.find((candidate) => candidate.name === name);
      return {
        ...d,
        environment_name: name,
        engine_kind: String(environment?.engine_kind || ''),
      };
    },
    required: true,
    idTag: 'environment_name',
    options: environments.map((environment) => ({
      value: environment.name,
      label: String(environment.display_name || environment.name),
    })),
    placeholder: i18n.t('manage:assistant_form.environment_select_placeholder'),
    help: i18n.t('manage:assistant_form.environment_select_help'),
  };

  return [
    {
      label: i18n.t('manage:assistant_form.section_basic'),
      fields: [
        {
          key: 'display_name',
          label: i18n.t('manage:assistant_form.name_label'),
          type: 'text',
          ...field('display_name'),
          required: true,
          idTag: 'display_name',
          placeholder: i18n.t('manage:assistant_form.name_placeholder'),
          help: i18n.t('manage:assistant_form.name_help'),
        },
        {
          key: 'engine_kind',
          label: i18n.t('manage:assistant_form.engine_label'),
          type: 'text',
          ...field('engine_kind'),
          required: true,
          idTag: 'engine_kind',
          editable: false,
          help: i18n.t('manage:assistant_form.engine_help'),
        },
        environmentField,
      ],
    },
    {
      label: i18n.t('manage:assistant_form.section_preferences'),
      fields: [
        {
          key: 'description',
          label: i18n.t('manage:assistant_form.description_label'),
          type: 'textarea',
          ...field('description'),
          idTag: 'description',
          rows: 3,
          placeholder: i18n.t('manage:assistant_form.description_placeholder'),
          help: i18n.t('manage:assistant_form.description_help'),
        },
        {
          key: 'permission_mode_default',
          label: i18n.t('manage:assistant_form.permission_label'),
          type: 'select',
          ...field('permission_mode_default'),
          idTag: 'permission_mode_default',
          options: permissionOptions(),
          help: i18n.t('manage:assistant_form.permission_help'),
        },
      ],
    },
  ];
}

// ── Edit flow ────────────────────────────────────────────────────────────────
// The mutable subset the backend PATCH accepts. engine_kind / environment_name are
// intentionally absent (immutable identity) — they are reported in the record
// page's fact rail but are never editable here.
export interface AssistantEditDraft {
  display_name: string;
  description: string;
  icon: string;
  permission_mode_default: string;
}

/** Seed an edit draft from a record (null-safe — icon/description may be null). */
export function buildAssistantEditDraft(a: AssistantRecord): AssistantEditDraft {
  return {
    display_name: a.display_name || '',
    description: a.description || '',
    icon: a.icon || '',
    permission_mode_default: a.permission_mode_default || 'default',
  };
}

/** Typed get/set over AssistantEditDraft so the form stays shape-agnostic. */
function editField<K extends keyof AssistantEditDraft>(key: K) {
  return {
    get: (d: Record<string, unknown>) => (d as unknown as AssistantEditDraft)[key],
    set: (d: Record<string, unknown>, value: unknown) => ({ ...d, [key]: value ?? '' }),
  };
}

/**
 * Inline-edit sections for an existing assistant — only the four mutable fields,
 * in the same two-group rhythm (Basic info / Runtime preferences) the create page
 * uses, so the assistant record reads like the environment one. Engine and
 * environment are absent: they are frozen identity, reported in the fact rail.
 */
export function buildAssistantEditSections(): EditSectionSpec[] {
  return [
    {
      label: i18n.t('manage:assistant_form.section_basic'),
      fields: [
        {
          key: 'display_name',
          label: i18n.t('manage:assistant_form.name_label'),
          type: 'text',
          ...editField('display_name'),
          required: true,
          idTag: 'display_name',
          placeholder: i18n.t('manage:assistant_form.name_placeholder'),
          help: i18n.t('manage:assistant_form.name_help'),
        },
        {
          key: 'icon',
          label: i18n.t('manage:assistant_form.icon_label'),
          type: 'text',
          ...editField('icon'),
          idTag: 'icon',
          placeholder: i18n.t('manage:assistant_form.icon_placeholder'),
          help: i18n.t('manage:assistant_form.icon_help'),
        },
      ],
    },
    {
      label: i18n.t('manage:assistant_form.section_preferences'),
      fields: [
        {
          key: 'description',
          label: i18n.t('manage:assistant_form.description_label'),
          type: 'textarea',
          ...editField('description'),
          idTag: 'description',
          rows: 3,
          placeholder: i18n.t('manage:assistant_form.description_placeholder'),
          help: i18n.t('manage:assistant_form.description_help'),
        },
        {
          key: 'permission_mode_default',
          label: i18n.t('manage:assistant_form.permission_label'),
          type: 'select',
          ...editField('permission_mode_default'),
          idTag: 'permission_mode_default',
          options: permissionOptions(),
          help: i18n.t('manage:assistant_form.permission_help'),
        },
      ],
    },
  ];
}
