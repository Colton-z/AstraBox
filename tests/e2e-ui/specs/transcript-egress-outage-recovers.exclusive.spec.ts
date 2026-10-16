/**
 * A live response must survive a real sandbox-to-platform transcript outage.
 *
 * The response blocks in one Bash call after publishing its user prefix. The
 * spec denies only the transcript destination through the gated platform seam,
 * releases the suffix, and proves one fsync'd append stays in the runner spool
 * across a retry interval. The response still completes, and the output the
 * platform recorded for it replays. Restoring the exact old rule drains those
 * same append ids into one dense, duplicate-free mirror sequence.
 */
import { expect, test } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi, sseFrames, type InputReceipt } from '../fixtures/astraApi';
import {
  oracleDbPath,
  postgresRows,
  sessionOutputRows,
  sessionResponseRows,
  sqlLiteral,
} from '../fixtures/dbOracle';
import { absoluteBaseUrl, apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import {
  requireSandboxHandle,
  sandboxExec,
  setSandboxEgressFault,
  type SandboxEgressFaultHandle,
  type SandboxHandle,
} from '../fixtures/sandboxOps';
import { trackSessions } from '../fixtures/sessionCleanup';

const TURN_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
const RETRY_WITNESS_MS = 2_500;

/** One stored SessionStore entry (`transcript_entries`) with the session whose store holds it. */
interface MirrorDoc {
  append_id: string;
  batch_index: number;
  entry_json: string;
  platform_session_id: string;
  scope_id: number;
  seq: number;
}

interface SpoolBatch {
  appendId: string;
  entryCount: number;
  path: string;
}

const sessions = trackSessions();

/** The stored entries `where` selects, by their place in their transcript. */
function mirrorDocs(where: string): MirrorDoc[] {
  return postgresRows<MirrorDoc>(
    `SELECT e.append_id, e.batch_index, e.entry_json, s.platform_session_id, e.scope_id, e.seq `
      + `FROM transcript_entries e JOIN transcript_scopes s ON s.scope_id = e.scope_id `
      + `WHERE ${where} ORDER BY e.seq, e.scope_id`,
  );
}

function platformMirrorDocs(sessionId: string): MirrorDoc[] {
  return mirrorDocs(`s.platform_session_id = ${sqlLiteral(sessionId)}`);
}

function scopeMirrorDocs(scopeId: string): MirrorDoc[] {
  return mirrorDocs(`e.scope_id = ${Number(scopeId)}`);
}

function appendMirrorDocs(appendId: string): MirrorDoc[] {
  return mirrorDocs(`e.append_id = ${sqlLiteral(appendId)}`);
}

/** A stored entry's identity: its place in its transcript. */
function mirrorIdentity(doc: MirrorDoc): string {
  return `${doc.scope_id}:${doc.seq}`;
}

function mirrorEntry(doc: MirrorDoc): Record<string, unknown> {
  if (typeof doc.entry_json !== 'string') return {};
  try {
    const parsed = JSON.parse(doc.entry_json) as unknown;
    return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : {};
  } catch {
    return {};
  }
}

function entriesWithMarker(docs: MirrorDoc[], type: string, marker: string): MirrorDoc[] {
  return docs.filter((doc) => {
    const entry = mirrorEntry(doc);
    return String(entry.type || '') === type && JSON.stringify(entry).includes(marker);
  });
}

function assistantTextEntriesWithMarker(docs: MirrorDoc[], marker: string): MirrorDoc[] {
  return docs.filter((doc) => {
    const entry = mirrorEntry(doc);
    if (String(entry.type || '') !== 'assistant') return false;
    const message = entry.message;
    if (!message || typeof message !== 'object') return false;
    const content = (message as Record<string, unknown>).content;
    if (!Array.isArray(content)) return false;
    return content.some((block) => (
      Boolean(block)
      && typeof block === 'object'
      && String((block as Record<string, unknown>).type || '') === 'text'
      && String((block as Record<string, unknown>).text || '').includes(marker)
    ));
  });
}

function onlyScopeId(docs: MirrorDoc[], description: string): string {
  const ids = new Set(docs.map((doc) => String(doc.scope_id || '').trim()));
  if (ids.has('') || ids.size !== 1) {
    throw new Error(`${description} must have one scope_id; got ${JSON.stringify([...ids])}`);
  }
  return [...ids][0];
}

async function waitForMirror(
  read: () => MirrorDoc[],
  predicate: (docs: MirrorDoc[]) => boolean,
  description: string,
): Promise<MirrorDoc[]> {
  const deadline = Date.now() + TURN_BUDGET_MS;
  let last: MirrorDoc[] = [];
  while (Date.now() < deadline) {
    last = read();
    if (predicate(last)) return last;
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(
    `${description} within ${TURN_BUDGET_MS}ms; last=`
      + JSON.stringify(last.map((doc) => ({
        seq: doc.seq,
        scope_id: doc.scope_id,
        append_id: doc.append_id,
        batch_index: doc.batch_index,
        type: mirrorEntry(doc).type,
      }))),
  );
}

async function waitForWorkspaceFile(
  api: AstraApi,
  sessionId: string,
  path: string,
): Promise<boolean> {
  const deadline = Date.now() + TURN_BUDGET_MS;
  while (Date.now() < deadline) {
    const output = await api.runTerminalCommand(
      sessionId,
      `test -f ${path} && echo READY || true`,
      undefined,
      30_000,
    );
    if (output.includes('READY')) return true;
    // READY on the detail: nothing runs and no message is held, so the
    // command this waits for was never started.
    const session = await api.getSession(sessionId);
    if (session.state === 'READY') return false;
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  return false;
}

function blockingPrompt(
  userMarker: string,
  suffixMarker: string,
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
    "        raise RuntimeError('E2E egress release timed out')",
    '    time.sleep(0.1)',
    `print(${JSON.stringify(suffixMarker)})`,
    'PY',
    '```',
    'Do not use any other tool. Wait for Bash to finish.',
    `Then reply with exactly ${assistantMarker}.`,
  ].join('\n');
}

function spooledBatchesForMarker(sandbox: SandboxHandle, marker: string): SpoolBatch[] {
  const script = [
    "python3 - <<'PY'",
    'import json',
    'from pathlib import Path',
    `marker = ${JSON.stringify(marker)}`,
    "roots = [Path('/tmp/astrabox-runner-spool'), Path('/home')]",
    'found = []',
    'seen = set()',
    'for root in roots:',
    '    if not root.exists():',
    '        continue',
    "    for path in root.rglob('*.batch.json'):",
    '        key = str(path)',
    '        if key in seen:',
    '            continue',
    '        seen.add(key)',
    '        try:',
    "            raw = path.read_text(encoding='utf-8')",
    '        except FileNotFoundError:',
    '            continue',
    '        if marker not in raw:',
    '            continue',
    '        payload = json.loads(raw)',
    "        entries = payload.get('entries') or []",
    '        found.append({',
    "            'appendId': str(payload.get('append_id') or ''),",
    "            'entryCount': len(entries),",
    "            'path': key,",
    '        })',
    "print(json.dumps(sorted(found, key=lambda item: item['path'])))",
    'PY',
  ].join('\n');
  const raw = sandboxExec(sandbox, script, 30_000).trim();
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    throw new Error(
      `egress spool probe returned invalid JSON ${JSON.stringify(raw)}: ${(error as Error).message}`,
    );
  }
  if (!Array.isArray(parsed)) throw new Error('egress spool probe did not return an array');
  return parsed.map((item, index) => {
    if (!item || typeof item !== 'object') {
      throw new Error(`egress spool probe item ${index} is not an object`);
    }
    const record = item as Record<string, unknown>;
    const appendId = String(record.appendId || '').trim();
    const entryCount = Number(record.entryCount);
    const path = String(record.path || '').trim();
    if (!appendId || !Number.isInteger(entryCount) || entryCount <= 0 || !path) {
      throw new Error(`egress spool probe item ${index} is invalid: ${JSON.stringify(record)}`);
    }
    return { appendId, entryCount, path };
  });
}

async function waitForSpool(
  sandbox: SandboxHandle,
  marker: string,
  predicate: (batches: SpoolBatch[]) => boolean,
  description: string,
): Promise<SpoolBatch[]> {
  const deadline = Date.now() + TURN_BUDGET_MS;
  let last: SpoolBatch[] = [];
  while (Date.now() < deadline) {
    last = spooledBatchesForMarker(sandbox, marker);
    if (predicate(last)) return last;
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(`${description}; last spool=${JSON.stringify(last)}`);
}

test('transcript egress outage retries its spooled suffix and preserves replay frames', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = `${Date.now()}-${test.info().workerIndex}`;
  const userMarker = `TRANSCRIPT_EGRESS_USER_${runId}`;
  const suffixMarker = `TRANSCRIPT_EGRESS_SUFFIX_${runId}`;
  const assistantMarker = `TRANSCRIPT_EGRESS_DONE_${runId}`;
  const startedPath = `.astrabox-egress-${runId}.started`;
  const releasePath = `.astrabox-egress-${runId}.release`;

  // Conversation tenancy. The egress fault this spec installs is a property of
  // the BOX, so on the campaign Agent it denies transcript egress for every
  // conversation cohabiting there, and the spool this spec then reads holds
  // their files as well as its own -- which is what put the 30s probe over
  // `spawnSync kubectl ETIMEDOUT`.
  const agent = await api.createColdTestAgent(`__e2e_egress_outage_${Date.now()}`);
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });

  let fault: SandboxEgressFaultHandle | null = null;
  try {
    await api.waitForSessionReady(sessionId);
    await api.setPermissionMode(sessionId, 'bypassPermissions');
    expect(platformMirrorDocs(sessionId)).toEqual([]);

    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
    const assistantsBefore = await page.getByTestId('assistant-message').count();
    const outagePrompt = blockingPrompt(
      userMarker,
      suffixMarker,
      assistantMarker,
      startedPath,
      releasePath,
    );
    const inputPath = apiPath(`/sessions/${sessionId}/inputs`);
    const sent = page.waitForResponse((response) => (
      response.request().method() === 'POST' && new URL(response.url()).pathname === inputPath
    ), { timeout: 30_000 });
    await page.getByTestId('composer-prompt').fill(outagePrompt);
    await page.getByTestId('composer-submit').click();
    const receiptResponse = await sent;
    expect(receiptResponse.status(), 'the platform must accept the message').toBe(200);
    const receipts = [((await receiptResponse.json()) as { data: InputReceipt }).data];
    await expect(page.getByTestId('user-message').last()).toContainText(userMarker, {
      timeout: 30_000,
    });

    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. A declined ask re-sends the same prompt once, then fails.
    const toolStarted = await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) receipts.push(await api.postTurnInput(sessionId, outagePrompt));
      },
      probe: async () =>
        (await waitForWorkspaceFile(api, sessionId, startedPath)) ? true : null,
      what: 'the configured model did not enter the requested blocking Bash tool; ' + 'there is no live-turn window in which to cut transcript egress',
      budgetMs: 60_000 * 2,
      probeMs: 60_000,
    });

    // The response under test is the one running the blocked command: the
    // only response open now, answering one of the messages sent for it.
    const running = await api.getSession(sessionId);
    const open = (running.current_responses ?? []).map((response) => response.response_id);
    expect(open, 'exactly one response must be running the blocked command').toHaveLength(1);
    const responseId = open[0];
    const answered = sessionResponseRows(sessionId).find((row) => row.response_id === responseId);
    expect(
      receipts.some((receipt) => answered?.input_ids.includes(receipt.input_id)),
      `the running response must answer the message sent for it; response=${JSON.stringify(answered)}`,
    ).toBe(true);

    const prefixRows = await waitForMirror(
      () => platformMirrorDocs(sessionId),
      (docs) => entriesWithMarker(docs, 'user', userMarker).length === 1,
      'the live-turn user prefix did not reach the mirror',
    );
    const scopeId = onlyScopeId(
      entriesWithMarker(prefixRows, 'user', userMarker),
      'the live-turn user entry',
    );
    const prefix = scopeMirrorDocs(scopeId);
    const live = await api.getSession(sessionId);
    expect(
      (live.current_responses ?? []).map((response) => response.response_id),
      'the same response must still be running when the transcript destination is denied',
    ).toEqual([responseId]);
    const sandboxId = String(live.sandbox_id || '').trim();
    expect(sandboxId, 'the session must name its sandbox while the response runs').not.toEqual('');
    const sandbox = await requireSandboxHandle(api, sandboxId);
    await waitForSpool(
      sandbox,
      userMarker,
      (batches) => batches.length === 0,
      'the established prefix did not leave the fsync spool',
    );

    // The maintained deployment gives its sandbox callbacks and its E2E API
    // the same node-private origin. The already-observed mirror prefix proves
    // the runner is using that route; denying its host below is the
    // anti-vacuity check, because a wrong host leaves no batch in the spool.
    const transcriptHost = new URL(absoluteBaseUrl()).hostname;
    fault = await setSandboxEgressFault(api, sandboxId, transcriptHost);
    test.info().annotations.push({
      type: 'sandbox-egress-fault',
      description: `sandbox=${sandboxId} target=${transcriptHost}`,
    });

    await api.runTerminalCommand(sessionId, `touch ${releasePath}`, undefined, 30_000);
    const firstPending = await waitForSpool(
      sandbox,
      suffixMarker,
      (batches) => batches.length > 0,
      'the denied transcript suffix never appeared in the fsync spool',
    );
    await new Promise((resolve) => setTimeout(resolve, RETRY_WITNESS_MS));
    const retriedPending = spooledBatchesForMarker(sandbox, suffixMarker);
    const retriedIds = new Set(retriedPending.map((batch) => batch.appendId));
    expect(
      firstPending.every((batch) => retriedIds.has(batch.appendId)),
      'the same durable append id must remain queued across a flusher retry interval',
    ).toBe(true);

    const settled = await api.waitForSession(
      sessionId,
      (session) => (
        session.state === 'READY'
        && session.last_response?.response_id === responseId
      ),
      TURN_BUDGET_MS,
    );
    expect(settled.last_response?.outcome).toBe('completed');
    expect(String(settled.last_error || '').trim()).toEqual('');
    await expect
      .poll(() => page.getByTestId('assistant-message').count(), { timeout: 60_000 })
      .toBeGreaterThan(assistantsBefore);
    await expect(page.getByTestId('assistant-message').last()).toContainText(assistantMarker);

    const pendingAtRestore = spooledBatchesForMarker(sandbox, suffixMarker);
    expect(pendingAtRestore.length).toBeGreaterThan(0);
    expect(
      assistantTextEntriesWithMarker(scopeMirrorDocs(scopeId), assistantMarker),
      'while egress is denied the platform mirror must not already own the terminal suffix',
    ).toHaveLength(0);

    await fault.restore();
    await waitForSpool(
      sandbox,
      suffixMarker,
      (batches) => batches.length === 0,
      'restored egress did not drain the faulted spool batch',
    );
    for (const batch of pendingAtRestore) {
      const landed = await waitForMirror(
        () => appendMirrorDocs(batch.appendId),
        (docs) => docs.length === batch.entryCount,
        `spooled append ${batch.appendId} did not replay exactly once`,
      );
      expect(landed.map((doc) => Number(doc.batch_index))).toEqual(
        Array.from({ length: batch.entryCount }, (_unused, index) => index),
      );
      expect(new Set(landed.map(mirrorIdentity)).size).toBe(batch.entryCount);
      expect(onlyScopeId(landed, `spooled append ${batch.appendId}`)).toBe(scopeId);
    }

    const complete = await waitForMirror(
      () => scopeMirrorDocs(scopeId),
      (docs) => assistantTextEntriesWithMarker(docs, assistantMarker).length === 1,
      'the restored transcript tail did not converge',
    );
    expect(complete.map((doc) => Number(doc.seq))).toEqual(
      Array.from({ length: complete.length }, (_unused, index) => index + 1),
    );
    expect(complete.length).toBeGreaterThan(prefix.length);
    expect(entriesWithMarker(complete, 'user', userMarker)).toHaveLength(1);
    expect(assistantTextEntriesWithMarker(complete, assistantMarker)).toHaveLength(1);

    const recorded = sessionResponseRows(sessionId).find((row) => row.response_id === responseId);
    expect(
      recorded ? `${recorded.status}/${recorded.outcome ?? ''}` : 'missing',
      'the platform must record the response as completed',
    ).toBe('ended/completed');
    const durableFrames = sessionOutputRows(sessionId, responseId);
    const frameSeqs = durableFrames.map((frame) => Number(frame.seq));
    expect(frameSeqs.length, 'the platform must have recorded the response\'s output').toBeGreaterThan(0);
    expect(frameSeqs.every(Number.isFinite)).toBe(true);
    expect(frameSeqs).toEqual([...frameSeqs].sort((left, right) => left - right));
    expect(new Set(frameSeqs).size).toBe(frameSeqs.length);
    const frameTypes = durableFrames.map((frame) => String(frame.part.type || ''));
    expect(frameTypes.filter((type) => type === 'finish')).toHaveLength(1);

    // Replayed from just before the response's first recorded part: the
    // stream serves what was recorded after that position through this
    // response's end.
    const replay = await api.resumeStream(sessionId, frameSeqs[0] - 1);
    expect(
      sseFrames(replay.raw).filter((frame) => frame.type === 'start').map((frame) => frame.messageId),
      'the replay must carry the response that ran through the outage',
    ).toEqual([responseId]);
    expect(replay.frameTypes).toContain('finish');
    expect(replay.text.split(assistantMarker).length - 1).toBe(1);
  } finally {
    await api.runTerminalCommand(sessionId, `touch ${releasePath}`, undefined, 30_000).catch(() => {});
    if (fault?.active) await fault.restore();
  }
});
