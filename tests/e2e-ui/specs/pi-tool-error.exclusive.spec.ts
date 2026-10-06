/** A native failed tool stays failed in the live stream and retained history. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { framesForTurn, sessionEvents } from '../fixtures/dbOracle';
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
    name: `__e2e_pi_tool_error_${randomUUID()}`, environment_name: profile.environment_name,
    model: profile.model, prewarm_enabled: false,
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
    await sendPrompt(page, sessionId, `Use bash exactly once to run this command: ${command}\n`
      + 'The nonzero exit is intentional. Do not retry or use another tool. '
      + 'After it fails, report its actual stdout marker and exit code in one short sentence.');
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
    const settled = await api.waitForSessionReady(sessionId);
    expect(settled.last_turn_status).toBe('COMPLETED');
    const history = await api.getMessages(sessionId, 100);
    const reply = history.messages.find((message) => message.blocks?.some((block) => block.type === 'tool_use' && block.id === call.id));
    expect(reply).toBeTruthy();
    const outputs = framesForTurn(reply!.turn_id).map((row) => object(row.payload))
      .filter((frame) => String(frame.type).startsWith('tool-output-') && frame.toolCallId === call.id);
    expect(outputs).toHaveLength(1);
    expect(outputs[0]!.type).toBe('tool-output-error');
    const output = JSON.parse(String(outputs[0]!.errorText));
    expect(output.content).toEqual(result.content);
    if (result.details !== undefined) expect(output.details).toEqual(result.details);
    const retained = reply!.blocks!.filter((block) => block.type === 'tool_result' && block.tool_use_id === call.id);
    expect(retained).toEqual([expect.objectContaining({
      tool_result_state: 'output-error', is_error: true, content: outputs[0]!.errorText,
    })]);
    await expect.poll(async () => (await browserStreamFrames(page))
      .filter((frame) => frame.type === 'tool-output-error' && frame.toolCallId === call.id))
      .toEqual([expect.objectContaining({ errorText: outputs[0]!.errorText })]);
    evidence.history = history;
    evidence.outputs = outputs;
    const replyRow = page.locator(`[data-message-id="${reply!.message_id}"]`);
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
    expect(sessionEvents(sessionId).filter((event) => event.event_type === 'turn.failed')).toEqual([]);
  } finally {
    await info.attach('pi-tool-error-scene', { body: JSON.stringify({ sessionId, agentId, ...evidence,
      native: nativeEntries(sessionId), events: sessionEvents(sessionId) }), contentType: 'application/json' });
  }
});
