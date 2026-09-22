import { describe, it, expect } from 'vitest';
import { Chat } from '@ai-sdk/react';
import type { ChatTransport, UIMessage, UIMessageChunk } from 'ai';

import type { PendingInteraction } from '../types';
import {
  ensureAssistantMessageForTurn,
  ensureAssistantStreamSlot,
  isTurnSettledInSessionSnapshot,
  isTransportIdleStatus,
  getTransportIdleTerminalTurnId,
  hasAssistantMessageForTurn,
  withoutAssistantStreamSlots,
  shouldAdoptAuthoritativeMessagesForLocalPendingInteraction,
} from './messageSettlement';

type TestMessage = UIMessage;

function assistantMessage(id: string, turnId?: string): TestMessage {
  return {
    id,
    role: 'assistant',
    parts: [],
    ...(turnId ? { metadata: { turn_id: turnId } } : {}),
  };
}

function userMessage(id: string, turnId?: string): TestMessage {
  return {
    id,
    role: 'user',
    parts: [],
    ...(turnId ? { metadata: { turn_id: turnId } } : {}),
  };
}

function toolPermissionInteraction(overrides: Partial<PendingInteraction> = {}): PendingInteraction {
  return {
    interaction_id: 'int-1',
    turn_id: 'turn-1',
    tool_name: 'Bash',
    presentation: 'tool_approval',
    ...overrides,
  } as PendingInteraction;
}

describe('isTurnSettledInSessionSnapshot', () => {
  it('is false when there is no session or no turnId', () => {
    expect(isTurnSettledInSessionSnapshot(null, 'turn-1')).toBe(false);
    expect(isTurnSettledInSessionSnapshot({}, null)).toBe(false);
    expect(isTurnSettledInSessionSnapshot({}, '')).toBe(false);
    expect(isTurnSettledInSessionSnapshot({}, '   ')).toBe(false);
  });

  it('is false while the turn is still the session current_turn_id', () => {
    const session = { current_turn_id: 'turn-1', last_turn_id: 'turn-1', last_turn_status: 'COMPLETED' };
    expect(isTurnSettledInSessionSnapshot(session, 'turn-1')).toBe(false);
  });

  it('is false while the turn still has a live pending_interaction attached', () => {
    const session = {
      last_turn_id: 'turn-1',
      last_turn_status: 'COMPLETED',
      pending_interaction: { interaction_id: 'int-1', turn_id: 'turn-1' },
    };
    expect(isTurnSettledInSessionSnapshot(session, 'turn-1')).toBe(false);
  });

  it('is true once last_turn_id matches and last_turn_status is terminal', () => {
    for (const status of ['COMPLETED', 'FAILED', 'INTERRUPTED']) {
      const session = { last_turn_id: 'turn-1', last_turn_status: status };
      expect(isTurnSettledInSessionSnapshot(session, 'turn-1')).toBe(true);
    }
  });

  it('is false when last_turn_id matches but the status is not yet terminal', () => {
    const session = { last_turn_id: 'turn-1', last_turn_status: 'RUNNING' };
    expect(isTurnSettledInSessionSnapshot(session, 'turn-1')).toBe(false);
    expect(isTurnSettledInSessionSnapshot({ last_turn_id: 'turn-1' }, 'turn-1')).toBe(false);
  });

  it('is false when last_turn_id refers to a different turn', () => {
    const session = { last_turn_id: 'turn-0', last_turn_status: 'COMPLETED' };
    expect(isTurnSettledInSessionSnapshot(session, 'turn-1')).toBe(false);
  });
});

describe('isTransportIdleStatus', () => {
  it('treats submitted/streaming as busy', () => {
    expect(isTransportIdleStatus('submitted')).toBe(false);
    expect(isTransportIdleStatus('streaming')).toBe(false);
  });

  it('treats everything else (including undefined) as idle', () => {
    expect(isTransportIdleStatus('ready')).toBe(true);
    expect(isTransportIdleStatus('error')).toBe(true);
    expect(isTransportIdleStatus(undefined)).toBe(true);
    expect(isTransportIdleStatus(null)).toBe(true);
    expect(isTransportIdleStatus('')).toBe(true);
  });
});

