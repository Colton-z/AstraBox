/**
 * E2E: an approval left waiting while nobody is connected is still answerable
 * long after the box would have given up on its own.
 *
 * The runner in the box holds a pending approval's answer slot only while its
 * host is connected: once the HOST has been absent longer than its budget
 * (`ASTRABOX_RUNNER_INTERACTION_WAIT_SECONDS`, 120s by default), it stops
 * waiting. The budget starts when the link drops and a reattach resets it. The
 * runner serves one host link, the session's reader, and a session needs a
 * reader for as long as its runtime lives: when a reader's link drops, the
 * reader host connects again (the reconnect loop of `ReaderHost._serve` in
 * `session_runtime/host.py`), and the periodic sweep starts a reader for any
 * session with a live runtime and no live reader lease. That reconnection,
 * made while nobody is looking, is what keeps a parked approval alive.
 *
 * WHY THIS SPENDS TWO MINUTES DOING NOTHING. A person's answer is stored on the
 * interaction and handed to the runtime by whichever reader holds the session,
 * so the answer request is accepted whether or not the box is still waiting.
 * What separates the two is the runtime taking the answer: a box that is still
 * waiting runs the tool and the interaction closes as answered; a box that
 * gave up does not, and the interaction closes as expired. A click that
 * arrives PROMPTLY reaches a waiting box whether or not anything was present
 * in between; only presence helps a click arriving LATE. So this spec waits
 * past the budget, touching nothing, and only then answers. The wait is the
 * assertion.
 *
 * The sibling spec `backend-restart-rehydrates-resident-session-and-pending-
 * approval` is the prompt-click half of this pair; both read success the same
 * way — accepted, closed as answered, and the same response recorded as ended
 * completed — and differ only in when the click lands.
 *
 * WHAT THIS DOES NOT DO, deliberately: it does not expire the reader's lease in
 * the database. That looks like an absent reader and is not one — the link is
 * still up, so the box's budget never starts and the scene cannot exercise
 * what it claims to. The link has to actually drop, which is what evicting the
 * runtime does: it disconnects the platform's client to the box.
 *
 * HOW TO PROVE THIS GATE CAN GO RED. Waiting longer cannot do it: while the
 * reader reconnects it resets the budget, so every duration passes and a green
 * run says nothing. The only discriminator is removing the reconnection: make
 * `ReaderHost._serve` (`session_runtime/host.py`) return when its reader's
 * stream ends instead of connecting again, and keep `ReaderHost._sweep_forever`
 * from starting a reader for the session — then redeploy and run this spec. It
 * must fail at the approval with the box having given up. A run that stays
 * green with the reconnection removed is a gate that proves nothing, and the
 * fault is in the gate, not the product.
 */
import { test, expect } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi, type PendingInteraction } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { parseTimeoutEnv } from '../fixtures/env';
import {
  sessionInteractionRows,
  sessionOutputRows,
  sessionResponseRows,
  type SessionResponseRow,
} from '../fixtures/dbOracle';

// Two minutes of deliberate waiting plus a provision, a gated response and its
// end — this spec is slow because the property is about elapsed time.
// The permission prompt is model-dependent; a bounded probe asks again rather
// than asserting on a model that chose not to call the tool.
/** What a user says when a model answers instead of acting. */
const INSIST_NUDGE =
  "You answered without using the tool. Do the tool call now, exactly as asked above, before replying again. \u8bf7\u73b0\u5728\u5c31\u6309\u4e0a\u9762\u7684\u8981\u6c42\u8c03\u7528\u5de5\u5177\uff0c\u4e0d\u8981\u53ea\u7528\u6587\u5b57\u56de\u7b54\u3002";

const PROMPT_MS = parseTimeoutEnv('ASTRABOX_E2E_PARKED_PROMPT_TIMEOUT_MS', 90_000);
// Longer than the runner's host-absence budget, so an unattended box would have
// stopped waiting by the time the answer arrives. Below that number this spec
// proves nothing the sibling does not already prove.
//
// The budget is the DEPLOYMENT's, not the image's: the runner takes
// `interaction_wait_s` per activation and the platform sends
// `ASTRABOX_RUNNER_INTERACTION_WAIT_SECONDS`. `run-playwright.sh` reads that
// value off the running server and passes this one as it plus ten seconds, so
// the two are one number and the lane cannot drift from what it is testing.
// The fallback is the product default plus that margin, for a run outside the
// lane. Against a 120s default this wait alone is most of the fixed 180s test
// budget, so the lane's shorter setting is what lets the spec fit.
const PAST_THE_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_PARKED_ABSENCE_MS', 130_000);
// Answering resumes the same model response, so its continuation gets the
// normal response budget rather than a shorter end-only cutoff.
const TURN_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
const TERMINAL_MS = parseTimeoutEnv('ASTRABOX_E2E_PARKED_TERMINAL_TIMEOUT_MS', TURN_BUDGET_MS);

