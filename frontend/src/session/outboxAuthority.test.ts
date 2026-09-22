import { describe, it, expect } from 'vitest';

import type { UIMessage as SDKUIMessage } from 'ai';

import type { MessageRecord, OutboxItem, SessionRecord } from '../types';
import {
  getDurableUserTurnIdForClientMessage,
  markOutboxItemFailed,
  reconcileNativeInputOutbox,
  reconcileOutboxWithSession,
  retainQueuedOutbox,
} from './outboxAuthority';

// Behavioral tests for the outbox reconciliation layer: the pure logic that
// decides when a locally-optimistic outbound message has become durable (and
// can be dropped from the outbox) versus when it should be marked failed.
// Fed straight from useSessionChat, the core turn-machinery hook, so this is
// one of the two most load-bearing pure modules in src/session.

type DurableMsg = Pick<MessageRecord, 'role' | 'client_message_id' | 'turn_id'>;
type FailureSession = Pick<SessionRecord, 'delivery_state' | 'delivery_failure'>;

function outboxItem(overrides: Partial<OutboxItem> = {}): OutboxItem {
  return {
    client_message_id: 'cm-1',
    text: 'hello',
    status: 'sending',
    ...overrides,
  };
}

describe('getDurableUserTurnIdForClientMessage', () => {
  it('returns empty string for a missing/blank client message id', () => {
    expect(getDurableUserTurnIdForClientMessage([], '')).toBe('');
    expect(getDurableUserTurnIdForClientMessage([], '   ')).toBe('');
  });

  it('returns empty string when nothing matches', () => {
    const durable: DurableMsg[] = [{ role: 'user', client_message_id: 'other', turn_id: 'turn-1' }];
    expect(getDurableUserTurnIdForClientMessage(durable, 'cm-1')).toBe('');
  });

  it('returns the turn_id of the matching durable user message', () => {
    const durable: DurableMsg[] = [
      { role: 'user', client_message_id: 'cm-1', turn_id: 'turn-1' },
      { role: 'assistant', client_message_id: undefined, turn_id: 'turn-1' },
    ];
    expect(getDurableUserTurnIdForClientMessage(durable, 'cm-1')).toBe('turn-1');
  });

  it('never matches an assistant message even if it happens to carry the same client_message_id', () => {
    const durable: DurableMsg[] = [{ role: 'assistant', client_message_id: 'cm-1', turn_id: 'turn-1' }];
    expect(getDurableUserTurnIdForClientMessage(durable, 'cm-1')).toBe('');
  });
});

describe('markOutboxItemFailed', () => {
  it('returns the same array reference for a blank client message id (no-op)', () => {
    const outbox = [outboxItem()];
    expect(markOutboxItemFailed(outbox, '', 'boom')).toBe(outbox);
    expect(markOutboxItemFailed(outbox, '   ', 'boom')).toBe(outbox);
  });

  it('returns the same array reference when nothing matches', () => {
    const outbox = [outboxItem({ client_message_id: 'other' })];
    expect(markOutboxItemFailed(outbox, 'cm-1', 'boom')).toBe(outbox);
  });

  it('leaves a non-"sending" item untouched even if the client_message_id matches', () => {
    const outbox = [outboxItem({ client_message_id: 'cm-1', status: 'accepted' })];
    expect(markOutboxItemFailed(outbox, 'cm-1', 'boom')).toBe(outbox);
  });

  it('flips the matching sending item to failed and stamps the failure reason, leaving siblings untouched', () => {
    const target = outboxItem({ client_message_id: 'cm-1', status: 'sending' });
    const sibling = outboxItem({ client_message_id: 'cm-2', status: 'sending' });
    const outbox = [target, sibling];

    const next = markOutboxItemFailed(outbox, 'cm-1', 'network error');

    expect(next).not.toBe(outbox);
    expect(next[0]).toEqual({ ...target, status: 'failed', failure_reason: 'network error' });
    expect(next[1]).toBe(sibling);
  });
});

