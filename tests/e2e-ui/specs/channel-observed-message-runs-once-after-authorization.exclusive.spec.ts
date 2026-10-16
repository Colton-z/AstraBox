/**
 * A split-feed platform reports a message on a context feed and authorizes it
 * on a separate trigger feed. Through a separately installed provider, the
 * message runs once, only after its authorization, in either order.
 */
import { createHmac, randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { channelCallback } from '../fixtures/channelCallback';
import { sessionInputRows } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import {
  agentSessionRows, channelInboundRows, channelMessageInputs, type ChannelInboundRow,
} from '../fixtures/ingressRecords';
import { InstalledChannelBackend } from '../fixtures/installedChannelBackend';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('expected an evidence object');
  return value as Record<string, unknown>;
}

test('an observed message runs once, only after the platform authorizes it, in either order', async ({ request }, info) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const recipient = await channelCallback();
  const id = randomUUID();
  const room = `room-${id}`;
  let installed: InstalledChannelBackend | undefined;
  let completed = false;
  const evidence: Record<string, unknown> = {};
  try {
    installed = new InstalledChannelBackend(`astrabox-installed-channel-${id}`, info.outputPath('installed-channel.json'));
    evidence.installedPackage = installed.evidence;
    await installed.ready();
    agentId = (await api.createColdTestAgent(`__e2e_channel_signals_${id}`)).agent_id;
    // attention_policy=all: an observation must stay context even when every
    // ordinary message would be answered.
    const deployment = await platform.createDeployment(agentId, {
      scene: 'channel:installed_probe', prompt_prefix: '', attention_policy: 'all',
    });
    deploymentId = deployment.deployment_id;
    const secret = String(deployment.secret);

    async function send(fields: Record<string, unknown>): Promise<Record<string, unknown>> {
      const body = JSON.stringify({ room_id: room, sender: 'participant', ...fields });
      const response = await request.post(apiPath(`/deployments/${deploymentId}/trigger`), {
        headers: {
          'content-type': 'application/json',
          'x-installed-signature': createHmac('sha256', secret).update(body).digest('hex'),
        },
        data: body,
      });
      expect(response.ok(), await response.text()).toBe(true);
      return object((await response.json()).data);
    }
    let clock = Date.now();
    const sentAt = () => new Date(clock += 1000).toISOString();
    const observe = (eventId: string, utterance: string) => send({
      signal: 'observation', event_id: eventId, utterance, retain: true, sent_at: sentAt(),
    });
    const authorize = (eventId: string) => send({
      signal: 'authorization', event_id: eventId, mention: true, callback_url: recipient.url,
    });
    const row = (eventId: string) => {
      const rows = channelInboundRows(deploymentId).filter((item) => item.dedup_key === eventId);
      expect(rows, `one durable record for ${eventId}`).toHaveLength(1);
      return rows[0]!;
    };
    // What a record says about its message; a redelivered signal must not change it.
    const messageFacts = (record: ChannelInboundRow) => ({
      state: record.state, content: record.content, reply_text: record.reply_text,
      participant: record.participant, conversation: record.conversation,
      frozen_content: record.frozen_content, frozen_context: record.frozen_context,
    });
    // A message that never ran became no input in any conversation.
    const neverRan = (eventId: string) => {
      expect(row(eventId).session_id).toBeNull();
      expect(channelMessageInputs(row(eventId).item_id), `${eventId} must not run`).toEqual([]);
    };
    const replies = () => {
      expect(recipient.errors).toEqual([]);
      return recipient.deliveries.map((delivery) => delivery.text);
    };
    const inputs = (sessionId: string) => sessionInputRows(sessionId);

    const contextId = `${id}-context`;
    const firstId = `${id}-first`;
    const secondId = `${id}-second`;
    const recalledId = `${id}-recalled`;
    const contextText = `Context ${id}: the review moved to Thursday.`;
    const firstText = `Reply exactly FIRST_${id}. Do not use tools.`;
    const secondText = `Reply exactly SECOND_${id}. Do not use tools.`;
    const recalledText = `Reply exactly RECALLED_${id}. Do not use tools.`;

    // An observation is context only: nothing starts for it.
    expect((await observe(contextId, contextText)).status).toBe('observed');
    expect((await observe(firstId, firstText)).status).toBe('observed');
    expect(row(firstId).state).toBe('OBSERVED');
    neverRan(firstId);
    expect(agentSessionRows(agentId)).toEqual([]);

    // Its authorization runs it once, with the earlier observation as context.
    const accepted = await authorize(firstId);
    expect(accepted.status).toBe('accepted');
    const sessionId = String(accepted.session_id || '');
    expect(sessionId).not.toBe('');
    sessions.push(sessionId);
    await expect.poll(replies, { timeout: 90_000 }).toEqual([expect.stringContaining(`FIRST_${id}`)]);
    await expect.poll(() => row(firstId).state, { timeout: 30_000 }).toBe('SETTLED');
    const first = row(firstId);
    expect(first.frozen_context).toEqual([contextId]);
    expect(String(first.frozen_content)).toContain(contextText);
    expect(String(first.frozen_content)).toContain(firstText);
    expect(inputs(sessionId).map((input) => ({ id: input.client_message_id, content: input.content })))
      .toEqual([{ id: first.item_id, content: first.frozen_content }]);
    evidence.first = first;

    // Redelivering either signal changes nothing and runs nothing.
    expect((await authorize(firstId)).status).toBe('duplicate');
    expect((await observe(firstId, `CHANGED_${id}`)).status).toBe('observed');
    expect(messageFacts(row(firstId))).toEqual(messageFacts(first));

    // An authorization that arrives first waits for its observation, then runs once.
    expect((await authorize(secondId)).status).toBe('authorized');
    expect(row(secondId).state).toBe('AUTHORIZED');
    neverRan(secondId);
    expect(inputs(sessionId)).toHaveLength(1);
    const completedSecond = await observe(secondId, secondText);
    expect(completedSecond).toMatchObject({ status: 'accepted', session_id: sessionId });
    await expect.poll(replies, { timeout: 90_000 }).toEqual([
      expect.stringContaining(`FIRST_${id}`), expect.stringContaining(`SECOND_${id}`),
    ]);
    await expect.poll(() => row(secondId).state, { timeout: 30_000 }).toBe('SETTLED');
    expect(String(row(secondId).frozen_content)).toContain(secondText);

    // A message recalled before its authorization is never answered.
    expect((await observe(recalledId, recalledText)).status).toBe('observed');
    expect((await send({
      event_id: `${recalledId}-deleted`, recall: { message_id: recalledId, timestamp: sentAt() },
    })).status).toBe('recalled');
    expect(await authorize(recalledId)).toMatchObject({ status: 'ignored', reason: 'recalled' });
    expect(row(recalledId).state).toBe('OBSERVED');
    neverRan(recalledId);

    const sent = inputs(sessionId);
    expect(sent.map((input) => input.client_message_id)).toEqual([first.item_id, row(secondId).item_id]);
    expect(sent.some((input) => input.content.includes(recalledText))).toBe(false);
    expect(replies()).toHaveLength(2);
    expect(agentSessionRows(agentId).map((item) => item.session_id)).toEqual([sessionId]);
    evidence.final = { inbound: channelInboundRows(deploymentId), inputs: sent, replies: replies() };
    completed = true;
  } finally {
    try {
      await info.attach('channel-signal-evidence', {
        body: JSON.stringify(evidence), contentType: 'application/json',
      });
      if (installed && deploymentId) {
        if (completed) {
          await platform.deleteDeployment(agentId, deploymentId);
          deploymentId = '';
        } else {
          await platform.updateDeployment(agentId, deploymentId, { enabled: false });
        }
      }
    } finally {
      try {
        if (installed) {
          await installed.restore();
          expect((await platform.listChannelProviders()).filter((row) => row.name === 'installed_probe'))
            .toEqual([]);
        }
      } finally {
        await recipient.close();
      }
    }
  }
});
