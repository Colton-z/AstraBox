// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { IssuedMcpClientToken, McpClientToken } from '@/types';

const SECRET = 'astrabox_mcp_the-only-copy-there-will-ever-be';

let listed: McpClientToken[] = [];
let issue: () => Promise<IssuedMcpClientToken> = async () => {
  throw new Error('not used in this test');
};

vi.mock('@/api', () => ({
  listMcpClientTokens: async () => listed,
  issueMcpClientToken: async () => issue(),
  revokeMcpClientToken: async () => undefined,
}));

const { default: McpTokensPage } = await import('./McpTokensPage');

afterEach(cleanup);
beforeAll(async () => {
  await i18n.changeLanguage('en');
});
beforeEach(() => {
  listed = [];
  issue = async () => ({
    token_id: 'mtk_1',
    name: 'Cursor on my laptop',
    scope: 'converse',
    secret: SECRET,
    created_at: '2026-08-09T00:00:00+00:00',
    last_used_at: null,
  });
});

function renderPage() {
  return render(
    <MemoryRouter>
      <McpTokensPage />
    </MemoryRouter>,
  );
}

async function issueAKey() {
  fireEvent.change(screen.getByLabelText(/Name/i), {
    target: { value: 'Cursor on my laptop' },
  });
  fireEvent.click(screen.getByRole('button', { name: /Issue key/i }));
  await waitFor(() => expect(screen.getByText(/is ready/i)).toBeTruthy());
}

describe('McpTokensPage', () => {
  it('keeps its heading action in the console-wide action band', () => {
    renderPage();

    // A heading action belongs to the page's heading band, which is one size
    // on every page that has it: the shared Button default, not the 28px
    // card-foot and row size (docs/frontend-design.md §9).
    //
    // Read off the height class, which is the only place the size survives:
    // Button carries the cva class list and no `data-size`, so `h-8` (32px) is
    // what says "default" here, and `h-7` is the 28px size being ruled out.
    const refresh = screen.getByRole('button', { name: /Refresh/i });
    expect(refresh.className).toContain('h-8');
    expect(refresh.className).not.toContain('h-7');
  });

  it('gives the reader a client config that already carries the key', async () => {
    renderPage();
    await issueAKey();

    // The page's whole job: something to paste. The endpoint and the header
    // have to be in it, or the reader still has assembly to do.
    const shown = document.body.textContent || '';
    expect(shown).toContain(SECRET);
    expect(shown).toContain('/api/v1/mcp');
    expect(shown).toContain('mcpServers');
  });

  it('loses the secret when the reader leaves that screen', async () => {
    // The property the design promises, asserted rather than described: the
    // issuing screen is the only place the secret has ever been, and nothing
    // after it can ask for the secret again — the server cannot answer.
    renderPage();
    await issueAKey();
    expect(document.body.textContent || '').toContain(SECRET);

    // Listing the key again is what a reader does next, and it must not bring
    // the secret back.
    listed = [
      {
        token_id: 'mtk_1',
        name: 'Cursor on my laptop',
        scope: 'converse',
        created_at: '2026-08-09T00:00:00+00:00',
        last_used_at: null,
      },
    ];
    fireEvent.click(screen.getByRole('button', { name: /I have copied it/i }));

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Issue key/i })).toBeTruthy(),
    );
    expect(document.body.textContent || '').not.toContain(SECRET);
  });

  it('says a key has never been used rather than leaving the column empty', async () => {
    // "Never" is what makes the column a reason to revoke; blank reads as
    // missing data and tells a reader nothing about the key.
    listed = [
      {
        token_id: 'mtk_2',
        name: 'an old script',
        scope: 'read',
        created_at: '2026-08-01T00:00:00+00:00',
        last_used_at: null,
      },
    ];
    renderPage();

    await waitFor(() => expect(screen.getByText('an old script')).toBeTruthy());
    // Matched across element boundaries: the cell wraps its value, so an
    // exact-node matcher would report the copy missing when it is on screen.
    const rendered = document.body.textContent || '';
    expect(rendered).toContain('Never');
    expect(rendered).toContain('Read only');
  });
});
