/**
 * E2E: an input the engine already accepted is answered once when the host
 * loses its runtime connection before the runner's receipt reaches it.
 *
 * The window is the runner wire's receipt boundary. The session's reader hands
 * an input to the runtime (`ClaudeRuntimeClient.submit` → `RunnerLink.deliver`),
 * which sends the `input` frame and waits for the runner's journaled
 * `input_ack`; the runner sends that receipt only after it handed the input to
 * the Claude SDK, and the platform marks the input delivered
 * (`session_inputs.state`) only after the receipt arrived. A receipt held on
 * the wire therefore leaves the engine working on an input the platform cannot
 * prove it delivered. Handing the same input over again is safe only because
 * the runtime takes inputs idempotently by input id — that is what this spec
 * holds the platform to.
 *
 * The fixture relaunches the image-baked runner behind an in-box proxy that
 * forwards every frame verbatim and holds only the armed session's first
 * `input_ack`. While it is held the spec proves the acceptance twice — on the
 * wire (the `input` frame carries the platform input id as its command id and
 * as the SDK message uuid) and natively (the same prompt as one root `user`
 * record in the platform-held SessionStore) — and proves the platform still
 * holds the input undelivered. It then evicts the host runtime, releases the
 * receipt, and requires ONE response to answer the input and complete, with
 * one native user record, one reply, no failure, a stream that delivered that
 * reply, and a cold page that renders the same. No second user submission is
 * ever made.
 *
 * Whole-box placement: the runner process is replaced, so the conversation
 * must own its box (`createColdTestAgent`). Failed runs keep the Session, the
 * Agent, the gated box and its proxy log for diagnosis.
 */
import { expect, test } from '@playwright/test';

import { AstraApi, messageText, sseFrames, type InputReceipt } from '../fixtures/astraApi';
import {
  postgresRows,
  sessionInputRows,
  sessionOutputRows,
  sessionResponseRows,
  sqlLiteral,
} from '../fixtures/dbOracle';
import {
  inputAckGateLogTail,
  installInputAckGate,
  readInputAckGate,
  releaseInputAckGate,
  waitForInputAckGateEntered,
} from '../fixtures/inputAckGate';
import { requireSandboxHandle, type SandboxHandle } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

// The lane kills a test at 180s. Waits below are cut from this smaller budget
// so the failure that surfaces is this spec's own message, not the watchdog's.
const SPEC_BUDGET_MS = 170_000;
// How long the proxy may hold the receipt before it reports `timed_out`. The
// runner link gives up on a receipt after two 10s windows, so the spec must
// evict and release well inside that; the proxy budget only bounds a spec that
// never reaches the release step.
const GATE_HOLD_BUDGET_SECONDS = 90;

let agentId = '';
let sessionId = '';
let sandbox: SandboxHandle | null = null;
const evidence: Record<string, unknown> = {};
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

interface NativeRootRow {
  seq: number;
  uuid: string;
  session_id: string;
  entry_json: string;
  entry: Record<string, unknown>;
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`expected an object, received ${JSON.stringify(value)}`);
  }
  return value as Record<string, unknown>;
}

/** Root (non-subagent) native SessionStore entries the platform holds for the conversation. */
function nativeRootRows(id: string): NativeRootRow[] {
  return postgresRows<{ seq: number; session_id: string; entry_json: string }>(
    `SELECT e.seq, s.native_session_id AS session_id, e.entry_json `
      + `FROM transcript_entries e JOIN transcript_scopes s ON s.scope_id = e.scope_id `
      + `WHERE s.platform_session_id = ${sqlLiteral(id)} AND s.subpath = '' ORDER BY e.seq`,
  ).map((row) => {
    const entry = object(JSON.parse(String(row.entry_json)));
    return {
      seq: Number(row.seq),
      uuid: String(entry.uuid || ''),
      session_id: String(row.session_id || ''),
      entry_json: String(row.entry_json || ''),
      entry,
    };
  });
}

