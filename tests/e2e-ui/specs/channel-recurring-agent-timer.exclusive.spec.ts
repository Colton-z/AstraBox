/** Native recurring prompts reach the channel without another inbound message. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { channelCallback } from '../fixtures/channelCallback';
import { documentsByField, sessionEvents } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

interface NativeBlock {
  type: string;
  id?: string;
  name?: string;
  input?: Record<string, unknown>;
  tool_use_id?: string;
  is_error?: boolean;
  text?: string;
  content?: string | NativeBlock[];
}

function blockText(content: string | NativeBlock[] | undefined): string {
  return typeof content === 'string' ? content : (content || []).map((block) => block.text || '').join('');
}

function nativeBlocks(sessionId: string): NativeBlock[] {
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .filter((row) => row.subpath == null)
    .sort((a, b) => Number(a.seq) - Number(b.seq))
    .flatMap((row) => {
      const entry = JSON.parse(String(row.entry_json)) as { message?: { content?: unknown } };
      return Array.isArray(entry.message?.content) ? entry.message.content as NativeBlock[] : [];
    });
}

test('a recurring native agent timer sends two channel replies without another input', async ({ request, page }, info) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const recipient = await channelCallback();
  const id = randomUUID();
  const filename = `timer-${id}.json`;
  const ready = `SCHEDULED_${id}`;
  const script = [
    'import json,time,uuid',
    'from pathlib import Path',
    `p=Path(${JSON.stringify(filename)})`,
    'rows=json.loads(p.read_text()) if p.exists() else []',
    'row={"count":len(rows)+1,"time_ms":time.time_ns()//1000000,"receipt":str(uuid.uuid4())}',
    'rows.append(row)',
    'p.write_text(json.dumps(rows))',
    'print("ASTRABOX_TIMER_RECEIPT",row["count"],row["time_ms"],row["receipt"])',
  ].join('\n');
  const command = `python3 - <<'PY'\n${script}\nPY`;
  const scheduledPrompt = [
    'This is a scheduled tick. Use Bash once to run this exact command:', command,
    'If its count is 2, use CronDelete to cancel this recurring job before replying.',
    'Reply with the exact stdout receipt. Do not fabricate a receipt or use other tools.',
  ].join('\n');
  const prompt = [
    'Create exactly one native CronCreate job with cron="* * * * *" and recurring=true.',
    `Use this exact scheduled prompt: ${JSON.stringify(scheduledPrompt)}`,
    `After CronCreate succeeds, reply ${ready} and finish your response.`,
    'Do not run Bash now. Do not sleep, poll, delegate, or simulate ticks.',
    'Let the native scheduler run the task on each minute until the second tick cancels it.',
  ].join('\n');
  let sessionId = '';
  const evidence: Record<string, unknown> = {};
  try {
    agentId = (await api.createColdTestAgent(`__e2e_channel_timer_${id}`)).agent_id;
    const deployment = await platform.createDeployment(agentId, { scene: 'channel:generic_json', prompt_prefix: '' });
    deploymentId = deployment.deployment_id;
    expect(deployment.secret).toBeTruthy();
    const triggered = await request.post(apiPath(`/deployments/${deploymentId}/trigger`), {
      headers: { 'x-channel-secret': String(deployment.secret) },
      data: { text: prompt, message_id: id, conversation_id: id, reply: { callback_url: recipient.url } },
    });
    expect(triggered.ok(), await triggered.text()).toBe(true);
    const receipt = (await triggered.json()).data;
    expect(receipt.status).toBe('accepted');
    sessionId = String(receipt.session_id || '');
    expect(sessionId).not.toBe('');
    sessions.push(sessionId);
    const calls = (name: string) => nativeBlocks(sessionId)
      .filter((block) => block.type === 'tool_use' && block.name === name);
    const result = (call: NativeBlock) => nativeBlocks(sessionId)
      .find((block) => block.type === 'tool_result' && block.tool_use_id === call.id);
    await expect.poll(() => calls('CronCreate'), { timeout: 45_000 }).toHaveLength(1);
    const creation = calls('CronCreate')[0];
    expect(creation.input).toMatchObject({ cron: '* * * * *', recurring: true, prompt: scheduledPrompt });
    await expect.poll(() => result(creation)).toBeTruthy();
    expect(result(creation)!.is_error).not.toBe(true);
    await expect.poll(() => recipient.deliveries.map((row) => row.text), { timeout: 15_000 })
      .toEqual([expect.stringContaining(ready)]);
    evidence.creation = { call: creation, result: result(creation), deliveries: [...recipient.deliveries] };

    // No input, upload, control operation or test-generated wake follows setup.
    await expect.poll(() => {
      expect(recipient.errors).toEqual([]);
      return recipient.deliveries.filter((row) => /ASTRABOX_TIMER_RECEIPT [12] \d+ [0-9a-f-]{36}/.test(row.text)).length;
    }, { timeout: 150_000, intervals: [500, 1000] }).toBe(2);
    const ticks = calls('Bash');
    expect(ticks).toHaveLength(2);
    expect(ticks.map((call) => call.input?.command)).toEqual([command, command]);
    const outputs = ticks.map((call) => {
      const output = result(call);
      expect(output).toBeTruthy();
      expect(output!.is_error).not.toBe(true);
      const match = blockText(output!.content).match(/ASTRABOX_TIMER_RECEIPT ([12]) (\d+) ([0-9a-f-]{36})/);
      expect(match).not.toBeNull();
      return { text: match![0], count: Number(match![1]), time: Number(match![2]), receipt: match![3] };
    });
    expect(outputs.map((row) => row.count)).toEqual([1, 2]);
    expect(outputs[1].receipt).not.toBe(outputs[0].receipt);
    expect(outputs[1].time).toBeGreaterThan(outputs[0].time);
    expect(calls('CronDelete')).toHaveLength(1);
    await expect.poll(() => result(calls('CronDelete')[0])).toBeTruthy();
    expect(result(calls('CronDelete')[0])!.is_error).not.toBe(true);
    const history = await api.getMessages(sessionId, 100);
    expect(history.has_more).toBe(false);
    const replies = history.messages.filter((row) => row.role === 'assistant');
    for (const output of outputs) {
      expect(replies.filter((row) => messageText(row).includes(output.text))).toHaveLength(1);
    }
    await expect.poll(() => recipient.deliveries.map((row) => row.text))
      .toEqual(replies.map((row) => messageText(row).trim()));
    expect(documentsByField('channel_inbound', '$.deployment_id', deploymentId)).toHaveLength(1);
    const inputCommands = sessionEvents(sessionId).filter((event) => {
      const command = event.payload as { command_type?: string };
      return event.event_type === 'command.accepted'
        && ['StartTurn', 'SubmitInput'].includes(command.command_type || '');
    });
    expect(inputCommands).toHaveLength(1);
    expect(inputCommands[0].payload).toMatchObject({ command_type: 'StartTurn', content: prompt });
    await expect.poll(() => documentsByField('channel_outbox', '$.session_id', sessionId)
      .filter((row) => row.state === 'DELIVERED').map((row) => row.response_id).sort())
      .toEqual(replies.map((row) => row.message_id).sort());
    await openSessionView(page, sessionId);
    for (const output of outputs) {
      await expect(page.getByTestId('assistant-text').filter({ hasText: output.text })).toBeVisible();
    }
    evidence.completed = { outputs, history, native: nativeBlocks(sessionId) };
  } finally {
    await info.attach('channel-native-timer', {
      body: JSON.stringify({ sessionId, ...evidence, deliveries: recipient.deliveries, errors: recipient.errors }),
      contentType: 'application/json',
    });
    await recipient.close();
  }
});
