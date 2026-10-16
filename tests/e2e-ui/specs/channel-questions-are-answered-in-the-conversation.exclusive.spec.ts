/**
 * A messaging conversation shows the question its Agent waits on and carries the answer back.
 *
 * Every assertion reads what the channel itself received (the generic_json
 * callback) and the side effect the answer gates, not only the Session
 * history: a parked turn that never reaches the conversation, or an answer
 * that silently became a new turn, both leave a plausible-looking history.
 */
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText, visibleMessages } from '../fixtures/astraApi';
import { channelCallback, type ChannelDelivery } from '../fixtures/channelCallback';
import {
  decisionId,
  engineCases,
  engineProfileFor,
  modeName,
  toolName,
  type EngineProfile,
} from '../fixtures/engineProfile';
import { apiPath } from '../fixtures/env';
import { channelInbound } from '../fixtures/ingressRecords';
import { PlatformApi } from '../fixtures/platformApi';
import { satoriSource } from '../fixtures/satoriSource';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
const created: Array<{ agentId: string; deploymentId: string }> = [];
onPassOnly(async ({ request }) => {
  for (const { agentId, deploymentId } of created.splice(0)) {
    if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
    if (agentId) await new AstraApi(request).deleteAgent(agentId);
  }
});

type Recipient = Awaited<ReturnType<typeof channelCallback>>;

interface Conversation {
  recipient: Recipient;
  sessionId: string;
  send(message: { text?: string; replyTo?: string; action?: string }): Promise<void>;
  deliveries(): ChannelDelivery[];
}

/** A generic_json conversation on a fresh Agent of this engine, warmed by one reply. */
async function openConversation(request: APIRequestContext, profile: EngineProfile): Promise<Conversation> {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const id = randomUUID();
  const agentId = (await api.createAgent({
    name: `__e2e_channel_question_${profile.engine_kind}_${id}`,
    environment_name: profile.environment_name, model: profile.model, prepared_capacity: 0,
  })).agent_id;
  const deployment = await platform.createDeployment(agentId, { scene: 'channel:generic_json', prompt_prefix: '' });
  created.push({ agentId, deploymentId: deployment.deployment_id });
  const recipient = await channelCallback();
  let sessionId = '';
  let sequence = 0;
  async function send({ text = '', replyTo, action }: { text?: string; replyTo?: string; action?: string }) {
    sequence += 1;
    const response = await request.post(apiPath(`/deployments/${deployment.deployment_id}/trigger`), {
      headers: { 'x-channel-secret': String(deployment.secret) },
      data: {
        text, message_id: `${id}-${sequence}`, conversation_id: id,
        reply: { callback_url: recipient.url },
        ...(replyTo ? { reply_to: replyTo } : {}),
        ...(action ? { action } : {}),
      },
    });
    expect(response.ok(), await response.text()).toBe(true);
    const receipt = (await response.json()).data;
    expect(receipt.status).toBe('accepted');
    if (!sessionId) sessionId = String(receipt.session_id || '');
    expect(String(receipt.session_id || ''), 'one thread must keep one conversation').toBe(sessionId);
  }
  const deliveries = () => {
    expect(recipient.errors).toEqual([]);
    return [...recipient.deliveries];
  };
  await send({ text: `Reply exactly READY_${id}. Do not use tools.` });
  sessions.push(sessionId);
  await expect.poll(() => deliveries().map((row) => row.text), { timeout: 60_000 })
    .toEqual([expect.stringContaining(`READY_${id}`)]);
  await api.waitForSessionReady(sessionId);
  return { recipient, get sessionId() { return sessionId; }, send, deliveries };
}

