/** A cold snapshot's open questionnaire settles through its cursor, without a reload. */
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { nativeTranscriptRows, postgresRows, sqlLiteral } from '../fixtures/dbOracle';
import { appPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();

test('cold questionnaire answer updates the same live history card and native tool result', async ({ page, request }, testInfo) => {
  const api = new AstraApi(request);
  const marker = `LIVE_FORM_${Date.now()}`;
  const agent = await api.defaultAgent();
  const { session_id: sid } = await api.startConversation(agent.agent_id);
  sessions.push(sid);
  await api.waitForSessionReady(sid);
  await api.setPermissionMode(sid, 'default');
  await page.addInitScript(() => window.localStorage.setItem('astrabox-lang', 'en'));
  await openSessionView(page, sid);
  await sendPrompt(page, sid, [
    'Use exactly two tools in this order, once each. Do not use any other tool.',
    `First Bash: printf '${marker}\\n' >> /workspace/${marker}.txt; cat /workspace/${marker}.txt`,
    `Then AskUserQuestion: ask ${marker}, single-select options YES and NO. Wait for the user.`,
    `After YES reply exactly ${marker}=YES.`,
  ].join('\n'));
  const approval = await api.waitForPendingInteraction(sid);
  expect(approval.tool_name).toBe('Bash');
  await api.approvePendingInteraction(sid, { interactionId: approval.interaction_id });
  await api.waitForSession(sid, (detail) => detail.pending_interaction?.tool_name === 'AskUserQuestion');
  const question = await api.waitForPendingInteraction(sid);
  const iid = question.interaction_id;
  const responseId = String(question.response_id);
  const toolId = String(question.tool_call_id);
  const nativeBefore = nativeTranscriptRows(sid).filter((row) => !row.subpath);
  expect(nativeBefore.length).toBeGreaterThan(0);

  await page.goto('about:blank');
  await api.adminEvictRuntime(sid);
  let release!: () => void;
  let held = false;
  let snapshotSeq = -1;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const streamCursors: number[] = [];
  page.on('request', (outgoing) => {
    const url = new URL(outgoing.url());
    if (url.pathname.endsWith(`/sessions/${sid}/stream`)) {
      streamCursors.push(Number(url.searchParams.get('after_seq')));
    }
  });
  await page.route(`**/sessions/${sid}/history-blocks?*`, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    snapshotSeq = body.data.output_seq;
    held = true;
    await gate;
    await route.fulfill({ response });
  }, { times: 1 });
  await page.goto(appPath(`/sessions/${sid}`), { waitUntil: 'domcontentloaded' });
  await expect.poll(() => held).toBe(true);
  expect(streamCursors, 'the stream must wait for the cold snapshot').toEqual([]);
  expect(snapshotSeq).toBeGreaterThan(0);
  release();
  await expect(page.getByTestId('run-view')).toBeVisible();
  await expect.poll(() => streamCursors.length).toBeGreaterThan(0);
  expect(streamCursors[0]).toBe(snapshotSeq);

  const panel = page.getByTestId('pending-interaction-panel');
  await expect(panel).toContainText(marker);
  await panel.getByRole('radio', { name: /^(?:\d+\.\s*)?YES\b/ }).click();
  const posted = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith(`/interactions/${encodeURIComponent(iid)}/answer`));
  await panel.getByRole('button', { name: 'Submit answer', exact: true }).click();
  expect((await posted).status()).toBe(200);
  await api.waitForSession(sid, (detail) => !detail.pending_interaction
    && detail.last_response?.response_id === responseId
    && detail.last_response.outcome === 'completed');
  await expect(panel).toHaveCount(0);
  await expect(page.getByTestId('assistant-text').filter({ hasText: `${marker}=YES` })).toBeVisible();
  const message = page.getByTestId('assistant-message').filter({ hasText: `${marker}=YES` });
  const process = message.getByTestId('assistant-turn-process-trigger');
  if (await process.count() && await process.getAttribute('aria-expanded') === 'false') await process.click();
  for (const group of await message.getByTestId('assistant-process-trigger').all()) {
    if (await group.getAttribute('aria-expanded') === 'false') await group.click();
  }
  const card = page.locator(`[data-interaction-id="${iid}"]`);
  await expect(card).toHaveCount(1);
  await expect(card).toHaveAttribute('data-interaction-status', 'answered');
  await expect(card.getByRole('radio', { name: /^(?:\d+\.\s*)?YES\b/ })).toBeChecked();
  await expect(card.getByRole('radio', { name: /^(?:\d+\.\s*)?YES\b/ })).toBeDisabled();
  await expect(card.getByRole('radio', { name: /^(?:\d+\.\s*)?NO\b/ })).not.toBeChecked();
  await expect(card).toContainText('This questionnaire is closed and can no longer be submitted.');

  const history = await api.getMessages(sid, 100);
  const reply = history.messages.find((item) => item.id === responseId)!;
  expect(messageText(reply)).toContain(`${marker}=YES`);
  const forms = reply.parts.filter((part) => part.type === 'data-interaction'
    && (part.data as Record<string, unknown>)?.interaction_id === iid);
  expect(forms).toHaveLength(1);
  expect(forms[0].data).toMatchObject({ status: 'answered', tool_call_id: toolId,
    answer: { answers: [{ option_label: 'YES' }] } });
  const nativeAfter = nativeTranscriptRows(sid).filter((row) => !row.subpath);
  expect(nativeAfter.slice(0, nativeBefore.length)).toEqual(nativeBefore);
  const blocks = nativeAfter.flatMap((row) => {
    const entry = JSON.parse(row.entry_json);
    return Array.isArray(entry.message?.content) ? entry.message.content as Array<Record<string, unknown>> : [];
  });
  for (const name of ['Bash', 'AskUserQuestion']) {
    const calls = blocks.filter((part) => part.type === 'tool_use' && part.name === name);
    expect(calls).toHaveLength(1);
    const results = blocks.filter((part) => part.type === 'tool_result' && part.tool_use_id === calls[0].id);
    expect(results).toHaveLength(1);
    expect(results[0].is_error).not.toBe(true);
    expect(JSON.stringify(results[0].content)).toContain(name === 'Bash' ? marker : 'YES');
    if (name === 'AskUserQuestion') expect(calls[0].id).toBe(toolId);
  }
  const output = postgresRows<{ seq: number; part: Record<string, unknown> }>(
    `SELECT seq, part FROM session_output WHERE session_id=${sqlLiteral(sid)} ORDER BY seq`,
  );
  expect(output.map((row) => row.seq)).toEqual(output.map((_, index) => index + 1));
  expect(output.some((row) => row.seq > snapshotSeq && row.part.type === 'data-interaction'
    && row.part.id === iid && (row.part.data as Record<string, unknown>).status === 'answered')).toBe(true);
  await testInfo.attach('live-cold-native-evidence', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
    sid, iid, responseId, toolId, snapshotSeq, streamCursors, history: reply, nativeBefore, nativeAfter, output,
  })) });
});
