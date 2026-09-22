/**
 * E2E: an input the engine already accepted recovers as the same turn when the
 * host runtime is lost before the runner's receipt reaches it.
 *
 * The window is the platform's own receipt boundary on the runner wire. The
 * host's `RunnerLink.deliver()` sends the `input` frame and waits for the
 * runner's journaled `input_ack`; the runner sends that receipt only after
 * `RunnerSession.submit()` handed the input to the Claude SDK, and the platform
 * journals `input.delivered` only after the receipt arrived. A receipt held on
 * the wire therefore leaves the engine working on an input the host cannot
 * prove it delivered.
 *
 * The fixture relaunches the image-baked runner behind an in-box proxy that
 * forwards every frame verbatim and holds only the armed Session's first
 * `input_ack`. While it is held the spec proves the acceptance twice — on the
 * wire (command id and platform input id of the `input` frame) and natively
 * (the same prompt as one root `user` record in the platform-held SessionStore)
 * — and proves the platform holds no delivery receipt. It then evicts the host
 * runtime, releases the receipt, and requires the ORIGINAL turn to settle
 * COMPLETED with one accepted input, one native user record, one reply, no
 * failure, a clean original request, and a cold page that renders the same.
 * No second user submission is ever made.
 *
 * Whole-box placement: the runner process is replaced, so the conversation
 * must own its box (`createColdTestAgent`). Failed runs keep the Session, the
 * Agent, the gated box and its proxy log for diagnosis.
 */
import { randomUUID } from 'node:crypto';

import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import {
  documentsByField,
  framesForTurn,
  sessionEvents,
  snapshotDoc,
  waitForTurnTerminalProof,
} from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
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
// host gives up its receipt after two 10s windows, so the spec must evict and
// release well inside that; the proxy budget only bounds a spec that never
// reaches the release step.
const GATE_HOLD_BUDGET_SECONDS = 90;
// Bound on the original request after the durable recovery, as in the source
// spec: a request still open then is reported, not waited on.
const ORIGINAL_REQUEST_GRACE_MS = 30_000;

const RECEIPT_EVENT_TYPES = new Set(['input.delivered', 'input.consumed']);
const TURN_TERMINAL_EVENT_TYPES = new Set(['turn.completed', 'turn.recovered']);

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

interface CoupledTurnOutcome {
  status: number;
  frameTypes: string[];
  text: string;
  errorText: string | null;
  error: string;
}

interface CoupledTurn {
  clientMessageId: string;
  commandId: string;
  inputId: string;
  outcome: Promise<CoupledTurnOutcome>;
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`expected an object, received ${JSON.stringify(value)}`);
  }
  return value as Record<string, unknown>;
}

