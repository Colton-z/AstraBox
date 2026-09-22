/**
 * Document-store oracle for the community backend.
 *
 * The community deployment persists documents as PostgreSQL JSONB in one
 * `astrabox_documents(seq, collection, doc_id, doc)` table. The oracle executes
 * `psql` inside the selected deployment database container, so it observes the
 * same committed data as the server without depending on a host-mounted database
 * file or a second client library.
 */
import { execFileSync, spawnSync } from 'node:child_process';

import {
  POSTGRES_CONTAINER_HANDLE,
  requireServiceContainer,
} from './serviceContainer';
import type { DockerResult, DockerRunner } from './serviceContainer';

// Frames and snapshots are returned as opaque documents (verbatim stored
// docs) so specs read whatever fields they need without this oracle pinning a
// frame schema.

const COLLECTIONS = {
  agents: 'agents',
  sessions: 'sessions',
  snapshots: 'session_snapshots',
  events: 'session_events',
  assistantWorkspaces: 'assistant_workspace',
} as const;

let _postgresContainer: string | null = null;

function postgresContainerName(): string {
  if (_postgresContainer) return _postgresContainer;
  _postgresContainer = requireServiceContainer(POSTGRES_CONTAINER_HANDLE);
  return _postgresContainer;
}

interface PostgresTextOptions {
  runDocker?: DockerRunner;
  timeoutMs?: number;
}

function runPostgresDocker(args: string[], timeoutMs = 120_000): DockerResult {
  const result = spawnSync('docker', args, {
    encoding: 'utf8',
    timeout: timeoutMs,
    stdio: ['ignore', 'pipe', 'pipe'],
    maxBuffer: 512 * 1024 * 1024,
  });
  return {
    status: result.status,
    stdout: result.stdout || '',
    stderr: result.stderr || '',
    error: result.error,
  };
}

function postgresDiagnostic(result: DockerResult): string {
  const detail = String(result.stderr || result.error?.message || result.stdout || '').trim();
  const status = result.status === null ? 'no exit status' : `exit ${result.status}`;
  return detail ? `${status}: ${detail}` : status;
}

/** Run a text-row PostgreSQL probe with deployment-target diagnostics. */
export function postgresTextRows(
  container: string,
  database: string,
  user: string,
  sql: string,
  options: PostgresTextOptions = {},
): string[] {
  const args = [
    'exec', container, 'psql', '-X', '-A', '-t', '-q',
    '-v', 'ON_ERROR_STOP=1', '-U', user, '-d', database, '-c', sql,
  ];
  const result = (options.runDocker ?? runPostgresDocker)(
    args,
    options.timeoutMs ?? 120_000,
  );
  if (result.error || result.status !== 0) {
    throw new Error(
      `dbOracle: PostgreSQL probe failed in container ${JSON.stringify(container)} ` +
        `on database ${JSON.stringify(database)} as role ${JSON.stringify(user)}: ` +
        `${postgresDiagnostic(result)}\nSQL: ${sql.slice(0, 400)}`,
    );
  }
  const output = result.stdout.trim();
  return output ? output.split('\n').filter(Boolean) : [];
}

