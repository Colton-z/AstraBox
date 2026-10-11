// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { ExtensionProviderView } from '@/types';
import { CatalogExtensionField } from './CatalogExtensionField';

beforeAll(async () => { await i18n.changeLanguage('en'); });
afterEach(cleanup);

function show(provider: ExtensionProviderView) {
  return render(
    <CatalogExtensionField
      kind="mcp"
      id="agent-extension-mcp"
      provider={provider}
      catalog={{ mcp_servers: [{ id: 'srv-1', name: 'search' }], skills: [] }}
      value={[]}
      onChange={() => {}}
      loading={false}
      disabled={false}
      // The Agent page always asks for the console; whether it renders is the
      // provider's declaration.
      manage={{ opening: false, busy: false, onOpen: vi.fn() }}
    >
      <div>own MCP servers</div>
    </CatalogExtensionField>,
  );
}

describe('a catalog field says what the selected provider declares', () => {
  it('names the catalog and its console after the provider', () => {
    show({ name: 'litellm', display_name: 'LiteLLM', management_console: true });

    expect(screen.getByText('MCP servers from LiteLLM')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Manage MCP servers in LiteLLM' })).toBeTruthy();
  });

  it('offers no console a provider does not declare, and no other provider\'s name', () => {
    show({ name: 'builtin', display_name: 'AstraBox', management_console: false });

    expect(screen.getByText('MCP servers from AstraBox')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /^Manage / })).toBeNull();
    expect(screen.queryByText(/LiteLLM/)).toBeNull();
  });
});
