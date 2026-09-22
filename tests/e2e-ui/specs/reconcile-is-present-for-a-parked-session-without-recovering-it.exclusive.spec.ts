/**
 * E2E: an approval left waiting while nobody is connected is still answerable
 * long after the box would have given up on its own.
 *
 * A parked session's box blocks its PreToolUse hook holding the answer slot and
 * stops waiting once the HOST has been absent longer than its budget
 * (`DEFAULT_INTERACTION_WAIT_S = 120s`). The budget starts when the link drops
 * and a reattach resets it, so what keeps a parked approval alive is something
 * reconnecting while nobody is looking — the reconciler being present for a
 * session it must never recover.
 *
 * WHY THIS SPENDS TWO MINUTES DOING NOTHING. Two separate mechanisms make an
 * approval answerable after the platform loses its runtime, and they are
 * distinguishable only in time:
 *
 *   - the answer path attaches before it refuses, so a click that arrives
 *     PROMPTLY succeeds whether or not anything was present in between;
 *   - the reconciler's presence keeps the box from giving up at all, which is
 *     the only thing that helps a click arriving LATE.
 *
 * A gate that answers promptly passes on the first mechanism alone and says
 * nothing about the second. So this one waits past the budget, touching
 * nothing, and only then answers. The wait is the assertion.
 *
 * The sibling spec `backend-restart-rehydrates-resident-session-and-pending-
 * approval` is the prompt-click half of this pair; both read success the same
 * way — accepted, answered, and a durable terminal on the same turn — and
 * differ only in when the click lands.
 *
 * WHAT THIS DOES NOT DO, deliberately: it does not write a stale heartbeat into
 * the store. That looks like a dead worker and is not one — the link is still
 * up, so the box's budget never starts and the scene cannot exercise what it
 * claims to. The link has to actually drop, which is what evicting the runtime
 * does.
 *
 * HOW TO PROVE THIS GATE CAN GO RED. Waiting longer cannot do it: while the
 * presence arm works it reconnects and resets the budget, so every duration
 * passes and a green run says nothing. The only discriminator is removing the
 * arm. In `session_kernel/service_mixins/bootstrap.py`, construct the
 * ReconcileWorker with `attach_parked_runtime_fn=None` — the parked branch
 * already guards on that being set, so one word disables presence and nothing
 * else — then redeploy and run this spec. It must fail at the approval with
 * the box having given up. A run that stays green with the arm removed is a
 * gate that proves nothing, and the fault is in the gate, not the product.
 */
import { test, expect } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi, type PendingInteraction } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { parseTimeoutEnv } from '../fixtures/env';
import { snapshotDoc, waitForTurnTerminalProof } from '../fixtures/dbOracle';

// Two minutes of deliberate waiting plus a provision, a gated turn and a
// terminal — this spec is slow because the property is about elapsed time.
// The permission prompt is model-dependent; a bounded probe skips rather than
// asserting on a model that chose not to call the tool.
/** What a user says when a model answers instead of acting. */
const INSIST_NUDGE =
  "You answered without using the tool. Do the tool call now, exactly as asked above, before replying again. \u8bf7\u73b0\u5728\u5c31\u6309\u4e0a\u9762\u7684\u8981\u6c42\u8c03\u7528\u5de5\u5177\uff0c\u4e0d\u8981\u53ea\u7528\u6587\u5b57\u56de\u7b54\u3002";

const PROMPT_MS = parseTimeoutEnv('ASTRABOX_E2E_PARKED_PROMPT_TIMEOUT_MS', 90_000);
// Longer than the runner's host-absence budget, so an unattended box would have
// stopped waiting by the time the answer arrives. Below that number this spec
// proves nothing the sibling does not already prove.
//
// The budget is the DEPLOYMENT's, not the image's: the runner has always taken
// `interaction_wait_s` per activation and the platform now sends
// `ASTRABOX_RUNNER_INTERACTION_WAIT_SECONDS`. `run-playwright.sh` reads that
// value off the running server and passes this one as it plus ten seconds, so
// the two are one number and the lane cannot drift from what it is testing.
// The fallback is the product default plus that margin, for a run outside the
// lane. Against a 120s default this wait alone was 72% of the fixed 180s test
// budget, and the spec finished at 183s -- over the wall it had passed under
// before, having proved nothing extra for the extra seconds.
const PAST_THE_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_PARKED_ABSENCE_MS', 130_000);
// Answering resumes the same model turn, so its continuation gets the normal
// turn budget rather than a shorter terminal-only cutoff.
const TURN_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
const TERMINAL_MS = parseTimeoutEnv('ASTRABOX_E2E_PARKED_TERMINAL_TIMEOUT_MS', TURN_BUDGET_MS);

// No store fault is injected, so there is nothing to restore afterwards.
let sessionId = '';
const sessions = trackSessions();