/** The text content of a native `user`/`assistant` record, whichever shape the SDK wrote. */
function nativeRecordText(entry: Record<string, unknown>): string {
  const message = entry.message;
  if (!message || typeof message !== 'object') return '';
  const content = (message as Record<string, unknown>).content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .map((block) => (block && typeof block === 'object' && typeof (block as Record<string, unknown>).text === 'string'
      ? String((block as Record<string, unknown>).text)
      : ''))
    .join('');
}

function nativeUserRowsWithPrompt(rows: NativeRootRow[], prompt: string): NativeRootRow[] {
  return rows.filter((row) => row.entry.type === 'user' && nativeRecordText(row.entry) === prompt);
}

async function waitForNativeUserRow(id: string, prompt: string, timeoutMs: number): Promise<NativeRootRow> {
  const deadline = Date.now() + timeoutMs;
  let matches: NativeRootRow[] = [];
  while (Date.now() < deadline) {
    matches = nativeUserRowsWithPrompt(nativeRootRows(id), prompt);
    if (matches.length > 0) break;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  expect(matches, 'the accepted input must be exactly one native root user record while its receipt is withheld')
    .toHaveLength(1);
  return matches[0];
}

async function observe(read: () => unknown | Promise<unknown>): Promise<unknown> {
  try { return await read(); }
  catch (error) { return { unavailable: String(error) }; }
}

test.afterEach(async ({ request }, info) => {
  if (!['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  const api = new AstraApi(request);
  const box = sandbox;
  await info.attach('accepted-input-runtime-loss-scene', {
    body: JSON.stringify({
      sessionId,
      agentId,
      ...evidence,
      gate: box ? await observe(() => readInputAckGate(box)) : null,
      gateLog: box ? await observe(() => inputAckGateLogTail(box, 120)) : null,
      session: sessionId ? await observe(() => api.getSession(sessionId)) : null,
      adminDetail: sessionId ? await observe(() => api.adminSessionDetail(sessionId)) : null,
      history: sessionId ? await observe(() => api.getMessages(sessionId, 100)) : null,
      inputs: sessionId ? await observe(() => sessionInputRows(sessionId)) : null,
      responses: sessionId ? await observe(() => sessionResponseRows(sessionId)) : null,
      native: sessionId ? await observe(() => nativeRootRows(sessionId)) : null,
    }),
    contentType: 'application/json',
  });
});

test('an input the engine accepted before its receipt reached the host recovers as the same turn after the host runtime is evicted', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const startedAt = Date.now();
  const remaining = (floorMs: number): number => Math.max(floorMs, SPEC_BUDGET_MS - (Date.now() - startedAt));
  const runId = `${Date.now()}-${test.info().workerIndex}`;
  const prompt = `E2E accepted-input runtime loss ${runId}: 不要使用工具。请简短回复一句话。`;

  // ── Conversation tenancy: the fault replaces the box's runner process, so
  //    this conversation must be the box's sole occupant. ────────────────────
  const agent = await api.createColdTestAgent(`__e2e_accepted_input_loss_${runId}`);
  agentId = agent.agent_id;
  const created = await api.startConversation(agentId);
  sessionId = created.session_id;
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId);
  const sandboxId = String(ready.sandbox_id || '').trim();
  expect(sandboxId, 'the conversation must own a sandbox before the fault').not.toEqual('');
  const detailBefore = await api.adminSessionDetail(sessionId);
  const identity = detailBefore.runtime_identity;
  expect(identity && typeof identity === 'object', 'the fault needs the runtime identity as ownership evidence').toBeTruthy();
  expect(
    String(identity?.isolated_session_id || '').trim(),
    'this fault replaces the box runner, so the conversation must own the whole box',
  ).toEqual('');
  expect(String(identity?.sandbox_id || '').trim(), 'runtime_identity.sandbox_id must name the session sandbox').toBe(sandboxId);
  sandbox = await requireSandboxHandle(api, sandboxId);
  evidence.sandboxId = sandboxId;

  // Relaunch the runner behind the receipt gate, then evict the host's cached
  // link to the terminated process, so the session's reader connects again
  // through the gate.
  const installed = installInputAckGate(sandbox, { sessionId, maxWaitSeconds: GATE_HOLD_BUDGET_SECONDS });
  evidence.install = installed;
  test.info().annotations.push({ type: 'e2e_input_ack_gate_install', description: JSON.stringify(installed) });
  const evictedBeforeSend = await api.adminEvictRuntime(sessionId);
  expect(evictedBeforeSend.evicted, 'evict-runtime should report the evicted session').toBe(sessionId);
  const afterInstall = await api.getSession(sessionId);
  expect(String(afterInstall.sandbox_id || '').trim(), 'installing the gate must not move the conversation off its box').toBe(sandboxId);

  // The user's tab is open before the message is sent; it is not reloaded
  // until the cold assertions at the end.
  await openSessionView(page, sessionId);

  // ── THE ORIGINAL MESSAGE: exactly one submission for the whole spec. ──────
  // The reply is read off the session's stream from the position before the
  // send, so the read covers everything the response writes.
  const afterSeq = Number((await api.getSession(sessionId)).output_seq ?? 0);
  const receipt: InputReceipt = await api.postTurnInput(sessionId, prompt);
  evidence.inputId = receipt.input_id;
  expect(receipt.duplicate, 'the message must be a new input').toBe(false);
  const streamRead = api.readResponse(sessionId, afterSeq, receipt.input_id, startedAt + SPEC_BUDGET_MS)
    .then((read) => ({ ...read, error: '' }))
    .catch((error: unknown) => ({ frames: [] as Record<string, unknown>[], raw: '', error: String((error as Error)?.message ?? error) }));

  // ── Upstream acceptance: the runner accepted this exact input and its
  //    receipt is now withheld on the wire. ───────────────────────────────────
  const entered = await waitForInputAckGateEntered(sandbox, sessionId, remaining(60_000));
  const held = entered.entered!;
  evidence.entered = held;
  test.info().annotations.push({ type: 'e2e_input_ack_gate_entered', description: JSON.stringify(held) });
  expect(held.command_id, 'the held receipt must be for the platform input of the original message').toBe(receipt.input_id);
  expect(held.input_id, 'the SDK message must carry the platform input id as its uuid').toBe(receipt.input_id);
  expect(Number(held.sequence), 'the wire input must carry a positive delivery sequence').toBeGreaterThan(0);
  expect(Number(held.ack_seq), 'the withheld receipt must be a journaled runner frame').toBeGreaterThan(0);
  expect(held.ack_duplicate, 'the withheld receipt must be the first acceptance, not a replayed duplicate').toBe(false);

  // Platform side of the same input while the receipt is withheld: accepted
  // once, and not marked delivered.
  const inputsWhileHeld = sessionInputRows(sessionId);
  expect(inputsWhileHeld.map((row) => row.input_id), 'the message must be accepted exactly once').toEqual([receipt.input_id]);
  expect(inputsWhileHeld[0].content, 'the platform must record the submitted prompt').toBe(prompt);
  expect(
    { state: inputsWhileHeld[0].state, delivered_at: inputsWhileHeld[0].delivered_at },
    'the platform must not have marked the input delivered while the runner receipt is withheld',
  ).toEqual({ state: 'pending', delivered_at: null });

  // ── Native acceptance: the same prompt is one root user record in the
  //    platform-held SessionStore before anything is evicted. ────────────────
  const nativeUser = await waitForNativeUserRow(sessionId, prompt, remaining(30_000));
  evidence.nativeUser = { seq: nativeUser.seq, uuid: nativeUser.uuid, session_id: nativeUser.session_id };
  expect(nativeUser.uuid, 'the native user record must carry its native identity').not.toEqual('');
  expect(nativeUser.session_id, 'the native user record must be filed under a native session key').not.toEqual('');
  test.info().annotations.push({
    type: 'e2e_native_input',
    description: JSON.stringify({ seq: nativeUser.seq, uuid: nativeUser.uuid, native_session: nativeUser.session_id }),
  });
  const gateAtNative = readInputAckGate(sandbox);
  expect(gateAtNative.state, 'the receipt must still be withheld when the native record is proven').toBe('entered');
  expect(gateAtNative.released_at, 'nothing may have released the receipt yet').toBeUndefined();
  expect(
    sessionInputRows(sessionId)[0].state,
    'the native record must not have been mistaken for a delivery receipt',
  ).toBe('pending');

  // ── FAULT: drop the host-side runtime connection while the receipt is
  //    withheld. The box, its runner, the SDK process and the native store
  //    are untouched. ─────────────────────────────────────────────────────────
  const evicted = await api.adminEvictRuntime(sessionId);
  expect(evicted.evicted, 'evict-runtime should report the evicted session').toBe(sessionId);
  const gateAtEvict = readInputAckGate(sandbox);
  expect(gateAtEvict.state, 'the receipt must still be withheld at the moment of eviction').toBe('entered');
  expect(
    String((await api.getSession(sessionId)).sandbox_id || '').trim(),
    'eviction must not drop the session sandbox',
  ).toBe(sandboxId);
  test.info().annotations.push({
    type: 'injected_fault',
    description: `host runtime evicted while runner input_ack seq=${held.ack_seq} for input ${receipt.input_id} was withheld`,
  });

  // ── Release the original receipt. Whatever is still listening gets it now. ─
  const released = await releaseInputAckGate(sandbox);
  evidence.released = { holds: released.holds, connections: released.connections, released_at: released.released_at };
  expect(released.state).toBe('released');
  expect(Number(released.holds), 'the receipt must have been held at least once').toBeGreaterThanOrEqual(1);

  // ── ONE response answers the original input, and it completes. ───────────
  await expect
    .poll(() => {
      const rows = sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(receipt.input_id));
      return rows.map((row) => `${row.status}/${row.outcome ?? ''}`).join(',');
    }, { timeout: remaining(30_000), message: 'the response to the original input must complete' })
    .toBe('ended/completed');
  const answering = sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(receipt.input_id));
  evidence.responses = answering;
  expect(answering, 'exactly one response answers the original input').toHaveLength(1);
  const responseId = answering[0].response_id;
  expect(answering[0].origin, 'the response answers the input, it was not started by the engine').toBe('input');
  expect(answering[0].error, 'the response carries no failure').toBeNull();
  const settled = await api.waitForSessionReady(sessionId, remaining(10_000));
  expect(settled.last_response?.response_id, 'the last response is the one answering the input').toBe(responseId);
  expect(settled.last_response?.outcome, 'the original input must be answered').toBe('completed');
  expect(String(settled.sandbox_id || '').trim(), 'recovery must stay on the same sandbox').toBe(sandboxId);
  expect(Boolean(settled.runtime_unavailable), 'recovery must not mark the runtime unavailable').toBe(false);
  expect(String(settled.last_error || '').trim(), 'recovery must not leave a user-visible error').toBe('');

  // The input: accepted once, taken by the runtime, never refused.
  const inputsAfter = sessionInputRows(sessionId);
  evidence.inputs = inputsAfter;
  expect(inputsAfter.map((row) => row.input_id), 'recovery must not accept the input a second time').toEqual([receipt.input_id]);
  expect(['delivered', 'admitted'], 'the input must end with the runtime').toContain(inputsAfter[0].state);
  expect(inputsAfter[0].failure, 'the input must not be refused').toBeNull();

  // Output: the response ends in one finish and carries no error part.
  const outputTypes = sessionOutputRows(sessionId, responseId).map((row) => String(row.part.type ?? ''));
  evidence.outputTypes = outputTypes;
  expect(outputTypes, 'the response must not carry an error part').not.toContain('error');
  expect(outputTypes.filter((type) => type === 'finish'), 'the response must store exactly one finish').toHaveLength(1);

  // Native custody: the original user record survives by uuid and payload
  // exactly once, the prompt is one native user record in total, and a native
  // assistant record follows it under the same native session.
  const nativeAfter = nativeRootRows(sessionId);
  expect(
    nativeAfter.filter((row) => row.uuid === nativeUser.uuid && row.entry_json === nativeUser.entry_json),
    'the original native user record must survive exactly once with its payload',
  ).toHaveLength(1);
  expect(nativeUserRowsWithPrompt(nativeAfter, prompt), 'the prompt must be one native user record, never re-fed').toHaveLength(1);
  const nativeReplies = nativeAfter.filter(
    (row) => row.seq > nativeUser.seq && row.entry.type === 'assistant' && nativeRecordText(row.entry).trim() !== '',
  );
  expect(nativeReplies.length, 'a native assistant record must follow the accepted input').toBeGreaterThanOrEqual(1);
  for (const row of nativeAfter) {
    expect(row.session_id, 'every native record must belong to the same native session').toBe(nativeUser.session_id);
  }
  const detailAfter = await api.adminSessionDetail(sessionId);
  expect(
    String(detailAfter.engine_session_key || '').trim(),
    'the recovered conversation must persist the native session key its records are filed under',
  ).toBe(nativeUser.session_id);

  // Public history: one user message, one reply answering it, no failure.
  const history = await api.getMessages(sessionId, 100);
  expect(history.has_more).toBe(false);
  expect(
    history.messages.filter((message) => message.role === 'user').map(messageText),
    'history must hold exactly the one submitted input',
  ).toEqual([prompt]);
  const assistants = history.messages.filter((message) => message.role === 'assistant');
  expect(assistants, 'history must hold exactly one reply').toHaveLength(1);
  expect(assistants[0].id, 'the reply must be the response that answered the input').toBe(responseId);
  expect(assistants[0].metadata?.outcome, 'the reply must have completed').toBe('completed');
  expect(messageText(assistants[0]).trim(), 'the recovered reply must have content').not.toEqual('');

  // The session's stream, read from before the send: it carried this
  // response, with its text, to a `finish` and without an `error`.
  const streamed = await streamRead;
  evidence.stream = { error: streamed.error, types: streamed.frames.map((frame) => frame.type) };
  expect(streamed.error, `the stream must deliver the response to the original input; read=${JSON.stringify(evidence.stream)}`).toEqual('');
  const streamedTypes = streamed.frames.map((frame) => String(frame.type ?? ''));
  expect(streamedTypes, 'the stream must not deliver an error for this response').not.toContain('error');
  expect(streamedTypes, 'the stream must deliver the reply text').toContain('text-delta');
  expect(streamedTypes.at(-1), 'the stream must deliver the response to its finish').toBe('finish');
  expect(
    sseFrames(streamed.raw).filter((frame) => frame.type === 'start').map((frame) => frame.messageId),
    'the stream must deliver one response, the one that answered the input',
  ).toEqual([responseId]);

  // The gate is spent: released once, never timed out.
  const gateFinal = readInputAckGate(sandbox);
  evidence.gateFinal = gateFinal;
  expect(gateFinal.state, `input-ack gate=${JSON.stringify(gateFinal)}`).toBe('released');
  expect(gateFinal.timed_out_at).toBeUndefined();

  // ── Cold page: the same conversation renders one input, one reply, READY. ──
  await openSessionView(page, sessionId);
  await expect(page.getByTestId('user-message').filter({ hasText: prompt })).toHaveCount(1, { timeout: 45_000 });
  await expect(page.getByTestId('user-message')).toHaveCount(1);
  await expect(page.getByTestId('assistant-message')).toHaveCount(1, { timeout: 45_000 });
  await expect.poll(async () => (await page.getByTestId('assistant-message').last().innerText()).trim(), {
    timeout: 45_000,
    message: 'the recovered assistant card must render non-empty content',
  }).not.toEqual('');
  const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
  await expect(pill).toHaveAttribute('data-state', 'READY', { timeout: 30_000 });
  await expect(pill).toHaveAttribute('data-pulse', 'false', { timeout: 30_000 });
});
