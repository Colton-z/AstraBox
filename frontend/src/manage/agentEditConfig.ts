// Map the server-driven Agent FormSchema into the console's declarative edit
// config so create and edit share one field definition.
//
// This is the agent twin of environmentEditConfig.ts. Two agent-specific extensions
// over the environment bridge:
//   1. `env_ref` (environment_name) needs the live environment list as its select
//      options — the schema carries no `enum` for it, so the page passes the fetched
//      environments in and the options are built from them here.
//   2. Access control (visibility / admin / allowlist) is stored separately from
//      the schema. It is appended as its own section in the same edit grammar.
import { createElement, Fragment } from 'react';
import i18n from '@/i18n';
import type { EnvironmentConfig, FormFieldSchema, FormSchema } from '@/types';
import { deleteByPath, getByPath, setByPath } from '@/components/form/paths';

import type {
  DrawerSectionSpec,
  EditFieldSpec,
  EditFieldType,
  EditSectionSpec,
} from './console';
import { isEmptyValue } from './agentConfig';
import { ModelGatewayLink } from './ModelGatewayLink';

// Schema field type → console edit control. Structurally rich blocks
// (key_value / object / object_list / free-form) degrade to the mono JSON
// escape hatch — the one editor that can represent any shape losslessly. This is
// the same doctrine the environment bridge uses; the agent schema just exercises
// it harder (model_config / mcp_config / engine_options / default_repo / …).
function editType(field: FormFieldSchema): EditFieldType {
  switch (field.type) {
    case 'text':
      return 'textarea';
    case 'boolean':
      return 'toggle';
    case 'integer':
      return 'number';
    case 'enum':
    case 'env_ref':
      return 'select';
    case 'string_list':
      return 'list';
    case 'key_value':
    case 'object':
    case 'object_list':
      return 'json';
    case 'string':
    default:
      return 'text';
  }
}

const isEmpty = (v: unknown) =>
  v === undefined ||
  v === '' ||
  (v != null && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === 0);

// Build the get/set pair for one field, honoring `path` (display_meta.display_name)
// and pruning empties so `display_meta: {}` / `name: ''` debris is never persisted.
// Empty lists stay explicit: omitting an update field preserves its stored value.
function accessors(field: FormFieldSchema): Pick<EditFieldSpec, 'get' | 'set'> {
  return {
    get: (draft) => (field.path ? getByPath(draft, field.path) : draft[field.key]),
    set: (draft, value) => {
      // Integers: '' → drop the key (backend falls back to default), else Number.
      const v =
        field.type === 'integer' && value !== '' && value != null ? Number(value) : value;
      if (field.path) {
        if (field.path.startsWith('engine_options.')) {
          if (v === undefined) {
            const bag = draft.engine_options;
            if (!bag || typeof bag !== 'object' || Array.isArray(bag)) return draft;
            const next = { ...bag } as Record<string, unknown>;
            delete next[field.path.slice('engine_options.'.length)];
            return { ...draft, engine_options: next };
          }
          return setByPath(draft, field.path, v);
        }
        return (isEmpty(v) ? deleteByPath(draft, field.path) : setByPath(draft, field.path, v)) as Record<
          string,
          unknown
        >;
      }
      const next = { ...draft };
      if (field.key === 'engine_options') next[field.key] = v ?? {};
      else if (isEmpty(v)) delete next[field.key];
      else next[field.key] = v;
      return next;
    },
  };
}

export type AgentEditOpts = {
  /** True while creating (name input) vs editing (name locked). */
  nameEditable: boolean;
  /** Live environment list for the `env_ref` (environment_name) picker. */
  environments?: EnvironmentConfig[];
  /** Model ids advertised by the currently selected Environment. */
  models?: string[];
  /**
   * The selected Environment's engine declaration for the engine_options bag.
   * Non-empty: declares native JSON blocks, not their internal fields. Empty or absent:
   * the engine takes no bag and the engine_options control does not render.
   */
  engineOptionsSchema?: FormFieldSchema[];
};

const AGENT_JSON_EXAMPLES: Record<string, unknown> = {
  mcp_servers: { docs: { type: 'http', url: 'https://mcp.example.com/mcp' } },
  default_repo: {
    url: 'https://github.com/example/project.git',
    protocol: 'https',
    branch: 'main',
    depth: 1,
  },
  plugin_repos: [{
    url: 'https://github.com/anthropics/claude-plugins-official.git',
    protocol: 'https',
    branch: 'main',
    depth: 1,
    plugin_paths: ['plugins/frontend-design'],
  }],
};

