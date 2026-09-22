/**
 * The two questions a record page asks on every keystroke, answered without
 * rendering anything.
 *
 * Asked of the functions directly: a wrong answer here is a wrong answer
 * whatever it is rendered into, and reading it out of a rendered form makes a
 * logic failure and a layout failure look the same.
 */
import { describe, expect, it } from 'vitest';

import { missingRequiredFields, revertFields, sectionIsDirty } from './editDraft';
import type { EditFieldSpec, EditSectionSpec } from './editFields';

const field = (key: string, extra: Partial<EditFieldSpec> = {}): EditFieldSpec => ({
  key,
  label: key,
  type: 'text',
  get: (d) => d[key],
  set: (d, v) => ({ ...d, [key]: v }),
  ...extra,
});

describe('sectionIsDirty', () => {
  const fields = [field('name'), field('image')];

  it('is false when every field matches what is stored', () => {
    const stored = { name: 'a', image: 'b' };
    expect(sectionIsDirty(fields, { ...stored }, stored)).toBe(false);
  });

  it('is true when any one field differs', () => {
    expect(sectionIsDirty(fields, { name: 'a', image: 'c' }, { name: 'a', image: 'b' })).toBe(true);
  });

  it('compares by value, not identity — a list that reads the same is not a change', () => {
    const tags = field('tags');
    expect(sectionIsDirty([tags], { tags: ['x', 'y'] }, { tags: ['x', 'y'] })).toBe(false);
    expect(sectionIsDirty([tags], { tags: ['y', 'x'] }, { tags: ['x', 'y'] })).toBe(true);
  });

  it('ignores a field the form does not let anyone write', () => {
    // A difference in a reported field belongs to the deployment, not the
    // reader — offering a Save for it would write a value nobody typed.
    const reported = [field('state', { editable: false })];
    expect(sectionIsDirty(reported, { state: 'RUNNING' }, { state: 'PENDING' })).toBe(false);
  });

  it('is false before either side has arrived', () => {
    expect(sectionIsDirty(fields, null, { name: 'a' })).toBe(false);
    expect(sectionIsDirty(fields, { name: 'a' }, null)).toBe(false);
  });
});

describe('missingRequiredFields', () => {
  const sections: EditSectionSpec[] = [
    { label: 'Identity', fields: [field('name', { required: true })] },
    { label: 'Runtime', fields: [field('image'), field('tags', { required: true })] },
  ];

  it('reaches across every section, because the write does', () => {
    const missing = missingRequiredFields(sections, { name: '', image: 'i', tags: [] });
    expect(missing.map((f) => f.key)).toEqual(['name', 'tags']);
  });

  it('counts whitespace as blank', () => {
    expect(missingRequiredFields(sections, { name: '   ', tags: ['x'] }).map((f) => f.key)).toEqual([
      'name',
    ]);
  });

  it('says nothing about a field that is filled', () => {
    expect(missingRequiredFields(sections, { name: 'n', tags: ['x'] })).toEqual([]);
  });

  it('skips a required field nobody can fill here', () => {
    // Required by the schema and disabled by this form is the deployment's
    // problem to report, not a blank the reader can act on.
    const locked: EditSectionSpec[] = [
      { label: 'x', fields: [field('name', { required: true, disabled: true })] },
    ];
    expect(missingRequiredFields(locked, { name: '' })).toEqual([]);
  });
});

describe('revertFields', () => {
  it('puts back only the fields it was given', () => {
    const stored = { name: 'a', image: 'b' };
    const draft = { name: 'edited', image: 'also-edited' };
    const back = revertFields([field('name')], draft, stored);
    expect(back.name).toBe('a');
    // The other card's pending edit survives a revert that was not its own.
    expect(back.image).toBe('also-edited');
  });
});
