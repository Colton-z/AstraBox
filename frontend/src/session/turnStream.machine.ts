/**
 * The statechart behind session/turnStream.ts.
 *
 * The phase vocabulary is this machine's states, so a phase change is a
 * transition and the phases stay mutually exclusive by construction. Anything
 * the phase does not name — the turn it describes, the durable cursor, the
 * auto-resume budget — is context.
 *
 * The chart is data: guards and assignments are named, and it declares no
 * actor, no invoke and no delayed transition, so `stately.ai/viz` renders it
 * and session/turnStream.ts can step it with XState's pure `transition`
 * instead of running an actor.
 */
import { assertEvent, assign, not, setup } from 'xstate';

export type StreamPhase =
  /** No stream this page owns has touched this turn. */
  | 'none'
  /** The backend accepted the turn, but the page has not opened its output stream yet. */
  | 'needed'
  /** A stream belongs to this page: in flight, or about to be opened by the code that said so. */
  | 'owned'
  /** The stream this page owned ended cleanly — as far as it saw, the turn is done. */
  | 'finished'
  /** The stream this page owned broke. A resume is wanted, and wanted repeatedly. */
  | 'dropped';

export interface StreamCursor {
  /** Turn attribution for phase/message bookkeeping; never cursor scope. */
  turnId: string | null;
  /** The last complete frame in the Session-wide monotonic sequence. */
  frameSeq: number;
}

/** Everything the machine carries that the phase itself does not name. */
export interface TurnStreamContext {
  /** The turn the server's projection calls current. */
  serverTurnId: string | null;
  /** The turn the phase describes. A phase for another turn is never consulted. */
  phaseTurnId: string | null;
  /** Where this Session's next subscription starts; -1 replays from the head. */
  cursor: StreamCursor;
  /** Auto-resume attempts already spent on `serverTurnId`. */
  attempts: number;
}

export interface TurnStreamInput {
  serverTurnId: string | null;
  cursor?: StreamCursor | null;
}

export type TurnStreamEvent =
  /** A different session is on screen: nothing carries over but the cursor it came with. */
  | { type: 'session-changed'; serverTurnId: string | null; cursor?: StreamCursor }
  /** The session projection reported the current turn. */
  | { type: 'server-turn-observed'; turnId: string | null }
  /** The user sent a message; its output stream becomes actionable after acceptance supplies a turn id. */
  | { type: 'send-started' }
  /** The input receipt or output stream identified the accepted turn. */
  | { type: 'turn-accepted'; turnId: string }
  /** A durable-frame cursor arrived on the stream. */
  | { type: 'cursor-advanced'; turnId: string | null; frameSeq: number }
  /**
   * A rehydrate returned the server's own overlay cursor. That is authority,
   * not an increment: it replaces whatever the stream had accumulated,
   * including backwards and across turns.
   */
  | { type: 'cursor-rebased'; cursor: StreamCursor }
  /** The runner cursor expired: rebuild from SessionStore before this stream is reopened. */
  | { type: 'session-store-rebuild-required'; turnId: string | null; resumeSequence: number }
  /** The page's stream ended. */
  | { type: 'stream-finished'; outcome: 'clean' | 'disconnected' | 'aborted' | 'error'; turnId: string | null }
  /** The transport errored mid-stream. */
  | { type: 'stream-error'; hasActiveTurn: boolean }
  /**
   * A pending interaction was answered. The turn CONTINUES — the backend closes
   * the segment at a park by design and resumes the same turn under a new
   * command id — so this can never leave a 'finished' phase standing.
   */
  | { type: 'interaction-answered'; turnId: string | null }
  /** A resume stream is being opened now. */
  | { type: 'resume-started'; turnId: string | null }
  /**
   * A stream the page opened came back with response headers. That is the
   * proof a reconnect worked, and it is separate from any frame arriving:
   * a resumed stream can sit open for minutes before the engine says
   * anything.
   */
  | { type: 'stream-established' }
  /** The turn was confirmed terminal against the server, not merely finished locally. */
  | { type: 'turn-settled' }
  /** Lifecycle and transport are both idle; no stream this page owns is outstanding. */
  | { type: 'went-idle' };

