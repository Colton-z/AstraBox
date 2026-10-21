/** A late native completion stays in the stopped reply, live and after a cold read. */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { sessionOutputRows, sessionResponseRows } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { IMAGE_RUNNER_LAUNCHER, imageRunnerRestartScript } from '../fixtures/runnerRestart';
import { requireSandboxHandle, sandboxExec } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => { if (agentId) await new AstraApi(request).deleteAgent(agentId); });

interface Observation {
  ack_seq?: number;
  result_seq?: number;
  streamed_text: string;
  complete_text: string;
  stream_message_id?: string;
  complete_message_id?: string;
  native_terminal_reason?: string;
  tail_parts: string[];
  events: Array<{ op: string; kind?: string; seq: number; engine_boundary: boolean }>;
}

test('interrupt ACK before complete AssistantMessage retains one cancelled reply and the next input works', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(`__e2e_interrupt_tail_${Date.now()}`);
  agentId = agent.agent_id;
  const runtime_profiles = await api.data<Array<{ name: string; engine_kind: string }>>('GET', '/admin/runtime-profiles');
  expect(runtime_profiles.find((item) => item.name === agent.runtime_profile_name)?.engine_kind).toBe('claude_code');
  const { session_id: sid } = await api.startConversation(agentId);
  sessions.push(sid);
  const ready = await api.waitForSessionReady(sid);
  const identity = (await api.adminSessionDetail(sid)).runtime_identity;
  expect(identity?.isolated_session_id ?? '', 'replacing this runner requires whole-box ownership').toBe('');
  expect(identity?.sandbox_id).toBe(ready.sandbox_id);
  const box = await requireSandboxHandle(api, String(ready.sandbox_id));
  const root = `/tmp/astrabox-interrupt-tail-${sid}`;
  const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
  const source = readFileSync(join(__dirname, '../fixtures/interrupt_tail_runner.py'));
  sandboxExec(box, [
    'set -eu',
    `install -d -m 700 -o "$ASTRABOX_WORKLOAD_USER" ${quote(root)}`,
    `printf '%s' ${quote(source.toString('base64'))} | base64 -d | runuser -u "$ASTRABOX_WORKLOAD_USER" -- tee ${quote(`${root}/runner.py`)} >/dev/null`,
    imageRunnerRestartScript({
      launch: `env ASTRABOX_INBOX_SERVER=${quote(`${root}/runner.py`)} ${IMAGE_RUNNER_LAUNCHER}`,
      log: `${root}/runner.log`, evidence: 'echo interrupt-tail-runner-ready', name: 'interrupt tail runner',
    }),
  ].join('\n'), 30_000);
  await api.adminEvictRuntime(sid);
  const observe = (): Observation => JSON.parse(sandboxExec(box,
    `cat ${quote(`${root}/observation.json`)}`, 5_000));
  await page.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
  await openSessionView(page, sid);
  const receipt = (await (await sendPrompt(page, sid,
    'Do not use tools. Do not use Markdown or code fences. Output 900 plain text lines labeled Line 1:, Line 2:, and so on, each saying: A long streaming reply must retain its text exactly once. Start now.',
  )).json()).data;
  await expect.poll(() => observe().streamed_text.length, { timeout: 60_000 }).toBeGreaterThan(200);
  const running = await api.getSession(sid);
  expect(running.current_responses).toHaveLength(1);
  const responseId = running.current_responses![0].response_id;
  expect(responseId).toBeTruthy();
  await expect(page.getByTestId('assistant-message')).toContainText('streaming reply');
  const stopped = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === apiPath(`/sessions/${sid}/interrupt`));
  await page.getByRole('button', { name: 'Stop generating', exact: true }).click();
  expect((await stopped).status()).toBe(200);
  await expect.poll(() => observe().result_seq, { timeout: 30_000 }).toBeTruthy();
  const native = observe();
  await test.info().attach('native-interrupt-order', { body: JSON.stringify(native), contentType: 'application/json' });
  expect(native.stream_message_id).toBeTruthy();
  expect(native.complete_message_id).toBe(native.stream_message_id);
  expect(native.complete_text, 'the real SDK must emit a completion block for this observation').not.toBe('');
  expect(['aborted_streaming', 'aborted_tools']).toContain(native.native_terminal_reason);
  const tails = native.events.filter((event) => event.kind === 'AssistantMessage' && event.seq > native.ack_seq!);
  expect(tails.length, 'the native complete block must reach the runner after interrupt ACK').toBeGreaterThan(0);
  expect(tails.every((event) => !event.engine_boundary)).toBe(true);
  expect(native.result_seq!).toBeGreaterThan(native.ack_seq!);
  await expect.poll(async () => (await api.getSession(sid)).runtime_status).toBe('idle');
  const rows = sessionResponseRows(sid);
  expect(rows).toEqual([expect.objectContaining({ response_id: responseId, origin: 'input', outcome: 'cancelled',
    input_ids: [receipt.input_id] })]);
  const before = await api.getMessages(sid);
  const assistants = before.messages.filter((message) => message.role === 'assistant');
  expect(assistants).toHaveLength(1);
  expect(messageText(assistants[0])).toBe(native.streamed_text);
  const output = sessionOutputRows(sid);
  const finish = output.find((row) => row.part.type === 'finish');
  expect(finish).toBeTruthy();
  const tail = output.filter((row) => row.seq > finish!.seq
    && ['text-end', 'reasoning-end', 'finish-step'].includes(String(row.part.type)));
  expect(tail.map((row) => row.part.type)).toEqual(native.tail_parts);
  expect(tail.map((row) => row.response_id)).toEqual(native.tail_parts.map(() => responseId));
  await api.adminEvictRuntime(sid);
  await page.reload({ waitUntil: 'domcontentloaded' });
  expect((await api.getMessages(sid)).messages).toEqual(before.messages);
  expect((await api.getHistoryBlocks(sid)).messages.filter((message) => message.role === 'assistant')).toHaveLength(1);
  await expect(page.getByTestId('assistant-message')).toHaveCount(1);
  await expect(page.locator(`[data-message-id="${responseId}"]`)).toContainText(native.streamed_text.trim());
  await sendPrompt(page, sid, 'Reply with only the number 2. Do not use tools.');
  await expect.poll(async () => (await api.getSession(sid)).last_response?.outcome, { timeout: 60_000 }).toBe('completed');
  const after = (await api.getMessages(sid)).messages.filter((message) => message.role === 'assistant');
  expect(after).toHaveLength(2);
  expect(messageText(after[0])).toBe(native.streamed_text);
  expect(messageText(after[1]).trim()).toBe('2');
  expect(sessionResponseRows(sid).map((row) => row.origin)).toEqual(['input', 'input']);
});
