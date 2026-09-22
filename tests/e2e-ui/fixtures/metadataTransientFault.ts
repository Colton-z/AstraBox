/**
 * Transient metadata faults for one Session, raised by PostgreSQL itself.
 *
 * The community metadata store is one JSONB table, `astrabox_documents`, read
 * and written by the server's own role through SQLAlchemy + asyncpg. The DAL
 * wraps every repository operation in `run_mongo_with_retry`, whose PostgreSQL
 * classifier (`astrabox/persistence/repository/postgresql/__init__.py`)
 * retries, among others, a statement that fails with SQLSTATE `40001`
 * (`asyncpg.exceptions.SerializationError`). There is no fault hook in that
 * path, and this fixture does not add one: it makes the database raise the
 * classified error on exact statements against the armed Session's own rows.
 *
 * Mechanism — row-level security on the document table, armed and disarmed in
 * one transaction each:
 *
 * - A `FOR SELECT` policy calls the fault function for every row a statement
 *   visits. The function raises `40001` on the first `readFailures` visits to
 *   the armed Session's `sessions` document (a plain `SELECT`, or the
 *   `SELECT … FOR UPDATE` that begins every mutation of that row).
 * - A `FOR UPDATE … WITH CHECK` policy calls the same function for every new
 *   row an `UPDATE` produces. It raises on the first `writeFailures` updates
 *   of the armed Session's `session_snapshots` document.
 * - `INSERT`/`DELETE` policies are unconditional so the product's other
 *   statements are unaffected. `FORCE ROW LEVEL SECURITY` is required because
 *   the server role owns the table; a superuser or `BYPASSRLS` role would never
 *   evaluate a policy, so the preflight refuses such a deployment loudly.
 *
 * Consumption ledger — one sequence per fault kind. The function advances the
 * sequence on every visit to the target row before deciding whether to raise,
 * and `nextval` is never rolled back, so the sequence survives the aborted
 * statement. `visits <= failures` are raised faults; a later visit is the
 * product reading or writing the same row again after the failure. The raised
 * message names the fault kind, its ordinal, the row and a per-run marker, so
 * the server log line `transient persistence error op=<op> attempt=<n>/<m>
 * err=…` written by the DAL's retry funnel identifies which repository
 * operation met the fault. `readMetadataFaultLogEvidence` extracts those lines.
 *
 * The fixture's own `psql` probes are exempt (`application_name = 'psql'`);
 * every other connection is subject to the fault. The function also stops
 * raising after `expiresAt`, so a test process killed before its `finally`
 * cannot leave the shared database raising; the next armed run removes any
 * leftover objects before arming again. While armed, non-leakproof JSON
 * pushdowns on the table cannot serve as index conditions, so reads of large
 * collections scan more rows than usual: keep the armed window short and run
 * the spec serially.
 */
import { spawnSync } from 'node:child_process';

import { postgresTextRows } from './dbOracle';
import {
  POSTGRES_CONTAINER_HANDLE,
  SERVER_CONTAINER_HANDLE,
  requireServiceContainer,
} from './serviceContainer';

export const METADATA_FAULT_TABLE = 'astrabox_documents';
export const METADATA_FAULT_FUNCTION = 'astrabox_e2e_metadata_fault';
export const METADATA_FAULT_READ_SEQUENCE = 'astrabox_e2e_metadata_fault_read_visits';
export const METADATA_FAULT_WRITE_SEQUENCE = 'astrabox_e2e_metadata_fault_write_visits';
export const METADATA_FAULT_POLICIES = [
  'astrabox_e2e_metadata_fault_select',
  'astrabox_e2e_metadata_fault_update',
  'astrabox_e2e_metadata_fault_insert',
  'astrabox_e2e_metadata_fault_delete',
] as const;
/** The SQLSTATE the product classifies as transient (`SerializationError`). */
export const METADATA_FAULT_SQLSTATE = '40001';
/** Statement text prefix of every raised fault, also present in the server log line. */
export const METADATA_FAULT_MESSAGE_PREFIX = 'astrabox-e2e metadata transient fault';

export type MetadataFaultKind = 'read' | 'write';

