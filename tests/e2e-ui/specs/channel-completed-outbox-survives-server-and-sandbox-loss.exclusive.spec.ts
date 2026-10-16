/** Replay a completed channel reply from durable history, with no old process or box. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi, messageText, type MessagePage, type MessageRecord } from '../fixtures/astraApi';
import { channelCallback } from '../fixtures/channelCallback';
import { sessionInputRows, sessionResponseRows } from '../fixtures/dbOracle';
import { textBlockValues } from '../fixtures/dshOutput';
import { absoluteBaseUrl, apiPath } from '../fixtures/env';
import { abandonOutboxDelivery, channelOutboxRows } from '../fixtures/ingressRecords';
import { PlatformApi } from '../fixtures/platformApi';
import { killSandbox, requireSandboxHandle, restartServerContainer, sandboxRunning } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

function serverCommand(server: string, args: string[]) {
  return execFileSync('docker', [...args, server], {
    encoding: 'utf8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
}

/**
 * What a text-only channel is sent for one reply: the reply's text parts as
 * paragraphs (`channel_replies.segment_reply`), read here from history.
 */
function replyText(message: MessageRecord): string {
  return textBlockValues(message.parts).filter(Boolean).join('\n\n').trim();
}

/** A history page as the comparisons below read it: identity, role, text and how a response ended. */
function historyRows(page: MessagePage) {
  return page.messages.map((row) => ({
    id: row.id, role: row.role, text: messageText(row), outcome: row.metadata?.outcome ?? null,
  }));
}