test('an approval nobody attended to for longer than the box waits is still answerable', async ({
  request,
}) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  sessionId = created.session_id;
  sessions.push(sessionId);

  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'default');
  await api
    .streamPrompt(sessionId, 'Use the Write tool to create notes.txt containing PARKED.')
    .catch(() => undefined);

  // Ask again rather than skip: a skip ends the round exactly as a
  // failure does, so the model's choice decided it instead of the
  // platform. The probe returns the moment the turn settles ungated,
  // so a declined ask costs that turn rather than the whole budget.
  const pending = await insist<PendingInteraction>({
    ask: async (attempt) => {
      if (attempt > 1) await api.postTurnInput(sessionId, INSIST_NUDGE);
    },
    probe: () =>
      api.waitForPendingInteractionOrSettledTurn(sessionId, PROMPT_MS),
    what: 'the model did not call a gated tool, so no session was parked',
    budgetMs: PROMPT_MS * 2,
    probeMs: PROMPT_MS,
  });
  const interactionId = String(pending?.interaction_id || '').trim();
  expect(interactionId, 'the parked session must name its interaction').not.toEqual('');

  const parkedTurnId = String(snapshotDoc(sessionId)?.current_turn_id ?? '').trim();
  expect(parkedTurnId, 'the parked turn must have an id to prove a terminal on').not.toEqual('');
  expect(
    String(snapshotDoc(sessionId)?.conversation_state ?? ''),
    'the session must genuinely be parked before the link is dropped',
  ).toBe('WAITING_FOR_INTERACTION');

  // Drop the link for real. A stale heartbeat in the store would not do this:
  // the box watches its host connection, not the database.
  await api.adminEvictRuntime(sessionId);

  // Now do nothing at all for longer than the box waits on an absent host.
  // Nothing here may touch the session — reading it through an endpoint that
  // attaches would be the test performing the presence it is trying to observe.
  await new Promise((resolve) => setTimeout(resolve, PAST_THE_BUDGET_MS));

  // The answer arrives late. It must still be accepted, and the decision must
  // actually reach the tool call rather than being accepted into nothing.
  const answered = await api.approvePendingInteraction(sessionId, { interactionId });
  expect(
    Boolean(answered.answered),
    `approving after ${Math.round(PAST_THE_BUDGET_MS / 1000)}s of nobody being connected was ` +
      'refused: the box gave up on a wait nobody abandoned, which is what presence prevents',
  ).toBe(true);
  expect(
    String(answered.interaction_id || ''),
    'the platform must answer the interaction that was pending, not a stale one',
  ).toBe(interactionId);

  // Accepted is not the same as effective. One answer is also not guaranteed
  // to be the last: the model may gate a further tool call on this same turn.
  // Keep answering distinct follow-up gates inside the original continuation
  // budget, with a hard cap so a model loop cannot be laundered into a pass.
  const answeredIds = new Set([interactionId]);
  const terminalDeadline = Date.now() + TERMINAL_MS;
  while (Date.now() < terminalDeadline) {
    const currentSnapshot = snapshotDoc(sessionId);
    const terminalFrame = currentSnapshot?.last_turn_terminal_frame;
    if (
      terminalFrame
      && typeof terminalFrame === 'object'
      && String((terminalFrame as Record<string, unknown>).turn_id ?? '') === parkedTurnId
    ) {
      break;
    }

    const again = await api.getPendingInteraction(sessionId);
    const againId = String(again?.interaction_id ?? '').trim();
    if (againId && !answeredIds.has(againId)) {
      expect(
        String(currentSnapshot?.current_turn_id ?? '').trim(),
        'a follow-up gate must belong to the same parked turn the late answer resumed',
      ).toBe(parkedTurnId);
      expect(
        answeredIds.size,
        'the requested single-Write turn should not need more than 4 gates — a longer chain is the model looping',
      ).toBeLessThan(4);
      const furtherAnswer = await api.approvePendingInteraction(sessionId, {
        interactionId: againId,
      });
      expect(Boolean(furtherAnswer.answered), `follow-up gate ${againId} was not answered`).toBe(true);
      expect(String(furtherAnswer.interaction_id || '')).toBe(againId);
      answeredIds.add(againId);
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }

  // The turn the late approval unblocked has to reach a durable terminal, or
  // the decision was merely recorded and dropped. Use only the budget left by
  // the bounded answering loop above.
  await waitForTurnTerminalProof(
    sessionId,
    parkedTurnId,
    'COMPLETED',
    Math.max(1_000, terminalDeadline - Date.now()),
  );
  expect(
    await api.downloadFileText(sessionId, 'notes.txt', Math.max(1_000, terminalDeadline - Date.now())),
    'the late approval must let the requested Write create notes.txt containing PARKED',
  ).toContain('PARKED');
});
