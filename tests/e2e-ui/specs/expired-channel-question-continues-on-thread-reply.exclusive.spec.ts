/**
 * E2E: a channel user can continue an expired question in the same thread.
 *
 * The built-in generic channel identifies the external thread through
 * conversation_id: every message carrying that identity resolves to one Agent
 * conversation.
 * After the question's sandbox is reclaimed, the old in-box interaction is
 * honestly settled; the user's next thread message must still enter that SAME
 * session, borrow fresh compute, and complete exactly once.
 *
 * A separate same-box runner-restart case proves that an expired callback
 * cannot accept the old form or write an answer to the native store.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

import {
  AstraApi,
  messageText,
  type PendingInteraction,
} from '../fixtures/astraApi';
import { insist } from '../fixtures/insist';
import { documentsByField, sessionDoc, sessionEvents } from '../fixtures/dbOracle';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { IMAGE_RUNNER_LAUNCHER, imageRunnerPidLookup, imageRunnerRestartScript } from '../fixtures/runnerRestart';
import { requireSandboxHandle, sandboxExec, sandboxRunning } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

interface ChannelTriggerReceipt {
  deployment_id: string;
  session_id: string;
  status: string;
  [key: string]: unknown;
}

/** What a user says when a model answers instead of acting. */
const INSIST_NUDGE =
  "You answered without using the tool. Do the tool call now, exactly as asked above, before replying again. \u8bf7\u73b0\u5728\u5c31\u6309\u4e0a\u9762\u7684\u8981\u6c42\u8c03\u7528\u5de5\u5177\uff0c\u4e0d\u8981\u53ea\u7528\u6587\u5b57\u56de\u7b54\u3002";

const TURN_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_EXPIRED_CHANNEL_TURN_TIMEOUT_MS',
  300_000,
);
const PANEL_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_RECLAIM_WAITING_RENDER_MS',
  60_000,
);

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (deploymentId && agentId) {
    await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  }
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

async function triggerThreadMessage(
  request: APIRequestContext,
  {
    deploymentId,
    secret,
    threadId,
    messageId,
    text,
  }: {
    deploymentId: string;
    secret: string;
    threadId: string;
    messageId: string;
    text: string;
  },
): Promise<ChannelTriggerReceipt> {
  const response = await request.fetch(apiPath(`/deployments/${deploymentId}/trigger`), {
    method: 'POST',
    data: {
      text,
      message_id: messageId,
      conversation_id: threadId,
    },
    headers: { 'x-channel-secret': secret },
    timeout: 60_000,
  });
  const raw = await response.text();
  expect(response.ok(), raw).toBe(true);
  const envelope = JSON.parse(raw) as {
    data?: ChannelTriggerReceipt;
  } & ChannelTriggerReceipt;
  return (envelope.data ?? envelope) as ChannelTriggerReceipt;
}

async function assistantCount(api: AstraApi, sessionId: string): Promise<number> {
  const history = await api.getMessages(sessionId, 100);
  return history.messages.filter((message) => message.role === 'assistant').length;
}

async function exactUserInputCount(
  api: AstraApi,
  sessionId: string,
  expected: string,
): Promise<number> {
  const history = await api.getMessages(sessionId, 100);
  return history.messages.filter(
    (message) => message.role === 'user' && messageText(message).trim() === expected,
  ).length;
}

function storedObject(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`expected a stored object, received ${JSON.stringify(value)}`);
  }
  return value as Record<string, unknown>;
}