function fieldExample(field: FormFieldSchema, type: EditFieldType): string | undefined {
  // Supplier placeholder copy need not be an input value. Only examples opt
  // into Tab acceptance; vendor-defined JSON keys remain the vendor's concern.
  if (field.placeholder !== undefined) return undefined;
  const key = `misc:agent_form.fields.${field.key}.example`;
  if (i18n.exists(key)) return i18n.t(key);
  if (field.key === 'skills') return 'https://github.com/anthropics/skills.git@main#skills/skill-creator';
  if (field.key === 'idle_hibernate_seconds') return '300';
  if (type === 'json') {
    return JSON.stringify(AGENT_JSON_EXAMPLES[field.key] ?? (field.type === 'object_list' ? [] : {}), null, 2);
  }
  return undefined;
}

function toFieldSpec(field: FormFieldSchema, opts: AgentEditOpts): EditFieldSpec {
  const isModel = field.key === 'model';
  const type: EditFieldType = isModel ? 'search_select' : editType(field);
  const example = fieldExample(field, type);

  // env_ref options come from the live env list rather than `field.enum`:
  // eligibility is an adapter capability returned by the server, and an
  // engine's identity is never a UI allowlist.
  const envOptions =
    field.type === 'env_ref'
      ? (opts.environments ?? [])
          .filter(
            (e) =>
              e.enabled !== false &&
              e.engine_available !== false &&
              e.supported_session_kinds?.includes('agent_chat') === true,
          )
          .map((e) => ({
            value: e.name,
            label: String(e.display_name || e.name),
          }))
      : undefined;

  return {
    key: field.key,
    // An engine-declared field carries its own copy (the engine author's
    // voice); platform schema fields keep copy in the i18n catalogue.
    label: field.label ?? i18n.t(`misc:agent_form.fields.${field.key}.label`),
    type,
    ...accessors(field),
    // `name` is the identity key: editable only while creating; locked in edit.
    editable: field.key === 'name' ? opts.nameEditable : true,
    required: field.required,
    idTag: field.key,
    help: isModel
      ? createElement(Fragment, null,
          i18n.t('misc:agent_form.fields.model.help'), ' ', createElement(ModelGatewayLink))
      : field.help
      ?? (i18n.exists(`misc:agent_form.fields.${field.key}.help`)
        ? i18n.t(`misc:agent_form.fields.${field.key}.help`)
        : undefined),
    options: isModel
      ? (opts.models ?? []).map((model) => ({ value: model, label: model }))
      : envOptions ?? field.enum?.map((v) => ({ value: v, label: v })),
    example,
    placeholder: field.placeholder ?? example ?? (
      field.type === 'env_ref'
        ? i18n.t('manage:agent_form.select_env_placeholder')
        : isModel
          ? i18n.t('manage:agent_form.select_model_placeholder')
          : field.type === 'enum'
            ? i18n.t('manage:agent_form.select_placeholder')
            : field.key === 'name'
              ? i18n.t('manage:agent_form.name_placeholder')
              : undefined),
    searchPlaceholder: isModel
      ? i18n.t('manage:agent_form.search_model_placeholder')
      : undefined,
    emptyLabel: isModel
      ? i18n.t('manage:agent_form.no_models')
      : field.type === 'env_ref'
        ? i18n.t('manage:agent_form.no_environments')
        : undefined,
    allowCustom: isModel,
    rows: field.type === 'text' ? (field.key === 'system' ? 8 : 3) : type === 'json' ? 6 : undefined,
    mono: field.key === 'system',
  };
}

// ── Access control (not in the schema) ──────────────────────────────────────
// The agent owns visibility / admins / allowed_user_ids outside the template
// schema. Render them as a trailing edit section in the same grammar so access
// is edited inline with the rest. created_by is server-stamped + locked.
const visibilityOptions = () => [
  { value: 'public', label: i18n.t('manage:visibility.public_option') },
  { value: 'private', label: i18n.t('manage:visibility.private_option') },
  { value: 'allowlist', label: i18n.t('manage:visibility.allowlist_option') },
];

