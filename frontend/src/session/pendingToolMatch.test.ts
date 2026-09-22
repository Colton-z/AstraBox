import { describe, expect, it } from 'vitest';

import { getPendingInteractionPart } from './pendingToolMatch';

describe('getPendingInteractionPart', () => {
  it('accepts a complete vendor interaction frame', () => {
    const interaction = {
      interaction_id: 'interaction-1',
      turn_id: 'turn-1',
      tool_call_id: 'tool-call-1',
      tool_name: 'AskUserQuestion',
      presentation: 'form',
    };

    expect(getPendingInteractionPart({
      type: 'data-interaction',
      data: interaction,
    })).toBe(interaction);
  });

  it.each([
    null,
    {},
    { type: 'data-interaction' },
    { type: 'data-interaction', data: {} },
    {
      type: 'data-interaction',
      data: {
        interaction_id: 'interaction-1',
        turn_id: 'turn-1',
        presentation: 'tool_approval',
      },
    },
    // Same frame as the accepted one above minus the declared presentation:
    // without it no card can be chosen, so the part is not a pending
    // interaction at all.
    {
      type: 'data-interaction',
      data: {
        interaction_id: 'interaction-1',
        turn_id: 'turn-1',
        tool_call_id: 'tool-call-1',
        tool_name: 'AskUserQuestion',
      },
    },
  ])('rejects malformed interaction transport data', (part) => {
    expect(getPendingInteractionPart(part)).toBeNull();
  });
});
