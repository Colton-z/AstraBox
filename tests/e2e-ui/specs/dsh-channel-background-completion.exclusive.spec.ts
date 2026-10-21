/** A native DSH settlement produces a new reply at the originating channel. */
import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText, messageToolCalls, visibleMessages, type MessageRecord } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { channelCallback } from '../fixtures/channelCallback';
import { sessionOutputRows, sessionResponseRows } from '../fixtures/dbOracle';
import { events, textBlocks, textBlockValues, messageProseBlocks, type NativeEvent } from '../fixtures/dshOutput';
import { engineProfileFor } from '../fixtures/engineProfile';
import { absoluteBaseUrl, apiPath } from '../fixtures/env';
import {
  channelInboundRows, channelOutboxRows, channelOutputSubscriptions, rewindOutputSubscriptions,
} from '../fixtures/ingressRecords';
import { childPrompt, expectNativeMode, launchEvidence, nativeRows } from '../fixtures/nativeChildLifecycle';
import { PlatformApi } from '../fixtures/platformApi';
import { restartServerContainer } from '../fixtures/sandboxOps';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

const cases = [
  { restart: false, partial: false, title: 'DSH sends its native background completion to the channel without another input' },
  { restart: true, partial: false, title: 'DSH delivers its native background completion after a server restart without another input' },
  { restart: false, partial: true, title: 'DSH preserves partial autonomous text and its running tool across a server restart' },
];

