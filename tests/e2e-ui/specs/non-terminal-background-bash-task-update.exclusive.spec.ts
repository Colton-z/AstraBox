/**
 * E2E: a Claude background Bash transition is an update, not a turn failure.
 *
 * Claude emits `task_updated` with `patch.is_backgrounded=true` and no status
 * when a long foreground Bash call is automatically moved into the background.
 * The SDK declares that status optional; the update must remain non-terminal:
 * the response that made the call ends completed, the command keeps running
 * until a fixture-controlled release, and Claude's own report of its
 * completion reaches the conversation without becoming a platform input.
 *
 * The SDK's lifecycle messages are diagnostics the platform does not keep, so
 * the transition is read from Claude's own transcript: the Bash call has a
 * successful result while its command is still blocked on the release, which
 * only a call moved into the background can have, and the command's output
 * appears only after the release.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, messageText, type TurnResult } from '../fixtures/astraApi';
import { terminalOutput } from '../fixtures/nativeMcpServer';
import {
  nativeTranscriptRows,
  sessionInputRows,
  sessionOutputRows,
  sessionResponseRows,
} from '../fixtures/dbOracle';
import { trackSessions } from '../fixtures/sessionCleanup';
import { parseTimeoutEnv } from '../fixtures/env';

const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_BACKGROUND_BASH_TIMEOUT_MS', 170_000);
const FOREGROUND_TIMEOUT_MS = 10_000;

const sessions = trackSessions();

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('expected an evidence object');
  return value as Record<string, unknown>;
}

/** Claude's root transcript, as the platform's SessionStore mirror holds it. */
function nativeRows(sessionId: string) {
  return nativeTranscriptRows(sessionId)
    .filter((row) => row.subpath === null)
    .map((row) => ({ seq: Number(row.seq), entry: object(JSON.parse(String(row.entry_json))) }))
    .sort((left, right) => left.seq - right.seq);
}

function nativeBlocks(sessionId: string) {
  return nativeRows(sessionId).flatMap(({ seq, entry }) => {
    if (!entry.message) return [];
    const content = object(entry.message).content;
    return Array.isArray(content) ? content.map(object).map((block) => ({ seq, block })) : [];
  });
}

/** A native line's text: a string content, or its text blocks. */
function entryTexts(entry: Record<string, unknown>): string[] {
  if (entry.type === 'queue-operation') return typeof entry.content === 'string' ? [entry.content] : [];
  if (entry.type !== 'user' || !entry.message) return [];
  const content = object(entry.message).content;
  if (typeof content === 'string') return [content];
  return Array.isArray(content)
    ? content.map(object).filter((block) => block.type === 'text').map((block) => String(block.text || ''))
    : [];
}

