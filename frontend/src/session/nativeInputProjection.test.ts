import type { UIMessage as SDKUIMessage } from 'ai';
import { describe, expect, it } from 'vitest';

import {
  projectNativeInputs,
  retireAdoptedNativeInputs,
  type NativeInputProjectionBoundary,
} from './nativeInputProjection';

const boundary: NativeInputProjectionBoundary = {
  clientMessageId: 'client-3',
  inputId: 'input-3',
  platformTurnId: 'turn-3',
  responseMessageId: 'response-3',
  content: 'third prompt',
};

function assistant(id: string): SDKUIMessage {
  return { id, role: 'assistant', parts: [] };
}

describe('projectNativeInputs', () => {
  it('shows a submitted input before the SDK has accepted or consumed it', () => {
    const submitted = {
      clientMessageId: 'client-submitted',
      inputId: 'client-submitted',
      platformTurnId: '',
      responseMessageId: '',
      content: 'visible immediately',
    };

    expect(projectNativeInputs([assistant('turn-before')], [submitted]))
      .toEqual([
        assistant('turn-before'),
        expect.objectContaining({
          id: 'client-submitted:user',
          client_message_id: 'client-submitted',
          role: 'user',
          parts: [{ type: 'text', text: 'visible immediately' }],
        }),
      ]);
  });

  it('restores a consumed user row after an assistant stream update replaces the message array', () => {
    const projected = projectNativeInputs(
      [assistant('response-1'), assistant('turn-3')],
      [boundary],
    );

    expect(projected.map((message) => [message.role, message.id])).toEqual([
      ['assistant', 'response-1'],
      ['user', 'input-3:user'],
      ['assistant', 'turn-3'],
    ]);
    expect(projected[1].parts).toEqual([{ type: 'text', text: 'third prompt' }]);
  });

  it('does not duplicate the boundary after durable history takes ownership', () => {
    const durableUser = {
      id: 'input-3:user',
      role: 'user' as const,
      metadata: { turn_id: 'turn-3' },
      client_message_id: 'client-3',
      parts: [{ type: 'text' as const, text: 'third prompt' }],
    } as SDKUIMessage;
    const messages = [durableUser, assistant('turn-3')];

    expect(projectNativeInputs(messages, [boundary])).toBe(messages);
  });

  it('keeps system messages without treating them as consumed user input', () => {
    const systemMessage = {
      id: 'system-1',
      role: 'system' as const,
      parts: [{ type: 'text' as const, text: 'system context' }],
    } as SDKUIMessage;

    expect(projectNativeInputs([systemMessage, assistant('turn-3')], [boundary]))
      .toEqual([
        systemMessage,
        expect.objectContaining({ id: 'input-3:user', role: 'user' }),
        assistant('turn-3'),
      ]);
  });

  it('keeps multiple consumed roots in their matching assistant order', () => {
    const first = {
      clientMessageId: 'client-2',
      inputId: 'input-2',
      platformTurnId: 'turn-2',
      responseMessageId: 'response-2',
      content: 'second prompt',
    };
    const projected = projectNativeInputs(
      [assistant('turn-2'), assistant('turn-3')],
      [first, boundary],
    );

    expect(projected.map((message) => [message.role, message.id])).toEqual([
      ['user', 'input-2:user'],
      ['assistant', 'turn-2'],
      ['user', 'input-3:user'],
      ['assistant', 'turn-3'],
    ]);
  });

  it('places an input taken during a running turn after the reply that came before it', () => {
    // A Hermes conversation where the second message was sent while the first
    // reply ran: the engine took it into the same platform turn. Each reply is
    // opened by the `start` the server emits with `data-input-consumed`
    // (`_ResumeCursorTracker.message_payloads`): named by the consumed frame's
    // responseMessageId and carrying the platform turn in its metadata.
    const turnId = '0c744864-acf0-4658-b2e8-b4158ee8d6d9';
    const reply = (id: string): SDKUIMessage => ({
      id,
      role: 'assistant',
      metadata: { part_contract: 'astrabox.parts/1', turn_id: turnId },
      parts: [],
    });
    // As useSessionChat builds them from each `data-input-consumed` frame.
    const first: NativeInputProjectionBoundary = {
      clientMessageId: '40667a0e-62ee-4734-be78-1218d4e03e01',
      inputId: '40667a0e-62ee-4734-be78-1218d4e03e01',
      platformTurnId: turnId,
      responseMessageId: 'a75bc814-d87f-56de-b76e-16226add2439',
      content: 'first prompt',
    };
    const carried: NativeInputProjectionBoundary = {
      clientMessageId: '1abf08f1-c70b-4458-971a-4d19b8388403',
      inputId: '1abf08f1-c70b-4458-971a-4d19b8388403',
      platformTurnId: turnId,
      responseMessageId: '449c5ea0-64c3-5ebc-a82a-1d2d58924e4d',
      content: 'sent while the first reply ran',
    };
    const order = (messages: SDKUIMessage[]) => messages.map((message) => [message.role, message.id]);

    // The SDK's throttled snapshot can render the boundary before the reply it opened.
    expect(order(projectNativeInputs(
      [reply(first.responseMessageId)],
      [first, carried],
    ))).toEqual([
      ['user', `${first.inputId}:user`],
      ['assistant', first.responseMessageId],
      ['user', `${carried.inputId}:user`],
    ]);
    expect(order(projectNativeInputs(
      [reply(first.responseMessageId), reply(carried.responseMessageId)],
      [first, carried],
    ))).toEqual([
      ['user', `${first.inputId}:user`],
      ['assistant', first.responseMessageId],
      ['user', `${carried.inputId}:user`],
      ['assistant', carried.responseMessageId],
    ]);
  });

  it('uses the platform turn as the assistant anchor when the response id differs', () => {
    const projected = projectNativeInputs(
      [assistant('turn-3')],
      [boundary],
    );

    expect(projected.map((message) => [message.role, message.id])).toEqual([
      ['user', 'input-3:user'],
      ['assistant', 'turn-3'],
    ]);
  });
});

