/**
 * E2E: a turn that ends in a legitimate error is recorded as one, and is not
 * also complained about for lacking a completion it never claimed.
 *
 * Whether a turn failed was derivable four ways — the stream's trailing
 * status, the live result's error flag, an error event, and the durable
 * terminal frame — and different branches consulted different subsets. One
 * disagreement recorded a failed turn as COMPLETED; the opposite one raised
 * `missing durable finish frame proof for completed turn` against a turn that
 * had ended in a perfectly good error, because the failure flag only ever read
 * the live result.
 *
 * The first of those is pinned by `turn-terminal-is-first-writer-not-last`,
 * which requires a killed turn not to read COMPLETED. This is its mirror: the
 * same killed turn must not acquire a second, invented complaint on the way to
 * being recorded correctly. Two specs, one direction each, because a single
 * assertion that tried to hold both would pass whenever the turn was simply
 * left alone.
 *
 * The absence assertion is paired with two positives, because an absence is
 * satisfied by an empty record and by a dead matcher just as readily as by
 * correct behaviour: the turn's status is required to be a real terminal, and
 * the matcher is required to match the text it is looking for.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { killSandbox, requireSandboxHandle } from '../fixtures/sandboxOps';
import { parseTimeoutEnv } from '../fixtures/env';

const IN_TURN_SLEEP_S = 90;
const KILL_DELAY_MS = parseTimeoutEnv('ASTRABOX_E2E_ERROR_PROOF_KILL_DELAY_MS', 15_000);
const SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_ERROR_PROOF_SETTLE_TIMEOUT_MS', 240_000);

/** The complaint a turn must not attract for ending in an error it declared. */
const SPURIOUS_PROOF_COMPLAINT = /missing durable finish frame proof/i;

let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('a turn killed mid-stream is not also accused of missing its proof', async ({
  request,
}) => {

  // The matcher is alive: it matches the text it exists to find. Without this,
  // a typo here would make the absence below true forever.
  expect(
    SPURIOUS_PROOF_COMPLAINT.test('missing durable finish frame proof for completed turn'),
    'the matcher must match the complaint it is looking for',
  ).toBe(true);

  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(
    `__e2e_error_proof_${Date.now()}_${test.info().workerIndex}`,
  );
  agentId = agent.agent_id;
  const created = await api.startConversation(agentId);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  const ready = await api.waitForSessionReady(sessionId);
  const sandboxId = String(ready.sandbox_id || '').trim();
  expect(sandboxId, 'conversation should have a sandbox_id when READY').not.toEqual('');
  const sandboxHandle = await requireSandboxHandle(api, sandboxId);

  const streaming = api
    .streamPrompt(sessionId, `Run \`sleep ${IN_TURN_SLEEP_S}\` with the Bash tool, then say DONE.`)
    .catch(() => undefined);

  await new Promise((resolve) => setTimeout(resolve, KILL_DELAY_MS));
  killSandbox(sandboxHandle);
  await streaming;

  const settled = await api.waitForSessionState(sessionId, 'READY', SETTLE_MS);

  // Positive: the record is populated, so the absence below is a statement
  // about its contents rather than about an empty document.
  const status = String(settled.last_turn_status || '').trim();
  expect(
    status,
    'the killed turn must carry a terminal status, or there is no record to check',
  ).not.toEqual('');

  const errorText = String(settled.last_error || '');
  expect(
    SPURIOUS_PROOF_COMPLAINT.test(errorText),
    `the turn ended in an error it declared (status ${status}) and was ALSO ` +
      `accused of missing a completion proof: ${errorText.slice(0, 300)}. ` +
      'The outcome and the proof check are reading different books again',
  ).toBe(false);
});
