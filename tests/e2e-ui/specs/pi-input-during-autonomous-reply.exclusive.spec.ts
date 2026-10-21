/**
 * A Web input must preserve the Pi reply already running for its channel.
 *
 * Pi takes every input as a follow-up and drains them first in, first out
 * (`engine/pi_runtime_client.py`), so a message sent while a reply runs waits
 * in Pi's own queue: the session records it as delivered, not yet taken. The
 * reply that was running ends where Pi drains the follow-up, and the
 * follow-up is answered by a response of its own.
 */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import {
  AstraApi,
  answersInput,
  messageText,
  messageToolCalls,
  visibleMessages,
  type MessageRecord,
} from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { channelCallback } from '../fixtures/channelCallback';
import {
  postgresRows,
  sessionInputRows,
  sessionOutputRows,
  sessionResponseRows,
  sqlLiteral,
} from '../fixtures/dbOracle';
import { textBlockValues } from '../fixtures/dshOutput';
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

/** A message's prose: its text parts, each one separately, in order. */
function proseParts(message: MessageRecord): string[] {
  return textBlockValues(message.parts);
}

/**
 * The execd pipe Pi runs on for a session. The session's reader stores the
 * runtime cursor it has read up to, and a Pi cursor's generation is that pipe
 * (`PiRuntimeClient`, `engine/pi_runtime_client.py`).
 */
function piPipeId(sessionId: string): string {
  const [row] = postgresRows<{ reader_cursor: Record<string, unknown> | null }>(
    `SELECT reader_cursor FROM sessions WHERE session_id = ${sqlLiteral(sessionId)}`,
  );
  return String(row?.reader_cursor?.generation ?? '');
}

/** The tool output parts a response stored for one call. */
function toolOutputs(sessionId: string, responseId: string, toolCallId: unknown): Record<string, unknown>[] {
  return sessionOutputRows(sessionId, responseId).map((row) => row.part)
    .filter((part) => part.type === 'tool-output-available' && part.toolCallId === toolCallId);
}

