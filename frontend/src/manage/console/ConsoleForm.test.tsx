// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';

import i18n from '@/i18n';
import { ConsoleFieldRow, ConsoleSearchSelect } from './ConsoleForm';

afterEach(cleanup);
beforeAll(async () => {
  await i18n.changeLanguage('en');
});

describe('ConsoleFieldRow naming', () => {
  it('names a required searchable field by its label alone, with the marker beside it', () => {
    render(
      <ConsoleFieldRow label="Model" htmlFor="model" required idTag="model">
        <ConsoleSearchSelect
          id="model"
          value=""
          onChange={() => undefined}
          options={[{ value: 'deepseek-v4-flash', label: 'deepseek-v4-flash' }]}
          allowCustom
        />
      </ConsoleFieldRow>,
    );
    // The label element is what `aria-labelledby` names a control with, and
    // that name takes the element whole. Only the label text may live in it.
    const label = document.querySelector('label[for="model"]');
    expect(label?.textContent).toBe('Model');
    expect(screen.getByText('*').getAttribute('aria-hidden')).toBe('true');
    expect(screen.getByRole('combobox', { name: 'Model' })).toBeTruthy();
  });
});
