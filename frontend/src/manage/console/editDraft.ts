import type { EditFieldSpec, EditSectionSpec } from './editFields';

/**
 * What a draft differs from, and what it is still missing.
 *
 * A record page reads and writes the same fields (docs/frontend-design.md §4),
 * so the two questions it has to answer on every keystroke are "has this
 * section changed" and "can it be written yet".
 *
 * Plain functions rather than a hook: neither question involves React, and a
 * pure function is testable without rendering anything to ask it.
 */

/** JSON rather than `===`: a field's value may be a list or an object. */
function fieldChanged(
  field: EditFieldSpec,
  draft: Record<string, unknown>,
  baseline: Record<string, unknown>,
): boolean {
  return JSON.stringify(field.get(draft) ?? null) !== JSON.stringify(field.get(baseline) ?? null);
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
