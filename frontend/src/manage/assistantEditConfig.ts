import type { SkillDescriptor } from '@/types';
// The create-flow and edit-flow edit configs for an Assistant, in the same
// declarative grammar runtimeProfileEditConfig.ts produces (EditSectionSpec[]), so
// the create page and the record page's edit cards both render console-grammar
// inputs from config rather than a bespoke form.
//
// Identity vs mutable: an assistant's identity (engine_kind / runtime_profile_name) is
// frozen after the workspace materializes (the backend rejects changes with 409),
// so those fields are locked — present in create, absent from edit. The mutable
// set the backend PATCH accepts is display_name / icon / description /
// permission_mode_default / system; only those drive the edit sections. The create form's
// environment options are injected from the live list of assistant-capable
// runtime_profiles, with no free-text fallback for an environment nothing backs.
import i18n from '@/i18n';
import type { AssistantRecord } from '@/assistant/types';
import type { RuntimeProfileConfig } from '@/types';

import { permissionModeLabel, type AssistantDraft } from './assistantConfig';
import type { EditFieldSpec, EditSectionSpec } from './console';

// The system prompt, shared by create and edit. Mono like the Agent form's:
// it is text the reader writes for the program, not product copy.
const systemField = (
  accessors: {
    get: (d: Record<string, unknown>) => unknown;
    set: (d: Record<string, unknown>, value: unknown) => Record<string, unknown>;
  },
) => ({
  key: 'system',
  label: i18n.t('manage:assistant_form.system_label'),
  type: 'textarea' as const,
  ...accessors,
  idTag: 'system',
  rows: 8,
  mono: true,
  placeholder: i18n.t('manage:assistant_form.system_placeholder'),
  help: i18n.t('manage:assistant_form.system_help'),
});

// The owner's timezone, shared by create and edit: the IANA names this browser
// knows, searchable, since the list is long.
const timezoneField = (
  accessors: {
    get: (d: Record<string, unknown>) => unknown;
    set: (d: Record<string, unknown>, value: unknown) => Record<string, unknown>;
  },
): EditFieldSpec => ({
  key: 'timezone',
  label: i18n.t('manage:assistant_form.timezone_label'),
  type: 'search_select',
  ...accessors,
  idTag: 'timezone',
  options: Intl.supportedValuesOf('timeZone').map((zone) => ({ value: zone, label: zone })),
  placeholder: i18n.t('manage:assistant_form.timezone_placeholder'),
  searchPlaceholder: i18n.t('manage:assistant_form.timezone_search'),
  emptyLabel: i18n.t('manage:assistant_form.timezone_empty'),
  help: i18n.t('manage:assistant_form.timezone_help'),
});

function permissionField(
  accessors: {
    get: (d: Record<string, unknown>) => unknown;
    set: (d: Record<string, unknown>, value: unknown) => Record<string, unknown>;
  },
  options: { value: string; label: string }[],
): EditFieldSpec[] {
  if (options.length === 0) return [];
  return [
    {
      key: 'permission_mode_default',
      label: i18n.t('manage:assistant_form.permission_label'),
      type: 'select',
      ...accessors,
      idTag: 'permission_mode_default',
      options,
      help: i18n.t('manage:assistant_form.permission_help'),
    },
  ];
}

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

/** The permission modes the named RuntimeProfile's program declares. */
export function runtimeProfilePermissionModes(
  runtime_profiles: RuntimeProfileConfig[],
  name: string,
): string[] {
  return runtime_profiles.find((environment) => environment.name === name)?.permission_modes ?? [];
}

/**
 * Build the create-page edit sections. `runtime_profiles` is the live list of
 * assistant-capable environment presets — an assistant is not an agent: it names
 * an environment (engine + provider access) and layers its own overrides, so the
 * environment is the thing being chosen here. The engine identity is derived
 * from that choice and shown read-only. `essentials` (name/engine/environment)
 * sits above `Runtime preferences` (description + permission default), matching the
 * environment form's two-group rhythm.
 */
