/**
 * E2E: an expired runner cursor rebuilds history once, then resumes once.
 *
 * The spec restarts its image-baked runner with a one-envelope compaction
 * threshold, then completes one real SDK turn. It observes the store-covered
 * journal compaction before evicting only the host runtime. The browser restores
 * the completed prefix from SessionStore before opening its idle subscription.
 * The resident receiver consumes the expired runner cursor independently; a
 * prefix already restored by bootstrap requires no second history rebuild.
 * The next turn must preserve those records and converge at its durable result.
 */
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { documentsByField, sessionEvents, waitForTurnTerminalProof } from '../fixtures/dbOracle';
import { appPath } from '../fixtures/env';
import { observedJournalRunnerLaunch, readCompactedRunnerJournal } from '../fixtures/runnerJournal';
import {
  IMAGE_RUNNER_PORT,
  imageRunnerRestartScript,
} from '../fixtures/runnerRestart';
import { requireSandboxHandle, sandboxExec, type SandboxHandle } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});
const RUNNER_RESTART_LOG = '/tmp/astrabox-gap-rebuild-runner.log';
const JOURNAL_COMPACTION_THRESHOLD = 1;
const JOURNAL_COMPACTION_LOG = 'runner journal compacted:';

if (JOURNAL_COMPACTION_THRESHOLD !== 1) {
  throw new Error(
    'the gap-rebuild spec requires a one-envelope journal threshold so a '
      + 'store-covered Result can expire cursor zero',
  );
}

/**
 * Restart this spec's exclusive runner with only its in-memory bound changed.
 * The image launcher remains as a `runuser` parent, so only the Python child is
 * the runner process this fault may replace; `imageRunnerRestartScript` owns
 * that single-process lookup, the TERM/wait, and the `/health` gate.
 */
function compactingRunnerRestartScript(sandbox: SandboxHandle): string {
  return imageRunnerRestartScript({
    launch: observedJournalRunnerLaunch(sandbox, JOURNAL_COMPACTION_THRESHOLD),
    log: RUNNER_RESTART_LOG,
    evidence: `printf 'old_pid=%s threshold=%s port=%s\n' "$old_pid" '${JOURNAL_COMPACTION_THRESHOLD}' '${IMAGE_RUNNER_PORT}'`,
    name: 'compacting runner',
  });
}

function nativeRows(sessionId: string) {
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .filter((row) => row.subpath == null)
    .map((row): Record<string, unknown> & { entry: Record<string, unknown> } =>
      ({ ...row, entry: JSON.parse(String(row.entry_json)) as Record<string, unknown> }));
}

function nativeText(entry: Record<string, unknown>): string {
  const content = (entry.message as Record<string, unknown> | undefined)?.content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.filter((block) => block.type === 'text').map((block) => String(block.text || '')).join('');
}

