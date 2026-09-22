/**
 * A real in-box transcript producer must not lose a batch when the host append
 * endpoint is temporarily unavailable.
 *
 * The turn is held inside one Bash call while its user prefix reaches the
 * mirror. The test then arms three host-side 503 responses, releases the turn,
 * and proves the same persisted append_id was rejected three times before its
 * whole batch landed exactly once. Dense store sequences plus the final user
 * and assistant entries prove the rest of the turn stayed complete and ordered.
 *
 * The server must run with ASTRABOX_E2E_FAULTS=1 and share the configured fault
 * directory with the Playwright host. Without that deployment contract the
 * anti-vacuity assertion fails with the exact path that needs wiring.
 */
import fs from 'node:fs';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField, oracleDbPath } from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';

const TURN_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
const MIRROR_BUDGET_MS = TURN_BUDGET_MS;
const FAULT_FAILURE_COUNT = 3;

// This is the existing shared fault declaration channel. The backend reads the
// base path and `<base>.d/*.json`; each spec owns one file under the directory.
const FAULT_BASE = (
  process.env.ASTRABOX_E2E_TURN_TERMINAL_DROP_FAULT_FILE
  || '/tmp/astrabox-e2e-turn-terminal-drop-faults.json'
).trim();
const FAULT_DIR = `${FAULT_BASE}.d`;

interface TranscriptAppendConsumption {
  fault: string;
  session_id: string;
  append_id: string;
  entry_count: number;
}

interface TranscriptAppendFaultState {
  remaining: number;
  consumed: TranscriptAppendConsumption[];
}

interface MirrorDoc extends Record<string, unknown> {
  _id?: string;
  append_id?: string;
  batch_index?: number;
  entry_json?: string;
  platform_session_id?: string;
  scope_id?: string;
  seq?: number;
  subpath?: string | null;
}

type DocumentReader = typeof documentsByField;

interface MirrorWaitClock {
  now: () => number;
  sleep: (delayMs: number) => Promise<void>;
}

const systemMirrorWaitClock: MirrorWaitClock = {
  now: () => Date.now(),
  sleep: (delayMs) => new Promise((resolve) => setTimeout(resolve, delayMs)),
};

const sessions = trackSessions();

function faultFilePathFor(sessionId: string): string {
  const slug = `${test.info().title} ${sessionId}`
    .replace(/[^a-zA-Z0-9_.-]+/g, '-')
    .replace(/^-|-$/g, '');
  return path.join(FAULT_DIR, `w${test.info().workerIndex}-${process.pid}-${slug}.json`);
}

function armTranscriptAppendFault(faultPath: string, sessionId: string): void {
  fs.mkdirSync(FAULT_DIR, { recursive: true });
  // The Playwright host and backend container commonly use different uids. The
  // backend atomically rewrites the declaration after every consumed failure.
  fs.chmodSync(FAULT_DIR, 0o777);
  fs.writeFileSync(
    faultPath,
    JSON.stringify({
      faults: { transcript_append_5xx: FAULT_FAILURE_COUNT },
      match: { session_id: sessionId },
    }),
    'utf8',
  );
  // The controller deliberately runs with a restrictive umask. Make the
  // declaration readable by the backend's different container uid; atomic
  // write-back needs the writable directory above, not a writable file.
  fs.chmodSync(faultPath, 0o644);
}

function readTranscriptAppendFault(faultPath: string): TranscriptAppendFaultState {
  if (!fs.existsSync(faultPath)) return { remaining: -1, consumed: [] };
  try {
    const payload = JSON.parse(fs.readFileSync(faultPath, 'utf8')) as {
      faults?: { transcript_append_5xx?: unknown };
      consumed?: unknown;
    };
    const rawRemaining = Number(payload.faults?.transcript_append_5xx);
    const consumed = Array.isArray(payload.consumed)
      ? payload.consumed.filter((item): item is TranscriptAppendConsumption => (
        typeof item === 'object' && item !== null
        && String((item as Record<string, unknown>).fault || '') === 'transcript_append_5xx'
      ))
      : [];
    return {
      remaining: Number.isFinite(rawRemaining) ? rawRemaining : -1,
      consumed,
    };
  } catch {
    return { remaining: -1, consumed: [] };
  }
}

function clearTranscriptAppendFault(faultPath: string): void {
  try {
    if (fs.existsSync(faultPath)) fs.rmSync(faultPath, { force: true });
  } catch {
    // Best-effort teardown of this spec's declaration only.
  }
}