test('a completed channel outbox replays its original reply after the server and sandbox disappear', async ({
  page, request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const recipient = await channelCallback();
  const evidence: Record<string, unknown> = {};
  let sessionId = '';
  let stopped = false;
  try {
    const unique = randomUUID();
    const prompt = `Conversation label ${unique}. Explain what a notebook is in one short plain sentence. Do not use tools.`;
    const agent = await api.createColdTestAgent(`__e2e_channel_completed_${unique}`);
    agentId = agent.agent_id;
    const deployment = await platform.createDeployment(agentId, {
      scene: 'channel:generic_json', prompt_prefix: '',
    });
    deploymentId = deployment.deployment_id;
    expect(deployment.secret).toBeTruthy();
    const triggered = await request.post(apiPath(`/deployments/${deploymentId}/trigger`), {
      headers: { 'x-channel-secret': String(deployment.secret) },
      data: {
        text: prompt, message_id: unique, conversation_id: unique,
        reply: { callback_url: recipient.url },
      },
    });
    expect(triggered.ok(), await triggered.text()).toBe(true);
    const receipt = (await triggered.json()).data;
    expect(receipt.status).toBe('accepted');
    sessionId = String(receipt.session_id || '');
    expect(sessionId).not.toEqual('');
    sessions.push(sessionId);
    let originalResponseId = '';
    const outbox = () => {
      const rows = channelOutboxRows(sessionId)
        .filter((row) => !originalResponseId || row.response_id === originalResponseId);
      expect(rows, 'the original mainline reply must own exactly one outbox').toHaveLength(1);
      return rows[0];
    };
    await expect.poll(() => {
      if (recipient.errors.length) throw new Error(recipient.errors.join('; '));
      return recipient.deliveries.length;
    }, { timeout: 60_000, message: 'the real baseline reply must reach the HTTP recipient' }).toBe(1);
    await expect.poll(() => outbox().state).toBe('DELIVERED');
    const ready = await api.waitForSessionReady(sessionId, 30_000);
    expect(ready.last_error ?? null).toBeNull();
    const originalOutbox = outbox();
    originalResponseId = originalOutbox.response_id;
    expect(originalResponseId).not.toEqual('');
    expect(originalOutbox.streaming).toBe(false);
    // The delivery reports the response that answered the channel message.
    const originalInputs = sessionInputRows(sessionId);
    expect(originalInputs.map((row) => ({ source: row.source, content: row.content, state: row.state })))
      .toEqual([{ source: 'channel', content: prompt, state: 'admitted' }]);
    const originalResponses = sessionResponseRows(sessionId);
    expect(originalResponses).toEqual([expect.objectContaining({
      response_id: originalResponseId, origin: 'input', status: 'ended', outcome: 'completed',
      input_ids: [originalInputs[0].input_id], error: null,
    })]);
    const originalHistory = await api.getMessages(sessionId, 50);
    expect(originalHistory.has_more).toBe(false);
    const originalRows = historyRows(originalHistory);
    expect(originalRows.filter((row) => row.role === 'user').map((row) => row.text)).toEqual([prompt]);
    const replies = originalHistory.messages.filter((row) => row.role === 'assistant');
    expect(replies).toHaveLength(1);
    expect(replies[0].id).toBe(originalResponseId);
    expect(messageText(replies[0]).trim()).not.toEqual('');
    expect(recipient.deliveries[0].text).toBe(replyText(replies[0]));
    await openSessionView(page, sessionId);
    const renderedReply = page.getByTestId('assistant-text');
    await expect(renderedReply).toHaveCount(1);
    await expect(renderedReply).toBeVisible();
    await expect(renderedReply).toHaveText(/\S/);
    const originalRenderedReply = await renderedReply.innerText();
    await page.goto('about:blank');
    const originalTranscript = await api.adminSessionTranscript(sessionId);
    expect(originalTranscript).toContain(unique);
    const nativeEntries = originalTranscript.split('\n').filter((line) => line.trim())
      .map((line) => JSON.parse(line) as Record<string, unknown>);
    const nativeUsers = nativeEntries.filter((entry) => entry.type === 'user'
      && JSON.stringify(entry.message).includes(unique));
    expect(nativeUsers, 'the original channel input must occur once in native custody').toHaveLength(1);
    const nativeAnswers = nativeEntries.filter((entry) => entry.type === 'assistant').flatMap((entry) => {
      const content = (entry.message as { content?: Array<{ type: string; text?: string }> }).content || [];
      return content.filter((block) => block.type === 'text').map((block) => block.text || '');
    });
    expect(nativeAnswers.join('')).toBe(messageText(replies[0]));
    const detail = await api.adminSessionDetail(sessionId);
    expect(String(detail.runtime_identity?.isolated_session_id || ''), 'the case owns its entire box').toEqual('');
    const oldSandbox = await requireSandboxHandle(api, String(ready.sandbox_id));
    evidence.baseline = { sessionId, originalOutbox, originalRows, originalRenderedReply,
      originalInputs, originalResponses, oldSandbox, deliveries: [...recipient.deliveries] };

    // Match the donor's incident boundary: successful baseline, then leave
    // this delivery as a deliverer that stopped mid-send leaves it, while the
    // platform cannot race the change.
    serverCommand(server, ['stop', '--time', '10']);
    stopped = true;
    expect(serverCommand(server, ['inspect', '--format', '{{.State.Running}}'])).toBe('false');
    const deadOwner = `dead-${unique}`;
    const deadGeneration = Number(originalOutbox.lease_generation) + 1;
    expect(Number.isSafeInteger(deadGeneration)).toBe(true);
    const abandoned = abandonOutboxDelivery(originalOutbox.outbox_id, deadOwner);
    expect(abandoned).toEqual([{
      ...originalOutbox, state: 'SENDING', lease_owner: deadOwner, lease_generation: deadGeneration,
      lease_live: false, next_attempt_at: null,
    }]);
    expect(outbox()).toEqual(abandoned[0]);
    killSandbox(oldSandbox);
    await expect.poll(() => sandboxRunning(oldSandbox), {
      timeout: 30_000, message: 'both the old sandbox ownership root and compute must disappear',
    }).toBe(false);
    evidence.abandoned = abandoned[0];
    evidence.oldSandboxStopped = true;
    await restartServerContainer(absoluteBaseUrl(), 60_000, server);
    stopped = false;
    await expect.poll(() => {
      if (recipient.errors.length) throw new Error(recipient.errors.join('; '));
      return recipient.deliveries.length;
    }, { timeout: 45_000, message: 'the replacement platform must deliver the stored reply without its old box' }).toBe(2);
    await expect.poll(() => outbox().state).toBe('DELIVERED');
    const recovered = outbox();
    expect(recovered.outbox_id).toBe(originalOutbox.outbox_id);
    expect(recovered.response_id).toBe(originalResponseId);
    expect(recovered.lease_generation).toBe(deadGeneration + 1);
    expect(recovered.lease_owner).toBeNull();
    // Compute loss is a lifecycle fact; it must not fail the completed reply.
    const recoveredSession = await api.getSession(sessionId);
    expect(recoveredSession.state).toBe('READY');
    expect(recoveredSession.current_responses ?? []).toEqual([]);
    expect(recoveredSession.last_response).toMatchObject({ response_id: originalResponseId, outcome: 'completed' });
    expect(sessionResponseRows(sessionId)).toEqual(originalResponses);
    expect(recipient.deliveries.map((delivery) => delivery.text))
      .toEqual([replyText(replies[0]), replyText(replies[0])]);
    expect(sessionInputRows(sessionId), 'the replayed delivery sends nothing to the Agent').toEqual(originalInputs);
    expect(await api.adminSessionTranscript(sessionId)).toBe(originalTranscript);
    const history = await api.getMessages(sessionId, 50);
    expect(historyRows(history)).toEqual(originalRows);
    await openSessionView(page, sessionId);
    await expect(page.getByTestId('user-message')).toHaveText([prompt]);
    await expect(page.getByTestId('assistant-text')).toHaveText([originalRenderedReply]);
    expect(recipient.deliveries).toHaveLength(2);
    expect(recipient.errors).toEqual([]);
    evidence.recovered = { outbox: recovered, deliveries: recipient.deliveries };

    // Only after proving replay without input, exercise transparent recovery.
    const nextPrompt = 'Explain what a pencil is in one short plain sentence. Do not use tools.';
    await sendPrompt(page, sessionId, nextPrompt);
    await expect(page.getByTestId('user-message').getByRole('paragraph')).toHaveText([prompt, nextPrompt]);
    await expect(page.getByTestId('assistant-text')).toHaveCount(2, { timeout: 60_000 });
    const nextReply = page.getByTestId('assistant-message').last();
    await expect(nextReply).not.toHaveAttribute('data-streaming', 'true', { timeout: 60_000 });
    const rebound = await api.waitForSession(sessionId, (value) => value.state === 'READY'
      && Boolean(value.last_response) && value.last_response?.response_id !== originalResponseId, 30_000);
    expect(rebound.last_response?.outcome).toBe('completed');
    expect(rebound.runtime_unavailable).toBe(false);
    expect(rebound.last_error ?? null).toBeNull();
    expect(String(rebound.sandbox_id || '')).not.toEqual('');
    expect(rebound.sandbox_id).not.toEqual(ready.sandbox_id);
    const continued = await api.getMessages(sessionId, 50);
    for (const original of originalHistory.messages) {
      expect(continued.messages.find((row) => row.id === original.id)).toEqual(original);
    }
    expect(continued.messages.filter((row) => row.role === 'user').map(messageText)).toEqual([prompt, nextPrompt]);
    const nextAnswer = continued.messages.filter((row) => row.role === 'assistant'
      && row.id === rebound.last_response?.response_id);
    expect(nextAnswer).toHaveLength(1);
    expect(messageText(nextAnswer[0]).trim()).not.toEqual('');
    await expect(page.getByTestId('assistant-text').last()).toHaveText(messageText(nextAnswer[0]));
    await expect.poll(() => {
      if (recipient.errors.length) throw new Error(recipient.errors.join('; '));
      return recipient.deliveries.map((delivery) => delivery.text);
    }, { timeout: 30_000 }).toEqual([
      replyText(replies[0]), replyText(replies[0]), replyText(nextAnswer[0]),
    ]);
    const continuedOutboxes = channelOutboxRows(sessionId);
    expect(continuedOutboxes).toHaveLength(2);
    expect(continuedOutboxes.map((row) => row.response_id).sort())
      .toEqual([originalResponseId, nextAnswer[0].id].sort());
    expect(outbox()).toEqual(recovered);
    evidence.continued = { sandboxId: rebound.sandbox_id, responseId: rebound.last_response?.response_id };
  } finally {
    try {
      if (stopped) serverCommand(server, ['start']);
    } finally {
      await recipient.close();
      await test.info().attach('channel-completed-outbox-recovery', {
        body: JSON.stringify({ sessionId, ...evidence, deliveries: recipient.deliveries, errors: recipient.errors }),
        contentType: 'application/json',
      });
    }
  }
});
