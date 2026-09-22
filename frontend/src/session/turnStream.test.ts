import { describe, expect, it } from 'vitest';
import {
  bootstrapStreamCursor,
  CLEAN_RESUME_ATTEMPTS,
  DROPPED_RESUME_ATTEMPTS,
  decideAutoResume,
  initialTurnStreamState,
  phaseFor,
  reduceTurnStream,
  shouldOpenSessionSubscription,
  turnStreamMachine,
  type AutoResumeContext,
  type StreamPhase,
  type TurnStreamEvent,
  type TurnStreamState,
} from './turnStream';

const TURN = 'turn-a';
const OTHER = 'turn-b';

function run(state: TurnStreamState, ...events: TurnStreamEvent[]): TurnStreamState {
  return events.reduce(reduceTurnStream, state);
}

function ctx(over: Partial<AutoResumeContext> = {}): AutoResumeContext {
  return { transportBusy: false, pendingInteraction: false, needsResume: true, ...over };
}

describe('turnStream — the statechart is the phase vocabulary', () => {
  const PHASES: StreamPhase[] = ['none', 'needed', 'owned', 'finished', 'dropped'];

  it('names one state per phase and nothing besides', () => {
    // `reduceTurnStream` reads the snapshot value as a `StreamPhase`, so a
    // state the vocabulary does not name fails to compile. This covers the
    // other direction: a phase `decideAutoResume` branches on that no state
    // produces makes that branch unreachable, and only a runtime check sees it.
    expect(Object.keys(turnStreamMachine.states).sort()).toEqual([...PHASES].sort());
  });

  it('declares no actor and no delayed transition', () => {
    // The module steps the chart with XState's pure `transition` and starts no
    // actor, so an `invoke` or an `after` would sit in the chart and never
    // fire: whatever phase it targets becomes unreachable in the page.
    for (const node of [turnStreamMachine.root, ...Object.values(turnStreamMachine.states)]) {
      expect(node.invoke).toEqual([]);
      expect(node.after).toEqual([]);
    }
  });
});

describe('turnStream — the phase describes one turn and only that turn', () => {
  it('ignores a phase belonging to a different turn', () => {
    const state = run(
      initialTurnStreamState(TURN),
      { type: 'stream-finished', outcome: 'clean', turnId: OTHER },
    );
    expect(phaseFor(state, TURN)).toBe('none');
    expect(phaseFor(state, OTHER)).toBe('finished');
  });

  it('a clean finish of an earlier turn does not block the next one', () => {
    // `cleanFinishSuppressResume` was a single shared boolean, so a clean finish
    // of turn A would block auto-resume of turn B until some unrelated call site
    // cleared it. Scoping the phase to `phaseTurnId` keeps a finish on A from
    // being consulted when resume decides for B.
    const state = run(
      initialTurnStreamState(TURN),
      { type: 'stream-finished', outcome: 'clean', turnId: TURN },
      { type: 'server-turn-observed', turnId: OTHER },
    );
    expect(decideAutoResume(state, ctx())).toMatchObject({ kind: 'resume', turnId: OTHER });
  });

  it('a new turn restores the auto-resume budget', () => {
    const spent = run(
      initialTurnStreamState(TURN),
      { type: 'stream-error', hasActiveTurn: true },
      { type: 'resume-started', turnId: TURN },
    );
    expect(spent.attempts).toBe(1);
    expect(run(spent, { type: 'server-turn-observed', turnId: OTHER }).attempts).toBe(0);
  });
});

describe('turnStream — answering an interaction means the turn continues', () => {
  it('cannot leave a finished phase standing', () => {
    // Answering an interaction creates a new output-stream obligation. One
    // phase field prevents the same turn from remaining both finished and needed.
    const state = run(
      initialTurnStreamState(TURN),
      { type: 'stream-finished', outcome: 'clean', turnId: TURN },
      { type: 'interaction-answered', turnId: TURN },
    );
    expect(phaseFor(state, TURN)).toBe('needed');
    expect(decideAutoResume(state, ctx())).toMatchObject({ kind: 'resume', turnId: TURN });
  });

  it('claims the turn even when the answer arrives without a turn id', () => {
    const state = run(
      initialTurnStreamState(TURN),
      { type: 'stream-finished', outcome: 'clean', turnId: TURN },
      { type: 'interaction-answered', turnId: null },
    );
    expect(phaseFor(state, TURN)).toBe('needed');
  });
});

