import isEqual from 'lodash/isEqual';

import { ApiError } from '@/api';

import type { EditFieldSpec, EditSectionSpec } from './editFields';

/**
 * What a draft differs from, what it is still missing, and what a refusal named.
 *
 * A record page reads and writes the same fields (docs/frontend-design.md §4),
 * so the two questions it has to answer on every keystroke are "has this
 * section changed" and "can it be written yet". After a refused write it asks
 * a third: which field the server's refusal is about.
 *
 * Plain functions rather than a hook: none of the questions involves React,
 * and a pure function is testable without rendering anything to ask it.
 */

/**
 * By the JSON the write would send, compared as a structure rather than as
 * text. The server's JSON columns keep no key order, so a record read back
 * after a save lists an object's keys in the database's order rather than the
 * order they were typed in; comparing the two texts would leave a saved field
 * dirty. A list's order is part of its value and still counts.
 */
function fieldChanged(
  field: EditFieldSpec,
  draft: Record<string, unknown>,
  baseline: Record<string, unknown>,
): boolean {
  return !isEqual(asSent(field.get(draft)), asSent(field.get(baseline)));
}

/** The value as the request body carries it: absent keys dropped, absence as null. */
function asSent(value: unknown): unknown {
  return JSON.parse(JSON.stringify(value ?? null));
}

/**
 * Whether any of `fields` differs from what is stored.
 *
 * A field the form does not let anyone write cannot be dirty, whatever the two
 * records say — it is reported, not edited, so a difference there is the
 * deployment's, not the reader's.
 */
export function sectionIsDirty(
  fields: EditFieldSpec[],
  draft: Record<string, unknown> | null | undefined,
  baseline: Record<string, unknown> | null | undefined,
): boolean {
  if (!draft || !baseline) return false;
  return fields.some((f) => f.editable !== false && fieldChanged(f, draft, baseline));
}

/**
 * Required fields left blank, anywhere in the form.
 *
 * They block every section, not only their own: the server validates the whole
 * payload on each write, so a card that let itself be saved while another
 * card's required field was empty would return a 400 about a field further
 * down the page.
 */
export function missingRequiredFields(
  sections: EditSectionSpec[],
  draft: Record<string, unknown> | null | undefined,
): EditFieldSpec[] {
  if (!draft) return [];
  return sections
    .flatMap((s) => s.fields)
    .filter((f) => f.required && f.editable !== false && !f.disabled)
    .filter((f) => {
      const value = f.get(draft);
      return (
        value == null ||
        (typeof value === 'string' && value.trim() === '') ||
        (Array.isArray(value) && value.length === 0)
      );
    });
}

/**
 * The field a refused write names, when it is one of the form's fields.
 *
 * The server validates a form's payload field by field
 * (`astrabox/core/service/orchestrator/schema_validation.py`) and refuses with
 * `INVALID_REQUEST` and a message that starts with the key it judged: `model is
 * required`, `packages[0].kind is required`. A nested key names the field that
 * holds it. Any other refusal names no field, and neither does a key the form
 * does not show.
 */
export function fieldNamedByRefusal(error: unknown, sections: EditSectionSpec[]): string | null {
  if (!(error instanceof ApiError) || error.code !== 'INVALID_REQUEST') return null;
  // `ApiError.message` reads `CODE: text`.
  const named = /^[A-Za-z_][\w.[\]]*/.exec(error.message.slice(error.code.length + 2))?.[0];
  if (!named) return null;
  const keys = new Set(sections.flatMap((section) => section.fields.map((field) => field.key)));
  if (keys.has(named)) return named;
  const holder = named.split(/[.[]/, 1)[0];
  return keys.has(holder) ? holder : null;
}

/**
 * `draft` with `fields` put back to what is stored, and everything else left
 * alone — a revert belongs to the section whose button was pressed, so edits
 * pending in another card survive it.
 */
export function revertFields(
  fields: EditFieldSpec[],
  draft: Record<string, unknown>,
  baseline: Record<string, unknown>,
): Record<string, unknown> {
  return fields.reduce((acc, f) => f.set(acc, f.get(baseline)), draft);
}