/** A turn id as the machine compares it: blank and whitespace are absence. */
export function clean(value: unknown): string | null {
  const trimmed = String(value ?? '').trim();
  return trimmed || null;
}

function freshContext(serverTurnId: string | null, cursor: StreamCursor | null): TurnStreamContext {
  const turnId = clean(serverTurnId);
  return {
    serverTurnId: turnId,
    phaseTurnId: null,
    cursor: cursor ?? { turnId, frameSeq: -1 },
    attempts: 0,
  };
}

export const turnStreamMachine = setup({
  types: {
    context: {} as TurnStreamContext,
    events: {} as TurnStreamEvent,
    input: {} as TurnStreamInput,
  },
  guards: {
    namesADifferentTurn: ({ context, event }) => {
      assertEvent(event, 'server-turn-observed');
      return clean(event.turnId) !== context.serverTurnId;
    },
    acceptanceCarriesATurnId: ({ event }) => {
      assertEvent(event, 'turn-accepted');
      return clean(event.turnId) !== null;
    },
    acceptanceKeepsThisStream: ({ context, event }) => {
      assertEvent(event, 'turn-accepted');
      const turnId = clean(event.turnId);
      if (!turnId) return false;
      const owned = clean(context.phaseTurnId);
      // A stream opened before the turn had an id claims the turn it accepts.
      return owned === null || owned === turnId;
    },
    frameSeqIsDurable: ({ event }) => {
      assertEvent(event, 'cursor-advanced');
      return Number.isFinite(event.frameSeq) && event.frameSeq >= 0;
    },
    resumeSequenceIsDurable: ({ event }) => {
      assertEvent(event, 'session-store-rebuild-required');
      return Number.isInteger(event.resumeSequence) && event.resumeSequence >= 0;
    },
    finishedCleanly: ({ event }) => {
      assertEvent(event, 'stream-finished');
      return event.outcome === 'clean';
    },
    finishedByDisconnect: ({ event }) => {
      assertEvent(event, 'stream-finished');
      return event.outcome === 'disconnected';
    },
    streamErrorHasActiveTurn: ({ event }) => {
      assertEvent(event, 'stream-error');
      return event.hasActiveTurn;
    },
    hasServerTurn: ({ context }) => context.serverTurnId !== null,
  },
  actions: {
    restartForSession: assign(({ event }) => {
      assertEvent(event, 'session-changed');
      return freshContext(event.serverTurnId, event.cursor ?? null);
    }),
    adoptServerTurn: assign({
      serverTurnId: ({ event }) => {
        assertEvent(event, 'server-turn-observed');
        return clean(event.turnId);
      },
      // A new turn gets a fresh budget. The phase keeps its own turn stamp and
      // this transition has no target, so a phase belonging to the turn that
      // just ended stays put and `phaseFor` stops consulting it.
      attempts: 0,
    }),
    awaitAcceptance: assign({
      // The turn id does not exist yet; acceptance supplies it. The frame
      // boundary belongs to the Session, so a new turn cannot reset it.
      phaseTurnId: null,
      attempts: 0,
    }),
    adoptAcceptedTurn: assign(({ context, event }) => {
      assertEvent(event, 'turn-accepted');
      const turnId = clean(event.turnId);
      return {
        serverTurnId: turnId,
        phaseTurnId: turnId,
        cursor: context.cursor.turnId === turnId
          ? context.cursor
          : { turnId, frameSeq: context.cursor.frameSeq },
      };
    }),
    advanceCursor: assign(({ context, event }) => {
      assertEvent(event, 'cursor-advanced');
      const turnId = clean(event.turnId);
      const prev = context.cursor;
      if (event.frameSeq < prev.frameSeq) {
        return { cursor: prev };
      }
      return {
        cursor: {
          turnId: turnId ?? prev.turnId,
          frameSeq: event.frameSeq,
        },
      };
    }),
    rebaseCursor: assign({
      cursor: ({ event }) => {
        assertEvent(event, 'cursor-rebased');
        return event.cursor;
      },
    }),
    stampRebuildTurn: assign({
      phaseTurnId: ({ context, event }) => {
        assertEvent(event, 'session-store-rebuild-required');
        return clean(event.turnId) ?? context.cursor.turnId ?? context.serverTurnId;
      },
    }),
    stampFinishedTurn: assign({
      phaseTurnId: ({ context, event }) => {
        assertEvent(event, 'stream-finished');
        return clean(event.turnId) ?? context.phaseTurnId;
      },
    }),
    stampErroredTurn: assign({ phaseTurnId: ({ context }) => context.serverTurnId }),
    stampContinuedTurn: assign({
      phaseTurnId: ({ context, event }) => {
        assertEvent(event, 'interaction-answered');
        return clean(event.turnId) ?? context.serverTurnId;
      },
    }),
    stampResumeTurn: assign({
      phaseTurnId: ({ context, event }) => {
        assertEvent(event, 'resume-started');
        return clean(event.turnId) ?? context.serverTurnId;
      },
      attempts: ({ context }) => context.attempts + 1,
    }),
    releasePhase: assign({ phaseTurnId: null }),
    releasePhaseAndBudget: assign({ phaseTurnId: null, attempts: 0 }),
    // The budget counts reconnects that did not work. One that did is not a
    // debt the next disconnect inherits, so an established stream returns it.
    // Without this the budget is spent per turn rather than per failure, and
    // a long turn on a flaky link stops resuming while every reconnect it
    // ever made had succeeded.
    replenishBudget: assign({ attempts: 0 }),
  },
}).createMachine({
  id: 'turnStream',
  initial: 'none',
  context: ({ input }) => freshContext(input.serverTurnId, input.cursor ?? null),
  // Transitions every phase answers the same way. A state below that answers
  // one of these itself is consulted first; XState falls back to this list
  // when the state's own guard rejects the event.
  on: {
    'session-changed': { target: '.none', actions: 'restartForSession' },
    'server-turn-observed': { guard: 'namesADifferentTurn', actions: 'adoptServerTurn' },
    'send-started': { target: '.needed', actions: 'awaitAcceptance' },
    'turn-accepted': {
      guard: 'acceptanceCarriesATurnId',
      target: '.needed',
      actions: 'adoptAcceptedTurn',
    },
    'cursor-advanced': { guard: 'frameSeqIsDurable', actions: 'advanceCursor' },
    'cursor-rebased': { actions: 'rebaseCursor' },
    'session-store-rebuild-required': {
      guard: 'resumeSequenceIsDurable',
      target: '.dropped',
      actions: 'stampRebuildTurn',
    },
    'stream-finished': [
      { guard: 'finishedCleanly', target: '.finished', actions: 'stampFinishedTurn' },
      { guard: 'finishedByDisconnect', target: '.dropped', actions: 'stampFinishedTurn' },
      // An abort or an error is not evidence either way: the turn is neither
      // known finished nor known to want a retry.
      { target: '.none', actions: 'releasePhase' },
    ],
    'stream-error': {
      guard: 'streamErrorHasActiveTurn',
      target: '.dropped',
      actions: 'stampErroredTurn',
    },
    'interaction-answered': { target: '.needed', actions: 'stampContinuedTurn' },
    'resume-started': { target: '.owned', actions: 'stampResumeTurn' },
    // No target: establishing a stream says nothing about which phase the
    // turn is in, only that the attempt that opened it is not owed back.
    'stream-established': { actions: 'replenishBudget' },
    'turn-settled': { target: '.none', actions: 'releasePhase' },
    // Ownership and a wanted retry both expire when nothing is outstanding,
    // and with no current turn the budget expires too. A *finished* phase
    // stays finished while the server still names its turn current — that is
    // the whole point of the mark — so `finished` declares no exit below.
    'went-idle': { guard: not('hasServerTurn'), target: '.none', actions: 'releasePhaseAndBudget' },
  },
  states: {
    none: {},
    needed: {
      on: { 'went-idle': { guard: 'hasServerTurn', target: 'none', actions: 'releasePhase' } },
    },
    owned: {
      on: {
        'turn-accepted': { guard: 'acceptanceKeepsThisStream', actions: 'adoptAcceptedTurn' },
        'went-idle': { guard: 'hasServerTurn', target: 'none', actions: 'releasePhase' },
      },
    },
    finished: {},
    dropped: {
      on: { 'went-idle': { guard: 'hasServerTurn', target: 'none', actions: 'releasePhase' } },
    },
  },
});