function sortedMirrorDocs(docs: Record<string, unknown>[]): MirrorDoc[] {
  return docs
    .map((doc) => doc as MirrorDoc)
    .sort((left, right) => Number(left.seq || 0) - Number(right.seq || 0));
}

function platformMirrorDocs(
  sessionId: string,
  readDocuments: DocumentReader = documentsByField,
): MirrorDoc[] {
  return sortedMirrorDocs(
    readDocuments('transcript_entries', '$.platform_session_id', sessionId),
  );
}

function appendMirrorDocs(
  appendId: string,
  readDocuments: DocumentReader = documentsByField,
): MirrorDoc[] {
  return sortedMirrorDocs(
    readDocuments('transcript_entries', '$.append_id', appendId),
  );
}

function scopeMirrorDocs(
  scopeId: string,
  readDocuments: DocumentReader = documentsByField,
): MirrorDoc[] {
  return sortedMirrorDocs(
    readDocuments('transcript_entries', '$.scope_id', scopeId),
  );
}

function mirrorEntry(doc: MirrorDoc): Record<string, unknown> {
  if (typeof doc.entry_json !== 'string') return {};
  try {
    const parsed = JSON.parse(doc.entry_json) as unknown;
    return typeof parsed === 'object' && parsed !== null
      ? parsed as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

function visibleMessageText(entry: Record<string, unknown>): string {
  const message = entry.message;
  if (typeof message !== 'object' || message === null || Array.isArray(message)) return '';
  const content = (message as Record<string, unknown>).content;
  const blocks = typeof content === 'string' ? [content] : content;
  if (!Array.isArray(blocks)) return '';
  return blocks.flatMap((block) => {
    if (typeof block === 'string') return [block];
    if (typeof block !== 'object' || block === null || Array.isArray(block)) return [];
    const record = block as Record<string, unknown>;
    return record.type === 'text' && typeof record.text === 'string' ? [record.text] : [];
  }).join('\n');
}

function entriesWithVisibleTextMarker(
  docs: MirrorDoc[],
  type: string,
  marker: string,
): MirrorDoc[] {
  return docs.filter((doc) => {
    const entry = mirrorEntry(doc);
    return String(entry.type || '') === type && visibleMessageText(entry).includes(marker);
  });
}

function onlyScopeId(docs: MirrorDoc[], description: string): string {
  const scopeIds = new Set(docs.map((doc) => String(doc.scope_id || '').trim()));
  if (scopeIds.has('') || scopeIds.size !== 1) {
    throw new Error(
      `${description} must belong to one stored transcript scope; scope_ids=`
        + JSON.stringify([...scopeIds]),
    );
  }
  return [...scopeIds][0];
}

function mirrorForFaultedBatch(
  platformSessionId: string,
  faultedBatch: MirrorDoc[],
  readDocuments: DocumentReader = documentsByField,
): { scopeId: string; docs: MirrorDoc[] } {
  // platform_session_id is the tenant fence, not a sequence domain. The fault's
  // append_id identifies the batch; its scope_id identifies the ordered mirror.
  const platformSessionIds = new Set(
    faultedBatch.map((doc) => String(doc.platform_session_id || '').trim()),
  );
  if (
    platformSessionIds.has('')
    || platformSessionIds.size !== 1
    || !platformSessionIds.has(platformSessionId)
  ) {
    throw new Error(
      `the faulted batch must belong to platform session ${platformSessionId}; `
        + `platform_session_ids=${JSON.stringify([...platformSessionIds])}`,
    );
  }
  const scopeId = onlyScopeId(faultedBatch, 'the faulted append batch');
  return { scopeId, docs: scopeMirrorDocs(scopeId, readDocuments) };
}

async function waitForMirror(
  readMirror: () => MirrorDoc[],
  predicate: (docs: MirrorDoc[]) => boolean,
  description: string,
  clock: MirrorWaitClock = systemMirrorWaitClock,
): Promise<MirrorDoc[]> {
  const deadline = clock.now() + MIRROR_BUDGET_MS;
  let last: MirrorDoc[] = [];
  while (clock.now() < deadline) {
    last = readMirror();
    if (predicate(last)) return last;
    await clock.sleep(1_000);
  }
  throw new Error(
    `${description} within ${MIRROR_BUDGET_MS}ms; last mirror=`
      + JSON.stringify(last.map((doc) => ({
        seq: doc.seq,
        scope_id: doc.scope_id,
        append_id: doc.append_id,
        batch_index: doc.batch_index,
        subpath: doc.subpath,
        type: mirrorEntry(doc).type,
      }))),
  );
}

test('mirror oracle follows the exact sequence scope through turn-budget convergence', async () => {
  const platformSessionId = 'platform-session';
  const appendId = 'faulted-append';
  const scopeId = 'main-scope';
  const marker = 'TERMINAL_ASSISTANT_MARKER';
  const otherScope: MirrorDoc[] = [
    { seq: 1, scope_id: 'queue-scope', platform_session_id: platformSessionId, entry_json: JSON.stringify({ type: 'queue-operation' }) },
    { seq: 2, scope_id: 'queue-scope', platform_session_id: platformSessionId, entry_json: JSON.stringify({ type: 'queue-operation' }) },
  ];
  const complete: MirrorDoc[] = [
    { seq: 1, scope_id: scopeId, platform_session_id: platformSessionId, append_id: 'prefix', batch_index: 0, entry_json: JSON.stringify({ type: 'user', message: { role: 'user', content: 'prompt' } }) },
    { seq: 2, scope_id: scopeId, platform_session_id: platformSessionId, append_id: appendId, batch_index: 0, entry_json: JSON.stringify({ type: 'assistant', message: { role: 'assistant', content: [{ type: 'tool_use', input: { command: `print(${marker})` } }], stop_reason: 'tool_use' } }) },
    { seq: 3, scope_id: scopeId, platform_session_id: platformSessionId, append_id: appendId, batch_index: 1, entry_json: JSON.stringify({ type: 'user' }) },
    { seq: 4, scope_id: scopeId, platform_session_id: platformSessionId, append_id: 'terminal', batch_index: 0, entry_json: JSON.stringify({ type: 'assistant', message: { role: 'assistant', content: [{ type: 'text', text: marker }], stop_reason: 'end_turn' } }) },
  ];
  const readDocuments: DocumentReader = (_collection, jsonPath, value) => {
    if (jsonPath === '$.platform_session_id' && value === platformSessionId) {
      return [...otherScope, ...complete];
    }
    if (jsonPath === '$.append_id' && value === appendId) {
      return complete.filter((doc) => doc.append_id === appendId);
    }
    if (jsonPath === '$.scope_id' && value === scopeId) return complete;
    return [];
  };

  expect(
    new Set(platformMirrorDocs(platformSessionId, readDocuments).map((doc) => doc.scope_id)),
    'a platform session is a tenant boundary and can contain multiple sequence scopes',
  ).toEqual(new Set(['queue-scope', scopeId]));
  const faultedBatch = appendMirrorDocs(appendId, readDocuments);
  expect(faultedBatch).toHaveLength(2);
  const { docs: mirror } = mirrorForFaultedBatch(
    platformSessionId,
    faultedBatch,
    readDocuments,
  );
  expect(mirror.map((doc) => Number(doc.seq))).toEqual([1, 2, 3, 4]);
  expect(
    mirror.filter((doc) => JSON.stringify(mirrorEntry(doc)).includes(marker)),
    'the fixture must contain the real collision between tool input and visible assistant text',
  ).toHaveLength(2);
  expect(entriesWithVisibleTextMarker(mirror, 'assistant', marker)).toHaveLength(1);

  expect(
    MIRROR_BUDGET_MS,
    'an asynchronously flushed terminal tail gets the same convergence window as its turn',
  ).toBe(TURN_BUDGET_MS);
  const terminalArrivalMs = Math.max(0, TURN_BUDGET_MS - 30_000);
  let elapsedMs = 0;
  const delayedMirror = await waitForMirror(
    () => (elapsedMs >= terminalArrivalMs ? mirror : mirror.slice(0, -1)),
    (docs) => entriesWithVisibleTextMarker(docs, 'assistant', marker).length === 1,
    'the terminal assistant entry did not converge within the turn budget',
    {
      now: () => elapsedMs,
      sleep: async (delayMs) => { elapsedMs += delayMs; },
    },
  );
  expect(entriesWithVisibleTextMarker(delayedMirror, 'assistant', marker)).toHaveLength(1);
  expect(elapsedMs).toBeGreaterThanOrEqual(terminalArrivalMs);
  expect(elapsedMs).toBeLessThan(MIRROR_BUDGET_MS);
});

async function waitForSandboxFile(
  api: AstraApi,
  sessionId: string,
  filePath: string,
): Promise<boolean> {
  const deadline = Date.now() + TURN_BUDGET_MS;
  while (Date.now() < deadline) {
    const output = await api.runTerminalCommand(
      sessionId,
      `test -f ${filePath} && echo READY || true`,
      '/workspace',
      30_000,
    );
    if (output.includes('READY')) return true;
    const session = await api.getSession(sessionId);
    if (!String(session.current_turn_id || '').trim() && session.state === 'READY') return false;
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  return false;
}

function blockingPrompt(
  userMarker: string,
  assistantMarker: string,
  startedPath: string,
  releasePath: string,
): string {
  return [
    userMarker,
    'Use the Bash tool exactly once to run this command verbatim:',
    '```bash',
    "python3 - <<'PY'",
    'import time',
    'from pathlib import Path',
    `started = Path(${JSON.stringify(startedPath)})`,
    `release = Path(${JSON.stringify(releasePath)})`,
    "started.write_text('started', encoding='utf-8')",
    'deadline = time.monotonic() + 300',
    'while not release.is_file():',
    '    if time.monotonic() >= deadline:',
    "        raise RuntimeError('E2E transcript append release timed out')",
    '    time.sleep(0.1)',
    `print(${JSON.stringify(assistantMarker)})`,
    'PY',
    '```',
    'Do not use any other tool. Wait for Bash to finish.',
    `Then reply with exactly ${assistantMarker}.`,
  ].join('\n');
}

test('transient transcript append failures retry the same batch into a complete mirror', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = `${Date.now()}-${test.info().workerIndex}`;
  const userMarker = `TRANSCRIPT_APPEND_RETRY_USER_${runId}`;
  const assistantMarker = `TRANSCRIPT_APPEND_RETRY_DONE_${runId}`;
  const startedPath = `/workspace/.astrabox-e2e-transcript-append-retry-${runId}.started`;
  const releasePath = `/workspace/.astrabox-e2e-transcript-append-retry-${runId}.release`;

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const faultPath = faultFilePathFor(sessionId);
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });
  test.info().annotations.push({ type: 'e2e_transcript_append_fault_file', description: faultPath });

  try {
    await api.waitForSessionReady(sessionId);
    await api.setPermissionMode(sessionId, 'bypassPermissions');
    expect(
      platformMirrorDocs(sessionId),
      'a fresh conversation must have no earlier mirror rows that could satisfy this turn',
    ).toEqual([]);

    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
    const assistantsBefore = await page.getByTestId('assistant-message').count();
    const prompt = blockingPrompt(userMarker, assistantMarker, startedPath, releasePath);
    await page.getByTestId('composer-prompt').fill(prompt);
    await page.getByTestId('composer-submit').click();
    const queuedPrompt = page.getByTestId('composer-queue').filter({ hasText: userMarker });
    const userMessage = page.getByTestId('user-message').filter({ hasText: userMarker });
    await expect(
      queuedPrompt.or(userMessage).first(),
      'the accepted input remains visible in the queue or transcript',
    ).toBeVisible({ timeout: 30_000 });

    const running = await api.waitForSession(
      sessionId,
      (session) => Boolean(String(session.current_turn_id || '').trim()),
      60_000,
    );
    const turnId = String(running.current_turn_id || '').trim();
    const toolStarted = await waitForSandboxFile(api, sessionId, startedPath);
    expect(
      toolStarted,
      'the configured model did not enter the requested blocking Bash tool; '
        + 'there is no live-turn window in which to arm transcript append failures',
    ).toBe(true);

    // Establish a complete prefix first. The Bash process keeps the real turn
    // live, so every append after this point belongs to the released suffix.
    const platformPrefix = await waitForMirror(
      () => platformMirrorDocs(sessionId),
      (docs) => entriesWithVisibleTextMarker(docs, 'user', userMarker).length === 1,
      'the live turn user prefix did not reach the mirror',
    );
    const prefixScopeId = onlyScopeId(
      entriesWithVisibleTextMarker(platformPrefix, 'user', userMarker),
      'the live turn user entry',
    );
    const prefix = scopeMirrorDocs(prefixScopeId);
    expect(entriesWithVisibleTextMarker(prefix, 'user', userMarker)).toHaveLength(1);
    const stillRunning = await api.getSession(sessionId);
    expect(String(stillRunning.current_turn_id || '').trim()).toBe(turnId);

    armTranscriptAppendFault(faultPath, sessionId);
    await api.runTerminalCommand(sessionId, `touch ${releasePath}`, '/workspace', 30_000);

    const settled = await api.waitForSession(
      sessionId,
      (session) => (
        session.state === 'READY'
        && !String(session.current_turn_id || '').trim()
        && String(session.last_turn_id || '').trim() === turnId
      ),
      TURN_BUDGET_MS,
    );
    expect(settled.last_turn_status).toBe('COMPLETED');
    expect(String(settled.last_error || '').trim()).toEqual('');
    await expect(
      userMessage,
      'engine consumption hands the input from the queue to one transcript row',
    ).toHaveCount(1, { timeout: 60_000 });
    await expect(
      queuedPrompt,
      'the queue stops owning the input after the transcript receives it',
    ).toHaveCount(0, { timeout: 60_000 });
    await expect
      .poll(() => page.getByTestId('assistant-message').count(), { timeout: 60_000 })
      .toBeGreaterThan(assistantsBefore);
    await expect(page.getByTestId('assistant-message').last()).toContainText(assistantMarker);

    let faultState: TranscriptAppendFaultState = { remaining: -1, consumed: [] };
    await expect.poll(
      () => {
        faultState = readTranscriptAppendFault(faultPath);
        return [faultState.remaining, faultState.consumed.length];
      },
      {
        timeout: MIRROR_BUDGET_MS,
        message: 'the real host transcript append route must consume all three 503 faults — '
          + `start the server with ASTRABOX_E2E_FAULTS=1 and make ${FAULT_DIR} a shared `
          + 'Playwright-host↔backend-container mount',
      },
    ).toEqual([0, FAULT_FAILURE_COUNT]);

    const consumed = faultState.consumed;
    expect(new Set(consumed.map((item) => item.session_id))).toEqual(new Set([sessionId]));
    expect(new Set(consumed.map((item) => item.fault))).toEqual(
      new Set(['transcript_append_5xx']),
    );
    const failedAppendIds = new Set(consumed.map((item) => item.append_id).filter(Boolean));
    expect(
      failedAppendIds.size,
      'the serial spool must retry one blocked batch until it lands before advancing',
    ).toBe(1);
    const failedAppendId = [...failedAppendIds][0];
    const entryCounts = new Set(consumed.map((item) => Number(item.entry_count)));
    expect(entryCounts.size).toBe(1);
    const failedEntryCount = [...entryCounts][0];
    expect(failedEntryCount).toBeGreaterThan(0);

    const failedBatch = await waitForMirror(
      () => appendMirrorDocs(failedAppendId),
      (docs) => {
        if (docs.length > failedEntryCount) {
          throw new Error(
            `faulted append ${failedAppendId} stored ${docs.length} rows; `
              + `expected exactly ${failedEntryCount}`,
          );
        }
        return docs.length === failedEntryCount;
      },
      'the faulted append batch did not reach the mirror',
    );
    const faultedMirror = () => mirrorForFaultedBatch(sessionId, failedBatch);
    const failedScopeId = faultedMirror().scopeId;
    expect(
      failedScopeId,
      'the retried batch must land in the same transcript scope as the live-turn prefix',
    ).toBe(prefixScopeId);

    const complete = await waitForMirror(
      () => faultedMirror().docs,
      (docs) => entriesWithVisibleTextMarker(docs, 'assistant', assistantMarker).length === 1,
      'the terminal assistant entry did not reach the faulted batch scope',
    );

    // Store sequence is the durable ordering authority. A complete main scope
    // is exactly 1..N: no missing middle and no duplicate position.
    expect(complete.map((doc) => Number(doc.seq))).toEqual(
      Array.from({ length: complete.length }, (_unused, index) => index + 1),
    );
    expect(complete.length).toBeGreaterThan(prefix.length);
    const signature = (doc: MirrorDoc): string => JSON.stringify([
      doc.seq, doc.append_id, doc.batch_index, doc.entry_json,
    ]);
    expect(complete.slice(0, prefix.length).map(signature)).toEqual(prefix.map(signature));
    expect(entriesWithVisibleTextMarker(complete, 'user', userMarker)).toHaveLength(1);
    expect(entriesWithVisibleTextMarker(complete, 'assistant', assistantMarker)).toHaveLength(1);

    const completeFailedBatch = complete.filter((doc) => doc.append_id === failedAppendId);
    expect(completeFailedBatch).toHaveLength(failedEntryCount);
    expect(completeFailedBatch.map((doc) => Number(doc.batch_index))).toEqual(
      Array.from({ length: failedEntryCount }, (_unused, index) => index),
    );
    expect(new Set(completeFailedBatch.map((doc) => doc._id)).size).toBe(failedEntryCount);
  } finally {
    clearTranscriptAppendFault(faultPath);
    await api.runTerminalCommand(
      sessionId,
      `touch ${releasePath}`,
      '/workspace',
      30_000,
    ).catch(() => {});
    // trackSessions decides after the result whether to delete or preserve it.
  }
});
