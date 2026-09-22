/** Real background completion can ask a question which the user answers or stops. */
import { expect, test, type Page } from '@playwright/test';

import {
  AstraApi, messageText, visibleMessages,
  type ChildRunRecord, type PendingInteraction,
} from '../fixtures/astraApi';
import { documentsByField, framesForTurn, sessionEvents } from '../fixtures/dbOracle';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, startPromptDelivery, expectPromptDelivered } from '../fixtures/sessionPage';
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
const PROOF = /BG_RUNTIME_PROOF_[a-f0-9]{32}/g;

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

function nativeEntries(sessionId: string) {
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .map((row) => ({
      subpath: row.subpath,
      seq: Number(row.seq),
      entry: JSON.parse(String(row.entry_json)) as Record<string, unknown>,
    }))
    .sort((a, b) => a.seq - b.seq);
}

function rootEntries(sessionId: string) {
  return nativeEntries(sessionId).filter(({ subpath, entry }) => (
    subpath == null && entry.isSidechain !== true
    && !String(entry.parentToolUseId ?? entry.parent_tool_use_id ?? '').trim()
  ));
}

function blocks(entry: Record<string, unknown>): Record<string, unknown>[] {
  const content = record(entry.message).content;
  return Array.isArray(content) ? content.map(record) : [];
}

function toolUses(sessionId: string, name: string) {
  return rootEntries(sessionId).flatMap(({ seq, entry }) => (
    entry.type === 'assistant'
      ? blocks(entry).filter((block) => block.type === 'tool_use' && block.name === name)
        .map((block) => ({ seq, entry, block })) : []
  ));
}

async function fileExists(api: AstraApi, sessionId: string, path: string): Promise<boolean> {
  try {
    await api.downloadFileText(sessionId, path, 30_000);
    return true;
  } catch (error) {
    // Match only the file API's exact missing-file response, not a transport error.
    if (/^Error: files\/download -> 404:/.test(String(error))) return false;
    throw error;
  }
}

async function assertChildPanel(page: Page, child: ChildRunRecord) {
  await page.getByRole('tab', { name: /^Agents/ }).click();
  const panel = page.getByTestId('subagent-agents-panel');
  await expect(panel.getByTestId('subagent-agent-row'), 'one Agent, not its internal Bash task').toHaveCount(1);
  const row = panel.locator(`[data-child-run-id="${child.child_run_id}"]`);
  await expect(row).toBeVisible();
  await expect(row.getByText('completed', { exact: true })).toBeVisible();
}

async function observe(read: () => unknown) {
  try { return { available: true, value: await read() }; }
  catch (error) { return { available: false, error: String(error) }; }
}