export interface MetadataFaultTarget {
  sessionId: string;
  /** Per-run token carried in every raised message; ties log lines to this test. */
  marker: string;
  readFailures: number;
  writeFailures: number;
  /** Absolute time after which the function stops raising, whatever remains unconsumed. */
  expiresAt: Date;
}

export interface MetadataFaultSurface {
  currentUser: string;
  superuser: boolean;
  bypassRls: boolean;
  tableOwner: string;
  rowSecurity: boolean;
  forceRowSecurity: boolean;
  policies: string[];
  leftoverFunction: boolean;
  leftoverSequences: string[];
}

export interface MetadataFaultCounter {
  /** Visits to the target row by the faulted statement kind, raised or not. */
  visits: number;
  /** Raised faults: `min(visits, failures)`. */
  raised: number;
  failures: number;
}

export interface MetadataFaultCounters {
  read: MetadataFaultCounter;
  write: MetadataFaultCounter;
}

export interface MetadataFaultHandle {
  target: MetadataFaultTarget;
  database: string;
  role: string;
  container: string;
  armedAt: Date;
  /** The surface before arming; restore is verified against it. */
  before: MetadataFaultSurface;
  /** Objects left behind by an earlier run and removed before arming. */
  leftoversRemoved: string[];
}

export interface MetadataFaultRetryWarning {
  line: string;
  /** The repository operation the DAL was retrying, e.g. `sessions.get_session`. */
  op: string;
  attempt: number;
  attempts: number;
  kind: MetadataFaultKind;
  ordinal: number;
  /** The row the database raised on, from the raised message: `sessions` or `session_snapshots`. */
  collection: string;
  sessionId: string;
}

export interface MetadataFaultLogEvidence {
  /** Lines carrying the marker that are the DAL's own transient-retry warning. */
  retryWarnings: MetadataFaultRetryWarning[];
  /** Lines carrying the marker that are anything else: the fault escaped the retry funnel. */
  otherLines: string[];
}

function postgresDatabase(): string {
  return process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox';
}

function postgresRole(): string {
  return process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox';
}

function sqlLiteral(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

function positiveCount(name: string, value: number): number {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`metadataTransientFault: ${name} must be a positive integer, got ${String(value)}`);
  }
  return value;
}

function runSql(container: string, sql: string, timeoutMs = 30_000): string[] {
  return postgresTextRows(container, postgresDatabase(), postgresRole(), sql, { timeoutMs });
}

function oneJsonRow<T>(container: string, sql: string, what: string): T {
  const rows = runSql(container, sql);
  if (rows.length !== 1) {
    throw new Error(
      `metadataTransientFault: ${what} returned ${rows.length} rows from ${postgresDatabase()} ` +
        `as ${postgresRole()} in container ${container}; expected exactly one`,
    );
  }
  return JSON.parse(rows[0]) as T;
}

const SURFACE_SQL = `SELECT json_build_object(
  'currentUser', current_user,
  'superuser', r.rolsuper,
  'bypassRls', r.rolbypassrls,
  'tableOwner', pg_get_userbyid(c.relowner),
  'rowSecurity', c.relrowsecurity,
  'forceRowSecurity', c.relforcerowsecurity,
  'policies', (
    SELECT coalesce(json_agg(p.policyname ORDER BY p.policyname), '[]'::json)
    FROM pg_policies p WHERE p.schemaname = n.nspname AND p.tablename = c.relname
  ),
  'leftoverFunction', to_regprocedure(${sqlLiteral(`${METADATA_FAULT_FUNCTION}(text,text,jsonb)`)}) IS NOT NULL,
  'leftoverSequences', (
    SELECT coalesce(json_agg(s.relname ORDER BY s.relname), '[]'::json)
    FROM pg_class s WHERE s.relkind = 'S' AND s.relname LIKE 'astrabox_e2e_metadata_fault%'
  )
)::text
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_roles r ON r.rolname = current_user
WHERE c.relname = ${sqlLiteral(METADATA_FAULT_TABLE)} AND c.relkind = 'r' AND pg_table_is_visible(c.oid);`;

/** The current RLS/ownership state of the document table as seen by the fixture role. */
export function readMetadataFaultSurface(container = requireServiceContainer(POSTGRES_CONTAINER_HANDLE)): MetadataFaultSurface {
  return oneJsonRow<MetadataFaultSurface>(container, SURFACE_SQL, `${METADATA_FAULT_TABLE} surface probe`);
}