/** Root (non-subagent) native SessionStore rows the platform holds for the conversation. */
function nativeRootRows(id: string): NativeRootRow[] {
  return documentsByField('transcript_entries', '$.platform_session_id', id)
    .filter((row) => row.subpath == null)
    .map((row) => ({
      seq: Number(row.seq),
      uuid: String(row.uuid || ''),
      session_id: String(row.session_id || ''),
      entry_json: String(row.entry_json || ''),
      entry: object(JSON.parse(String(row.entry_json))),
    }))
    .sort((a, b) => a.seq - b.seq);
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

/** `input.delivered` / `input.consumed` journal rows for one command — the platform's own delivery receipts. */
function deliveryReceipts(id: string, commandId: string): Record<string, unknown>[] {
  return sessionEvents(id).filter(
    (event) => RECEIPT_EVENT_TYPES.has(String(event.event_type || ''))
      && String(event.causation_id || '') === commandId,
  );
}

/**
 * The original request, kept as the source spec kept it: one coupled
 * `POST ai-stream` whose body is read to the end. The client message id is
 * minted here so the wire and journal identities can be checked exactly: the
 * platform input id of a UUID client message id is that UUID, and the command
 * id is `<session>:<client message id>` (`turn_dispatch._input_id` /
 * `_input_command_id`).
 */
function startCoupledTurn(request: APIRequestContext, id: string, content: string): CoupledTurn {
  const clientMessageId = randomUUID();
  const outcome = request
    .fetch(apiPath(`/sessions/${id}/ai-stream`), {
      method: 'POST',
      data: { content, client_message_id: clientMessageId },
      headers: { Accept: 'text/event-stream' },
      timeout: SPEC_BUDGET_MS,
    })
    .then(async (response): Promise<CoupledTurnOutcome> => {
      const raw = await response.text();
      const result: CoupledTurnOutcome = {
        status: response.status(), frameTypes: [], text: '', errorText: null, error: '',
      };
      if (!response.ok()) {
        result.error = `ai-stream -> ${response.status()}: ${raw.slice(0, 500)}`;
        return result;
      }
      for (const rawLine of raw.split('\n')) {
        const line = rawLine.trim();
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (!payload || payload === '[DONE]') continue;
        let frame: Record<string, unknown>;
        try {
          frame = JSON.parse(payload) as Record<string, unknown>;
        } catch {
          continue;
        }
        const type = String(frame.type || '');
        result.frameTypes.push(type);
        if (type === 'text-delta' && typeof frame.delta === 'string') result.text += frame.delta;
        else if (type === 'error' && result.errorText === null) result.errorText = String(frame.errorText ?? 'unknown error');
      }
      return result;
    })
    .catch((error: unknown): CoupledTurnOutcome => ({
      status: 0, frameTypes: [], text: '', errorText: null, error: String((error as Error)?.message ?? error),
    }));
  return { clientMessageId, commandId: `${id}:${clientMessageId}`, inputId: clientMessageId, outcome };
}

async function waitForCurrentTurn(id: string, timeoutMs: number): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  let last = '';
  while (Date.now() < deadline) {
    last = String(snapshotDoc(id)?.current_turn_id ?? '').trim();
    if (last) return last;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`session ${id} snapshot never named a current turn within ${timeoutMs}ms`);
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
      snapshot: sessionId ? await observe(() => snapshotDoc(sessionId)) : null,
      events: sessionId ? await observe(() => sessionEvents(sessionId)) : null,
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
  // link to the terminated process. The next turn must rebuild its transport
  // through the gate so the test can control acknowledgement delivery.
  const installed = installInputAckGate(sandbox, { sessionId, maxWaitSeconds: GATE_HOLD_BUDGET_SECONDS });
  evidence.install = installed;
  test.info().annotations.push({ type: 'e2e_input_ack_gate_install', description: JSON.stringify(installed) });
  const evictedBeforeTurn = await api.adminEvictRuntime(sessionId);
  expect(evictedBeforeTurn.evicted, 'evict-runtime should report the evicted session').toBe(sessionId);
  const afterInstall = await api.getSession(sessionId);
  expect(String(afterInstall.sandbox_id || '').trim(), 'installing the gate must not move the conversation off its box').toBe(sandboxId);

  // The user's tab is open before the turn, as in the source spec; it is not
  // reloaded until the cold assertions at the end.
  await openSessionView(page, sessionId);

  // ── THE ORIGINAL REQUEST: exactly one submission for the whole spec. ──────
  const turn = startCoupledTurn(request, sessionId, prompt);
  evidence.commandId = turn.commandId;
  evidence.inputId = turn.inputId;

  // ── Upstream acceptance: the runner accepted this exact input and its
  //    receipt is now withheld on the wire. ───────────────────────────────────
  const entered = await waitForInputAckGateEntered(sandbox, sessionId, remaining(60_000));
  const held = entered.entered!;
  evidence.entered = held;
  test.info().annotations.push({ type: 'e2e_input_ack_gate_entered', description: JSON.stringify(held) });
  expect(held.command_id, 'the held receipt must belong to the original request command').toBe(turn.commandId);
  expect(held.input_id, 'the wire input must carry the platform input id of the original request').toBe(turn.inputId);
  expect(Number(held.sequence), 'the wire input must carry a positive FIFO sequence').toBeGreaterThan(0);
  expect(Number(held.ack_seq), 'the withheld receipt must be a journaled runner frame').toBeGreaterThan(0);
  expect(held.ack_duplicate, 'the withheld receipt must be the first acceptance, not a replayed duplicate').toBe(false);

  // Platform side of the same command while the receipt is withheld: accepted
  // once under one turn, and no delivery or consumption receipt anywhere.
  const turnId = await waitForCurrentTurn(sessionId, remaining(15_000));
  evidence.turnId = turnId;
  test.info().annotations.push(
    { type: 'e2e_session_id', description: sessionId },
    { type: 'e2e_turn_id', description: turnId },
    { type: 'e2e_command_id', description: turn.commandId },
  );
  const accepted = sessionEvents(sessionId).filter(
    (event) => String(event.event_type || '') === 'command.accepted' && String(event.causation_id || '') === turn.commandId,
  );
  expect(accepted, 'the original request must be accepted exactly once').toHaveLength(1);
  expect(String(accepted[0].turn_id || ''), 'the accepted command must belong to the current turn').toBe(turnId);
  const acceptedPayload = object(accepted[0].payload);
  expect(String(acceptedPayload.input_id || ''), 'the journal must record the same platform input id the wire carried').toBe(turn.inputId);
  expect(acceptedPayload.content, 'the journal must record the submitted prompt').toBe(prompt);
  expect(
    deliveryReceipts(sessionId, turn.commandId),
    'the platform must hold no delivery or consumption receipt while the runner receipt is withheld',
  ).toEqual([]);

  // ── Native acceptance: the same prompt is one root user record in the
  //    platform-held SessionStore before anything is evicted. The native uuid
  //    is the runner's per-attempt vendor identity, so identity here is the
  //    exact payload plus that uuid, retained below. ──────────────────────────
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
    deliveryReceipts(sessionId, turn.commandId),
    'the native record must not have been mistaken for a platform delivery receipt',
  ).toEqual([]);

  // ── FAULT: drop the host-side runtime handle while the receipt is withheld.
  //    The box, its runner, the SDK process and the native store are untouched. ─
  const evicted = await api.adminEvictRuntime(sessionId);
  expect(evicted.evicted, 'evict-runtime should report the evicted session').toBe(sessionId);
  const gateAtEvict = readInputAckGate(sandbox);
  expect(gateAtEvict.state, 'the receipt must still be withheld at the moment of eviction').toBe('entered');
  const detailAfterEvict = await api.adminSessionDetail(sessionId);
  expect(detailAfterEvict.has_local_runtime, 'eviction must drop the host runtime handle').toBe(false);
  expect(
    String((await api.getSession(sessionId)).sandbox_id || '').trim(),
    'eviction must not drop the session sandbox',
  ).toBe(sandboxId);
  test.info().annotations.push({
    type: 'injected_fault',
    description: `host runtime evicted while runner input_ack seq=${held.ack_seq} for ${turn.commandId} was withheld`,
  });

  // ── Release the original receipt. Whatever is still listening gets it now. ─
  const released = await releaseInputAckGate(sandbox);
  evidence.released = { holds: released.holds, connections: released.connections, released_at: released.released_at };
  expect(released.state).toBe('released');
  expect(Number(released.holds), 'the receipt must have been held at least once').toBeGreaterThanOrEqual(1);

  // ── The ORIGINAL turn recovers and completes. ─────────────────────────────
  const settledSnapshot = await waitForTurnTerminalProof(sessionId, turnId, 'COMPLETED', remaining(30_000));
  evidence.settledSnapshot = settledSnapshot;
  const settled = await api.waitForSession(
    sessionId,
    (session) => session.state === 'READY' && !String(session.current_turn_id || '').trim(),
    remaining(10_000),
  );
  expect(String(settled.last_turn_id || ''), 'the recovered turn must be the original turn').toBe(turnId);
  expect(String(settled.last_turn_status || ''), 'the original turn must complete').toBe('COMPLETED');
  expect(String(settled.sandbox_id || '').trim(), 'recovery must stay on the same sandbox').toBe(sandboxId);
  expect(Boolean(settled.runtime_unavailable), 'recovery must not mark the runtime unavailable').toBe(false);
  expect(String(settled.last_error || '').trim(), 'recovery must not leave a user-visible error').toBe('');
  expect(settled.delivery_failure ?? null, 'recovery must not report a delivery failure').toBeFalsy();

  // Journal: one acceptance, no failure verdict, a terminal for this turn, and
  // the FIFO row closed by its own receipts naming the same input.
  const events = sessionEvents(sessionId);
  expect(
    events.filter((event) => String(event.event_type || '') === 'command.accepted' && String(event.causation_id || '') === turn.commandId),
    'recovery must not accept the input a second time',
  ).toHaveLength(1);
  expect(
    events.filter((event) => String(event.turn_id || '') === turnId && String(event.event_type || '') === 'turn.failed'),
    'the original turn must never be settled as failed',
  ).toEqual([]);
  expect(
    events.filter((event) => String(event.turn_id || '') === turnId && TURN_TERMINAL_EVENT_TYPES.has(String(event.event_type || ''))).length,
    'the original turn must carry a durable completed terminal',
  ).toBeGreaterThanOrEqual(1);
  const receipts = deliveryReceipts(sessionId, turn.commandId);
  evidence.receipts = receipts;
  expect(receipts.length, 'the FIFO row must be closed by a delivery or consumption receipt').toBeGreaterThanOrEqual(1);
  for (const type of RECEIPT_EVENT_TYPES) {
    expect(
      receipts.filter((event) => String(event.event_type || '') === type).length,
      `${type} must be journaled at most once for the original command`,
    ).toBeLessThanOrEqual(1);
  }
  for (const receipt of receipts) {
    expect(String(object(receipt.payload).input_id || ''), 'every receipt must name the original input id').toBe(turn.inputId);
  }

  // Frames: the original turn ends in a finish, carries no error, and any
  // consumption marker it carries names the original input.
  const frames = framesForTurn(turnId);
  const frameTypes = frames.map((frame) => String(object(frame.payload).type || ''));
  evidence.frameTypes = frameTypes;
  expect(frameTypes, 'the original turn must not carry an error frame').not.toContain('error');
  expect(frameTypes, 'the original turn must carry a finish frame').toContain('finish');
  for (const frame of frames) {
    const payload = object(frame.payload);
    if (String(payload.type || '') !== 'data-input-consumed') continue;
    expect(String(object(payload.data).inputId || ''), 'a consumption marker on this turn must name the original input').toBe(turn.inputId);
  }

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

  // Public history: one user message, one reply on the original turn, no failure card.
  const history = await api.getMessages(sessionId, 100);
  expect(history.has_more).toBe(false);
  expect(
    history.messages.filter((message) => message.role === 'user').map(messageText),
    'durable history must hold exactly the one submitted input',
  ).toEqual([prompt]);
  const assistants = history.messages.filter((message) => message.role === 'assistant');
  expect(assistants, 'durable history must hold exactly one reply').toHaveLength(1);
  expect(String(assistants[0].turn_id || ''), 'the reply must belong to the original turn').toBe(turnId);
  expect(messageText(assistants[0]).trim(), 'the recovered reply must have content').not.toEqual('');
  const failureBlocks = [
    ...(assistants[0].blocks || []),
    ...(assistants[0].content_blocks || []),
    ...(assistants[0].parts || []),
  ].filter((block) => String(block.type || '') === 'turn_failure');
  expect(failureBlocks, 'the recovered reply must not carry a turn failure').toEqual([]);

  // The original request, as the source asserted it: it was never re-sent, it
  // ends without an error frame, and it received the reply's text-delta
  // before its terminal. Whether the community's coupled tail delivers the
  // recovered text to this response is a live question; the assertion is
  // kept at the source's strength until a live run answers it.
  const outcome = await Promise.race([
    turn.outcome,
    new Promise<CoupledTurnOutcome>((resolve) => {
      setTimeout(() => resolve({
        status: 0, frameTypes: [], text: '', errorText: null, error: 'original request still pending after recovery',
      }), ORIGINAL_REQUEST_GRACE_MS);
    }),
  ]);
  evidence.originalRequest = outcome;
  test.info().annotations.push({ type: 'e2e_original_request_outcome', description: JSON.stringify(outcome) });
  expect(outcome.error, `the original request must survive the eviction; outcome=${JSON.stringify(outcome)}`).toEqual('');
  expect(outcome.status, 'the original request must have been admitted').toBe(200);
  expect(outcome.errorText, `the original request must not receive an error frame; outcome=${JSON.stringify(outcome)}`).toBeNull();
  expect(
    outcome.frameTypes,
    `the original request must receive the reply text before its terminal; outcome=${JSON.stringify(outcome)}`,
  ).toContain('text-delta');
  expect(
    outcome.text.trim(),
    `the original request must carry non-empty reply text; outcome=${JSON.stringify(outcome)}`,
  ).not.toEqual('');

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
