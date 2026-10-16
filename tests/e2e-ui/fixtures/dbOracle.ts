/**
 * Database oracle for the community backend.
 *
 * The deployment stores everything in PostgreSQL tables. A session's runtime
 * facts live in typed tables (`session_inputs`, `session_responses`,
 * `session_output`, `session_interactions`, and the runtime transcripts in
 * `transcript_scopes` / `transcript_entries`), which the readers under
 * "Session runtime facts" read; `postgresRows` answers any other query. The
 * oracle executes `psql` inside the selected deployment database container,
 * so it observes the same committed data as the server without depending on a
 * host-mounted database file or a second client library.
 */
import { execFileSync, spawnSync } from 'node:child_process';

import {
  POSTGRES_CONTAINER_HANDLE,
  requireServiceContainer,
} from './serviceContainer';
import type { DockerResult, DockerRunner } from './serviceContainer';

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
export function sqlLiteral(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

/** Run a query and return each result row as a JSON object. */
export function postgresRows<T = Record<string, unknown>>(sql: string, timeoutMs = 20_000): T[] {
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

// ── Session runtime facts (typed tables) ──────────────────────────────────
//
// What the session's reader recorded, read from the tables it writes
// (`astrabox/persistence/sql/models/sessions.py`): the inputs the session
// accepted, the responses the runtime produced, the parts relayed for each,
// and the interactions a response waited on. These are the platform's own
// records, independent of the page and of the API projection under test.

/** One accepted input (`session_inputs`). */
export interface SessionInputRow {
  input_id: string;
  seq: number;
  client_message_id: string;
  author_user_id: string | null;
  source: string | null;
  content: string;
  content_blocks: unknown[] | null;
  /** `pending`, `delivered`, `admitted` or `failed`. */
  state: string;
  response_id: string | null;
  failure: Record<string, unknown> | null;
  created_at: string;
  delivered_at: string | null;
  admitted_at: string | null;
}

/** One response the runtime produced (`session_responses`), with the inputs it answers. */
export interface SessionResponseRow {
  response_id: string;
  seq: number;
  /** `input`, or `engine` for one the runtime started itself. */
  origin: string;
  /** `running`, `waiting` or `ended`. */
  status: string;
  /** `completed`, `failed` or `cancelled` once ended. */
  outcome: string | null;
  native_reason: string | null;
  usage: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  started_at: string;
  ended_at: string | null;
  input_ids: string[];
}

/** One relayed browser part (`session_output`). */
export interface SessionOutputRow {
  seq: number;
  response_id: string | null;
  part: Record<string, unknown>;
}

/** One approval or question a response waited on (`session_interactions`). */
export interface SessionInteractionRow {
  interaction_id: string;
  response_id: string;
  contract: Record<string, unknown>;
  /** `open`, `answered`, `withdrawn` or `expired`. */
  status: string;
  answer: Record<string, unknown> | null;
  answered_by: string | null;
  opened_at: string;
  answered_at: string | null;
  closed_at: string | null;
}

/** The inputs a session accepted, in acceptance order. */
export function sessionInputRows(sessionId: string): SessionInputRow[] {
  return postgresRows<SessionInputRow>(
    `SELECT input_id, seq, client_message_id, author_user_id, source, content, content_blocks, `
      + `state, response_id, failure, created_at, delivered_at, admitted_at `
      + `FROM session_inputs WHERE session_id = ${sqlLiteral(sessionId)} ORDER BY seq`,
  );
}

/** The responses a session's runtime produced, oldest first. */
export function sessionResponseRows(sessionId: string): SessionResponseRow[] {
  return postgresRows<SessionResponseRow>(
    `SELECT r.response_id, r.seq, r.origin, r.status, r.outcome, r.native_reason, r.usage, r.error, `
      + `r.started_at, r.ended_at, COALESCE((SELECT array_agg(ri.input_id ORDER BY ri.input_id) `
      + `FROM session_response_inputs ri WHERE ri.session_id = r.session_id `
      + `AND ri.response_id = r.response_id), ARRAY[]::text[]) AS input_ids `
      + `FROM session_responses r WHERE r.session_id = ${sqlLiteral(sessionId)} ORDER BY r.seq`,
  );
}

/** The parts relayed for a session, in order; only one response's when `responseId` is given. */
export function sessionOutputRows(sessionId: string, responseId?: string): SessionOutputRow[] {
  const response = responseId === undefined ? '' : ` AND response_id = ${sqlLiteral(responseId)}`;
  return postgresRows<SessionOutputRow>(
    `SELECT seq, response_id, part FROM session_output `
      + `WHERE session_id = ${sqlLiteral(sessionId)}${response} ORDER BY seq`,
  );
}

/** The interactions a session's responses waited on, in the order they opened. */
export function sessionInteractionRows(sessionId: string): SessionInteractionRow[] {
  return postgresRows<SessionInteractionRow>(
    `SELECT interaction_id, response_id, contract, status, answer, answered_by, opened_at, `
      + `answered_at, closed_at FROM session_interactions `
      + `WHERE session_id = ${sqlLiteral(sessionId)} ORDER BY opened_at, interaction_id`,
  );
}

/**
 * One entry of a runtime transcript (`transcript_entries`) with the file it
 * belongs to (`transcript_scopes`). `subpath` is `null` for the main
 * transcript and the sub-file's path for a sub-agent's; `entry_json` is the
 * entry as the runtime wrote it, and `uuid` is that entry's own `uuid` field
 * when it has one (the table keeps the entry, not a column for it).
 */
export interface NativeTranscriptRow {
  seq: number;
  uuid: string | null;
  entry_json: string;
  subpath: string | null;
  native_session_id: string;
  project_key: string;
}

/**
 * A session's runtime transcripts, the main one first, each file in the order
 * the runtime appended to it. This is the supplier's own record, independent
 * of what the platform projected from it.
 *
 * The table keeps each entry only as the JSON text the runtime sent
 * (`persistence/repository/transcript_entry_repository.py`), so an entry's
 * `uuid` is read from that text rather than from a column.
 */
export function nativeTranscriptRows(sessionId: string): NativeTranscriptRow[] {
  return postgresRows<Omit<NativeTranscriptRow, 'uuid'>>(
    `SELECT e.seq, e.entry_json, NULLIF(s.subpath, '') AS subpath, s.native_session_id, `
      + `s.project_key FROM transcript_entries e JOIN transcript_scopes s ON s.scope_id = e.scope_id `
      + `WHERE s.platform_session_id = ${sqlLiteral(sessionId)} `
      + `ORDER BY (s.subpath <> ''), s.subpath, e.seq`,
  ).map((row) => ({ ...row, uuid: entryUuid(row.entry_json) }));
}

/** An entry's own `uuid`, or `null` for an entry that carries none. Every stored entry is JSON. */
function entryUuid(entryJson: string): string | null {
  const entry: unknown = JSON.parse(entryJson);
  const uuid = entry && typeof entry === 'object' ? (entry as { uuid?: unknown }).uuid : undefined;
  return typeof uuid === 'string' && uuid ? uuid : null;
}

/** Effective database target in use (kept under the existing diagnostic API). */
export function oracleDbPath(): string {
  return `postgresql://${postgresContainerName()}/${process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox'}`;
}
