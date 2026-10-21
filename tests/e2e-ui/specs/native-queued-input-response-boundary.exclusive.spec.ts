/** Native queued inputs steer one response; their receipts stay visible live and cold. */
import { test, expect, type Page } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { nativeTranscriptRows, sessionInputRows, sessionOutputRows, sessionResponseRows } from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { terminalOutput } from '../fixtures/nativeMcpServer';
import { trackSessions } from '../fixtures/sessionCleanup';

const TURN_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 120_000);
const sessions = trackSessions();

function nativeEntries(sessionId: string): Array<{ seq: number; entry: Record<string, any> }> {
  return nativeTranscriptRows(sessionId).filter((row) => row.subpath === null)
    .map((row) => ({ seq: Number(row.seq), entry: JSON.parse(row.entry_json) }));
}

test('native queued inputs keep their actual response and visible consumption boundary live and cold', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const { session_id: sessionId } = await api.startConversation(agent.agent_id);
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  const run = Date.now();
  const start = `/workspace/queue-boundary-${run}.started`;
  const release = `/workspace/queue-boundary-${run}.release`;
  const toolReceipt = `TOOL_FINISHED_${run}`;
  const command = `touch ${start}; while [ ! -f ${release} ]; do sleep 0.2; done; printf ${toolReceipt}`;
  const followups = [`ADDITIONAL_A_${run}`, `ADDITIONAL_B_${run}`];
  const prompts = followups.map((marker) => `Additional request: include ${marker} in your final reply after Bash finishes. Do not call another tool.`);
  const evidence: Record<string, unknown> = { sessionId, command, prompts };

  const send = async (text: string) => {
    await page.getByTestId('composer-prompt').fill(text);
    await page.getByTestId('composer-submit').click();
    await expect(page.getByTestId('user-message').filter({ hasText: text })).toHaveCount(1);
  };
  try {
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('composer-prompt')).toBeEnabled({ timeout: 60_000 });
    await send([
      'Run Bash once in the foreground with timeout 120000, using exactly this command:',
      command,
      'Do not set run_in_background or release the command yourself. The test harness releases it.',
      'After it finishes, reply with its output and any additional requests you receive while it runs.',
      'Do not call any other tool.',
    ].join('\n'));
    await expect.poll(async () => terminalOutput(await api.runTerminalCommand(
      sessionId, `if test -f ${start}; then echo started; else echo waiting; fi`, '/workspace', 10_000,
    )).trim(), { timeout: TURN_MS, message: 'the real native Bash must start before additional inputs' }).toBe('started');
    expect(sessionResponseRows(sessionId)).toHaveLength(1);
    const responseId = sessionResponseRows(sessionId)[0]!.response_id;
    expect(sessionResponseRows(sessionId)[0]!.status).not.toBe('ended');
    for (const prompt of prompts) await send(prompt);
    await expect.poll(() => nativeEntries(sessionId).filter(({ entry }) => (
      entry.type === 'queue-operation' && entry.operation === 'enqueue'
      && prompts.includes(String(entry.content))
    )).length, { timeout: 30_000, message: 'both inputs reach the supplier queue while Bash is held' }).toBe(2);
    await api.runTerminalCommand(sessionId, `touch ${release}`, '/workspace', 10_000);

    await expect.poll(() => sessionResponseRows(sessionId)[0]?.outcome, { timeout: TURN_MS }).toBe('completed');
    const responses = sessionResponseRows(sessionId);
    expect(responses, 'native steering does not fabricate a response per input').toHaveLength(1);
    const inputs = sessionInputRows(sessionId);
    expect(inputs).toHaveLength(3);
    expect(inputs.every((input) => input.state === 'admitted' && input.failure === null)).toBe(true);
    expect([...responses[0]!.input_ids].sort()).toEqual(inputs.map((input) => input.input_id).sort());
    await expect.poll(() => nativeEntries(sessionId).filter(({ entry }) => (
      entry.type === 'attachment' && entry.attachment?.type === 'queued_command'
      && prompts.includes(entry.attachment?.prompt)
    )).length, { timeout: 30_000, message: 'native consumption records must reach SessionStore custody' }).toBe(2);
    const native = nativeEntries(sessionId);
    const calls = native.flatMap(({ seq, entry }) => (entry.message?.content instanceof Array
      ? entry.message.content.filter((block: any) => block.type === 'tool_use' && block.name === 'Bash')
        .map((block: any) => ({ seq, block })) : []));
    expect(calls).toHaveLength(1);
    expect(calls[0]!.block.input.command).toBe(command);
    const results = native.flatMap(({ seq, entry }) => (entry.message?.content instanceof Array
      ? entry.message.content.filter((block: any) => block.type === 'tool_result' && block.tool_use_id === calls[0]!.block.id)
        .map((block: any) => ({ seq, block })) : []));
    expect(results).toHaveLength(1);
    expect(JSON.stringify(results[0]!.block.content)).toContain(toolReceipt);
    const consumedNative = prompts.map((prompt) => native.filter(({ entry }) => (
      entry.type === 'attachment' && entry.attachment?.type === 'queued_command'
      && entry.attachment?.prompt === prompt
    )));
    for (const rows of consumedNative) expect(rows, 'one native queued_command consumption per input').toHaveLength(1);
    const nativeReplies = native.filter(({ entry }) => entry.type === 'assistant')
      .flatMap(({ seq, entry }) => (entry.message?.content instanceof Array
        ? entry.message.content.filter((block: any) => block.type === 'text').map((block: any) => ({ seq, text: String(block.text) })) : []));
    for (let i = 0; i < followups.length; i += 1) {
      expect(nativeReplies.some(({ seq, text }) => seq > consumedNative[i]![0]!.seq && text.includes(followups[i]!))).toBe(true);
    }

    const outputs = sessionOutputRows(sessionId);
    const receipts = outputs.filter((row) => row.part.type === 'data-input-consumed');
    expect(receipts).toHaveLength(3);
    for (const input of inputs) {
      const matched = receipts.filter((row) => (row.part.data as any).inputId === input.input_id);
      expect(matched).toHaveLength(1);
      expect((matched[0]!.part.data as any).responseMessageId).toBe(responseId);
      expect(matched[0]!.response_id).toBe(responseId);
    }
    evidence.native = native;
    evidence.inputs = inputs;
    evidence.responses = responses;
    evidence.outputs = outputs;

    const assertDisplay = async (view: Page) => {
      const response = view.locator(`[data-message-id="${responseId}"]`);
      await expect(response).toBeVisible();
      await expect(response.getByTestId('additional-input-receipt')).toHaveCount(2);
      for (const prompt of prompts) {
        const input = inputs.find((row) => row.content === prompt)!;
        const receipt = response.locator(`[data-testid="additional-input-receipt"][data-input-id="${input.input_id}"]`);
        await expect(receipt).toBeVisible();
        await expect(receipt).toContainText(/Additional input received|已接收追加消息/);
        await expect(receipt).toContainText(prompt);
        const user = view.getByTestId('user-message').filter({ hasText: prompt });
        await expect(user).toHaveCount(1);
        await expect(user.getByTestId('input-response-association')).toHaveAttribute('data-response-id', responseId);
        await expect(user).toContainText(/Added to the response above|已并入上方回复/);
      }
      for (const marker of followups) await expect(response.getByTestId('assistant-text').filter({ hasText: marker })).toBeVisible();
      const order = await response.locator('[data-testid="additional-input-receipt"], [data-testid="assistant-text"]').allTextContents();
      for (let i = 0; i < prompts.length; i += 1) {
        expect(order.findIndex((text) => text.includes(prompts[i]!))).toBeLessThan(
          order.findIndex((text) => text.includes(followups[i]!) && !text.includes(prompts[i]!)),
        );
      }
      return order;
    };
    const live = await assertDisplay(page);
    await page.reload();
    expect(await assertDisplay(page), 'cold projection preserves the native input boundaries').toEqual(live);
    const idle = `IDLE_REPLY_${run}`;
    await send(`Reply exactly ${idle} without tools.`);
    await expect.poll(() => sessionResponseRows(sessionId).length).toBe(2);
    await expect.poll(() => sessionResponseRows(sessionId)[1]?.outcome, { timeout: TURN_MS }).toBe('completed');
    const nextResponse = sessionResponseRows(sessionId)[1]!;
    expect(nextResponse.response_id).not.toBe(responseId);
    await expect(page.locator(`[data-message-id="${nextResponse.response_id}"]`).getByTestId('assistant-text')).toContainText(idle);
    const history = await api.getMessages(sessionId, 50);
    expect(history.messages.filter((message) => message.role === 'assistant' && messageText(message).includes(idle))).toHaveLength(1);
    evidence.finalResponses = sessionResponseRows(sessionId);
  } finally {
    evidence.native = nativeEntries(sessionId);
    evidence.inputs = sessionInputRows(sessionId);
    evidence.responses = sessionResponseRows(sessionId);
    evidence.outputs = sessionOutputRows(sessionId);
    await test.info().attach('native-queued-input-boundary-evidence', { body: JSON.stringify(evidence), contentType: 'application/json' });
  }
});
