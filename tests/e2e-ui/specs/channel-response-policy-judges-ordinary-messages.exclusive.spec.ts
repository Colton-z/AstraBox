/**
 * Under a response policy the model decides whether an ordinary group message,
 * one nobody addressed to the agent, is answered. Under `participated` only a
 * topic the agent already answered is judged; under `proactive` any topic is.
 * A declined message stays context and creates nothing; a selected one runs
 * once with that context, and a redelivery is not judged again.
 */
import { createHmac, randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { channelCallback } from '../fixtures/channelCallback';
import { sessionInputRows } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import {
  agentSessionRows, channelConversationRows, channelInboundRows, channelMessageInputs, type ChannelInboundRow,
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

test('a response policy answers only the ordinary messages its judge selects', async ({ request }, info) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const recipient = await channelCallback();
  const id = randomUUID();
  const keyword = `RESPOND_${id.slice(0, 8)}`;
  let installed: InstalledChannelBackend | undefined;
  let completed = false;
  const evidence: Record<string, unknown> = {};
  try {
    installed = new InstalledChannelBackend(`astrabox-installed-channel-${id}`, info.outputPath('installed-channel.json'));
    evidence.installedPackage = installed.evidence;
    await installed.ready();
    agentId = (await api.createColdTestAgent(`__e2e_channel_response_${id}`)).agent_id;
    const deployment = await platform.createDeployment(agentId, {
      scene: 'channel:installed_probe', prompt_prefix: '', attention_policy: 'participated',
      response_instruction: `Reply only when the message contains the word ${keyword}; otherwise stay silent.`,
    });
    deploymentId = deployment.deployment_id;
    const secret = String(deployment.secret);

    let clock = Date.now();
    async function send(room: string, eventId: string, utterance: string, mention = false) {
      const body = JSON.stringify({
        room_id: room, sender: 'participant', event_id: eventId, utterance, retain: true,
        sent_at: new Date(clock += 1000).toISOString(), callback_url: recipient.url,
        ...(mention ? { mention: true } : {}),
      });
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
    const row = (eventId: string) => {
      const rows = channelInboundRows(deploymentId).filter((item) => item.dedup_key === eventId);
      expect(rows, `one durable record for ${eventId}`).toHaveLength(1);
      return rows[0]!;
    };
    const judgment = (record: ChannelInboundRow) => ({
      respond: record.judgment_respond, reason: record.judgment_reason,
      judge: record.judgment_judge, judged_at: record.judged_at,
    });
    const replies = () => {
      expect(recipient.errors).toEqual([]);
      return recipient.deliveries.map((delivery) => delivery.text);
    };
    const contextIds = (eventId: string) => row(eventId).frozen_context;

    // participated: in a topic the agent has not answered, an ordinary message is
    // context and the judge is not asked, even when the instruction would select it.
    const room = `room-${id}`;
    const unjudgedId = `${id}-unjudged`;
    expect((await send(room, unjudgedId, `${keyword}: the review moved to Thursday.`)).status).toBe('observed');
    expect(row(unjudgedId)).toMatchObject({ state: 'OBSERVED', session_id: null });
    expect(row(unjudgedId).judged_at, 'nobody asked the judge').toBeNull();

    // A mention is answered without the judge.
    const mentionId = `${id}-mention`;
    expect((await send(room, mentionId, `Reply exactly MENTION_${id}. Do not use tools.`, true)).status)
      .toBe('accepted');
    await expect.poll(replies, { timeout: 90_000 }).toEqual([expect.stringContaining(`MENTION_${id}`)]);
    await expect.poll(() => row(mentionId).state, { timeout: 30_000 }).toBe('SETTLED');
    expect(row(mentionId).judged_at, 'a mention is answered without the judge').toBeNull();
    expect(contextIds(mentionId)).toEqual([unjudgedId]);
    const sessionId = String(row(mentionId).session_id);
    sessions.push(sessionId);
    const inputs = () => sessionInputRows(sessionId);

    // Now the topic is answered: the judge declines chatter, which stays context.
    const declinedId = `${id}-declined`;
    expect((await send(room, declinedId, `Chatter ${id}: the coffee machine is fixed.`)).status).toBe('accepted');
    await expect.poll(() => row(declinedId).state, { timeout: 60_000 }).toBe('OBSERVED');
    const declined = row(declinedId);
    expect(judgment(declined)).toMatchObject({ respond: false, judge: 'model' });
    expect(declined).toMatchObject({
      selection: 'judge', selection_reason: 'participated_topic', participated: true,
    });
    expect(channelMessageInputs(declined.item_id), 'a declined message is never sent').toEqual([]);
    expect(inputs()).toHaveLength(1);
    evidence.declined = declined;

    // ...and selects a message its instruction asks for, which runs once with that context.
    const selectedId = `${id}-selected`;
    expect((await send(room, selectedId, `${keyword}: reply exactly ANSWER_${id}. Do not use tools.`)).status)
      .toBe('accepted');
    await expect.poll(replies, { timeout: 90_000 }).toEqual([
      expect.stringContaining(`MENTION_${id}`), expect.stringContaining(`ANSWER_${id}`),
    ]);
    await expect.poll(() => row(selectedId).state, { timeout: 30_000 }).toBe('SETTLED');
    const selected = row(selectedId);
    expect(judgment(selected)).toMatchObject({ respond: true, judge: 'model' });
    expect(selected.session_id).toBe(sessionId);
    expect(contextIds(selectedId)).toEqual([declinedId]);
    expect(inputs().map((input) => input.client_message_id)).toEqual([row(mentionId).item_id, selected.item_id]);

    // A redelivered message is not judged again and runs nothing more.
    expect((await send(room, selectedId, `${keyword}: reply exactly ANSWER_${id}.`)).status).toBe('duplicate');
    expect(judgment(row(selectedId))).toEqual(judgment(selected));
    expect(inputs()).toHaveLength(2);
    evidence.selected = selected;

    // proactive: a topic the agent never answered is judged, and a declined
    // message there creates no conversation or session.
    await platform.updateDeployment(agentId, deploymentId, { attention_policy: 'proactive' });
    const freshId = `${id}-fresh`;
    expect((await send(`room-b-${id}`, freshId, `Chatter ${id}: lunch is at noon.`)).status).toBe('accepted');
    await expect.poll(() => row(freshId).state, { timeout: 60_000 }).toBe('OBSERVED');
    const fresh = row(freshId);
    expect(judgment(fresh)).toMatchObject({ respond: false, judge: 'model' });
    expect(fresh).toMatchObject({
      selection: 'judge', selection_reason: 'proactive_policy', participated: false, session_id: null,
    });
    expect(channelMessageInputs(fresh.item_id)).toEqual([]);
    expect(channelConversationRows(deploymentId)).toHaveLength(1);
    expect(agentSessionRows(agentId).map((item) => item.session_id)).toEqual([sessionId]);
    expect(replies()).toHaveLength(2);
    evidence.final = { inbound: channelInboundRows(deploymentId), replies: replies() };
    completed = true;
  } finally {
    try {
      await info.attach('channel-response-policy-evidence', {
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
