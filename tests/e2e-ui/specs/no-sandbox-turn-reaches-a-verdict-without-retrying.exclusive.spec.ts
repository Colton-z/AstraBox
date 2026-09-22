/**
 * E2E: a row the reconciler selects but can never recover reaches a verdict
 * once and is then left alone.
 *
 * The scan selects on conversation_state and a stale heartbeat; recovery needs
 * a turn to recover. Those are different questions, and a row where they
 * disagree is selected on every tick for ever. A missing sandbox — what this
 * scene injects, and what the file is named after — is one instance of that
 * gap; a conversation in an active state naming no current turn is another.
 * The property under test is the gap, not the instance.
 *
 * The cost is 5768 attempts over five hours against a session that can never
 * make progress, every one of them logged as a successful reconciliation.
 *
 * So the assertion is about COUNT, not state. A state check passes the moment
 * the phase clears and cannot tell "settled once" from "settling again every
 * few seconds" — which is exactly how the defect stayed invisible while it was
 * running. This samples the journal after the verdict and requires it to stop
 * growing.
 *
 * The fault is injected through the document store: the session keeps its rows
 * and loses its sandbox pointer, which is the shape a reclaimed box leaves
 * behind.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { parseTimeoutEnv } from '../fixtures/env';
import {
  sessionEvents,
  patchSessionDoc,
  patchSnapshotDoc,
  restoreDoc,
  snapshotDoc,
} from '../fixtures/dbOracle';

// The reconciler must reach a verdict inside this window.
const VERDICT_MS = parseTimeoutEnv('ASTRABOX_E2E_NO_SANDBOX_VERDICT_TIMEOUT_MS', 180_000);
// Long enough to span several reconcile passes. If work is still happening,
// this is where it shows: the count moves.
const QUIET_OBSERVATION_MS = parseTimeoutEnv('ASTRABOX_E2E_NO_SANDBOX_QUIET_MS', 60_000);
// A heartbeat old enough that the turn is reclaimable rather than live.
const STALE_HEARTBEAT_AT = '2020-01-01T00:00:00+00:00';

// The fault is restored only on a pass; a failure keeps the scene, and the
// report tail names the session so the oracle DB still holds the evidence.
let sessionId = '';
let sessionBefore: Record<string, unknown>[] = [];
let snapshotBefore: Record<string, unknown>[] = [];
onPassOnly(async () => {
  if (sessionBefore[0]) restoreDoc('sessions', { '$.session_id': sessionId }, sessionBefore[0]);
  if (snapshotBefore[0]) restoreDoc('snapshots', { '$.session_id': sessionId }, snapshotBefore[0]);
});
const sessions = trackSessions();

test('a selected-but-unrecoverable turn settles once and is not retried', async ({ request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  sessionId = created.session_id;
  sessions.push(sessionId);

  await api.waitForSessionReady(sessionId);
  await api.streamPrompt(sessionId, 'Say BASELINE and nothing else.');
  const settled = await api.waitForSessionState(sessionId, 'READY', VERDICT_MS);
  const turnId = String(settled.last_turn_id || '').trim();
  expect(turnId, 'the baseline turn must have an id to park').not.toEqual('');

  // The session loses its sandbox and its last turn is parked unresolved:
  // the state a reclaimed box leaves behind, with no path back to compute.
  sessionBefore = patchSessionDoc(sessionId, { sandbox_id: null });
  // `conversation_state` is what the reconcile scan selects on, and it is a
  // different slot from `last_turn_status`: the stale-worker arm asks for a
  // conversation that is PROCESSING/STREAMING/INTERRUPTING with an old
  // heartbeat. A row that carries the turn status alone is not a row the scan
  // can see, so the fault it describes is one nothing was ever going to answer.
  snapshotBefore = patchSnapshotDoc(sessionId, {
    sandbox_id: null,
    conversation_state: 'PROCESSING',
    last_turn_status: 'PROCESSING',
    turn_recovery_phase: 'AWAITING_RUNTIME',
    last_turn_terminal_frame: null,
    worker_heartbeat_at: STALE_HEARTBEAT_AT,
  });
  expect(sessionBefore.length, 'the fault must find the session row').toBeGreaterThan(0);
  expect(snapshotBefore.length, 'the fault must find the snapshot row').toBeGreaterThan(0);
  expect(
    snapshotDoc(sessionId)?.sandbox_id ?? null,
    'the fault must actually remove the sandbox pointer',
  ).toBeNull();

  // 1. A verdict is reached — the turn does not sit in its recovery phase.
  await expect
    .poll(() => String(snapshotDoc(sessionId)?.turn_recovery_phase ?? ''), {
      timeout: VERDICT_MS,
      message:
        'a row the scan keeps selecting and recovery can never advance must ' +
        'reach a verdict, not wait for one',
    })
    .not.toBe('AWAITING_RUNTIME');

  // 2. And then nothing keeps working on it. A reconciler with no destination
  //    for a row it cannot advance retries it, logs success, and retries
  //    again; only the count separates that from settling once.
  const afterVerdict = sessionEvents(sessionId).length;
  await new Promise((resolve) => setTimeout(resolve, QUIET_OBSERVATION_MS));
  const afterQuiet = sessionEvents(sessionId).length;

  expect(
    afterQuiet,
    `the journal grew from ${afterVerdict} to ${afterQuiet} entries in the ` +
      `${Math.round(QUIET_OBSERVATION_MS / 1000)}s after the verdict — the turn is ` +
      'being reconciled repeatedly, which is what "no sandbox is transient" looks like',
  ).toEqual(afterVerdict);
});
