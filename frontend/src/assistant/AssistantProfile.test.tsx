// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { SWRConfig } from 'swr';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { getExtensionProvider, listAssistantConnectors, listConnectors } from '@/api';
import { getAssistant, listAssistantScheduledJobs } from './api';
import type { AssistantRecord } from './types';
import { AssistantProfile } from './AssistantProfile';

vi.mock('@/api', () => ({
  getExtensionProvider: vi.fn(),
  listConnectors: vi.fn(),
  listAssistantConnectors: vi.fn(),
  setAssistantConnector: vi.fn(),
}));
vi.mock('./api', () => ({
  getAssistant: vi.fn(),
  listAssistantScheduledJobs: vi.fn(),
  updateAssistant: vi.fn(),
  deleteAssistant: vi.fn(),
}));

const OWN_SKILL = 'https://git.example.test/team/skills.git#own-skill';
const CATALOG_SKILL = 'https://git.example.test/catalog/skills.git#catalog-skill';

/** An Assistant whose defaults write one Skill and select a Skill and two MCP servers from a catalog. */
const assistant: AssistantRecord = {
  assistant_id: 'as-1',
  owner_id: 'owner',
  display_name: 'Mine',
  icon: null,
  description: null,
  engine_kind: 'hermes',
  environment_name: 'assistants',
  permission_mode_default: '',
  inherited: {
    instructions: false,
    skills: [OWN_SKILL],
    catalog_skills: [CATALOG_SKILL],
    mcp_servers: [],
    catalog_mcp_servers: 2,
    packages: [],
    credential_vaults: 0,
  },
};

beforeAll(async () => { await i18n.changeLanguage('en'); });
beforeEach(() => {
  vi.mocked(getAssistant).mockReset().mockResolvedValue(assistant);
  vi.mocked(listAssistantScheduledJobs).mockReset().mockResolvedValue({
    assistant_id: 'as-1', workspace_state: 'NOT_MATERIALIZED', jobs: null, reason: 'workspace_not_running',
  });
  vi.mocked(listConnectors).mockReset().mockResolvedValue([]);
  vi.mocked(listAssistantConnectors).mockReset().mockResolvedValue([]);
});
afterEach(cleanup);

async function openProfile() {
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <MemoryRouter>
        <AssistantProfile assistantId="as-1" />
      </MemoryRouter>
    </SWRConfig>,
  );
  fireEvent.click(screen.getByTestId('assistant-profile-open'));
  await screen.findByText(OWN_SKILL);
}

describe('what an Assistant inherits from an extension catalog', () => {
  it('is reported unavailable, not provided, when the deployment runs no catalog', async () => {
    vi.mocked(getExtensionProvider).mockResolvedValue(null);
    await openProfile();

    expect(
      await screen.findByText('3 selections from an extension catalog are unavailable: this deployment runs no catalog.'),
    ).toBeTruthy();
    expect(screen.queryByText(CATALOG_SKILL)).toBeNull();
    expect(screen.queryByText(/tool servers? from the catalog/)).toBeNull();
  });

  it('is listed as provided while the deployment runs one', async () => {
    vi.mocked(getExtensionProvider).mockResolvedValue({
      name: 'litellm', display_name: 'LiteLLM', management_console: true,
    });
    await openProfile();

    expect(await screen.findByText('Plus 2 tool servers from the catalog.')).toBeTruthy();
    expect(screen.getByText(CATALOG_SKILL)).toBeTruthy();
    expect(screen.queryByTestId('assistant-profile-catalog-unavailable')).toBeNull();
  });
});
