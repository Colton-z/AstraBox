/** A native failed tool stays failed in the live stream and retained history. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageToolCalls } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { sessionInputRows, sessionOutputRows, sessionResponseRows } from '../fixtures/dbOracle';
import { engineProfileFor } from '../fixtures/engineProfile';
import { browserStreamFrames } from '../fixtures/nativeMcpServer';
import { nativeEntries, nativeText, object } from '../fixtures/piChildFailure';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { mirrorSseBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('Pi retains a native tool failure across reload and continues the conversation', async ({ page, request }, info) => {
  const api = new AstraApi(request);
  const profile = engineProfileFor('pi');
  agentId = (await api.createAgent({
    name: `__e2e_pi_tool_error_${randomUUID()}`, runtime_profile_name: profile.runtime_profile_name,
    model: profile.model, prepared_capacity: 0,
  })).agent_id;
  const sessionId = (await api.startConversation(agentId)).session_id;
  sessions.push(sessionId);
  const evidence: Record<string, unknown> = {};
  try {
    await api.waitForSessionReady(sessionId);
    if (profile.modes.unattended) await api.setPermissionMode(sessionId, profile.modes.unattended);
    await mirrorSseBodies(page);
    await openSessionView(page, sessionId);
    const marker = `NATIVE_FAILURE_${randomUUID()}`;
    const command = `printf '%s\\n' '${marker}'; exit 17`;
    const prompt = `Use bash exactly once to run this command: ${command}\n`
      + 'The nonzero exit is intentional. Do not retry or use another tool. '
      + 'After it fails, report its actual stdout marker and exit code in one short sentence.';
    await sendPrompt(page, sessionId, prompt);
    const native = () => nativeEntries(sessionId);
    const calls = () => native().flatMap(({ entry, subpath }) => {
      const message = object(entry.message);
      return message.role === 'assistant' && Array.isArray(message.content)
        ? message.content.map(object).filter((part) => part.type === 'toolCall' && part.name === 'bash'
          && String(object(part.arguments).command ?? '').includes(marker))
          .map((part) => ({ id: String(part.id), subpath })) : [];
    });
    await expect.poll(calls, { timeout: 45_000 }).toHaveLength(1);
    const call = calls()[0]!;
    const results = () => native().filter((row) => row.subpath === call.subpath)
      .map(({ entry }) => object(entry.message))
      .filter((message) => message.role === 'toolResult' && message.toolCallId === call.id);
    await expect.poll(results, { timeout: 30_000 }).toHaveLength(1);
    const result = results()[0]!;
    expect(result.isError).toBe(true);
    expect(nativeText(result.content)).toContain(marker);
    expect(nativeText(result.content)).toContain('17');
    evidence.nativeToolResult = result;
    const inputs = sessionInputRows(sessionId).filter((row) => row.content === prompt);
    expect(inputs, 'the prompt is one recorded input').toHaveLength(1);
    const reply = await api.waitForReplyTo(sessionId, inputs[0]!.input_id);
    expect(reply.metadata?.outcome, 'a failed tool does not fail the response that ran it').toBe('completed');
    const settled = await api.waitForSessionReady(sessionId);
    expect(settled.last_response).toMatchObject({ response_id: reply.id, outcome: 'completed' });
    const history = await api.getMessages(sessionId, 100);
    expect(history.messages.filter((message) => messageToolCalls(message).some((part) => part.toolCallId === call.id))
      .map((message) => message.id), 'the native call is shown once, in the reply to the prompt').toEqual([reply.id]);
    const outputs = sessionOutputRows(sessionId, reply.id).map((row) => row.part)
      .filter((frame) => String(frame.type).startsWith('tool-output-') && frame.toolCallId === call.id);
    expect(outputs).toHaveLength(1);
    expect(outputs[0]!.type).toBe('tool-output-error');
    const output = JSON.parse(String(outputs[0]!.errorText));
    expect(output.content).toEqual(result.content);
    if (result.details !== undefined) expect(output.details).toEqual(result.details);
    const retained = messageToolCalls(reply).filter((part) => part.toolCallId === call.id);
    expect(retained).toEqual([expect.objectContaining({
      toolName: 'bash', state: 'output-error', errorText: outputs[0]!.errorText,
    })]);
    await expect.poll(async () => (await browserStreamFrames(page))
      .filter((frame) => frame.type === 'tool-output-error' && frame.toolCallId === call.id))
      .toEqual([expect.objectContaining({ errorText: outputs[0]!.errorText })]);
    evidence.history = history;
    evidence.outputs = outputs;
    const replyRow = page.locator(`[data-message-id="${reply.id}"]`);
    const card = replyRow.locator(`[data-tool-call-id="${String(call.id)}"]`);
    async function expectFailedCard() {
      await expect(replyRow.getByTestId('assistant-turn-process')).toHaveCount(1);
      await revealAssistantProcess(page, { within: replyRow });
      await expect(card).toHaveCount(1);
      const toggle = card.getByRole('button', { name: /^bash (Failed|执行失败)$/ });
      await expect(toggle).toBeVisible();
      if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
      await expect(card).toContainText(String(outputs[0]!.errorText));
    }
    await expectFailedCard();
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible();
    await expectFailedCard();
    const reloaded = await api.getMessages(sessionId, 100);
    expect(reloaded.messages).toEqual(history.messages);
    const receipt = `CONTINUED_${randomUUID()}`;
    const next = await api.sendTurn(sessionId, `Reply exactly ${receipt}. Do not use tools.`, 45_000);
    expect(next.errorText).toBeNull();
    expect(next.text.trim()).toBe(receipt);
    expect((await api.waitForReplyTo(sessionId, next.inputId)).metadata?.outcome).toBe('completed');
    expect(sessionResponseRows(sessionId).filter((row) => row.outcome !== 'completed'),
      'no response of the conversation fails or is cancelled').toEqual([]);
  } finally {
    await info.attach('pi-tool-error-scene', { body: JSON.stringify({ sessionId, agentId, ...evidence,
      native: nativeEntries(sessionId), inputs: sessionInputRows(sessionId),
      responses: sessionResponseRows(sessionId) }), contentType: 'application/json' });
  }
});
