/** Replay a completed channel reply from durable history, with no old process or box. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { channelCallback } from '../fixtures/channelCallback';
import { documentsByField, replaceDocs, sessionEvents } from '../fixtures/dbOracle';
import { absoluteBaseUrl, apiPath } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { killSandbox, requireSandboxHandle, restartServerContainer, sandboxRunning } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';
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
    const outbox = () => {
      const rows = documentsByField('channel_outbox', '$.session_id', sessionId);
      expect(rows, 'one accepted channel input must own one outbox').toHaveLength(1);
      return rows[0];
    };
    await expect.poll(() => {
      if (recipient.errors.length) throw new Error(recipient.errors.join('; '));
      return recipient.deliveries.length;
    }, { timeout: 60_000, message: 'the real baseline reply must reach the HTTP recipient' }).toBe(1);
    await expect.poll(() => outbox().state).toBe('DELIVERED');
    const ready = await api.waitForSession(sessionId,
      (value) => value.state === 'READY' && !value.current_turn_id, 30_000);
    expect(ready.last_error ?? null).toBeNull();
    const originalOutbox = outbox();
    const turnId = String(originalOutbox.turn_id || '');
    expect(turnId).not.toEqual('');
    expect(originalOutbox.streaming).toBe(false);
    const originalHistory = await api.getMessages(sessionId, 50);
    expect(originalHistory.has_more).toBe(false);
    const originalRows = originalHistory.messages.map((row) => ({
      id: row.message_id, role: row.role, turn: row.turn_id, text: messageText(row),
    }));
    expect(originalRows.filter((row) => row.role === 'user').map((row) => row.text)).toEqual([prompt]);
    const replies = originalRows.filter((row) => row.role === 'assistant' && row.turn === turnId);
    expect(replies).toHaveLength(1);
    expect(replies[0].text.trim()).not.toEqual('');
    expect(recipient.deliveries[0].text).toBe(replies[0].text.trim());
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
    expect(nativeAnswers.join('')).toBe(replies[0].text);
    const originalCommands = sessionEvents(sessionId).filter((event) => event.event_type === 'command.accepted');
    expect(originalCommands.filter((event) => event.turn_id === turnId)).toHaveLength(1);
    const detail = await api.adminSessionDetail(sessionId);
    expect(String(detail.runtime_identity?.isolated_session_id || ''), 'the case owns its entire box').toEqual('');
    const oldSandbox = await requireSandboxHandle(api, String(ready.sandbox_id));
    evidence.baseline = { sessionId, originalOutbox, originalRows, originalRenderedReply,
      oldSandbox, deliveries: [...recipient.deliveries] };

    // Match the donor's incident boundary: successful baseline, then rewind
    // only this outbox's lease while the platform cannot race the mutation.
    serverCommand(server, ['stop', '--time', '10']);
    stopped = true;
    expect(serverCommand(server, ['inspect', '--format', '{{.State.Running}}'])).toBe('false');
    const deadGeneration = Number(originalOutbox.lease_generation) + 1;
    expect(Number.isSafeInteger(deadGeneration)).toBe(true);
    const expired: Record<string, unknown> = {
      ...originalOutbox, state: 'SENDING', lease_owner: `dead-${unique}`,
      lease_generation: deadGeneration, lease_expires_epoch: 1,
      next_attempt_at: '2000-01-01T00:00:00+00:00',
    };
    delete expired.delivered_at;
    expect(replaceDocs('channel_outbox', { '$._id': String(originalOutbox._id) }, expired))
      .toEqual([originalOutbox]);
    expect(outbox()).toEqual(expired);
    killSandbox(oldSandbox);
    await expect.poll(() => sandboxRunning(oldSandbox), {
      timeout: 30_000, message: 'both the old sandbox ownership root and compute must disappear',
    }).toBe(false);
    evidence.expired = expired;
    evidence.oldSandboxStopped = true;
    await restartServerContainer(absoluteBaseUrl(), 60_000, server);
    stopped = false;
    await expect.poll(() => {
      if (recipient.errors.length) throw new Error(recipient.errors.join('; '));
      return recipient.deliveries.length;
    }, { timeout: 45_000, message: 'the replacement platform must deliver the stored reply without its old box' }).toBe(2);
    await expect.poll(() => outbox().state).toBe('DELIVERED');
    const recovered = outbox();
    expect(recovered._id).toBe(originalOutbox._id);
    expect(recovered.command_id).toBe(originalOutbox.command_id);
    expect(recovered.turn_id).toBe(turnId);
    expect(recovered.lease_generation).toBe(deadGeneration + 1);
    expect(recovered.lease_owner).toBeNull();
    const recoveredSession = await api.getSession(sessionId);
    expect(recoveredSession).toMatchObject({ state: 'READY', last_turn_id: turnId, last_turn_status: 'COMPLETED' });
    expect(recoveredSession.current_turn_id ?? null).toBeNull();
    expect(recoveredSession.last_error ?? null).toBeNull();
    expect(recipient.deliveries.map((delivery) => delivery.text))
      .toEqual([replies[0].text.trim(), replies[0].text.trim()]);
    expect(sessionEvents(sessionId).filter((event) => event.event_type === 'command.accepted'))
      .toEqual(originalCommands);
    expect(await api.adminSessionTranscript(sessionId)).toBe(originalTranscript);
    const history = await api.getMessages(sessionId, 50);
    expect(history.messages.map((row) => ({
      id: row.message_id, role: row.role, turn: row.turn_id, text: messageText(row),
    }))).toEqual(originalRows);
    await openSessionView(page, sessionId);
    await expect(page.getByTestId('user-message')).toHaveText([prompt]);
    await expect(page.getByTestId('assistant-text')).toHaveText([originalRenderedReply]);
    expect(recipient.deliveries).toHaveLength(2);
    expect(recipient.errors).toEqual([]);
    evidence.recovered = { outbox: recovered, deliveries: recipient.deliveries };
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