function nativeQuestionEvidence(sessionId: string, toolId: string) {
  const records = documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .filter((row) => row.subpath == null)
    .map((row) => storedObject(JSON.parse(String(row.entry_json))))
    .filter((entry) => entry.type === 'user' || entry.type === 'assistant');
  // Native resume may reappend a UUID with new metadata, not a new input.
  const byUuid = new Map<string, Record<string, unknown>>();
  for (const entry of records) {
    const uuid = String(entry.uuid || '').trim();
    expect(uuid, 'native message records must carry their vendor identity').not.toBe('');
    const original = byUuid.get(uuid);
    if (original) {
      expect(entry.type).toBe(original.type);
      expect(entry.message, `reappended native ${uuid} must keep its message`).toEqual(original.message);
    } else byUuid.set(uuid, entry);
  }
  const entries = [...byUuid.values()];
  const blocks = entries.flatMap((entry) => {
    const content = storedObject(entry.message).content;
    return Array.isArray(content) ? content.map(storedObject) : [];
  });
  return {
    inputs: entries.filter((entry) => entry.type === 'user')
      .map((entry) => ({ uuid: String(entry.uuid), message: entry.message }))
      .sort((left, right) => left.uuid.localeCompare(right.uuid)),
    calls: blocks.filter((block) => block.type === 'tool_use' && block.id === toolId),
    results: blocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === toolId),
  };
}