async function runQuestionJourney(api: AstraApi, page: Page, action: 'answer' | 'stop') {
  const runId = `${action}_${Date.now()}`;
  const prefix = `/workspace/.astrabox-background-question-${runId}`;
  const started = `${prefix}.started`;
  const release = `${prefix}.release`;
  const parentMarker = `PARENT_RESULT_${runId}`;
  const childMarker = `BACKGROUND_QUESTION_CHILD_${runId}`;
  const command = [
    "python3 - <<'PY'", 'from pathlib import Path', 'import secrets, time',
    `Path(${JSON.stringify(started)}).write_text('started')`,
    `release = Path(${JSON.stringify(release)})`,
    'deadline = time.monotonic() + 100',
    'while not release.exists():',
    '    if time.monotonic() >= deadline:',
    "        raise TimeoutError('the test did not release the background workload')",
    '    time.sleep(0.2)',
    `print(${JSON.stringify(childMarker)})`,
    'print("BG_RUNTIME_PROOF_" + secrets.token_hex(16))', 'PY',
  ].join('\n');
  const prompt = [
    `Background completion questionnaire ${runId}.`,
    'In the initial response launch exactly one Agent with run_in_background=true and subagent_type=general-purpose.',
    'The background Agent must call Bash exactly once with this command verbatim:', command,
    'PROOF means the complete actual Bash output line BG_RUNTIME_PROOF_<32 lowercase hexadecimal characters>, including the literal BG_RUNTIME_PROOF_ prefix.',
    `The background Agent must copy that complete PROOF line verbatim in its final answer alongside ${childMarker}.`,
    'The background Agent must not ask the question itself.',
    `Immediately after launch, the parent must reply with ${parentMarker} and end its response without waiting.`,
    'Do not call Bash, TaskOutput or AskUserQuestion in the initial parent response.',
    `Only after the later actual task notification contains ${childMarker}, the parent must call AskUserQuestion exactly once.`,
    'Ask one single-select question containing that complete PROOF line verbatim, with header E2E and options YES and NO.',
    'Wait for the user answer. After YES, reply with CONTINUATION_ANSWERED_<PROOF> using the actual value and no more tools.',
    'If the question is declined or stopped, finish without asking again or using more tools.',
    'Do not create the release file; the test owns it.',
  ].join('\n');
  const agent = await api.defaultAgent();
  const sessionId = (await api.startConversation(agent.agent_id)).session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  let launchTurn = '';
  let continuationTurn = '';
  let childId = '';
  await mirrorSseBodies(page);
  try {
    await api.waitForSessionReady(sessionId);
    await api.setPermissionMode(sessionId, 'bypassPermissions');
    await openSessionView(page, sessionId);
    const delivery = await startPromptDelivery(page, sessionId, prompt);
    await expectPromptDelivered(delivery);

    await expect.poll(async () => {
      const history = await api.getMessages(sessionId, 100);
      const parents = history.messages.filter((message) => (
        message.role === 'assistant' && messageText(message).includes(parentMarker)
      ));
      if (parents.length !== 1) return false;
      launchTurn = parents[0].turn_id;
      const detail = await api.getSession(sessionId);
      return !detail.current_turn_id && detail.last_turn_status === 'COMPLETED'
        && framesForTurn(launchTurn).some((frame) => record(frame.payload).type === 'data-result');
    }, { timeout: 60_000, message: 'the launch Result must precede background completion' }).toBe(true);
    await expect.poll(() => fileExists(api, sessionId, started), {
      timeout: 30_000, message: 'the real child Bash must reach its workload gate',
    }).toBe(true);
    const launched = await api.waitForChildRuns(sessionId, (rows) => rows.length > 0, 30_000);
    expect(launched, 'only the requested Agent belongs in the public index').toHaveLength(1);
    childId = launched[0].child_run_id;
    expect(launched[0].closed, 'the child must still be held after the launch Result').toBe(false);
    await expect.poll(() => toolUses(sessionId, 'Agent').length, {
      timeout: 20_000, message: 'the native mirror must retain the real Agent launch',
    }).toBe(1);
    const launches = toolUses(sessionId, 'Agent');
    expect(launches, 'one real native background Agent launch').toHaveLength(1);
    const launchToolId = String(launches[0].block.id);
    expect(record(launches[0].block.input).run_in_background).toBe(true);
    const nativeSessionId = String(launches[0].entry.sessionId || '').trim();
    expect(nativeSessionId).not.toBe('');

    await api.runTerminalCommand(sessionId, `touch ${release}`, '/workspace', 30_000);
    expect(await fileExists(api, sessionId, release), 'the workload release must exist').toBe(true);
    const waiting = await api.waitForSession(sessionId, (detail) => (
      detail.pending_interaction?.tool_name === 'AskUserQuestion'
    ), 60_000);
    const pending = waiting.pending_interaction as PendingInteraction;
    continuationTurn = String(pending.turn_id || '');
    const questionId = String(pending.tool_call_id || '');
    expect(continuationTurn).not.toBe('');
    expect(questionId).not.toBe('');
    expect(waiting.current_turn_id, 'the question belongs to the active continuation').toBe(continuationTurn);

    const completed = await api.waitForChildRuns(sessionId, (rows) => rows.some((row) => row.closed), 30_000);
    expect(completed).toEqual([expect.objectContaining({
      child_run_id: childId, closed: true, engine_status: 'completed',
    })]);
    let proof = '';
    let bashId = '';
    await expect.poll(async () => {
      const child = await api.getChildRunMessages(sessionId, childId);
      const content = child.messages.flatMap((message) => message.content);
      const uses = content.filter((block) => block.type === 'tool_use' && block.name === 'Bash');
      if (uses.length !== 1) return { uses: uses.length, results: 0, proofs: 0 };
      expect(JSON.stringify(uses[0].input), 'the nested Bash is the workload we released').toContain(childMarker);
      expect(JSON.stringify(uses[0].input)).toContain(release);
      bashId = String(uses[0].id);
      const results = content.filter((block) => block.type === 'tool_result' && block.tool_use_id === bashId);
      const proofs = results.flatMap((block) => JSON.stringify(block.content).match(PROOF) || []);
      proof = proofs[0] || '';
      return { uses: uses.length, results: results.length, proofs: proofs.length };
    }, { timeout: 30_000, message: 'the completed child must retain its exact Bash/result pair and runtime proof' })
      .toEqual({ uses: 1, results: 1, proofs: 1 });
    expect(JSON.stringify(pending)).toContain(proof);
    expect(JSON.stringify(pending)).toContain('YES');
    expect(JSON.stringify(pending)).toContain('NO');

    await expect.poll(() => {
      const asks = toolUses(sessionId, 'AskUserQuestion');
      const notifications = rootEntries(sessionId).filter(({ entry }) => (
        entry.type === 'user' && record(entry.origin).kind === 'task-notification'
        && JSON.stringify(record(entry.message).content).includes(proof)
      ));
      return {
        asks: asks.map(({ block }) => String(block.id)),
        notifications: notifications.length,
        afterNotification: asks.length === 1 && notifications.length === 1
          && asks[0].seq > notifications[0].seq,
        nativeSession: asks[0]?.entry.sessionId,
      };
    }, { timeout: 30_000, message: 'the same native conversation must ask only after its real completion notification' })
      .toEqual({ asks: [questionId], notifications: 1, afterNotification: true, nativeSession: nativeSessionId });
    await expect.poll(async () => {
      const history = await api.getMessages(sessionId, 100);
      const active = visibleMessages(history).filter((message) => message.turn_id === continuationTurn);
      const uses = active.flatMap((message) => message.blocks || []).filter((block) => (
        block.type === 'tool_use' && block.name === 'AskUserQuestion' && block.id === questionId
        && JSON.stringify(block.input).includes(proof)
      ));
      const interactions = framesForTurn(continuationTurn).filter((frame) => {
        const payload = record(frame.payload);
        return payload.type === 'data-interaction'
          && record(payload.data).interaction_id === pending.interaction_id;
      });
      return { asks: uses.length, interaction: interactions.length > 0 };
    }, { timeout: 20_000, message: 'pending authority and projected question must share their real owner and IDs' })
      .toEqual({ asks: 1, interaction: true });
    const panel = page.getByTestId('pending-interaction-panel');
    await expect(panel).toContainText(proof);
    await assertChildPanel(page, completed[0]);

    if (action === 'answer') {
      // The answer API is the donor's real submission path; the panel was independently read above.
      const questions = pending.questions as Array<{ id: string }>;
      expect(questions).toHaveLength(1);
      expect(questions[0].id).toBeTruthy();
      const answer = await api.answerPendingInteraction(sessionId, pending.interaction_id, {
        answers: [{ question_id: questions[0].id, option_label: 'YES' }],
      });
      expect(answer.answered).toBe(true);
      expect(answer.interaction_id).toBe(pending.interaction_id);
    } else {
      await api.interruptSession(sessionId);
    }
    const ready = await api.waitForSession(sessionId, (detail) => (
      detail.state === 'READY' && !detail.current_turn_id && !detail.pending_interaction
    ), 60_000);
    expect(ready.last_turn_status, 'answer and Stop must both complete normally').toBe('COMPLETED');
    expect(ready.last_error).toBeFalsy();
    await expect(panel).toHaveCount(0);

    const final = await api.getMessages(sessionId, 100);
    const launchMessages = final.messages.filter((message) => message.turn_id === launchTurn);
    expect(launchMessages.some((message) => messageText(message).includes(parentMarker))).toBe(true);
    expect(launchMessages.flatMap((message) => message.blocks || [])
      .filter((block) => block.type === 'tool_use' && block.name === 'Agent').map((block) => block.id))
      .toEqual([launchToolId]);
    expect(final.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([prompt]);
    expect(final.messages.find((message) => message.role === 'user')?.client_message_id)
      .toBe(delivery.clientMessageId);
    const continuation = final.messages.filter((message) => message.turn_id === continuationTurn);
    const finalBlocks = continuation.flatMap((message) => message.blocks || []);
    expect(finalBlocks.filter((block) => block.type === 'tool_use' && block.name === 'AskUserQuestion')
      .map((block) => block.id)).toEqual([questionId]);
    expect(finalBlocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === questionId)).toHaveLength(1);
    if (action === 'answer') {
      expect(continuation.filter((message) => message.role === 'assistant'
        && messageText(message).includes(`CONTINUATION_ANSWERED_${proof}`))).toHaveLength(1);
    }
    await expect.poll(() => rootEntries(sessionId).flatMap(({ entry }) => blocks(entry))
      .filter((block) => block.type === 'tool_result' && block.tool_use_id === questionId).length, {
      timeout: 20_000, message: 'native Store must retain the same question result once',
    }).toBe(1);
    expect((await api.listChildRuns(sessionId)).child_runs).toEqual([
      expect.objectContaining({ child_run_id: childId, closed: true, engine_status: 'completed' }),
    ]);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible();
    await expect(page.getByTestId('pending-interaction-panel')).toHaveCount(0);
    await expect(page.getByTestId('user-message')).toHaveCount(1);
    await assertChildPanel(page, completed[0]);
    const cold = await api.getMessages(sessionId, 100);
    expect(cold.messages.map((message) => [message.message_id, message.role, message.turn_id]))
      .toEqual(final.messages.map((message) => [message.message_id, message.role, message.turn_id]));
    test.info().annotations.push({ type: 'background_question_identity', description: JSON.stringify({
      action, sessionId, nativeSessionId, launchTurn, launchToolId, continuationTurn,
      childId, bashId, questionId, interactionId: pending.interaction_id, proof,
    }) });
  } catch (error) {
    const evidence = {
      action, sessionId, launchTurn, continuationTurn, childId,
      session: await observe(() => api.getSession(sessionId)),
      messages: await observe(() => api.getMessages(sessionId, 100)),
      children: await observe(() => api.listChildRuns(sessionId)),
      childTranscript: await observe(() => childId ? api.getChildRunMessages(sessionId, childId) : null),
      native: await observe(() => nativeEntries(sessionId)),
      events: await observe(() => sessionEvents(sessionId)),
      frames: await observe(() => continuationTurn ? framesForTurn(continuationTurn) : []),
      browser: await observe(() => aiStreamBodies(page)),
    };
    await test.info().attach('background-question-failure', {
      body: Buffer.from(JSON.stringify(evidence)), contentType: 'application/json',
    });
    throw error;
  }
  // trackSessions owns pass/failure cleanup; no Stop, eviction or gate release here.
}

test('background completion question accepts an answer and retains its child after reload', async ({ page, request }) => {
  await runQuestionJourney(new AstraApi(request), page, 'answer');
});

test('stopping a background completion question keeps one completed Agent after reload', async ({ page, request }) => {
  await runQuestionJourney(new AstraApi(request), page, 'stop');
});
