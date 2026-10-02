/** A normal control attachment retains an already-started native reply. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText, visibleMessages } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { documentsByField, framesForTurn, sessionEvents } from '../fixtures/dbOracle';
import { events, messageProseBlocks, textBlockValues, textBlocks } from '../fixtures/dshOutput';
import { engineProfileFor } from '../fixtures/engineProfile';
import { absoluteBaseUrl } from '../fixtures/env';
import { childPrompt, expectNativeMode, launchEvidence, nativeRows } from '../fixtures/nativeChildLifecycle';
import { PlatformApi } from '../fixtures/platformApi';
import { normalizeRendered } from '../fixtures/renderedTranscript';
import { restartServerContainer } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

const cases = [
  { input: false, unobserved: false, title: 'a DSH control operation restores partial autonomous output before a Web subscription' },
  { input: true, unobserved: false, title: 'a DSH input after restart preserves the unfinished autonomous reply and its own answer' },
  { input: true, unobserved: true, title: 'a DSH input after restart preserves autonomous output not yet observed by the platform' },
] as const;

for (const scenario of cases) test(scenario.title, async ({ request, page }, info) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const profile = engineProfileFor('deepseek_harness');
  const id = randomUUID();
  let sessionId = '';
  const evidence: Record<string, unknown> = {};
  try {
    agentId = (await api.createAgent({ name: `__e2e_dsh_control_${id}`,
      environment_name: profile.environment_name, model: profile.model, prewarm_enabled: false })).agent_id;
    sessionId = (await api.startConversation(agentId)).session_id;
    sessions.push(sessionId);
    await api.waitForSessionReady(sessionId);
    const greeting = `Reply exactly READY_${id}. Do not use tools.`;
    await api.streamPrompt(sessionId, greeting, profile.modes.unattended, 60_000);
    await expect.poll(async () => (await api.getMessages(sessionId)).messages
      .filter((message) => message.role === 'assistant').map(messageText)).toEqual([`READY_${id}`]);
    const detail = await api.adminSessionDetail(sessionId);
    const root = String(detail.runtime_identity?.workspace_dir || '').replace(/\/+$/, '');
    expect(root).toMatch(/^\//);
    const gate = { marker: `DSH_CONTROL_${id}`, started: `${root}/.${id}.child-started`,
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
      "        raise TimeoutError('control recovery observer did not release parent')",
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
      `Then call bash once in the foreground with command="python3 ${script}" and timeoutMs=120000.`,
      'Wait for its result, then reply with both the child receipt and this command stdout.',
      'Do not create release files, delegate this verification, or repeat it for later notifications.',
    ].join('\n');
    await api.streamPrompt(sessionId, prompt, profile.modes.unattended, 60_000);
    await expect.poll(() => launchEvidence(sessionId, profile, gate)).toHaveLength(1);
    const launch = launchEvidence(sessionId, profile, gate);
    expectNativeMode(launch, profile, 'background');
    await expect.poll(() => readFile(gate.started)).toBe(gate.marker);
    const childrenBefore = (await api.listChildRuns(sessionId)).child_runs;
    expect(childrenBefore).toHaveLength(1);
    const native = () => nativeRows(sessionId).filter((row) => row.subpath === launch[0]!.subpath)
      .flatMap((row) => events(JSON.parse(String(row.entry_json)))).sort((a, b) => a.seq - b.seq);
    let nextInput: Promise<{ raw: string } | { error: unknown }> | undefined;
    async function submitNextInput(): Promise<void> {
      nextInput = api.streamPrompt(sessionId, nextPrompt, profile.modes.unattended, 60_000)
        .then((raw) => ({ raw }), (error: unknown) => ({ error }));
      await expect.poll(() => sessionEvents(sessionId).filter((event) =>
        event.event_type === 'command.accepted'
        && (event.payload as Record<string, unknown>).content === nextPrompt)).toHaveLength(1);
    }
    // Files resolve the sandbox without acquiring an engine output reader.
    // The native parent can start after restart while its journal is empty.
    if (scenario.unobserved) await restartServerContainer(absoluteBaseUrl(), 60_000);
    const childReceipt = `CHILD_${randomUUID()}`;
    await api.uploadFileText(sessionId, root, gate.release.split('/').at(-1)!, childReceipt);
    await expect.poll(() => readFile(started), { timeout: 30_000 }).toBe(prefix);
    if (scenario.unobserved) {
      const unread = documentsByField('session_events', '$.session_id', sessionId)
        .filter((row) => row.source_kind === 'resident_engine_output');
      expect(unread).toEqual([]);
      evidence.unobservedParentStarted = true;
      await submitNextInput();
    }
    const calls = () => native().filter((event) => event.type === 'tool/call'
      && event.data.name === 'bash' && String(event.data.arguments).includes(script));
    await expect.poll(calls).toHaveLength(1);
    const call = calls()[0]!;
    const partial = async () => visibleMessages(await api.getMessages(sessionId))
      .filter((message) => message.role === 'assistant' && messageText(message).includes(prefix));
    await expect.poll(partial).toHaveLength(1);
    const before = (await partial())[0]!;
    expect(before.blocks).toContainEqual(expect.objectContaining({ type: 'tool_use', id: call.data.callId,
      name: 'bash', input: JSON.parse(String(call.data.arguments)) }));
    expect(native().some((event) => event.type === 'turn/end' && event.data.turn === call.data.turn)).toBe(false);
    expect(documentsByField('channel_output_subscriptions', '$.session_id', sessionId)).toEqual([]);
    evidence.before = before;
    evidence.nativeCall = call;
    evidence.nativeBefore = native();

    // No Web page or channel reader is connected: the selected operation is
    // the first request that can acquire a runtime on this fresh backend.
    if (!scenario.unobserved) await restartServerContainer(absoluteBaseUrl(), 60_000);
    if (scenario.input && !scenario.unobserved) await submitNextInput();
    if (!scenario.input) {
      const childrenAfter = await api.listChildRuns(sessionId);
      expect(childrenAfter.child_runs.map((child) => child.child_run_id))
        .toEqual(childrenBefore.map((child) => child.child_run_id));
      evidence.control = childrenAfter;
    }
    await expect.poll(async () => (await partial()).map((message) => ({
      id: message.message_id, turn: message.turn_id, blocks: message.blocks,
    }))).toEqual([{ id: before.message_id, turn: before.turn_id, blocks: before.blocks }]);
    expect(native().some((event) => event.type === 'turn/end' && event.data.turn === call.data.turn)).toBe(false);
    await api.uploadFileText(sessionId, root, release.split('/').at(-1)!, parentReceipt);
    if (!scenario.input) await openSessionView(page, sessionId);
    await expect.poll(() => native().find((event) => event.type === 'turn/end'
      && event.data.turn === call.data.turn)?.data.reason, { timeout: 30_000 }).toEqual({ kind: 'completed' });
    if (nextInput) {
      const result = await nextInput;
      if ('error' in result) throw result.error;
      evidence.inputStream = result.raw;
      const nativeInputs = () => native().filter((event) => event.type === 'user/message'
        && (event.data.source as Record<string, unknown> | undefined)?.kind === 'user'
        && textBlocks(event.data.content) === nextPrompt);
      await expect.poll(nativeInputs).toHaveLength(1);
      const inputSequence = nativeInputs()[0]!.seq;
      expect(inputSequence).toBeGreaterThan(native().find((event) => event.type === 'turn/end'
        && event.data.turn === call.data.turn)!.seq);
      await expect.poll(async () => (await api.getMessages(sessionId)).messages
        .filter((message) => message.role === 'assistant' && messageText(message) === inputReceipt)).toHaveLength(1);
      const ownAnswer = (await api.getMessages(sessionId)).messages.find((message) =>
        message.role === 'assistant' && messageText(message) === inputReceipt)!;
      expect(ownAnswer.message_id).not.toBe(before.message_id);
      expect(ownAnswer.turn_id).not.toBe(before.turn_id);
      evidence.ownAnswer = ownAnswer;
      // Native history is mirrored independently of the live reply stream.
      // Its terminal makes the whole reply available to the native oracle.
      await expect.poll(() => native().find((event) => event.type === 'turn/end'
        && event.seq > inputSequence)?.data.reason).toEqual({ kind: 'completed' });
      expect(native().filter((event) => event.type === 'assistant/message' && event.seq > inputSequence)
        .flatMap((event) => textBlockValues((event.data.message as Record<string, unknown>).content)))
        .toEqual([inputReceipt]);
    }
    const expectedBlocks = native().filter((event) => event.type === 'assistant/message'
      && event.data.turn === call.data.turn)
      .flatMap((event) => textBlockValues((event.data.message as Record<string, unknown>).content));
    expect(expectedBlocks.join('\n')).toContain(parentReceipt);
    evidence.expectedBlocks = expectedBlocks;
    const restored = async () => (await api.getMessages(sessionId)).messages
      .filter((message) => message.message_id === before.message_id);
    await expect.poll(async () => (await restored()).map(messageProseBlocks), { timeout: 30_000 }).toEqual([expectedBlocks]);
    const history = await api.getMessages(sessionId);
    expect(history.messages.filter((message) => message.role === 'user').map(messageText))
      .toEqual(scenario.input ? [greeting, prompt, nextPrompt] : [greeting, prompt]);
    const results = native().filter((event) => event.type === 'tool/result' && event.data.turn === call.data.turn)
      .flatMap((event) => (event.data.message as { content: Array<Record<string, unknown>> }).content)
      .filter((block) => block.type === 'tool-result' && block.toolCallId === call.data.callId);
    expect(results).toHaveLength(1);
    expect(results[0]!.isError).toBe(false);
    expect(textBlocks(results[0]!.content)).toContain(parentReceipt);
    const recovered = (await restored())[0]!;
    expect(recovered.turn_id).toBe(before.turn_id);
    expect(recovered.blocks?.filter((block) => block.type === 'tool_use')).toEqual([
      expect.objectContaining({ id: call.data.callId, name: 'bash', input: JSON.parse(String(call.data.arguments)) }),
    ]);
    expect(recovered.blocks?.filter((block) => block.type === 'tool_result')).toEqual([
      expect.objectContaining({ tool_use_id: call.data.callId, is_error: false, content: expect.stringContaining(parentReceipt) }),
    ]);
    const output = framesForTurn(before.turn_id).map((row) => row.payload as Record<string, unknown>)
      .filter((frame) => frame.type === 'tool-output-available' && frame.toolCallId === call.data.callId);
    expect(output).toEqual([expect.objectContaining({ output: { content: results[0]!.content, isError: false } })]);
    if (scenario.input) await openSessionView(page, sessionId);
    await expect(page.getByTestId('assistant-text').filter({ hasText: parentReceipt }).last()).toBeVisible();
    const reply = page.locator(`[data-message-id="${before.message_id}"]`);
    // Live completion delays its process fold until the blocks settle. Wait
    // for this reply's header before expanding its groups inside that fold.
    await expect(reply.getByTestId('assistant-turn-process')).toHaveCount(1);
    await revealAssistantProcess(page, { within: reply });
    const originalText = before.blocks!.find((block) => block.type === 'text'
      && String(block.text).includes(prefix))!.text as string;
    const originalProse = reply.getByTestId('assistant-text').filter({ hasText: prefix }).first();
    await expect(originalProse).toBeVisible();
    await expect.poll(async () => normalizeRendered(await originalProse.innerText()))
      .toBe(normalizeRendered(originalText));
    await expect(originalProse).toContainText(childReceipt);
    const card = reply.locator(`[data-tool-call-id="${String(call.data.callId)}"]`);
    await expect(card).toHaveCount(1);
    const toggle = card.locator('button[aria-expanded]').first();
    if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
    await expect(card).toContainText(parentReceipt);
    if (scenario.input) {
      await expect(page.getByTestId('assistant-text').filter({ hasText: inputReceipt })).toBeVisible();
    }
    evidence.final = history;
  } finally {
    await info.attach('dsh-output-control-scene', { body: JSON.stringify({ ...evidence, sessionId, agentId,
      native: sessionId ? nativeRows(sessionId) : [], events: sessionId ? sessionEvents(sessionId) : [] }),
    contentType: 'application/json' });
  }
});
