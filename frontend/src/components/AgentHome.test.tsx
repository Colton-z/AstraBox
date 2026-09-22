// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AgentConfig } from '@/types';

const listAgents = vi.fn();
vi.mock('@/api', () => ({
  listAgents: () => listAgents(),
  startAgentConversation: vi.fn(),
}));

import { AgentHome } from './AgentHome';

afterEach(() => {
  cleanup();
  listAgents.mockReset();
});

function agent(over: Partial<AgentConfig>): AgentConfig {
  return { agent_id: 'a', name: 'Agent', model: 'm', state: 'ACTIVE', ...over } as AgentConfig;
}

/**
 * The page exists so somebody can choose. That makes "the cards say something
 * different from each other" the property worth pinning: a single constant
 * repeated on every card is exactly as useful as no copy at all
 * (docs/frontend-design.md §2).
 */
describe('AgentHome cards', () => {
  it('shows each agent its own description, so two cards can be told apart', async () => {
    listAgents.mockResolvedValue([
      agent({ agent_id: 'a1', name: 'Data Analyst', description: 'Inspects CSV and plots it.' }),
      agent({ agent_id: 'a2', name: 'Claude Code', description: 'Works in a repo checkout.' }),
    ]);
    render(<AgentHome onConversationCreated={() => {}} />);

    expect(await screen.findByText('Inspects CSV and plots it.')).toBeTruthy();
    expect(screen.getByText('Works in a repo checkout.')).toBeTruthy();
  });

  it('names the deployment default for an agent that pins no model', async () => {
    listAgents.mockResolvedValue([
      agent({ agent_id: 'a1', name: 'Pinned', model: 'claude-opus-5' }),
      agent({ agent_id: 'a2', name: 'Unpinned', model: '' }),
    ]);
    render(<AgentHome onConversationCreated={() => {}} />);

    expect(await screen.findByText('claude-opus-5')).toBeTruthy();
    // Not a dash: an empty model resolves to the deployment's configured one
    // at turn time, so the card names that — once, on the card that lacks it.
    // Assert the rendered sentence rather than the key so the test also proves
    // that the catalogue entry resolves through the i18n runtime.
    expect(screen.getAllByText('Deployment default')).toHaveLength(1);
  });

  it('says nothing about an agent that set no description', async () => {
    listAgents.mockResolvedValue([
      agent({ agent_id: 'a1', name: 'Described', description: 'Its own line.' }),
      agent({ agent_id: 'a2', name: 'Bare', description: '   ' }),
    ]);
    render(<AgentHome onConversationCreated={() => {}} />);

    expect(await screen.findByText('Its own line.')).toBeTruthy();
    // A card must not repeat its own button. The fallback sentence for a
    // description-less agent is "Start a new conversation with this Agent."
    // directly above a button reading "Start conversation" — the same words
    // twice, telling a reader choosing between cards nothing
    // (docs/frontend-design.md §2). Silence is the honest answer.
    const cards = screen.getAllByTestId('agent-option');
    const bare = cards.find((c) => c.getAttribute('data-agent-name') === 'Bare');
    expect(bare).toBeTruthy();
    const buttonLabel = bare!.querySelector('button')?.textContent?.trim() ?? '';
    expect(buttonLabel).not.toEqual('');
    expect(bare!.textContent?.split(buttonLabel).length).toBe(2);
  });
});