/** A command only this engine's gated mode makes it ask for, writing a file the test can read. */
async function approvalPrompt(api: AstraApi, profile: EngineProfile, sessionId: string, marker: string) {
  const detail = await api.adminSessionDetail(sessionId);
  const root = String(detail.runtime_identity?.workspace_dir || '').replace(/\/+$/, '');
  expect(root, 'the session must report its workspace').toMatch(/^\//);
  const filename = `${marker}.txt`;
  const target = `${root}/${filename}`;
  await api.uploadFileText(sessionId, root, filename, 'NOT_EXECUTED');
  const engine = profile.engine_kind;
  const command = `printf %s '${marker}' > '${target}'`;
  const prompt = [
    engine === 'deepseek_harness'
      ? 'Use bash for one command, following its native denial-then-escalation flow.'
      : `Use your ${toolName(profile, 'command')} shell command tool exactly once.`,
    `Execute exactly this command: ${command}`,
    'This must be your first tool call. Do not inspect files, delegate, or use a file-editing tool.',
    'If this command needs permission, request execution approval and wait for my decision.',
    ...(engine === 'codex' ? [
      'Use exec_command with sandbox_permissions="require_escalated" and justification="Run the requested approval verification command" on the initial call, not after a failed unapproved attempt.',
    ] : []),
    ...(engine === 'deepseek_harness' ? [
      'First call bash without escalation. After its read-only sandbox denial, retry exactly the same command once with sandbox_permissions="workspace-write" and a justification based on that denial, then wait for my approval. Do not make any other tool calls.',
    ] : []),
    `After the command succeeds, reply exactly DONE_${marker} without calling more tools.`,
  ].join('\n');
  return { prompt, target };
}

/** The delivery that carries the question the Session is waiting on now. */
async function questionDelivery(conversation: Conversation, after: number): Promise<ChannelDelivery> {
  await expect.poll(() => conversation.deliveries().slice(after).filter((row) => row.question).length, {
    timeout: 120_000,
    message: 'the parked turn must reach the conversation with its question',
  }).toBeGreaterThan(0);
  return conversation.deliveries().slice(after).find((row) => row.question)!;
}

const approving = engineCases().filter((profile) => profile.contracts.tool_approval === true);
const asking = engineCases().filter((profile) => profile.contracts.question_interaction === true);

for (const declared of approving) {
  const engine = declared.engine_kind;

  test(`${engine}: a channel approval arrives with the reply and is answered by its label`, async ({ request }) => {
    const profile = engineProfileFor(engine);
    const api = new AstraApi(request);
    const conversation = await openConversation(request, profile);
    const { sessionId } = conversation;
    await api.setPermissionMode(sessionId, modeName(profile, 'gated'));
    const marker = `CHANNEL_APPROVAL_${engine}_${Date.now()}`;
    const { prompt, target } = await approvalPrompt(api, profile, sessionId, marker);
    const before = conversation.deliveries().length;
    await conversation.send({ text: prompt });

    const shown = await questionDelivery(conversation, before);
    const pending = await api.waitForPendingInteraction(sessionId);
    const question = shown.question!;
    expect(question.interaction_id, 'the channel must show the interaction the Session waits on')
      .toBe(pending.interaction_id);
    expect(question.presentation).toBe(profile.presentation);
    expect(shown.text, 'text-only channels receive the same question').toContain(question.text);
    const approve = decisionId(profile, 'approve');
    expect(question.choices.map((choice) => choice.label.toLowerCase())).toContain(approve.toLowerCase());
    expect(await api.downloadFileText(sessionId, target), 'nothing runs before the answer').toBe('NOT_EXECUTED');

    const usersBefore = visibleMessages(await api.getMessages(sessionId, 100))
      .filter((message) => message.role === 'user').length;
    const answeredAt = conversation.deliveries().length;
    await conversation.send({ text: approve });

    await expect.poll(() => api.downloadFileText(sessionId, target), { timeout: 120_000 }).toBe(marker);
    await expect.poll(() => conversation.deliveries().slice(answeredAt).map((row) => row.text).join('\n'), {
      timeout: 120_000,
      message: 'the reply after the answer must reach the conversation',
    }).toContain(`DONE_${marker}`);
    const settled = await api.waitForSessionReady(sessionId);
    expect(settled.last_response?.response_id, 'the answer continued the response that asked')
      .toBe(pending.response_id);
    expect(settled.last_response?.outcome).toBe('completed');
    expect(conversation.deliveries().slice(answeredAt).filter((row) => row.question),
      'an accepted answer asks nothing again').toEqual([]);
    const users = visibleMessages(await api.getMessages(sessionId, 100))
      .filter((message) => message.role === 'user');
    expect(users.length, 'the answer continues the turn; it is not a new message').toBe(usersBefore);
  });

  test(`${engine}: a new channel message stops the turn waiting on an approval`, async ({ request }) => {
    const profile = engineProfileFor(engine);
    const api = new AstraApi(request);
    const conversation = await openConversation(request, profile);
    const { sessionId } = conversation;
    await api.setPermissionMode(sessionId, modeName(profile, 'gated'));
    const marker = `CHANNEL_SUPERSEDE_${engine}_${Date.now()}`;
    const { prompt, target } = await approvalPrompt(api, profile, sessionId, marker);
    const before = conversation.deliveries().length;
    await conversation.send({ text: prompt });
    const shown = await questionDelivery(conversation, before);
    const parked = String((await api.waitForPendingInteraction(sessionId)).response_id || '');
    expect(parked).not.toBe('');

    const next = `NEXT_${randomUUID()}`;
    const supersededAt = conversation.deliveries().length;
    await conversation.send({ text: `Forget the command. Reply exactly ${next} and do not use tools.` });

    await expect.poll(() => conversation.deliveries().slice(supersededAt).map((row) => row.text).join('\n'), {
      timeout: 120_000,
      message: 'the new message must run instead of waiting behind the question',
    }).toContain(next);
    const settled = await api.waitForSessionReady(sessionId);
    expect(settled.last_response?.outcome, 'the new message ran to completion').toBe('completed');
    expect(settled.last_response?.response_id).not.toBe(parked);
    const stopped = visibleMessages(await api.getMessages(sessionId, 100))
      .find((message) => message.role === 'assistant' && message.id === parked);
    expect(stopped?.metadata?.outcome, 'the response waiting on the approval was stopped, not answered')
      .toBe('cancelled');
    expect(await api.downloadFileText(sessionId, target), 'a superseded approval is never granted')
      .toBe('NOT_EXECUTED');

    // The old question is closed: answering it now is refused, not run.
    const usersBefore = visibleMessages(await api.getMessages(sessionId, 100))
      .filter((message) => message.role === 'user').length;
    const lateAt = conversation.deliveries().length;
    await conversation.send({ text: decisionId(profile, 'approve'), replyTo: shown.messageId });
    await expect.poll(() => conversation.deliveries().slice(lateAt).map((row) => row.text).join('\n'), {
      timeout: 60_000,
    }).toContain('no longer open');
    await api.waitForSessionReady(sessionId);
    expect(await api.downloadFileText(sessionId, target)).toBe('NOT_EXECUTED');
    expect(visibleMessages(await api.getMessages(sessionId, 100))
      .filter((message) => message.role === 'user').length, 'a refused answer starts no turn')
      .toBe(usersBefore);
  });
}

for (const declared of asking) {
  const engine = declared.engine_kind;

  test(`${engine}: a two-question form is asked one question at a time in the channel`, async ({ request }) => {
    const profile = engineProfileFor(engine);
    const api = new AstraApi(request);
    const conversation = await openConversation(request, profile);
    const { sessionId } = conversation;
    if (profile.modes.gated) await api.setPermissionMode(sessionId, modeName(profile, 'gated'));
    const marker = `CHANNEL_FORM_${engine}_${Date.now()}`;
    const before = conversation.deliveries().length;
    await conversation.send({
      text: [
        `Call ${toolName(profile, 'question')} exactly once as your first and only tool, with two single-select questions.`,
        'Question one: "Which colour?" with options RED and BLUE.',
        'Question two: "Which size?" with options SMALL and LARGE.',
        `Wait for the answers, then reply exactly ${marker} followed by the two chosen options. Do not answer the questions yourself.`,
      ].join('\n'),
    });

    const first = await questionDelivery(conversation, before);
    expect(first.question!.position).toEqual([1, 2]);
    const blue = first.question!.choices.find((choice) => choice.label === 'BLUE');
    expect(blue, 'the first question must offer BLUE').toBeTruthy();

    const firstAt = conversation.deliveries().length;
    await conversation.send({ text: String(blue!.number), replyTo: first.messageId });
    const second = await questionDelivery(conversation, firstAt);
    expect(second.question!.interaction_id).toBe(first.question!.interaction_id);
    expect(second.question!.position).toEqual([2, 2]);
    expect((await api.getSession(sessionId)).pending_interaction, 'one answer is not the whole form')
      .toBeTruthy();
    const large = second.question!.choices.find((choice) => choice.label === 'LARGE');
    expect(large?.action, 'a single-select choice carries a button action').toBeTruthy();

    const secondAt = conversation.deliveries().length;
    await conversation.send({ action: large!.action });
    await expect.poll(() => conversation.deliveries().slice(secondAt).map((row) => row.text).join('\n'), {
      timeout: 120_000,
      message: 'the reply after the form must reach the conversation',
    }).toContain(marker);
    const texts = conversation.deliveries().slice(secondAt).map((row) => row.text);
    // The confirmation names every answer the form gave, in question order.
    expect(texts.some((text) => text.includes('Answered: BLUE; LARGE')),
      'a button press is confirmed in the conversation').toBe(true);
    await api.waitForSessionReady(sessionId);
    const reply = visibleMessages(await api.getMessages(sessionId, 100))
      .filter((message) => message.role === 'assistant').map(messageText).join('\n');
    expect(reply).toContain('BLUE');
    expect(reply).toContain('LARGE');
  });
}

/** The buttons an official Satori delivery carried, as (action id, label). */
function satoriButtons(content: string): Array<{ id: string; label: string }> {
  return [...content.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)].map((match) => ({
    id: (/\bid="([^"]*)"/.exec(match[1]) || [])[1] || '',
    label: match[2],
  }));
}

