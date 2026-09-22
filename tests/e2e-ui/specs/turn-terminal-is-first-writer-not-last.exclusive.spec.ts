/**
 * E2E: a turn killed mid-stream stays failed — the engine's exit does not
 * overwrite the terminal that is already written.
 *
 * Two writers reach a turn's terminal. The failure is recorded when the box
 * dies under a running turn; the engine stream then reports READY on its way
 * out whether the turn succeeded or failed, and a READY branch that appends
 * `finish` on top turns a dead turn into a completed one. Measured after an
 * out-of-band sandbox death: the session read as settled and the reply simply
 * never existed. The rule is that the FIRST terminal wins and later ones are
 * refused, so this pins the observable half of it — no assertion here knows
 * how many writers there are or which module holds the rule.
 *
 * This is deliberately mid-turn. `sandbox-oob-death-reborrow` kills BETWEEN
 * turns and proves the next message re-borrows; nothing kills a turn while it
 * is streaming, which is the only way to make two writers race for one
 * terminal.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { killSandbox, requireSandboxHandle } from '../fixtures/sandboxOps';
import { parseTimeoutEnv } from '../fixtures/env';

// Long enough that the turn is still streaming when the kill lands. The prompt
// asks for a sleep rather than a large answer because token rate is the
// model's business and this test needs a floor it controls.
const IN_TURN_SLEEP_S = 90;
// Give the kill a moment to land after the turn is observably running: killing
// before the box has begun the tool call would settle the turn by a different
// path and prove nothing about two writers.
const KILL_DELAY_MS = parseTimeoutEnv('ASTRABOX_E2E_TERMINAL_KILL_DELAY_MS', 15_000);
// The failure has to travel from the dead box to a durable terminal.
const SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_TERMINAL_SETTLE_TIMEOUT_MS', 240_000);

let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('a turn killed mid-stream does not settle as completed', async ({ request }) => {
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(
    `__e2e_terminal_first_writer_${Date.now()}_${test.info().workerIndex}`,
  );
  agentId = agent.agent_id;
  const created = await api.startConversation(agentId);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  const ready = await api.waitForSessionReady(sessionId);
  const sandboxId = String(ready.sandbox_id || '').trim();
  expect(sandboxId, 'conversation should have a sandbox_id when READY').not.toEqual('');
  const sandboxHandle = await requireSandboxHandle(api, sandboxId);

  // Start the turn and leave it running. Its stream is expected to break when
  // the box dies, so its rejection is the fault landing, not a test failure.
  const streaming = api
    .streamPrompt(sessionId, `Run \`sleep ${IN_TURN_SLEEP_S}\` with the Bash tool, then say DONE.`)
    .catch(() => undefined);

  await new Promise((resolve) => setTimeout(resolve, KILL_DELAY_MS));
  killSandbox(sandboxHandle);
  test.info().annotations.push({ type: 'e2e_killed_sandbox_id', description: sandboxId });
  await streaming;

  const settled = await api.waitForSessionState(sessionId, 'READY', SETTLE_MS);
  const status = String(settled.last_turn_status || '').trim().toUpperCase();

  expect(
    status,
    'the turn died with its box and produced no answer, so its durable status must ' +
      'not read as completed — a later READY overwrote the failure that was ' +
      'already recorded',
  ).not.toEqual('COMPLETED');
  expect(
    status,
    'the turn should carry a terminal status of its own rather than none at all',
  ).not.toEqual('');
});