describe('retireAdoptedNativeInputs', () => {
  it('keeps the boundary until the array adopted by useChat owns the user row', () => {
    expect(retireAdoptedNativeInputs([boundary], [assistant('turn-3')], 'turn-3'))
      .toEqual([boundary]);
  });

  it('does not transfer ownership from a live boundary to an interim history read', () => {
    const adoptedUser = {
      id: 'input-3:user',
      role: 'user' as const,
      client_message_id: 'client-3',
      parts: [{ type: 'text' as const, text: 'third prompt' }],
    } as SDKUIMessage;

    expect(retireAdoptedNativeInputs(
      [boundary],
      [adoptedUser, assistant('turn-3')],
      null,
    )).toEqual([boundary]);
  });

  it('retires the boundary after terminal durable adoption owns the user row', () => {
    const adoptedUser = {
      id: 'input-3:user',
      role: 'user' as const,
      client_message_id: 'client-3',
      parts: [{ type: 'text' as const, text: 'third prompt' }],
    } as SDKUIMessage;

    expect(retireAdoptedNativeInputs(
      [boundary],
      [adoptedUser, assistant('turn-3')],
      'turn-3',
    )).toEqual([]);
  });

  it('does not let a prior turn completion retire a newer live input', () => {
    const prior = {
      clientMessageId: 'client-2',
      inputId: 'input-2',
      platformTurnId: 'turn-2',
      responseMessageId: 'response-2',
      content: 'second prompt',
    };
    const priorUser = {
      id: 'input-2:user',
      role: 'user' as const,
      client_message_id: 'client-2',
      parts: [{ type: 'text' as const, text: 'second prompt' }],
    } as SDKUIMessage;
    const currentUser = {
      id: 'input-3:user',
      role: 'user' as const,
      client_message_id: 'client-3',
      parts: [{ type: 'text' as const, text: 'third prompt' }],
    } as SDKUIMessage;

    expect(retireAdoptedNativeInputs(
      [prior, boundary],
      [priorUser, assistant('turn-2'), currentUser, assistant('turn-3')],
      'turn-2',
    )).toEqual([boundary]);
  });
});
