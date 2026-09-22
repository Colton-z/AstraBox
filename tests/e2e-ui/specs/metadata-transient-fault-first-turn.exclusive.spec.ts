/**
 * E2E: the first real user turn survives a transient failure of its metadata
 * read and of its snapshot write, and the test proves each injected failure
 * was met and recovered by the product, with one input and no error.
 *
 * The faults are raised by PostgreSQL on the Session's own rows
 * (`fixtures/metadataTransientFault.ts`): the first read of this Session's
 * `sessions` document and the first update of its `session_snapshots` document
 * after arming fail with SQLSTATE 40001, the class the DAL's PostgreSQL
 * classifier retries. Nothing is armed until the Session is READY, and the
 * original request is the next Session API call the test sends; background
 * product readers can also meet the fault, and the retry log names the owner.
 *
 * Consumption evidence is exact and two-sided. The database side is a
 * sequence per fault kind: `raised` visits are the failures, and a visit past
 * them is the product reading or writing the same row again. The server side
 * is the DAL's own `transient persistence error op=… attempt=…` line carrying
 * the run marker for each raised fault, which names the repository operation
 * that met it; a marker line that is anything else means the error escaped the
 * retry funnel and fails the test. The turn itself must settle COMPLETED with
 * one accepted command, one native user record, one reply, no failure card, a
 * clean original request, and a cold page that renders the same.
 *
 * Placement: the armed window changes row visibility rules on the shared
 * document table for every connection, so this file belongs in the serial
 * tail of the exclusive lane. The fault targets one Session's rows only and
 * needs no box ownership, so the campaign Agent's conversation is used, as the
 * source did. The window is disarmed as soon as both faults have been raised
 * and re-visited, and again in `finally`; failed runs keep the Session.
 */
import { randomUUID } from 'node:crypto';

import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText, type MessageRecord } from '../fixtures/astraApi';
import {
  documentsByField,
  framesForTurn,
  sessionEvents,
  snapshotDoc,
  waitForTurnTerminalProof,
} from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import {
  armMetadataTransientFault,
  disarmMetadataTransientFault,
  readMetadataFaultCounters,
  readMetadataFaultLogEvidence,
  serverLogLinesSince,
  type MetadataFaultCounters,
  type MetadataFaultDisarmResult,
  type MetadataFaultHandle,
} from '../fixtures/metadataTransientFault';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

// The lane kills a test at 180s. Waits below are cut from this smaller budget
// so the failure that surfaces is this spec's own message, not the watchdog's.
const SPEC_BUDGET_MS = 170_000;
// Bound on the original request after the turn has settled: a request still
// open then is reported, not waited on.
const ORIGINAL_REQUEST_GRACE_MS = 30_000;
// One failure per path, as the source armed them: one metadata read, one
// snapshot write. Each must be raised once and then re-visited.
const READ_FAILURES = 1;
const WRITE_FAILURES = 1;

const TURN_TERMINAL_EVENT_TYPES = new Set(['turn.completed', 'turn.recovered']);

let sessionId = '';
let fault: MetadataFaultHandle | null = null;
let logsSince: Date | null = null;
const evidence: Record<string, unknown> = {};
const sessions = trackSessions();

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

/**
 * The source's reply check: non-empty, not a terminal model API failure, and
 * not composed from model API retry errors.
 */
function expectAssistantText(message: MessageRecord): void {
  const text = messageText(message).trim();
  expect(text, 'assistant response should not be empty').not.toEqual('');
  expect(text, 'assistant response must not be a terminal model API failure').not.toMatch(/^API Error:\s*\d+\b/);
  const apiRetryBlocks = (message.blocks || []).filter((block) => {
    const raw = block.raw && typeof block.raw === 'object' ? block.raw as Record<string, unknown> : {};
    return String(block.type || '') === 'raw_event'
      && (String(block.subtype || '') === 'api_retry' || String(raw.subtype || '') === 'api_retry');
  });
  expect(apiRetryBlocks, 'assistant response must not be composed from model API retry errors').toEqual([]);
}

