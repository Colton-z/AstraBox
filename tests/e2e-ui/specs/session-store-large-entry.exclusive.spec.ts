/** One SessionStore entry larger than the historical transport envelope round-trips exactly. */
import { createHash } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, sqlLiteral } from '../fixtures/dbOracle';
import { parseTimeoutEnv } from '../fixtures/env';
import { requireSandboxHandle, sandboxExec } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import {
  enqueueLargeTranscriptEntry,
  type TranscriptScopeKey,
} from '../fixtures/transcriptStoreProbe';

const TURN_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
const ENTRY_PAYLOAD_BYTES = 17_000_000;
const EXPECTED_TOOL_RESULT = {
  durationSeconds: 1.8768265600000014,
  query: 'duration float fidelity',
  results: [],
  searchCount: 0,
};

/**
 * The session's main SessionStore transcripts that hold entries, keyed the way
 * the runner names them (`transcript_scopes`: project key and the runtime's own
 * session id, with an empty subpath for the main transcript).
 */
function mainTranscriptScopes(sessionId: string): TranscriptScopeKey[] {
  return postgresRows<{ project_key: string; native_session_id: string }>(
    `SELECT project_key, native_session_id FROM transcript_scopes `
      + `WHERE platform_session_id = ${sqlLiteral(sessionId)} AND subpath = '' AND last_sequence > 0 `
      + `ORDER BY scope_id`,
  ).map((row) => ({ project_key: row.project_key, session_id: row.native_session_id }));
}

/** One stored entry of the append `appendId`, with the session whose store holds it. */
interface AppendedEntry {
  platform_session_id: string;
  entry_json: string;
}

/** The entries one append stored, in whichever session's store holds them. */
function appendedEntries(appendId: string): AppendedEntry[] {
  return postgresRows<AppendedEntry>(
    `SELECT s.platform_session_id, e.entry_json FROM transcript_entries e `
      + `JOIN transcript_scopes s ON s.scope_id = e.scope_id `
      + `WHERE e.append_id = ${sqlLiteral(appendId)} ORDER BY e.scope_id, e.seq`,
  );
}

/** How many entries one append stored, without reading their bytes. */
function appendedEntryCount(appendId: string): number {
  const [row] = postgresRows<{ count: number }>(
    `SELECT count(*)::int AS count FROM transcript_entries WHERE append_id = ${sqlLiteral(appendId)}`,
  );
  return Number(row?.count ?? 0);
}

let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('SessionStore round-trips one 17MB entry through the scoped transcript repository', async ({
  request,
}) => {
  const api = new AstraApi(request);
  const marker = `LARGE_SESSION_STORE_${Date.now()}_${test.info().workerIndex}`;
  // The fixture writes the box-account runner's spool, so this case needs the
  // deployment's conversation-tenancy RuntimeProfile rather than the campaign
  // Agent's per-conversation account inside a shared box.
  const agent = await api.createColdTestAgent(`__e2e_large_session_store_${marker}`);
  agentId = agent.agent_id;
  const created = await api.startConversation(agentId);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  await api.waitForSessionReady(sessionId);
  const warmup = await api.sendTurn(
    sessionId,
    'E2E SessionStore probe warmup: do not use tools; reply briefly.',
    TURN_BUDGET_MS,
  );
  expect(warmup.errorText, 'the warmup must start the real engine process').toBeNull();
  const ready = await api.waitForSessionReady(sessionId);
  const sandboxId = String(ready.sandbox_id || '').trim();
  expect(sandboxId).not.toEqual('');
  const sandbox = await requireSandboxHandle(api, sandboxId);

  await expect.poll(
    () => mainTranscriptScopes(sessionId).length,
    {
      timeout: TURN_BUDGET_MS,
      message: 'the warmup must publish exactly one concrete main SessionStore scope',
    },
  ).toBe(1);
  const [mainScope] = mainTranscriptScopes(sessionId);

  const evidence = enqueueLargeTranscriptEntry(
    sandbox,
    mainScope,
    marker,
    ENTRY_PAYLOAD_BYTES,
  );
  const flush = await api.sendTurn(
    sessionId,
    'E2E SessionStore flush: do not use tools; reply briefly.',
    TURN_BUDGET_MS,
  );
  expect(flush.errorText, 'a real SDK append must wake and drain the resident spool').toBeNull();

  expect(evidence.batchBytes, 'the fsync-backed batch must exceed the 17MB entry').toBeGreaterThan(
    ENTRY_PAYLOAD_BYTES,
  );
  expect(evidence.entryPayloadBytes).toBe(ENTRY_PAYLOAD_BYTES);
  await expect.poll(
    () => appendedEntryCount(evidence.appendId),
    {
      timeout: TURN_BUDGET_MS,
      message: 'the real runner must flush the oversized append exactly once',
    },
  ).toBe(1);
  await expect.poll(
    () => sandboxExec(
      sandbox,
      `test -e ${evidence.batchPath} && echo present || echo absent`,
    ).trim(),
    {
      timeout: TURN_BUDGET_MS,
      message: 'the runner must unlink the batch after the platform acknowledges it',
    },
  ).toBe('absent');

  const stored = appendedEntries(evidence.appendId);
  expect(stored, 'the acknowledged append must remain unique in the database').toHaveLength(1);
  expect(stored[0].platform_session_id, 'the append must land in this conversation\'s store').toBe(sessionId);
  const storedEntry = JSON.parse(String(stored[0].entry_json)) as Record<string, unknown>;
  expect(storedEntry.toolUseResult, 'database storage must preserve the exact nested duration')
    .toEqual(EXPECTED_TOOL_RESULT);

  const exported = await api.adminSessionTranscript(sessionId, TURN_BUDGET_MS);
  const matchingLines = exported.split('\n').filter((line) => line.includes(marker));
  expect(matchingLines, 'the repository export must contain the oversized entry once').toHaveLength(1);
  const loaded = JSON.parse(matchingLines[0]) as Record<string, unknown>;
  const loadedPayload = String(loaded.payload || '');
  expect(Object.keys(loaded).sort()).toEqual(['marker', 'payload', 'toolUseResult', 'type']);
  expect(loaded.toolUseResult, 'native export must not round the stored duration')
    .toEqual(EXPECTED_TOOL_RESULT);
  expect(String(loaded.type || '')).toBe('e2e_large_entry');
  expect(String(loaded.marker || '')).toBe(marker);
  expect(Buffer.byteLength(loadedPayload, 'utf8')).toBe(ENTRY_PAYLOAD_BYTES);
  expect(createHash('sha256').update(loadedPayload).digest('hex')).toBe(
    evidence.entryPayloadSha256,
  );
});
