/**
 * An Assistant on a messaging channel answers only its owner, and the job it
 * schedules reports back to that chat, unless the run answers Hermes' quiet
 * marker because it has nothing to report.
 */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { channelCallback } from '../fixtures/channelCallback';
import { documentsByField, sessionEvents } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { requireSandboxHandle, sandboxExec } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

const sessions = trackSessions();
let assistantId = '';
onPassOnly(async ({ request }) => {
  // Deleting the Assistant deletes its triggers.
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

/** The text Hermes saved as the run's response, the part after its ``## Response`` heading. */
function savedResponse(output: string): string {
  const marker = '## Response';
  return (output.includes(marker) ? output.split(marker, 2)[1] : output).trim();
}

test('an Assistant job created from a chat reports its run to that chat without another message', async ({ request, page }, info) => {
  const api = new AstraApi(request);
  const recipient = await channelCallback();
  const id = randomUUID();
  const owner = `e2e:owner-${id}`;
  const strangerMarker = `STRANGER_${id}`;
  const ready = `READY_${id}`;
  const cronMarker = `CRON_${id}`;
  const jobPrompt = `Reply with exactly ${cronMarker} and nothing else.`;
  const quietPrompt = 'Reply with exactly [SILENT] and nothing else.';
  // Hermes' scheduler checks for due jobs once a minute; a one-shot due shortly
  // after the reply runs at the next check.
  const dueAt = () => new Date(Date.now() + 20_000).toISOString().replace(/\.\d{3}Z$/, 'Z');
  const prompt = (schedule: string) => [
    `Use the cronjob_manage tool exactly twice, both times with action="create" and schedule=${JSON.stringify(schedule)}:`,
    `first with this exact prompt: ${JSON.stringify(jobPrompt)}`,
    `then with this exact prompt: ${JSON.stringify(quietPrompt)}`,
    `After both succeed, reply ${ready} and finish your response.`,
    `Do not run the jobs now, do not use action="run", and never write ${cronMarker} yourself.`,
  ].join('\n');
  let sessionId = '';
  const evidence: Record<string, unknown> = {};
  try {
    const environmentName = await api.assistantEnvironmentName();
    const assistant = await api.createAssistant({
      display_name: `__e2e_assistant_channel_cron_${id}`,
      environment_name: environmentName,
      model_config_override: { model_name: await api.assistantModelName(environmentName) },
    });
    assistantId = String(assistant.assistant_id || '');
    expect(assistantId).not.toEqual('');
    const workspace = await api.waitForWorkspaceReady(assistantId, 60_000);
    const sandboxId = String(workspace.current_sandbox_id || workspace.sandbox_id || '');
    expect(sandboxId).not.toEqual('');

    const refused = await request.post(apiPath(`/assistants/${assistantId}/deployments`), {
      data: { scene: 'channel:generic_json', prompt_prefix: '' },
    });
    expect(refused.status(), 'a channel on an Assistant must name who may write to it').toBe(400);
    const deployment = await api.data<Record<string, unknown>>('POST', `/assistants/${assistantId}/deployments`, {
      scene: 'channel:generic_json', prompt_prefix: '', allowed_participants: [owner],
    });
    const deploymentId = String(deployment.deployment_id || '');
    expect(deployment).toMatchObject({ assistant_id: assistantId, allowed_participants: [owner] });
    const secret = String(deployment.secret || '');
    expect(secret).not.toEqual('');
    const send = (body: Record<string, unknown>) => request.post(apiPath(`/deployments/${deploymentId}/trigger`), {
      headers: { 'x-channel-secret': secret },
      data: { conversation_id: id, reply: { callback_url: recipient.url }, ...body },
    });

    // A stranger writing in the same chat first reaches nothing, now or later.
    const strangerMessage = await send({ text: `Reply ${strangerMarker}.`, message_id: `${id}-stranger`, sender: 'e2e:stranger' });
    expect(strangerMessage.ok(), await strangerMessage.text()).toBe(true);
    expect((await strangerMessage.json()).data).toMatchObject({ status: 'ignored' });
    expect((await strangerMessage.json()).data.session_id ?? '').toBe('');

    const ownerPrompt = prompt(dueAt());
    const ownerMessage = await send({ text: ownerPrompt, message_id: `${id}-owner`, sender: owner });
    expect(ownerMessage.ok(), await ownerMessage.text()).toBe(true);
    const receipt = (await ownerMessage.json()).data;
    expect(receipt.status).toBe('accepted');
    sessionId = String(receipt.session_id || '');
    expect(sessionId).not.toEqual('');
    sessions.push(sessionId);

    await expect.poll(() => recipient.deliveries.map((row) => row.text), { timeout: 60_000 })
      .toEqual([expect.stringContaining(ready)]);
    const readyAt = recipient.deliveries[0].receivedAt;
    const owned = () => documentsByField('hermes_cron_jobs', '$.session_id', sessionId);
    await expect.poll(() => owned().length).toBe(2);
    evidence.created = { jobs: owned(), deliveries: [...recipient.deliveries] };

    // No input follows: the run itself has to reach the chat.
    await expect.poll(() => {
      expect(recipient.errors).toEqual([]);
      return recipient.deliveries.length;
    }, { timeout: 100_000, intervals: [1_000, 2_000] }).toBe(2);
    const cronDelivery = recipient.deliveries[1];
    expect(cronDelivery.text).toContain(cronMarker);
    expect(cronDelivery.receivedAt).toBeGreaterThan(readyAt);

    // The delivered text is the run Hermes recorded in the box, not a reply the conversation wrote.
    const handle = await requireSandboxHandle(api, sandboxId);
    const savedRun = (id: string) => sandboxExec(handle, [
      `f=$(find / -path /proc -prune -o -type f -path '*/cron/output/${id}/*.md' -print 2>/dev/null | sort | tail -n 1)`,
      '[ -n "$f" ] && cat -- "$f"',
    ].join('\n'));
    const runs = Object.fromEntries(owned().map((row) => [String(row.job_id), savedRun(String(row.job_id))]));
    const jobId = Object.keys(runs).find((id) => savedResponse(runs[id]).includes(cronMarker)) || '';
    const quietJobId = Object.keys(runs).find((id) => id !== jobId) || '';
    expect(jobId, JSON.stringify(runs)).not.toEqual('');
    const saved = runs[jobId];
    expect(cronDelivery.text.trim()).toBe(savedResponse(saved));
    // The quiet job ran too, and its run was read and passed over, not lost.
    await expect.poll(() => savedResponse(savedRun(quietJobId)), { timeout: 60_000 }).toBe('[SILENT]');
    await expect.poll(() => owned().map((row) => String(row.delivered_run_at || '')).filter(Boolean).length,
      { timeout: 60_000 }).toBe(2);
    expect(recipient.deliveries, 'a quiet run reaches no one').toHaveLength(2);

    const history = await api.getMessages(sessionId, 100);
    expect(history.has_more).toBe(false);
    const replies = history.messages.filter((row) => row.role === 'assistant');
    const runReplies = replies.filter((row) => String(row.message_id).startsWith(`hermes-cron:${jobId}:`));
    expect(runReplies).toHaveLength(1);
    expect(replies.filter((row) => String(row.message_id).startsWith(`hermes-cron:${quietJobId}:`))).toEqual([]);
    expect(messageText(runReplies[0]).trim()).toBe(savedResponse(saved));
    expect(recipient.deliveries.map((row) => row.text)).toEqual(replies.map((row) => messageText(row).trim()));
    await expect.poll(() => documentsByField('channel_outbox', '$.session_id', sessionId)
      .filter((row) => row.state === 'DELIVERED').map((row) => row.response_id).sort())
      .toEqual(replies.map((row) => row.message_id).sort());

    const inbound = documentsByField('channel_inbound', '$.deployment_id', deploymentId);
    expect(inbound).toHaveLength(2);
    const ignored = inbound.filter((row) => row.state === 'IGNORED');
    expect(ignored).toHaveLength(1);
    expect(ignored[0]).toMatchObject({ context_excluded: true });
    const inputCommands = sessionEvents(sessionId).filter((event) => {
      const command = event.payload as { command_type?: string };
      return event.event_type === 'command.accepted'
        && ['StartTurn', 'SubmitInput'].includes(command.command_type || '');
    });
    expect(inputCommands).toHaveLength(1);
    expect(inputCommands[0].payload).toMatchObject({ command_type: 'StartTurn', content: ownerPrompt });
    expect(JSON.stringify(sessionEvents(sessionId))).not.toContain(strangerMarker);
    expect(JSON.stringify(history.messages)).not.toContain(strangerMarker);

    await openSessionView(page, sessionId);
    await expect(page.getByTestId('assistant-text').filter({ hasText: cronMarker })).toBeVisible();
    evidence.completed = { saved, history };
  } finally {
    await info.attach('assistant-channel-cron', {
      body: JSON.stringify({ assistantId, sessionId, ...evidence, deliveries: recipient.deliveries, errors: recipient.errors }),
      contentType: 'application/json',
    });
    await recipient.close();
  }
});
