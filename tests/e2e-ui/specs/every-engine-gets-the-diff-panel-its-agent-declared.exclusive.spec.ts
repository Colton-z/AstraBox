/** The Diff panel is what the Agent declared it would be, for every engine. */
import { expect, test, type Page } from '@playwright/test';

import { AstraApi, visibleMessages } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { engineCases, engineProfileFor, toolName } from '../fixtures/engineProfile';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
let sessionId = '';
let releaseHistory = () => {};
const observations: unknown[] = [];

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

async function observe(read: () => unknown | Promise<unknown>): Promise<unknown> {
  try { return await read(); }
  catch (error) { return { unavailable: String(error) }; }
}

test.afterEach(async ({ request, page }, info) => {
  releaseHistory();
  if (!['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  const api = new AstraApi(request);
  const [session, history, stream] = await Promise.all([
    observe(() => api.getSession(sessionId)),
    observe(() => api.getMessages(sessionId, 50)),
    observe(() => aiStreamBodies(page)),
  ]);
  await info.attach('diff-panel-scene', {
    body: JSON.stringify({ sessionId, observations, session, history, stream }),
    contentType: 'application/json',
  });
});

async function workspacePath(api: AstraApi, session: string, name: string): Promise<string> {
  const detail = record(await api.adminSessionDetail(session));
  const identity = record(detail.runtime_identity);
  const root = String(identity.workspace_dir || '').replace(/\/+$/, '');
  expect(root, `session ${session} reports no workspace_dir`).not.toBe('');
  return `${root}/${name}`;
}

function toolCards(page: Page) {
  // The card root carries the call's own id. A collapsible-shaped locator would
  // also match the turn fold and the process group the card now sits inside.
  return page.getByTestId('run-view').getByTestId('assistant-message')
    .locator('[data-tool-call-id]');
}

async function expectLiveToolCard(page: Page, marker: string): Promise<void> {
  const chunks = (await aiStreamBodies(page)).flatMap((body) => body.text.split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trim())
    .filter((line) => line && line !== '[DONE]')
    .map((line) => record(JSON.parse(line))));
  const calls = chunks.filter((chunk) => chunk.type === 'tool-input-available'
    && JSON.stringify(chunk.input).includes(marker));
  expect(calls.length, 'the actual write must arrive on the browser stream as a tool call').toBeGreaterThan(0);
  for (const call of calls) {
    expect(call.dynamic, `${String(call.toolName)} uses the runtime-discovered tool UI`).toBe(true);
    expect(chunks.some((chunk) => chunk.type === 'tool-output-available'
      && chunk.toolCallId === call.toolCallId), 'the same native call must have its completed result').toBe(true);
  }
  await expectCompletedToolCard(page, marker);
}

async function expectCompletedToolCard(page: Page, marker: string): Promise<void> {
  // A settled response's tool work folds behind one header, and on a reloaded
  // page the cards it stands for are fetched only when that header is opened.
  // Everything below is about the cards, so the fold is opened first.
  await expect(
    page.getByTestId('assistant-turn-process').first(),
    'the write response must fold into a header the reader can open',
  ).toBeVisible({ timeout: 60_000 });
  await revealAssistantProcess(page);
  const cards = toolCards(page);
  await expect(cards.first()).toBeVisible();
  let matched = false;
  // The runtime may legitimately inspect a directory before writing. The DOM
  // exposes no toolCallId, so match the real call's unique input, not its order.
  for (let index = 0; index < await cards.count(); index += 1) {
    const card = cards.nth(index);
    const header = card.locator('[data-slot="collapsible-trigger"]').first();
    const wasOpen = await header.getAttribute('aria-expanded') === 'true';
    if (!wasOpen) await header.click();
    const panel = card.locator('[data-slot="collapsible-content"]');
    await expect(panel).toBeVisible();
    if ((await panel.innerText()).includes(marker)) {
      await expect(header.locator('[data-slot="badge"]').first()).toHaveText(/^(Done|已完成)$/);
      matched = true;
    }
    if (!wasOpen) await header.click();
    if (matched) break;
  }
  expect(matched, 'a completed visible card must contain the actual write call input').toBe(true);
}

async function expectOrdinaryReplyAndColdCards(page: Page, api: AstraApi, session: string, marker: string): Promise<void> {
  const before = new Set(visibleMessages(await api.getMessages(session, 50)).map((message) => message.message_id));
  // Counted from what the reader can reach: a folded header holds its cards out
  // of the page entirely once the transcript has been rebuilt from history.
  await revealAssistantProcess(page);
  const count = await toolCards(page).count();
  expect(count, 'the conversation already contains rendered tool cards').toBeGreaterThan(0);
  await sendPrompt(page, session, 'The file work is finished. What does a text file store? Answer in one short plain sentence without Markdown. Do not use tools.');
  const readReplyText = async () => visibleMessages(await api.getMessages(session, 50))
      .filter((message) => message.role === 'assistant' && !before.has(message.message_id))
      .map((message) => (message.blocks ?? []).filter((block) => block.type === 'text')
        .map((block) => String(block.text ?? '')).join('\n').trim())
      .find((text) => text.length > 0) ?? '';
  await expect.poll(async () => (await readReplyText()).length, { timeout: 60_000 }).toBeGreaterThan(0);
  await expect.poll(async () => record(await api.getSession(session)).state, { timeout: 60_000 }).toBe('READY');
  const replyText = await readReplyText();
  expect(replyText, 'the completed next answer must have durable text').not.toBe('');
  await expect(page.getByTestId('assistant-message').last()).toContainText(replyText);
  await revealAssistantProcess(page);
  await expect(toolCards(page), 'an ordinary reply must not reuse or fabricate a tool card').toHaveCount(count);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await revealAssistantProcess(page);
  await expect(toolCards(page), 'the same completed cards survive database history reload').toHaveCount(count);
  await expect(page.getByTestId('assistant-message').last()).toContainText(replyText);
  await expectCompletedToolCard(page, marker);
}

// Every engine that can write at all. `tools.write` is the engine's own name
// for it — Claude's `Write`, Codex's `commandExecution`, the harness's `write`.
const writing = engineCases()
  .filter((profile) => Array.isArray(profile.tools?.write) && profile.tools.write.length > 0);

for (const declared of writing) {
  const engine = declared.engine_kind;

  test(`${engine} gets the diff panel its Agent declared, over a write it really made`, async ({ page, request }) => {
    const profile = engineProfileFor(engine);
    const api = new AstraApi(request);
    const runId = Date.now();
    const fileName = `diff-${engine}-${runId}.txt`;
    const contentMarker = `DIFFMARK-${engine.toUpperCase()}-${runId}`;
    sessionId = (await api.startConversation(profile.agent_id)).session_id;
    sessions.push(sessionId);
    test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
    test.info().annotations.push({ type: 'engine_kind', description: engine });
    await api.waitForSessionReady(sessionId);

    // The conversation keeps the unattended mode it was created with, so the
    // write RUNS rather than parking on an approval. This spec is about what a
    // settled write renders, and a gate in front of it would never get there.
    const target = await workspacePath(api, sessionId, fileName);
    // The session detail as THIS TAB received it. The panel counts from the
    // copy the page holds, so a declaration that reaches a fresh API call but
    // not this one is indistinguishable from outside without recording it.
    const pageSessions: Array<Record<string, unknown>> = [];
    page.on('response', (response) => {
      if (!response.url().includes(`/sessions/${sessionId}`)) return;
      if (response.request().method() !== 'GET') return;
      void response
        .json()
        .then((body) => {
          const detail = record(record(body).data ?? body);
          pageSessions.push({
            state: detail.state,
            workspace_panels: detail.workspace_panels,
          });
        })
        .catch(() => undefined);
    });
    await mirrorSseBodies(page);
    // Prove the first card came from live chunks, not a history refresh that
    // reconstructs a dynamic tool and conceals a broken streaming envelope.
    let holdHistory = false;
    let initialHistoryReads = 0;
    const historyHold = new Promise<void>((resolve) => {
      releaseHistory = () => { holdHistory = false; resolve(); };
    });
    await page.route((url) => url.pathname.endsWith(`/sessions/${sessionId}/history-blocks`), async (route) => {
      if (holdHistory) {
        await historyHold;
        await route.continue();
      } else {
        const response = await route.fetch();
        expect(response.status()).toBe(200);
        await route.fulfill({ response });
        initialHistoryReads += 1;
      }
    });
    await openSessionView(page, sessionId);
    await expect.poll(() => initialHistoryReads).toBeGreaterThan(0);
    holdHistory = true;
    await sendPrompt(page, sessionId, [
      `E2E diff ${engine} ${runId}.`,
      engine === 'codex'
        ? `Use Codex's apply_patch capability to create a file at ${target}`
        : `Use the ${toolName(profile, 'write')} tool to create a file at ${target}`,
      `containing exactly ${contentMarker}.`,
      'Create only that one file, then say you are done.',
      engine === 'codex'
        ? [
          'Run exactly this command through your shell tool. apply_patch takes the patch on stdin, not a filename argument:',
          "apply_patch <<'PATCH'",
          '*** Begin Patch',
          `*** Add File: ${target}`,
          `+${contentMarker}`,
          '*** End Patch',
          'PATCH',
          'Do not use another write mechanism or inspect the tool executable. Do not ask for confirmation.',
        ].join('\n')
        : 'Do not ask for confirmation and do not use any other tool.',
    ].join('\n'));

    // The engine really wrote it. Asserting this first keeps the two failures
    // apart: a failed write is not evidence of a missing Diff projection.
    await expect
      .poll(async () => {
        const body = await observe(() => api.downloadFileText(sessionId, target));
        return typeof body === 'string' && body.includes(contentMarker);
      }, { timeout: 150_000 })
      .toBe(true);
    observations.push({ engine, target, wrote: true });

    // What the panel owes is what the Agent declared, not what one engine's
    // tool names happen to be. An Agent that declared no diff view must not be
    // offered the tab at all; one that declared it must count the write.
    const declared = record(record(await api.getSession(sessionId)).workspace_panels);
    observations.push({ engine, declared });
    // The write landing in the sandbox is not the turn ending. The panel counts
    // from the tool parts this tab has been delivered, so a turn still running
    // is a count that has not finished arriving — a different thing from a
    // count the panel refused to make.
    await expect
      .poll(async () => String(record(await api.getSession(sessionId)).state), { timeout: 150_000 })
      .toBe('READY');
    observations.push({ engine, pageSessions: pageSessions.slice(-4) });
    await expectLiveToolCard(page, contentMarker);
    releaseHistory();
    if (engine === 'codex') {
      await test.info().attach('codex-file-change-source', {
        body: JSON.stringify({
          sessionId, target, declared,
          history: await api.getMessages(sessionId, 50),
          stream: await aiStreamBodies(page),
        }),
        contentType: 'application/json',
      });
    }
    const diffTab = page.getByTestId('run-view').getByRole('tab', { name: /^Diff\b/ });
    if (declared.diff !== true) {
      await expect(
        diffTab,
        `${engine}'s Agent declared no diff panel, so the tab must not be offered`,
      ).toHaveCount(0);
      await expectOrdinaryReplyAndColdCards(page, api, sessionId, contentMarker);
      return;
    }

    await expect(
      diffTab,
      `the Diff tab must count the file ${engine} wrote with its ${toolName(profile, 'write')} tool`,
    ).toHaveText(/^Diff \(1\)$/, { timeout: 60_000 });

    await diffTab.click();
    const diffPanel = page.getByTestId('run-view').locator('[data-slot="tabs-content"]');
    await expect(
      diffPanel.getByText(new RegExp(`${fileName}$`)).last(),
      'the panel must name the file that changed',
    ).toBeVisible({ timeout: 30_000 });

    // Exercise an actual replacement, not just an empty-file creation whose
    // path/count could pass with no native result consumed at all.
    const originalContent = await api.downloadFileText(sessionId, target);
    const replacement = `${contentMarker}-REPLACED`;
    const patch = [
      "apply_patch <<'PATCH'", '*** Begin Patch', `*** Update File: ${target}`,
      '@@', `-${contentMarker}`, `+${replacement}`, '*** End Patch', 'PATCH',
    ].join('\n');
    const editInstruction = engine === 'codex'
      ? `Run exactly this shell command, without other write mechanisms:\n${patch}`
      : engine === 'pi'
        ? `Use edit with ${JSON.stringify({ path: target, edits: [{ oldText: contentMarker, newText: replacement }] })}.`
        : `Use ${engine === 'claude_code' ? 'Edit' : 'edit'} with ${JSON.stringify({ file_path: target, old_string: contentMarker, new_string: replacement })}.`;
    await sendPrompt(page, sessionId, [
      `Replace the marker in ${target}.`, editInstruction,
      'You may read that file first if required. Change no other file; do not ask for confirmation.',
    ].join('\n'));
    await expect.poll(() => api.downloadFileText(sessionId, target), { timeout: 60_000 })
      .toBe(originalContent.replace(contentMarker, replacement));
    const content = page.getByTestId('file-diff-content');
    await expect(content.locator('[data-diff-kind="removed"]')).toContainText(contentMarker);
    await expect(content.locator('[data-diff-kind="added"]')).toContainText(replacement);
    await expect.poll(async () => record(await api.getSession(sessionId)).last_turn_status,
      { timeout: 60_000 }).toBe('COMPLETED');
    const beforeReload = await content.innerText();
    const history = await api.getMessages(sessionId, 50);
    expect(JSON.stringify(history), 'native result projection must be in database-backed history')
      .toContain('data-file-changes');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(diffTab).toHaveText(/^Diff \(1\)$/);
    await diffTab.click();
    await expect(content).toHaveText(beforeReload, { useInnerText: true });
    await expectOrdinaryReplyAndColdCards(page, api, sessionId, contentMarker);
  });
}