describe('turnStream — the resume decision', () => {
  it('starts an idle subscription response at the session history boundary', () => {
    expect(bootstrapStreamCursor(null, null, 27)).toEqual({
      turnId: null,
      frameSeq: 27,
    });
    expect(bootstrapStreamCursor(null, null, null)).toEqual({
      turnId: null,
      frameSeq: -1,
    });
  });

  it('prefers an active overlay boundary and rejects a malformed wire cursor', () => {
    expect(bootstrapStreamCursor('turn-live', {
      turn_id: 'turn-overlay',
      frame_seq: 12,
    }, 27)).toEqual({
      turnId: 'turn-overlay',
      frameSeq: 12,
    });
    expect(() => bootstrapStreamCursor(null, null, undefined)).toThrow(
      'session frame cursor must be a non-negative integer or null',
    );
  });

  it('opens a subscription response before the first turn and gives that turn ownership', () => {
    const idle = initialTurnStreamState(null);
    const decision = decideAutoResume(idle, ctx({ needsResume: false }));

    expect(decision).toMatchObject({ kind: 'idle', reason: 'no-turn' });
    expect(shouldOpenSessionSubscription(true, decision)).toBe(true);

    const accepted = run(
      idle,
      { type: 'resume-started', turnId: null },
      { type: 'turn-accepted', turnId: TURN },
    );
    expect(phaseFor(accepted, TURN)).toBe('owned');
  });

  it('does not open while provisioning, while transport is busy, or after exhaustion', () => {
    const idle = decideAutoResume(
      initialTurnStreamState(null),
      ctx({ needsResume: false }),
    );
    const busy = decideAutoResume(
      initialTurnStreamState(TURN),
      ctx({ transportBusy: true }),
    );
    const exhausted = decideAutoResume(
      run(
        initialTurnStreamState(TURN),
        { type: 'resume-started', turnId: TURN },
        { type: 'went-idle' },
      ),
      ctx(),
    );

    expect(shouldOpenSessionSubscription(false, idle)).toBe(false);
    expect(shouldOpenSessionSubscription(true, busy)).toBe(false);
    expect(shouldOpenSessionSubscription(true, exhausted)).toBe(false);
  });

  it('continues an accepted next turn from the Session history boundary', () => {
    const state = run(
      initialTurnStreamState(null, { turnId: OTHER, frameSeq: 50 }),
      { type: 'send-started' },
      { type: 'turn-accepted', turnId: TURN },
    );

    expect(state.serverTurnId).toBe(TURN);
    expect(state.cursor).toEqual({ turnId: TURN, frameSeq: 50 });
    expect(phaseFor(state, TURN)).toBe('needed');
    expect(decideAutoResume(state, ctx({ needsResume: false })))
      .toMatchObject({ kind: 'resume', turnId: TURN, afterSeq: 50, delayMs: 0 });
  });

  it('stands down while an interaction is pending: answering it owns the resume', () => {
    const state = initialTurnStreamState(TURN);
    expect(decideAutoResume(state, ctx({ pendingInteraction: true })))
      .toMatchObject({ reason: 'pending-interaction' });
  });

  it('stands down while the transport is mid-request', () => {
    const state = initialTurnStreamState(TURN);
    expect(decideAutoResume(state, ctx({ transportBusy: true })))
      .toMatchObject({ reason: 'transport-busy' });
  });

  it('stands down when nobody wants a resume and nothing broke', () => {
    const state = initialTurnStreamState(TURN);
    expect(decideAutoResume(state, ctx({ needsResume: false })))
      .toMatchObject({ reason: 'resume-not-wanted' });
  });

  it('resumes a broken stream even when the projection did not ask', () => {
    const state = run(
      initialTurnStreamState(TURN),
      { type: 'stream-error', hasActiveTurn: true },
    );
    expect(decideAutoResume(state, ctx({ needsResume: false })))
      .toMatchObject({ kind: 'resume', turnId: TURN });
  });

  it('replays from the Session cursor regardless of which turn emitted it', () => {
    const own = run(
      initialTurnStreamState(TURN),
      { type: 'cursor-advanced', turnId: TURN, frameSeq: 14 },
    );
    expect(decideAutoResume(own, ctx())).toMatchObject({ afterSeq: 14 });

    const foreign = run(
      initialTurnStreamState(TURN),
      { type: 'cursor-advanced', turnId: OTHER, frameSeq: 14 },
    );
    expect(decideAutoResume(foreign, ctx())).toMatchObject({ afterSeq: 14 });
  });

  it('spends one attempt on a quiet turn and backs off on a broken one', () => {
    let state = initialTurnStreamState(TURN);
    expect(decideAutoResume(state, ctx())).toMatchObject({ kind: 'resume', delayMs: 0 });
    state = run(state, { type: 'resume-started', turnId: TURN }, { type: 'went-idle' });
    expect(state.attempts).toBe(CLEAN_RESUME_ATTEMPTS);
    expect(decideAutoResume(state, ctx())).toMatchObject({ kind: 'exhausted' });

    let broken = run(
      initialTurnStreamState(TURN),
      { type: 'stream-error', hasActiveTurn: true },
    );
    for (let i = 0; i < 3; i += 1) {
      const action = decideAutoResume(broken, ctx());
      expect(action.kind).toBe('resume');
      broken = run(
        broken,
        { type: 'resume-started', turnId: TURN },
        { type: 'stream-error', hasActiveTurn: true },
      );
    }
    expect(decideAutoResume(broken, ctx())).toMatchObject({ delayMs: 4000 });
    expect(DROPPED_RESUME_ATTEMPTS).toBeGreaterThan(CLEAN_RESUME_ATTEMPTS);
  });

  it('returns the attempt a reconnect that worked had spent', () => {
    // The budget exists to stop retrying at a door that stays shut. A
    // reconnect that opened one is not evidence of that, so it must not
    // count — otherwise the budget measures how long the turn ran on a
    // flaky link rather than how often reconnecting failed.
    let state = run(
      initialTurnStreamState(TURN),
      { type: 'stream-error', hasActiveTurn: true },
      { type: 'resume-started', turnId: TURN },
    );
    expect(state.attempts).toBe(1);

    state = run(state, { type: 'stream-established' });

    expect(state.attempts).toBe(0);
    // Establishing says nothing about the turn's phase, so an owned stream
    // stays owned.
    expect(phaseFor(state, TURN)).toBe('owned');
  });

  it('keeps spending the budget while every reconnect fails to establish', () => {
    // The other half of the pair above: without an established stream the
    // count still climbs, so the test above is evidence about establishing
    // and not about the counter having stopped working.
    let state = run(
      initialTurnStreamState(TURN),
      { type: 'stream-error', hasActiveTurn: true },
    );
    for (let i = 0; i < 3; i += 1) {
      state = run(
        state,
        { type: 'resume-started', turnId: TURN },
        { type: 'stream-error', hasActiveTurn: true },
      );
    }
    expect(state.attempts).toBe(3);
  });

  it('never lets a long turn on a flaky link run out of reconnects', () => {
    // One disconnect per reconnect, more of them than the budget holds.
    // Every replacement stream established, so none of them is owed back.
    let state = run(
      initialTurnStreamState(TURN),
      { type: 'stream-error', hasActiveTurn: true },
    );
    for (let i = 0; i < DROPPED_RESUME_ATTEMPTS + 5; i += 1) {
      expect(decideAutoResume(state, ctx()).kind).toBe('resume');
      state = run(
        state,
        { type: 'resume-started', turnId: TURN },
        { type: 'stream-established' },
        { type: 'stream-error', hasActiveTurn: true },
      );
    }
    expect(decideAutoResume(state, ctx()).kind).toBe('resume');
  });
});