function ownPolicies(surface: MetadataFaultSurface): string[] {
  return surface.policies.filter((name) => (METADATA_FAULT_POLICIES as readonly string[]).includes(name));
}

function foreignPolicies(surface: MetadataFaultSurface): string[] {
  return surface.policies.filter((name) => !(METADATA_FAULT_POLICIES as readonly string[]).includes(name));
}

function hasLeftovers(surface: MetadataFaultSurface): boolean {
  return ownPolicies(surface).length > 0 || surface.leftoverFunction || surface.leftoverSequences.length > 0;
}

function removalSql(): string {
  return [
    'BEGIN;',
    "SET LOCAL lock_timeout = '10s';",
    `ALTER TABLE ${METADATA_FAULT_TABLE} NO FORCE ROW LEVEL SECURITY, DISABLE ROW LEVEL SECURITY;`,
    ...METADATA_FAULT_POLICIES.map((name) => `DROP POLICY IF EXISTS ${name} ON ${METADATA_FAULT_TABLE};`),
    `DROP FUNCTION IF EXISTS ${METADATA_FAULT_FUNCTION}(text, text, jsonb);`,
    `DROP SEQUENCE IF EXISTS ${METADATA_FAULT_READ_SEQUENCE};`,
    `DROP SEQUENCE IF EXISTS ${METADATA_FAULT_WRITE_SEQUENCE};`,
    'COMMIT;',
  ].join('\n');
}

function describeSurface(surface: MetadataFaultSurface): string {
  return JSON.stringify(surface);
}

/**
 * Refuse a deployment on which the fault cannot be real or cannot be restored.
 *
 * The fixture role must own the table and be subject to policies; a foreign
 * policy or an already-enabled RLS state belongs to someone else and is never
 * touched. Leftovers with this fixture's own names are removed so an earlier
 * run killed before its `finally` does not leave the database faulted or block
 * this one; their names are returned for the test's evidence.
 */
function preflight(container: string): { before: MetadataFaultSurface; leftoversRemoved: string[] } {
  let surface = readMetadataFaultSurface(container);
  if (surface.superuser || surface.bypassRls) {
    throw new Error(
      `metadataTransientFault: role ${surface.currentUser} is superuser=${surface.superuser} ` +
        `bypassrls=${surface.bypassRls}; row-level security never evaluates for it, so no fault could reach ` +
        'the server. Point ASTRABOX_E2E_POSTGRES_USER at the non-superuser role the server connects with.',
    );
  }
  if (surface.tableOwner !== surface.currentUser) {
    throw new Error(
      `metadataTransientFault: ${METADATA_FAULT_TABLE} is owned by ${surface.tableOwner}, not by the fixture ` +
        `role ${surface.currentUser}; the fixture must run as the owning role to install and remove policies.`,
    );
  }
  const foreign = foreignPolicies(surface);
  if (foreign.length > 0) {
    throw new Error(
      `metadataTransientFault: ${METADATA_FAULT_TABLE} already carries policies this fixture does not own ` +
        `(${foreign.join(', ')}); refusing to arm on top of them. surface=${describeSurface(surface)}`,
    );
  }
  const leftoversRemoved: string[] = [];
  if (hasLeftovers(surface)) {
    leftoversRemoved.push(
      ...ownPolicies(surface).map((name) => `policy:${name}`),
      ...(surface.leftoverFunction ? [`function:${METADATA_FAULT_FUNCTION}`] : []),
      ...surface.leftoverSequences.map((name) => `sequence:${name}`),
    );
    runSql(container, removalSql());
    surface = readMetadataFaultSurface(container);
  }
  if (surface.rowSecurity || surface.forceRowSecurity || surface.policies.length > 0 || hasLeftovers(surface)) {
    throw new Error(
      `metadataTransientFault: ${METADATA_FAULT_TABLE} is not in the plain state this fixture restores to ` +
        `(no RLS, no policies); refusing to arm. surface=${describeSurface(surface)}`,
    );
  }
  return { before: surface, leftoversRemoved };
}

