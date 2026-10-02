/** Codex channels carry main-agent replies, preserving native idle completion semantics. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { channelCallback } from '../fixtures/channelCallback';
import { documentsByField, sessionEvents } from '../fixtures/dbOracle';
import { engineProfileFor } from '../fixtures/engineProfile';
import { apiPath } from '../fixtures/env';
import { childPrompt, codexWaits, expectNativeMode, launchEvidence, nativeRows } from '../fixtures/nativeChildLifecycle';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

function nativeText(content: unknown): string {
  return Array.isArray(content) ? content.filter((part) => ['input_text', 'output_text'].includes(part.type))
    .map((part) => String(part.text)).join('') : '';
}

test('Codex forwards the next main-agent reply after retaining an idle child completion', async ({ request, page }, info) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const profile = engineProfileFor('codex');
  const recipient = await channelCallback();
  const id = randomUUID();
  let sessionId = '';
  let rootScope: unknown;
  const evidence: Record<string, unknown> = {};
  function native() {
    return nativeRows(sessionId).filter((row) => row.subpath === rootScope)
      .map((row) => ({ seq: Number(row.seq), entry: JSON.parse(String(row.entry_json)) }))
      .sort((a, b) => a.seq - b.seq);
  }
  function nativeReplies() {
    return native().filter(({ entry }) => entry.type === 'response_item'
      && entry.payload.type === 'message' && entry.payload.role === 'assistant')
      .map(({ entry }) => nativeText(entry.payload.content).trim()).filter(Boolean);
  }
  function deliveries() {
    expect(recipient.errors).toEqual([]);
    return recipient.deliveries.map((row) => row.text);
  }
  try {
    agentId = (await api.createAgent({
      name: `__e2e_codex_channel_completion_${id}`,
      environment_name: profile.environment_name, model: profile.model, prewarm_enabled: false,
    })).agent_id;
    const deployment = await platform.createDeployment(agentId, { scene: 'channel:generic_json', prompt_prefix: '' });
    deploymentId = deployment.deployment_id;
    expect(deployment.secret).toBeTruthy();
    async function send(text: string, messageId: string): Promise<string> {
      const response = await request.post(apiPath(`/deployments/${deploymentId}/trigger`), {
        headers: { 'x-channel-secret': String(deployment.secret) },
        data: { text, message_id: messageId, conversation_id: id, reply: { callback_url: recipient.url } },
      });
      expect(response.ok(), await response.text()).toBe(true);
      const receipt = (await response.json()).data;
      expect(receipt.status).toBe('accepted');
      return String(receipt.session_id || '');
    }
    const greeting = `Reply exactly READY_${id}. Do not use tools.`;
    sessionId = await send(greeting, `${id}-greeting`);
    expect(sessionId).not.toBe('');
    sessions.push(sessionId);
    await expect.poll(deliveries, { timeout: 60_000 }).toEqual([expect.stringContaining(`READY_${id}`)]);
    await api.waitForSessionReady(sessionId);
    if (profile.modes.unattended) await api.setPermissionMode(sessionId, profile.modes.unattended);
    const detail = await api.adminSessionDetail(sessionId);
    const root = String(detail.runtime_identity?.workspace_dir || '').replace(/\/+$/, '');
    expect(root).toMatch(/^\//);
    const launched = `LAUNCHED_${id}`;
    const gate = {
      marker: `CODEX_CHANNEL_${id}`, started: `${root}/.${id}.started`,
      release: `${root}/.${id}.release`, completed: `${root}/.${id}.completed`,
    };
    async function readGate(path: string): Promise<string | null> {
      const files = await platform.listFiles(sessionId, root, 10_000);
      return files.entries?.some((entry) => entry.name === path.split('/').at(-1) && entry.kind === 'file')
        ? api.downloadFileText(sessionId, path, 10_000) : null;
    }
    const prompt = [childPrompt(profile, 'background', gate),
      `After spawning, reply exactly ${launched}. Do not give any other commentary.`].join('\n');
    expect(await send(prompt, `${id}-launch`)).toBe(sessionId);
    await expect.poll(deliveries, { timeout: 60_000 }).toEqual([
      expect.stringContaining(`READY_${id}`), expect.stringContaining(launched),
    ]);
    await expect.poll(() => launchEvidence(sessionId, profile, gate)).toHaveLength(1);
    const launches = launchEvidence(sessionId, profile, gate);
    expectNativeMode(launches, profile, 'background');
    rootScope = launches[0]!.subpath;
    expect(codexWaits(sessionId, rootScope)).toEqual([]);
    await expect.poll(() => readGate(gate.started)).toBe(gate.marker);
    await api.waitForChildRuns(sessionId, (rows) => rows.length === 1 && rows[0].active, 15_000);
    expect((await api.getSession(sessionId)).current_turn_id).toBeFalsy();
    const initialReplies = nativeReplies();
    expect(initialReplies).toEqual(deliveries());
    const nativeFloor = Math.max(...native().map((row) => row.seq));
    const receipt = `COMPLETED_${randomUUID()}`;
    expect(JSON.stringify(native())).not.toContain(receipt);
    await api.uploadFileText(sessionId, root, gate.release.split('/').at(-1)!, receipt);
    await expect.poll(() => readGate(gate.completed), { timeout: 30_000 }).toBe(receipt);

    // Codex 0.153.4 records this v1 notification with inject_no_new_turn.
    // A child result is context for the main agent, never a channel reply itself.
    const notices = () => native().filter(({ seq, entry }) => seq > nativeFloor
      && entry.type === 'response_item' && entry.payload.type === 'message' && entry.payload.role === 'user'
      && nativeText(entry.payload.content).includes('<subagent_notification>')
      && nativeText(entry.payload.content).includes(receipt));
    await expect.poll(() => notices().length, { timeout: 30_000 }).toBe(1);
    await api.waitForChildRuns(sessionId, (rows) => rows.length === 1 && rows[0].closed, 15_000);
    expect(nativeReplies()).toEqual(initialReplies);
    expect(deliveries()).toEqual(initialReplies);
    expect((await api.getSession(sessionId)).current_turn_id).toBeFalsy();
    evidence.nativeNotices = notices();
    evidence.launches = launches;

    const followup = 'Report the actual stdout receipt from the completed child notification. Do not use any tools.';
    await openSessionView(page, sessionId);
    await sendPrompt(page, sessionId, followup);
    await expect.poll(nativeReplies, { timeout: 30_000 }).toEqual([
      ...initialReplies, expect.stringContaining(receipt),
    ]);
    const expectedReplies = nativeReplies();
    await expect.poll(deliveries, { timeout: 30_000 }).toEqual(expectedReplies);
    const final = await api.getMessages(sessionId, 50);
    expect(final.messages.filter((message) => message.role === 'assistant')
      .map((message) => messageText(message).trim())).toEqual(expectedReplies);
    expect(final.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([greeting, prompt, followup]);
    expect(documentsByField('channel_inbound', '$.deployment_id', deploymentId)).toHaveLength(2);
    await expect.poll(() => documentsByField('channel_outbox', '$.session_id', sessionId)
      .filter((row) => row.state === 'DELIVERED').length).toBe(expectedReplies.length);
    expect(codexWaits(sessionId, rootScope)).toEqual([]);
    await expect(page.getByTestId('assistant-text').filter({ hasText: receipt })).toBeVisible();
    evidence.expectedReplies = expectedReplies;
  } finally {
    try {
      await info.attach('codex-channel-completion-scene', {
        body: JSON.stringify({ ...evidence, sessionId, agentId, deploymentId, deliveries: recipient.deliveries,
          recipientErrors: recipient.errors, native: sessionId ? nativeRows(sessionId) : [],
          events: sessionId ? sessionEvents(sessionId) : [],
          outbox: sessionId ? documentsByField('channel_outbox', '$.session_id', sessionId) : [],
        }), contentType: 'application/json',
      });
    } finally {
      await recipient.close();
    }
  }
});