describe('getTransportIdleTerminalTurnId', () => {
  const settledSession = { last_turn_id: 'turn-1', last_turn_status: 'COMPLETED' };

  it('is null when there is no local turn id', () => {
    expect(getTransportIdleTerminalTurnId(settledSession, 'ready', null)).toBeNull();
    expect(getTransportIdleTerminalTurnId(settledSession, 'ready', '')).toBeNull();
  });

  it('is null while the transport is still submitted/streaming, even if the session already settled', () => {
    expect(getTransportIdleTerminalTurnId(settledSession, 'submitted', 'turn-1')).toBeNull();
    expect(getTransportIdleTerminalTurnId(settledSession, 'streaming', 'turn-1')).toBeNull();
  });

  it('is null when the session reports the turn was never received by the backend', () => {
    const session = { last_turn_id: 'turn-1', last_turn_status: 'COMPLETED', delivery_state: 'NOT_RECEIVED' as const };
    expect(getTransportIdleTerminalTurnId(session, 'ready', 'turn-1')).toBeNull();
  });

  it('is null when the last turn failed before dispatch even if last_turn_id matches', () => {
    const session = {
      last_turn_id: 'turn-1',
      last_turn_status: 'COMPLETED',
      last_turn_failure_phase: 'pre_dispatch' as const,
    };
    expect(getTransportIdleTerminalTurnId(session, 'ready', 'turn-1')).toBeNull();
  });

  it('returns the turn id once the transport is idle and the snapshot confirms settlement', () => {
    expect(getTransportIdleTerminalTurnId(settledSession, 'ready', 'turn-1')).toBe('turn-1');
    expect(getTransportIdleTerminalTurnId(settledSession, undefined, 'turn-1')).toBe('turn-1');
  });

  it('is null when idle but the snapshot has not caught up to this turn yet', () => {
    const session = { last_turn_id: 'turn-0', last_turn_status: 'COMPLETED' };
    expect(getTransportIdleTerminalTurnId(session, 'ready', 'turn-1')).toBeNull();
  });
});

describe('hasAssistantMessageForTurn', () => {
  it('is false for an empty/missing turn id', () => {
    expect(hasAssistantMessageForTurn([assistantMessage('m1', 'turn-1')], null)).toBe(false);
    expect(hasAssistantMessageForTurn([assistantMessage('m1', 'turn-1')], '')).toBe(false);
  });

  it('matches the explicit AI SDK message metadata', () => {
    const messages = [userMessage('u1', 'turn-1'), assistantMessage('a1', 'turn-1')];
    expect(hasAssistantMessageForTurn(messages, 'turn-1')).toBe(true);
  });

  it('does not reinterpret an engine message id as a platform turn id', () => {
    const messages: TestMessage[] = [{ id: 'turn-1', role: 'assistant', parts: [] }];
    expect(hasAssistantMessageForTurn(messages, 'turn-1')).toBe(false);
  });

  it('ignores a matching user message — only an assistant message settles the turn', () => {
    const messages = [userMessage('u1', 'turn-1')];
    expect(hasAssistantMessageForTurn(messages, 'turn-1')).toBe(false);
  });

  it('is false when no message matches the turn', () => {
    const messages = [assistantMessage('a1', 'turn-0')];
    expect(hasAssistantMessageForTurn(messages, 'turn-1')).toBe(false);
  });
});

