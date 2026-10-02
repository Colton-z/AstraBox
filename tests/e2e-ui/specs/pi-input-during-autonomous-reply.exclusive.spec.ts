/** A Web input must preserve the Pi reply already running for its channel. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText, visibleMessages } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { channelCallback } from '../fixtures/channelCallback';
import { documentsByField, framesForTurn, sessionEvents, snapshotDoc } from '../fixtures/dbOracle';
import { messageProseBlocks, textBlockValues } from '../fixtures/dshOutput';
import { engineProfileFor } from '../fixtures/engineProfile';
import { apiPath } from '../fixtures/env';
import { childPrompt, expectNativeMode, launchEvidence, nativeRows } from '../fixtures/nativeChildLifecycle';
import { finalReplies, nativeEntries, nativeText, object } from '../fixtures/piChildFailure';
import { piNativeOutput } from '../fixtures/piNativeFormSemantics';
import { PlatformApi } from '../fixtures/platformApi';
import { normalizeRendered } from '../fixtures/renderedTranscript';
import { requireSandboxHandle } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('Pi preserves an ordinary reply when a Web input is queued during its tool', async ({ request, page }, info) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const profile = engineProfileFor('pi');
  const recipient = await channelCallback();
  const id = randomUUID();
  let sessionId = '';
  let rootScope = '';
  const evidence: Record<string, unknown> = {};
  const native = () => nativeEntries(sessionId, rootScope);
  const deliveries = () => recipient.deliveries.map((row) => row.text);
  try {
    agentId = (await api.createAgent({ name: `__e2e_pi_queued_input_${id}`,
      environment_name: profile.environment_name, model: profile.model, prewarm_enabled: false })).agent_id;
    const deployment = await platform.createDeployment(agentId, { scene: 'channel:generic_json', prompt_prefix: '' });
    deploymentId = deployment.deployment_id;
    async function channelInput(text: string, messageId: string): Promise<string> {
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
    sessionId = await channelInput(greeting, `${id}-greeting`);
    expect(sessionId).not.toBe('');
    sessions.push(sessionId);
    await expect.poll(deliveries, { timeout: 60_000 }).toEqual([expect.stringContaining(`READY_${id}`)]);
    await api.waitForSessionReady(sessionId);
    if (profile.modes.unattended) await api.setPermissionMode(sessionId, profile.modes.unattended);
    const roots = () => [...new Set(nativeEntries(sessionId).filter(({ entry }) => object(entry.message).role === 'user'
      && nativeText(object(entry.message).content) === greeting).map((row) => row.subpath))];
    await expect.poll(roots).toHaveLength(1);
    rootScope = roots()[0]!;
    const detail = await api.adminSessionDetail(sessionId);
    const root = String(detail.runtime_identity?.workspace_dir || '').replace(/\/+$/, '');
    expect(root).toMatch(/^\//);
    const script = `${root}/.${id}.ordinary.py`;
    const started = `${root}/.${id}.started`;
    const release = `${root}/.${id}.release`;
    const prefix = `PREFIX_${id}`;
    const toolReceipt = `TOOL_${randomUUID()}`;
    const inputReceipt = `INPUT_${randomUUID()}`;
    const nextPrompt = `Reply exactly ${inputReceipt}. Do not use tools.`;
    await api.uploadFileText(sessionId, root, script.split('/').at(-1)!, [
      'from pathlib import Path', 'import time',
      `Path(${JSON.stringify(started)}).write_text(${JSON.stringify(prefix)})`,
      `release = Path(${JSON.stringify(release)})`, 'deadline = time.monotonic() + 90',
      'while not release.exists():', '    if time.monotonic() >= deadline:',
      "        raise TimeoutError('Pi ordinary input observer did not release the tool')",
      '    time.sleep(0.1)', 'print(release.read_text())',
    ].join('\n'));
    const floor = Math.max(...native().map((row) => row.seq));
    const prompt = `Write ${prefix} as visible text, then call bash once in the foreground with command="python3 ${script}". `
      + 'Wait for its result and include the actual stdout in your final reply. '
      + 'Do not create the release file, delegate, repeat the call, or use any other tools.';
    expect(await channelInput(prompt, `${id}-tool`)).toBe(sessionId);
    await expect.poll(async () => {
      const files = await platform.listFiles(sessionId, root, 10_000);
      return files.entries?.some((entry) => entry.name === started.split('/').at(-1))
        ? api.downloadFileText(sessionId, started, 10_000) : null;
    }, { timeout: 30_000 }).toBe(prefix);
    const calls = () => native().flatMap(({ entry }) => {
      const content = object(entry.message).content;
      return Array.isArray(content) ? content.map(object).filter((part) => part.type === 'toolCall'
        && part.name === 'bash' && String(object(part.arguments).command).includes(script)) : [];
    });
    await expect.poll(calls).toHaveLength(1);
    const call = calls()[0]!;
    const toolResults = () => native().map(({ entry }) => object(entry.message))
      .filter((message) => message.role === 'toolResult' && message.toolCallId === call.id);
    const partial = async () => visibleMessages(await api.getMessages(sessionId)).filter((message) =>
      message.role === 'assistant' && messageText(message).includes(prefix));
    await expect.poll(partial).toHaveLength(1);
    const before = (await partial())[0]!;
    evidence.before = before;
    expect(before.blocks).toContainEqual(expect.objectContaining({ type: 'tool_use', id: call.id, name: 'bash', input: call.arguments }));
    const anchor = String(object(snapshotDoc(sessionId)?.current_turn_engine_anchor).engine_turn_id);
    expect(anchor).toMatch(/^pi-rpc-v1\./);
    const pipe = String(object(JSON.parse(Buffer.from(anchor.slice('pi-rpc-v1.'.length), 'base64url').toString())).pty_session_id);
    const handle = await requireSandboxHandle(api, String(detail.sandbox_id));
    const wire = () => piNativeOutput(handle, pipe, object(detail.runtime_identity));
    expect(toolResults()).toEqual([]);
    expect(deliveries()).toHaveLength(1);
    expect((await api.getSession(sessionId)).current_turn_id).toBe(before.turn_id);
    await openSessionView(page, sessionId);
    await sendPrompt(page, sessionId, nextPrompt);
    await expect.poll(() => wire().filter((row) => row.type === 'queue_update'
      && Array.isArray(row.followUp) && row.followUp.includes(nextPrompt))).toHaveLength(1);
    expect(toolResults(), 'the native queue must accept the input while the actual tool is still held').toEqual([]);
    evidence.queuedWire = wire();
    await api.uploadFileText(sessionId, root, release.split('/').at(-1)!, toolReceipt);
    const echoed = () => native().filter(({ entry }) => object(entry.message).role === 'user'
      && nativeText(object(entry.message).content) === nextPrompt);
    await expect.poll(echoed, { timeout: 30_000 }).toHaveLength(1);
    await expect.poll(() => finalReplies(native(), echoed()[0]!.seq).map((reply) => reply.text), { timeout: 30_000 })
      .toEqual([inputReceipt]);
    await expect.poll(toolResults).toHaveLength(1);
    expect(toolResults()[0]!.isError).toBe(false);
    expect(nativeText(toolResults()[0]!.content)).toContain(toolReceipt);
    const oldFinal = finalReplies(native(), floor).find((reply) => reply.text.includes(toolReceipt));
    expect(oldFinal).toBeTruthy();
    expect(oldFinal!.seq).toBeLessThan(echoed()[0]!.seq);
    const expectedBlocks = native().filter((row) => row.seq > floor && row.seq <= oldFinal!.seq)
      .flatMap(({ entry }) => object(entry.message).role === 'assistant' ? textBlockValues(object(entry.message).content) : []);
    await expect.poll(async () => (await api.getMessages(sessionId)).messages
      .filter((message) => message.message_id === before.message_id).map(messageProseBlocks)).toEqual([expectedBlocks]);
    await expect.poll(deliveries, { timeout: 30_000 }).toEqual([
      expect.stringContaining(`READY_${id}`), expectedBlocks.join('\n\n').trim(), inputReceipt,
    ]);
    const final = await api.getMessages(sessionId);
    const own = final.messages.filter((message) => message.role === 'assistant' && messageText(message) === inputReceipt);
    expect(own).toHaveLength(1);
    expect(own[0]!.message_id).not.toBe(before.message_id);
    expect(own[0]!.turn_id, 'the native follow-up stays in the same active platform turn').toBe(before.turn_id);
    const completed = await api.waitForSessionReady(sessionId);
    expect(completed.last_turn_id).toBe(before.turn_id);
    expect(completed.last_turn_status).toBe('COMPLETED');
    expect(sessionEvents(sessionId).filter((event) => event.event_type === 'turn.failed')).toEqual([]);
    expect(final.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([greeting, prompt, nextPrompt]);
    expect(recipient.errors).toEqual([]);
    const outputs = framesForTurn(before.turn_id).map((row) => object(row.payload))
      .filter((frame) => frame.type === 'tool-output-available' && frame.toolCallId === call.id);
    expect(outputs).toEqual([expect.objectContaining({ output: expect.objectContaining({ isError: false,
      content: expect.objectContaining({ content: toolResults()[0]!.content }) }) })]);
    await expect(page.getByTestId('assistant-text').filter({ hasText: inputReceipt })).toBeVisible();
    const reply = page.locator(`[data-message-id="${before.message_id}"]`);
    await expect(reply.getByTestId('assistant-turn-process')).toHaveCount(1);
    await revealAssistantProcess(page, { within: reply });
    await expect.poll(async () => normalizeRendered((await reply.getByTestId('assistant-text').allInnerTexts()).join('\n')))
      .toBe(normalizeRendered(expectedBlocks.join('\n')));
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(reply.getByTestId('assistant-turn-process')).toHaveCount(1);
    await revealAssistantProcess(page, { within: reply });
    await expect.poll(async () => normalizeRendered((await reply.getByTestId('assistant-text').allInnerTexts()).join('\n')))
      .toBe(normalizeRendered(expectedBlocks.join('\n')));
    const card = reply.locator(`[data-tool-call-id="${String(call.id)}"]`);
    await expect(card).toHaveCount(1);
    const toggle = card.locator('button[aria-expanded]').first();
    if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
    await expect(card).toContainText(toolReceipt);
    await expect(page.getByTestId('assistant-text').filter({ hasText: inputReceipt })).toBeVisible();
    evidence.final = final;
    evidence.completed = completed;
    evidence.expectedBlocks = expectedBlocks;
  } finally {
    try {
      await info.attach('pi-ordinary-queued-input', { body: JSON.stringify({ ...evidence, sessionId, agentId, deploymentId,
        native: sessionId ? nativeRows(sessionId) : [], events: sessionId ? sessionEvents(sessionId) : [],
        deliveries: recipient.deliveries, errors: recipient.errors }), contentType: 'application/json' });
    } finally { await recipient.close(); }
  }
});

test('Pi preserves an autonomous reply when a Web input arrives before it settles', async ({ request, page }, info) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const profile = engineProfileFor('pi');
  const recipient = await channelCallback();
  const id = randomUUID();
  let sessionId = '';
  let rootScope = '';
  const evidence: Record<string, unknown> = {};
  const native = () => nativeEntries(sessionId, rootScope);
  function deliveries(): string[] {
    expect(recipient.errors).toEqual([]);
    return recipient.deliveries.map((row) => row.text);
  }
  try {
    agentId = (await api.createAgent({ name: `__e2e_pi_active_input_${id}`,
      environment_name: profile.environment_name, model: profile.model, prewarm_enabled: false })).agent_id;
    const deployment = await platform.createDeployment(agentId, { scene: 'channel:generic_json', prompt_prefix: '' });
    deploymentId = deployment.deployment_id;
    expect(deployment.secret).toBeTruthy();
    async function channelInput(text: string, messageId: string): Promise<string> {
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
    sessionId = await channelInput(greeting, `${id}-greeting`);
    expect(sessionId).not.toBe('');
    sessions.push(sessionId);
    await expect.poll(deliveries, { timeout: 60_000 }).toEqual([expect.stringContaining(`READY_${id}`)]);
    await api.waitForSessionReady(sessionId);
    if (profile.modes.unattended) await api.setPermissionMode(sessionId, profile.modes.unattended);
    const detail = await api.adminSessionDetail(sessionId);
    const root = String(detail.runtime_identity?.workspace_dir || '').replace(/\/+$/, '');
    expect(root).toMatch(/^\//);
    const gate = { marker: `PI_INPUT_${id}`, started: `${root}/.${id}.child-started`,
      release: `${root}/.${id}.child-release`, completed: `${root}/.${id}.child-completed` };
    const prefix = `PREFIX_${id}`;
    const script = `${root}/.${id}.parent.py`;
    const started = `${root}/.${id}.parent-started`;
    const release = `${root}/.${id}.parent-release`;
    const parentReceipt = `PARENT_${randomUUID()}`;
    const inputReceipt = `INPUT_${randomUUID()}`;
    const nextPrompt = `Reply exactly ${inputReceipt}. Do not use tools.`;
    await api.uploadFileText(sessionId, root, script.split('/').at(-1)!, [
      'from pathlib import Path', 'import time',
      `Path(${JSON.stringify(started)}).write_text(${JSON.stringify(prefix)})`,
      `release = Path(${JSON.stringify(release)})`, 'deadline = time.monotonic() + 90',
      'while not release.exists():', '    if time.monotonic() >= deadline:',
      "        raise TimeoutError('Pi input observer did not release parent')",
      '    time.sleep(0.1)', 'print(release.read_text())',
    ].join('\n'));
    async function readFile(path: string): Promise<string | null> {
      const files = await platform.listFiles(sessionId, root, 10_000);
      return files.entries?.some((entry) => entry.name === path.split('/').at(-1) && entry.kind === 'file')
        ? api.downloadFileText(sessionId, path, 10_000) : null;
    }
    const launched = `LAUNCHED_${id}`;
    const prompt = [
      childPrompt(profile, 'background', gate).replace('Do not run shell commands yourself.',
        'While the child is running, do not run shell commands yourself.'),
      `Your initial acknowledgement must contain exactly ${launched}.`,
      `When the native child settlement arrives, write ${prefix} and its actual stdout as visible text.`,
      `Then call bash once in the foreground with command="python3 ${script}".`,
      'Wait for its result, then reply with both the child receipt and this command stdout.',
      'Do not create release files, delegate this verification, or repeat it for later notifications.',
    ].join('\n');
    expect(await channelInput(prompt, `${id}-launch`)).toBe(sessionId);
    await expect.poll(deliveries, { timeout: 60_000 }).toEqual([
      expect.stringContaining(`READY_${id}`), expect.stringContaining(launched),
    ]);
    const initialDeliveries = deliveries();
    await expect.poll(() => launchEvidence(sessionId, profile, gate)).toHaveLength(1);
    const launch = launchEvidence(sessionId, profile, gate);
    expectNativeMode(launch, profile, 'background');
    rootScope = String(launch[0]!.subpath);
    await expect.poll(() => finalReplies(native()).some((reply) => reply.text.includes(launched))).toBe(true);
    await expect.poll(() => readFile(gate.started)).toBe(gate.marker);
    expect((await api.getSession(sessionId)).current_turn_id).toBeFalsy();
    const nativeFloor = Math.max(...native().map((row) => row.seq));
    const childReceipt = `CHILD_${randomUUID()}`;
    await api.uploadFileText(sessionId, root, gate.release.split('/').at(-1)!, childReceipt);
    await expect.poll(() => readFile(started), { timeout: 30_000 }).toBe(prefix);
    const parentCalls = () => native().flatMap(({ entry }) => {
      const content = object(entry.message).content;
      return Array.isArray(content) ? content.map(object).filter((part) => part.type === 'toolCall'
        && part.name === 'bash' && String(object(part.arguments).command).includes(script)) : [];
    });
    await expect.poll(parentCalls).toHaveLength(1);
    const call = parentCalls()[0]!;
    const toolResults = () => native().map(({ entry }) => object(entry.message))
      .filter((message) => message.role === 'toolResult' && message.toolCallId === call.id);
    const partial = async () => visibleMessages(await api.getMessages(sessionId)).filter((message) =>
      message.role === 'assistant' && messageText(message).includes(prefix));
    await expect.poll(partial).toHaveLength(1);
    const before = (await partial())[0]!;
    evidence.before = before;
    evidence.nativeCall = call;
    expect(before.blocks).toContainEqual(expect.objectContaining({ type: 'tool_use',
      id: call.id, name: 'bash', input: call.arguments }));
    expect(toolResults()).toEqual([]);
    expect(deliveries()).toEqual(initialDeliveries);
    await openSessionView(page, sessionId);
    await expect(page.getByTestId('assistant-text').filter({ hasText: prefix })).toBeVisible();
    await sendPrompt(page, sessionId, nextPrompt);
    expect(sessionEvents(sessionId).filter((event) => event.event_type === 'command.accepted'
      && object(event.payload).content === nextPrompt)).toHaveLength(1);
    expect(toolResults(), 'the input must arrive during the actual foreground tool').toEqual([]);
    await expect.poll(async () => (await partial()).map((message) => ({ id: message.message_id, blocks: message.blocks })))
      .toEqual([{ id: before.message_id, blocks: before.blocks }]);
    await api.uploadFileText(sessionId, root, release.split('/').at(-1)!, parentReceipt);
    const echoed = () => native().filter(({ entry }) => object(entry.message).role === 'user'
      && nativeText(object(entry.message).content) === nextPrompt);
    await expect.poll(echoed, { timeout: 30_000 }).toHaveLength(1);
    await expect.poll(toolResults).toHaveLength(1);
    expect(toolResults()[0]!.isError).toBe(false);
    expect(nativeText(toolResults()[0]!.content)).toContain(parentReceipt);
    await expect.poll(() => finalReplies(native(), nativeFloor).some((reply) => reply.text.includes(parentReceipt))).toBe(true);
    const parentFinal = finalReplies(native(), nativeFloor).find((reply) => reply.text.includes(parentReceipt))!;
    const inputSequence = echoed()[0]!.seq;
    expect(inputSequence).toBeGreaterThan(parentFinal.seq);
    await expect.poll(() => finalReplies(native(), inputSequence).map((reply) => reply.text), { timeout: 30_000 })
      .toEqual([inputReceipt]);
    const expectedBlocks = native().filter((row) => row.seq > nativeFloor && row.seq <= parentFinal.seq)
      .flatMap(({ entry }) => object(entry.message).role === 'assistant'
        ? textBlockValues(object(entry.message).content) : []);
    expect(expectedBlocks.join('\n')).toContain(prefix);
    expect(expectedBlocks.join('\n')).toContain(childReceipt);
    const restored = async () => (await api.getMessages(sessionId)).messages
      .filter((message) => message.message_id === before.message_id);
    await expect.poll(async () => (await restored()).map(messageProseBlocks)).toEqual([expectedBlocks]);
    const expectedReplies = finalReplies(native(), nativeFloor).map((reply) =>
      reply.seq === parentFinal.seq ? expectedBlocks.join('\n\n').trim() : reply.text.trim());
    await expect.poll(deliveries, { timeout: 30_000 }).toEqual([...initialDeliveries, ...expectedReplies]);
    const final = await api.getMessages(sessionId);
    const ownAnswer = final.messages.filter((message) => message.role === 'assistant' && messageText(message) === inputReceipt);
    expect(ownAnswer).toHaveLength(1);
    expect(ownAnswer[0]!.message_id).not.toBe(before.message_id);
    expect(ownAnswer[0]!.turn_id).not.toBe(before.turn_id);
    expect(final.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([greeting, prompt, nextPrompt]);
    expect(documentsByField('channel_inbound', '$.deployment_id', deploymentId)).toHaveLength(2);
    const outputs = framesForTurn(before.turn_id).map((row) => object(row.payload))
      .filter((frame) => frame.type === 'tool-output-available' && frame.toolCallId === call.id);
    expect(outputs).toEqual([expect.objectContaining({ output: expect.objectContaining({ isError: false,
      content: expect.objectContaining({ content: toolResults()[0]!.content }) }) })]);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('assistant-text').filter({ hasText: inputReceipt })).toBeVisible();
    const reply = page.locator(`[data-message-id="${before.message_id}"]`);
    await expect(reply.getByTestId('assistant-turn-process')).toHaveCount(1);
    await revealAssistantProcess(page, { within: reply });
    await expect.poll(async () => normalizeRendered((await reply.getByTestId('assistant-text').allInnerTexts()).join('\n')))
      .toBe(normalizeRendered(expectedBlocks.join('\n')));
    const card = reply.locator(`[data-tool-call-id="${String(call.id)}"]`);
    await expect(card).toHaveCount(1);
    const toggle = card.locator('button[aria-expanded]').first();
    if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
    await expect(card).toContainText(parentReceipt);
    evidence.final = final;
    evidence.expectedBlocks = expectedBlocks;
    evidence.expectedReplies = expectedReplies;
  } finally {
    try {
      await info.attach('pi-active-input-scene', { body: JSON.stringify({ ...evidence, sessionId, agentId, deploymentId,
        native: sessionId ? nativeRows(sessionId) : [], events: sessionId ? sessionEvents(sessionId) : [],
        deliveries: recipient.deliveries, recipientErrors: recipient.errors }), contentType: 'application/json' });
    } finally {
      await recipient.close();
    }
  }
});
