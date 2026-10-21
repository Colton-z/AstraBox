// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { AdminAgentCategory, AgentCategory, DeletedAgentCategory } from '@/types';

// Plain functions rather than spies, so a refusal reaches the page as the
// rejection the page handles instead of one a spy reports as unhandled.
let listed: AdminAgentCategory[] = [];
let create: (name: string) => Promise<AgentCategory> = async (name) => ({
  category_id: 'cat_new',
  name,
  position: listed.length,
});
const orders: string[][] = [];
const deletions: string[] = [];

vi.mock('@/api', async () => ({
  // The page tells a refusal apart by its code, read off the real class.
  ApiError: (await vi.importActual<typeof import('@/api')>('@/api')).ApiError,
  listAdminAgentCategories: async () => listed,
  createAdminAgentCategory: (name: string) => create(name),
  renameAdminAgentCategory: async (categoryId: string, name: string): Promise<AgentCategory> => ({
    category_id: categoryId,
    name,
    position: 0,
  }),
  reorderAdminAgentCategories: async (categoryIds: string[]): Promise<AgentCategory[]> => {
    orders.push(categoryIds);
    return categoryIds.map((categoryId, position) => ({
      category_id: categoryId,
      name: listed.find((row) => row.category_id === categoryId)?.name ?? categoryId,
      position,
    }));
  },
  deleteAdminAgentCategory: async (categoryId: string): Promise<DeletedAgentCategory> => {
    deletions.push(categoryId);
    return {
      category_id: categoryId,
      unbound_agents: listed.find((row) => row.category_id === categoryId)?.agents ?? 0,
    };
  },
}));

const { ApiError } = await import('@/api');
const { default: AgentCategoriesPage } = await import('./AgentCategoriesPage');

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
beforeEach(() => {
  // `GET /api/v1/admin/agent-categories`: the administrator's order, with
  // how many Agents each category holds.
  listed = [
    { category_id: 'cat_writing', name: 'Writing', position: 0, agents: 2 },
    { category_id: 'cat_data', name: 'Data', position: 1, agents: 3 },
    { category_id: 'cat_legal', name: 'Legal', position: 2, agents: 0 },
  ];
  create = async (name) => ({ category_id: 'cat_new', name, position: listed.length });
  orders.length = 0;
  deletions.length = 0;
});
afterEach(cleanup);

function renderPage() {
  return render(
    <MemoryRouter>
      <AgentCategoriesPage />
    </MemoryRouter>,
  );
}

const rowNames = () =>
  screen
    .getAllByTestId('agent-category-row')
    .map((row) => row.querySelector('[data-slot="item-title"]')?.textContent);

describe('the Agent categories page', () => {
  it('sends every category, in the new order, when one moves', async () => {
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Move Legal up' }));

    // The server refuses a partial list, so the write names all three.
    await waitFor(() => expect(orders).toEqual([['cat_writing', 'cat_legal', 'cat_data']]));
    expect(rowNames()).toEqual(['Writing', 'Legal', 'Data']);
  });

  it('says how many Agents will lose a category before deleting it', async () => {
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Delete Data' }));
    const dialog = await screen.findByRole('alertdialog');
    expect(dialog.textContent).toContain('3 Agents will lose this category.');
    expect(deletions).toEqual([]);

    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete category' }));
    await waitFor(() => expect(deletions).toEqual(['cat_data']));
    await waitFor(() => expect(rowNames()).toEqual(['Writing', 'Legal']));
  });

  it('says in place that a new name is taken', async () => {
    create = async () => {
      throw new ApiError(
        'AGENT_CATEGORY_NAME_TAKEN',
        "an Agent category named 'data' already exists",
        409,
      );
    };
    renderPage();

    const field = await screen.findByLabelText('Name');
    fireEvent.change(field, { target: { value: 'data' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add category' }));

    expect(await screen.findByText('A category named “data” already exists.')).toBeTruthy();
    expect(field.getAttribute('aria-invalid')).toBe('true');
    expect(rowNames()).toEqual(['Writing', 'Data', 'Legal']);
  });
});
