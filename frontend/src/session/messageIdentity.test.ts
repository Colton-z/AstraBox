import { describe, it, expect } from 'vitest';
import type { UIMessage as SDKUIMessage } from 'ai';

import { conversationMessageKeys } from './messageIdentity';

// The two shapes the transcript takes for one turn: the live assistant message
// is named by the turn, while the durable one uses the engine's message id.
// Keying on `message.id` therefore
// changes at the instant an answer completes, React rebuilds the element, and
// the enter animation replays — the flicker this pins.
const TURN = '5b1b3362-da13-4e5c-a666-2cdcfca634b7';
const DURABLE_ASSISTANT = '18c477ff-5afd-5453-b5fb-4151c13d86eb';
const CLIENT_MESSAGE = '6d155861-189f-4632-b793-53b261dcdf74';

const message = (over: Partial<SDKUIMessage> & { role: SDKUIMessage['role'] }): SDKUIMessage => ({
  parts: [{ type: 'text', text: 'x' }],
  ...over,
} as SDKUIMessage);

describe('conversationMessageKeys', () => {
  const live = [
    message({ id: `${CLIENT_MESSAGE}:user`, role: 'user', metadata: { turn_id: TURN } }),
    message({ id: TURN, role: 'assistant', metadata: { turn_id: TURN } }),
  ];
  const settled = [
    message({ id: `${CLIENT_MESSAGE}:user`, role: 'user', metadata: { turn_id: TURN } }),
    message({ id: DURABLE_ASSISTANT, role: 'assistant', metadata: { turn_id: TURN } }),
  ];

  it('gives the same keys before and after a turn settles', () => {
    expect(conversationMessageKeys(live)).toEqual(conversationMessageKeys(settled));
  });

  it('keys a user row by its own id, which already survives the swap', () => {
    // The durable user id is derived from the client message id the live
    // projection uses. Keying it by turn instead would remount the bubble
    // whenever a queued input has no turn id yet — the defect, moved.
    expect(conversationMessageKeys(live)[0]).toBe(`${CLIENT_MESSAGE}:user`);
  });

  it('separates several durable messages of one turn', () => {
    const many = [
      message({ id: 'a', role: 'assistant', metadata: { turn_id: TURN } }),
      message({ id: 'b', role: 'assistant', metadata: { turn_id: TURN } }),
    ];
    const keys = conversationMessageKeys(many);
    expect(new Set(keys).size).toBe(2);
    // The first keeps the live message's key: one engine message became
    // several, and only the ones after the first are new to the reader.
    expect(keys[0]).toBe(conversationMessageKeys(live)[1]);
  });

  it('falls back to the id when a message carries no turn yet', () => {
    const projected = [message({ id: 'input-7:user', role: 'user' })];
    expect(conversationMessageKeys(projected)).toEqual(['input-7:user']);
  });

  it('keeps keys unique across turns', () => {
    const two = [
      message({ id: TURN, role: 'assistant', metadata: { turn_id: TURN } }),
      message({ id: 'other', role: 'assistant', metadata: { turn_id: 'turn-2' } }),
    ];
    expect(new Set(conversationMessageKeys(two)).size).toBe(2);
  });
});
