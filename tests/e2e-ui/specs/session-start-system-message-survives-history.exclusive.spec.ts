/** Native startup output and a queued first input keep one order on every read. */
import { execFileSync } from 'node:child_process';
import { expect, test, type Page } from '@playwright/test';

import { AstraApi, messageText, sseFrames, type MessageRecord } from '../fixtures/astraApi';
import { nativeTranscriptRows, postgresTextRows, sessionInputRows } from '../fixtures/dbOracle';
import { absoluteBaseUrl, apiPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { restartServerContainer } from '../fixtures/sandboxOps';
import { POSTGRES_CONTAINER_HANDLE, SERVER_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
  agentId = '';
});

async function visibleOrder(
  page: Page,
  expected: readonly { role: 'user' | 'assistant'; text: string }[],
): Promise<void> {
  const rows = page.locator('[data-testid="user-message"], [data-testid="assistant-message"]');
  await expect.poll(async () => {
    const messages = await rows.evaluateAll((elements) => elements.map((element) => ({
      role: element.getAttribute('data-testid'), text: element.textContent || '',
    })));
    return expected.map((item) => messages.findIndex((message) => (
      message.role === `${item.role}-message` && message.text.includes(item.text)
    )));
  }, { timeout: 30_000 }).toEqual(expected.map((_item, index) => index));
  expect(await rows.count(), 'no duplicate or invented message rows').toBe(expected.length);
}