for (const scenario of cases) test(scenario.title, async ({ request, page }, info) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const profile = engineProfileFor('deepseek_harness');
  const recipient = await channelCallback();
  const id = randomUUID();
  let sessionId = '';
  let stoppedServer = '';
  let rootScope: unknown;
  const evidence: Record<string, unknown> = {};
  try {
    agentId = (await api.createAgent({
      name: `__e2e_dsh_channel_background_${id}`,
      runtime_profile_name: profile.runtime_profile_name, model: profile.model, prepared_capacity: 0,
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
    function deliveries() {
      expect(recipient.errors).toEqual([]);
      return recipient.deliveries.map((row) => row.text);
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
      marker: `DSH_CHANNEL_${id}`, started: `${root}/.${id}.started`,
      release: `${root}/.${id}.release`, completed: `${root}/.${id}.completed`,
    };
    const parent = {
      prefix: `PARENT_PREFIX_${id}`, script: `${root}/.${id}.parent.py`,
      started: `${root}/.${id}.parent-started`, release: `${root}/.${id}.parent-release`,
    };
    const parentReceipt = `PARENT_COMPLETED_${randomUUID()}`;
    if (scenario.partial) {
      await api.uploadFileText(sessionId, root, parent.script.split('/').at(-1)!, [
        'from pathlib import Path', 'import time',
        `Path(${JSON.stringify(parent.started)}).write_text(${JSON.stringify(parent.prefix)})`,
        `release = Path(${JSON.stringify(parent.release)})`,
        'deadline = time.monotonic() + 90',
        'while not release.exists():',
        '    if time.monotonic() >= deadline:',
        "        raise TimeoutError('parent observer did not release the workload')",
        '    time.sleep(0.1)',
        'print(release.read_text())',
      ].join('\n'));
    }
    async function readGate(path: string): Promise<string | null> {
      const files = await platform.listFiles(sessionId, root, 10_000);
      return files.entries?.some((entry) => entry.name === path.split('/').at(-1) && entry.kind === 'file')
        ? api.downloadFileText(sessionId, path, 10_000) : null;
    }
    const prompt = [
      scenario.partial
        ? childPrompt(profile, 'background', gate).replace('Do not run shell commands yourself.',
          'While the child is running, do not run shell commands yourself.')
        : childPrompt(profile, 'background', gate),
      `Your initial acknowledgement must contain exactly ${launched}.`,
      'When the runtime delivers the child settlement notice, send a new reply containing its actual stdout receipt.',
      scenario.partial
        ? `After the native notification, write ${parent.prefix} and the child receipt as visible text before calling any tool. `
          + `Then call bash exactly once in the foreground with command="python3 ${parent.script}" and timeoutMs=120000. `
          + 'Wait for its actual result, then reply with both the child receipt and this command stdout. '
          + 'Do not create release files, repeat this command for later notifications, or delegate this verification.'
        : 'Do not poll or call more tools to collect it. Wait for the native notification.',
    ].join('\n');
    expect(await send(prompt, `${id}-launch`)).toBe(sessionId);
    await expect.poll(deliveries, { timeout: 60_000 }).toEqual([
      expect.stringContaining(`READY_${id}`), expect.stringContaining(launched),
    ]);
    const initialDeliveries = deliveries();
    await expect.poll(() => launchEvidence(sessionId, profile, gate)).toHaveLength(1);
    const launches = launchEvidence(sessionId, profile, gate);
    expectNativeMode(launches, profile, 'background');
    rootScope = launches[0]!.subpath;
    await api.waitForChildRuns(sessionId, (rows) => rows.length === 1 && rows[0].active, 15_000);
    await expect.poll(() => readGate(gate.started)).toBe(gate.marker);
    expect((await api.getSession(sessionId)).current_responses ?? [], 'the parent replied and is not working')
      .toEqual([]);
    const initialHistory = await api.getMessages(sessionId, 50);
    evidence.initialHistory = initialHistory;
    evidence.launches = launches;
    const receipt = `COMPLETED_${randomUUID()}`;
    expect(JSON.stringify(initialHistory)).not.toContain(receipt);
    if (scenario.restart) {
      await restartServerContainer(absoluteBaseUrl(), 60_000);
      expect(deliveries()).toEqual(initialDeliveries);
    }
    await api.uploadFileText(sessionId, root, gate.release.split('/').at(-1)!, receipt);
    await expect.poll(() => readGate(gate.completed), { timeout: 30_000 }).toBe(receipt);

    const native = () => nativeRows(sessionId).filter((row) => row.subpath === rootScope)
      .flatMap((row) => events(JSON.parse(String(row.entry_json)))).sort((a, b) => a.seq - b.seq);
    const notices = () => native().filter((event) => event.type === 'user/message'
      && (event.data.source as Record<string, unknown> | undefined)?.kind === 'subagent-settled'
      && textBlocks(event.data.content).includes(receipt));
    let partialReply: MessageRecord | undefined;
    let parentCall: NativeEvent | undefined;
    if (scenario.partial) {
      await expect.poll(() => readGate(parent.started), { timeout: 30_000 }).toBe(parent.prefix);
      const calls = () => native().filter((event) => event.type === 'tool/call'
        && event.data.name === 'bash' && String(event.data.arguments).includes(parent.script));
      await expect.poll(calls).toHaveLength(1);
      parentCall = calls()[0]!;
      expect(native().some((event) => event.type === 'turn/end' && event.data.turn === parentCall!.data.turn)).toBe(false);
      const partial = async () => visibleMessages(await api.getMessages(sessionId, 50))
        .filter((message) => message.role === 'assistant' && messageText(message).includes(parent.prefix));
      await expect.poll(partial).toHaveLength(1);
      partialReply = (await partial())[0]!;
      // The reply shows the running native call as its tool part.
      expect(messageToolCalls(partialReply)).toContainEqual(expect.objectContaining({
        toolCallId: parentCall.data.callId, toolName: 'bash', input: JSON.parse(String(parentCall.data.arguments)),
      }));
      expect(partialReply.metadata?.status, 'the reply is still running its tool').not.toBe('ended');
      expect(deliveries()).toEqual(initialDeliveries);
      evidence.partialReply = partialReply;
      evidence.parentCall = parentCall;
      await openSessionView(page, sessionId);
      await expect(page.getByTestId('assistant-text').filter({ hasText: parent.prefix })).toBeVisible();
      await page.goto('about:blank');
      await restartServerContainer(absoluteBaseUrl(), 60_000);
      await expect.poll(async () => (await partial()).map((message) => ({
        id: message.id, parts: message.parts,
      }))).toEqual([{ id: partialReply.id, parts: partialReply.parts }]);
      expect(native().some((event) => event.type === 'turn/end' && event.data.turn === parentCall!.data.turn)).toBe(false);
      await api.uploadFileText(sessionId, root, parent.release.split('/').at(-1)!, parentReceipt);
    }
    await expect.poll(notices, { timeout: 30_000 }).toHaveLength(1);
    const notice = notices()[0]!;
    expect((notice.data.source as Record<string, unknown>).senderSessionId).toBeTruthy();
    const nativeReplies = () => native().filter((event) => event.type === 'assistant/message'
      && textBlocks((event.data.message as Record<string, unknown>).content).includes(receipt));
    await expect.poll(() => nativeReplies().filter((event) => event.seq > notice.seq).length, {
      timeout: 30_000,
    }).toBeGreaterThan(0);
    const settlementReply = nativeReplies().filter((event) => event.seq > notice.seq).at(-1)!;
    await expect.poll(() => native().find((event) => event.type === 'turn/end'
      && event.data.turn === settlementReply.data.turn)?.data.reason).toEqual({ kind: 'completed' });
    const completionTurns = new Set(nativeReplies().map((event) => event.data.turn));
    const repliesByTurn = new Map<number, string[]>();
    for (const reply of native().filter((event) => event.type === 'assistant/message'
      && completionTurns.has(event.data.turn))) {
      const turn = Number(reply.data.turn);
      const parts = repliesByTurn.get(turn) ?? [];
      parts.push(...textBlockValues((reply.data.message as Record<string, unknown>).content));
      repliesByTurn.set(turn, parts);
    }
    const expectedBlocks = [...repliesByTurn.values()];
    const expectedReplies = expectedBlocks.map((parts) => parts.filter(Boolean).join('\n\n').trim());
    evidence.expectedBlocks = expectedBlocks;
    evidence.expectedReplies = expectedReplies;
    const completionReplies = async () => (await api.getMessages(sessionId, 50)).messages
      .filter((message) => message.role === 'assistant' && messageText(message).includes(receipt));
    await expect.poll(async () => (await completionReplies()).map(messageProseBlocks), {
      timeout: 30_000,
    }).toEqual(expectedBlocks);
    await expect.poll(deliveries, { timeout: 30_000 }).toEqual([...initialDeliveries, ...expectedReplies]);
    // Each native turn the settlement started is a response no input asked for.
    const responses = sessionResponseRows(sessionId);
    for (const completion of await completionReplies()) {
      expect(responses.find((row) => row.response_id === completion.id)).toMatchObject({
        origin: 'engine', input_ids: [], status: 'ended', outcome: 'completed',
      });
    }
    const final = await api.getMessages(sessionId, 50);
    if (partialReply && parentCall) {
      const restored = final.messages.find((message) => message.id === partialReply!.id)!;
      expect(restored.metadata?.status).toBe('ended');
      expect(messageText(restored)).toContain(parentReceipt);
      const nativeResults = native().filter((event) => event.type === 'tool/result'
        && event.data.turn === parentCall!.data.turn)
        .map((event) => event.data.message as Record<string, unknown>)
        .filter((message) => message.role === 'tool' && message.toolCallId === parentCall!.data.callId);
      expect(nativeResults).toHaveLength(1);
      expect(nativeResults[0]!.isError ?? false).toBe(false);
      expect(textBlocks(nativeResults[0]!.content)).toContain(parentReceipt);
      // The settled tool part carries the native call and its native result.
      expect(messageToolCalls(restored)).toEqual([expect.objectContaining({
        toolCallId: parentCall.data.callId, toolName: 'bash', state: 'output-available',
        input: JSON.parse(String(parentCall.data.arguments)),
        output: { content: nativeResults[0]!.content, isError: false },
      })]);
      const outputs = sessionOutputRows(sessionId, restored.id).map((row) => row.part)
        .filter((part) => part.type === 'tool-output-available' && part.toolCallId === parentCall!.data.callId);
      expect(outputs).toEqual([expect.objectContaining({
        output: { content: nativeResults[0]!.content, isError: false },
      })]);
      evidence.parentResult = nativeResults[0];
    }
    expect(final.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([greeting, prompt]);
    expect(channelInboundRows(deploymentId)).toHaveLength(2);
    await expect.poll(() => channelOutboxRows(sessionId)
      .filter((row) => row.state === 'DELIVERED').length).toBe(initialDeliveries.length + expectedReplies.length);
    await openSessionView(page, sessionId);
    // A settled reply folds its pre-tool text into the process alongside the
    // tool. Open that content before comparing the complete recovered reply.
    if (parentCall) {
      await expect(page.getByTestId('assistant-text').filter({ hasText: parentReceipt }).last()).toBeVisible();
      await revealAssistantProcess(page, {
        within: page.locator(`[data-message-id="${partialReply!.id}"]`),
      });
    }
    await expect(page.getByTestId('assistant-text').filter({ hasText: receipt }).first()).toBeVisible();
    if (parentCall) {
      await expect(page.getByTestId('assistant-turn-process')
        .getByTestId('assistant-text').filter({ hasText: parent.prefix })).toBeVisible();
      const card = page.locator(`[data-tool-call-id="${String(parentCall.data.callId)}"]`);
      await expect(card).toHaveCount(1);
      const toggle = card.locator('button[aria-expanded]').first();
      if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
      await expect(card).toContainText(parentReceipt);
    }
    if (scenario.restart) {
      const subscription = () => channelOutputSubscriptions(sessionId)[0];
      const completionCursor = Math.max(...(await completionReplies())
        .flatMap((message) => sessionOutputRows(sessionId, message.id))
        .map((row) => row.seq));
      expect(Number.isFinite(completionCursor)).toBe(true);
      await expect.poll(() => Number(subscription()?.after_seq), { timeout: 30_000 })
        .toBeGreaterThanOrEqual(completionCursor);
      const deliveredRows = channelOutboxRows(sessionId);
      const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
      await page.goto('about:blank');
      stoppedServer = server;
      execFileSync('docker', ['stop', '--time', '10', server], { timeout: 30_000 });
      const saved = subscription();
      // Rewind the stopped platform's projection so the next one must revisit
      // the delivered replies without creating another history or outbox entry.
      expect(rewindOutputSubscriptions(sessionId, 0)).toEqual([{ ...saved, after_seq: 0 }]);
      await restartServerContainer(absoluteBaseUrl(), 60_000, server);
      stoppedServer = '';
      await expect.poll(() => Number(subscription()?.after_seq), { timeout: 30_000 })
        .toBeGreaterThanOrEqual(Number(saved.after_seq));
      expect(deliveries()).toEqual([...initialDeliveries, ...expectedReplies]);
      expect(channelOutboxRows(sessionId)).toEqual(deliveredRows);
      const reloaded = await api.getMessages(sessionId, 50);
      const content = (message: MessageRecord) => ({ id: message.id, role: message.role, parts: message.parts });
      expect(reloaded.messages.map(content)).toEqual(final.messages.map(content));
      await openSessionView(page, sessionId);
      await expect(page.getByTestId('assistant-text').filter({ hasText: receipt }).first()).toBeVisible();
      evidence.completedReplyRestart = { completionCursor, beforeSeq: saved.after_seq, afterSeq: subscription()?.after_seq };
    }
    evidence.native = native();
  } finally {
    try {
      if (stoppedServer) await restartServerContainer(absoluteBaseUrl(), 60_000, stoppedServer);
      await info.attach('dsh-channel-background-scene', {
        body: JSON.stringify({ ...evidence, sessionId, agentId, deploymentId, deliveries: recipient.deliveries,
          recipientErrors: recipient.errors,
          native: sessionId ? nativeRows(sessionId) : [],
          responses: sessionId ? sessionResponseRows(sessionId) : [],
          outbox: sessionId ? channelOutboxRows(sessionId) : [],
        }), contentType: 'application/json',
      });
    } finally {
      await recipient.close();
    }
  }
});
