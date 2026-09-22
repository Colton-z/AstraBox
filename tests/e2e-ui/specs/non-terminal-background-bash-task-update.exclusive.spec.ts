/**
 * E2E: a Claude background Bash transition is an update, not a turn failure.
 *
 * Claude emits `task_updated` with `patch.is_backgrounded=true` and no status
 * when a long foreground Bash call is automatically moved into the background.
 * The SDK declares that status optional; the update must remain non-terminal
 * until a fixture-controlled release lets the tool and root turn finish.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, type TurnResult } from '../fixtures/astraApi';
import { documentsByField, sessionEvents } from '../fixtures/dbOracle';
import { trackSessions } from '../fixtures/sessionCleanup';
import { parseTimeoutEnv } from '../fixtures/env';

const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_BACKGROUND_BASH_TIMEOUT_MS', 170_000);
const FOREGROUND_TIMEOUT_MS = 10_000;

const sessions = trackSessions();

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('expected an evidence object');
  return value as Record<string, unknown>;
}

function nativeRows(sessionId: string) {
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .filter((row) => row.subpath == null)
    .map((row) => ({ seq: Number(row.seq), entry: object(JSON.parse(String(row.entry_json))) }))
    .sort((left, right) => left.seq - right.seq);
}

function nativeBlocks(sessionId: string) {
  return nativeRows(sessionId).flatMap(({ entry }) => {
    if (!entry.message) return [];
    const content = object(entry.message).content;
    return Array.isArray(content) ? content.map(object) : [];
  });
}

function taskDiagnostics(sessionId: string) {
  return sessionEvents(sessionId)
    .filter((event) => event.event_type === 'engine.diagnostic'
      && object(event.payload).event_type === 'claude_code.sdk')
    .map((event) => ({ event, raw: object(object(event.payload).raw) }));
}

function completedNotifications(sessionId: string, taskId: string) {
  return nativeRows(sessionId).filter(({ entry }) => {
    const content = entry.type === 'queue-operation' ? entry.content
      : entry.type === 'user' && entry.message ? object(entry.message).content : null;
    return typeof content === 'string' && content.trim().startsWith('<task-notification>')
      && content.includes(`<task-id>${taskId}</task-id>`)
      && content.includes('<status>completed</status>');
  });
}

test('non-terminal background Bash task_updated does not fail the turn', async ({ request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  const runId = Date.now();
  const parentMarker = `BACKGROUND_BASH_PARENT_${runId}`;
  const childMarker = `BACKGROUND_BASH_CHILD_${runId}`;
  const rendezvous = `/workspace/.astrabox-e2e-background-bash-${runId}`;
  const started = `${rendezvous}.started`;
  const release = `${rendezvous}.release`;
  const output = `${rendezvous}.output`;
  const command = `touch ${started}; while [ ! -f ${release} ]; do sleep 0.2; done; printf ${childMarker} | tee ${output}`;
  const evidence: Record<string, unknown> = { sessionId, started, release, output, command };
  let released = false;
  let failed = false;
  type Outcome = { kind: 'result'; turn: TurnResult } | { kind: 'error'; error: unknown };
  let outcome: Outcome | undefined;

  await api.waitForSessionReady(sessionId);
  // Attach both handlers immediately: a transport rejection while another
  // observer is polling must remain the original failure, never unhandled.
  const turnPromise = api.sendTurn(sessionId, [
    `E2E automatic background Bash ${runId}. Follow these tool instructions literally.`,
    'Call Bash exactly once as a foreground tool. Do not set run_in_background.',
    `Set the Bash tool input timeout to ${FOREGROUND_TIMEOUT_MS} milliseconds. This is its foreground wait budget, not a shell timeout.`,
    `Run exactly this command: ${command}`,
    'Do not call Agent and do not replace the command with a fixed sleep.',
    'If Claude moves that command to the background, do not call any tool again.',
    `Do not create ${release}; the E2E harness owns it. Wait for the task completion notification.`,
    `After Bash finishes, reply exactly ${parentMarker}.`,
  ].join('\n'), TURN_TIMEOUT_MS).then(
    (turn): Outcome => { outcome = { kind: 'result', turn }; return outcome; },
    (error: unknown): Outcome => { outcome = { kind: 'error', error }; return outcome; },
  );
  const assertTurnHealthy = () => {
    if (outcome?.kind === 'error') throw outcome.error;
    if (outcome?.kind === 'result' && outcome.turn.errorText) {
      throw new Error(`the background transition failed the root turn: ${JSON.stringify(outcome.turn)}`);
    }
  };

  try {
    await expect.poll(() => {
      assertTurnHealthy();
      return nativeBlocks(sessionId).filter((block) => block.type === 'tool_use' && block.name === 'Bash').length;
    }, { timeout: 60_000, message: 'the actual native Bash input must be durable before waiting for its transition' }).toBe(1);
    const call = nativeBlocks(sessionId).find((block) => block.type === 'tool_use' && block.name === 'Bash')!;
    expect(object(call.input).command).toBe(command);
    expect(object(call.input).timeout).toBe(FOREGROUND_TIMEOUT_MS);
    expect(object(call.input)).not.toHaveProperty('run_in_background');
    expect(String(call.id || '')).not.toBe('');
    evidence.call = call;
    await expect.poll(
      async () => {
        assertTurnHealthy();
        return (await api.runTerminalCommand(
          sessionId, `test -f ${started} && echo yes || echo no`, '/workspace', 30_000,
        )).includes('yes');
      },
      { message: 'the foreground Bash call must start', timeout: 60_000, intervals: [1_000, 2_000] },
    ).toBe(true);

    const starts = () => taskDiagnostics(sessionId).filter(({ raw }) => raw.subtype === 'task_started'
      && raw.task_type === 'local_bash' && raw.tool_use_id === call.id);
    await expect.poll(() => { assertTurnHealthy(); return starts().length; },
      { timeout: 30_000, message: 'the real Bash task must be consumed with its original tool lineage' }).toBe(1);
    const startedTask = starts()[0];
    expect(object(startedTask.raw.data).is_backgrounded,
      'the supplier must start the real task in the foreground before its transition').toBe(false);
    const taskId = String(startedTask.raw.task_id || '');
    expect(taskId).not.toBe('');
    const backgroundUpdates = () => taskDiagnostics(sessionId).filter(({ raw }) => (
      raw.subtype === 'task_updated' && raw.task_id === taskId
      && !raw.status && raw.patch && !object(raw.patch).status
      && object(raw.patch).is_backgrounded === true
    ));
    await expect.poll(() => { assertTurnHealthy(); return backgroundUpdates().length; },
      { timeout: 30_000, message: 'the same task statusless background update must reach the platform before release' }).toBe(1);
    const update = backgroundUpdates()[0];
    expect(Number(update.event.event_seq)).toBeGreaterThan(Number(startedTask.event.event_seq));
    evidence.transition = { startedTask, update, taskId };

    await api.runTerminalCommand(sessionId, `touch ${release}`, '/workspace', 30_000);
    released = true;
    const result = await turnPromise;
    if (result.kind === 'error') throw result.error;
    const turn = result.turn;
    expect(
      turn.errorText,
      `the statusless background transition must not fail the root turn; frames=${turn.frameTypes.join(',')}`,
    ).toBeNull();
    expect(
      turn.frameTypes.filter((type) => type === 'finish'),
      'the released root turn must publish exactly one clean terminal frame',
    ).toHaveLength(1);

    // The SDK can complete its task after the public root stream closes. Its
    // resident message/store remains authoritative without a new root request.
    const completedTasks = () => sessionEvents(sessionId)
      .filter((event) => event.event_type === 'engine.message')
      .map((event) => ({ event, raw: object(object(event.payload).message) }))
      .filter(({ raw }) => raw.task_id === taskId && (
        (raw.subtype === 'task_notification' && raw.status === 'completed')
        || (raw.subtype === 'task_updated' && raw.patch && object(raw.patch).status === 'completed')
      ));
    await expect.poll(() => completedTasks().length + completedNotifications(sessionId, taskId).length,
      { timeout: 30_000, message: 'that same Bash task must really complete, through its SDK terminal or native notification' })
      .toBeGreaterThan(0);
    expect(await api.downloadFileText(sessionId, output), 'the actual released command must produce its complete stdout')
      .toBe(childMarker);
    await expect.poll(() => nativeRows(sessionId).filter(({ entry }) => entry.type === 'assistant')
      .flatMap(({ entry }) => {
        const content = object(entry.message).content;
        return Array.isArray(content) ? content.map(object) : [];
      }).filter((block) => block.type === 'text').map((block) => String(block.text || '')).join('\n'),
    { timeout: 30_000, message: 'the actual native parent reply must survive the task-notification continuation' })
      .toContain(parentMarker);
    expect(nativeBlocks(sessionId).filter((block) => block.type === 'tool_use')).toEqual([call]);
    expect((await api.listChildRuns(sessionId)).child_runs,
      'a Bash task is not an Agent and creates no child-Agent control').toEqual([]);
    const settled = await api.waitForSession(sessionId, (row) => (
      row.state === 'READY' && !row.current_turn_id
    ), TURN_TIMEOUT_MS);
    expect(settled.last_turn_status, 'automatic backgrounding must not rewrite the turn as failed').not.toBe('FAILED');
    expect(settled.last_turn_status).toBe('COMPLETED');
    expect((await api.getMessages(sessionId, 100)).messages.flatMap((message) => message.blocks || [])
      .filter((block) => block.type === 'turn_failure')).toEqual([]);
    expect(sessionEvents(sessionId).filter((event) => event.event_type === 'command.accepted'
      && object(event.payload).command_type === 'StartTurn')
      .map((event) => object(event.payload).command_type),
    'the SDK task notification must not manufacture another platform conversation input').toEqual(['StartTurn']);
    evidence.completed = { turn, settled, tasks: completedTasks(), notifications: completedNotifications(sessionId, taskId) };
  } catch (error) {
    failed = true;
    throw error;
  } finally {
    if (!released) {
      await api.runTerminalCommand(sessionId, `touch ${release}`, '/workspace', 15_000).catch((error: unknown) => {
        test.info().annotations.push({ type: 'background_bash_release_error', description: String(error) });
        if (!failed) throw error;
      });
    }
    try {
      await test.info().attach('background-bash-native-evidence', {
        body: JSON.stringify({ ...evidence, outcome, native: nativeRows(sessionId), journal: sessionEvents(sessionId) }),
        contentType: 'application/json',
      });
    } catch (error) {
      test.info().annotations.push({ type: 'background_bash_attachment_error', description: String(error) });
      if (!failed) throw error;
    }
  }
});