test('SessionStart systemMessage appears once before a turn and after history reload', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const welcome = `SESSION_START_WELCOME_${runId}`;
  const answer = `NATIVE_ORDER_REPLY_${runId}`;
  const prompt = `Reply with exactly ${answer}. Do not use tools.`;
  const hookReceipt = `/workspace/.astrabox-e2e-hook-${runId}`;
  const hookCommand = [
    "python3 -c 'from pathlib import Path; import json,time; time.sleep(6); ",
    `Path(${JSON.stringify(hookReceipt)}).write_text(json.dumps({"welcome": ${JSON.stringify(welcome)}, "completed_at": time.time()})); `,
    `print(json.dumps({"systemMessage": ${JSON.stringify(welcome)}}))'`,
  ].join('');
  const agent = await api.createColdTestAgent(`e2e-session-start-${runId}`);
  agentId = agent.agent_id;
  await api.updateAgent(agentId, {
    name: agent.name,
    model: agent.model,
    runtime_profile_name: agent.runtime_profile_name,
    version: agent.version,
    engine_options: { sdk_options: {
      include_hook_events: true,
      settings: { hooks: { SessionStart: [{
        matcher: 'startup',
        hooks: [{ type: 'command', command: hookCommand, timeout: 10 }],
      }] } },
    } },
  });
  const sessionId = (await api.startConversation(agentId)).session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  await openSessionView(page, sessionId);
  // Retain the first rendered order. A later history refresh must not hide
  // an initially wrong live placement of the queued input.
  await page.evaluate(({ welcome, prompt }) => {
    const state = window as Window & { nativeFirstOrder?: string[] };
    const observer = new MutationObserver(() => {
      const texts = [...document.querySelectorAll(
        '[data-testid="user-message"], [data-testid="assistant-message"]',
      )].map((element) => element.textContent || '');
      if (texts.some((text) => text.includes(welcome)) && texts.some((text) => text.includes(prompt))) {
        state.nativeFirstOrder = texts.filter((text) => text.includes(welcome) || text.includes(prompt))
          .map((text) => text.includes(welcome) ? 'welcome' : 'input');
        observer.disconnect();
      }
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  }, { welcome, prompt });
  const accepted = await sendPrompt(page, sessionId, prompt);
  expect((await api.getMessages(sessionId)).messages.some((message) => (
    messageText(message).includes(welcome)
  )), 'the first input must be queued before the hook publishes its welcome').toBe(false);
  const acceptance = await accepted.json() as { data: { input_id: string } };
  const inputId = acceptance.data.input_id;
  expect(inputId).not.toEqual('');
  await api.waitForAssistantMessageMatching(sessionId, 0, (message) => (
    message.metadata?.status === 'ended' && message.metadata?.outcome === 'completed'
    && messageText(message).includes(answer)
  ), 60_000);
  await api.waitForSessionReady(sessionId);
  const expectedMessages = [
    { role: 'assistant', text: welcome },
    { role: 'user', text: prompt },
    { role: 'assistant', text: answer },
  ] as const;
  const expected = expectedMessages.map((message) => message.text);
  await visibleOrder(page, expectedMessages);
  const firstOrder = await page.evaluate(() => (window as Window & { nativeFirstOrder?: string[] }).nativeFirstOrder);
  expect(firstOrder, 'the first live render must already follow native order').toEqual(['welcome', 'input']);
  await expect(page.getByText(welcome, { exact: true })).toHaveCount(1);
  await expect(page.getByText('SessionStart:startup', { exact: false })).toHaveCount(0);

  const history = await api.getMessages(sessionId);
  expect(history.messages.map((message) => message.role)).toEqual(['assistant', 'user', 'assistant']);
  expect(history.messages.map(messageText).map((text) => text.trim())).toEqual(expected);
  const greeting = history.messages[0]!;
  expect(greeting.metadata).toMatchObject({ origin: 'engine' });
  expect(history.messages[1]!.metadata?.input_id).toBe(inputId);
  // Acceptance clocks remain facts; the display must not sort on these clocks.
  expect(Date.parse(history.messages[1]!.metadata!.created_at!))
    .toBeLessThan(Date.parse(greeting.metadata!.started_at!));
  const input = sessionInputRows(sessionId).find((row) => row.input_id === inputId)!;
  expect(input.state).toBe('admitted');
  expect(Date.parse(input.admitted_at!)).toBeGreaterThanOrEqual(Date.parse(greeting.metadata!.ended_at!));

  const terminal = sseFrames(await api.runTerminalCommand(
    sessionId, `cat ${hookReceipt}`, '/workspace',
  ));
  expect(terminal.filter((frame) => frame.type === 'exit').map((frame) => frame.exit_code)).toEqual([0]);
  const receipt = JSON.parse(terminal.filter((frame) => frame.type === 'stdout')
    .map((frame) => String(frame.text ?? '')).join('').trim()) as { welcome: string; completed_at: number };
  expect(receipt.welcome).toBe(welcome);
  // The supplier's JSONL is independent of the browser/output projection.
  // Hook systemMessage is an SDK event, not a user/assistant JSONL entry.
  await api.adminSessionTranscript(sessionId);
  const nativeBefore = nativeTranscriptRows(sessionId).filter((row) => !row.subpath);
  const native = nativeBefore.map((row) => JSON.parse(row.entry_json) as {
      type?: string; timestamp?: string; message?: { role?: string; content?: unknown };
    });
  const userIndex = native.findIndex((entry) => entry.type === 'user'
    && JSON.stringify(entry.message?.content ?? '').includes(prompt));
  const replyIndex = native.findIndex((entry) => entry.type === 'assistant'
    && JSON.stringify(entry.message?.content ?? '').includes(answer));
  expect(userIndex, 'the exact submitted input exists in native JSONL').toBeGreaterThanOrEqual(0);
  expect(replyIndex, 'the native answer follows that input').toBeGreaterThan(userIndex);
  expect(Date.parse(native[userIndex]!.timestamp!)).toBeGreaterThanOrEqual(receipt.completed_at * 1000);
  await test.info().attach('native-startup-order.json', {
    body: JSON.stringify({ receipt, input, native, history, firstOrder }, null, 2), contentType: 'application/json',
  });

  const replay = await api.resumeStream(sessionId, 0, 30_000);
  const start = sseFrames(replay.raw).find((frame) => frame.type === 'start');
  expect(start?.messageId).toBe(greeting.id);
  expect(start?.messageMetadata).toMatchObject({ origin: 'engine', input_ids: [] });
  expect(replay.errorText).toBeNull();
  expect(replay.text.trim()).toBe(welcome);

  const paged: MessageRecord[] = [];
  let before: string | null = null;
  do {
    const next = await api.getHistoryBlocks(sessionId, 1, before);
    paged.unshift(...next.messages);
    before = next.next_cursor || null;
  } while (before);
  expect(paged.map((message) => message.id)).toEqual(history.messages.map((message) => message.id));
  expect(new Set(paged.map((message) => message.id)).size).toBe(3);
  // The uncollapsed history endpoint has its own cursor encoding.
  const rawPaged: MessageRecord[] = [];
  before = null;
  do {
    const suffix = before ? `&before=${encodeURIComponent(before)}` : '';
    const response = await request.get(apiPath(`/sessions/${sessionId}/messages?limit=1${suffix}`));
    expect(response.ok()).toBe(true);
    const next = (await response.json()).data as { messages: MessageRecord[]; before?: string | null };
    rawPaged.unshift(...next.messages);
    before = next.before || null;
  } while (before);
  expect(rawPaged.map((message) => message.id)).toEqual(history.messages.map((message) => message.id));

  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await visibleOrder(page, expectedMessages);
  // Retain the real messages/output/native store and exercise the shipped
  // upgrade on the previous schema's projection. No source records are seeded
  // or rewritten: a missing backfill must reproduce the old wrong order.
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const postgres = requireServiceContainer(POSTGRES_CONTAINER_HANDLE);
  const database = process.env.ASTRABOX_E2E_POSTGRES_DB;
  const databaseUser = process.env.ASTRABOX_E2E_POSTGRES_USER;
  if (!database || !databaseUser) throw new Error('the selected upgrade case needs explicit isolated database handles');
  expect(postgresTextRows(postgres, database, databaseUser,
    'SELECT version_num FROM alembic_version')).toEqual(['0012']);
  execFileSync('docker', ['stop', '--time', '10', server], { timeout: 20_000 });
  // Back to the schema before 0011, so the upgrade replays 0011's backfill;
  // the later revisions' columns go too, as they did not exist then.
  expect(postgresTextRows(postgres, database, databaseUser, `
    BEGIN;
    ALTER TABLE assistants DROP COLUMN proactive;
    ALTER TABLE session_inputs DROP COLUMN conversation_order;
    ALTER TABLE session_responses DROP COLUMN conversation_order;
    UPDATE alembic_version SET version_num = '0010' WHERE version_num = '0012';
    COMMIT;
    SELECT version_num FROM alembic_version;
  `)).toEqual(['0010']);
  await restartServerContainer(absoluteBaseUrl(), 30_000, server);
  expect(postgresTextRows(postgres, database, databaseUser,
    'SELECT version_num FROM alembic_version')).toEqual(['0012']);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await visibleOrder(page, expectedMessages);
  const afterTakeover = await api.getMessages(sessionId);
  expect(afterTakeover.messages.map((message) => message.id)).toEqual(history.messages.map((message) => message.id));
  expect(afterTakeover.messages.map(messageText).map((text) => text.trim())).toEqual(expected);
  expect(afterTakeover.messages.map((message) => ({
    id: message.id, role: message.role, parts: message.parts,
    created_at: message.metadata?.created_at,
    started_at: message.metadata?.started_at, ended_at: message.metadata?.ended_at,
  }))).toEqual(history.messages.map((message) => ({
    id: message.id, role: message.role, parts: message.parts,
    created_at: message.metadata?.created_at,
    started_at: message.metadata?.started_at, ended_at: message.metadata?.ended_at,
  })));
  const nextAnswer = `AFTER_READER_TAKEOVER_${runId}`;
  const nextPrompt = `Reply with exactly ${nextAnswer}. Do not use tools.`;
  await sendPrompt(page, sessionId, nextPrompt);
  await api.waitForAssistantMessageMatching(sessionId, 2, (message) => (
    message.metadata?.status === 'ended' && message.metadata?.outcome === 'completed'
    && messageText(message).includes(nextAnswer)
  ), 60_000);
  await api.waitForSessionReady(sessionId);
  await api.adminSessionTranscript(sessionId);
  const nativeAfter = nativeTranscriptRows(sessionId).filter((row) => !row.subpath);
  expect(nativeAfter.slice(0, nativeBefore.length).map((row) => row.entry_json))
    .toEqual(nativeBefore.map((row) => row.entry_json));
  const finalHistory = await api.getMessages(sessionId);
  expect(finalHistory.messages.map(messageText).map((text) => text.trim()))
    .toEqual([...expected, nextPrompt, nextAnswer]);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await visibleOrder(page, [
    ...expectedMessages, { role: 'user', text: nextPrompt }, { role: 'assistant', text: nextAnswer },
  ]);
});
