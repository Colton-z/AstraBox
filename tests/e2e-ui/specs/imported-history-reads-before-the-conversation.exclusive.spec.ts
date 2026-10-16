/**
 * A migrated conversation's earlier messages are imported for reading only.
 * They run nothing and change no session state, keep their tool work, images,
 * source identity and an unrecorded ending as such, and precede the
 * conversation's own messages. Re-running the import changes nothing; other
 * content under a used source id, or history after the conversation began,
 * is refused.
 *
 * An imported user message is an input no runtime is handed (`source`
 * `import`, state `imported`); an imported reply is a response that ended
 * before anything ran (`origin` `import`). History names where each came
 * from in `metadata.history_import`.
 */
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageParts, messageText, messageToolCalls } from '../fixtures/astraApi';
import {
  postgresRows,
  sessionInputRows,
  sessionResponseRows,
  sqlLiteral,
} from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

// The image a migrated user message carried, with no caption: one red pixel.
const PIXEL_PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

async function importHistory(request: APIRequestContext, sessionId: string, messages: unknown[]) {
  const response = await request.post(apiPath(`/admin/sessions/${sessionId}/history-import`), {
    data: { messages },
  });
  return { status: response.status(), body: await response.json() };
}

test('imported history reads before the conversation and runs nothing', async ({ page, request }) => {
  const api = new AstraApi(request);
  const id = randomUUID().slice(0, 8);
  agentId = (await api.createColdTestAgent(`__e2e_history_import_${id}`)).agent_id;
  const created = await api.startConversation(agentId);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const before = await api.waitForSessionReady(sessionId);

  const history = [
    {
      source_id: `${id}-u1`, role: 'user', created_at: '2026-01-05T09:00:00Z',
      blocks: [{ type: 'text', text: `IMPORTED_QUESTION_${id}` }],
    },
    {
      source_id: `${id}-a1`, role: 'assistant', created_at: '2026-01-05T09:00:20Z', completion: 'complete',
      blocks: [
        { type: 'tool_use', id: `toolu_${id}`, name: 'Bash', input: { command: 'ls' } },
        { type: 'tool_result', tool_use_id: `toolu_${id}`, content: 'README.md', is_error: false },
        { type: 'text', text: `IMPORTED_ANSWER_${id}` },
      ],
    },
    {
      source_id: `${id}-u2`, role: 'user', created_at: '2026-01-05T09:01:00Z',
      blocks: [{ type: 'image', source: { type: 'base64', media_type: 'image/png', data: PIXEL_PNG } }],
    },
    {
      source_id: `${id}-a2`, role: 'assistant', created_at: '2026-01-05T09:01:30Z', completion: 'unknown',
      parent_source_id: `${id}-u2`,
      blocks: [{ type: 'text', text: `IMPORTED_PARTIAL_${id}` }],
    },
  ];

  expect(await importHistory(request, sessionId, history))
    .toMatchObject({ status: 200, body: { data: { imported: 4, unchanged: 0 } } });
  // A re-run is a no-op; a used source id with other content is refused.
  expect(await importHistory(request, sessionId, history))
    .toMatchObject({ status: 200, body: { data: { imported: 0, unchanged: 4 } } });
  const changed = await importHistory(request, sessionId, [
    { ...history[0], blocks: [{ type: 'text', text: 'something else' }] },
  ]);
  expect(changed.status).toBe(409);
  expect(changed.body.code).toBe('HISTORY_IMPORT_CONFLICT');

  // Importing ran nothing: the same box, state and session set, and no input
  // or response a runtime took part in.
  const after = await api.waitForSessionReady(sessionId);
  expect(after.sandbox_id).toBe(before.sandbox_id);
  expect(after.state).toBe(before.state);
  expect(postgresRows<{ session_id: string }>(
    `SELECT session_id FROM sessions WHERE agent_id = ${sqlLiteral(agentId)}`,
  ).map((row) => row.session_id)).toEqual([sessionId]);
  expect(
    sessionInputRows(sessionId).map((row) => [row.source, row.state]),
    'an imported user message is never handed to a runtime',
  ).toEqual([['import', 'imported'], ['import', 'imported']]);
  expect(
    sessionResponseRows(sessionId).map((row) => [row.origin, row.status, row.outcome]),
    'an imported reply ended before anything ran; an unrecorded ending stays unrecorded',
  ).toEqual([['import', 'ended', 'completed'], ['import', 'ended', null]]);

  // The read model carries each message with its source identity and completion.
  const page1 = await api.getMessages(sessionId, 50);
  const imported = page1.messages.filter((message) => message.metadata?.history_import);
  expect(imported.map((message) => [message.role, message.metadata?.history_import])).toEqual([
    ['user', { source_id: `${id}-u1` }],
    ['assistant', { source_id: `${id}-a1`, completion: 'complete' }],
    ['user', { source_id: `${id}-u2` }],
    ['assistant', { source_id: `${id}-a2`, completion: 'unknown', parent_source_id: `${id}-u2` }],
  ]);
  // A user message keeps the source's time as its `created_at`, a reply as
  // its `started_at`.
  expect(imported.map((message) => Date.parse(String(
    message.role === 'user' ? message.metadata?.created_at : message.metadata?.started_at,
  )))).toEqual(history.map((message) => Date.parse(message.created_at)));
  expect(
    messageToolCalls(imported[1]).map((call) => [call.toolName, call.state, call.input, call.output]),
    'the imported reply keeps its tool call and what it returned',
  ).toEqual([['Bash', 'output-available', { command: 'ls' }, 'README.md']]);
  expect(messageText(imported[1])).toBe(`IMPORTED_ANSWER_${id}`);
  expect(messageText(imported[2])).toBe('');
  expect(messageParts(imported[2], 'file'), 'the captionless message is its picture').toEqual([
    expect.objectContaining({ mediaType: 'image/png', url: `data:image/png;base64,${PIXEL_PNG}` }),
  ]);

  // The browser shows it as history: the picture, the answer, and the reply
  // whose ending was never recorded, marked as such and nothing more.
  await openSessionView(page, sessionId);
  const users = page.getByTestId('user-message');
  const assistants = page.getByTestId('assistant-message');
  await expect(users.first()).toContainText(`IMPORTED_QUESTION_${id}`);
  await expect(users.nth(1).locator('img')).toHaveCount(1);
  await expect(assistants.first()).toContainText(`IMPORTED_ANSWER_${id}`);
  await expect(assistants.nth(1)).toContainText(`IMPORTED_PARTIAL_${id}`);
  await expect(assistants.nth(1).getByTestId('history-no-recorded-ending')).toBeVisible();
  await expect(assistants.first().getByTestId('history-no-recorded-ending')).toHaveCount(0);

  // The conversation continues after its history.
  const assistantsBefore = await api.assistantCount(sessionId);
  await sendPrompt(page, sessionId, `Reply exactly LIVE_${id}. Do not use tools.`);
  const reply = await api.waitForAssistantMessageMatching(
    sessionId, assistantsBefore, (message) => messageText(message).includes(`LIVE_${id}`), 120_000,
  );
  expect(reply.metadata?.history_import).toBeUndefined();
  await expect(assistants.last()).toContainText(`LIVE_${id}`);
  await expect(assistants).toHaveCount(3);
  const ordered = (await api.getMessages(sessionId, 50)).messages.map((message) => (
    (message.metadata?.history_import as { source_id?: string } | undefined)?.source_id ?? 'live'
  ));
  expect(ordered.slice(0, 4)).toEqual([`${id}-u1`, `${id}-a1`, `${id}-u2`, `${id}-a2`]);
  expect(ordered.slice(4).every((source) => source === 'live')).toBe(true);

  // History cannot be placed before messages the conversation already ordered.
  const late = await importHistory(request, sessionId, [
    ...history,
    { source_id: `${id}-u3`, role: 'user', created_at: '2026-01-05T09:02:00Z', blocks: [{ type: 'text', text: 'late' }] },
  ]);
  expect(late.status).toBe(409);
  expect(late.body.code).toBe('HISTORY_IMPORT_AFTER_CONVERSATION');
  expect(await importHistory(request, sessionId, history))
    .toMatchObject({ status: 200, body: { data: { imported: 0, unchanged: 4 } } });
});