export function buildAssistantCreateSections(
  runtime_profiles: RuntimeProfileConfig[],
  runtimeProfileName: string,
): EditSectionSpec[] {
  const runtimeProfileField = {
    key: 'runtime_profile_name',
    label: i18n.t('manage:assistant_form.runtime_profile_label'),
    type: 'select' as const,
    get: (d: Record<string, unknown>) => (d as unknown as AssistantDraft).runtime_profile_name,
    set: (d: Record<string, unknown>, value: unknown) => {
      const name = String(value || '');
      const environment = runtime_profiles.find((candidate) => candidate.name === name);
      const modes = environment?.permission_modes ?? [];
      return {
        ...d,
        runtime_profile_name: name,
        engine_kind: String(environment?.engine_kind || ''),
        permission_mode_default: modes.includes('default') ? 'default' : (modes[0] ?? ''),
      };
    },
    required: true,
    idTag: 'runtime_profile_name',
    options: runtime_profiles.map((environment) => ({
      value: environment.name,
      label: String(environment.display_name || environment.name),
    })),
    placeholder: i18n.t('manage:assistant_form.runtime_profile_select_placeholder'),
    help: i18n.t('manage:assistant_form.runtime_profile_select_help'),
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
        runtimeProfileField,
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
        // Offered only when the chosen RuntimeProfile's program has modes: one
        // without them refuses any value, so a select here could only fail.
        ...permissionField(
          field('permission_mode_default'),
          runtimeProfilePermissionModes(runtime_profiles, runtimeProfileName).map((mode) => ({
            value: mode,
            label: permissionModeLabel(mode),
          })),
        ),
        timezoneField(field('timezone')),
        systemField(field('system')),
      ],
    },
  ];
}

// ── Edit flow ────────────────────────────────────────────────────────────────
// The mutable subset the backend PATCH accepts. engine_kind / runtime_profile_name are
// intentionally absent (immutable identity) — they are reported in the record
// page's fact rail but are never editable here.
export interface AssistantEditDraft {
  display_name: string;
  description: string;
  icon: string;
  permission_mode_default: string;
  system: string;
  timezone: string;
  skills: SkillDescriptor[];
  mcp_servers: Record<string, unknown> | undefined;
  packages: unknown[] | undefined;
  engine_options: Record<string, unknown>;
}

/** Seed an edit draft from a record (null-safe — icon/description may be null). */
export function buildAssistantEditDraft(a: AssistantRecord): AssistantEditDraft {
  return {
    display_name: a.display_name || '',
    description: a.description || '',
    icon: a.icon || '',
    permission_mode_default: a.permission_mode_default || '',
    system: a.system || '',
    timezone: a.timezone || '',
    skills: a.skills ?? [],
    mcp_servers: a.mcp_servers ?? undefined,
    packages: a.packages ?? undefined,
    engine_options: a.engine_options ?? {},
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
 * Inline-edit sections for an existing assistant — only the mutable fields,
 * and the permission mode only when the record has one (the server stores
 * none for a program that declares no modes),
 * in the same two-group rhythm (Basic info / Runtime preferences) the create page
 * uses, so the assistant record reads like the environment one. Engine and
 * environment are absent: they are frozen identity, reported in the fact rail.
 */
export function buildAssistantEditSections(hasPermissionMode: boolean): EditSectionSpec[] {
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
        ...permissionField(
          editField('permission_mode_default'),
          hasPermissionMode ? permissionOptions() : [],
        ),
        timezoneField(editField('timezone')),
        systemField(editField('system')),
      ],
    },
  ];
}

// ── Extensions ───────────────────────────────────────────────────────────────
// What the Assistant adds to its program, rendered from the program's own
// declaration (the RuntimeProfile's `extensions` and `engine_options_schema`), so
// a program offers only what it reads and a new program needs no form change.

