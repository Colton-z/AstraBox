/**
 * Owns the page's streaming state for one Session.
 *
 * `phaseTurnId` keeps state from one turn from affecting another. The phase
 * values are the states of `turnStreamMachine` (session/turnStream.machine.ts),
 * so they are mutually exclusive by construction: the page cannot own a stream
 * and consider the same turn finished at once. The durable frame cursor is
 * Session-wide and never resets between those turns. This module is independent
 * of React and the AI SDK: it steps that machine as a pure function and answers
 * `decideAutoResume`.
 */
import { initialTransition, transition } from 'xstate';
import type { SnapshotFrom } from 'xstate';

import { clean, turnStreamMachine } from './turnStream.machine';
import type {
  StreamCursor,
  StreamPhase,
  TurnStreamContext,
  TurnStreamEvent,
} from './turnStream.machine';

export { turnStreamMachine } from './turnStream.machine';
export type {
  StreamCursor,
  StreamPhase,
  TurnStreamContext,
  TurnStreamEvent,
} from './turnStream.machine';

export interface BootstrapStreamCursor {
  turn_id?: unknown;
  frame_seq?: unknown;
}

/** The machine's state as one plain object: its phase beside its context. */
export interface TurnStreamState extends TurnStreamContext {
  phase: StreamPhase;
}

/** A stream that ended cleanly gets one auto-resume; a broken one keeps trying. */
export const CLEAN_RESUME_ATTEMPTS = 1;
export const DROPPED_RESUME_ATTEMPTS = 100;
const MAX_BACKOFF_MS = 5000;

export interface AutoResumeContext {
  /** The SDK transport is mid-request; opening another would clobber it. */
  transportBusy: boolean;
  /** A pending interaction is on screen; answering it owns the resume. */
  pendingInteraction: boolean;
  /** The session projection says this turn wants a stream. */
  needsResume: boolean;
}

export type TurnStreamAction =
  | { kind: 'idle'; reason: string }
  | { kind: 'exhausted'; turnId: string }
  | { kind: 'resume'; turnId: string; afterSeq: number; delayMs: number };

/**
 * A session output subscription is a runtime resource, not a turn retry.
 * Per-turn state may delay a retry, but it must not prevent an idle READY
 * session from opening the response that will carry its next accepted input.
 */
export function shouldOpenSessionSubscription(
  enabled: boolean,
  action: TurnStreamAction,
): boolean {
  if (!enabled || action.kind === 'exhausted') return false;
  return action.kind !== 'idle' || action.reason !== 'transport-busy';
}

function bootstrapFrameSeq(value: unknown, label: string): number {
  if (value === null) return -1;
  if (!Number.isInteger(value) || Number(value) < 0) {
    throw new Error(`${label} must be a non-negative integer or null`);
  }
  return Number(value);
}

/** Choose the durable boundary from which a newly mounted stream starts. */
export function bootstrapStreamCursor(
  activeTurnId: string | null,
  overlayCursor: BootstrapStreamCursor | null,
  sessionFrameSeq: unknown,
): StreamCursor {
  if (overlayCursor) {
    return {
      turnId: clean(overlayCursor.turn_id),
      frameSeq: bootstrapFrameSeq(
        overlayCursor.frame_seq ?? null,
        'active turn resume cursor',
      ),
    };
  }
  return {
    turnId: clean(activeTurnId),
    frameSeq: bootstrapFrameSeq(sessionFrameSeq, 'session frame cursor'),
  };
}

type TurnStreamSnapshot = SnapshotFrom<typeof turnStreamMachine>;

function flatten(snapshot: TurnStreamSnapshot): TurnStreamState {
  return { phase: snapshot.value, ...snapshot.context };
}

function restore(state: TurnStreamState): TurnStreamSnapshot {
  const { phase, ...context } = state;
  return turnStreamMachine.resolveState({ value: phase, context });
}

export function initialTurnStreamState(
  serverTurnId: string | null,
  cursor?: StreamCursor | null,
): TurnStreamState {
  const [snapshot] = initialTransition(turnStreamMachine, { serverTurnId, cursor });
  return flatten(snapshot);
}

/** The phase, but only if it describes the turn being asked about. */
export function phaseFor(state: TurnStreamState, turnId: string | null): StreamPhase {
  const asked = clean(turnId);
  if (!asked) return 'none';
  return clean(state.phaseTurnId) === asked ? state.phase : 'none';
}

export function reduceTurnStream(
  state: TurnStreamState,
  event: TurnStreamEvent,
): TurnStreamState {
  const snapshot = restore(state);
  const [next] = transition(turnStreamMachine, snapshot, event);
  // XState returns the snapshot it was handed when no transition is selected
  // — the event was not one this phase answers, or every guard rejected it.
  // Returning the caller's own object keeps that a true no-op.
  return next === snapshot ? state : flatten(next);
}

export function decideAutoResume(
  state: TurnStreamState,
  ctx: AutoResumeContext,
): TurnStreamAction {
  if (ctx.pendingInteraction) return { kind: 'idle', reason: 'pending-interaction' };
  if (ctx.transportBusy) return { kind: 'idle', reason: 'transport-busy' };

  const turnId = state.serverTurnId;
  if (!turnId) return { kind: 'idle', reason: 'no-turn' };

  const phase = phaseFor(state, turnId);
  if (phase === 'finished') return { kind: 'idle', reason: 'turn-finished' };
  if (phase === 'owned') return { kind: 'idle', reason: 'stream-owned' };
  if (phase !== 'needed' && phase !== 'dropped' && !ctx.needsResume) {
    return { kind: 'idle', reason: 'resume-not-wanted' };
  }

  const budget = phase === 'dropped' ? DROPPED_RESUME_ATTEMPTS : CLEAN_RESUME_ATTEMPTS;
  if (state.attempts >= budget) return { kind: 'exhausted', turnId };

  return {
    kind: 'resume',
    turnId,
    // The backend allocates frame sequence across the whole Session. Rewinding
    // when the turn changes replays every earlier turn through a fresh parser.
    afterSeq: state.cursor.frameSeq,
    delayMs: phase === 'dropped'
      ? Math.min(1000 * (state.attempts + 1), MAX_BACKOFF_MS)
      : 0,
  };
}
