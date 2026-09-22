// @vitest-environment jsdom
/**
 * Typing into a valued search select must not be erased.
 *
 * Base UI compares the `value` object by identity when deciding whether the
 * selection changed, and a perceived change rewrites the input text to the
 * selected label. A component that hands Base UI a fresh object per render —
 * from an option list rebuilt on each keystroke or a catalogue fetch landing
 * mid-typing — therefore erases the person's query as they type it.
 * e2e/specs/extension-console-local.spec.ts covers the same behaviour against a
 * live deployment; the cases below pin the mechanism.
 */
import { useState } from 'react';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';

import i18n from '@/i18n';

import { ConsoleSearchSelect } from './ConsoleForm';

afterEach(cleanup);
beforeAll(async () => {
  await i18n.changeLanguage('en');
});

// Reassigned by the harness so a test can land a "catalogue fetch" from
// outside the input interaction, exactly as the page's effect does. The open
// popup marks the rest of the document inert, so a DOM control cannot do it.
let reloadCatalogue: () => void = () => {};

function Harness({ initialOptions }: { initialOptions: string[] }) {
  const [value, setValue] = useState('current-model');
  const [options, setOptions] = useState(initialOptions);
  reloadCatalogue = () => setOptions((prev) => [...prev]);
  return (
    <ConsoleSearchSelect
      value={value}
      onChange={setValue}
      // A fresh array of fresh objects per render, exactly as a page that
      // maps its catalogue state inline hands them over.
      options={options.map((v) => ({ value: v, label: v }))}
      searchPlaceholder="Search models"
      allowCustom
    />
  );
}

describe('ConsoleSearchSelect keeps the typed query', () => {
  it('survives keystrokes while a value is selected', async () => {
    const user = userEvent.setup();
    render(<Harness initialOptions={['current-model', 'deepseek-v4-flash']} />);
    const input = screen.getByPlaceholderText('Search models') as HTMLInputElement;
    await user.click(input);
    // Each keystroke re-renders in between, which is exactly where an
    // identity-churned selection would rewrite the input.
    await user.keyboard('deep');
    expect(input.value).toContain('deep');
  });

  it('survives the catalogue reloading mid-typing', async () => {
    const user = userEvent.setup();
    render(<Harness initialOptions={['current-model', 'deepseek-v4-flash']} />);
    const input = screen.getByPlaceholderText('Search models') as HTMLInputElement;
    await user.click(input);
    await user.keyboard('deep');
    // A fetch landing replaces the options array with equal content — the
    // logical selection is unchanged and the query must survive it.
    act(() => {
      reloadCatalogue();
    });
    expect(input.value).toContain('deep');
  });
});