describe('reconcileNativeInputOutbox', () => {
  const consumedUserMessage = (id: string): SDKUIMessage => ({
    id,
    role: 'user',
    parts: [{ type: 'text', text: 'consumed' }],
  } as SDKUIMessage);

  it('drops an accepted native input once the FIFO stops listing it and the transcript shows it', () => {
    const consumed = outboxItem({
      client_message_id: 'cm-consumed',
      command_id: 'command-consumed',
      input_id: 'input-consumed',
      status: 'accepted',
    });
    const pending = outboxItem({
      client_message_id: 'cm-pending',
      command_id: 'command-pending',
      input_id: 'input-pending',
      status: 'accepted',
    });

    const next = reconcileNativeInputOutbox(
      [consumed, pending],
      [pending],
      [consumedUserMessage('input-consumed:user')],
    );

    expect(next).toEqual([pending]);
  });

  it('holds a consumed row until the transcript catches up with it', () => {
    // Leaving that FIFO means consumed, and consumption also emits the frame
    // that puts the message on screen — but the two race, and a session read
    // that wins would otherwise take the message off both surfaces.
    const consumed = outboxItem({
      client_message_id: 'cm-consumed',
      command_id: 'command-consumed',
      input_id: 'input-consumed',
      status: 'accepted',
    });

    expect(reconcileNativeInputOutbox([consumed], [], [])).toEqual([consumed]);
  });

  it('does not drop a local send before it has a correlated native receipt', () => {
    const sending = outboxItem({
      client_message_id: 'cm-sending',
      status: 'sending',
    });
    expect(reconcileNativeInputOutbox([sending], [])).toEqual([sending]);
  });

  it('rehydrates a server pending row and upgrades its matching local send', () => {
    const sending = outboxItem({
      client_message_id: 'cm-1',
      status: 'sending',
    });
    const durable = outboxItem({
      client_message_id: 'cm-1',
      command_id: 'command-1',
      input_id: 'input-1',
      status: 'accepted',
    });

    expect(reconcileNativeInputOutbox([sending], [durable])).toEqual([durable]);
  });
});

describe('reconcileOutboxWithSession', () => {
  const noFailure: FailureSession = { delivery_state: null, delivery_failure: null };

  it('returns the same array reference for an empty outbox', () => {
    const outbox: OutboxItem[] = [];
    expect(reconcileOutboxWithSession(outbox, noFailure)).toBe(outbox);
  });

  it('always keeps an item with no client_message_id (defensive: nothing to reconcile against)', () => {
    const outbox = [outboxItem({ client_message_id: '' })];
    expect(reconcileOutboxWithSession(outbox, noFailure)).toEqual(outbox);
  });

  it('drops an item once its client_message_id shows up among durable user messages', () => {
    const outbox = [outboxItem({ client_message_id: 'cm-1' }), outboxItem({ client_message_id: 'cm-2' })];
    const durable: DurableMsg[] = [{ role: 'user', client_message_id: 'cm-1', turn_id: 'turn-1' }];

    const next = reconcileOutboxWithSession(outbox, noFailure, durable);

    expect(next.map((i) => i.client_message_id)).toEqual(['cm-2']);
  });

  it('keeps an item that is still in flight (not durable, not currently failing)', () => {
    const outbox = [outboxItem({ client_message_id: 'cm-1' })];
    expect(reconcileOutboxWithSession(outbox, noFailure, [])).toEqual(outbox);
  });

  it('keeps the item currently reported as the delivery failure, even before it is durable', () => {
    const outbox = [outboxItem({ client_message_id: 'cm-1' })];
    const session: FailureSession = {
      delivery_state: 'NOT_RECEIVED',
      delivery_failure: { turn_id: 'turn-1', client_message_id: 'cm-1', text: 'hi', summary: 'send failed' },
    };
    expect(reconcileOutboxWithSession(outbox, session, [])).toEqual(outbox);
  });

  it('ignores delivery_failure.client_message_id unless delivery_state is exactly NOT_RECEIVED', () => {
    const outbox = [outboxItem({ client_message_id: 'cm-1' })];
    const durable: DurableMsg[] = [{ role: 'user', client_message_id: 'cm-1', turn_id: 'turn-1' }];
    // delivery_state has since moved on (e.g. RECEIVED) — the stale failure pointer
    // must not keep a now-durable item alive in the outbox.
    const session: FailureSession = {
      delivery_state: 'RECEIVED',
      delivery_failure: { turn_id: 'turn-1', client_message_id: 'cm-1', text: 'hi', summary: 'send failed' },
    };
    expect(reconcileOutboxWithSession(outbox, session, durable)).toEqual([]);
  });

  it('prioritizes the current-failure check over the durable check for the same client_message_id', () => {
    // Pathological but real: the item is both the live delivery failure AND
    // already visible in durable history. The failure check runs first in
    // reconcileOutboxWithSession, so it must win — the item stays visible.
    const outbox = [outboxItem({ client_message_id: 'cm-1' })];
    const durable: DurableMsg[] = [{ role: 'user', client_message_id: 'cm-1', turn_id: 'turn-1' }];
    const session: FailureSession = {
      delivery_state: 'NOT_RECEIVED',
      delivery_failure: { turn_id: 'turn-1', client_message_id: 'cm-1', text: 'hi', summary: 'send failed' },
    };
    expect(reconcileOutboxWithSession(outbox, session, durable)).toEqual(outbox);
  });
});

