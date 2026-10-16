/** A DSH reply the harness started on its own keeps its output across a platform restart. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import {
  AstraApi, messageParts, messageText, messageToolCalls, visibleMessages,
} from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { sessionInputRows, sessionOutputRows, sessionResponseRows } from '../fixtures/dbOracle';
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

/** Read evidence for an attachment without letting a broken read replace the test's own failure. */
function observe(read: () => unknown): unknown {
  try { return read(); } catch (error) { return { unavailable: String(error) }; }
}

// Each case restarts the server while the harness's own reply is held in a
// foreground tool. Opening the conversation does not start a reader, so
// without an input the reply is recorded to its end only because the
// platform reads a live runtime by itself after the restart
// (`session_runtime/host.py`); an input sent after the restart starts the
// reader on its own path.
const cases = [
  { input: false, title: 'a DSH autonomous reply held across a platform restart keeps its partial output and completes without an input' },
  { input: true, title: 'a DSH input after restart preserves the unfinished autonomous reply and its own answer' },
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
      environment_name: profile.environment_name, model: profile.model, prepared_capacity: 0 })).agent_id;
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
    const childReceipt = `CHILD_${randomUUID()}`;
    await api.uploadFileText(sessionId, root, gate.release.split('/').at(-1)!, childReceipt);
    await expect.poll(() => readFile(started), { timeout: 30_000 }).toBe(prefix);
    const calls = () => native().filter((event) => event.type === 'tool/call'
      && event.data.name === 'bash' && String(event.data.arguments).includes(script));
    await expect.poll(calls).toHaveLength(1);
    const call = calls()[0]!;
    const partial = async () => visibleMessages(await api.getMessages(sessionId))
      .filter((message) => message.role === 'assistant' && messageText(message).includes(prefix));
    // The held call is recorded with its input and without a result.
    await expect.poll(async () => (await partial()).map((message) => messageToolCalls(message)
      .filter((tool) => tool.toolCallId === String(call.data.callId)).map((tool) => tool.state)))
      .toEqual([['input-available']]);
    const before = (await partial())[0]!;
    expect(messageToolCalls(before)).toContainEqual(expect.objectContaining({ toolCallId: call.data.callId,
      toolName: 'bash', input: JSON.parse(String(call.data.arguments)) }));
    // The harness started this reply for its child's settlement: no input asked for it.
    expect(before.metadata).toMatchObject({ origin: 'engine', input_ids: [], status: 'running' });
    expect(((await api.getSession(sessionId)).current_responses ?? []).map((response) => response.response_id))
      .toEqual([before.id]);
    expect(native().some((event) => event.type === 'turn/end' && event.data.turn === call.data.turn)).toBe(false);
    evidence.before = before;
    evidence.nativeCall = call;
    evidence.nativeBefore = native();

    // Nothing reads this conversation from a page or a channel when the server restarts.
    await restartServerContainer(absoluteBaseUrl(), 60_000);
    let nextInput: Promise<{ raw: string } | { error: unknown }> | undefined;
    if (scenario.input) {
      nextInput = api.streamPrompt(sessionId, nextPrompt, profile.modes.unattended, 60_000)
        .then((raw) => ({ raw }), (error: unknown) => ({ error }));
      await expect.poll(() => sessionInputRows(sessionId).filter((row) => row.content === nextPrompt))
        .toHaveLength(1);
    } else {
      const childrenAfter = await api.listChildRuns(sessionId);
      expect(childrenAfter.child_runs.map((child) => child.child_run_id), 'the restart keeps the child it found')
        .toEqual(childrenBefore.map((child) => child.child_run_id));
      evidence.childrenAfter = childrenAfter;
    }
    // The restart neither ends the held reply nor writes its output a second time.
    await expect.poll(async () => (await partial()).map((message) => ({ id: message.id, parts: message.parts })))
      .toEqual([{ id: before.id, parts: before.parts }]);
    expect(((await api.getSession(sessionId)).current_responses ?? []).map((response) => response.response_id))
      .toEqual([before.id]);
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
      const webInputs = sessionInputRows(sessionId).filter((row) => row.content === nextPrompt);
      expect(webInputs, 'the input is accepted once').toHaveLength(1);
      const ownAnswer = await api.waitForReplyTo(sessionId, webInputs[0]!.input_id);
      expect(messageText(ownAnswer)).toBe(inputReceipt);
      expect(ownAnswer.id).not.toBe(before.id);
      expect(ownAnswer.metadata).toMatchObject({ origin: 'input', input_ids: [webInputs[0]!.input_id],
        outcome: 'completed' });
      expect(sessionInputRows(sessionId).filter((row) => row.content === nextPrompt)).toEqual([
        expect.objectContaining({ state: 'admitted', response_id: ownAnswer.id }),
      ]);
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
      .filter((message) => message.id === before.id);
    await expect.poll(async () => (await restored()).map(messageProseBlocks), { timeout: 30_000 }).toEqual([expectedBlocks]);
    const history = await api.getMessages(sessionId);
    expect(history.messages.filter((message) => message.role === 'user').map(messageText))
      .toEqual(scenario.input ? [greeting, prompt, nextPrompt] : [greeting, prompt]);
    const results = native().filter((event) => event.type === 'tool/result' && event.data.turn === call.data.turn)
      .map((event) => event.data.message as Record<string, unknown>)
      .filter((message) => message.role === 'tool' && message.toolCallId === call.data.callId);
    expect(results).toHaveLength(1);
    expect(results[0]!.isError ?? false).toBe(false);
    expect(textBlocks(results[0]!.content)).toContain(parentReceipt);
    const recovered = (await restored())[0]!;
    expect(recovered.metadata).toMatchObject({ origin: 'engine', input_ids: [], status: 'ended', outcome: 'completed' });
    expect(messageToolCalls(recovered).filter((tool) => tool.toolCallId === String(call.data.callId))).toEqual([
      expect.objectContaining({ toolName: 'bash', input: JSON.parse(String(call.data.arguments)),
        state: 'output-available', output: { content: results[0]!.content, isError: false } }),
    ]);
    const output = sessionOutputRows(sessionId, before.id).map((row) => row.part)
      .filter((frame) => frame.type === 'tool-output-available' && frame.toolCallId === call.data.callId);
    expect(output).toEqual([expect.objectContaining({ output: { content: results[0]!.content, isError: false } })]);
    // The platform's own records: the reply the harness started completed, and
    // no response ended failed or cancelled across the restart.
    const responses = sessionResponseRows(sessionId);
    expect(responses.find((row) => row.response_id === before.id)).toMatchObject({
      origin: 'engine', status: 'ended', outcome: 'completed', native_reason: 'completed', input_ids: [],
    });
    expect(responses.filter((row) => row.status === 'ended' && row.outcome !== 'completed')).toEqual([]);
    if (scenario.input) await openSessionView(page, sessionId);
    await expect(page.getByTestId('assistant-text').filter({ hasText: parentReceipt }).last()).toBeVisible();
    const reply = page.locator(`[data-message-id="${before.id}"]`);
    // Live completion delays its process fold until the blocks settle. Wait
    // for this reply's header before expanding its groups inside that fold.
    await expect(reply.getByTestId('assistant-turn-process')).toHaveCount(1);
    await revealAssistantProcess(page, { within: reply });
    const originalText = String(messageParts(before, 'text')
      .find((part) => String(part.text).includes(prefix))!.text);
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
      native: sessionId ? observe(() => nativeRows(sessionId)) : [],
      inputs: sessionId ? observe(() => sessionInputRows(sessionId)) : [],
      responses: sessionId ? observe(() => sessionResponseRows(sessionId)) : [] }),
    contentType: 'application/json' });
  }
});
