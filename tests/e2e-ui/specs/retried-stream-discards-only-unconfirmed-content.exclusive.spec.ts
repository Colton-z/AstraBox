/** A real CLI retries truncated model streams without retaining abandoned previews. */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test } from '@playwright/test';
import { answersInput, AstraApi, messageText } from '../fixtures/astraApi';
import { IMAGE_RUNNER_LAUNCHER, imageRunnerRestartScript } from '../fixtures/runnerRestart';
import { requireSandboxHandle, sandboxExec } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => { if (agentId) await new AstraApi(request).deleteAgent(agentId); });

test('a replacement drops thinking and text previews while completed tools and later real work survive', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(`__e2e_stream_replacement_${Date.now()}`);
  agentId = agent.agent_id;
  const { session_id: sid } = await api.startConversation(agentId);
  sessions.push(sid);
  const ready = await api.waitForSessionReady(sid);
  const identity = (await api.adminSessionDetail(sid)).runtime_identity;
  expect(identity?.isolated_session_id ?? '', 'the native HTTP fault owns one whole cold test box').toBe('');
  expect(identity?.sandbox_id).toBe(ready.sandbox_id);
  const box = await requireSandboxHandle(api, String(ready.sandbox_id));
  const root = `/tmp/astrabox-stream-replacement-${sid}`;
  const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
  const source = readFileSync(join(__dirname, '../fixtures/stream_replacement_runner.py'));
  sandboxExec(box, [
    'set -eu',
    `install -d -m 700 -o "$ASTRABOX_WORKLOAD_USER" ${quote(root)}`,
    `printf '%s' ${quote(source.toString('base64'))} | base64 -d | runuser -u "$ASTRABOX_WORKLOAD_USER" -- tee ${quote(`${root}/runner.py`)} >/dev/null`,
    imageRunnerRestartScript({
      launch: `env ASTRABOX_INBOX_SERVER=${quote(`${root}/runner.py`)} ${IMAGE_RUNNER_LAUNCHER}`,
      log: `${root}/runner.log`, evidence: 'echo native-model-fault-ready', name: 'stream replacement runner',
    }),
  ].join('\n'), 30_000);
  await api.adminEvictRuntime(sid);
  const observe = () => JSON.parse(sandboxExec(box, `cat ${quote(`${root}/observation.json`)}`, 5_000)) as {
    requests: Record<string, number>;
    native: Array<{ kind: string; event?: Record<string, any>; message_id?: string; content?: unknown }>;
  };
  await page.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
  await openSessionView(page, sid);
  for (const mode of ['thinking', 'text']) {
    const result = await (await sendPrompt(page, sid, `E2E_REPLACE_${mode.toUpperCase()}: inspect the workspace.`)).json();
    const inputId = result.data.input_id;
    await expect.poll(() => JSON.stringify(observe().native), { timeout: 60_000 }).toContain(`E2E_ABANDONED_${mode}`);
    if (mode === 'thinking') {
      await page.getByTestId('assistant-message').last().getByTestId('assistant-process-trigger').last().click();
      const reasoning = page.getByTestId('assistant-message').last().getByTestId('reasoning-part').last();
      const trigger = reasoning.getByRole('button');
      if (await trigger.getAttribute('aria-expanded') === 'false') await trigger.click();
    }
    await expect(page.getByTestId('assistant-message').last()).toContainText(`E2E_ABANDONED_${mode}`);
    sandboxExec(box, `runuser -u "$ASTRABOX_WORKLOAD_USER" -- touch ${quote(`${root}/truncate-${mode}`)}`, 5_000);
    // The runner keeps its native process; a new host-side client must replay the attempt.
    await api.adminEvictRuntime(sid);
    await page.reload({ waitUntil: 'domcontentloaded' });
    sandboxExec(box, `runuser -u "$ASTRABOX_WORKLOAD_USER" -- touch ${quote(`${root}/release-${mode}`)}`, 5_000);
    await expect.poll(async () => {
      const response = (await api.getMessages(sid)).messages.find((item) => answersInput(item, inputId));
      return response?.metadata?.outcome ?? '';
    }, { timeout: 90_000 }).toBe('completed');
    await expect(page.getByTestId('assistant-message').last()).toContainText(`E2E_REPLACED_${mode}_DONE`);
    await expect(page.getByTestId('assistant-message').last()).not.toContainText('E2E_ABANDONED_');
    await expect(page.getByTestId('assistant-message').last()).not.toContainText('Retrying');
    const native = observe();
    const attempts = native.native.filter((item) => item.kind === 'stream'
      && item.event?.type === 'message_start' && String(item.event?.message?.id).includes(`_${mode}_`));
    expect(attempts.length, 'the CLI must receive a real partial model message').toBeGreaterThan(0);
    const complete = native.native.filter((item) => item.kind === 'assistant'
      && String(item.message_id).includes(`_${mode}_`));
    expect(complete.some((item) => JSON.stringify(item.content).includes(`E2E_REPLACED_${mode}_DONE`))).toBe(true);
    expect(complete.some((item) => JSON.stringify(item.content).includes('E2E_ABANDONED_'))).toBe(false);
    const history = await api.getMessages(sid);
    expect(JSON.stringify(history.messages)).not.toContain('E2E_ABANDONED_');
    expect(history.messages.filter((item) => item.role === 'assistant').map(messageText).join('\n'))
      .toContain('E2E_KEEP_COMPLETED_STEP');
    const folded = await api.getHistoryBlocks(sid);
    for (const message of folded.messages) {
      for (const part of message.parts ?? []) {
        if (part.type !== 'data-process-block') continue;
        const block = part.data as { block_id: string; cursor: string };
        const detail = await api.data('GET', `/sessions/${sid}/history-blocks/${encodeURIComponent(block.block_id)}?cursor=${encodeURIComponent(block.cursor)}`);
        expect(JSON.stringify(detail)).not.toContain('E2E_ABANDONED_');
      }
    }
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('assistant-message').last()).toContainText(`E2E_REPLACED_${mode}_DONE`);
    await expect(page.getByTestId('assistant-message').last()).not.toContainText('Retrying');
  }
  const before = await api.getMessages(sid);
  expect(observe().native.some((item) => item.kind === 'user'
    && JSON.stringify(item.content).includes('E2E_KEEP_TOOL_RESULT'))).toBe(true);
  expect(JSON.stringify(before)).toContain('E2E_KEEP_TOOL_RESULT');
  const receipt = await (await sendPrompt(page, sid,
    'Run pwd and printf replacement-recovered using Bash, then report the exact output.',
  )).json();
  await expect.poll(async () => {
    const response = (await api.getMessages(sid)).messages.find((item) => answersInput(item, receipt.data.input_id));
    return response?.metadata?.outcome ?? '';
  }, { timeout: 90_000 }).toBe('completed');
  const after = await api.getMessages(sid);
  expect(after.messages.slice(0, before.messages.length)).toEqual(before.messages);
  const last = after.messages.filter((item) => item.role === 'assistant').at(-1)!;
  expect(last.parts?.some((part) => part.type === 'dynamic-tool' && part.state === 'output-available'
    && JSON.stringify(part.output).includes('replacement-recovered'))).toBe(true);
  await test.info().attach('native-replacement-observation', { body: JSON.stringify(observe()), contentType: 'application/json' });
});
