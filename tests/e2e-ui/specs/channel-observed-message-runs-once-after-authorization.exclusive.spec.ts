/**
 * A split-feed platform reports a message on a context feed and authorizes it
 * on a separate trigger feed. Through a separately installed provider, the
 * message runs once, only after its authorization, in either order.
 */
import { createHmac, randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { channelCallback } from '../fixtures/channelCallback';
import { documentsByField, sessionEvents } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
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
      const rows = documentsByField('channel_inbound', '$.deployment_id', deploymentId)
        .filter((item) => item.dedup_key === eventId);
      expect(rows, `one durable record for ${eventId}`).toHaveLength(1);
      return rows[0];
    };
    const replies = () => {
      expect(recipient.errors).toEqual([]);
      return recipient.deliveries.map((delivery) => delivery.text);
    };
    const startTurns = (sessionId: string) => sessionEvents(sessionId).filter((event) => (
      event.event_type === 'command.accepted' && object(event.payload).command_type === 'StartTurn'));

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
    expect(row(firstId)).toMatchObject({ state: 'OBSERVED', session_id: null, command_id: null });
    expect(documentsByField('sessions', '$.agent_id', agentId)).toEqual([]);

    // Its authorization runs it once, with the earlier observation as context.
    const accepted = await authorize(firstId);
    expect(accepted.status).toBe('accepted');
    const sessionId = String(accepted.session_id || '');
    expect(sessionId).not.toBe('');
    sessions.push(sessionId);
    await expect.poll(replies, { timeout: 90_000 }).toEqual([expect.stringContaining(`FIRST_${id}`)]);
    await expect.poll(() => row(firstId).state, { timeout: 30_000 }).toBe('SETTLED');
    const first = row(firstId);
    const firstInput = object(first.frozen_input);
    expect((firstInput.context as Array<Record<string, unknown>>).map((entry) => entry.message_id))
      .toEqual([contextId]);
    expect(String(firstInput.content)).toContain(contextText);
    expect(String(firstInput.content)).toContain(firstText);
    expect(startTurns(sessionId).map((event) => object(event.payload).client_message_id))
      .toEqual([`${first._id}::a0`]);
    evidence.first = first;

    // Redelivering either signal changes nothing and runs nothing.
    expect((await authorize(firstId)).status).toBe('duplicate');
    expect((await observe(firstId, `CHANGED_${id}`)).status).toBe('observed');
    expect(row(firstId).payload).toEqual(first.payload);

    // An authorization that arrives first waits for its observation, then runs once.
    expect((await authorize(secondId)).status).toBe('authorized');
    expect(row(secondId)).toMatchObject({ state: 'AUTHORIZED', session_id: null, command_id: null });
    expect(startTurns(sessionId)).toHaveLength(1);
    const completedSecond = await observe(secondId, secondText);
    expect(completedSecond).toMatchObject({ status: 'accepted', session_id: sessionId });
    await expect.poll(replies, { timeout: 90_000 }).toEqual([
      expect.stringContaining(`FIRST_${id}`), expect.stringContaining(`SECOND_${id}`),
    ]);
    await expect.poll(() => row(secondId).state, { timeout: 30_000 }).toBe('SETTLED');
    expect(String(object(row(secondId).frozen_input).content)).toContain(secondText);

    // A message recalled before its authorization is never answered.
    expect((await observe(recalledId, recalledText)).status).toBe('observed');
    expect((await send({
      event_id: `${recalledId}-deleted`, recall: { message_id: recalledId, timestamp: sentAt() },
    })).status).toBe('recalled');
    expect(await authorize(recalledId)).toMatchObject({ status: 'ignored', reason: 'recalled' });
    expect(row(recalledId)).toMatchObject({ state: 'OBSERVED', session_id: null, command_id: null });

    const turns = startTurns(sessionId);
    expect(turns).toHaveLength(2);
    expect(turns.some((event) => String(object(event.payload).content).includes(recalledText))).toBe(false);
    expect(replies()).toHaveLength(2);
    expect(documentsByField('sessions', '$.agent_id', agentId).map((item) => item.session_id))
      .toEqual([sessionId]);
    evidence.final = {
      inbound: documentsByField('channel_inbound', '$.deployment_id', deploymentId),
      turns, replies: replies(),
    };
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