test('expired cursor rebuilds SessionStore history and resumes exactly once', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  // Conversation tenancy, not the campaign Agent's. This spec replaces the
  // runner process inside the box, and asserts below that the session is the
  // box's sole occupant -- which the campaign Agent can never be, since every
  // one of its conversations is an isolation session in one shared box.
  const agent = await api.createColdTestAgent(
    `__e2e_gap_rebuild_${runId}_${test.info().workerIndex}`,
  );
  agentId = agent.agent_id;
  const created = await api.startConversation(agentId);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const warmupMarker = `GAP_REBUILD_WARMUP_${runId}`;
  const compactionMarker = `GAP_REBUILD_COMPACT_${runId}`;
  const secondMarker = `GAP_REBUILD_AFTER_${runId}`;

  await api.waitForSessionReady(sessionId);
  const warmup = await api.sendTurn(
    sessionId,
    `Do not use tools. Reply exactly ${warmupMarker}`,
    180_000,
  );
  expect(warmup.errorText, 'the provisioning turn must complete').toBeNull();
  const provisioned = await api.waitForSession(
    sessionId,
    (session) => session.state === 'READY' && !session.current_turn_id,
    90_000,
  );
  const sandboxId = String(provisioned.sandbox_id || '').trim();
  expect(sandboxId, 'the provisioning turn must own a sandbox').not.toEqual('');
  const runtimeIdentity = (await api.adminSessionDetail(sessionId)).runtime_identity;
  expect(
    runtimeIdentity && typeof runtimeIdentity === 'object',
    'runner replacement requires the session runtime identity as ownership evidence',
  ).toBeTruthy();
  expect(
    String(runtimeIdentity?.isolated_session_id || '').trim(),
    'this fault must own the whole runner process, not a shared-sandbox sibling',
  ).toEqual('');
  const sandbox = await requireSandboxHandle(api, sandboxId);

  await api.adminEvictRuntime(sessionId);
  const restartEvidence = sandboxExec(sandbox, compactingRunnerRestartScript(sandbox), 30_000).trim();
  expect(restartEvidence, 'the compacting runner must replace the image-started process').toContain(
    `threshold=${JOURNAL_COMPACTION_THRESHOLD}`,
  );
  test.info().annotations.push({
    type: 'gap_rebuild_runner',
    description: `${restartEvidence} sandbox=${sandboxId} runtime=${sandbox.runtime}`,
  });

  const compactionPrompt = `Do not use tools. Reply exactly ${compactionMarker}`;
  const churn = await api.sendTurn(
    sessionId,
    compactionPrompt,
    240_000,
  );
  expect(churn.errorText, 'the journal-producing turn must complete').toBeNull();
  expect(churn.text, 'the compaction checkpoint must follow one real SDK answer').toContain(
    compactionMarker,
  );
  await api.waitForSession(
    sessionId,
    (session) => session.state === 'READY' && !session.current_turn_id,
    90_000,
  );
  let compactionEvidence = '';
  await expect.poll(() => {
    compactionEvidence = sandboxExec(
      sandbox,
      `grep -F ${JSON.stringify(JOURNAL_COMPACTION_LOG)} ${JSON.stringify(RUNNER_RESTART_LOG)} | tail -1 || true`,
      30_000,
    ).trim();
    return compactionEvidence;
  }, {
    timeout: 30_000,
    intervals: [250, 500, 1_000],
    message: 'cursor zero must be expired before exercising the rebuild consumer',
  }).toContain(JOURNAL_COMPACTION_LOG);
  test.info().annotations.push({
    type: 'gap_rebuild_compaction',
    description: compactionEvidence,
  });
  await api.adminEvictRuntime(sessionId);

  const journal = readCompactedRunnerJournal(sandbox, sessionId);
  await test.info().attach('runner-journal-terminal-boundary', {
    body: JSON.stringify(journal), contentType: 'application/json',
  });
  expect(journal.observations.length).toBeGreaterThan(0);
  const firstCompaction = journal.observations[0];
  const lastCompaction = journal.observations.at(-1)!;
  const originalResults = firstCompaction.before.filter((frame) => frame.message_type === 'ResultMessage');
  expect(originalResults).toHaveLength(1);
  const originalResult = originalResults[0];
  expect(originalResult).toMatchObject({ op: 'event', session_id: sessionId,
    seq: firstCompaction.result_sequence, history_live_sequence: firstCompaction.result_sequence,
    message: { subtype: 'success', is_error: false, terminal_reason: 'completed' } });
  expect(originalResult.message?.result).toContain(compactionMarker);
  expect(originalResult.seq).toBeGreaterThan(JOURNAL_COMPACTION_THRESHOLD);
  expect(firstCompaction.before[0].seq).toBeLessThan(originalResult.seq!);
  for (const observation of journal.observations) {
    expect(observation.session_id).toBe(sessionId);
    expect(observation.result_sequence).toBe(originalResult.seq);
    expect(observation.removed).toBeGreaterThan(0);
    expect(observation.before.length - observation.after.length).toBe(observation.removed);
    expect(observation.after).toEqual(observation.before.slice(observation.removed));
    expect(observation.after.filter((frame) => frame.message_type === 'ResultMessage')).toEqual([originalResult]);
  }
  const coldGaps = journal.cold.frames.filter((frame) => frame.op === 'gap');
  expect(journal.cold.after_sequence).toBe(0);
  expect(coldGaps).toHaveLength(1);
  expect(coldGaps[0]).toMatchObject({ after_sequence: 0,
    first_retained_sequence: journal.cold.hello.first_retained_sequence,
    last_sequence: journal.cold.hello.last_seq });
  expect(journal.cold.hello.first_retained_sequence).toBeGreaterThan(1);
  expect(journal.cold.hello.first_retained_sequence).toBe(lastCompaction.after[0].seq);
  expect(journal.cold.hello.last_seq).toBeGreaterThanOrEqual(originalResult.seq!);
  expect(journal.terminal.after_sequence).toBe(originalResult.seq! - 1);
  expect(journal.terminal.hello).toEqual(journal.cold.hello);
  expect(journal.terminal.frames.filter((frame) => frame.op === 'gap')).toEqual([]);
  for (const batch of [journal.cold, journal.terminal]) {
    const sequenced = batch.frames.filter((frame) => frame.seq !== undefined);
    expect(sequenced.map((frame) => frame.seq)).toEqual(Array.from(
      { length: batch.hello.last_seq - Math.max(batch.after_sequence, batch.hello.first_retained_sequence - 1) },
      (_, index) => Math.max(batch.after_sequence, batch.hello.first_retained_sequence - 1) + index + 1,
    ));
    expect(batch.frames.filter((frame) => frame.message_type === 'ResultMessage')).toEqual([originalResult]);
  }
  // The community journal also carries an idle status after the SDK Result.
  // The terminal SDK-message tail, not that mixed transport tail, is one item.
  expect(journal.terminal.frames.filter((frame) => frame.op === 'event')).toEqual([originalResult]);

  const compactedNative = nativeRows(sessionId);
  const storedInput = compactedNative.filter((row) => row.entry.type === 'user'
    && nativeText(row.entry) === compactionPrompt);
  const storedAnswer = compactedNative.filter((row) => row.entry.type === 'assistant'
    && nativeText(row.entry).includes(compactionMarker));
  expect(storedInput).toHaveLength(1);
  expect(storedAnswer).toHaveLength(1);
  expect(nativeText(storedAnswer[0].entry)).toBe(churn.text);
  const commands = sessionEvents(sessionId).filter((row) => row.event_type === 'command.accepted'
    && (row.payload as Record<string, unknown>).content === compactionPrompt);
  expect(commands).toHaveLength(1);
  const compactedTerminal = await waitForTurnTerminalProof(sessionId, String(commands[0].turn_id), 'COMPLETED', 30_000);
  expect(compactedTerminal.last_turn_error ?? null).toBeNull();
  const compactedSession = await api.adminSessionDetail(sessionId);
  expect(compactedSession.sandbox_id).toBe(sandboxId);
  expect(compactedSession.engine_session_key).toBe(originalResult.message?.session_id);
  expect(storedInput[0].session_id).toBe(compactedSession.engine_session_key);
  expect(storedAnswer[0].session_id).toBe(compactedSession.engine_session_key);
  await test.info().attach('runner-journal-native-completion', {
    body: JSON.stringify({ command: commands[0], compactedTerminal, storedInput, storedAnswer,
      nativeSessionKey: compactedSession.engine_session_key }), contentType: 'application/json',
  });

  let messagesRequests = 0;
  const resumeAfterSeq: number[] = [];
  const browserReadOrder: Array<'messages' | 'stream'> = [];
  page.on('request', (req) => {
    const url = new URL(req.url());
    if (url.pathname.endsWith(`/sessions/${sessionId}/history-blocks`)) {
      messagesRequests += 1;
      browserReadOrder.push('messages');
    }
    if (
      req.method() === 'GET'
      && url.pathname.endsWith(`/sessions/${sessionId}/ai-stream`)
    ) {
      const rawAfterSeq = url.searchParams.get('after_seq');
      resumeAfterSeq.push(rawAfterSeq === null ? Number.NaN : Number(rawAfterSeq));
      browserReadOrder.push('stream');
    }
  });
  await page.goto(appPath(`/sessions/${sessionId}`));
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
  await expect.poll(
    () => resumeAfterSeq.length,
    {
      timeout: 15_000,
      intervals: [100, 250, 500],
      message: 'the idle READY session opens exactly one output response',
    },
  ).toBe(1);
  expect(
    resumeAfterSeq[0],
    'the first response starts at the durable session bootstrap cursor',
  ).toBeGreaterThanOrEqual(0);
  expect(messagesRequests, 'bootstrap restores the compacted prefix from SessionStore once').toBe(1);
  for (const marker of [warmupMarker, compactionMarker]) {
    const restored = page.getByTestId('assistant-message').filter({ hasText: marker });
    await expect(restored, 'bootstrap must not duplicate a completed reply').toHaveCount(1);
    await expect(restored, 'the completed prefix must be visible before submitting another turn').toBeVisible();
  }
  const baselineMessagesRequests = messagesRequests;
  const baselineResumeGets = resumeAfterSeq.length;
  const baselineBrowserReads = browserReadOrder.length;
  await page.locator('textarea').fill(`Reply exactly ${secondMarker}`);
  await page.getByTestId('composer-submit').click();

  await expect.poll(async () => {
    const history = await api.getMessages(sessionId, 50);
    return history.messages.filter(
      (message) => message.role === 'assistant' && messageText(message).includes(secondMarker),
    ).length;
  }, {
    timeout: 240_000,
    intervals: [1_000, 2_000],
    message: 'the new turn must complete after bootstrap restored the compacted prefix',
  }).toBe(1);

  await expect.poll(
    () => resumeAfterSeq.length - baselineResumeGets,
    {
      timeout: 30_000,
      message: 'the durable-result history read must finish before one idle response opens',
    },
  ).toBe(1);
  expect(
    messagesRequests - baselineMessagesRequests,
    'the restored prefix needs only the new turn durable-result history read',
  ).toBe(1);
  expect(
    resumeAfterSeq[1],
    'the post-terminal response starts after the bootstrapped response cursor',
  ).toBeGreaterThan(resumeAfterSeq[0]);
  expect(
    browserReadOrder.slice(baselineBrowserReads),
    'the durable-result read precedes idle resume without rereading the already restored prefix',
  ).toEqual(['messages', 'stream']);
  await expect(page.getByTestId('assistant-message').filter({ hasText: secondMarker })).toHaveCount(1);
  const afterNative = nativeRows(sessionId);
  for (const original of compactedNative) {
    expect(typeof original._id, 'every stored native record must have its own identity').toBe('string');
    expect(original._id).not.toBe('');
    const matching = afterNative.filter((row) => row._id === original._id);
    expect(matching, 'replay must preserve exactly one unchanged copy of every native record').toEqual([original]);
  }
  expect(afterNative.filter((row) => row.entry.type === 'user' && nativeText(row.entry) === compactionPrompt)).toHaveLength(1);
  expect(afterNative.filter((row) => row.entry.type === 'assistant' && nativeText(row.entry).includes(compactionMarker))).toHaveLength(1);
  await page.reload();
  await expect(page.getByTestId('assistant-message').filter({ hasText: compactionMarker })).toHaveCount(1);
  await expect(page.getByTestId('assistant-message').filter({ hasText: secondMarker })).toHaveCount(1);
});