function faultFunctionSql(target: MetadataFaultTarget): string {
  const sessionId = sqlLiteral(target.sessionId);
  const marker = sqlLiteral(target.marker);
  const expires = sqlLiteral(target.expiresAt.toISOString());
  const message = (kind: MetadataFaultKind, collection: string) => sqlLiteral(
    `${METADATA_FAULT_MESSAGE_PREFIX} ${kind}#% ${collection}/% marker=%`,
  );
  return `CREATE FUNCTION ${METADATA_FAULT_FUNCTION}(op text, p_collection text, p_doc jsonb) RETURNS boolean
LANGUAGE plpgsql VOLATILE AS $fault$
DECLARE
  visit bigint;
BEGIN
  IF pg_catalog.current_setting('application_name', true) = 'psql' THEN
    RETURN true;
  END IF;
  IF pg_catalog.clock_timestamp() > ${expires}::timestamptz THEN
    RETURN true;
  END IF;
  IF op = 'read' AND p_collection = 'sessions' AND p_doc ->> 'session_id' = ${sessionId} THEN
    visit := nextval(${sqlLiteral(METADATA_FAULT_READ_SEQUENCE)});
    IF visit <= ${target.readFailures} THEN
      RAISE EXCEPTION ${message('read', 'sessions')}, visit, ${sessionId}, ${marker}
        USING ERRCODE = ${sqlLiteral(METADATA_FAULT_SQLSTATE)};
    END IF;
  ELSIF op = 'write' AND p_collection = 'session_snapshots' AND p_doc ->> 'session_id' = ${sessionId} THEN
    visit := nextval(${sqlLiteral(METADATA_FAULT_WRITE_SEQUENCE)});
    IF visit <= ${target.writeFailures} THEN
      RAISE EXCEPTION ${message('write', 'session_snapshots')}, visit, ${sessionId}, ${marker}
        USING ERRCODE = ${sqlLiteral(METADATA_FAULT_SQLSTATE)};
    END IF;
  END IF;
  RETURN true;
END
$fault$;`;
}

function armSql(target: MetadataFaultTarget): string {
  const table = METADATA_FAULT_TABLE;
  const fn = METADATA_FAULT_FUNCTION;
  return [
    'BEGIN;',
    "SET LOCAL lock_timeout = '10s';",
    `CREATE SEQUENCE ${METADATA_FAULT_READ_SEQUENCE};`,
    `CREATE SEQUENCE ${METADATA_FAULT_WRITE_SEQUENCE};`,
    faultFunctionSql(target),
    `CREATE POLICY ${METADATA_FAULT_POLICIES[0]} ON ${table} AS PERMISSIVE FOR SELECT USING (${fn}('read', collection, doc));`,
    `CREATE POLICY ${METADATA_FAULT_POLICIES[1]} ON ${table} AS PERMISSIVE FOR UPDATE USING (true) WITH CHECK (${fn}('write', collection, doc));`,
    `CREATE POLICY ${METADATA_FAULT_POLICIES[2]} ON ${table} AS PERMISSIVE FOR INSERT WITH CHECK (true);`,
    `CREATE POLICY ${METADATA_FAULT_POLICIES[3]} ON ${table} AS PERMISSIVE FOR DELETE USING (true);`,
    `ALTER TABLE ${table} ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;`,
    'COMMIT;',
  ].join('\n');
}

/**
 * Arm the fault for one Session. Preflight, leftover removal and the armed
 * DDL run as the fixture role inside the selected PostgreSQL container; the
 * arm itself is one transaction, so the table is either fully armed or
 * untouched. Callers must disarm in `finally`.
 */
