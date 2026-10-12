// @vitest-environment jsdom
/**
 * The Agent form's categories and tags: the two fields the home page finds an
 * Agent by. The form is built the way both Agent pages build it — the schema
 * the server serves, through `buildAgentEditSections`, rendered by
 * `ConsoleEditSections` — so what is asserted is what an author sees and what
 * the save would send.
 */
import { useState } from 'react';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';

import i18n from '@/i18n';
import type { AgentCategory, FormSchema } from '@/types';

import { ConsoleEditSections, splitAdvanced } from './console';
import { buildAgentEditSections } from './agentEditConfig';

// The identity group of `GET /api/v1/agent-configuration/schema`, field for
// field as agent_schema.py declares it.
const SCHEMA: FormSchema = {
  version: 1,
  groups: [{ id: 'identity' }],
  fields: [
    { key: 'name', type: 'string', group: 'identity', required: true },
    { key: 'display_name', type: 'string', group: 'identity', path: 'display_meta.display_name' },
    { key: 'description', type: 'text', group: 'identity' },
    { key: 'icon', type: 'string', advanced: true, group: 'identity', path: 'display_meta.icon' },
    { key: 'tags', type: 'string_list', advanced: true, group: 'identity', path: 'display_meta.tags' },
    { key: 'use_cases', type: 'string_list', advanced: true, group: 'identity' },
    { key: 'categories', type: 'string_list', group: 'identity' },
  ],
};

// `GET /api/v1/agent-categories`.
const CATEGORIES: AgentCategory[] = [
  { category_id: 'cat_writing', name: 'Writing', position: 0 },
  { category_id: 'cat_data', name: 'Data', position: 1 },
];

const OPTS = {
  nameEditable: true,
  categories: CATEGORIES,
  // `GET /api/v1/agent-tags`, as the pages pass it on.
  tagSuggestions: ['pdf', 'research'],
};

/** The last draft the form reported: what a save would send. */
let written: Record<string, unknown> | null = null;

function Form({ initial }: { initial: Record<string, unknown> }) {
  const [draft, setDraft] = useState(initial);
  const identity = buildAgentEditSections(SCHEMA, draft, OPTS).sections.filter((section) =>
    section.fields.some((field) => field.key === 'tags'),
  );
  return (
    <ConsoleEditSections
      sections={identity}
      draft={draft}
      onDraftChange={(next) => {
        written = next;
        setDraft(next);
      }}
      idPrefix="agent"
      invalidKeys={new Set()}
      onInvalidChange={() => {}}
    />
  );
}

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
beforeEach(() => {
  written = null;
});
afterEach(cleanup);

describe('the Agent form', () => {
  it('shows tags beside categories, outside the advanced fields a create page folds', () => {
    const { sections, advancedKeys } = buildAgentEditSections(SCHEMA, {}, OPTS);
    const { essential } = splitAdvanced(sections, advancedKeys);
    expect(essential[0].fields.map((field) => field.key)).toEqual([
      'name',
      'display_name',
      'description',
      'categories',
      'tags',
    ]);
  });

  it('shows the form a tag is stored in before adding it, and writes that form', async () => {
    const user = userEvent.setup();
    render(<Form initial={{ name: 'researcher' }} />);

    const tags = screen.getByRole('combobox', { name: 'Tags' }) as HTMLInputElement;
    await user.type(tags, '  #Code Review');
    // The stored form, before it is added: no `#`, lower case, one word.
    expect(screen.getByText('Press Enter to add #code-review')).toBeTruthy();
    expect(written).toBeNull();

    await user.keyboard('{Enter}');
    expect(screen.getByText('#code-review')).toBeTruthy();
    expect(tags.value).toBe('');
    expect(written?.display_meta).toEqual({ tags: ['code-review'] });
  });

  it('refuses a sixth tag in place and keeps what was typed', async () => {
    const user = userEvent.setup();
    render(<Form initial={{ name: 'researcher', display_meta: { tags: ['a', 'b', 'c', 'd', 'e'] } }} />);

    const tags = screen.getByRole('combobox', { name: 'Tags' }) as HTMLInputElement;
    await user.type(tags, 'extra{Enter}');

    expect(screen.getByRole('alert').textContent).toBe('Up to 5 tags. Remove one to add another.');
    expect(tags.value).toBe('extra');
    // Nothing reached the draft, so a save would not send a sixth tag.
    expect(written).toBeNull();
  });

  it('shows a stored category that no longer exists as unavailable, and lets it be removed', async () => {
    const user = userEvent.setup();
    render(<Form initial={{ name: 'researcher', categories: ['cat_data', 'cat_gone'] }} />);

    expect(screen.getByText('Data')).toBeTruthy();
    const gone = screen.getByText('Unavailable category');
    await user.click(within(gone).getByRole('button'));

    expect(screen.queryByText('Unavailable category')).toBeNull();
    expect(written?.categories).toEqual(['cat_data']);
  });
});