/** Single-quote a value for a SQL string literal (doubling embedded quotes). */
function sqlLiteral(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

/** Run a query and return each result row as a JSON object. */
function postgresRows<T = Record<string, unknown>>(sql: string, timeoutMs = 20_000): T[] {
  const database = process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox';
  const user = process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox';
  const inner = sql.trim().replace(/;$/, '');
  const wrapped = `SELECT row_to_json(_row)::text FROM (${inner}) AS _row;`;
  let raw: string;
  try {
    raw = execFileSync('docker', [
      'exec', postgresContainerName(), 'psql', '-X', '-A', '-t', '-q',
      '-v', 'ON_ERROR_STOP=1', '-U', user, '-d', database, '-c', wrapped,
    ], {
      encoding: 'utf8',
      timeout: timeoutMs,
      stdio: ['ignore', 'pipe', 'pipe'],
      maxBuffer: 512 * 1024 * 1024,
    });
  } catch (error) {
    const err = error as { stderr?: Buffer | string; message?: string };
    const stderr = typeof err.stderr === 'string' ? err.stderr : err.stderr?.toString() ?? '';
    throw new Error(`dbOracle: PostgreSQL query failed: ${stderr.trim() || err.message}\nSQL: ${sql}`);
  }
  const text = raw.trim();
  if (!text) return [];
  return text.split('\n').filter(Boolean).map((line) => JSON.parse(line) as T);
}

function jsonField(jsonPath: string): string {
  const match = /^\$\.([A-Za-z_][A-Za-z0-9_]*)$/.exec(jsonPath);
  if (!match) throw new Error(`dbOracle: only top-level JSON paths are supported, got ${jsonPath}`);
  return match[1];
}

/**
 * Load full documents from one collection whose JSON field at `jsonPath` equals
 * `value`.
 */
function loadDocs(
  collection: string,
  jsonPath: string,
  value: string,
  orderByJsonPath?: string,
): Record<string, unknown>[] {
  const field = jsonField(jsonPath);
  const where = `collection = ${sqlLiteral(collection)} AND doc ->> ${sqlLiteral(field)} = ${sqlLiteral(value)}`;
  const order = orderByJsonPath
    ? `doc -> ${sqlLiteral(jsonField(orderByJsonPath))} ASC, seq ASC`
    : 'seq ASC';
  const rows = postgresRows<{ doc: Record<string, unknown> }>(
    `SELECT doc FROM astrabox_documents WHERE ${where} ORDER BY ${order};`,
  );
  return rows.map((row) => row.doc);
}

// ── Public oracle surface ─────────────────────────────────────────────────

/** Full documents matching one top-level JSON field, for cross-boundary E2E checks. */
export function documentsByField(
  collection: string,
  jsonPath: string,
  value: string,
): Record<string, unknown>[] {
  return loadDocs(collection, jsonPath, value);
}

/** Count matching documents without materializing their JSON payloads. */
export function documentCountByField(
  collection: string,
  jsonPath: string,
  value: string,
): number {
  const field = jsonField(jsonPath);
  const rows = postgresRows<{ count: number }>(
    `SELECT count(*)::int AS count FROM astrabox_documents `
      + `WHERE collection = ${sqlLiteral(collection)} `
      + `AND doc ->> ${sqlLiteral(field)} = ${sqlLiteral(value)};`,
  );
  return Number(rows[0]?.count ?? 0);
}

/** The `sessions`-collection document for a session (or null). Keyed by `session_id`. */
export function sessionDoc(sessionId: string): Record<string, unknown> | null {
  return loadDocs(COLLECTIONS.sessions, '$.session_id', sessionId)[0] ?? null;
}

/** The `session_snapshots` projection document for a session (or null). Keyed by `session_id`. */
export function snapshotDoc(sessionId: string): Record<string, unknown> | null {
  return loadDocs(COLLECTIONS.snapshots, '$.session_id', sessionId)[0] ?? null;
}

/** All canonical engine-frame events for a turn, ordered by the shared event sequence. */
export function framesForTurn(turnId: string): Record<string, unknown>[] {
  return loadDocs(COLLECTIONS.events, '$.turn_id', turnId, '$.event_seq').filter(
    (event) => event.event_kind === 'engine_frame',
  );
}

/**
 * The session snapshot, but ONLY while it is describing `turnId` — else null.
 *
 * The durable record of one turn's progress is the session snapshot, not a
 * per-turn row: the kernel writes a single authority, keyed by session, that
 * carries (`conversation_state`, `active_interaction_id`, terminal proof) for
 * whichever turn it is currently on.
 *
 * The turn binding is the reason this exists rather than callers reaching for
 * `snapshotDoc`. A snapshot is per SESSION: read it without checking whose turn
 * it describes and "this turn is paused on this interaction" quietly weakens to
 * "some turn is" — an assertion that still passes while proving less.
 */
export function turnSnapshot(sessionId: string, turnId: string): Record<string, unknown> | null {
  const snap = snapshotDoc(sessionId);
  if (!snap) return null;
  return String(snap.current_turn_id ?? '').trim() === turnId ? snap : null;
}

/**
 * `turnSnapshot`, awaited — for a precondition that a live API already revealed.
 *
 * The pending interaction becomes readable over HTTP as soon as the kernel
 * registers it; the snapshot naming the turn it parked on is a separate durable
 * write. Sampling `turnSnapshot` once at the instant the API answers reads a
 * convergence as though it were an instant, and fails on the interval between
 * the two writes rather than on anything the spec is about.
 *
 * Waiting does not weaken what is asserted: the snapshot must still be on THIS
 * turn before the spec proceeds, and a snapshot that never gets there throws
 * with what it was on instead — which IS the defect this precondition guards
 * (a restart in that state has nothing durable to rehydrate from).
 */
export async function waitForTurnSnapshot(
  sessionId: string,
  turnId: string,
  timeoutMs = 30_000,
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + timeoutMs;
  let lastSeen: string | null = null;
  for (;;) {
    const snap = turnSnapshot(sessionId, turnId);
    if (snap) return snap;
    lastSeen = String(snapshotDoc(sessionId)?.current_turn_id ?? '<no snapshot>');
    if (Date.now() >= deadline) break;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(
    `dbOracle: session ${sessionId} snapshot never reached turn ${turnId} within ${timeoutMs}ms; ` +
      `it was on current_turn_id=${lastSeen}. Read from ${oracleDbPath()}; set ` +
      'ASTRABOX_E2E_POSTGRES_CONTAINER if that is not the database used by the server.',
  );
}

/** Platform command/lifecycle events, excluding canonical engine-frame events. */
export function sessionEvents(sessionId: string): Record<string, unknown>[] {
  return loadDocs(COLLECTIONS.events, '$.session_id', sessionId, '$.event_seq').filter(
    (event) => event.event_kind !== 'engine_frame',
  );
}

/**
 * Await the turn's expected outcome and durable terminal proof.
 *
 * Both the snapshot's last-turn outcome and its terminal frame must belong to
 * the requested turn. A terminal frame alone only proves the turn ended.
 */
export async function waitForTurnTerminalProof(
  sessionId: string,
  turnId: string,
  expectedState: string,
  timeoutMs = 90_000,
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + timeoutMs;
  let lastSnapshot: Record<string, unknown> | null = null;
  while (Date.now() < deadline) {
    const snap = snapshotDoc(sessionId);
    lastSnapshot = snap;
    const frame = (snap?.last_turn_terminal_frame ?? null) as Record<string, unknown> | null;
    if (
      snap
      && String(snap.last_turn_id ?? '') === turnId
      && String(snap.last_turn_status ?? '') === expectedState
      && frame
      && String(frame.turn_id ?? '') === turnId
    ) return snap;
    await new Promise((r) => setTimeout(r, 1_000));
  }
  throw new Error(
    `dbOracle: turn ${sessionId}/${turnId} did not reach ${expectedState} with a durable terminal proof within ${timeoutMs}ms; ` +
      `last_turn_id=${String(lastSnapshot?.last_turn_id ?? '<none>')}; ` +
      `last_turn_status=${String(lastSnapshot?.last_turn_status ?? '<none>')}; ` +
      `last_turn_error=${String(lastSnapshot?.last_turn_error ?? '<none>')}; ` +
      `last_turn_terminal_frame=${JSON.stringify(lastSnapshot?.last_turn_terminal_frame ?? null)}`,
  );
}

/** Effective database target in use (kept under the existing diagnostic API). */
export function oracleDbPath(): string {
  return `postgresql://${postgresContainerName()}/${process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox'}`;
}

// ── Write-mutators (recovery fault simulation) ────────────────────────────
//
// Recovery specs need to REWRITE stored documents (simulate a soft-deleted
// session, park a turn unresolved) and restore them in
// cleanup. Writes run in the real PostgreSQL service. Every mutator returns the
// pre-mutation document so `finally` blocks can restore verbatim.

/** Remove exact, observed event sequences from one test-owned Session; return the removed evidence. */
export function deleteSessionEvents(sessionId: string, eventSeqs: number[]): Record<string, unknown>[] {
  if (!sessionId.trim() || eventSeqs.length === 0
    || eventSeqs.some((seq) => !Number.isSafeInteger(seq) || seq <= 0)
    || new Set(eventSeqs).size !== eventSeqs.length) {
    throw new Error('dbOracle.deleteSessionEvents requires one Session and distinct positive event sequences');
  }
  const sql = `WITH removed AS (
    DELETE FROM astrabox_documents
    WHERE collection = ${sqlLiteral(COLLECTIONS.events)}
      AND doc ->> 'session_id' = ${sqlLiteral(sessionId)}
      AND (doc ->> 'event_seq')::bigint IN (${eventSeqs.join(',')})
    RETURNING doc
  ) SELECT doc::text FROM removed ORDER BY (doc ->> 'event_seq')::bigint;`;
  const removed = postgresTextRows(
    postgresContainerName(), process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox', sql, { timeoutMs: 20_000 },
  ).map((row) => JSON.parse(row) as Record<string, unknown>);
  if (removed.length !== eventSeqs.length) {
    throw new Error(`dbOracle.deleteSessionEvents removed ${removed.length} of ${eventSeqs.length} events for ${sessionId}`);
  }
  return removed;
}

function postgresWrite(sql: string, timeoutMs = 20_000): void {
  const database = process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox';
  const user = process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox';
  try {
    execFileSync(
      'docker',
      [
        'exec', postgresContainerName(), 'psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1',
        '-U', user, '-d', database, '-c', sql,
      ],
      { encoding: 'utf8', timeout: timeoutMs, stdio: ['ignore', 'pipe', 'pipe'] },
    );
  } catch (error) {
    const err = error as { stderr?: Buffer | string; message?: string };
    const stderr = typeof err.stderr === 'string' ? err.stderr : err.stderr?.toString() ?? '';
    throw new Error(`dbOracle: PostgreSQL write failed: ${stderr.trim() || err.message}\nSQL: ${sql.slice(0, 400)}`);
  }
}

function docWhere(collection: string, matches: Record<string, string>): string {
  const clauses = Object.entries(matches).map(
    ([jsonPath, value]) => `doc ->> ${sqlLiteral(jsonField(jsonPath))} = ${sqlLiteral(value)}`,
  );
  return [`collection = ${sqlLiteral(collection)}`, ...clauses].join(' AND ');
}

/**
 * Replace matching documents wholesale with `nextDoc` (serialized verbatim).
 * Returns the pre-mutation documents (empty array when nothing matched).
 */
export function replaceDocs(
  collection: string,
  matches: Record<string, string>,
  nextDoc: Record<string, unknown>,
): Record<string, unknown>[] {
  const where = docWhere(collection, matches);
  const beforeRows = postgresRows<{ doc: Record<string, unknown> }>(
    `SELECT doc FROM astrabox_documents WHERE ${where};`,
  );
  const before = beforeRows.map((row) => row.doc);
  if (before.length > 0) {
    postgresWrite(
      `UPDATE astrabox_documents SET doc = ${sqlLiteral(JSON.stringify(nextDoc))}::jsonb WHERE ${where};`,
    );
  }
  return before;
}

/**
 * Shallow-merge `patch` into every matching document (a `patch` value of null
 * DELETES that key). Returns the pre-mutation documents.
 */
export function patchDocs(
  collection: string,
  matches: Record<string, string>,
  patch: Record<string, unknown>,
): Record<string, unknown>[] {
  const where = docWhere(collection, matches);
  const beforeRows = postgresRows<{ doc: Record<string, unknown> }>(
    `SELECT doc FROM astrabox_documents WHERE ${where};`,
  );
  const before = beforeRows.map((row) => row.doc);
  // The identity keys in `matches` are unique per document for every caller
  // (session_id / session_id+turn_id), so patch the single matched doc. A
  // byte-equality guard on the original doc text cannot work across
  // serializers (Python json.dumps vs JS JSON.stringify formatting).
  if (before.length !== 1) {
    throw new Error(
      `dbOracle.patchDocs expects exactly one matched doc, got ${before.length} `
        + `for ${collection} ${JSON.stringify(matches)}`,
    );
  }
  const next: Record<string, unknown> = { ...before[0] };
  for (const [key, value] of Object.entries(patch)) {
    if (value === null) delete next[key];
    else next[key] = value;
  }
  postgresWrite(
    `UPDATE astrabox_documents SET doc = ${sqlLiteral(JSON.stringify(next))}::jsonb WHERE ${where};`,
  );
  return before;
}

/** Convenience typed wrappers for common recovery-simulation collections. */
export function patchSessionDoc(sessionId: string, patch: Record<string, unknown>): Record<string, unknown>[] {
  return patchDocs(COLLECTIONS.sessions, { '$.session_id': sessionId }, patch);
}

/** Lapse only this still-bound sandbox's lease; never restore a concurrently cleared binding. */
export function lapseSessionSandboxLease(
  sessionId: string, sandboxId: string, expiresAt: string,
): Record<string, unknown>[] {
  const timestamp = Date.parse(expiresAt);
  if (!sessionId.trim() || !sandboxId.trim() || !Number.isFinite(timestamp) || timestamp >= Date.now()) {
    throw new Error('lease lapse requires an exact Session, sandbox and past expiry');
  }
  const sql = `WITH lapsed AS (
    UPDATE astrabox_documents
    SET doc = jsonb_set(doc, '{expires_at}', ${sqlLiteral(JSON.stringify(expiresAt))}::jsonb, true)
    WHERE ${docWhere(COLLECTIONS.sessions, { '$.session_id': sessionId, '$.sandbox_id': sandboxId })}
    RETURNING doc
  ) SELECT json_build_object('session_id', doc -> 'session_id',
    'sandbox_id', doc -> 'sandbox_id', 'expires_at', doc -> 'expires_at')::text FROM lapsed;`;
  const rows = postgresTextRows(
    postgresContainerName(), process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox', sql, { timeoutMs: 20_000 },
  ).map((row) => JSON.parse(row) as Record<string, unknown>);
  if (rows.length > 1) throw new Error(`lease lapse matched ${rows.length} rows for ${sessionId}/${sandboxId}`);
  // Zero means the exact old binding is gone, not that convergence
  // succeeded. The caller must independently prove READY and its real cause.
  return rows;
}

/**
 * Lapse only this still-bound Assistant workspace lease, and return what landed.
 *
 * The same fencing discipline as `lapseSessionSandboxLease`, on the owner
 * collection a workspace lives in: matching `assistant_id` AND
 * `current_sandbox_id` means a workspace that has already republished a
 * different box is left alone rather than dragged back onto a dead pointer.
 *
 * One statement, not a read-modify-write: `patchDocs` would rewrite the whole
 * document from a value read in a separate `psql` call, and the expiration
 * watcher writes this row. The `RETURNING` projection is also the read-back —
 * the caller asserts the intended past expiry from the same statement that
 * wrote it, rather than from a second query that could observe a later one.
 *
 * Zero rows means the exact binding is already gone, which is a broken premise
 * for any caller that just proved the workspace was READY on that box; the
 * caller says what that means for its own journey.
 */
export function lapseAssistantWorkspaceLease(
  assistantId: string, sandboxId: string, expiresAt: string,
): Record<string, unknown>[] {
  const timestamp = Date.parse(expiresAt);
  if (!assistantId.trim() || !sandboxId.trim() || !Number.isFinite(timestamp) || timestamp >= Date.now()) {
    throw new Error('workspace lease lapse requires an exact Assistant, sandbox and past expiry');
  }
  const sql = `WITH lapsed AS (
    UPDATE astrabox_documents
    SET doc = jsonb_set(doc, '{current_sandbox_expires_at}', ${sqlLiteral(JSON.stringify(expiresAt))}::jsonb, true)
    WHERE ${docWhere(COLLECTIONS.assistantWorkspaces, {
      '$.assistant_id': assistantId,
      '$.current_sandbox_id': sandboxId,
    })}
    RETURNING doc
  ) SELECT json_build_object('assistant_id', doc -> 'assistant_id',
    'current_sandbox_id', doc -> 'current_sandbox_id',
    'current_sandbox_expires_at', doc -> 'current_sandbox_expires_at')::text FROM lapsed;`;
  const rows = postgresTextRows(
    postgresContainerName(), process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox', sql, { timeoutMs: 20_000 },
  ).map((row) => JSON.parse(row) as Record<string, unknown>);
  if (rows.length > 1) {
    throw new Error(
      `workspace lease lapse matched ${rows.length} rows for ${assistantId}/${sandboxId}`,
    );
  }
  return rows;
}

/**
 * Age exactly this prepared slot manifest, and nothing else on the Agent row.
 *
 * TTL-driven preparation behaviour is separated from the rest of the product by
 * half an hour, which no lane spec can spend. `prepared_at` is where that time
 * enters the mechanism, so this writes that one key.
 *
 * Deliberately not `patchDocs`: that reads the whole Agent document and writes
 * it back, and the platform's own sweeps rewrite the very same row (clearing
 * `sandbox_id` when the resident box is confirmed gone). A read-modify-write
 * would restore the pointer the sweep had just cleared, which is the row shape
 * the renewal question is about — the fixture would then be the thing under
 * test. One statement, guarded down to the slot id, cannot do that.
 */
export function backdatePreparedSlot(
  agentId: string, slotId: string, preparedAt: string,
): Record<string, unknown>[] {
  const timestamp = Date.parse(preparedAt);
  if (!agentId.trim() || !slotId.trim() || !Number.isFinite(timestamp) || timestamp >= Date.now()) {
    throw new Error('prepared-slot ageing requires an exact Agent, slot and past prepared_at');
  }
  const sql = `WITH aged AS (
    UPDATE astrabox_documents
    SET doc = jsonb_set(doc, '{_prepared_slot,prepared_at}', ${sqlLiteral(JSON.stringify(preparedAt))}::jsonb, true)
    WHERE ${docWhere(COLLECTIONS.agents, { '$.agent_id': agentId })}
      AND doc -> '_prepared_slot' ->> 'slot_id' = ${sqlLiteral(slotId)}
    RETURNING doc
  ) SELECT json_build_object('agent_id', doc -> 'agent_id',
    'sandbox_id', doc -> 'sandbox_id',
    'slot_id', doc -> '_prepared_slot' -> 'slot_id',
    'state', doc -> '_prepared_slot' -> 'state',
    'prepared_at', doc -> '_prepared_slot' -> 'prepared_at')::text FROM aged;`;
  const rows = postgresTextRows(
    postgresContainerName(), process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox', sql, { timeoutMs: 20_000 },
  ).map((row) => JSON.parse(row) as Record<string, unknown>);
  if (rows.length > 1) throw new Error(`prepared-slot ageing matched ${rows.length} rows for ${agentId}/${slotId}`);
  // Zero means this exact manifest is not on the row — a replacement, a
  // discard, or a slot id the caller never observed. The caller decides which,
  // because none of them is the scene it asked to age.
  return rows;
}

/**
 * Age exactly this CLAIMED manifest past the orphan fence its claimer owns.
 *
 * A claim is a two-part hand-off: `claim_prepared_slot` flips the manifest to
 * `claimed` and the starting Session clears it at the end of its own startup
 * (`clear_claimed_slot`, engine/startup.py:434). Between those two lines the
 * manifest belongs to that Session and nothing may touch it —
 * `retire_prepared_runtime` says so and returns early inside the fence
 * (prepared_slots.py:400-417). `claimed_at` is the only field that decides
 * which side of the fence a manifest is on, and `CLAIMED_SLOT_ORPHAN_SECONDS`
 * is a hardcoded ten minutes (prepared_slots.py:124) with no env-registry row,
 * so no deployment setting brings that boundary inside a 180s wall.
 *
 * The state guard is load-bearing and not defensive dressing: ageing a
 * `prepared` manifest through this function would silently produce the TTL
 * scene that `backdatePreparedSlot` above already owns, and the caller would
 * be asserting against a different defect than the one it named.
 *
 * Deliberately not `patchDocs`, for the same reason its two siblings give: a
 * read-modify-write of the whole Agent document loses whatever the platform's
 * own sweeps wrote to that row in between, and the manifest is exactly what
 * those sweeps rewrite. One statement, guarded down to the slot id and the
 * state, cannot do that; the `RETURNING` projection is the read-back, so the
 * caller asserts what landed from the statement that wrote it.
 */
export function orphanClaimedSlot(
  agentId: string, slotId: string, claimedAt: string,
): Record<string, unknown>[] {
  const timestamp = Date.parse(claimedAt);
  if (!agentId.trim() || !slotId.trim() || !Number.isFinite(timestamp) || timestamp >= Date.now()) {
    throw new Error('claimed-slot ageing requires an exact Agent, slot and past claimed_at');
  }
  const sql = `WITH orphaned AS (
    UPDATE astrabox_documents
    SET doc = jsonb_set(doc, '{_prepared_slot,claimed_at}', ${sqlLiteral(JSON.stringify(claimedAt))}::jsonb, true)
    WHERE ${docWhere(COLLECTIONS.agents, { '$.agent_id': agentId })}
      AND doc -> '_prepared_slot' ->> 'slot_id' = ${sqlLiteral(slotId)}
      AND doc -> '_prepared_slot' ->> 'state' = 'claimed'
    RETURNING doc
  ) SELECT json_build_object('agent_id', doc -> 'agent_id',
    'slot_id', doc -> '_prepared_slot' -> 'slot_id',
    'state', doc -> '_prepared_slot' -> 'state',
    'claimed_at', doc -> '_prepared_slot' -> 'claimed_at',
    'claimed_session_id', doc -> '_prepared_slot' -> 'claimed_session_id')::text FROM orphaned;`;
  const rows = postgresTextRows(
    postgresContainerName(), process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox', sql, { timeoutMs: 20_000 },
  ).map((row) => JSON.parse(row) as Record<string, unknown>);
  if (rows.length > 1) {
    throw new Error(`claimed-slot ageing matched ${rows.length} rows for ${agentId}/${slotId}`);
  }
  // Zero rows means the guard matched nothing: this slot is not on the
  // row, or it is not `claimed` — a replacement, a discard, or a claim
  // something already cleared. The caller decides which, because none of them
  // is the orphan it asked to age.
  return rows;
}

/**
 * Write one runtime generation onto exactly this prepared slot manifest.
 *
 * The generation is how the product itself marks a prepared unit unclaimable:
 * `claim_prepared_slot` refuses a manifest whose `runtime_generation` is not the
 * Agent's current one, and that is the state a row genuinely sits in between a
 * configuration change and the refill that rebuilds its slot. Unlike the TTL, no
 * timer stands over that rule — `prepared_slot_is_due_for_renewal` substitutes
 * the manifest's own generation, so the renewal sweep neither sees a mismatch
 * nor repairs one, and a spec that writes one can read the row back without
 * racing a rebuild it did not ask for.
 *
 * Deliberately not `patchDocs`, for the reason its sibling above gives: that
 * reads the whole Agent document in one `psql` call and writes it back in
 * another, and `matches` can only name the Agent. A refill landing in between
 * would be overwritten — and the restamp would then be sitting on a manifest the
 * product had already replaced, which reads to every later assertion as the
 * scene the caller asked for. One statement, guarded down to the slot id, cannot
 * do that. The `RETURNING` projection is also the read-back, so the caller
 * asserts what landed from the statement that wrote it.
 */
export function restampPreparedSlotGeneration(
  agentId: string, slotId: string, runtimeGeneration: string,
): Record<string, unknown>[] {
  if (!agentId.trim() || !slotId.trim() || !runtimeGeneration.trim()) {
    throw new Error('prepared-slot restamping requires an exact Agent, slot and runtime generation');
  }
  const sql = `WITH restamped AS (
    UPDATE astrabox_documents
    SET doc = jsonb_set(doc, '{_prepared_slot,runtime_generation}', ${sqlLiteral(JSON.stringify(runtimeGeneration))}::jsonb, true)
    WHERE ${docWhere(COLLECTIONS.agents, { '$.agent_id': agentId })}
      AND doc -> '_prepared_slot' ->> 'slot_id' = ${sqlLiteral(slotId)}
    RETURNING doc
  ) SELECT json_build_object('agent_id', doc -> 'agent_id',
    'slot_id', doc -> '_prepared_slot' -> 'slot_id',
    'state', doc -> '_prepared_slot' -> 'state',
    'runtime_generation', doc -> '_prepared_slot' -> 'runtime_generation',
    'agent_runtime_generation', doc -> '_runtime_generation')::text FROM restamped;`;
  const rows = postgresTextRows(
    postgresContainerName(), process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox', sql, { timeoutMs: 20_000 },
  ).map((row) => JSON.parse(row) as Record<string, unknown>);
  if (rows.length > 1) {
    throw new Error(`prepared-slot restamping matched ${rows.length} rows for ${agentId}/${slotId}`);
  }
  // Zero means this exact manifest is not on the row — a replacement, a
  // discard, or a slot id the caller never observed. The caller decides which,
  // because none of them is the scene it asked to restamp.
  return rows;
}

export function patchSnapshotDoc(sessionId: string, patch: Record<string, unknown>): Record<string, unknown>[] {
  return patchDocs(COLLECTIONS.snapshots, { '$.session_id': sessionId }, patch);
}

export function restoreDoc(
  collection: keyof typeof COLLECTIONS,
  matches: Record<string, string>,
  originalDoc: Record<string, unknown>,
): void {
  replaceDocs(COLLECTIONS[collection], matches, originalDoc);
}

// ── Background-continuation window ────────────────────────────────────────
//
// The sweep that carries a finished background Agent's result back into its
// conversation reads the fifty NEWEST opened manifests deployment-wide, and
// nothing narrows that to the ones still waiting: `find({channel, event_type})`
// `.sort([(occurred_at,-1),(event_seq,-1)]).limit(50)` (background_continuation
// .py:136-159). `session_events` is append-only and never pruned, so the window
// is a queue that history fills. Proving what happens to a manifest that falls
// out of it means putting fifty newer ones in front of it, and fifty real
// background launches do not fit a 180s wall.
//
// A seeded pair is an ALREADY SETTLED manifest: an opened row plus the
// materialized counterpart the sweep looks up by causation id. It occupies a
// window slot and does no work —
// `_materialize_background_continuation_event` returns at its first line when
// that counterpart exists (background_continuation.py:258-263). That is the
// defect's own shape, not a fabricated failure, and the seeding is faithful in
// the only two properties the sweep reads: a newer `(occurred_at, event_seq)`
// sort key, and a counterpart that is already claimed.

/** Field stamped on every seeded row, so cleanup is an equality, not a guess. */
const SEEDED_MANIFEST_TAG_FIELD = 'e2e_seed_tag';

/**
 * Tag prefix for seeded manifests. No SQL `LIKE` metacharacter (`_`, `%`) may
 * appear in it: the leftover sweep matches on this prefix directly.
 */
export const SEEDED_MANIFEST_TAG_PREFIX = 'e2e-bgwindow-';

/** A tag is this prefix plus lowercase-safe, metacharacter-free identity. */
const SEEDED_MANIFEST_TAG_RE = new RegExp(`^${SEEDED_MANIFEST_TAG_PREFIX}[a-z0-9-]+$`);

export interface SeededBackgroundManifests {
  tag: string;
  /** Settled manifests written (one opened row + one materialized row each). */
  manifests: number;
  /** Rows written — twice `manifests`, read back from the INSERT itself. */
  rows: number;
  firstOccurredAt: string;
  lastOccurredAt: string;
}

export interface OpenedManifestWindowRow {
  session_id: string;
  event_seq: number;
  occurred_at: string;
}

/**
 * Write `count` settled background manifests stamped just after `afterOccurredAt`.
 *
 * Timestamps are computed in SQL from the caller's own ISO string, so the
 * seeded rows keep the source row's microseconds and land strictly after it —
 * a JavaScript `Date` would truncate to milliseconds first and could stamp a
 * row that ties the event it is supposed to bury. They are past, never future:
 * `afterOccurredAt` is an observed row's `occurred_at` and the offsets are
 * milliseconds.
 *
 * One statement, so a killed worker cannot leave half a backlog: both
 * data-modifying CTEs run to completion exactly once, and the returned counts
 * come from the inserts themselves rather than a second query.
 */
export function seedSettledBackgroundManifests(options: {
  count: number;
  afterOccurredAt: string;
  tag: string;
}): SeededBackgroundManifests {
  const { count, afterOccurredAt, tag } = options;
  if (!Number.isSafeInteger(count) || count < 1 || count > 500) {
    throw new Error(`dbOracle.seedSettledBackgroundManifests: count must be 1..500, got ${count}`);
  }
  if (!SEEDED_MANIFEST_TAG_RE.test(tag)) {
    throw new Error(
      `dbOracle.seedSettledBackgroundManifests: tag must match ${SEEDED_MANIFEST_TAG_RE.source}, got ${JSON.stringify(tag)}`,
    );
  }
  if (!Number.isFinite(Date.parse(afterOccurredAt))) {
    throw new Error(
      `dbOracle.seedSettledBackgroundManifests: afterOccurredAt must be an ISO timestamp, got ${JSON.stringify(afterOccurredAt)}`,
    );
  }
  const tagLiteral = sqlLiteral(tag);
  const tagField = sqlLiteral(SEEDED_MANIFEST_TAG_FIELD);
  const sql = `WITH base AS (
    SELECT (${sqlLiteral(afterOccurredAt)})::timestamptz AT TIME ZONE 'UTC' AS at
  ), stamped AS (
    SELECT g.n,
           ${tagLiteral} || '-' || g.n AS session_id,
           to_char(
             (SELECT at FROM base) + (g.n || ' milliseconds')::interval,
             'YYYY-MM-DD"T"HH24:MI:SS.US'
           ) || '+00:00' AS occurred_at
    FROM generate_series(1, ${count}) AS g(n)
  ), opened AS (
    INSERT INTO astrabox_documents (collection, doc_id, doc)
    SELECT ${sqlLiteral(COLLECTIONS.events)}, s.session_id || '-opened', jsonb_build_object(
      '_id', s.session_id || '-opened',
      'session_id', s.session_id,
      'channel', 'conversation',
      'turn_id', s.session_id || '-turn',
      'event_type', 'turn.background_tasks_opened',
      'event_seq', 1,
      'event_kind', 'command',
      'causation_id', 'cmd-' || s.n || ':background-continuation',
      'correlation_id', s.session_id,
      'idempotency_key', s.session_id || '-opened',
      'occurred_at', s.occurred_at,
      'payload', jsonb_build_object(
        'command_id', 'cmd-' || s.n,
        'source', 'e2e_seeded_background_task',
        'engine_refs', jsonb_build_array(s.session_id || '-child')
      ),
      ${tagField}, ${tagLiteral}
    ) FROM stamped s
    RETURNING 1
  ), settled AS (
    INSERT INTO astrabox_documents (collection, doc_id, doc)
    SELECT ${sqlLiteral(COLLECTIONS.events)}, s.session_id || '-materialized', jsonb_build_object(
      '_id', s.session_id || '-materialized',
      'session_id', s.session_id,
      'channel', 'conversation',
      'turn_id', s.session_id || '-turn',
      'event_type', 'turn.background_tasks_materialized',
      'event_seq', 2,
      'event_kind', 'command',
      'causation_id', '1:background-tasks-materialized',
      'correlation_id', s.session_id,
      'idempotency_key', s.session_id || '-materialized',
      'occurred_at', s.occurred_at,
      'payload', jsonb_build_object(
        'source', 'e2e_seeded_background_task',
        'source_opened_event_seq', 1,
        'blocks', jsonb_build_array()
      ),
      ${tagField}, ${tagLiteral}
    ) FROM stamped s
    RETURNING 1
  ) SELECT json_build_object(
      'opened', (SELECT count(*) FROM opened),
      'settled', (SELECT count(*) FROM settled),
      'first_occurred_at', (SELECT min(occurred_at) FROM stamped),
      'last_occurred_at', (SELECT max(occurred_at) FROM stamped)
    )::text;`;
  const rows = postgresTextRows(
    postgresContainerName(), process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox', sql, { timeoutMs: 30_000 },
  ).map((row) => JSON.parse(row) as Record<string, unknown>);
  const result = rows[0];
  if (!result) {
    throw new Error(`dbOracle.seedSettledBackgroundManifests wrote nothing for ${tag}`);
  }
  const opened = Number(result.opened ?? 0);
  const settled = Number(result.settled ?? 0);
  if (opened !== count || settled !== count) {
    throw new Error(
      `dbOracle.seedSettledBackgroundManifests wrote ${opened} opened and ${settled} materialized rows, expected ${count} of each`,
    );
  }
  return {
    tag,
    manifests: count,
    rows: opened + settled,
    firstOccurredAt: String(result.first_occurred_at ?? ''),
    lastOccurredAt: String(result.last_occurred_at ?? ''),
  };
}

/**
 * Delete seeded manifests by exact tag, or sweep every leftover under the prefix.
 *
 * Matching is on the seeding field these rows carry and no product row has, so
 * this can never reach a real conversation's journal. Returns the row count
 * removed; zero from an exact tag means the backlog was already gone.
 */
export function removeSeededBackgroundManifests(
  match: { tag: string } | { prefix: string },
): number {
  const predicate = 'tag' in match
    ? `doc ->> ${sqlLiteral(SEEDED_MANIFEST_TAG_FIELD)} = ${sqlLiteral(match.tag)}`
    : `doc ->> ${sqlLiteral(SEEDED_MANIFEST_TAG_FIELD)} LIKE ${sqlLiteral(`${match.prefix}%`)}`;
  if ('tag' in match) {
    if (!SEEDED_MANIFEST_TAG_RE.test(match.tag)) {
      throw new Error(`dbOracle.removeSeededBackgroundManifests: unknown tag ${JSON.stringify(match.tag)}`);
    }
  } else if (!match.prefix.startsWith(SEEDED_MANIFEST_TAG_PREFIX)) {
    throw new Error(
      `dbOracle.removeSeededBackgroundManifests: a sweep prefix must start with ${SEEDED_MANIFEST_TAG_PREFIX}, got ${JSON.stringify(match.prefix)}`,
    );
  }
  const sql = `WITH removed AS (
    DELETE FROM astrabox_documents
    WHERE collection = ${sqlLiteral(COLLECTIONS.events)} AND ${predicate}
    RETURNING 1
  ) SELECT count(*)::text FROM removed;`;
  const rows = postgresTextRows(
    postgresContainerName(), process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox', sql, { timeoutMs: 30_000 },
  );
  return Number(rows[0] ?? 0);
}

/**
 * The newest opened manifests deployment-wide, newest first.
 *
 * This is the production query's ordering written a second time, in SQL, which
 * is exactly why it is scaffolding and never a verdict: if the sweep's sort key
 * changes and this does not, it will report a window the sweep does not have.
 * `COLLATE "C"` is what keeps it comparable at all — the sweep sorts these ISO
 * strings in Python, by code point, and a database collation orders punctuation
 * by its own rules.
 */
export function newestOpenedManifestWindow(limit = 50): OpenedManifestWindowRow[] {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 500) {
    throw new Error(`dbOracle.newestOpenedManifestWindow: limit must be 1..500, got ${limit}`);
  }
  const sql = `SELECT json_build_object(
      'session_id', doc ->> 'session_id',
      'event_seq', (doc ->> 'event_seq')::bigint,
      'occurred_at', doc ->> 'occurred_at'
    )::text
    FROM astrabox_documents
    WHERE collection = ${sqlLiteral(COLLECTIONS.events)}
      AND doc ->> 'channel' = 'conversation'
      AND doc ->> 'event_type' = 'turn.background_tasks_opened'
    ORDER BY (doc ->> 'occurred_at') COLLATE "C" DESC, (doc ->> 'event_seq')::bigint DESC
    LIMIT ${limit};`;
  return postgresTextRows(
    postgresContainerName(), process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox', sql, { timeoutMs: 30_000 },
  ).map((row) => JSON.parse(row) as OpenedManifestWindowRow);
}