/**
 * The original request: one coupled `POST ai-stream` whose body is read to the
 * end. The client message id is minted here so the journal identity can be
 * checked exactly: the platform input id of a UUID client message id is that
 * UUID, and the command id is `<session>:<client message id>`.
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

/**
 * Wait until both faults have been raised and the same rows visited again.
 *
 * Only the fixture's exempt `psql` probes run here: an API read of the Session
 * during this wait would itself be the product reading the row, so it is not
 * made. The armed window ends the moment this returns.
 */
async function waitForFaultsConsumed(handle: MetadataFaultHandle, timeoutMs: number): Promise<MetadataFaultCounters> {
  const deadline = Date.now() + timeoutMs;
  let last = readMetadataFaultCounters(handle);
  for (;;) {
    if (last.read.visits > last.read.failures && last.write.visits > last.write.failures) return last;
    if (Date.now() >= deadline) break;
    await new Promise((resolve) => setTimeout(resolve, 250));
    last = readMetadataFaultCounters(handle);
  }
  throw new Error(
    `both faults must be raised and re-visited within ${timeoutMs}ms; counters=${JSON.stringify(last)}`,
  );
}

async function observe(read: () => unknown | Promise<unknown>): Promise<unknown> {
  try { return await read(); }
  catch (error) { return { unavailable: String(error) }; }
}