test('expired channel question continues in the same thread on fresh compute', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const threadId = `e2e-expired-question-thread-${runId}`;

  try {
    const agent = await api.createColdTestAgent(
      `__e2e_expired_channel_question_${runId}`,
    );
    agentId = String(agent.agent_id || '').trim();
    expect(agentId, 'created agent must have an id').not.toEqual('');

    const deployment = await platform.createDeployment(agentId, {
      scene: 'channel:generic_json',
      prompt_prefix: '',
    });
    deploymentId = String(deployment.deployment_id || '').trim();
    const secret = String(deployment.secret || '');
    expect(deploymentId, 'channel binding must have an id').not.toEqual('');
    expect(secret, 'channel binding must expose its trigger credential').not.toEqual('');

    // First establish the external thread and let its conversation reach an
    // ordinary READY boundary before asking the interactive question.
    const warmText = `E2E_CHANNEL_WARM_${runId}: do not use tools; reply briefly.`;
    const warm = await triggerThreadMessage(request, {
      deploymentId,
      secret,
      threadId,
      messageId: `warm-${runId}`,
      text: warmText,
    });
    expect(warm.status).toBe('accepted');
    const sessionId = String(warm.session_id || '').trim();
    expect(sessionId, 'the first channel message must bind a conversation').not.toEqual('');
    sessions.push(sessionId);
    await api.waitForAssistantMessageCount(sessionId, 0, TURN_TIMEOUT_MS);
    await api.waitForSessionReady(sessionId, TURN_TIMEOUT_MS);
    await api.setPermissionMode(sessionId, 'default');

    const questionMarker = `E2E_CHANNEL_QUESTION_${runId}`;
    const questionText = [
      `${questionMarker}: call AskUserQuestion exactly once as your first and only tool.`,
      'Ask exactly two questions. The first is single-select with options DIRECT and ANALYZE.',
      'The second is multi-select with options LOAN and AUTO.',
      'Wait for the user answer and do not answer the questions yourself.',
    ].join(' ');
    const questionReceipt = await triggerThreadMessage(request, {
      deploymentId,
      secret,
      threadId,
      messageId: `question-${runId}`,
      text: questionText,
    });
    expect(questionReceipt.status).toBe('accepted');
    expect(
      questionReceipt.session_id,
      'the external thread must reuse its existing Agent conversation',
    ).toBe(sessionId);

    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. The probe returns the moment the turn settles ungated,
    // so a declined ask costs that turn rather than the whole budget.
    const pending = await insist<PendingInteraction>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(sessionId, INSIST_NUDGE);
      },
      probe: () =>
        api.waitForPendingInteractionOrSettledTurn(sessionId, TURN_TIMEOUT_MS),
      what: 'the configured model did not invoke AskUserQuestion, so the expired channel question was not exercised',
      budgetMs: TURN_TIMEOUT_MS * 2,
      probeMs: TURN_TIMEOUT_MS,
    });
    const interaction = pending as PendingInteraction;
    expect(String(interaction.presentation || '')).toBe('form');
    expect(String(interaction.tool_name || '')).toBe('AskUserQuestion');
    const questions = Array.isArray(interaction.questions)
      ? interaction.questions as Array<Record<string, unknown>>
      : [];
    expect(questions, 'the channel question must expose both requested fields').toHaveLength(2);
    const interactionId = String(interaction.interaction_id || '').trim();
    expect(interactionId).not.toEqual('');

    const waiting = await api.getSession(sessionId);
    const oldSandbox = String(waiting.sandbox_id || '').trim();
    expect(oldSandbox, 'the parked channel question must still own compute').not.toEqual('');

    await page.goto(appPath(`/sessions/${sessionId}`), { waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: PANEL_TIMEOUT_MS });
    const panel = page.getByTestId('pending-interaction-panel');
    await expect(
      panel,
      'the channel-created question must reach the same browser interaction surface',
    ).toBeVisible({ timeout: PANEL_TIMEOUT_MS });

    const reclaim = await api.terminateSandbox(sessionId);
    expect(reclaim.status).toBe('sandbox-reclaimed');
    expect(String(reclaim.sandbox_id || '')).toBe(oldSandbox);
    const reclaimed = await api.waitForSession(sessionId, (session) => (
      session.state === 'READY'
      && !session.pending_interaction
      && !session.sandbox_id
      && String(session.last_turn_status || '') === 'FAILED'
    ), 60_000);
    expect(
      reclaimed.runtime_unavailable,
      'the formally reclaimed conversation must expose recoverable compute loss',
    ).toBe(true);

    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: PANEL_TIMEOUT_MS });
    await expect(
      panel,
      'the reclaimed question must not remain as an unanswerable browser card',
    ).toHaveCount(0);

    const staleFormAnswers = questions.map((question, index) => {
      const questionId = String(question.id || '').trim();
      expect(questionId, `question ${index + 1} must expose a stable id`).not.toEqual('');
      const options = Array.isArray(question.options)
        ? question.options as Array<Record<string, unknown>>
        : [];
      const optionLabel = String(options[0]?.label || '').trim();
      return {
        question_id: questionId,
        ...(optionLabel
          ? { option_label: optionLabel }
          : { free_text: `expired form answer ${index + 1}` }),
      };
    });
    const expiredForm = await request.fetch(
      apiPath(`/sessions/${sessionId}/interaction-respond`),
      {
        method: 'POST',
        data: {
          interaction_id: interactionId,
          answer: { answers: staleFormAnswers },
        },
        timeout: 30_000,
      },
    );
    const expiredFormBody = await expiredForm.text();
    expect(
      [400, 409],
      `the expired structured form must be refused; body=${expiredFormBody.slice(0, 300)}`,
    ).toContain(expiredForm.status());

    const answerText = [
      `E2E_CHANNEL_THREAD_ANSWER_${runId}.`,
      'For the first question choose DIRECT; for the second choose LOAN and AUTO.',
      'Do not call tools; acknowledge this answer briefly.',
    ].join(' ');
    const assistantsBefore = await assistantCount(api, sessionId);
    const answerReceipt = await triggerThreadMessage(request, {
      deploymentId,
      secret,
      threadId,
      messageId: `answer-${runId}`,
      text: answerText,
    });
    expect(answerReceipt.status).toBe('accepted');
    expect(
      answerReceipt.session_id,
      'the post-reclaim thread reply must continue the original conversation',
    ).toBe(sessionId);

    await expect.poll(
      () => exactUserInputCount(api, sessionId, answerText),
      {
        timeout: TURN_TIMEOUT_MS,
        intervals: [500, 1_000, 1_500],
        message: 'the thread answer must enter the original transcript exactly once',
      },
    ).toBe(1);
    await api.waitForAssistantMessageCount(sessionId, assistantsBefore, TURN_TIMEOUT_MS);
    const rebuilt = await api.waitForSession(sessionId, (session) => (
      session.state === 'READY'
      && String(session.last_turn_status || '') === 'COMPLETED'
      && Boolean(String(session.sandbox_id || '').trim())
      && String(session.sandbox_id || '').trim() !== oldSandbox
      && !session.pending_interaction
    ), TURN_TIMEOUT_MS);
    expect(rebuilt.last_error ?? null).toBeNull();

    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: PANEL_TIMEOUT_MS });
    await expect(
      page.getByTestId('user-message').filter({ hasText: `E2E_CHANNEL_THREAD_ANSWER_${runId}` }),
      'the same-thread answer must render once after a cold browser read',
    ).toHaveCount(1);
    await expect(panel).toHaveCount(0);

    test.info().annotations.push(
      { type: 'e2e_channel_thread_session_id', description: sessionId },
      { type: 'e2e_reclaimed_sandbox_id', description: oldSandbox },
      {
        type: 'e2e_reborrowed_sandbox_id',
        description: String(rebuilt.sandbox_id || ''),
      },
    );
  } finally {
    // Session, deployment, and Agent cleanup all run only after a pass. A
    // failure keeps the entire channel-created conversation as evidence.
  }
});