/** Read evidence for an attachment without letting a broken read replace the test's own failure. */
function observe(read: () => unknown): unknown {
  try { return read(); } catch (error) { return { unavailable: String(error) }; }
}
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
      runtime_profile_name: profile.runtime_profile_name, model: profile.model, prepared_capacity: 0 })).agent_id;
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
    expect(messageToolCalls(before)).toContainEqual(expect.objectContaining({
      toolCallId: String(call.id), toolName: 'bash', input: call.arguments,
    }));
    const promptInput = sessionInputRows(sessionId).find((row) => row.content === prompt);
    expect(promptInput, 'the channel prompt is one recorded input').toBeTruthy();
    expect(answersInput(before, promptInput!.input_id), 'the running reply answers the channel prompt').toBe(true);
    const pipe = piPipeId(sessionId);
    expect(pipe, 'the session reader must be reading a Pi pipe').not.toBe('');
    const handle = await requireSandboxHandle(api, String(detail.sandbox_id));
    const wire = () => piNativeOutput(handle, pipe, object(detail.runtime_identity));
    expect(toolResults()).toEqual([]);
    expect(deliveries()).toHaveLength(1);
    expect(((await api.getSession(sessionId)).current_responses ?? []).map((response) => response.response_id))
      .toEqual([before.id]);
    await openSessionView(page, sessionId);
    await sendPrompt(page, sessionId, nextPrompt);
    await expect.poll(() => wire().filter((row) => row.type === 'queue_update'
      && Array.isArray(row.followUp) && row.followUp.includes(nextPrompt))).toHaveLength(1);
    const webInput = () => sessionInputRows(sessionId).filter((row) => row.content === nextPrompt);
    await expect.poll(() => webInput().map((row) => row.state),
      { message: 'Pi holds the Web input in its queue: delivered, not yet taken' }).toEqual(['delivered']);
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
      .filter((message) => message.id === before.id).map(proseParts)).toEqual([expectedBlocks]);
    await expect.poll(deliveries, { timeout: 30_000 }).toEqual([
      expect.stringContaining(`READY_${id}`), expectedBlocks.join('\n\n').trim(), inputReceipt,
    ]);
    const final = await api.getMessages(sessionId);
    const own = final.messages.filter((message) => message.role === 'assistant' && messageText(message) === inputReceipt);
    expect(own).toHaveLength(1);
    expect(own[0]!.id).not.toBe(before.id);
    expect(webInput(), 'Pi took the Web input once it drained its queue').toEqual([
      expect.objectContaining({ state: 'admitted', response_id: own[0]!.id }),
    ]);
    expect(answersInput(own[0]!, webInput()[0]!.input_id), 'the follow-up is answered by a response of its own')
      .toBe(true);
    const completed = await api.waitForSessionReady(sessionId);
    expect(completed.last_response?.response_id).toBe(own[0]!.id);
    expect(completed.last_response?.outcome).toBe('completed');
    const responses = sessionResponseRows(sessionId);
    expect(responses.find((row) => row.response_id === before.id), 'the running reply completes')
      .toMatchObject({ status: 'ended', outcome: 'completed' });
    expect(responses.filter((row) => row.outcome !== 'completed'), 'no response fails or is cancelled').toEqual([]);
    expect(final.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([greeting, prompt, nextPrompt]);
    expect(recipient.errors).toEqual([]);
    expect(toolOutputs(sessionId, before.id, call.id)).toEqual([expect.objectContaining({ output: expect.objectContaining({ isError: false,
      content: expect.objectContaining({ content: toolResults()[0]!.content }) }) })]);
    await expect(page.getByTestId('assistant-text').filter({ hasText: inputReceipt })).toBeVisible();
    const reply = page.locator(`[data-message-id="${before.id}"]`);
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
        native: sessionId ? observe(() => nativeRows(sessionId)) : [],
        inputs: sessionId ? observe(() => sessionInputRows(sessionId)) : [],
        responses: sessionId ? observe(() => sessionResponseRows(sessionId)) : [],
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
      runtime_profile_name: profile.runtime_profile_name, model: profile.model, prepared_capacity: 0 })).agent_id;
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
    expect(((await api.getSession(sessionId)).current_responses ?? []).length,
      'the launch reply has ended while the background child runs').toBe(0);
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
    expect(messageToolCalls(before)).toContainEqual(expect.objectContaining({
      toolCallId: String(call.id), toolName: 'bash', input: call.arguments,
    }));
    expect(before.metadata?.origin, 'Pi started the reply to the child settlement on its own').toBe('engine');
    expect(before.metadata?.input_ids ?? [], 'a reply Pi started on its own answers no input').toEqual([]);
    expect(toolResults()).toEqual([]);
    expect(deliveries()).toEqual(initialDeliveries);
    await openSessionView(page, sessionId);
    await expect(page.getByTestId('assistant-text').filter({ hasText: prefix })).toBeVisible();
    await sendPrompt(page, sessionId, nextPrompt);
    const webInput = () => sessionInputRows(sessionId).filter((row) => row.content === nextPrompt);
    expect(webInput(), 'the Web input is accepted once').toHaveLength(1);
    expect(toolResults(), 'the input must arrive during the actual foreground tool').toEqual([]);
    await expect.poll(async () => (await partial()).map((message) => ({ id: message.id, parts: message.parts })))
      .toEqual([{ id: before.id, parts: before.parts }]);
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
      .filter((message) => message.id === before.id);
    await expect.poll(async () => (await restored()).map(proseParts)).toEqual([expectedBlocks]);
    const expectedReplies = finalReplies(native(), nativeFloor).map((reply) =>
      reply.seq === parentFinal.seq ? expectedBlocks.join('\n\n').trim() : reply.text.trim());
    await expect.poll(deliveries, { timeout: 30_000 }).toEqual([...initialDeliveries, ...expectedReplies]);
    const final = await api.getMessages(sessionId);
    const ownAnswer = final.messages.filter((message) => message.role === 'assistant' && messageText(message) === inputReceipt);
    expect(ownAnswer).toHaveLength(1);
    expect(ownAnswer[0]!.id).not.toBe(before.id);
    expect(answersInput(ownAnswer[0]!, webInput()[0]!.input_id), 'the Web input is answered by a response of its own')
      .toBe(true);
    expect(sessionResponseRows(sessionId).find((row) => row.response_id === before.id),
      'the reply Pi started on its own completes').toMatchObject({ origin: 'engine', status: 'ended', outcome: 'completed' });
    expect(final.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([greeting, prompt, nextPrompt]);
    expect(postgresRows(`SELECT item_id FROM channel_inbound WHERE deployment_id = ${sqlLiteral(deploymentId)}`),
      'the channel admitted its two messages, and the Web input is not one of them').toHaveLength(2);
    expect(toolOutputs(sessionId, before.id, call.id)).toEqual([expect.objectContaining({ output: expect.objectContaining({ isError: false,
      content: expect.objectContaining({ content: toolResults()[0]!.content }) }) })]);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('assistant-text').filter({ hasText: inputReceipt })).toBeVisible();
    const reply = page.locator(`[data-message-id="${before.id}"]`);
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
        native: sessionId ? observe(() => nativeRows(sessionId)) : [],
        inputs: sessionId ? observe(() => sessionInputRows(sessionId)) : [],
        responses: sessionId ? observe(() => sessionResponseRows(sessionId)) : [],
        deliveries: recipient.deliveries, recipientErrors: recipient.errors }), contentType: 'application/json' });
    } finally {
      await recipient.close();
    }
  }
});
