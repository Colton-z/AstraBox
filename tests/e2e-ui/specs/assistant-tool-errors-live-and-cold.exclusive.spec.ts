/** Native validation failures must stay failed in the live card and cold history. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageToolCalls } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { sessionInputRows, sessionOutputRows } from '../fixtures/dbOracle';
import { browserStreamFrames, terminalOutput } from '../fixtures/nativeMcpServer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { mirrorSseBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
let assistantId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

test('an Assistant native tool validation error stays failed live and cold while successful business error text stays Done', async ({ page, request }, info) => {
  const api = new AstraApi(request);
  const profile = await api.assistantEnvironmentName();
  assistantId = (await api.createAssistant({ display_name: `__e2e_tool_error_${randomUUID()}`,
    runtime_profile_name: profile,
    model_config_override: { model_name: await api.assistantModelName(profile) },
    engine_options: { config: { tools: { tool_search: { enabled: 'on' } } } },
  })).assistant_id;
  const sessionId = (await api.startAssistantConversation(assistantId)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId, 90_000);
  const marker = `SUCCESS_${randomUUID().replace(/-/g, '')}`;
  const validation = "tool_call calls[0] requires a 'name'";
  const malformed = { calls: [{ arguments: { action: 'create', name: 'example-job' } }] };
  const command = `printf '%s\\n' '{"error":"business text ${marker}"}'`;
  const prompt = [
    'Perform this intentional, non-destructive native tool validation check. Exactly two tool calls:',
    `1. Call tool_call with exactly this JSON: ${JSON.stringify(malformed)}`,
    'The absent calls[0].name is intentional. Do not repair the arguments, discover tools, create a job, or retry.',
    `2. Call terminal directly with command ${JSON.stringify(command)}. Do not treat its printed business JSON as an execution failure.`,
    `Then report the actual validation error and say ${marker}. Use no other tools.`,
  ].join('\n');
  const evidence: Record<string, unknown> = { sessionId, assistantId, prompt };
  let releaseHistory = () => {};
  let holdHistory = false;
  let initialReads = 0;
  const hold = new Promise<void>((resolve) => { releaseHistory = resolve; });
  await page.route((url) => url.pathname.endsWith(`/sessions/${sessionId}/history-blocks`), async (route) => {
    if (holdHistory) { await hold; await route.continue(); }
    else { const response = await route.fetch(); await route.fulfill({ response }); initialReads += 1; }
  });
  try {
    await mirrorSseBodies(page);
    await openSessionView(page, sessionId);
    await expect.poll(() => initialReads).toBeGreaterThan(0);
    await expect(page.getByTestId('composer-prompt')).toBeEnabled();
    holdHistory = true;
    await sendPrompt(page, sessionId, prompt);
    await expect(page.getByTestId('assistant-text').filter({ hasText: marker })).toBeVisible({ timeout: 60_000 });
    await api.waitForSessionReady(sessionId, 30_000);
    const detail = await api.adminSessionDetail(sessionId);
    const home = String(detail.runtime_identity?.config_dir || '');
    const nativeId = String(detail.engine_session_key || '');
    expect(home).toMatch(/^\/home\//);
    expect(nativeId).not.toBe('');
    // Read the actual supplier SQLite rows, not the adapter's output or compact display history.
    const program = [
      'import json, sqlite3',
      `connection = sqlite3.connect(${JSON.stringify(`file:${home}/state.db?mode=ro`)}, uri=True)`,
      'connection.row_factory = sqlite3.Row',
      `rows = [dict(row) for row in connection.execute("SELECT role,content,tool_calls,tool_call_id FROM messages WHERE session_id=? ORDER BY id", (${JSON.stringify(nativeId)},))]`,
      'for row in rows:',
      '    row["tool_calls"] = json.loads(row["tool_calls"]) if row["tool_calls"] else []',
      'print("E2E_NATIVE_JSON:" + json.dumps(rows))',
    ].join('\n');
    const output = terminalOutput(await api.runTerminalCommand(sessionId,
      `/opt/hermes/.venv/bin/python - <<'PY'\n${program}\nPY`, '/tmp', 30_000));
    const records = output.split('\n').filter((line) => line.startsWith('E2E_NATIVE_JSON:'));
    expect(records, output).toHaveLength(1);
    type NativeRow = { role: string; content: string; tool_call_id: string;
      tool_calls: Array<{ id: string; function: { name: string; arguments: string } }> };
    const native = JSON.parse(records[0]!.slice('E2E_NATIVE_JSON:'.length)) as NativeRow[];
    evidence.native = native;
    const calls = native.filter((row) => row.role === 'assistant').flatMap((row) => row.tool_calls);
    const invalid = calls.filter((call) => call.function.name === 'tool_call');
    const valid = calls.filter((call) => call.function.name === 'terminal');
    expect(invalid, 'the original malformed call must reach native validation once').toHaveLength(1);
    expect(JSON.parse(invalid[0]!.function.arguments)).toEqual(malformed);
    expect(valid).toHaveLength(1);
    expect(JSON.parse(valid[0]!.function.arguments).command).toBe(command);
    const nativeError = native.filter((row) => row.role === 'tool' && row.tool_call_id === invalid[0]!.id);
    const nativeSuccess = native.filter((row) => row.role === 'tool' && row.tool_call_id === valid[0]!.id);
    expect(nativeError).toHaveLength(1);
    expect(nativeError[0]!.content).toContain(validation);
    expect(nativeSuccess).toHaveLength(1);
    expect(JSON.parse(nativeSuccess[0]!.content)).toMatchObject({ exit_code: 0, output: expect.stringContaining(`"error":"business text ${marker}"`) });
    const inputs = sessionInputRows(sessionId).filter((row) => row.content === prompt);
    expect(inputs).toHaveLength(1);
    const reply = await api.waitForReplyTo(sessionId, inputs[0]!.input_id, { timeoutMs: 10_000 });
    expect(reply.metadata?.outcome).toBe('completed');
    const parts = sessionOutputRows(sessionId, reply.id).map((row) => row.part);
    const failures = parts.filter((part) => String(part.type).startsWith('tool-output-') && part.toolCallId === invalid[0]!.id);
    expect(failures).toEqual([expect.objectContaining({ type: 'tool-output-error', errorText: expect.stringContaining(validation) })]);
    const successes = parts.filter((part) => String(part.type).startsWith('tool-output-') && part.toolCallId === valid[0]!.id);
    expect(successes).toEqual([expect.objectContaining({ type: 'tool-output-available' })]);
    await expect.poll(async () => (await browserStreamFrames(page)).filter((frame) => frame.type === 'tool-output-error'
      && frame.toolCallId === invalid[0]!.id)).toEqual([expect.objectContaining({ errorText: failures[0]!.errorText })]);
    evidence.stream = await browserStreamFrames(page);
    evidence.reply = reply;
    evidence.outputs = parts;
    const row = page.locator(`[data-message-id="${reply.id}"]`);
    async function checkCards() {
      await expect(row.getByTestId('assistant-turn-process')).toHaveCount(1);
      await revealAssistantProcess(page, { within: row });
      const failed = row.locator(`[data-tool-call-id="${invalid[0]!.id}"]`);
      const toggle = failed.getByRole('button', { name: /^tool_call (Failed|执行失败)$/ });
      await expect(toggle).toBeVisible();
      if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
      await expect(failed).toContainText(validation);
      const succeeded = row.locator(`[data-tool-call-id="${valid[0]!.id}"]`);
      await expect(succeeded.getByRole('button', { name: /^terminal (Done|已完成)$/ })).toBeVisible();
    }
    await checkCards();
    holdHistory = false;
    releaseHistory();
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible();
    await checkCards();
    const retained = (await api.getMessages(sessionId)).messages.find((message) => message.id === reply.id);
    expect(retained).toBeDefined();
    expect(messageToolCalls(retained!).filter((part) => part.toolCallId === invalid[0]!.id))
      .toEqual([expect.objectContaining({ state: 'output-error', errorText: failures[0]!.errorText })]);
    await page.screenshot({ path: info.outputPath('native-tool-failure-cold.png') });
  } finally {
    releaseHistory();
    await info.attach('hermes-tool-error-evidence', { body: JSON.stringify(evidence), contentType: 'application/json' });
  }
});
