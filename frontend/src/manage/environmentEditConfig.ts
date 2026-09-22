// Map the server-driven environment FormSchema into the console's declarative
// edit config (EditSectionSpec[]), so inline edit and create render the same
// field set the schema declares, without a second copy of the field list. The
// bridge other entities follow: schema groups → sections, schema fields → typed
// edit specs, reusing the form's dotted-path get/set so nested storage
// (display_meta.*) stays canonical.
import i18n from '@/i18n';
import type { FormFieldSchema, FormSchema } from '@/types';
import { deleteByPath, getByPath, setByPath } from '@/components/form/paths';

import type { EditFieldSpec, EditFieldType, EditSectionSpec } from './console';

// Schema field type → console edit control. Structurally rich blocks
// (key_value / object / object_list / free-form) degrade to the mono JSON
// escape hatch — the one editor that can represent any shape losslessly.
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

// Build the get/set pair for one field, honoring `path` (display_meta.display_name)
// and pruning empties so the setter never persists `display_meta: {}` / `name: ''` debris.
function accessors(field: FormFieldSchema): Pick<EditFieldSpec, 'get' | 'set'> {
  const loc = field.path || field.key;
  const isEmpty = (v: unknown) =>
    v === undefined ||
    v === '' ||
    (Array.isArray(v) && v.length === 0) ||
    (v != null && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === 0);

  return {
    get: (draft) => (field.path ? getByPath(draft, field.path) : draft[field.key]),
    set: (draft, value) => {
      // Integers: '' → drop the key (backend falls back to default), else Number.
      const v =
        field.type === 'integer' && value !== '' && value != null ? Number(value) : value;
      if (field.path) {
        return (isEmpty(v) ? deleteByPath(draft, field.path) : setByPath(draft, field.path, v)) as Record<
          string,
          unknown
        >;
      }
      const next = { ...draft };
      if (isEmpty(v)) delete next[field.key];
      else next[field.key] = v;
      return next;
    },
  };
}

/**
 * The dropdown text for one enum value.
 *
 * Falls back to the raw value, which is right for the enums whose values are
 * the name an operator works in — a backend is called `open_sandbox` in the
 * docs, the logs and the env var, and translating it would make three places
 * disagree. Where the value is an internal spelling of a choice, copy exists
 * and wins: `sandbox_tenancy` is `conversation`/`agent` on the wire and "a
 * sandbox per conversation" / "one sandbox per agent" on screen.
 */
function optionLabel(key: string, value: string): string {
  const path = `manage:env_form.fields.${key}.options.${value}`;
  return i18n.exists(path) ? i18n.t(path) : value;
}

function toFieldSpec(field: FormFieldSchema, opts: { nameEditable: boolean }): EditFieldSpec {
  const type = editType(field);
  return {
    key: field.key,
    label: i18n.t(`manage:env_form.fields.${field.key}.label`),
    type,
    ...accessors(field),
    // `name` is the identity key: editable only while creating; locked in edit.
    editable: field.key === 'name' ? opts.nameEditable : true,
    required: field.required,
    idTag: field.key,
    help: i18n.exists(`manage:env_form.fields.${field.key}.help`)
      ? i18n.t(`manage:env_form.fields.${field.key}.help`)
      : undefined,
    options: field.enum?.map((v) => ({ value: v, label: optionLabel(field.key, v) })),
    placeholder:
      field.type === 'enum'
        ? i18n.t('manage:env_form.select_placeholder')
        : type === 'json'
          ? '{ }'
          : field.key === 'name'
            ? i18n.t('manage:env_form.name_placeholder')
            : undefined,
    rows: field.type === 'text' ? 3 : type === 'json' ? 5 : undefined,
  };
}

/**
 * Object fields whose `item_schema` is rendered as individual controls instead
 * of the JSON escape hatch.
 *
 * `networking` qualifies because its sub-fields are a closed set
 * of product-level scalars. Handing it over as raw JSON would expose a
 * substrate-shaped escape hatch instead of the Environment choices they are.
 *
 * `provider_access` does not qualify: it carries `api_key`, which read views
 * mask. Inlining it would put a masked value in a text input, and saving the
 * form would then persist the mask over the real credential. It stays behind
 * the JSON field until the read side stops masking or the write side learns to
 * leave an untouched secret alone.
 */
const INLINE_ITEM_SCHEMA_KEYS = new Set(['networking']);

/** Expand one object field's `item_schema` into dotted-path scalar fields. */
function inlineItemFields(
  field: FormFieldSchema,
  opts: { nameEditable: boolean },
): EditFieldSpec[] {
  return (field.item_schema ?? []).map((sub) => {
    const path = `${field.key}.${sub.key}`;
    return toFieldSpec({ ...sub, key: path, path }, opts);
  });
}

function expandField(
  field: FormFieldSchema,
  opts: { nameEditable: boolean },
): EditFieldSpec[] {
  return INLINE_ITEM_SCHEMA_KEYS.has(field.key) && field.item_schema?.length
    ? inlineItemFields(field, opts)
    : [toFieldSpec(field, opts)];
}

/**
 * Full edit config for an environment, in schema
 * order. `nameEditable` distinguishes create (name input) from edit (name locked).
 * Advanced fields are tagged so the page can fold them.
 *
 * `idleActions` is the list of idle actions the backend can carry out
 * (`GET /api/v1/admin/sandbox-idle-action`). Where it is known, `idle_action`
 * offers exactly those: an action the backend cannot honor is refused at write
 * time, so offering it would only let an operator discover that by being
 * rejected. The remaining choices stay valid, and `terminate` must remain
 * selectable on a backend that cannot pause. Omitted or empty leaves every
 * action the schema declares on offer.
 */
export function buildEnvEditSections(
  schema: FormSchema | null,
  opts: {
    nameEditable: boolean;
    idleActions?: string[] | null;
  },
): { sections: EditSectionSpec[] } {
  if (!schema) return { sections: [] };
  const idleActions = opts.idleActions?.length ? opts.idleActions : null;
  const sections = schema.groups
    .map((group) => ({
      label: i18n.t(`manage:env_form.groups.${group.id}.label`),
      fields: schema.fields
        .filter((f) => f.group === group.id)
        .flatMap((f) => expandField(f, opts))
        .map((spec) =>
          idleActions && spec.key === 'idle_action' && spec.options
            ? {
                ...spec,
                // Every option is a real action — an environment states its own
                // and there is no empty value deferring to the installation —
                // so the narrowing applies to all of them.
                options: spec.options.filter((option) =>
                  idleActions.includes(option.value),
                ),
              }
            : spec,
        ),
    }))
    .filter((s) => s.fields.length > 0);
  return { sections };
}