function accessControlSection(draft: Record<string, unknown>): EditSectionSpec {
  const visibility = String(draft.visibility || 'private');
  const fields: EditFieldSpec[] = [
    {
      key: 'visibility',
      label: i18n.t('manage:agent_form.visibility_label'),
      type: 'select',
      get: (d) => (d.visibility == null ? 'private' : d.visibility),
      set: (d, v) => ({ ...d, visibility: v }),
      options: visibilityOptions(),
      idTag: 'visibility',
      help: i18n.t('manage:agent_form.visibility_help'),
    },
    {
      key: 'admins',
      label: i18n.t('manage:agent_form.admins_label'),
      type: 'list',
      get: (d) => d.admins,
      set: (d, v) => {
        const list = Array.isArray(v) ? v.map(String).filter((s) => s.trim() !== '') : [];
        const next = { ...d };
        if (list.length === 0) delete next.admins;
        else next.admins = list;
        return next;
      },
      placeholder: i18n.t('manage:agent_form.user_id_placeholder'),
      idTag: 'admins',
      help: i18n.t('manage:agent_form.admins_help'),
    },
  ];
  // allowlist only matters under allowlist visibility — show it only then, keeping
  // the create and edit forms free of a dead field for public/private agents.
  if (visibility === 'allowlist') {
    fields.push({
      key: 'allowed_user_ids',
      label: i18n.t('manage:agent_form.allowlist_label'),
      type: 'list',
      get: (d) => d.allowed_user_ids,
      set: (d, v) => {
        const list = Array.isArray(v) ? v.map(String).filter((s) => s.trim() !== '') : [];
        const next = { ...d };
        if (list.length === 0) delete next.allowed_user_ids;
        else next.allowed_user_ids = list;
        return next;
      },
      placeholder: i18n.t('manage:agent_form.user_id_placeholder'),
      idTag: 'allowed_user_ids',
      help: i18n.t('manage:agent_form.allowlist_help'),
    });
  }
  return { label: i18n.t('manage:agent_form.access_control_section'), fields };
}

/**
 * Object fields whose `item_schema` is rendered as individual controls instead
 * of the JSON escape hatch — the agent-side twin of the set in
 * `environmentEditConfig.ts`.
 *
 * While the set is empty, every object block on this form is edited as JSON. A
 * set makes enabling an inline field declarative, and
 * `tests/agent_form_copy_test.py` uses the same keys to require labels for each
 * exposed sub-field.
 */
const INLINE_ITEM_SCHEMA_KEYS = new Set<string>([]);

/** Expand one object field's `item_schema` into dotted-path scalar fields. */
function inlineItemFields(field: FormFieldSchema, opts: AgentEditOpts): EditFieldSpec[] {
  return (field.item_schema ?? []).map((sub) =>
    // key === path, so the label lookup and the payload agree on the name:
    // `default_repo.url` reads `misc:agent_form.fields.default_repo.url.label`.
    toFieldSpec({ ...sub, key: `${field.key}.${sub.key}`, path: `${field.key}.${sub.key}` }, opts),
  );
}

/** Each engine declares native JSON targets; their contents belong to the vendor. */
function engineOptionsFields(field: FormFieldSchema, opts: AgentEditOpts): EditFieldSpec[] {
  const declared = opts.engineOptionsSchema ?? [];
  return declared.map((block) => toFieldSpec({
    ...block,
    key: `${field.key}.${block.key}`,
    path: `${field.key}.${block.key}`,
    label: block.label ?? block.key,
    help: [block.help, i18n.t('misc:agent_form.fields.engine_options.help')]
      .filter(Boolean).join('\n'),
  }, opts));
}

function expandField(field: FormFieldSchema, opts: AgentEditOpts): EditFieldSpec[] {
  if (field.key === 'engine_options') {
    return engineOptionsFields(field, opts);
  }
  return INLINE_ITEM_SCHEMA_KEYS.has(field.key) && field.item_schema?.length
    ? inlineItemFields(field, opts)
    : [toFieldSpec(field, opts)];
}

/**
 * Full edit config (schema groups + access control) for an agent, in schema order.
 * `nameEditable` distinguishes create (name input) from edit (name locked). The
 * access-control section is appended last and reacts to the current visibility.
 */
export function buildAgentEditSections(
  schema: FormSchema | null,
  draft: Record<string, unknown> | null,
  opts: AgentEditOpts,
): { sections: EditSectionSpec[]; advancedKeys: Set<string> } {
  if (!schema) return { sections: [], advancedKeys: new Set() };
  // Which keys a form MAY fold. Whether it does is the page's call — a create
  // page can, a record page cannot, because there each section owns its own
  // save and lifting fields out of a card would separate them from it.
  const advancedKeys = new Set(
    schema.fields
      .filter((f) => f.advanced)
      .flatMap((f) => expandField(f, opts).map((spec) => spec.key)),
  );
  const schemaSections = schema.groups
    .map((group) => ({
      label: i18n.t(`misc:agent_form.groups.${group.id}.label`),
      fields: schema.fields
        .filter((f) => f.group === group.id)
        .flatMap((f) => expandField(f, opts)),
    }))
    .filter((s) => s.fields.length > 0);
  // Access control is not in the schema, so it carries no advancedness and
  // stays where a reader expects it: visible.
  const sections = [...schemaSections, accessControlSection(draft ?? {})];
  return { sections, advancedKeys };
}
