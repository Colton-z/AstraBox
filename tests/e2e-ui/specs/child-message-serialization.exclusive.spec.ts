import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';
import { AstraApi } from '../fixtures/astraApi';
import { appendChildFrames } from '../fixtures/childMessageReplay';
import { documentsByField } from '../fixtures/dbOracle';
import { engineProfiles } from '../fixtures/engineProfile';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

const sessions = trackSessions();

test('child WebFetch error cards keep structured results readable after expansion and reload', async ({ page, request }) => {
  const profile = engineProfiles().find((item) => item.engine_kind === 'claude_code');
  expect(profile, 'the deployment must provide the native content-block profile').toBeDefined();
  const api = new AstraApi(request);
  const sessionId = (await api.startConversation(profile!.agent_id)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  const engineRef = `fetch-${randomUUID()}`;
  const frame = (data: Record<string, unknown>) => ({
    type: 'data-subagent', id: `subagent:${randomUUID()}`, transient: true,
    data: { engineKind: 'claude_code', engineRef, ...data },
  });
  const cases = [
    { name: 'WebFetch', content: [{ type: 'text', text: `FETCH_FAILURE_${randomUUID()}` }], is_error: true },
    { name: 'WebSearch', content: `SEARCH_FAILURE_${randomUUID()}`, is_error: true },
    { name: 'Read', content: [{ type: 'text', text: `READ_RESULT_${randomUUID()}` }], is_error: false },
  ];
  // Supplier-supported content shapes, not a reconstruction of the deleted user scene.
  appendChildFrames(sessionId, [
    frame({ kind: 'lifecycle', event: 'closed', engineEvent: 'task_notification',
      engineStatus: 'completed', operations: [], name: 'Tool result rendering' }),
    ...cases.flatMap((item) => {
      const id = `tool-${randomUUID()}`;
      return [
        frame({ kind: 'message', role: 'assistant', content: [
          { type: 'tool_use', id, name: item.name, input: { url: 'https://example.com/' } },
        ] }),
        frame({ kind: 'message', role: 'user', content: [
          { type: 'tool_result', tool_use_id: id, content: item.content, is_error: item.is_error },
        ] }),
      ];
    }),
  ]);
  const children = (await api.listChildRuns(sessionId)).child_runs;
  expect(children).toHaveLength(1);
  const childId = children[0]!.child_run_id;
  const transcript = await api.getChildRunMessages(sessionId, childId);
  expect(transcript.messages.flatMap((message) => message.content)
    .filter((block) => block.type === 'tool_result').map((block) => block.content))
    .toEqual(cases.map((item) => item.content));
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  for (const cold of [false, true]) {
    if (cold) await page.reload({ waitUntil: 'domcontentloaded' });
    else await openSessionView(page, sessionId);
    await page.getByRole('tab', { name: /^Agents/ }).click();
    await page.locator(`[data-child-run-id="${childId}"]`).click();
    const column = page.getByTestId('subagent-transcript-column');
    for (const item of cases) {
      const tool = column.getByRole('button', { name: new RegExp(`^${item.name} `) });
      await expect(tool).toBeVisible();
      await tool.click();
      const text = typeof item.content === 'string' ? item.content : item.content[0]!.text;
      await expect(column).toContainText(text);
      await expect(page.getByTestId('run-view')).toBeVisible();
      expect(pageErrors, 'expanding a supplier result must not crash the page').toEqual([]);
      await tool.click();
    }
    expect((await api.getChildRunMessages(sessionId, childId)).messages).toEqual(transcript.messages);
  }
});

test('Claude persisted SDK and Store tool results remain readable across cold reload', async ({ page, request }) => {
  const profile = engineProfiles().find((item) => item.engine_kind === 'claude_code');
  expect(profile, 'the selected deployment must provide Claude').toBeDefined();
  const api = new AstraApi(request);
  const sessionId = (await api.startConversation(profile!.agent_id)).session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  await api.waitForSessionReady(sessionId);
  const engineRef = `replay-${randomUUID()}`;
  const toolId = `call-${randomUUID()}`;
  const messageId = `subagent:msg:${randomUUID()}`;
  const receipt = `RETAINED_SEARCH_RESULT_${randomUUID()}`;
  const query = 'Xiaomi news';
  const frame = (id: string, data: Record<string, unknown>) => ({
    type: 'data-subagent', id, transient: true,
    data: { engineKind: 'claude_code', engineRef, ...data },
  });
  // Retained user incident 0dc2c7e7, events 53/54: the sole difference was
  // tool_result.is_error null vs absent. Keep both historical encodings;
  // passing only newly normalized producer output would miss broken old reads.
  const result = { type: 'tool_result', tool_use_id: toolId, content: receipt };
  const payloads = [
    frame(`subagent:lifecycle:${randomUUID()}`, {
      kind: 'lifecycle', event: 'closed', engineEvent: 'task_notification',
      engineStatus: 'completed', operations: [], name: 'Retained search',
    }),
    frame(`subagent:msg:${randomUUID()}`, {
      kind: 'message', role: 'assistant',
      content: [{ type: 'tool_use', id: toolId, name: 'WebSearch', input: { query } }],
    }),
    frame(messageId, { kind: 'message', role: 'user', content: [{ ...result, is_error: null }] }),
    frame(messageId, { kind: 'message', role: 'user', content: [result] }),
  ];
  appendChildFrames(sessionId, payloads);
  const stored = documentsByField('session_events', '$.session_id', sessionId)
    .map((row) => row.payload as Record<string, unknown>).filter((payload) => payload?.id === messageId);
  expect(stored, 'the regression must retain both original encodings').toEqual(payloads.slice(2));
  const detail = await request.get(`/api/v1/sessions/${sessionId}`);
  const catalog = await request.get(`/api/v1/sessions/${sessionId}/child-runs`);
  await test.info().attach('retained-child-replay', {
    body: JSON.stringify({ payloads, detail: { status: detail.status(), body: await detail.text() },
      catalog: { status: catalog.status(), body: await catalog.text() } }), contentType: 'application/json',
  });
  expect(detail.status(), 'equivalent child results must not break the parent detail').toBe(200);
  expect(catalog.status(), 'equivalent child results must not be treated as an identity collision').toBe(200);
  const children = (await api.listChildRuns(sessionId)).child_runs;
  expect(children).toHaveLength(1);
  expect(children[0]!.active).toBe(false);
  const childId = children[0]!.child_run_id;
  const transcript = await api.getChildRunMessages(sessionId, childId);
  const results = transcript.messages.flatMap((message) => message.content)
    .filter((block) => block.type === 'tool_result' && block.tool_use_id === toolId);
  expect(results).toHaveLength(1);
  expect(results[0]!.content).toBe(receipt);
  expect(results[0]!.is_error).not.toBe(true);
  for (const cold of [false, true]) {
    if (cold) await page.reload({ waitUntil: 'domcontentloaded' });
    else await openSessionView(page, sessionId);
    await expect(page.getByTestId('run-view')).toBeVisible();
    await page.getByRole('tab', { name: /^Agents/ }).click();
    await page.locator(`[data-child-run-id="${childId}"]`).click();
    const column = page.getByTestId('subagent-transcript-column');
    const tool = column.getByRole('button', { name: /^WebSearch (Done|已完成)$/ });
    await expect(tool).toHaveCount(1);
    if (await tool.getAttribute('aria-expanded') !== 'true') await tool.click();
    await expect(column).toContainText(query);
    await expect(column).toContainText(receipt);
    expect((await api.getChildRunMessages(sessionId, childId)).messages).toEqual(transcript.messages);
  }
  // The same native identity with genuinely different content must still fail.
  appendChildFrames(sessionId, [frame(messageId, {
    kind: 'message', role: 'user', content: [{ ...result, content: 'CONFLICTING_RESULT' }],
  })]);
  const conflict = await request.get(`/api/v1/sessions/${sessionId}/child-runs`);
  expect(conflict.status()).toBe(409);
  expect(await conflict.text()).toContain('CHILD_RUN_PROJECTION_INVALID');
});