export function armMetadataTransientFault(target: MetadataFaultTarget): MetadataFaultHandle {
  if (!target.sessionId.trim()) throw new Error('metadataTransientFault: sessionId is required');
  if (!/^[A-Za-z0-9._:-]+$/.test(target.marker)) {
    throw new Error(`metadataTransientFault: marker must be a plain token, got ${JSON.stringify(target.marker)}`);
  }
  positiveCount('readFailures', target.readFailures);
  positiveCount('writeFailures', target.writeFailures);
  if (!(target.expiresAt.getTime() > Date.now())) {
    throw new Error('metadataTransientFault: expiresAt must be in the future');
  }
  const container = requireServiceContainer(POSTGRES_CONTAINER_HANDLE);
  const { before, leftoversRemoved } = preflight(container);
  const armedAt = new Date();
  // From here on the table may be armed even when a step reports failure (a
  // psql process that died after COMMIT reports like one that never
  // committed), and no handle reaches the caller's `finally` until this
  // returns. Every failure path therefore removes the armed objects itself
  // before surfacing, and reports a removal failure alongside the cause.
  const failArmed = (cause: string): never => {
    let removal = '';
    try {
      runSql(container, removalSql());
    } catch (removeError) {
      removal = ` Removal after that failure also failed, so ${METADATA_FAULT_TABLE} may still be armed: ` +
        `${String((removeError as Error).message ?? removeError)}`;
    }
    throw new Error(`metadataTransientFault: ${cause}${removal}`);
  };
  try {
    runSql(container, armSql(target));
  } catch (error) {
    failArmed(`arm transaction failed: ${String((error as Error).message ?? error)}`);
  }
  let armed: MetadataFaultSurface | null = null;
  try {
    armed = readMetadataFaultSurface(container);
  } catch (error) {
    failArmed(`armed surface probe failed after the arm transaction: ${String((error as Error).message ?? error)}`);
  }
  const expectedPolicies = [...METADATA_FAULT_POLICIES].sort();
  if (
    !armed!.rowSecurity || !armed!.forceRowSecurity || !armed!.leftoverFunction
    || JSON.stringify(armed!.policies) !== JSON.stringify(expectedPolicies)
    || JSON.stringify(armed!.leftoverSequences) !== JSON.stringify(
      [METADATA_FAULT_READ_SEQUENCE, METADATA_FAULT_WRITE_SEQUENCE].sort(),
    )
  ) {
    failArmed(`arm transaction committed but the surface is not armed: ${describeSurface(armed!)}`);
  }
  return {
    target,
    database: postgresDatabase(),
    role: postgresRole(),
    container,
    armedAt,
    before,
    leftoversRemoved,
  };
}

const COUNTERS_SQL = `SELECT json_build_object(
  'read', json_build_object('last_value', r.last_value, 'is_called', r.is_called),
  'write', json_build_object('last_value', w.last_value, 'is_called', w.is_called)
)::text
FROM ${METADATA_FAULT_READ_SEQUENCE} r, ${METADATA_FAULT_WRITE_SEQUENCE} w;`;

interface RawSequenceState {
  last_value: number | string;
  is_called: boolean;
}

function counter(raw: RawSequenceState, failures: number): MetadataFaultCounter {
  const visits = raw.is_called ? Number(raw.last_value) : 0;
  return { visits, raised: Math.min(visits, failures), failures };
}

/** The consumption ledger while the fault is armed. */
export function readMetadataFaultCounters(handle: MetadataFaultHandle): MetadataFaultCounters {
  const raw = oneJsonRow<{ read: RawSequenceState; write: RawSequenceState }>(
    handle.container,
    COUNTERS_SQL,
    'fault counter probe',
  );
  return {
    read: counter(raw.read, handle.target.readFailures),
    write: counter(raw.write, handle.target.writeFailures),
  };
}

export interface MetadataFaultDisarmResult {
  /** The final ledger, read before the sequences were dropped; null when that read failed. */
  counters: MetadataFaultCounters | null;
  /** Why the final ledger could not be read; the removal ran regardless. */
  counterError: string | null;
  restored: MetadataFaultSurface;
}

/**
 * Remove every armed object and verify the table is back in its pre-arm state.
 *
 * The removal is unconditional: a failed counter read is reported in the
 * result, never allowed to skip the restore. A failed removal or a surface
 * that does not match the pre-arm state throws, because the shared table may
 * still be armed. Idempotent on an already-restored table (the counter read
 * then fails and is reported, the removal is a no-op, the verification passes).
 */