describe('assistant stream isolation', () => {
  const previousAssistant: TestMessage = {
    id: 'assistant-1',
    role: 'assistant',
    metadata: { turn_id: 'turn-1' },
    parts: [{ type: 'text', text: 'first reply' }],
  };

  it('seeds one assistant container for a known turn without changing prior output', () => {
    const seeded = ensureAssistantMessageForTurn([previousAssistant], 'turn-2');

    expect(seeded).toEqual([
      previousAssistant,
      {
        id: 'turn-2',
        role: 'assistant',
        metadata: { turn_id: 'turn-2' },
        parts: [],
      },
    ]);
    expect(seeded[0]).toBe(previousAssistant);
    expect(ensureAssistantMessageForTurn(seeded, 'turn-2')).toBe(seeded);
  });

  it('gives an idle session subscription an invisible fresh assistant slot', () => {
    const seeded = ensureAssistantStreamSlot([previousAssistant], 'session-1');

    expect(seeded).toHaveLength(2);
    expect(seeded[0]).toBe(previousAssistant);
    expect(seeded[1]).toMatchObject({ role: 'assistant', parts: [] });
    expect(seeded[1].id).not.toBe(previousAssistant.id);
    expect(ensureAssistantStreamSlot(seeded, 'session-1')).toBe(seeded);
    expect(withoutAssistantStreamSlots(seeded, 'session-1')).toEqual([previousAssistant]);
  });

  it('keeps the previous reply intact when AI SDK attaches a new turn to an idle stream', async () => {
    const chunks: UIMessageChunk[] = [
      {
        type: 'start',
        messageId: 'turn-2',
        messageMetadata: { turn_id: 'turn-2' },
      },
      { type: 'text-start', id: 'text-2' },
      { type: 'text-delta', id: 'text-2', delta: 'second reply' },
      { type: 'text-end', id: 'text-2' },
      { type: 'finish', finishReason: 'stop' },
    ];
    const transport: ChatTransport<TestMessage> = {
      sendMessages: async () => { throw new Error('not used'); },
      reconnectToStream: async () => new ReadableStream<UIMessageChunk>({
        start(controller) {
          chunks.forEach((chunk) => controller.enqueue(chunk));
          controller.close();
        },
      }),
    };
    const chat = new Chat<TestMessage>({
      id: 'session-1',
      messages: ensureAssistantStreamSlot([previousAssistant], 'session-1'),
      transport,
    });

    await chat.resumeStream();

    expect(withoutAssistantStreamSlots(chat.messages, 'session-1')).toEqual([
      previousAssistant,
      {
        id: 'turn-2',
        role: 'assistant',
        metadata: { turn_id: 'turn-2' },
        parts: [{ type: 'text', text: 'second reply', state: 'done' }],
      },
    ]);
  });
});

describe('shouldAdoptAuthoritativeMessagesForLocalPendingInteraction', () => {
  const readySession = { state: 'READY' };

  it('is false when there is no local pending interaction', () => {
    expect(
      shouldAdoptAuthoritativeMessagesForLocalPendingInteraction(readySession, null, null, false),
    ).toBe(false);
  });

  it('is false whenever the overlay already carries the truth', () => {
    const local = toolPermissionInteraction({ interaction_id: 'local-1' });
    expect(
      shouldAdoptAuthoritativeMessagesForLocalPendingInteraction(readySession, local, null, true),
    ).toBe(false);
  });

  it('is false unless the session state is READY', () => {
    const local = toolPermissionInteraction({ interaction_id: 'local-1' });
    expect(
      shouldAdoptAuthoritativeMessagesForLocalPendingInteraction({ state: 'BUSY' }, local, null, false),
    ).toBe(false);
  });

  it('is false while the session still has a current_turn_id in flight', () => {
    const local = toolPermissionInteraction({ interaction_id: 'local-1' });
    const session = { state: 'READY', current_turn_id: 'turn-9' };
    expect(
      shouldAdoptAuthoritativeMessagesForLocalPendingInteraction(session, local, null, false),
    ).toBe(false);
  });

  it('is false while the session already carries its own pending_interaction', () => {
    const local = toolPermissionInteraction({ interaction_id: 'local-1' });
    const session = { state: 'READY', pending_interaction: { interaction_id: 'server-1' } };
    expect(
      shouldAdoptAuthoritativeMessagesForLocalPendingInteraction(session, local, null, false),
    ).toBe(false);
  });

  it('is false when the authoritative interaction id already matches the local one', () => {
    const local = toolPermissionInteraction({ interaction_id: 'same-id' });
    const authoritative = toolPermissionInteraction({ interaction_id: 'same-id' });
    expect(
      shouldAdoptAuthoritativeMessagesForLocalPendingInteraction(readySession, local, authoritative, false),
    ).toBe(false);
  });

  it('is true once every gate clears and the authoritative interaction differs (including missing)', () => {
    const local = toolPermissionInteraction({ interaction_id: 'local-1' });
    expect(
      shouldAdoptAuthoritativeMessagesForLocalPendingInteraction(readySession, local, null, false),
    ).toBe(true);
    const authoritative = toolPermissionInteraction({ interaction_id: 'server-1' });
    expect(
      shouldAdoptAuthoritativeMessagesForLocalPendingInteraction(readySession, local, authoritative, false),
    ).toBe(true);
  });
});
