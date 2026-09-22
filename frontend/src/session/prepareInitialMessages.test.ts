import { describe, expect, it } from 'vitest';

import type { ActiveTurnOverlay } from '../api';
import type { MessageRecord } from '../types';
import { prepareInitialMessages } from './prepareInitialMessages';

function record(
  role: 'user' | 'assistant',
  messageId: string,
  turnId: string,
  content: string,
): MessageRecord {
  return {
    session_id: 'session-1',
    message_id: messageId,
    turn_id: turnId,
    client_message_id: role === 'user' ? messageId.replace(/:user$/, '') : null,
    role,
    content,
    blocks: role === 'assistant' && content
      ? [{ type: 'text', text: content }]
      : [],
  };
}

describe('prepareInitialMessages native SDK FIFO overlay', () => {
  it('rehydrates every pre-Result root exchange in SDK order', () => {
    const messages = [
      record('user', 'input-1:user', 'platform-turn-1', 'first'),
      record('assistant', 'response-1', 'platform-turn-1', 'first answer'),
      record('user', 'input-2:user', 'platform-turn-1', 'second'),
      record('assistant', 'response-2', 'platform-turn-1', ''),
    ];
    const overlay: ActiveTurnOverlay = {
      turn_id: 'platform-turn-1',
      message: messages[3],
      messages,
      resume_cursor: { turn_id: 'platform-turn-1', frame_seq: 8 },
    };

    const prepared = prepareInitialMessages([], overlay, {
      delivery_state: null,
      delivery_failure: null,
      pending_interaction: null,
    });

    expect(prepared.map((message) => [message.role, message.id])).toEqual([
      ['user', 'input-1:user'],
      ['assistant', 'response-1'],
      ['user', 'input-2:user'],
      ['assistant', 'response-2'],
    ]);
    expect(prepared[0].parts).toEqual([{ type: 'text', text: 'first' }]);
    expect(prepared[2].parts).toEqual([{ type: 'text', text: 'second' }]);
    expect(prepared[3].parts).toEqual([{ type: 'text', text: '' }]);
  });

  it('replaces a matching durable root instead of duplicating it', () => {
    const durable = [record('user', 'input-1:user', 'platform-turn-1', 'stale')];
    const live = record(
      'user',
      'input-1:user',
      'platform-turn-1',
      'authoritative live root',
    );
    const overlay: ActiveTurnOverlay = {
      turn_id: 'platform-turn-1',
      message: live,
      messages: [live],
      resume_cursor: { turn_id: 'platform-turn-1', frame_seq: 2 },
    };

    const prepared = prepareInitialMessages(durable, overlay, {
      delivery_state: null,
      delivery_failure: null,
      pending_interaction: null,
    });

    expect(prepared).toHaveLength(1);
    expect(prepared[0].parts).toEqual([
      { type: 'text', text: 'authoritative live root' },
    ]);
  });

  it('attaches a pending tool to its native message when one platform turn has multiple responses', () => {
    const first = record('assistant', 'response-1', 'platform-turn-1', '');
    first.blocks = [{
      type: 'tool_use',
      id: 'tool-1',
      name: 'Write',
      input: { file_path: 'first.txt' },
    }];
    const second = record('assistant', 'response-2', 'platform-turn-1', '');
    second.blocks = [{
      type: 'tool_use',
      id: 'tool-2',
      name: 'Write',
      input: { file_path: 'second.txt' },
    }];

    const prepared = prepareInitialMessages([first, second], null, {
      delivery_state: null,
      delivery_failure: null,
      pending_interaction: {
        interaction_id: 'interaction-2',
        turn_id: 'platform-turn-1',
        tool_call_id: 'tool-2',
        tool_name: 'Write',
        presentation: 'tool_approval',
      },
    });

    expect(prepared[0].parts.some((part) => part.type === 'data-interaction')).toBe(false);
    expect(prepared[1].parts).toEqual(expect.arrayContaining([
      expect.objectContaining({
        type: 'dynamic-tool',
        toolCallId: 'tool-2',
        state: 'approval-requested',
      }),
      expect.objectContaining({
        type: 'data-interaction',
        data: expect.objectContaining({ interaction_id: 'interaction-2' }),
      }),
    ]));
  });
});
