// Map the server-driven Agent FormSchema into the console's declarative edit
// config so create and edit share one field definition.
//
// This is the agent twin of runtimeProfileEditConfig.ts. Two agent-specific extensions
// over the environment bridge:
//   1. `runtime_profile_ref` (runtime_profile_name) needs the live environment list as its select
//      options — the schema carries no `enum` for it, so the page passes the fetched
//      runtime_profiles in and the options are built from them here.
//   2. Access control (visibility / admin / allowlist) is stored separately from
//      the schema. It is appended as its own section in the same edit grammar.
import { createElement, Fragment } from 'react';
import i18n from '@/i18n';
import type {
  AgentCategory,
  AgentConfig,
  FormFieldSchema,
  FormSchema,
  ModelAccount,
  ModelAccountVendor,
  RuntimeProfileConfig,
} from '@/types';
import { deleteByPath, getByPath, setByPath } from '@/components/form/paths';

import type {
  DrawerSectionSpec,
  EditFieldSpec,
  EditFieldType,
  EditSectionSpec,
} from './console';
import {
  MAX_AGENT_TAG_LENGTH,
  MAX_AGENT_TAGS,
  isEmptyValue,
  normalizeAgentTag,
} from './agentConfig';
import { ModelGatewayLink } from './ModelGatewayLink';
import { ManageUsageLink, ModelAccountsPageLink } from './ModelAccountLinks';

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
    case 'runtime_profile_ref':
      return 'select';
    case 'string_list':
      return 'list';
    case 'key_value':
    case 'object':
    case 'object_list':
    case 'skill_list':
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
          if (v == null) {
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

/** One model the model field offers: the id it stores, and the name it shows. */
export type ModelChoice = { value: string; label: string };

/**
 * The model endpoint providers whose conversations spend their owner's model
 * account rather than the deployment's model gateway, and the kind of account
 * each spends.
 */
const OWNER_ACCOUNT_PROVIDERS: Record<string, ModelAccountVendor> = {
  chatgpt_plan: 'chatgpt_plan',
  anthropic_api_key: 'anthropic_api_key',
};

/** The kind of model account an Agent on this RuntimeProfile spends; undefined where the deployment pays. */
export function ownerAccountVendor(profile: RuntimeProfileConfig | undefined): ModelAccountVendor | undefined {
  return OWNER_ACCOUNT_PROVIDERS[profile?.endpoint_provider ?? ''];
}

/**
 * The Agent payload to send for this RuntimeProfile.
 *
 * `model_account_id` means something only on a profile that spends an owner's
 * account, so it is not sent for any other: a choice left in the draft by a
 * profile the reader switched away from would otherwise be stored where
 * nothing reads it.
 */
export function agentPayloadForProfile<T extends object>(
  payload: T,
  profile: RuntimeProfileConfig | undefined,
): T {
  if (ownerAccountVendor(profile)) return payload;
  const next = { ...payload } as Record<string, unknown>;
  delete next.model_account_id;
  return next as T;
}

/** What the form knows on a RuntimeProfile that spends an owner's model account. */
export type OwnerAccountChoice = {
  /** The kind of account the profile spends. */
  vendor: ModelAccountVendor;
  /**
   * The reader's accounts of that vendor, offered for `model_account_id`.
   * Absent when the Agent is someone else's: the account it names is its
   * creator's, and the reader's own conversations with it spend the reader's
   * default.
   */
  accounts?: ModelAccount[];
  /** Why the reader's accounts could not be read; said under the account field. */
  accountsError?: string;
  /** Why the account's models could not be read; said where the reader picks a model. */
  modelsError?: string;
};

export type AgentEditOpts = {
  /** True while creating (name input) vs editing (name locked). */
  nameEditable: boolean;
  /** Live environment list for the `runtime_profile_ref` (runtime_profile_name) picker. */
  runtime_profiles?: RuntimeProfileConfig[];
  /**
   * The models the model field offers: the selected RuntimeProfile's gateway
   * catalog, or on a profile that spends an owner's account the chosen
   * account's models.
   */
  models?: ModelChoice[];
  /** Present when the selected RuntimeProfile spends an owner's model account. */
  ownerAccount?: OwnerAccountChoice;
  /**
   * The selected RuntimeProfile's engine declaration for the engine_options bag.
   * Non-empty: declares native JSON blocks, not their internal fields. Empty or absent:
   * the engine takes no bag and the engine_options control does not render.
   */
  engineOptionsSchema?: FormFieldSchema[];
  /**
   * The display name of the extension catalog whose pickers stand beside the
   * MCP and Skills fields (`CatalogExtensionField`). Absent where no picker
   * renders — a create page, or a deployment that runs no catalog — and the two
   * fields then read as the Agent's MCP servers and Skills, not as additions to
   * a catalog selection.
   */
  catalogName?: string;
  /** The categories a platform administrator defined, for the `categories` picker. */
  categories?: AgentCategory[];
  agents?: AgentConfig[];
  /** Tags the reader's Agents already carry, most used first, offered while typing one. */
  tagSuggestions?: string[];
};

/** Label and help of the Agent's own MCP and Skills fields, which depend on a catalog beside them. */
function ownExtensionCopy(key: string, catalogName: string | undefined): { label: string; help: string } | null {
  if (key !== 'mcp_servers' && key !== 'skills') return null;
  const base = `misc:agent_form.fields.${key}`;
  return catalogName
    ? { label: i18n.t(`${base}.label`), help: i18n.t(`${base}.help`, { catalog: catalogName }) }
    : { label: i18n.t(`${base}.label_alone`), help: i18n.t(`${base}.help_alone`) };
}

const AGENT_JSON_EXAMPLES: Record<string, unknown> = {
  mcp_servers: { docs: { type: 'http', url: 'https://mcp.example.com/mcp' } },
  default_repo: {
    url: 'https://github.com/example/project.git',
    protocol: 'https',
    branch: 'main',
    depth: 1,
  },
  packages: [{
    kind: 'claude-plugin',
    url: 'https://github.com/anthropics/claude-plugins-official.git',
    protocol: 'https',
    branch: 'main',
    depth: 1,
    paths: ['plugins/frontend-design'],
  }],
};

function fieldExample(field: FormFieldSchema, type: EditFieldType): string | undefined {
  // Supplier placeholder copy need not be an input value. Only examples opt
  // into Tab acceptance; vendor-defined JSON keys remain the vendor's concern.
  if (field.placeholder !== undefined) return undefined;
  const key = `misc:agent_form.fields.${field.key}.example`;
  if (i18n.exists(key)) return i18n.t(key);
  if (field.key === 'skills') return '["https://github.com/anthropics/skills.git@main#skills/skill-creator"]';
  if (field.key === 'idle_hibernate_seconds') return '300';
  if (type === 'json') {
    return JSON.stringify(AGENT_JSON_EXAMPLES[field.key] ?? (field.type === 'object_list' ? [] : {}), null, 2);
  }
  return undefined;
}

/**
 * The controls for the two fields the home page finds an Agent by.
 *
 * The schema types both as plain string lists. Categories are ids the reader
 * picks by name from the administrator's list; tags are free text the server
 * normalizes and caps, so the form shows the stored form and the cap while
 * the tag is typed.
 */
function catalogControl(field: FormFieldSchema, opts: AgentEditOpts): Partial<EditFieldSpec> {
  if (field.key === 'a2a_agent_ids') {
    return {
      type: 'multi_select',
      options: (opts.agents ?? []).map((agent) => ({
        value: agent.agent_id,
        label: String(agent.display_meta?.display_name || agent.name),
      })),
      unknownOptionLabel: () => i18n.t('misc:agent_form.fields.a2a_agent_ids.unavailable'),
    };
  }
  if (field.key === 'categories') {
    return {
      type: 'multi_select',
      options: (opts.categories ?? []).map((category) => ({
        value: category.category_id,
        label: category.name,
      })),
      unknownOptionLabel: () => i18n.t('manage:agent_form.category_unavailable'),
      placeholder: i18n.t('manage:agent_form.select_categories_placeholder'),
      searchPlaceholder: i18n.t('manage:agent_form.search_categories_placeholder'),
      // With none defined, the open list says who adds them; otherwise it is
      // a search that matched nothing, which the picker says itself.
      emptyLabel: opts.categories?.length ? undefined : i18n.t('manage:agent_form.no_categories'),
    };
  }
  if (field.key === 'tags') {
    return {
      type: 'tags',
      suggestions: opts.tagSuggestions ?? [],
      normalizeItem: normalizeAgentTag,
      maxItems: MAX_AGENT_TAGS,
      maxItemLength: MAX_AGENT_TAG_LENGTH,
      // The example is a placeholder only: Tab takes an example into a text
      // field, and a tag is added by Enter or a comma instead.
      example: undefined,
      placeholder: i18n.t('misc:agent_form.fields.tags.example'),
    };
  }
  return {};
}

/** How an account reads in the account picker: its label, its email, and a dead sign-in. */
function accountOptionLabel(account: ModelAccount): string {
  const name = account.email && account.email !== account.label
    ? `${account.label} · ${account.email}`
    : account.label;
  return account.status === 'sign_in_required'
    ? i18n.t('misc:agent_form.fields.model_account_id.needs_sign_in', { account: name })
    : name;
}

/**
 * The account the creator's conversations spend: one of the reader's accounts
 * of the profile's vendor, or their default of it.
 *
 * The empty choice is the default account and is stored as `null`, so picking
 * it on a saved Agent clears an account chosen before; an absent key would
 * leave the stored one in place.
 */
function modelAccountField(field: FormFieldSchema, choice: OwnerAccountChoice, accounts: ModelAccount[]): EditFieldSpec {
  const fallback = accounts.find((account) => account.is_default);
  const anthropic = choice.vendor === 'anthropic_api_key';
  return {
    key: field.key,
    label: i18n.t(anthropic
      ? 'misc:agent_form.fields.model_account_id.label_anthropic_api_key'
      : 'misc:agent_form.fields.model_account_id.label'),
    type: 'select',
    get: (draft) => (draft.model_account_id == null ? '' : String(draft.model_account_id)),
    set: (draft, value) => ({ ...draft, model_account_id: value ? String(value) : null }),
    options: [
      {
        value: '',
        label: fallback
          ? i18n.t('misc:agent_form.fields.model_account_id.default_named', { account: fallback.label })
          : i18n.t('misc:agent_form.fields.model_account_id.default'),
      },
      ...accounts.map((account) => ({ value: account.account_id, label: accountOptionLabel(account) })),
    ],
    idTag: field.key,
    help: createElement(Fragment, null,
      choice.accountsError ?? i18n.t(anthropic
        ? 'misc:agent_form.fields.model_account_id.help_anthropic_api_key'
        : 'misc:agent_form.fields.model_account_id.help'), ' ',
      createElement(ModelAccountsPageLink)),
  };
}

/** The model field's help on a profile that spends an owner's account of `vendor`. */
function ownerAccountModelHelp(vendor: ModelAccountVendor) {
  return vendor === 'anthropic_api_key'
    ? createElement(Fragment, null,
      i18n.t('misc:agent_form.fields.model.help_anthropic_api_key'), ' ', createElement(ModelAccountsPageLink))
    : createElement(Fragment, null,
      i18n.t('misc:agent_form.fields.model.help_chatgpt_plan'), ' ', createElement(ManageUsageLink));
}

function toFieldSpec(field: FormFieldSchema, opts: AgentEditOpts): EditFieldSpec {
  if (field.key === 'model_account_id' && opts.ownerAccount?.accounts) {
    return modelAccountField(field, opts.ownerAccount, opts.ownerAccount.accounts);
  }
  const isModel = field.key === 'model';
  const ownerAccount = isModel ? opts.ownerAccount : undefined;
  const ownCopy = ownExtensionCopy(field.key, opts.catalogName);
  const type: EditFieldType = isModel ? 'search_select' : editType(field);
  const example = fieldExample(field, type);

  // runtime_profile_ref options come from the live env list rather than `field.enum`:
  // eligibility is an adapter capability returned by the server, and an
  // engine's identity is never a UI allowlist.
  const envOptions =
    field.type === 'runtime_profile_ref'
      ? (opts.runtime_profiles ?? [])
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
    label: field.label ?? ownCopy?.label ?? i18n.t(`misc:agent_form.fields.${field.key}.label`),
    type,
    ...accessors(field),
    // `name` is the identity key: editable only while creating; locked in edit.
    editable: field.key === 'name' ? opts.nameEditable : true,
    required: field.required,
    idTag: field.key,
    help: ownerAccount
      ? ownerAccountModelHelp(ownerAccount.vendor)
      : isModel
      ? createElement(Fragment, null,
          i18n.t('misc:agent_form.fields.model.help'), ' ', createElement(ModelGatewayLink))
      : field.help
      ?? ownCopy?.help
      ?? (i18n.exists(`misc:agent_form.fields.${field.key}.help`)
        ? i18n.t(`misc:agent_form.fields.${field.key}.help`)
        : undefined),
    options: isModel
      ? opts.models ?? []
      : envOptions ?? field.enum?.map((v) => ({ value: v, label: field.key === 'a2a_access'
        ? i18n.t(`misc:agent_form.fields.a2a_access.options.${v}`) : v })),
    example,
    placeholder: field.placeholder ?? example ?? (
      field.type === 'runtime_profile_ref'
        ? i18n.t('manage:agent_form.select_runtime_profile_placeholder')
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
    emptyLabel: ownerAccount
      ? ownerAccount.modelsError ?? i18n.t('manage:agent_form.no_account_models')
      : isModel
      ? i18n.t('manage:agent_form.no_models')
      : field.type === 'runtime_profile_ref'
        ? i18n.t('manage:agent_form.no_runtime_profiles')
        : undefined,
    allowCustom: isModel,
    rows: field.type === 'text' ? (field.key === 'system' ? 8 : 3) : type === 'json' ? 6 : undefined,
    mono: field.key === 'system',
    ...catalogControl(field, opts),
  };
}

/**
 * The fields the home page finds an Agent by. The schema marks tags advanced,
 * among the display fields; the form keeps both visible, tags directly after
 * categories, because together they decide where the Agent is listed and what
 * a search finds it by.
 */
const CATALOG_KEYS = new Set(['categories', 'tags']);

/**
 * The account decides which models the model field offers, so it stands
 * directly after the model, and is never folded away from it. It shows only on
 * a RuntimeProfile that spends an owner's account, where it is a question
 * worth asking.
 */
function accountBesideModel(fields: FormFieldSchema[], opts: AgentEditOpts): FormFieldSchema[] {
  const account = fields.find((field) => field.key === 'model_account_id');
  if (!account) return fields;
  const rest = fields.filter((field) => field !== account);
  if (!opts.ownerAccount?.accounts) return rest;
  return rest.flatMap((field) => (field.key === 'model' ? [field, account] : [field]));
}

function catalogFieldsTogether(fields: FormFieldSchema[]): FormFieldSchema[] {
  const tags = fields.find((field) => field.key === 'tags');
  if (!tags || !fields.some((field) => field.key === 'categories')) return fields;
  return fields
    .filter((field) => field !== tags)
    .flatMap((field) => (field.key === 'categories' ? [field, tags] : [field]));
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
 * `runtimeProfileEditConfig.ts`.
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
      .filter((f) => f.advanced && !CATALOG_KEYS.has(f.key) && f.key !== 'model_account_id')
      .flatMap((f) => expandField(f, opts).map((spec) => spec.key)),
  );
  const schemaSections = schema.groups
    .map((group) => ({
      label: i18n.t(`misc:agent_form.groups.${group.id}.label`),
      fields: accountBesideModel(catalogFieldsTogether(schema.fields.filter((f) => f.group === group.id)), opts)
        .filter((f) => f.key !== 'a2a_agent_ids' || draft?.a2a_access === 'selected')
        .flatMap((f) => expandField(f, opts)),
    }))
    .filter((s) => s.fields.length > 0);
  // Access control is not in the schema, so it carries no advancedness and
  // stays where a reader expects it: visible.
  const sections = [...schemaSections, accessControlSection(draft ?? {})];
  return { sections, advancedKeys };
}