describe('retainQueuedOutbox', () => {
  // A queued message must stay visible until its user message reaches the
  // transcript. Ending the turn ahead of it does not establish that handover.
  function userMessage(overrides: Record<string, unknown> = {}): SDKUIMessage {
    return {
      id: 'msg-1',
      role: 'user',
      parts: [{ type: 'text', text: 'hello' }],
      ...overrides,
    } as SDKUIMessage;
  }

  it('returns the same array reference for an empty outbox', () => {
    const outbox: OutboxItem[] = [];
    expect(retainQueuedOutbox(outbox, [userMessage()])).toBe(outbox);
  });

  it('keeps a queued row while the transcript does not carry it', () => {
    const outbox = [outboxItem({ client_message_id: 'cm-queued' })];
    expect(retainQueuedOutbox(outbox, [userMessage({ id: 'cm-other:user' })])).toBe(outbox);
  });

  it('keeps a queued row when the transcript holds only the assistant reply', () => {
    const outbox = [outboxItem({ client_message_id: 'cm-queued' })];
    const assistant = {
      id: 'cm-queued:user',
      role: 'assistant',
      parts: [{ type: 'text', text: 'answering' }],
    } as SDKUIMessage;
    expect(retainQueuedOutbox(outbox, [assistant])).toBe(outbox);
  });

  it('drops the row once the projected user message carries its client id', () => {
    const outbox = [outboxItem({ client_message_id: 'cm-queued' }), outboxItem({ client_message_id: 'cm-next' })];
    const remaining = retainQueuedOutbox(outbox, [userMessage({ client_message_id: 'cm-queued' })]);
    expect(remaining.map((item) => item.client_message_id)).toEqual(['cm-next']);
  });

  it('drops the row when the transcript identifies it by engine input id', () => {
    // A backend-driven handover names the input, never the client's own id.
    const outbox = [outboxItem({ client_message_id: 'cm-queued', input_id: 'in-7' })];
    expect(retainQueuedOutbox(outbox, [userMessage({ id: 'in-7:user' })])).toEqual([]);
  });

  it('does not let a durable user row with no client id sweep an unrelated queued row', () => {
    const outbox = [outboxItem({ client_message_id: 'cm-queued' })];
    const anonymous = userMessage({ id: 'durable-1', client_message_id: undefined });
    expect(retainQueuedOutbox(outbox, [anonymous])).toBe(outbox);
  });

  it('keeps a failed row on screen even after the transcript carries the message', () => {
    // A failure is the user's to dismiss or retry; it is not waiting on a
    // handover, so the transcript arriving does not resolve it.
    const outbox = [outboxItem({ client_message_id: 'cm-failed', status: 'failed', failure_reason: 'boom' })];
    expect(retainQueuedOutbox(outbox, [userMessage({ client_message_id: 'cm-failed' })])).toBe(outbox);
  });
});

describe('outbox reconcilers are identity-stable on a no-op', () => {
  // useSessionChat stores reconciliation results in React state. Returning a
  // fresh array for an unchanged outbox can keep an effect with unstable
  // dependencies rendering until React reports "Maximum update depth exceeded".
  const queued = outboxItem({
    client_message_id: 'cm-1',
    command_id: 'command-1',
    input_id: 'input-1',
    status: 'accepted',
  });
  const idleSession = { delivery_state: 'RECEIVED', delivery_failure: null } as FailureSession;

  it('reconcileOutboxWithSession returns the same array when nothing is durable yet', () => {
    const outbox = [queued];
    expect(reconcileOutboxWithSession(outbox, idleSession, [])).toBe(outbox);
  });

  it('reconcileNativeInputOutbox returns the same array when the FIFO still lists the row', () => {
    const outbox = [queued];
    expect(reconcileNativeInputOutbox(outbox, [queued], [])).toBe(outbox);
  });

  it('retainQueuedOutbox returns the same array when the transcript carries nothing', () => {
    const outbox = [queued];
    expect(retainQueuedOutbox(outbox, [])).toBe(outbox);
  });
});
