/**
 * E2E: a conversation keeps the title its owner gives it.
 *
 * Owners rename conversations because the generated title does not say what
 * the conversation is about. The journey renames one from the conversation
 * list, lets the conversation answer a message that would be titled, renames
 * it again from its header, and reloads. What the owner must see at each step:
 * the list, the header and the browser tab carry the new title without a
 * reload, the list keeps its order, and no generated title takes its place.
 *
 * WHY A CONTROL CONVERSATION. A title the title service declines to write
 * leaves no record, so "the generated title never arrived" is read against a
 * second conversation that was not renamed: the same prompt, sent at the same
 * time, to the same Agent. Its generated title is the proof that this
 * deployment titles that prompt, and the renamed conversation's decision needs
 * no model call, so it is made before the control's title can exist.
 *
 * WHY THE HEADER IS CHECKED AFTER A LIST RENAME. The open page reads its header
 * from its own copy of the session, which the rename's session notice tells it
 * to read again; the list's copy is a different read. A rename that updated
 * only the list would leave a stale header beside a fresh row.
 *
 * Every oracle is scoped to this spec's two conversations, so other workers'
 * conversations in the same user's list can neither satisfy nor break it.
 */
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('a conversation keeps the title its owner gives it, from the list and from its header', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const agent = await api.defaultAgent();
  // One at a time: a deployment may admit a single box creation at once and
  // refuses the second rather than queueing it.
  const renamed = await api.startConversation(String(agent.agent_id));
  sessions.push(renamed.session_id);
  const sessionId = renamed.session_id;
  await api.waitForSessionReady(sessionId);
  const control = await api.startConversation(String(agent.agent_id));
  sessions.push(control.session_id);
  await api.waitForSessionReady(control.session_id);

  const row = page.locator(`[data-testid="session-row"][data-session-id="${sessionId}"]`);
  const header = page.getByTestId('run-view').locator('header h1');
  const renameResponse = () => page.waitForResponse((response) => response.request().method() === 'PATCH'
    && new URL(response.url()).pathname === apiPath(`/sessions/${sessionId}`));

  await openSessionView(page, sessionId);
  await expect(row).toBeVisible({ timeout: 30_000 });
  const before = await api.getSession(sessionId);

  // ── rename from the list, before any message ───────────────────────────────
  await row.hover();
  await row.getByRole('button', { name: 'More actions', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Rename', exact: true }).click();
  const field = row.getByRole('textbox', { name: 'Conversation title', exact: true });
  await expect(field, 'the field opens focused, in place of the row').toBeFocused();
  const listTitle = `Release checklist ${runId}`;
  // A title is one line: the runs of spaces collapse as the server stores them.
  await field.fill(`  Release   checklist ${runId} `);
  const listRename = renameResponse();
  await field.press('Enter');
  const listRenamed = await listRename;
  expect(listRenamed.status()).toBe(200);
  expect((await listRenamed.json()).data).toEqual({ session_id: sessionId, title: listTitle });

  await expect(row.getByText(listTitle, { exact: true }), 'the row shows the new title').toBeVisible();
  await expect(header, 'the open header follows a rename made in the list').toHaveText(listTitle, {
    timeout: 30_000,
  });
  await expect(page).toHaveTitle(`${listTitle} · AstraBox`);
  const stored = await api.adminSessionDetail(sessionId);
  expect({ title: stored.title, source: stored.title_source }).toEqual({ title: listTitle, source: 'user' });
  expect(
    (await api.getSession(sessionId)).updated_at,
    'renaming is not activity, so the list keeps its order',
  ).toBe(before.updated_at);

  // ── the same titleable prompt to both conversations ────────────────────────
  const prompt = [
    `请帮我制定一个 PostgreSQL 慢查询索引优化方案，测试编号 ${runId}。`,
    '不要使用任何工具。',
    '请用一句简短中文回答。',
  ].join('\n');
  const renamedCount = await api.assistantCount(sessionId);
  const controlCount = await api.assistantCount(control.session_id);
  await Promise.all([
    sendPrompt(page, sessionId, prompt),
    api.postTurnInput(control.session_id, prompt),
  ]);
  // A reply is listed while it is still being written, and a title is offered
  // only once its response has ended, which is when the Session reads READY again.
  await Promise.all([
    api.waitForAssistantMessageCount(sessionId, renamedCount, 180_000),
    api.waitForAssistantMessageCount(control.session_id, controlCount, 180_000),
  ]);
  await Promise.all([api.waitForSessionReady(sessionId), api.waitForSessionReady(control.session_id)]);
  await expect.poll(async () => {
    const detail = await api.adminSessionDetail(control.session_id);
    return { source: detail.title_source, status: detail.title_generation_status };
  }, {
    timeout: 60_000,
    intervals: [250, 500, 1_000],
    message: 'the control conversation must receive a generated title for the same prompt',
  }).toEqual({ source: 'generated', status: 'completed' });
  const answered = await api.adminSessionDetail(sessionId);
  expect(
    { title: answered.title, source: answered.title_source, status: answered.title_generation_status ?? null },
    'a response does not replace the title its owner gave the conversation',
  ).toEqual({ title: listTitle, source: 'user', status: null });
  await expect(header).toHaveText(listTitle);

  // ── rename from the header ──────────────────────────────────────────────────
  await header.getByRole('button').click();
  const headerField = page.getByTestId('run-view').getByRole('textbox', { name: 'Conversation title', exact: true });
  await expect(headerField).toBeFocused();
  const headerTitle = `Index tuning plan ${runId}`;
  await headerField.fill(headerTitle);
  const headerRename = renameResponse();
  await headerField.press('Enter');
  expect((await headerRename).status()).toBe(200);
  await expect(header).toHaveText(headerTitle);
  await expect(
    row.getByText(headerTitle, { exact: true }),
    'the list follows a rename made in the header',
  ).toBeVisible({ timeout: 30_000 });

  // ── Escape keeps the title ──────────────────────────────────────────────────
  await header.getByRole('button').click();
  await headerField.fill('discarded');
  await headerField.press('Escape');
  await expect(header).toHaveText(headerTitle);

  // ── the title is the stored one after a reload ─────────────────────────────
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(header).toHaveText(headerTitle, { timeout: 60_000 });
  await expect(row.getByText(headerTitle, { exact: true })).toBeVisible({ timeout: 30_000 });
  expect((await api.getSession(sessionId)).title).toBe(headerTitle);
});