export function disarmMetadataTransientFault(handle: MetadataFaultHandle): MetadataFaultDisarmResult {
  let counters: MetadataFaultCounters | null = null;
  let counterError: string | null = null;
  try {
    counters = readMetadataFaultCounters(handle);
  } catch (error) {
    counterError = String((error as Error).message ?? error);
  }
  try {
    runSql(handle.container, removalSql());
  } catch (error) {
    throw new Error(
      `metadataTransientFault: removal failed and ${METADATA_FAULT_TABLE} may still be armed: ` +
        `${String((error as Error).message ?? error)}`,
    );
  }
  const restored = readMetadataFaultSurface(handle.container);
  const plain: MetadataFaultSurface = {
    ...restored,
    // Role attributes are not part of what the fixture changes.
    currentUser: handle.before.currentUser,
    superuser: handle.before.superuser,
    bypassRls: handle.before.bypassRls,
    tableOwner: handle.before.tableOwner,
  };
  if (JSON.stringify(plain) !== JSON.stringify(handle.before)) {
    throw new Error(
      `metadataTransientFault: ${METADATA_FAULT_TABLE} was not restored to its pre-arm state; ` +
        `before=${describeSurface(handle.before)} after=${describeSurface(restored)}`,
    );
  }
  return { counters, counterError, restored };
}

const DOCKER_TIMESTAMP = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z) (.*)$/;

/**
 * Server container log lines since `since`, both streams merged by the
 * daemon's per-line timestamp.
 *
 * `docker logs` hands stdout and stderr back on two descriptors, so their
 * concatenation does not preserve emission order across streams. With
 * `--timestamps` every line carries the daemon's nanosecond RFC 3339 stamp;
 * the merge sorts by that stamp (stably, so lines within one stream keep their
 * order) and strips it.
 */
export function serverLogLinesSince(since: Date): string[] {
  const container = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const result = spawnSync('docker', ['logs', '--timestamps', '--since', since.toISOString(), container], {
    encoding: 'utf8',
    timeout: 60_000,
    stdio: ['ignore', 'pipe', 'pipe'],
    maxBuffer: 256 * 1024 * 1024,
  });
  if (result.error || result.status !== 0) {
    throw new Error(
      `metadataTransientFault: docker logs --timestamps --since ${since.toISOString()} ${container} failed: ` +
        `${String(result.stderr || result.error?.message || `exit ${result.status}`).trim()}`,
    );
  }
  const stamped = `${result.stdout || ''}\n${result.stderr || ''}`
    .split('\n')
    .filter((line) => line.trim() !== '')
    .map((line) => {
      const match = DOCKER_TIMESTAMP.exec(line);
      return match ? { stamp: match[1], text: match[2] } : { stamp: '', text: line };
    });
  return stamped
    .sort((a, b) => (a.stamp < b.stamp ? -1 : a.stamp > b.stamp ? 1 : 0))
    .map((entry) => entry.text);
}

const RETRY_WARNING = /transient persistence error op=(\S+) attempt=(\d+)\/(\d+) err=/;
const FAULT_MESSAGE = new RegExp(`${METADATA_FAULT_MESSAGE_PREFIX} (read|write)#(\\d+) ([a-z_]+)/(\\S+) marker=`);

/**
 * Classify every server log line that carries the run marker.
 *
 * A line is a retry warning when it is the DAL's `transient persistence error`
 * record for a raised fault: it names the repository operation (`op=`), the
 * attempt, and the fault kind/ordinal from the raised message. Any other line
 * carrying the marker means the raised error reached some other logger — the
 * fault escaped the retry funnel — and is returned for the assertion to fail on.
 */
export function readMetadataFaultLogEvidence(lines: string[], marker: string): MetadataFaultLogEvidence {
  const needle = `marker=${marker}`;
  const evidence: MetadataFaultLogEvidence = { retryWarnings: [], otherLines: [] };
  for (const line of lines) {
    if (!line.includes(needle)) continue;
    const retry = RETRY_WARNING.exec(line);
    const fault = FAULT_MESSAGE.exec(line);
    if (!retry || !fault) {
      evidence.otherLines.push(line);
      continue;
    }
    evidence.retryWarnings.push({
      line,
      op: retry[1],
      attempt: Number(retry[2]),
      attempts: Number(retry[3]),
      kind: fault[1] as MetadataFaultKind,
      ordinal: Number(fault[2]),
      collection: fault[3],
      sessionId: fault[4],
    });
  }
  return evidence;
}
