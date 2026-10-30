/** Native queued inputs steer one response; their receipts stay visible live and cold. */
import { test, expect, type Page } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { nativeTranscriptRows, sessionInputRows, sessionOutputRows, sessionResponseRows } from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { terminalOutput } from '../fixtures/nativeMcpServer';
import { trackSessions } from '../fixtures/sessionCleanup';
import { readFileSync } from 'node:fs';

const TURN_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 120_000);
const sessions = trackSessions();

function nativeEntries(sessionId: string): Array<{ seq: number; entry: Record<string, any> }> {
  return nativeTranscriptRows(sessionId).filter((row) => row.subpath === null)
    .map((row) => ({ seq: Number(row.seq), entry: JSON.parse(row.entry_json) }));
}

test('native queued inputs keep their actual response and visible consumption boundary live and cold', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const retained = (process.env.ASTRABOX_E2E_RETAINED_QUEUE_SESSION || '').trim();
  const { session_id: sessionId } = retained ? { session_id: retained } : await api.startConversation(agent.agent_id);
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  const previous = retained ? sessionInputRows(sessionId) : [];
  if (retained) expect(previous).toHaveLength(3);
  const run = retained ? Number(previous[0]!.content.match(/BEFORE_QUEUE_(\d+)/)?.[1]) : Date.now();
  expect(run).toBeGreaterThan(0);
  const start = `/workspace/queue-boundary-${run}.started`;
  const release = `/workspace/queue-boundary-${run}.release`;
  const secondStart = `/workspace/queue-boundary-${run}.second-started`;
  const secondRelease = `/workspace/queue-boundary-${run}.second-release`;
  const toolReceipt = `TOOL_FINISHED_${run}`;
  const secondToolReceipt = `SECOND_TOOL_FINISHED_${run}`;
  const command = `touch ${start}; while [ ! -f ${release} ]; do sleep 0.2; done; printf ${toolReceipt}`;
  const secondCommand = `touch ${secondStart}; while [ ! -f ${secondRelease} ]; do sleep 0.2; done; printf ${secondToolReceipt}`;
  const beforeText = `BEFORE_QUEUE_${run}`;
  const afterText = `AFTER_QUEUE_${run}`;
  const followups = [`ADDITIONAL_A_${run}`, `ADDITIONAL_B_${run}`];
  const prompts = followups.map((marker) => `Additional request: include ${marker} in your final reply after both prescribed Bash commands finish. Keep the original two-command plan; no extra tools.`);
  const evidence: Record<string, unknown> = { sessionId, command, secondCommand, prompts };

  const send = async (text: string) => {
    await page.getByTestId('composer-prompt').fill(text);
    await page.getByTestId('composer-submit').click();
    await expect(page.getByTestId('user-message').filter({ hasText: text })).toHaveCount(1);
  };
  let responseId = retained ? sessionResponseRows(sessionId)[0]!.response_id : '';
  const assertDisplay = async (view: Page) => {
    await expect(view.getByTestId('assistant-text').filter({ hasText: afterText })).toBeAttached();
      await expect(view.getByTestId('additional-input-receipt')).toHaveCount(0);
    for (const prompt of prompts) {
      const user = view.getByTestId('user-message').filter({ hasText: prompt });
      await expect(user).toHaveCount(1);
      await expect(user.getByTestId('input-response-association')).toHaveAttribute('data-response-id', responseId);
      await expect(user).toContainText(/Received by the runtime here|运行时在此处接收/);
    }
    const order = await view.locator('[data-testid="assistant-text"], [data-testid="user-message"]').allTextContents();
    const before = order.findIndex((text) => text.trim() === beforeText);
    const after = order.findIndex((text) => text.trim() === afterText);
    expect(before).toBeGreaterThanOrEqual(0);
    expect(after).toBeGreaterThan(before);
    for (const prompt of prompts) {
      const index = order.findIndex((text) => text.includes(prompt));
      expect(index).toBeGreaterThan(before);
      expect(index).toBeLessThan(after);
    }
    const responseIds = await view.locator('[data-response-segment]').evaluateAll((rows) => rows.map((row) => row.getAttribute('data-message-id')));
    expect(responseIds.length).toBeGreaterThan(1);
    expect(new Set(responseIds)).toEqual(new Set([responseId]));
    return order;
  };
  try {
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('composer-prompt')).toBeEnabled({ timeout: 60_000 });
    if (!retained) {
      await send([
        `First say exactly ${beforeText}, then run Bash in the foreground with timeout 120000, using exactly this command:`,
        command,
        'Do not set run_in_background or release the command yourself. The test harness releases it.',
        `After it finishes and you receive any queued requests, say exactly ${afterText}, then run Bash in the foreground with timeout 120000 using exactly:`,
        secondCommand,
        'The harness also releases this second command. After it finishes, reply with both outputs and all additional requests.',
        'Exactly these two Bash calls, no other tools or delegation.',
      ].join('\n'));
      await expect.poll(async () => terminalOutput(await api.runTerminalCommand(
        sessionId, `if test -f ${start}; then echo started; else echo waiting; fi`, '/workspace', 10_000,
      )).trim(), { timeout: TURN_MS, message: 'the real native Bash must start before additional inputs' }).toBe('started');
      expect(sessionResponseRows(sessionId)).toHaveLength(1);
      responseId = sessionResponseRows(sessionId)[0]!.response_id;
      expect(sessionResponseRows(sessionId)[0]!.status).not.toBe('ended');
      for (const prompt of prompts) await send(prompt);
      await expect.poll(() => nativeEntries(sessionId).filter(({ entry }) => (
        entry.type === 'queue-operation' && entry.operation === 'enqueue'
        && prompts.includes(String(entry.content))
      )).length, { timeout: 30_000, message: 'both inputs reach the supplier queue while Bash is held' }).toBe(2);
      for (const prompt of prompts) {
        const user = page.getByTestId('user-message').filter({ hasText: prompt });
        await expect(user).toHaveCount(1);
        await expect(user.getByTestId('input-response-association')).toHaveCount(0);
      }
      await api.runTerminalCommand(sessionId, `touch ${release}`, '/workspace', 10_000);
      await expect.poll(async () => terminalOutput(await api.runTerminalCommand(
        sessionId, `if test -f ${secondStart}; then echo started; else echo waiting; fi`, '/workspace', 10_000,
      )).trim(), { timeout: TURN_MS }).toBe('started');
      expect(sessionResponseRows(sessionId)[0]!.status).not.toBe('ended');

      evidence.streamingDisplay = await assertDisplay(page);
      await api.runTerminalCommand(sessionId, `touch ${secondRelease}`, '/workspace', 10_000);
    } else {
      const file = process.env.ASTRABOX_E2E_QUEUE_EVIDENCE_FILE;
      if (!file) throw new Error('continued scene requires its original native evidence receipt');
      const prior = JSON.parse(readFileSync(file, 'utf8'));
      expect(prior.sessionId).toBe(sessionId);
      expect(prior.command).toBe(command);
      expect(prior.prompts).toEqual(prompts);
      expect(prior.streamingDisplay).toBeInstanceOf(Array);
      evidence.streamingDisplay = prior.streamingDisplay;
      await test.info().attach('original-action-evidence', { path: file, contentType: 'application/json' });
    }

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
    expect(calls).toHaveLength(2);
    expect(calls[0]!.block.input.command).toBe(command);
    expect(calls[1]!.block.input.command).toBe(secondCommand);
    const results = native.flatMap(({ seq, entry }) => (entry.message?.content instanceof Array
      ? entry.message.content.filter((block: any) => block.type === 'tool_result' && block.tool_use_id === calls[0]!.block.id)
        .map((block: any) => ({ seq, block })) : []));
    expect(results).toHaveLength(1);
    expect(JSON.stringify(results[0]!.block.content)).toContain(toolReceipt);
    const secondResults = native.flatMap(({ entry }) => (entry.message?.content instanceof Array
      ? entry.message.content.filter((block: any) => block.type === 'tool_result' && block.tool_use_id === calls[1]!.block.id) : []));
    expect(secondResults).toHaveLength(1);
    expect(JSON.stringify(secondResults[0]!.content)).toContain(secondToolReceipt);
    const consumedNative = prompts.map((prompt) => native.filter(({ entry }) => (
      entry.type === 'attachment' && entry.attachment?.type === 'queued_command'
      && entry.attachment?.prompt === prompt
    )));
    for (const rows of consumedNative) expect(rows, 'one native queued_command consumption per input').toHaveLength(1);
    const nativeReplies = native.filter(({ entry }) => entry.type === 'assistant')
      .flatMap(({ seq, entry }) => (entry.message?.content instanceof Array
        ? entry.message.content.filter((block: any) => block.type === 'text').map((block: any) => ({ seq, text: String(block.text) })) : []));
    const nativeBefore = nativeReplies.find(({ text }) => text.trim() === beforeText)!;
    const nativeAfter = nativeReplies.find(({ text }) => text.trim() === afterText)!;
    for (let i = 0; i < followups.length; i += 1) {
      expect(nativeBefore.seq).toBeLessThan(consumedNative[i]![0]!.seq);
      expect(nativeAfter.seq).toBeGreaterThan(consumedNative[i]![0]!.seq);
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

    const assertTools = async () => {
      const blocks = page.getByTestId('assistant-turn-process').filter({
        has: page.getByTestId('assistant-turn-process-trigger').filter({ hasText: /\d+ tool calls?|\d+ 次工具调用/ }),
      });
      for (let index = 0; index < await blocks.count(); index += 1) {
        const trigger = blocks.nth(index).getByTestId('assistant-turn-process-trigger');
        if ((await trigger.getAttribute('aria-expanded')) !== 'true') await trigger.click();
      }
      const groups = page.getByTestId('assistant-process');
      for (let index = 0; index < await groups.count(); index += 1) {
        const group = groups.nth(index);
        if ((await group.getAttribute('data-open')) === null) await group.getByTestId('assistant-process-trigger').click();
      }
      for (const [index, call] of calls.entries()) {
        const card = page.locator(`[data-tool-call-id="${call.block.id}"]`);
        await expect(card).toHaveCount(1);
        const trigger = card.getByRole('button').first();
        if ((await trigger.getAttribute('aria-expanded')) !== 'true') await trigger.click();
        await expect(card.locator('pre').first()).toBeVisible();
        expect(JSON.parse((await card.locator('pre').first().textContent())!)).toEqual(call.block.input);
        await expect(card).toContainText(index === 0 ? toolReceipt : secondToolReceipt);
      }
    };
    const live = await assertDisplay(page);
    await assertTools();
    await page.reload();
    expect(await assertDisplay(page), 'cold projection preserves the native input boundaries').toEqual(live);
    await assertTools();
    const pages: any[] = [];
    await page.route('**/history-blocks?*', async (route) => {
      const url = new URL(route.request().url());
      url.searchParams.set('limit', '1');
      await route.continue({ url: url.toString() });
    });
    page.on('response', async (response) => {
      if (response.url().includes(`/sessions/${sessionId}/history-blocks?`)) pages.push(await response.json());
    });
    await page.reload();
    await expect(page.getByTestId('assistant-text').filter({ hasText: beforeText })).toBeAttached();
    expect(await assertDisplay(page), 'real one-message history pages reassemble the same native timeline').toEqual(live);
    await assertTools();
    expect(pages.length).toBeGreaterThan(1);
    evidence.pagedHistory = pages;
    await page.unroute('**/history-blocks?*');
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
