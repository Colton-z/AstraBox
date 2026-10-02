/** A channel receives the real follow-up to a background task without another input. */
import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { channelCallback } from '../fixtures/channelCallback';
import { deleteDocs, documentsByField, framesForTurn, replaceDocs, sessionEvents } from '../fixtures/dbOracle';
import { absoluteBaseUrl, apiPath } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { restartServerContainer } from '../fixtures/sandboxOps';
import { satoriReplyTexts, satoriSource } from '../fixtures/satoriSource';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

const cases = [
  { mode: 'generic', title: 'a background completion reaches its channel without a second inbound message' },
  { mode: 'satori', title: 'a background completion reaches the official Satori channel without another input' },
  { mode: 'browser', title: 'a channel subscription receives the same mainline replies after browser input' },
  { mode: 'restart', title: 'a background channel subscription survives restart without replaying delivered replies' },
  { mode: 'historical', title: 'a new channel output subscription does not broadcast historical background replies' },
] as const;

for (const scenario of cases) test(scenario.title, async ({
  request, page,
}, info) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const recipient = scenario.mode === 'satori' ? null : await channelCallback();
  const source = scenario.mode === 'satori' ? await satoriSource() : null;
  const id = randomUUID();
  async function deliveries(): Promise<string[]> {
    if (!source) return recipient!.deliveries.map((row) => row.text);
    return satoriReplyTexts(page, source.deliveries, id);
  }
  function healthy() {
    source?.healthy();
    if (recipient?.errors.length) throw new Error(recipient.errors.join('; '));
  }
  const evidence: Record<string, unknown> = {};
  let sessionId = '';
  let stoppedServer = '';
  try {
    const launched = `LAUNCHED_${id}`;
    const completed = `COMPLETED_${id}`;
    const releaseFile = `channel-background-${id}.release`;
    const prompt = [
      'Launch exactly one Agent with run_in_background=true.',
      'The child must use Bash to run this exact command:',
      "python3 - <<'PY'",
      'from pathlib import Path',
      'import time',
      `release = Path('/workspace/${releaseFile}')`,
      'deadline = time.monotonic() + 120',
      'while not release.exists():',
      '    if time.monotonic() >= deadline:',
      "        raise TimeoutError('channel test did not release the task')",
      '    time.sleep(0.2)',
      `print('${completed}')`,
      'PY',
      `The child must report ${completed} verbatim after the command completes.`,
      `Immediately after launching, reply only ${launched} verbatim and end your turn.`,
      'Do not wait for the child and do not run Bash in the parent.',
      `When the child's completion notice arrives, send a new reply containing ${completed} verbatim.`,
      'Do not claim completion or mention the completion token before that notice.',
    ].join('\n');
    agentId = (await api.createColdTestAgent(`__e2e_channel_background_${id}`)).agent_id;
    const deployment = await platform.createDeployment(agentId, source ? {
      scene: 'channel:satori', prompt_prefix: '', attention_policy: 'mentions',
      channel_config: { endpoint: source.endpoint }, credentials: { token: source.token },
    } : { scene: 'channel:generic_json', prompt_prefix: '' });
    deploymentId = deployment.deployment_id;
    const channelPrompt = scenario.mode === 'browser'
      ? `Conversation ${id}. Explain what a notebook is in one short sentence. Do not use tools.` : prompt;
    const expectedInputs = scenario.mode === 'browser' ? [channelPrompt, prompt] : [prompt];
    if (source) {
      await source.waitConnected();
      const content = channelPrompt.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
      source.send({ type: 'message-created', timestamp: Date.now(),
        channel: { id, type: 0 }, user: { id: 'participant', name: 'Participant' },
        message: { id, content: `<at id="${source.botId}"/>${content}`, created_at: Date.now() } });
      await expect.poll(() => {
        healthy();
        const inbound = documentsByField('channel_inbound', '$.deployment_id', deploymentId)
          .find((row) => row.dedup_key === `e2e:${source.botId}:message:${id}`);
        sessionId = String(inbound?.session_id || '');
        return sessionId;
      }, { timeout: 75_000 }).not.toBe('');
    } else {
      expect(deployment.secret).toBeTruthy();
      const triggered = await request.post(apiPath(`/deployments/${deploymentId}/trigger`), {
        headers: { 'x-channel-secret': String(deployment.secret) },
        data: { text: channelPrompt, message_id: id, conversation_id: id,
          reply: { callback_url: recipient!.url } },
      });
      expect(triggered.ok(), await triggered.text()).toBe(true);
      const receipt = (await triggered.json()).data;
      expect(receipt.status).toBe('accepted');
      sessionId = String(receipt.session_id || '');
    }
    expect(sessionId).not.toBe('');
    sessions.push(sessionId);

    await expect.poll(() => {
      healthy();
      return deliveries();
    }, { timeout: 75_000, message: 'the launch reply must reach the channel while the child is held' })
      .toEqual([scenario.mode === 'browser' ? expect.stringMatching(/\S/) : expect.stringContaining(launched)]);
    const initialDelivery = (await deliveries())[0];
    expect(initialDelivery).not.toContain(completed);
    await expect.poll(() => documentsByField('channel_outbox', '$.session_id', sessionId)
      .filter((row) => row.state === 'DELIVERED').length).toBe(1);
    if (scenario.mode === 'browser') {
      await openSessionView(page, sessionId);
      await sendPrompt(page, sessionId, prompt);
      await expect(page.getByTestId('assistant-text').filter({ hasText: launched })).toBeVisible();
    }
    const children = await api.waitForChildRuns(sessionId,
      (rows) => rows.length === 1 && rows[0].active, 15_000);
    evidence.heldChildren = children;
    const initialHistory = await api.getMessages(sessionId, 50);
    expect(initialHistory.messages.filter((row) => row.role === 'user').map(messageText)).toEqual(expectedInputs);
    const initialReplies = initialHistory.messages.filter((row) => row.role === 'assistant');
    evidence.initialHistory = initialHistory;

    if (scenario.mode === 'restart') {
      await restartServerContainer(absoluteBaseUrl(), 60_000);
      expect(await deliveries()).toEqual([initialDelivery]);
    }
    if (scenario.mode === 'historical') {
      const permissionMode = String((await api.getSession(sessionId)).permission_mode || '');
      expect(permissionMode).not.toBe('');
      const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
      execFileSync('docker', ['stop', '--time', '10', server], { timeout: 30_000 });
      stoppedServer = server;
      const removed = deleteDocs('channel_output_subscriptions', { '$.session_id': sessionId });
      expect(removed).toHaveLength(1);
      evidence.preSubscriptionHistory = removed;
      await restartServerContainer(absoluteBaseUrl(), 60_000, server);
      stoppedServer = '';
      // Model a pre-subscription conversation whose existing runtime is opened
      // through a user control. The native acknowledgement proves cold-cache
      // control recovery without sending another prompt or enabling replies.
      const control = await api.setPermissionMode(sessionId, permissionMode);
      expect(control).toMatchObject({ session_id: sessionId, permission_mode: permissionMode, applied: true });
      evidence.recoveredControl = control;
    }

    // A workspace file releases the native child; it is not a second user turn.
    await api.uploadFileText(sessionId, '', releaseFile, 'release\n');
    await expect.poll(async () => {
      const history = await api.getMessages(sessionId, 50);
      return history.messages.filter((row) => row.role === 'assistant'
        && !initialReplies.some((initial) => initial.message_id === row.message_id))
        .map(messageText).join('\n');
    }, { timeout: 60_000, message: 'the completed child must produce a distinct durable parent reply' })
      .toContain(completed);
    await openSessionView(page, sessionId);
    await expect(page.getByTestId('assistant-text').filter({ hasText: completed })).toBeVisible();
    const completedHistory = await api.getMessages(sessionId, 50);
    evidence.completedHistory = completedHistory;
    const completionReplies = completedHistory.messages.filter((row) => row.role === 'assistant'
      && !initialReplies.some((initial) => initial.message_id === row.message_id)
      && messageText(row).includes(completed));
    expect(completionReplies).toHaveLength(1);
    const nativeTranscript = documentsByField('transcript_entries', '$.platform_session_id', sessionId)
      .filter((row) => row.subpath == null)
      .sort((left, right) => Number(left.seq) - Number(right.seq))
      .map((row) => JSON.parse(String(row.entry_json)) as {
        type: string; message?: { content?: Array<{ type: string; text?: string }> };
      });
    evidence.nativeTranscript = nativeTranscript;
    const nativeAssistantText = nativeTranscript.filter((entry) => entry.type === 'assistant')
      .flatMap((entry) => entry.message?.content ?? [])
      .filter((block) => block.type === 'text').map((block) => block.text ?? '').join('\n');
    expect(nativeAssistantText, 'the completion must exist in the engine transcript').toContain(completed);

    let expectedDeliveries = [
      ...initialReplies.map((row) => messageText(row).trim()),
      messageText(completionReplies[0]).trim(),
    ];
    if (scenario.mode === 'historical') {
      expect(documentsByField('channel_output_subscriptions', '$.session_id', sessionId)).toEqual([]);
      expect(await deliveries()).toEqual([initialDelivery]);
      const nextPrompt = `Conversation ${id}. Explain what a calendar is in one short sentence. Do not use tools.`;
      const nextId = randomUUID();
      expectedInputs.push(nextPrompt);
      const next = await request.post(apiPath(`/deployments/${deploymentId}/trigger`), {
        headers: { 'x-channel-secret': String(deployment.secret) },
        data: { text: nextPrompt, message_id: nextId, conversation_id: id,
          reply: { callback_url: recipient!.url } },
      });
      expect(next.ok(), await next.text()).toBe(true);
      expect((await next.json()).data.session_id).toBe(sessionId);
      await expect.poll(() => documentsByField('channel_inbound', '$.deployment_id', deploymentId)
        .find((row) => row.dedup_key === nextId)?.state, { timeout: 60_000 }).toBe('SETTLED');
      const later = (await api.getMessages(sessionId, 50)).messages.filter((row) => row.role === 'assistant'
        && !completedHistory.messages.some((old) => old.message_id === row.message_id));
      expect(later).toHaveLength(1);
      expectedDeliveries = [initialDelivery, messageText(later[0]).trim()];
    }
    {
      await expect.poll(() => {
        healthy();
        return deliveries();
      }, { timeout: 30_000, message: 'the channel must receive the visible completion without another input' })
        .toEqual(expectedDeliveries);
    }
    const terminal = sessionEvents(sessionId).find((row) => row.turn_id === completionReplies[0].turn_id
      && row.event_type === 'turn.completed');
    expect(terminal).toBeTruthy();
    const completionFrames = framesForTurn(String(completionReplies[0].turn_id));
    const completionCursor = Math.max(...completionFrames.map((row) => Number(row.event_seq)));
    expect(Number.isFinite(completionCursor)).toBe(true);
    const subscription = () => documentsByField('channel_output_subscriptions', '$.session_id', sessionId)[0];
    await expect.poll(() => Number(subscription()?.after_seq), {
      timeout: 30_000, message: 'channel routing must finish before asserting no unwanted delivery',
    }).toBeGreaterThanOrEqual(completionCursor);
    expect(await deliveries()).toEqual(expectedDeliveries);
    const outboxes = () => documentsByField('channel_outbox', '$.session_id', sessionId);
    await expect.poll(() => outboxes().filter((row) => row.state === 'DELIVERED').length)
      .toBe(expectedDeliveries.length);
    const deliveredRows = outboxes();
    expect(deliveredRows).toHaveLength(expectedDeliveries.length);
    if (source) expect(source.deliveries.map((row) => row.channel_id)).toEqual([id, id]);

    if (scenario.mode === 'restart') {
      const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
      await page.goto('about:blank');
      execFileSync('docker', ['stop', '--time', '10', server], { timeout: 30_000 });
      stoppedServer = server;
      const saved = subscription();
      expect(replaceDocs('channel_output_subscriptions', { '$.session_id': sessionId }, { ...saved, after_seq: 0 }))
        .toEqual([saved]);
      await restartServerContainer(absoluteBaseUrl(), 60_000, server);
      stoppedServer = '';
      await expect.poll(() => Number(subscription()?.after_seq), { timeout: 30_000 })
        .toBeGreaterThanOrEqual(Number(saved.after_seq));
      expect(await deliveries()).toEqual(expectedDeliveries);
      expect(outboxes()).toEqual(deliveredRows);
    }
    expect(documentsByField('channel_inbound', '$.deployment_id', deploymentId)
      .filter((row) => !source || row.dedup_key === `e2e:${source.botId}:message:${id}`))
      .toHaveLength(scenario.mode === 'historical' ? 2 : 1);
    const finalHistory = await api.getMessages(sessionId, 50);
    expect(finalHistory.messages.filter((row) => row.role === 'user').map(messageText))
      .toEqual(expectedInputs);
  } finally {
    try {
      await info.attach('channel-background-delivery', {
        body: JSON.stringify({ ...evidence, sessionId, deploymentId,
          deliveries: source?.deliveries ?? recipient?.deliveries,
          recipientErrors: recipient?.errors, sourceErrors: source?.errors,
          events: sessionId ? sessionEvents(sessionId) : [],
          outbox: sessionId ? documentsByField('channel_outbox', '$.session_id', sessionId) : [],
        }),
        contentType: 'application/json',
      });
    } finally {
      if (stoppedServer) execFileSync('docker', ['start', stoppedServer], { timeout: 30_000 });
      await recipient?.close();
      await source?.close();
    }
  }
});