test.afterEach(async ({ request }, info) => {
  if (!['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  const api = new AstraApi(request);
  await info.attach('metadata-transient-fault-scene', {
    body: JSON.stringify({
      sessionId,
      ...evidence,
      session: sessionId ? await observe(() => api.getSession(sessionId)) : null,
      history: sessionId ? await observe(() => api.getMessages(sessionId, 100)) : null,
      snapshot: sessionId ? await observe(() => snapshotDoc(sessionId)) : null,
      events: sessionId ? await observe(() => sessionEvents(sessionId)) : null,
      native: sessionId ? await observe(() => nativeRootRows(sessionId)) : null,
      serverLog: logsSince && fault
        ? await observe(() => readMetadataFaultLogEvidence(serverLogLinesSince(logsSince!), fault!.target.marker))
        : null,
    }),
    contentType: 'application/json',
  });
});

test('the first turn survives a transient metadata read fault and a transient snapshot write fault, each raised once and retried', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const startedAt = Date.now();
  const remaining = (floorMs: number): number => Math.max(floorMs, SPEC_BUDGET_MS - (Date.now() - startedAt));
  const runId = `${Date.now()}-${test.info().workerIndex}`;
  const marker = `e2e-metadata-fault-${runId}`;
  const prompt = `E2E metadata transient fault ${runId}: 不要使用工具。请简短回复一句话。`;

  // ── The conversation, READY before anything is armed. ──────────────────────
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  sessionId = created.session_id;
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId);
  const sandboxId = String(ready.sandbox_id || '').trim();
  expect(sandboxId, 'the conversation must own a sandbox before the fault').not.toEqual('');
  const assistantsBefore = await api.assistantCount(sessionId);
  expect(assistantsBefore, 'a fresh conversation has no reply yet').toBe(0);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });

  // ── FAULT: the next read of this Session's metadata row and the next update
  //    of its snapshot row fail transiently. Armed after READY; the original
  //    request is issued immediately after, with no Session API call between. ─
  logsSince = new Date(Date.now() - 1_000);
  fault = armMetadataTransientFault({
    sessionId,
    marker,
    readFailures: READ_FAILURES,
    writeFailures: WRITE_FAILURES,
    expiresAt: new Date(startedAt + SPEC_BUDGET_MS),
  });
  evidence.fault = {
    marker,
    armedAt: fault.armedAt.toISOString(),
    database: fault.database,
    role: fault.role,
    container: fault.container,
    before: fault.before,
    leftoversRemoved: fault.leftoversRemoved,
  };
  test.info().annotations.push({
    type: 'injected_fault',
    description: `PostgreSQL row-level security raises SQLSTATE 40001 on the first ${READ_FAILURES} read(s) of `
      + `sessions/${sessionId} and the first ${WRITE_FAILURES} update(s) of session_snapshots/${sessionId}; marker=${marker}`,
  });
  if (fault.leftoversRemoved.length > 0) {
    test.info().annotations.push({
      type: 'e2e_metadata_fault_leftovers_removed',
      description: fault.leftoversRemoved.join(', '),
    });
  }

  let turn: CoupledTurn | null = null;
  let consumed: MetadataFaultCounters | null = null;
  let disarm: MetadataFaultDisarmResult | null = null;
  let disarmError: unknown = null;
  try {
    // ── THE ORIGINAL REQUEST: exactly one submission for the whole spec. ────
    turn = startCoupledTurn(request, sessionId, prompt);
    evidence.commandId = turn.commandId;
    evidence.inputId = turn.inputId;

    // ── Both faults raised, both rows visited again; then the window closes. ─
    consumed = await waitForFaultsConsumed(fault, remaining(60_000));
    evidence.consumed = consumed;
  } finally {
    // Restore is exact and verified against the pre-arm surface. It runs on
    // every path and its verified outcome is recorded; nothing here deletes
    // the Session, which trackSessions keeps on failure. A restore failure is
    // printed and kept in the scene even when the body already failed, and
    // fails the test itself when the body did not.
    try {
      disarm = disarmMetadataTransientFault(fault);
      evidence.disarm = disarm;
    } catch (error) {
      disarmError = error;
      evidence.disarm = { error: String((error as Error)?.message ?? error) };
      // eslint-disable-next-line no-console -- the report tail is where an operator looks
      console.error(`metadataTransientFault: restore failed; ${String((error as Error)?.message ?? error)}`);
    }
  }
  if (disarmError) throw disarmError;
  const finalCounters = disarm!.counters;
  test.info().annotations.push({ type: 'e2e_metadata_fault_counters', description: JSON.stringify(finalCounters) });
  expect(disarm!.counterError, 'the final counters must be readable before the armed objects are dropped').toBeNull();
  expect(finalCounters, 'the counters must be read before the armed objects are dropped').not.toBeNull();
  expect(finalCounters!.read.raised, 'the metadata read fault must be raised exactly the armed number of times').toBe(READ_FAILURES);
  expect(finalCounters!.write.raised, 'the snapshot write fault must be raised exactly the armed number of times').toBe(WRITE_FAILURES);
  expect(finalCounters!.read.visits, 'the metadata row must be read again after its fault').toBeGreaterThan(READ_FAILURES);
  expect(finalCounters!.write.visits, 'the snapshot row must be updated again after its fault').toBeGreaterThan(WRITE_FAILURES);

  // ── The ORIGINAL turn completes. ───────────────────────────────────────────
  const turnId = await waitForCurrentTurnOrSettled(sessionId, remaining(15_000));
  evidence.turnId = turnId;
  test.info().annotations.push(
    { type: 'e2e_turn_id', description: turnId },
    { type: 'e2e_command_id', description: turn!.commandId },
  );
  const settledSnapshot = await waitForTurnTerminalProof(sessionId, turnId, 'COMPLETED', remaining(30_000));
  evidence.settledSnapshot = settledSnapshot;
  expect(settledSnapshot.last_turn_error ?? null, 'the turn must not leave a turn error on its snapshot').toBeFalsy();
  const settled = await api.waitForSession(
    sessionId,
    (session) => session.state === 'READY' && !String(session.current_turn_id || '').trim(),
    remaining(10_000),
  );
  expect(String(settled.last_turn_id || ''), 'the completed turn must be the original turn').toBe(turnId);
  expect(String(settled.last_turn_status || ''), 'the original turn must complete').toBe('COMPLETED');
  expect(String(settled.sandbox_id || '').trim(), 'the turn must stay on its sandbox').toBe(sandboxId);
  expect(String(settled.last_error || '').trim(), 'the fault must not leak a last_error to the session').toBe('');
  expect(Boolean(settled.runtime_unavailable), 'the fault must not mark the runtime unavailable').toBe(false);
  expect(settled.delivery_failure ?? null, 'the fault must not report a delivery failure').toBeFalsy();

  // ── Server side of the consumption: each raised fault was met inside the
  //    DAL's retry funnel, which names the repository operation; the marker
  //    appears nowhere else in the server log. ──────────────────────────────
  const logEvidence = readMetadataFaultLogEvidence(serverLogLinesSince(logsSince), marker);
  evidence.logEvidence = logEvidence;
  test.info().annotations.push({
    type: 'e2e_metadata_fault_retry_ops',
    description: JSON.stringify(logEvidence.retryWarnings.map((w) => ({
      kind: w.kind, ordinal: w.ordinal, op: w.op, attempt: `${w.attempt}/${w.attempts}`,
    }))),
  });
  expect(
    logEvidence.otherLines,
    'the raised fault must appear in the server log only as the DAL transient-retry warning',
  ).toEqual([]);
  // Each raised fault is one retry warning: the database side of the line
  // names the exact row (`sessions/<this session>` for the read raised inside
  // the SELECT policy, `session_snapshots/<this session>` for the write
  // raised inside the UPDATE check), and the DAL side names the repository
  // operation that met it, which must belong to the matching repository.
  for (const [kind, failures, collection, repository] of [
    ['read', READ_FAILURES, 'sessions', 'sessions'],
    ['write', WRITE_FAILURES, 'session_snapshots', 'session_snapshots'],
  ] as const) {
    const warnings = logEvidence.retryWarnings.filter((w) => w.kind === kind);
    expect(
      warnings.map((w) => w.ordinal).sort((a, b) => a - b),
      `every raised ${kind} fault must be retried by the DAL exactly once; warnings=${JSON.stringify(warnings)}`,
    ).toEqual(Array.from({ length: failures }, (_unused, index) => index + 1));
    for (const warning of warnings) {
      expect(warning.sessionId, `${kind}#${warning.ordinal} must be raised on this Session's row`).toBe(sessionId);
      expect(warning.collection, `${kind}#${warning.ordinal} must be raised on the ${collection} row`).toBe(collection);
      expect(
        warning.op.split('.')[0],
        `${kind}#${warning.ordinal} must be met by a ${repository} repository operation; op=${warning.op}`,
      ).toBe(repository);
      expect(warning.attempt, `${kind}#${warning.ordinal} must be retried, not exhausted`).toBeLessThan(warning.attempts);
    }
  }

  // ── Journal: one acceptance, no failure verdict, a terminal for this turn. ─
  const events = sessionEvents(sessionId);
  const accepted = events.filter(
    (event) => String(event.event_type || '') === 'command.accepted' && String(event.causation_id || '') === turn!.commandId,
  );
  expect(accepted, 'the original request must be accepted exactly once').toHaveLength(1);
  expect(String(accepted[0].turn_id || ''), 'the accepted command must belong to the completed turn').toBe(turnId);
  expect(String(object(accepted[0].payload).input_id || ''), 'the journal must record the platform input id').toBe(turn!.inputId);
  expect(object(accepted[0].payload).content, 'the journal must record the submitted prompt').toBe(prompt);
  expect(
    events.filter((event) => String(event.turn_id || '') === turnId && String(event.event_type || '') === 'turn.failed'),
    'the original turn must never be settled as failed',
  ).toEqual([]);
  expect(
    events.filter((event) => String(event.turn_id || '') === turnId && TURN_TERMINAL_EVENT_TYPES.has(String(event.event_type || ''))).length,
    'the original turn must carry a durable completed terminal',
  ).toBeGreaterThanOrEqual(1);

  // ── Frames: a finish, no error. ────────────────────────────────────────────
  const frameTypes = framesForTurn(turnId).map((frame) => String(object(frame.payload).type || ''));
  evidence.frameTypes = frameTypes;
  expect(frameTypes, 'the original turn must not carry an error frame').not.toContain('error');
  expect(frameTypes, 'the original turn must carry a finish frame').toContain('finish');

  // ── Native custody: the prompt is one native user record, a reply follows. ─
  const native = nativeRootRows(sessionId);
  const nativeUsers = native.filter((row) => row.entry.type === 'user' && nativeRecordText(row.entry) === prompt);
  expect(nativeUsers, 'the prompt must be exactly one native user record, never re-fed').toHaveLength(1);
  expect(nativeUsers[0].uuid, 'the native user record must carry its native identity').not.toEqual('');
  const nativeReplies = native.filter(
    (row) => row.seq > nativeUsers[0].seq && row.entry.type === 'assistant' && nativeRecordText(row.entry).trim() !== '',
  );
  expect(nativeReplies.length, 'a native assistant record must follow the input').toBeGreaterThanOrEqual(1);
  for (const row of native) {
    expect(row.session_id, 'every native record must belong to the same native session').toBe(nativeUsers[0].session_id);
  }

  // ── Public history: one input, one reply on the original turn, no failure. ─
  const history = await api.getMessages(sessionId, 100);
  expect(history.has_more).toBe(false);
  expect(
    history.messages.filter((message) => message.role === 'user').map(messageText),
    'durable history must hold exactly the one submitted input',
  ).toEqual([prompt]);
  const assistants = history.messages.filter((message) => message.role === 'assistant');
  expect(assistants, 'durable history must hold exactly one reply').toHaveLength(1);
  expect(String(assistants[0].turn_id || ''), 'the reply must belong to the original turn').toBe(turnId);
  expectAssistantText(assistants[0]);
  const failureBlocks = [
    ...(assistants[0].blocks || []),
    ...(assistants[0].content_blocks || []),
    ...(assistants[0].parts || []),
  ].filter((block) => String(block.type || '') === 'turn_failure');
  expect(failureBlocks, 'the reply must not carry a turn failure').toEqual([]);

  // ── The original request: never re-sent, no error frame, the reply text
  //    before its terminal. ───────────────────────────────────────────────────
  const outcome = await Promise.race([
    turn!.outcome,
    new Promise<CoupledTurnOutcome>((resolve) => {
      setTimeout(() => resolve({
        status: 0, frameTypes: [], text: '', errorText: null, error: 'original request still pending after the turn settled',
      }), ORIGINAL_REQUEST_GRACE_MS);
    }),
  ]);
  evidence.originalRequest = outcome;
  test.info().annotations.push({ type: 'e2e_original_request_outcome', description: JSON.stringify(outcome) });
  expect(outcome.error, `the original request must survive both faults; outcome=${JSON.stringify(outcome)}`).toEqual('');
  expect(outcome.status, 'the original request must have been admitted').toBe(200);
  expect(outcome.errorText, `the original request must not receive an error frame; outcome=${JSON.stringify(outcome)}`).toBeNull();
  expect(
    outcome.frameTypes,
    `the original request must receive the reply text before its terminal; outcome=${JSON.stringify(outcome)}`,
  ).toContain('text-delta');
  expect(outcome.text.trim(), `the original request must carry non-empty reply text; outcome=${JSON.stringify(outcome)}`).not.toEqual('');

  // ── Cold page: the same conversation renders one input, one reply, READY. ──
  await openSessionView(page, sessionId);
  await expect(page.getByTestId('user-message').filter({ hasText: prompt })).toHaveCount(1, { timeout: 45_000 });
  await expect(page.getByTestId('user-message')).toHaveCount(1);
  await expect(page.getByTestId('assistant-message')).toHaveCount(1, { timeout: 45_000 });
  await expect.poll(async () => (await page.getByTestId('assistant-message').last().innerText()).trim(), {
    timeout: 45_000,
    message: 'the assistant card must render non-empty content on a cold page',
  }).not.toEqual('');
  const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
  await expect(pill).toHaveAttribute('data-state', 'READY', { timeout: 30_000 });
  await expect(pill).toHaveAttribute('data-pulse', 'false', { timeout: 30_000 });
});

/**
 * The turn id of the original request, whether it is still current or has
 * already settled: a short reply can complete before this reads the snapshot.
 */
async function waitForCurrentTurnOrSettled(id: string, timeoutMs: number): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  let snapshot: Record<string, unknown> | null = null;
  while (Date.now() < deadline) {
    snapshot = snapshotDoc(id);
    const current = String(snapshot?.current_turn_id ?? '').trim();
    if (current) return current;
    const last = String(snapshot?.last_turn_id ?? '').trim();
    if (last) return last;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(
    `session ${id} snapshot named neither a current nor a last turn within ${timeoutMs}ms; snapshot=${JSON.stringify(snapshot)}`,
  );
}