/**
 * What the platform recorded about the paused response and the approval it
 * waits on: the response's status, the interaction's status, and which
 * response the interaction pauses.
 */
function pausedFacts(sessionId: string, responseId: string, interactionId: string): {
  response: string | null;
  interaction: string | null;
  pauses: string | null;
} {
  const response = sessionResponseRows(sessionId).find((row) => row.response_id === responseId);
  const interaction = sessionInteractionRows(sessionId).find((row) => row.interaction_id === interactionId);
  return {
    response: response?.status ?? null,
    interaction: interaction?.status ?? null,
    pauses: interaction?.response_id ?? null,
  };
}

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
  // platform. The probe returns the moment the response settles ungated,
  // so a declined ask costs that response rather than the whole budget.
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

  const parkedResponseId = String(pending?.response_id ?? '').trim();
  expect(parkedResponseId, 'the parked interaction must name the response it pauses').not.toEqual('');
  // Polled: the detail can name the interaction a moment before the reader's
  // batch carrying it is the one this read sees; the interaction and the
  // response's waiting status are written by the same statement batch.
  await expect.poll(
    () => pausedFacts(sessionId, parkedResponseId, interactionId),
    { timeout: 15_000, message: 'the session must genuinely be parked before the link is dropped' },
  ).toEqual({ response: 'waiting', interaction: 'open', pauses: parkedResponseId });

  // Drop the link for real. An expired reader lease in the database would not
  // do this: the box watches its host connection, not the database.
  await api.adminEvictRuntime(sessionId);

  // Now do nothing at all for longer than the box waits on an absent host.
  // Nothing here may touch the session: the test must not perform the presence
  // it is trying to observe.
  await new Promise((resolve) => setTimeout(resolve, PAST_THE_BUDGET_MS));

  // The answer arrives late. It is stored for the interaction, which the
  // platform accepts whatever the box did meanwhile; whether the decision
  // reached the tool call is read below, from how the interaction closes.
  const answered = await api.approvePendingInteraction(sessionId, { interactionId });
  expect(
    Boolean(answered.answered),
    `approving after ${Math.round(PAST_THE_BUDGET_MS / 1000)}s of nobody being connected was ` +
      'refused: the interaction no longer counted as open',
  ).toBe(true);
  expect(
    String(answered.interaction_id || ''),
    'the platform must answer the interaction that was pending, not a stale one',
  ).toBe(interactionId);

  // Accepted is not the same as effective. One answer is also not guaranteed
  // to be the last: the model may gate a further tool call on this same
  // response. Keep answering distinct follow-up gates inside the original
  // continuation budget, with a hard cap so a model loop cannot be laundered
  // into a pass.
  const answeredIds = new Set([interactionId]);
  const terminalDeadline = Date.now() + TERMINAL_MS;
  let parked: SessionResponseRow | undefined;
  while (Date.now() < terminalDeadline) {
    parked = sessionResponseRows(sessionId).find((row) => row.response_id === parkedResponseId);
    if (parked?.status === 'ended') break;

    const again = await api.getPendingInteraction(sessionId);
    const againId = String(again?.interaction_id ?? '').trim();
    if (againId && !answeredIds.has(againId)) {
      expect(
        String(again?.response_id ?? '').trim(),
        'a follow-up gate must belong to the same parked response the late answer resumed',
      ).toBe(parkedResponseId);
      expect(
        answeredIds.size,
        'the requested single-Write response should not need more than 4 gates — a longer chain is the model looping',
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

  // The response the late approval unblocked has to be recorded as ended
  // completed, or the decision was merely recorded and dropped.
  expect(
    parked ? `${parked.status}/${parked.outcome ?? ''}` : 'missing',
    `the parked response must end completed after the late approval; row=${JSON.stringify(parked)}`,
  ).toBe('ended/completed');
  expect(
    sessionOutputRows(sessionId, parkedResponseId).map(({ part }) => part).at(-1)?.type,
    'the parked response ends on its own recorded finish part',
  ).toBe('finish');
  // A box that gave up leaves the stored answer untaken: its interaction closes
  // as expired, not answered.
  expect(
    sessionInteractionRows(sessionId)
      .filter((row) => answeredIds.has(row.interaction_id))
      .map((row) => ({ interaction_id: row.interaction_id, status: row.status }))
      .sort((left, right) => left.interaction_id.localeCompare(right.interaction_id)),
    'every late answer must have been taken by the waiting runtime, closing its approval as answered',
  ).toEqual(
    [...answeredIds]
      .map((id) => ({ interaction_id: id, status: 'answered' }))
      .sort((left, right) => left.interaction_id.localeCompare(right.interaction_id)),
  );
  expect(
    await api.downloadFileText(sessionId, 'notes.txt', Math.max(1_000, terminalDeadline - Date.now())),
    'the late approval must let the requested Write create notes.txt containing PARKED',
  ).toContain('PARKED');
});
