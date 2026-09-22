/** A real later native answer survives when only its complete SDK message arrives. */
import { expect, test } from '@playwright/test';

import { AstraApi, type MessageRecord } from '../fixtures/astraApi';
import { appPath } from '../fixtures/env';
import { requireSandboxHandle } from '../fixtures/sandboxOps';
import { sdkCompleteMessageFault } from '../fixtures/sdkCompleteMessage';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

function publicText(message: MessageRecord): string {
  expect(Array.isArray(message.blocks), 'public history must carry typed content blocks').toBe(true);
  return message.blocks!.filter((block) => block.type === 'text').map((block) => {
    expect(typeof block.text).toBe('string');
    return block.text as string;
  }).join('');
}

test('a later complete SDK answer survives earlier streamed tool content in live and cold views', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(`__e2e_sdk_complete_${Date.now()}`);
  agentId = agent.agent_id;
  const session = await api.startConversation(agentId);
  const id = session.session_id;
  sessions.push(id);
  await api.waitForSessionReady(id);
  const warmup = await api.sendTurn(id, 'Briefly greet me without using tools.', 90_000);
  expect(warmup.errorText).toBeNull();
  expect(warmup.text.trim()).not.toBe('');
  const ready = await api.waitForSession(id, (value) => value.state === 'READY' && !value.current_turn_id, 30_000);
  const detail = await api.adminSessionDetail(id);
  expect(String(detail.runtime_identity?.isolated_session_id || ''), 'the fault must own a dedicated runner').toBe('');
  const sandbox = await requireSandboxHandle(api, String(ready.sandbox_id));
  const fault = sdkCompleteMessageFault(sandbox, id);
  let installed = false;
  try {
    await api.adminEvictRuntime(id);
    const restart = fault.install();
    installed = true;
    await page.goto(appPath(`/sessions/${id}`));
    await expect(page.getByTestId('run-view')).toBeVisible();
    const prompt = [
      'First say one brief sentence explaining that you will generate a fresh identifier.',
      'Then use Bash exactly once to run: python3 -c "import uuid; print(\'TOOL_RESULT_\' + str(uuid.uuid4()))"',
      'Do not use any other tools. After Bash returns, reply with exactly its output.',
    ].join('\n');
    const response = api.sendTurn(id, prompt, 90_000).then(
      (value) => ({ value, error: null }),
      (error: Error) => ({ value: null, error: error.message }),
    );
    await expect.poll(() => {
      const evidence = fault.read();
      if (evidence?.errors.length) throw new Error(evidence.errors.join('; '));
      return evidence?.stage;
    }, { timeout: 60_000, intervals: [200, 500] }).toBe('complete-awaiting-release');
    const held = fault.read()!;
    expect(held.forwarded_text.trim(), 'the earlier native message must really have streamed text').not.toBe('');
    expect(held.first_tool_message_id).not.toBe('');
    expect(held.suppressed_events).toBeGreaterThan(0);
    const withheld = held.complete_messages.filter((message) =>
      held.suppressed_message_ids.includes(message.message_id) && message.text.trim());
    expect(withheld).toHaveLength(1);
    const finalText = withheld[0].text;
    const marker = finalText.match(/TOOL_RESULT_[a-f0-9-]{36}/)?.[0];
    expect(marker, 'the complete reply must contain the actual random tool output').toBeTruthy();
    await expect(page.getByTestId('assistant-message').last().getByTestId('assistant-text')
      .filter({ hasText: held.forwarded_text.trim() })).toHaveCount(1);
    await expect(page.getByTestId('assistant-message').last().getByTestId('assistant-text')
      .filter({ hasText: marker! })).toHaveCount(0);
    fault.release();
    const reply = await response;
    expect(reply.error, 'the user turn must finish successfully').toBeNull();
    expect(reply.value?.errorText).toBeNull();
    expect(reply.value?.text).toContain(finalText);
    expect(reply.value!.text.split(marker!).length - 1, 'complete content appears exactly once in the live stream').toBe(1);
    await expect(page.getByTestId('assistant-message').last().getByTestId('assistant-text')
      .filter({ hasText: finalText.trim() })).toHaveCount(1);
    const settled = await api.waitForSession(id, (value) => value.state === 'READY' && !value.current_turn_id, 30_000);
    expect(settled.last_turn_status).toBe('COMPLETED');
    const complete = fault.read()!;
    expect(complete.stage).toBe('finished');
    expect(complete.errors).toEqual([]);
    expect(complete.result_is_error).toBe(false);
    const native = (await api.adminSessionTranscript(id, 30_000)).split('\n').filter(Boolean)
      .map((line) => JSON.parse(line) as { type: string; message?: { id?: string; content?: Array<Record<string, unknown>> } });
    const nativeAnswer = native.filter((entry) => entry.type === 'assistant'
      && entry.message?.id === withheld[0].message_id)
      .flatMap((entry) => entry.message?.content ?? [])
      .filter((block) => block.type === 'text').map((block) => String(block.text));
    expect(nativeAnswer, 'independent native transcript must contain the unchanged SDK answer').toContain(finalText);
    const toolResults = native.filter((entry) => entry.type === 'user')
      .flatMap((entry) => Array.isArray(entry.message?.content) ? entry.message.content : [])
      .filter((block) => block.type === 'tool_result');
    expect(toolResults.filter((block) => JSON.stringify(block.content).includes(marker!)),
      'the answer must come from real tool output, independently read from the native store').toHaveLength(1);
    const history = await api.getMessages(id, 50);
    const turnAnswers = history.messages.filter((message) =>
      message.role === 'assistant' && message.turn_id === settled.last_turn_id);
    expect(turnAnswers, 'the completed platform turn has one root answer with all native steps').toHaveLength(1);
    const answer = turnAnswers[0];
    expect(publicText(answer)).toBe(reply.value!.text);
    await page.reload();
    await expect(page.getByTestId('assistant-message').last().getByTestId('assistant-text')
      .filter({ hasText: finalText.trim() })).toHaveCount(1);
    expect(publicText((await api.getMessages(id, 50)).messages.find((message) => message.message_id === answer.message_id)!))
      .toBe(reply.value!.text);
    await test.info().attach('sdk-complete-native-receipt', {
      body: JSON.stringify({ sessionId: id, restart, held, complete, nativeAnswer }), contentType: 'application/json',
    });
  } finally {
    if (installed) {
      fault.disarm();
      fault.release();
      await test.info().attach('sdk-complete-final-evidence', {
        body: JSON.stringify({ sessionId: id, evidence: fault.read() }), contentType: 'application/json',
      });
    }
  }
});
