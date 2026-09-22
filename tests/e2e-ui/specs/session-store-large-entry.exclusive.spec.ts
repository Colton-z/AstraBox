/** One SessionStore entry larger than the historical transport envelope round-trips exactly. */
import { createHash } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentCountByField, documentsByField } from '../fixtures/dbOracle';
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

function mainTranscriptScopes(sessionId: string): TranscriptScopeKey[] {
  const scopes = new Map<string, TranscriptScopeKey>();
  for (const doc of documentsByField('transcript_entries', '$.platform_session_id', sessionId)) {
    const projectKey = String(doc.project_key || '').trim();
    const sdkSessionId = String(doc.session_id || '').trim();
    if (!projectKey || !sdkSessionId || doc.subpath != null) continue;
    scopes.set(`${projectKey}\u0000${sdkSessionId}`, {
      project_key: projectKey,
      session_id: sdkSessionId,
    });
  }
  return [...scopes.values()];
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
  // deployment's conversation-tenancy Environment rather than the campaign
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
    () => documentCountByField('transcript_entries', '$.append_id', evidence.appendId),
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

  const stored = documentsByField('transcript_entries', '$.append_id', evidence.appendId);
  expect(stored, 'the acknowledged append must remain unique in the database').toHaveLength(1);
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