/** Claude's own notices that a task completed, after native position `afterSeq`. */
function completedNotifications(sessionId: string, afterSeq: number) {
  return nativeRows(sessionId).filter(({ seq, entry }) => seq > afterSeq && entryTexts(entry).some((text) => (
    text.trim().startsWith('<task-notification>') && text.includes('<status>completed</status>')
  )));
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
  const prompt = [
    `E2E automatic background Bash ${runId}. Follow these tool instructions literally.`,
    'Call Bash exactly once as a foreground tool. Do not set run_in_background.',
    `Set the Bash tool input timeout to ${FOREGROUND_TIMEOUT_MS} milliseconds. This is its foreground wait budget, not a shell timeout.`,
    `Run exactly this command: ${command}`,
    'Do not call Agent and do not replace the command with a fixed sleep.',
    'If Claude moves that command to the background, do not call any tool again.',
    `Do not create ${release}; the E2E harness owns it. Wait for the task completion notification.`,
    `After Bash finishes, reply exactly ${parentMarker}.`,
  ].join('\n');
  const evidence: Record<string, unknown> = { sessionId, started, release, output, command };
  let released = false;
  let failed = false;
  type Outcome = { kind: 'result'; turn: TurnResult } | { kind: 'error'; error: unknown };
  let outcome: Outcome | undefined;

  await api.waitForSessionReady(sessionId);
  // Attach both handlers immediately: a transport rejection while another
  // observer is polling must remain the original failure, never unhandled.
  const turnPromise = api.sendTurn(sessionId, prompt, TURN_TIMEOUT_MS).then(
    (turn): Outcome => { outcome = { kind: 'result', turn }; return outcome; },
    (error: unknown): Outcome => { outcome = { kind: 'error', error }; return outcome; },
  );
  const assertTurnHealthy = () => {
    if (outcome?.kind === 'error') throw outcome.error;
    if (outcome?.kind === 'result' && outcome.turn.errorText) {
      throw new Error(`the background transition failed the root turn: ${JSON.stringify(outcome.turn)}`);
    }
  };
  const fileExists = async (path: string) => terminalOutput(await api.runTerminalCommand(
    sessionId, `test -f ${path} && echo yes || echo no`, '/workspace', 30_000,
  )).trim() === 'yes';

  try {
    const bashCalls = () => nativeBlocks(sessionId)
      .filter(({ block }) => block.type === 'tool_use' && block.name === 'Bash');
    await expect.poll(() => {
      assertTurnHealthy();
      return bashCalls().length;
    }, { timeout: 60_000, message: 'the actual native Bash input must be durable before waiting for its transition' }).toBe(1);
    const { seq: callSeq, block: call } = bashCalls()[0]!;
    expect(object(call.input).command).toBe(command);
    expect(object(call.input).timeout).toBe(FOREGROUND_TIMEOUT_MS);
    expect(object(call.input)).not.toHaveProperty('run_in_background');
    expect(String(call.id || '')).not.toBe('');
    evidence.call = call;
    await expect.poll(
      async () => {
        assertTurnHealthy();
        return fileExists(started);
      },
      { message: 'the foreground Bash call must start', timeout: 60_000, intervals: [1_000, 2_000] },
    ).toBe(true);

    // The call returns while its command still waits for the release only if
    // Claude moved it into the background; a call killed at its timeout
    // returns an error and its command never writes the output below.
    const callResults = () => nativeBlocks(sessionId)
      .filter(({ block }) => block.type === 'tool_result' && block.tool_use_id === call.id);
    await expect.poll(() => { assertTurnHealthy(); return callResults().length; }, {
      timeout: 60_000,
      message: 'the foreground Bash call must return once its timeout moves the running command to the background',
    }).toBe(1);
    const backgrounded = callResults()[0]!;
    expect(backgrounded.block.is_error, 'the transition is not a failed call').not.toBe(true);
    expect(await fileExists(output), 'the command is still running when its call returns').toBe(false);
    evidence.transition = backgrounded;

    await api.runTerminalCommand(sessionId, `touch ${release}`, '/workspace', 30_000);
    released = true;
    const result = await turnPromise;
    if (result.kind === 'error') throw result.error;
    const turn = result.turn;
    expect(
      turn.errorText,
      `the statusless background transition must not fail the root turn; frames=${turn.frameTypes.join(',')}`,
    ).toBeNull();
    const inputs = sessionInputRows(sessionId);
    expect(inputs.map((row) => row.input_id), 'the prompt is the one input of this conversation').toEqual([turn.inputId]);
    const responseId = String(inputs[0]!.response_id || '');
    expect(responseId, 'the runtime took the prompt into a response').not.toBe('');
    await expect.poll(() => sessionResponseRows(sessionId).find((row) => row.response_id === responseId)?.status, {
      timeout: 30_000, message: 'the response that made the call must end',
    }).toBe('ended');
    const ownParts = sessionOutputRows(sessionId, responseId).map((row) => row.part);
    expect(
      ownParts.filter((part) => part.type === 'finish').map((part) => part.finishReason),
      'the released root turn must publish exactly one clean terminal frame',
    ).toEqual(['stop']);
    expect(ownParts.filter((part) => part.type === 'error')).toEqual([]);

    // The SDK can complete its task after the response that started it ended.
    // Claude reports that completion in its own transcript, without a new
    // request from anyone.
    await expect.poll(() => completedNotifications(sessionId, callSeq).length,
      { timeout: 30_000, message: 'that same Bash task must really complete, through its native notification' })
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
    expect(nativeBlocks(sessionId).filter(({ block }) => block.type === 'tool_use').map(({ block }) => block))
      .toEqual([call]);
    expect((await api.listChildRuns(sessionId)).child_runs,
      'a Bash task is not an Agent and creates no child-Agent control').toEqual([]);
    const settled = await api.waitForSession(sessionId, (row) => (
      row.state === 'READY' && (row.current_responses ?? []).length === 0 && !row.pending_interaction
    ), TURN_TIMEOUT_MS);
    expect(settled.last_response?.outcome, 'automatic backgrounding must not rewrite the turn as failed')
      .toBe('completed');
    const responses = sessionResponseRows(sessionId);
    expect(
      responses.filter((row) => row.status !== 'ended' || row.outcome !== 'completed'),
      'every response of the conversation, including one Claude starts for the notification, completes',
    ).toEqual([]);
    expect(sessionOutputRows(sessionId).filter((row) => row.part.type === 'error'),
      'no response of the conversation reports an error').toEqual([]);
    const history = await api.getMessages(sessionId, 100);
    expect(history.messages.filter((message) => message.role === 'assistant')
      .some((message) => messageText(message).includes(parentMarker)),
    'the conversation shows the reply Claude gave once the task completed').toBe(true);
    expect(history.messages.filter((message) => message.role === 'user').map(messageText),
      'the SDK task notification must not manufacture another platform conversation input').toEqual([prompt]);
    expect(sessionInputRows(sessionId).map((row) => row.input_id),
      'the platform accepted no input beyond the prompt').toEqual([turn.inputId]);
    evidence.completed = { turn, settled, responses, notifications: completedNotifications(sessionId, callSeq) };
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
        body: JSON.stringify({
          ...evidence,
          outcome,
          native: nativeRows(sessionId),
          inputs: sessionInputRows(sessionId),
          responses: sessionResponseRows(sessionId),
        }),
        contentType: 'application/json',
      });
    } catch (error) {
      test.info().annotations.push({ type: 'background_bash_attachment_error', description: String(error) });
      if (!failed) throw error;
    }
  }
});
