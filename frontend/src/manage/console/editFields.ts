import type { PillTone } from './StatusPill';

/**
 * The console's field vocabulary.
 *
 * A page declares its editable fields once and they render as controls
 * wherever they are read (docs/frontend-design.md §4). Every console form
 * speaks this vocabulary, so it lives here rather than inside any one of them.
 */

/**
 * One label/value row in a fact rail or a card — a `.console-label` micro-label
 * over (or beside) a value. Data values render mono (`.console-val`) to match the
 * console's data grammar; long values wrap, ids/timestamps stay tabular.
 */
export type DrawerFieldSpec = {
  label: React.ReactNode;
  value: React.ReactNode;
  /** Render the value in the mono/tabular face. Default true (kit data grammar). */
  mono?: boolean;
};

/** A labelled section: a section head + a stack of fields. */
export type DrawerSectionSpec = {
  label: React.ReactNode;
  fields: DrawerFieldSpec[];
};

/** A record's state, as a pill beside its heading. */
export type ConsoleStatus = {
  tone: PillTone;
  label: React.ReactNode;
};

/* ================================================================== */
/*  Field grammar — one declaration, no modes.                        */
/*                                                                     */
/*  A field is declared once (type + key + get/set). There is no view  */
/*  rendering of the same fields to switch to: an Edit button that     */
/*  only changes how a value is painted is what                        */
/*  docs/frontend-design.md §4 forbids. Fields that genuinely cannot   */
/*  be changed here say so with `editable: false`.                     */
/*                                                                     */
/*  The page owns the draft (a plain object) and a get/set pair,       */
/*  keeping the panel agnostic about nested storage shapes             */
/*  (display_meta.*, etc).                                             */
/* ================================================================== */

export type EditFieldType =
  | 'text'
  | 'password'
  | 'textarea'
  | 'select'
  | 'search_select'
  | 'toggle'
  | 'number'
  | 'list'
  | 'json';

export type EditFieldSpec = {
  /** Stable field key (used for React keys + label↔control pairing). */
  key: string;
  label: React.ReactNode;
  type: EditFieldType;
  /** Read the value from the draft. */
  get: (draft: Record<string, unknown>) => unknown;
  /** Return a new draft with this field set (immutable). */
  set: (draft: Record<string, unknown>, value: unknown) => Record<string, unknown>;
  /** When false the field is shown read-only (mono value, no control). Default true. */
  editable?: boolean;
  /**
   * Render the real control, but disabled. Distinct from `editable: false`: that
   * one says "this value is not yours to change here" (an identity key), while
   * this says "this control exists but the deployment cannot honor it" — the
   * shape of the setting stays visible, and `help` carries the reason. A
   * setting silently accepted and never acted on is the thing this avoids.
   */
  disabled?: boolean;
  required?: boolean;
  /** Options for `select` and `search_select`. */
  options?: { value: string; label: React.ReactNode }[];
  placeholder?: string;
  /** Suggested input, shown while empty and accepted only by an unmodified Tab. */
  example?: string;
  searchPlaceholder?: string;
  emptyLabel?: string;
  /** Let a searchable catalog accept an id the gateway did not enumerate. */
  allowCustom?: boolean;
  /** Mono key tag after the label (the schema key) — the kit's id grammar. */
  idTag?: React.ReactNode;
  help?: React.ReactNode;
  /** Textarea rows / json rows. */
  rows?: number;
  /** Treat the value as code (mono) in a textarea. */
  mono?: boolean;
  /** On/off legends for `toggle`. */
  onLabel?: React.ReactNode;
  offLabel?: React.ReactNode;
  /**
   * Reject a draft this field cannot be saved in, returning the sentence to
   * show under the control (or nothing when it is fine).
   *
   * It is handed the whole draft, not just this field's value, because the
   * constraints worth catching here are the ones between fields — a minimum
   * above its maximum, a switch turned on without the capacity it needs. The
   * owning section's save is blocked while any of its own fields complain, so
   * the answer arrives under the field the operator is looking at instead of as
   * a 400 from the server after the subject has already closed.
   *
   * Not a replacement for the server's own validation: the server stays the
   * authority, and this states the same rules where they can still be fixed.
   * Skipped for fields that are disabled or read-only — a rule the operator has
   * no control to satisfy must not hold the form hostage.
   */
  validate?: (draft: Record<string, unknown>) => React.ReactNode | undefined;
};

export type EditSectionSpec = {
  label: React.ReactNode;
  fields: EditFieldSpec[];
};

/**
 * Split a section list into what a form shows first and what it folds away.
 *
 * Advancedness is the schema's word, not a page's: the server marks a field
 * `advanced` and every console form that folds does it the same way. This lives
 * beside the vocabulary rather than in one form's config module, because the
 * form that needs it (Agent create) and the one that must not fold (Environment,
 * an admin surface whose reader already chose the sandbox backend) are two
 * different callers of the same grammar.
 *
 * Only a CREATE flow can fold. Once a record exists each section owns its own
 * save against a live row, so lifting some of a card's fields into a disclosure
 * elsewhere would separate them from the control that writes them.
 */
export function splitAdvanced(
  sections: EditSectionSpec[],
  advancedKeys: Set<string>,
): { essential: EditSectionSpec[]; advanced: EditSectionSpec[]; advancedCount: number } {
  const pick = (want: boolean) =>
    sections
      .map((s) => ({ ...s, fields: s.fields.filter((f) => advancedKeys.has(f.key) === want) }))
      .filter((s) => s.fields.length > 0);
  const advanced = pick(true);
  return {
    essential: pick(false),
    advanced,
    advancedCount: advanced.reduce((n, s) => n + s.fields.length, 0),
  };
}