describe('turnStream — cursor and lifecycle bookkeeping', () => {
  it('advances the cursor monotonically across the whole Session', () => {
    const state = run(
      initialTurnStreamState(TURN),
      { type: 'cursor-advanced', turnId: TURN, frameSeq: 14 },
      { type: 'cursor-advanced', turnId: TURN, frameSeq: 9 },
    );
    expect(state.cursor).toEqual({ turnId: TURN, frameSeq: 14 });
    expect(run(state, { type: 'cursor-advanced', turnId: OTHER, frameSeq: 2 }).cursor)
      .toEqual({ turnId: TURN, frameSeq: 14 });
  });

  it('ignores a cursor that carries no usable sequence', () => {
    const state = run(
      initialTurnStreamState(TURN),
      { type: 'cursor-advanced', turnId: TURN, frameSeq: 5 },
      { type: 'cursor-advanced', turnId: TURN, frameSeq: -1 },
      { type: 'cursor-advanced', turnId: TURN, frameSeq: Number.NaN },
    );
    expect(state.cursor.frameSeq).toBe(5);
  });

  it('going idle releases ownership but keeps a finished turn finished', () => {
    const owned = run(
      initialTurnStreamState(TURN),
      { type: 'send-started' },
      { type: 'turn-accepted', turnId: TURN },
      { type: 'went-idle' },
    );
    expect(phaseFor(owned, TURN)).toBe('none');

    const finished = run(
      initialTurnStreamState(TURN),
      { type: 'stream-finished', outcome: 'clean', turnId: TURN },
      { type: 'went-idle' },
    );
    expect(phaseFor(finished, TURN)).toBe('finished');
  });

  it('a confirmed settle clears the turn outright', () => {
    const state = run(
      initialTurnStreamState(TURN),
      { type: 'stream-finished', outcome: 'clean', turnId: TURN },
      { type: 'turn-settled' },
    );
    expect(phaseFor(state, TURN)).toBe('none');
  });

  it('an abort or error finish is not evidence the turn ended or broke', () => {
    for (const outcome of ['aborted', 'error'] as const) {
      const state = run(
        initialTurnStreamState(TURN),
        { type: 'stream-finished', outcome, turnId: TURN },
      );
      expect(phaseFor(state, TURN)).toBe('none');
    }
  });

  it('a session change carries nothing over but the cursor it was handed', () => {
    const dirty = run(
      initialTurnStreamState(TURN),
      { type: 'stream-error', hasActiveTurn: true },
      { type: 'resume-started', turnId: TURN },
    );
    const fresh = reduceTurnStream(dirty, {
      type: 'session-changed',
      serverTurnId: OTHER,
      cursor: { turnId: OTHER, frameSeq: 3 },
    });
    expect(fresh).toEqual({
      serverTurnId: OTHER,
      phase: 'none',
      phaseTurnId: null,
      cursor: { turnId: OTHER, frameSeq: 3 },
      attempts: 0,
    });
  });
});