test('expired channel answer is rejected after its callback exits on the same sandbox', async ({
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const threadId = `e2e-same-box-expired-question-${runId}`;
  const agent = await api.createColdTestAgent(`__e2e_same_box_expired_question_${runId}`);
  agentId = String(agent.agent_id || '').trim();
  expect(agentId).not.toBe('');
  const deployment = await platform.createDeployment(agentId, {
    scene: 'channel:generic_json', prompt_prefix: '',
  });
  deploymentId = String(deployment.deployment_id || '').trim();
  const secret = String(deployment.secret || '');
  expect(deploymentId).not.toBe('');
  expect(secret).not.toBe('');

  // Match the donor's established channel parent before opening its form.
  const warm = await triggerThreadMessage(request, {
    deploymentId, secret, threadId, messageId: `warm-${runId}`,
    text: `E2E_CHANNEL_WARM_${runId}: do not use tools; reply briefly.`,
  });
  expect(warm.status).toBe('accepted');
  const sessionId = String(warm.session_id || '').trim();
  expect(sessionId).not.toBe('');
  sessions.push(sessionId);
  const warmReply = await api.waitForAssistantMessageMatching(
    sessionId, 0, (message) => messageText(message).trim().length > 0, TURN_TIMEOUT_MS,
  );
  expect(messageText(warmReply).trim()).not.toBe('');
  expect(messageText(warmReply).trim()).not.toMatch(/^API Error:\s*\d+\b/);
  await api.waitForSessionReady(sessionId, TURN_TIMEOUT_MS);
  await api.setPermissionMode(sessionId, 'default');

  const questionReceipt = await triggerThreadMessage(request, {
    deploymentId, secret, threadId, messageId: `question-${runId}`,
    text: [
      `E2E_CHANNEL_QUESTION_${runId}: call AskUserQuestion exactly once as your first and only tool.`,
      'Ask exactly two questions. The first is single-select with options DIRECT and ANALYZE.',
      'The second is multi-select with options LOAN and AUTO.',
      'Wait for the user answer and do not answer the questions yourself.',
    ].join(' '),
  });
  expect(questionReceipt.status).toBe('accepted');
  expect(questionReceipt.session_id).toBe(sessionId);
  const interaction = await api.waitForPendingInteraction(sessionId, TURN_TIMEOUT_MS);
  expect(interaction.presentation).toBe('form');
  expect(interaction.tool_name).toBe('AskUserQuestion');
  const questions = Array.isArray(interaction.questions)
    ? interaction.questions as Array<Record<string, unknown>> : [];
  expect(questions).toHaveLength(2);
  const interactionId = String(interaction.interaction_id || '').trim();
  expect(interactionId).not.toBe('');
  const waiting = await api.getSession(sessionId);
  const oldSandbox = String(waiting.sandbox_id || '').trim();
  expect(oldSandbox).not.toBe('');

  const toolId = String(interaction.tool_call_id || '').trim();
  expect(toolId).not.toBe('');
  expect(questions.filter((question) => question.multi_select === true)).toHaveLength(1);
  const staleFormAnswers = questions.map((question, index) => {
    const questionId = String(question.id || '').trim();
    expect(questionId, `question ${index + 1} must expose a stable id`).not.toEqual('');
    const options = Array.isArray(question.options)
      ? question.options as Array<Record<string, unknown>> : [];
    expect(options).toHaveLength(2);
    const labels = options.map((option) => String(option.label || '').trim());
    expect(labels.every(Boolean), 'the real form must expose both option labels').toBe(true);
    return { question_id: questionId, ...(question.multi_select === true
      ? { option_labels: labels } : { option_label: labels[0] }) };
  });
  const questionRecord = () => {
    const rows = documentsByField('interaction_snapshots', '$.session_id', sessionId)
      .filter((row) => row.interaction_id === interactionId);
    expect(rows).toHaveLength(1);
    return rows[0];
  };
  expect(questionRecord()).toMatchObject({ interaction_state: 'OPEN', active: true });
  await expect.poll(() => nativeQuestionEvidence(sessionId, toolId).calls.length, {
    message: 'the original native question must reach database custody before its callback exits',
  }).toBe(1);
  const nativeBefore = nativeQuestionEvidence(sessionId, toolId);
  expect(nativeBefore.results).toHaveLength(0);
  const commands = () => sessionEvents(sessionId).filter((event) => event.event_type === 'command.accepted');
  const commandsBefore = commands();
  const detail = await api.adminSessionDetail(sessionId);
  expect(detail.runtime_identity?.sandbox_id).toBe(oldSandbox);
  expect(String(detail.runtime_identity?.isolated_session_id || '')).toBe('');
  const sandbox = await requireSandboxHandle(api, oldSandbox);
  expect(sandboxRunning(sandbox)).toBe(true);
  const readPid = () => sandboxExec(sandbox, `${imageRunnerPidLookup()}\nprintf '%s\\n' "$1"`).trim();
  const oldPid = readPid();
  expect(oldPid).toMatch(/^[1-9][0-9]+$/);
  const restart = sandboxExec(sandbox, imageRunnerRestartScript({
    signal: 'KILL',
    launch: IMAGE_RUNNER_LAUNCHER,
    log: '/tmp/astrabox-expired-channel-question-runner.log',
    evidence: 'printf \'old_pid=%s\\n\' "$old_pid"',
    name: 'expired-question image runner',
  }), 30_000).trim();
  expect(restart).toBe(`old_pid=${oldPid}`);
  const newPid = readPid();
  expect(newPid).toMatch(/^[1-9][0-9]+$/);
  expect(newPid).not.toBe(oldPid);
  await api.adminEvictRuntime(sessionId);
  expect(sessionDoc(sessionId)?.sandbox_id).toBe(oldSandbox);
  expect(sandboxRunning(sandbox), 'callback loss must not mean box loss').toBe(true);
  expect(questionRecord()).toMatchObject({ interaction_state: 'OPEN', active: true });

  const exitedAnswer = await request.post(apiPath(`/sessions/${sessionId}/interaction-respond`), {
    data: { interaction_id: interactionId, answer: { answers: staleFormAnswers } },
    timeout: 30_000,
  });
  const exitedBody = await exitedAnswer.text();
  await test.info().attach('same-box-expired-answer', {
    body: JSON.stringify({ sessionId, sandbox_id: oldSandbox, interactionId, toolId,
      oldPid, newPid, restart, status: exitedAnswer.status(), body: exitedBody }),
    contentType: 'application/json',
  });
  expect(exitedAnswer.status(), exitedBody).toBe(409);
  expect(JSON.parse(exitedBody)).toMatchObject({ code: 'INTERACTION_EXPIRED' });
  expect(questionRecord()).toMatchObject({ interaction_state: 'OPEN', active: true });
  expect(questionRecord().answer_command_id || null).toBeNull();
  expect(commands(), 'rejecting the old form must not accept another command').toEqual(commandsBefore);
  expect(sessionEvents(sessionId).filter((event) => event.event_type === 'interaction.answer_persisted'))
    .toHaveLength(0);
  expect(nativeQuestionEvidence(sessionId, toolId), 'an expired form must not become native input or a tool result')
    .toEqual(nativeBefore);
  expect(sessionDoc(sessionId)?.sandbox_id).toBe(oldSandbox);
  expect(sandboxRunning(sandbox)).toBe(true);
  expect(readPid(), 'the rejection must use the replacement process, not restart the box').toBe(newPid);
});