test('a group on the official Satori adapter answers by number and by button', async ({ request }) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const source = await satoriSource();
  const id = randomUUID();
  const marker = `SATORI_FORM_${id}`;
  try {
    const agentId = (await api.createColdTestAgent(`__e2e_satori_question_${id}`)).agent_id;
    const deployment = await platform.createDeployment(agentId, {
      scene: 'channel:satori', prompt_prefix: '', attention_policy: 'all',
      channel_config: { endpoint: source.endpoint }, credentials: { token: source.token },
    });
    created.push({ agentId, deploymentId: deployment.deployment_id });
    await source.waitConnected();
    const room = { id: `room-${id}`, type: 0 };
    let sequence = 0;
    function say(content: string) {
      sequence += 1;
      source.send({ type: 'message-created', timestamp: Date.now(), channel: room,
        user: { id: 'participant', name: 'Participant' },
        message: { id: `${id}-${sequence}`, content, created_at: Date.now() } });
    }
    say([
      'Call AskUserQuestion exactly once as your first and only tool, with two single-select questions.',
      'Question one: &quot;Which colour?&quot; with options RED and BLUE.',
      'Question two: &quot;Which size?&quot; with options SMALL and LARGE.',
      `Wait for the answers, then reply exactly ${marker} followed by the two chosen options.`,
      'Do not answer the questions yourself.',
    ].join(' '));
    let sessionId = '';
    await expect.poll(() => {
      source.healthy();
      const inbound = channelInbound(deployment.deployment_id, `e2e:${source.botId}:channel:room-${id}:message:${id}-1`);
      sessionId = String(inbound?.session_id || '');
      return sessionId;
    }, { timeout: 75_000 }).not.toBe('');
    sessions.push(sessionId);

    const withButtons = () => source.deliveries.filter((row) => satoriButtons(row.content).length > 0);
    await expect.poll(() => { source.healthy(); return withButtons().length; }, {
      timeout: 120_000, message: 'the question must reach the Satori channel with its buttons',
    }).toBe(1);
    const first = withButtons()[0];
    expect(first.content).toContain('Question 1 of 2');
    expect(satoriButtons(first.content).map((button) => button.label)).toEqual(['1. RED', '2. BLUE']);

    // A bare number in the group answers the question shown; it starts no turn.
    const usersBefore = visibleMessages(await api.getMessages(sessionId, 100))
      .filter((message) => message.role === 'user').length;
    say('2');
    await expect.poll(() => { source.healthy(); return withButtons().length; }, {
      timeout: 60_000, message: 'answering the first question must show the second',
    }).toBe(2);
    const second = withButtons()[1];
    expect(second.content).toContain('Question 2 of 2');
    const large = satoriButtons(second.content).find((button) => button.label === '2. LARGE');
    expect(large?.id, 'the second question must offer LARGE as a button').toBeTruthy();

    source.send({ type: 'interaction/button', timestamp: Date.now(), channel: room,
      user: { id: 'participant', name: 'Participant' },
      button: { id: large!.id }, message: { id: `press-${id}` } });
    await expect.poll(() => { source.healthy(); return source.deliveries.map((row) => row.content).join('\n'); }, {
      timeout: 120_000, message: 'the reply after the form must reach the Satori channel',
    }).toContain(marker);
    expect(source.deliveries.some((row) => row.content.includes('Answered: BLUE; LARGE')),
      'the button press is confirmed in the group').toBe(true);
    await api.waitForSessionReady(sessionId);
    const history = visibleMessages(await api.getMessages(sessionId, 100));
    expect(history.filter((message) => message.role === 'user').length,
      'neither answer is a new message to the Agent').toBe(usersBefore);
    const reply = history.filter((message) => message.role === 'assistant').map(messageText).join('\n');
    expect(reply).toContain('BLUE');
    expect(reply).toContain('LARGE');
  } finally {
    await source.close();
  }
});