/** The extension fields the RuntimeProfile's program reads, in a PATCH body. */
export function assistantExtensionUpdate(
  environment: RuntimeProfileConfig | undefined,
  draft: AssistantEditDraft,
): Record<string, unknown> {
  const extensions = environment?.extensions;
  if (!extensions) return {};
  return {
    ...(extensions.skills ? { skills: draft.skills } : {}),
    ...(extensions.mcp_transports.length > 0 ? { mcp_servers: draft.mcp_servers ?? null } : {}),
    ...(extensions.packages.length > 0 ? { packages: draft.packages ?? [] } : {}),
    ...((environment?.engine_options_schema ?? []).length > 0
      ? { engine_options: draft.engine_options }
      : {}),
  };
}

/** The Extensions card for the RuntimeProfile's program, or null when it reads none. */
export function buildAssistantExtensionSection(
  environment: RuntimeProfileConfig | undefined,
): EditSectionSpec | null {
  const extensions = environment?.extensions;
  if (!extensions) return null;
  const fields: EditFieldSpec[] = [];
  if (extensions.skills) {
    fields.push({
      key: 'skills',
      label: i18n.t('manage:assistant_form.skills_label'),
      type: 'json',
      get: (d) => (d as unknown as AssistantEditDraft).skills,
      set: (d, value) => ({
        ...d,
        skills: value ?? [],
      }),
      idTag: 'skills',
      example: '["https://github.com/anthropics/skills.git@main#skills/brand-guidelines"]',
      help: i18n.t('manage:assistant_form.skills_help'),
    });
  }
  if (extensions.mcp_transports.length > 0) {
    fields.push({
      key: 'mcp_servers',
      label: i18n.t('manage:assistant_form.mcp_label'),
      type: 'json',
      get: (d) => (d as unknown as AssistantEditDraft).mcp_servers,
      set: (d, value) => ({ ...d, mcp_servers: value }),
      idTag: 'mcp_servers',
      rows: 6,
      example: JSON.stringify({ docs: { type: 'http', url: 'https://mcp.example.com/mcp' } }, null, 2),
      help: i18n.t('manage:assistant_form.mcp_help', {
        transports: extensions.mcp_transports.join(', '),
      }),
    });
  }
  if (extensions.packages.length > 0) {
    const [first] = extensions.packages;
    fields.push({
      key: 'packages',
      label: i18n.t('manage:assistant_form.packages_label'),
      type: 'json',
      get: (d) => (d as unknown as AssistantEditDraft).packages,
      set: (d, value) => ({ ...d, packages: value }),
      idTag: 'packages',
      rows: 6,
      example: JSON.stringify([{
        kind: first.name,
        url: 'https://github.com/example/plugins.git',
        protocol: 'https',
        branch: 'main',
        paths: ['my-plugin'],
      }], null, 2),
      help: [
        i18n.t('manage:assistant_form.packages_help'),
        ...extensions.packages.map((kind) => `${kind.name} — ${kind.label}. ${kind.help}`),
      ].join('\n'),
    });
  }
  for (const block of environment?.engine_options_schema ?? []) {
    const reserved = block.protected_keys ?? [];
    fields.push({
      key: `engine_options.${block.key}`,
      label: block.label ?? block.key,
      type: 'json',
      // The page renders its sections against an empty draft before the
      // record arrives, so the bag may be absent.
      get: (d) => (d as Partial<AssistantEditDraft>).engine_options?.[block.key],
      set: (d, value) => {
        const bag = { ...(d as Partial<AssistantEditDraft>).engine_options };
        if (value == null) delete bag[block.key];
        else bag[block.key] = value;
        return { ...d, engine_options: bag };
      },
      idTag: `engine_options-${block.key}`,
      rows: 6,
      help: [
        block.help,
        reserved.length > 0
          ? i18n.t('manage:assistant_form.native_reserved', { keys: reserved.join(', ') })
          : '',
      ].filter(Boolean).join('\n'),
    });
  }
  return fields.length > 0
    ? { label: i18n.t('manage:assistant_form.section_extensions'), fields }
    : null;
}
