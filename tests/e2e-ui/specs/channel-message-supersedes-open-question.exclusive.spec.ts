/**
 * A new channel message supersedes an unanswered questionnaire in that thread.
 *
 * A later message that does not answer the question means the participant
 * moved on: the response waiting on it is stopped, which closes the old
 * AskUserQuestion with an error result, then the message is answered by a
 * response of its own on the same resident sandbox. The old answer path must
 * not be reused as the new message's input.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText, messageToolCalls } from '../fixtures/astraApi';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { apiPath } from '../fixtures/env';

interface ChannelTriggerReceipt {
  deployment_id: string;
  session_id: string;
  status: string;
  [key: string]: unknown;
}

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
    secret,
    threadId,
    messageId,
    text,
  }: {
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

test('a newer channel message stops the open AskUserQuestion before its own turn', async ({
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const threadId = `e2e-channel-supersede-${runId}`;
  const base = await api.defaultAgent();
  const runtimeProfileName = String(base.runtime_profile_name || '').trim();
  expect(runtimeProfileName, 'the seeded Agent must name an environment').not.toEqual('');
  let model = String(base.model || '').trim();
  if (!model || model.includes('*')) {
    const models = await api.listEnvironmentModels(runtimeProfileName);
    model = models.find((candidate) => candidate && !candidate.includes('*')) || '';
  }
  expect(model, 'the Agent environment must expose a concrete model').not.toEqual('');

  const agent = await api.createAgent({
    name: `__e2e_channel_supersede_${runId}`,
    model,
    runtime_profile_name: runtimeProfileName,
    prepared_capacity: 0,
  });
  agentId = String(agent.agent_id || '').trim();
  const deployment = await platform.createDeployment(agentId, {
    scene: 'channel:generic_json',
    prompt_prefix: '',
  });
  deploymentId = String(deployment.deployment_id || '').trim();
  const secret = String(deployment.secret || '').trim();
  expect(deploymentId, 'the channel deployment must expose its id').not.toEqual('');
  expect(secret, 'the channel deployment must expose its trigger secret').not.toEqual('');

  const warm = await triggerThreadMessage(request, {
    secret,
    threadId,
    messageId: `warm-${runId}`,
    text: `E2E_CHANNEL_SUPERSEDE_WARM_${runId}: reply briefly without tools.`,
  });
  expect(warm.status).toBe('accepted');
  const sessionId = String(warm.session_id || '').trim();
  expect(sessionId).not.toEqual('');
  sessions.push(sessionId);
  await api.waitForAssistantMessageCount(sessionId, 0);
  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'default');

  const questionMarker = `E2E_CHANNEL_OPEN_ASK_${runId}`;
  const question = await triggerThreadMessage(request, {
    secret,
    threadId,
    messageId: `question-${runId}`,
    text: [
      `${questionMarker}: call AskUserQuestion exactly once as your first and only tool.`,
      'Ask one single-select question with options YES and NO.',
      'Wait for the answer and do not answer the question yourself.',
    ].join(' '),
  });
  expect(question.status).toBe('accepted');
  expect(question.session_id).toBe(sessionId);

  const pending = await api.waitForPendingInteraction(sessionId);
  expect(String(pending.tool_name || '')).toBe('AskUserQuestion');
  const parked = String(pending.response_id || '');
  expect(parked, 'the open question must name the response it pauses').not.toEqual('');
  const toolCallId = String(pending.tool_call_id || '').trim();
  expect(toolCallId, 'the open channel question must expose its engine tool id')
    .not.toEqual('');
  const waiting = await api.getSession(sessionId);
  const sandboxId = String(waiting.sandbox_id || '').trim();
  expect(sandboxId, 'the open channel question must still own its sandbox').not.toEqual('');

  const nextMarker = `E2E_CHANNEL_SUPERSEDING_MESSAGE_${runId}`;
  const assistantsBeforeNext = await api.assistantCount(sessionId);
  const next = await triggerThreadMessage(request, {
    secret,
    threadId,
    messageId: `next-${runId}`,
    text: `${nextMarker}: this is a new question. Reply with exactly ${nextMarker}_DONE and do not use tools.`,
  });
  expect(next.status).toBe('accepted');
  expect(next.session_id, 'the same external thread must keep one conversation')
    .toBe(sessionId);

  await api.waitForAssistantMessageMatching(
    sessionId,
    assistantsBeforeNext,
    (message) => messageText(message).includes(`${nextMarker}_DONE`),
  );
  const completed = await api.waitForSessionReady(sessionId);
  expect(completed.last_response?.outcome, 'the newer message must be answered to completion').toBe('completed');
  expect(completed.last_response?.response_id).not.toBe(parked);
  expect(
    String(completed.sandbox_id || ''),
    'superseding a channel question must not replace healthy resident compute',
  ).toBe(sandboxId);

  const history = await api.getMessages(sessionId, 100);
  const resultMessageIndex = history.messages.findIndex((message) => (
    message.role === 'assistant'
    && messageToolCalls(message).some((call) => call.toolCallId === toolCallId && call.state === 'output-error')
  ));
  expect(
    history.messages.find((message) => message.role === 'assistant' && message.id === parked)?.metadata?.outcome,
    'the response waiting on the question was stopped, not answered',
  ).toBe('cancelled');
  const nextMessageIndex = history.messages.findIndex((message) => (
    message.role === 'user' && messageText(message).includes(nextMarker)
  ));
  expect(
    resultMessageIndex,
    'the superseding message must close the abandoned questionnaire as an error result',
  ).toBeGreaterThanOrEqual(0);
  expect(
    nextMessageIndex,
    'the newer channel question must enter history as a new user input',
  ).toBeGreaterThan(resultMessageIndex);
  expect(
    history.messages.filter((message) => (
      message.role === 'assistant' && messageText(message).includes(`${nextMarker}_DONE`)
    )),
    'the newer channel question must produce one durable answer',
  ).toHaveLength(1);
});
