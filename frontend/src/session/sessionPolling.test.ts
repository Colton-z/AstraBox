import { describe, expect, it } from 'vitest';

import { isSessionSteadyState, shouldPollBackgroundSubagentHistory, shouldPollSessionDetail } from './sessionPolling';

const idleAgentChat = {
  state: 'READY',
  session_kind: 'agent_chat',
  agent_id: 'agent-1',
  current_turn_id: null,
  pending_interaction: null,
  agent_runtime: { state: 'ACTIVE', sandbox_id: '', runtime_unavailable: false },
};

describe('isSessionSteadyState', () => {
  it('rests on a conversation-tenancy box recorded on the session', () => {
    expect(isSessionSteadyState({ ...idleAgentChat, sandbox_id: 'sb-1' })).toBe(true);
    expect(shouldPollSessionDetail('ready', { ...idleAgentChat, sandbox_id: 'sb-1' })).toBe(false);
  });

  it('rests on an agent-tenancy box recorded on the Agent', () => {
    expect(isSessionSteadyState({
      ...idleAgentChat,
      sandbox_id: '',
      agent_runtime: { state: 'ACTIVE', sandbox_id: 'sb-shared', runtime_unavailable: false },
    })).toBe(true);
  });

  it('keeps asking while no box is attached, a turn runs, or an interaction waits', () => {
    expect(isSessionSteadyState({ ...idleAgentChat, sandbox_id: '' })).toBe(false);
    expect(isSessionSteadyState({ ...idleAgentChat, sandbox_id: 'sb-1', current_turn_id: 'turn-1' })).toBe(false);
    expect(isSessionSteadyState({ ...idleAgentChat, sandbox_id: 'sb-1', pending_interaction: { id: 'ask' } })).toBe(false);
    expect(shouldPollSessionDetail('ready', { ...idleAgentChat, sandbox_id: '' })).toBe(true);
  });
});

describe('shouldPollBackgroundSubagentHistory', () => {
  it('keeps rehydrating after the parent turn ends while a child is still live', () => {
    expect(shouldPollBackgroundSubagentHistory({
      lifecycleState: 'ready',
      liveSubagentCount: 1,
      isSubmitted: false,
      isStreaming: false,
      hasPendingInteraction: false,
    })).toBe(true);
  });

  it('does not start a second history poll while the foreground stream owns updates', () => {
    expect(shouldPollBackgroundSubagentHistory({
      lifecycleState: 'ready',
      liveSubagentCount: 1,
      isSubmitted: false,
      isStreaming: true,
      hasPendingInteraction: false,
    })).toBe(false);
  });
});
