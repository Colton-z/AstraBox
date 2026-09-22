// @vitest-environment jsdom
/**
 * A form whose fields are the controls, saved a section at a time.
 *
 * This is what docs/frontend-design.md §4 asks for and what every record page
 * composes: `ConsoleEditSections` renders the fields, `editDraft` answers "has
 * this section changed" and "can it be written yet", and the page wires a save
 * per card. Three pieces rather than one component that owns all three, so the
 * cover belongs to the composition rather than to any one of them.
 *
 * The harness stands in for a record page and wires the three pieces the same
 * way a record page wires them.
 */
import { useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';

import { ConsoleEditSections } from './ConsoleEditSections';
import { missingRequiredFields, revertFields, sectionIsDirty } from './editDraft';
import type { EditFieldSpec, EditSectionSpec } from './editFields';

afterEach(cleanup);
beforeAll(async () => {
  await i18n.changeLanguage('en');
});

const text = (key: string, extra: Partial<EditFieldSpec> = {}): EditFieldSpec => ({
  key,
  label: key,
  type: 'text',
  get: (d) => d[key],
  set: (d, v) => ({ ...d, [key]: v }),
  ...extra,
});

const TWO_SECTIONS: EditSectionSpec[] = [
  { label: 'Identity', fields: [text('name')] },
  { label: 'Runtime', fields: [text('image')] },
];
const STORED = { name: 'claude-code', image: 'astrabox/sandbox:1' };

/** A record page, reduced to the parts these assertions are about. */
function Harness({
  sections = TWO_SECTIONS,
  baseline = STORED as Record<string, unknown>,
  onSaveSection = vi.fn(),
}: {
  sections?: EditSectionSpec[];
  baseline?: Record<string, unknown>;
  onSaveSection?: (fields: EditFieldSpec[], index: number) => void;
}) {
  const [draft, setDraft] = useState<Record<string, unknown>>({ ...baseline });
  const [invalidKeys, setInvalidKeys] = useState<Set<string>>(new Set());
  const missing = missingRequiredFields(sections, draft);
  return (
    <ConsoleEditSections
      sections={sections}
      draft={draft}
      onDraftChange={setDraft}
      idPrefix="t"
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
        dirty: sectionIsDirty(section.fields, draft, baseline),
        blocked: section.fields.some((f) => invalidKeys.has(f.key)) || missing.length > 0,
        blockedReason: missing.length
          ? i18n.t('manage:console.blocked_by_required', { field: String(missing[0].label) })
          : undefined,
        saveLabel: i18n.t('common:save'),
        revertLabel: i18n.t('common:revert'),
        onSave: () => onSaveSection(section.fields, index),
        onRevert: () => setDraft(revertFields(section.fields, draft, baseline)),
      })}
    />
  );
}

const saves = () => screen.queryAllByRole('button', { name: i18n.t('common:save') });

describe('a form with no view mode', () => {
  it('offers no Edit button and no Save until something differs from what is stored', () => {
    render(<Harness />);
    // §4: the fields are the controls, so there is nothing to switch into.
    expect(screen.queryByRole('button', { name: i18n.t('common:edit') })).toBeNull();
    expect(screen.getByDisplayValue('claude-code')).toBeTruthy();
    expect(saves()).toHaveLength(0);
  });

  it('gives the save to the edited section only, and reports that section', () => {
    const onSaveSection = vi.fn();
    render(<Harness onSaveSection={onSaveSection} />);

    fireEvent.change(screen.getByDisplayValue('astrabox/sandbox:1'), {
      target: { value: 'astrabox/sandbox:2' },
    });

    // Exactly one Save — the Runtime section's. Identity is untouched, so it
    // has nothing to write and offers nothing.
    expect(saves()).toHaveLength(1);

    fireEvent.click(saves()[0]);
    expect(onSaveSection).toHaveBeenCalledTimes(1);
    expect(onSaveSection.mock.calls[0][1]).toBe(1);
    // It is handed its own fields, so the page writes that section and no other.
    expect(onSaveSection.mock.calls[0][0].map((f: EditFieldSpec) => f.key)).toEqual(['image']);
  });

  it('reverts only the section it belongs to', () => {
    render(<Harness />);

    fireEvent.change(screen.getByDisplayValue('claude-code'), { target: { value: 'renamed' } });
    fireEvent.change(screen.getByDisplayValue('astrabox/sandbox:1'), {
      target: { value: 'astrabox/sandbox:2' },
    });
    expect(saves()).toHaveLength(2);

    const reverts = screen.getAllByRole('button', { name: i18n.t('common:revert') });
    fireEvent.click(reverts[0]);

    // Identity is back to stored; Runtime keeps the edit that was pending in it.
    expect(screen.getByDisplayValue('claude-code')).toBeTruthy();
    expect(screen.getByDisplayValue('astrabox/sandbox:2')).toBeTruthy();
    expect(saves()).toHaveLength(1);
  });
});

describe('a record that cannot be written yet', () => {
  const REQUIRED: EditSectionSpec[] = [
    { label: 'Identity', fields: [text('name', { required: true })] },
    { label: 'Runtime', fields: [text('image')] },
  ];

  it('says nothing about an untouched record — a blank required field is not yet a complaint', () => {
    render(<Harness sections={REQUIRED} baseline={{ name: 'env-1', image: 'i:1' }} />);
    expect(screen.queryByText(/required/i)).toBeNull();
  });

  it('blocks EVERY section while a required field is blank, and names the field', () => {
    render(<Harness sections={REQUIRED} baseline={{ name: 'env-1', image: 'i:1' }} />);

    fireEvent.change(screen.getByDisplayValue('env-1'), { target: { value: '' } });
    // Not only Identity's: the server validates the whole payload, so a Runtime
    // save that went through would come back as a 400 about a field on another
    // card.
    const blocked = screen.getAllByText(
      i18n.t('manage:console.blocked_by_required', { field: 'name' }),
    );
    expect(blocked.length).toBeGreaterThan(0);
    for (const button of saves()) expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it('releases every section once the required field is filled again', () => {
    render(<Harness sections={REQUIRED} baseline={{ name: 'env-1', image: 'i:1' }} />);

    fireEvent.change(screen.getByDisplayValue('env-1'), { target: { value: '' } });
    fireEvent.change(screen.getByDisplayValue(''), { target: { value: 'env-2' } });

    expect(
      screen.queryByText(i18n.t('manage:console.blocked_by_required', { field: 'name' })),
    ).toBeNull();
    expect(saves().every((b) => !(b as HTMLButtonElement).disabled)).toBe(true);
  });
});

describe('editing keeps the caret where the reader put it', () => {
  it('keeps focus in a field across keystrokes', () => {
    render(<Harness />);
    const field = screen.getByDisplayValue('claude-code') as HTMLInputElement;
    field.focus();
    expect(document.activeElement).toBe(field);

    // Each keystroke re-renders the whole section — a control rebuilt rather
    // than updated would take the caret with it, and the reader would be typing
    // into nothing after the first character.
    fireEvent.change(field, { target: { value: 'claude-code-x' } });
    expect(document.activeElement).toBe(screen.getByDisplayValue('claude-code-x'));
  });
});
