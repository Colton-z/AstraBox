import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';

import {
  AstraApi,
  messageText,
  visibleMessages,
  type SessionRecord,
} from '../fixtures/astraApi';
import { insist } from '../fixtures/insist';
import { openLiveProcessGroup, revealAssistantProcess } from '../fixtures/assistantProcess';
import { trackSessions } from '../fixtures/sessionCleanup';
import { apiPath, parseTimeoutEnv } from '../fixtures/env';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';
import { sessionEvents } from '../fixtures/dbOracle';

const TOOL_START_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TOOL_START_TIMEOUT_MS', 150_000);
const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
const FILE_NOT_FOUND = /404|FILE_NOT_FOUND|path not found/;

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
//
// One tracker for all six tests: it splices its array in each afterEach, so
// a test only ever owns the ids it pushed.
const sessions = trackSessions();

test.describe.configure({ mode: 'serial' });

async function createConversation(api: AstraApi): Promise<SessionRecord> {
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const ready = await api.waitForSessionReady(created.session_id);
  await api.setPermissionMode(created.session_id, 'bypassPermissions');
  test.info().annotations.push({ type: 'e2e_session_id', description: created.session_id });
  return ready;
}

function blockingCommand(startedPath: string, releasePath: string, resultMarker: string): string {
  return [
    "python3 - <<'PY'",
    'import time',
    'from pathlib import Path',
    `started = Path(${JSON.stringify(startedPath)})`,
    `release = Path(${JSON.stringify(releasePath)})`,
    "started.write_text('started', encoding='utf-8')",
    'deadline = time.monotonic() + 300',
    'while not release.is_file():',
    '    if time.monotonic() >= deadline:',
    "        raise RuntimeError('E2E release marker timed out')",
    '    time.sleep(0.1)',
    `print(${JSON.stringify(resultMarker)})`,
    'PY',
  ].join('\n');
}

function blockingPrompt(
  label: string,
  startedPath: string,
  releasePath: string,
  resultMarker: string,
  finalMarker: string,
): string {
  return [
    label,
    'Use the Bash tool exactly once to run this command verbatim:',
    '```bash',
    blockingCommand(startedPath, releasePath, resultMarker),
    '```',
    'Do not use any other tool. Wait for Bash before replying.',
    `After it finishes, reply with exactly ${finalMarker}.`,
  ].join('\n');
}

async function filesExist(
  api: AstraApi,
  sessionId: string,
  filePaths: string[],
): Promise<boolean[]> {
  // The Agent runner and terminal occupy separate isolated sessions; their
  // shared workspace and its file API are the rendezvous contract. Reading a
  // terminal stream would also mistake its echoed command for probe output.
  return Promise.all(filePaths.map(async (filePath) => {
    try {
      await api.downloadFileText(sessionId, filePath, 30_000);
      return true;
    } catch (error) {
      if (FILE_NOT_FOUND.test(String((error as Error)?.message ?? error))) return false;
      throw error;
    }
  }));
}

function activeBashEvidence(
  message: Record<string, unknown> | undefined,
  targetPaths: string[],
): {
  bashCalls: number;
  distinctToolCallIds: number;
  matchedTargets: string[];
  resultIds: string[];
} {
  const blocks = Array.isArray(message?.blocks)
    ? message.blocks.filter((block): block is Record<string, unknown> => (
      Boolean(block) && typeof block === 'object'
    ))
    : [];
  const bashCalls = blocks.filter((block) => (
    String(block.type || '') === 'tool_use' && String(block.name || '') === 'Bash'
  ));
  const toolCallIds = bashCalls
    .map((block) => String(block.id || '').trim())
    .filter(Boolean);
  const matchedTargets = bashCalls.flatMap((block) => {
    const input = block.input && typeof block.input === 'object'
      ? block.input as Record<string, unknown>
      : {};
    const command = String(input.command || '');
    return targetPaths.filter((target) => command.includes(target));
  });
  const toolCallIdSet = new Set(toolCallIds);
  const resultIds = blocks
    .filter((block) => String(block.type || '') === 'tool_result')
    .map((block) => String(block.tool_use_id || '').trim())
    .filter((toolCallId) => toolCallIdSet.has(toolCallId));
  return {
    bashCalls: bashCalls.length,
    distinctToolCallIds: toolCallIdSet.size,
    matchedTargets: [...matchedTargets].sort(),
    resultIds: [...resultIds].sort(),
  };
}

async function waitForToolStart(
  api: AstraApi,
  sessionId: string,
  startedPaths: string[],
  timeoutMs = TOOL_START_TIMEOUT_MS,
  terminalInputId?: string,
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const results = await filesExist(api, sessionId, startedPaths);
    if (results.every(Boolean)) return true;
    // A FIFO input may finish inside the first input's platform turn. Bind
    // terminal evidence to the consumed input, not the turn's first command.
    // Read its durable owner before the snapshot so the old READY handoff gap
    // cannot stand in for completion of an input that has not been consumed.
    const consumedInput = terminalInputId
      ? (await api.getMessages(sessionId, 100)).messages.find((message) => (
        message.role === 'user' && message.message_id === `${terminalInputId}:user`
      ))
      : undefined;
    const detail = await api.getSession(sessionId);
    if (
      !detail.current_turn_id
      && String(detail.state || '') === 'READY'
      && (
        !terminalInputId
        || (Boolean(consumedInput?.turn_id) && detail.last_turn_id === consumedInput?.turn_id)
      )
    ) return false;
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  return false;
}

async function releaseFiles(api: AstraApi, sessionId: string, paths: string[]): Promise<void> {
  if (paths.length === 0) return;
  // Loud. A swallowed release turns a diagnosable failure into a timeout sixty
  // seconds later, against an assertion about something else entirely: the
  // blocking Bash keeps blocking, the queued input keeps queueing, and the
  // report names the queue. One round read exactly that way — the first turn
  // ran for six minutes while the spec waited a minute for the second message.
  //
  // Then prove the file is there. `touch` exiting 0 is the terminal's verdict
  // on its own command, not evidence that the Bash tool waiting on this path
  // can see it: the two run in the box, and whether they share a view of /tmp
  // is exactly what a release that never arrives calls into question.
  const touch = await api.runTerminalCommand(
    sessionId,
    `touch ${paths.join(' ')} && ls -1 ${paths.join(' ')}`,
    '/tmp',
    30_000,
  );
  const seen = String(touch ?? '');
  for (const path of paths) {
    expect(
      seen,
      `the release file ${path} must exist after the terminal wrote it — the ` +
        'blocking tool waits on this exact path',
    ).toContain(path);
  }
}

async function queueFromComposer(
  page: Page,
  sessionId: string,
  marker: string,
): Promise<string> {
  const composer = page.getByTestId('composer-prompt');
  await expect(composer, 'busy conversations must keep the composer queueable').toBeEnabled({
    timeout: 30_000,
  });
  const response = await sendPrompt(page, sessionId, marker);
  const body = await response.json() as {
    data?: Record<string, unknown>;
    [key: string]: unknown;
  };
  const delivery = body.data ?? body;
  expect(delivery.status).toBe('delivered');
  const commandId = String(delivery.command_id ?? '').trim();
  expect(commandId).not.toEqual('');
  const inputId = String(delivery.input_id ?? '').trim();
  expect(inputId).toMatch(/^[0-9a-f-]{36}$/);
  await expect(
    page.getByTestId('composer-queue').filter({ hasText: marker }),
    'the busy input must be visible in the composer queue before handoff',
  ).toHaveCount(1, { timeout: 15_000 });
  return inputId;
}

async function waitForAssistantMarker(
  api: AstraApi,
  sessionId: string,
  marker: string,
  timeoutMs = TURN_TIMEOUT_MS,
  failWhenTurnSettles = false,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastAssistantText = '';
  while (Date.now() < deadline) {
    const detail = failWhenTurnSettles
      ? await api.getSession(sessionId)
      : null;
    const history = await api.getMessages(sessionId, 100);
    const messages = visibleMessages(history);
    const matchingMessages = messages.filter((message) => (
      message.role === 'assistant' && messageText(message).includes(marker)
    ));
    if (matchingMessages.length === 1) return;
    if (matchingMessages.length > 1) {
      throw new Error(`assistant response contains ${marker} more than once`);
    }
    lastAssistantText = messages
      .filter((message) => message.role === 'assistant')
      .map(messageText)
      .filter(Boolean)
      .at(-1) || '';
    if (
      detail
      && !detail.current_turn_id
      && ['COMPLETED', 'FAILED', 'CANCELLED'].includes(String(detail.last_turn_status || ''))
    ) {
      // Terminal projection writes the final UI message before it exposes the
      // terminal session snapshot. Read in that order so a turn settling
      // between two independent requests cannot make a stale message page look
      // like a markerless terminal result.
      throw new Error(
        `turn settled as ${detail.last_turn_status} without assistant marker ${marker}; `
        + `last assistant response=${JSON.stringify(lastAssistantText)}`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(
    `assistant response did not contain ${marker} within ${timeoutMs}ms; `
    + `last assistant response=${JSON.stringify(lastAssistantText)}`,
  );
}

async function answerWithStaleDetail(request: APIRequestContext, page: Page, coldBootstrap: boolean) {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const questionMarker = `SDK_LIVE_ASK_STALE_DETAIL_${runId}`;
  const finalMarker = `SDK_LIVE_ASK_ANSWERED_${runId}`;
  let stalePendingDetailCount = 0;
  // Live recovery gets one stale response; cold recovery hides detail authority
  // only after reload, so the first message page must restore the question.
  let forceStalePendingDetail = !coldBootstrap;

  try {
    await api.setPermissionMode(sessionId, 'default');
    await page.route(`**${apiPath(`/sessions/${sessionId}`)}`, async (route) => {
      if (route.request().method() !== 'GET') {
        await route.continue();
        return;
      }
      const response = await route.fetch();
      const body = await response.json() as Record<string, unknown>;
      const data = body.data && typeof body.data === 'object'
        ? body.data as Record<string, unknown>
        : body;
      if (
        forceStalePendingDetail
        && (coldBootstrap || stalePendingDetailCount === 0)
        && data.pending_interaction
        && typeof data.pending_interaction === 'object'
      ) {
        stalePendingDetailCount += 1;
        const stale = { ...data, pending_interaction: null };
        await route.fulfill({
          response,
          json: body.data && typeof body.data === 'object' ? { ...body, data: stale } : stale,
        });
        return;
      }
      await route.fulfill({ response, json: body });
    });

    await openSessionView(page, sessionId);
    await sendPrompt(page, sessionId, [
      `E2E live AskUserQuestion stale detail ${runId}.`,
      'Call AskUserQuestion exactly once as your first and only tool.',
      'Ask exactly two questions in that one call.',
      `Question 1 must contain ${questionMarker}_MULTI, use header SCENES, offer PRODUCT and OPS, and allow multiple selections.`,
      `Question 2 must contain ${questionMarker}_SINGLE, use header VALUE, offer YES and NO, and allow one selection.`,
      'Wait for both user answers and do not answer the questions yourself.',
      `After the user selects YES for Question 2, reply with exactly ${finalMarker}.`,
    ].join('\n'));

    const pending = await api.waitForPendingInteraction(sessionId, 150_000).catch((cause: unknown) => {
      const detail = cause instanceof Error ? cause.message : String(cause);
      throw new Error(
        `the live engine must expose and invoke native AskUserQuestion before the canonical-id oracle: ${detail}`,
      );
    });
    expect(String(pending?.presentation || '')).toBe('form');
    expect(String(pending?.tool_name || '')).toBe('AskUserQuestion');
    const questions = Array.isArray(pending?.questions)
      ? pending.questions as Array<Record<string, unknown>>
      : [];
    expect(questions, 'one AskUserQuestion call must expose both requested questions')
      .toHaveLength(2);
    const toolCallId = String(pending?.tool_call_id || '').trim();
    expect(toolCallId, 'the live questionnaire must expose the engine tool id').not.toEqual('');

    expect(String(pending.turn_id || '').trim()).not.toBe('');
    await expect.poll(async () => (
      visibleMessages(await api.getMessages(sessionId)).filter((message) => (
        message.role === 'assistant'
        && message.blocks?.some((block) => block.type === 'tool_use' && block.id === toolCallId)
      )).map((message) => message.turn_id)
    ), { message: 'the pending tool must have one owner in the exact interaction turn' })
      .toEqual([pending.turn_id]);
    const panel = page.getByTestId('pending-interaction-panel');
    if (!coldBootstrap) {
      await expect.poll(() => stalePendingDetailCount, {
        timeout: 15_000,
        message: 'the open page must exercise the one stale detail response from the original live scenario',
      }).toBe(1);
    }
    await expect(panel, 'the live question must be answerable before any reload').toBeVisible();
    await expect(panel.getByRole('checkbox', { name: /product/i }).first()).toBeEnabled();
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first())
      .toHaveAttribute('data-state', 'WAITING_INPUT');
    await expect(page.getByTestId('session-conversation-shell'))
      .toHaveAttribute('data-pending-tool-call-id', toolCallId);

    if (coldBootstrap) {
      stalePendingDetailCount = 0;
      forceStalePendingDetail = true;
      await page.reload();
      await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
      await expect.poll(() => stalePendingDetailCount, {
        timeout: 15_000,
        message: 'cold bootstrap must exercise a session detail that hides pending authority',
      }).toBeGreaterThan(0);
    }

    await expect(
      panel,
      'the first-page pending snapshot must remain answerable when detail is stale',
    ).toBeVisible();
    forceStalePendingDetail = false;
    await expect(panel).toContainText(`${questionMarker}_MULTI`);
    // The question switcher is a step navigation of buttons carrying
    // aria-current="step" — deliberately not a tab strip, since the answer
    // area is a fieldset rather than a tabpanel (docs/frontend-design.md §10).
    const scenesStep = panel.getByRole('button', { name: /SCENES/ });
    const valueStep = panel.getByRole('button', { name: /VALUE/ });
    const progress = panel.getByTestId('questionnaire-progress');
    const nextQuestion = panel.getByRole('button', { name: /Next question|下一题/ });
    const previousQuestion = panel.getByRole('button', { name: /Previous question|上一题/ });
    const submitHint = panel.getByTestId('questionnaire-submit-hint');
    await expect(scenesStep).toHaveAttribute('aria-current', 'step');
    await expect(progress).toContainText('0/2');
    await expect(nextQuestion).toBeDisabled();
    await expect(previousQuestion).not.toBeVisible();
    await expect(submitHint).toContainText(/Questions remaining: 2|还剩 2 题/);
    const product = panel.getByRole('checkbox', { name: /product/i }).first();
    await expect(product).toBeEnabled();
    await product.click();
    await expect(product).toBeChecked();
    await expect(progress).toContainText('1/2');
    await expect(submitHint).toContainText(/Questions remaining: 1|还剩 1 题/);
    await expect(nextQuestion).toBeEnabled();
    const navigationIsInsideScroller = await valueStep.evaluate((element) => {
      let parent = element.parentElement;
      while (parent && !parent.matches('[data-testid="pending-interaction-panel"]')) {
        if (['auto', 'scroll'].includes(window.getComputedStyle(parent).overflowY)) return true;
        parent = parent.parentElement;
      }
      return false;
    });
    expect(navigationIsInsideScroller, 'question navigation must stay outside the choices scroller').toBe(false);
    await expect(
      valueStep,
      'question navigation must remain fully visible without scrolling the answer choices',
    ).toBeInViewport({ ratio: 1 });
    const actionNavigationInsideScroller = await nextQuestion.evaluate((element) => (
      element.closest('[data-slot="pending-interaction-scroll-body"]') !== null
    ));
    expect(actionNavigationInsideScroller, 'previous/next stay outside the choices scroller').toBe(false);
    await expect(nextQuestion).toBeInViewport({ ratio: 1 });
    await nextQuestion.click();
    await expect(valueStep).toHaveAttribute('aria-current', 'step');
    await expect(panel).toContainText(`${questionMarker}_SINGLE`);
    const yes = panel.getByRole('radio', { name: /yes/i }).first();
    await expect(yes).toBeEnabled();
    await yes.click();
    await expect(yes).toBeChecked();
    await expect(progress).toContainText('2/2');
    await expect(submitHint).toContainText(/All questions answered|已完成全部问题/);
    await expect(nextQuestion).not.toBeVisible();
    await expect(previousQuestion).toBeInViewport({ ratio: 1 });
    await previousQuestion.click();
    await expect(scenesStep).toHaveAttribute('aria-current', 'step');
    await expect(product).toBeChecked();
    await expect(progress).toContainText('2/2');
    await valueStep.click();
    await expect(yes).toBeChecked();
    const submit = panel.getByRole('button', { name: /Submit answer|提交回答/ });
    await expect(submit).toBeEnabled();
    await expect(
      submit,
      'the questionnaire action must remain fully visible after navigating questions',
    ).toBeInViewport({ ratio: 1 });
    const answeredResponse = page.waitForResponse((response) => (
      response.request().method() === 'POST'
      && response.url().includes(`/sessions/${sessionId}/interaction-respond`)
    ));
    await submit.click();
    const answered = await answeredResponse;
    expect(answered.status(), 'the browser answer must reach the interaction endpoint').toBe(200);
    const answeredBody = await answered.json() as Record<string, unknown>;
    const answeredData = answeredBody.data && typeof answeredBody.data === 'object'
      ? answeredBody.data as Record<string, unknown>
      : answeredBody;
    expect(String(answeredData.interaction_id || '').trim()).toBe(pending?.interaction_id);

    await waitForAssistantMarker(api, sessionId, finalMarker, TURN_TIMEOUT_MS, true);
    await api.waitForSessionReady(sessionId, TURN_TIMEOUT_MS);
    await expect(panel, 'the accepted answer must close the questionnaire without another reload').toHaveCount(0);
    const visibleAnswer = page.getByTestId('assistant-text').filter({ hasText: finalMarker });
    await expect(visibleAnswer, 'the continuation must reach the browser, not only durable history').toHaveCount(1);
    await expect(visibleAnswer).toBeVisible();

    const canonicalAskEvidence = async () => {
      const history = await api.getMessages(sessionId, 100);
      const blocks = history.messages.flatMap((message) => message.blocks || []);
      const askIds = blocks
        .filter((block) => (
          block.type === 'tool_use'
          && block.name === 'AskUserQuestion'
          && JSON.stringify(block.input || {}).includes(questionMarker)
        ))
        .map((block) => String(block.id || '').trim())
        .filter(Boolean);
      const resultIds = blocks
        .filter((block) => block.type === 'tool_result')
        .map((block) => String(block.tool_use_id || '').trim())
        .filter((id) => id === toolCallId);
      return { askIds, resultIds };
    };
    await expect.poll(canonicalAskEvidence, {
      timeout: 45_000,
      message: 'answer settlement must retain one AskUserQuestion id and its matching result',
    }).toEqual({ askIds: [toolCallId], resultIds: [toolCallId] });

    await page.reload();
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
    await expect.poll(canonicalAskEvidence, {
      timeout: 45_000,
      message: 'cold history must preserve the same canonical AskUserQuestion tool id',
    }).toEqual({ askIds: [toolCallId], resultIds: [toolCallId] });
  } finally {
    await page.unrouteAll({ behavior: 'wait' }).catch(() => undefined);
    // The session is not deleted here — `trackSessions()` decides in an
    // afterEach, where the test's real status is known.
  }
}

for (const [coldBootstrap, title] of [
  [false, 'live AskUserQuestion remains answerable when the concurrent session detail read is stale'],
  [true, 'AskUserQuestion cold bootstrap stays answerable when session detail is stale'],
] as const) {
  test(title, async ({ request, page }) => {
    await answerWithStaleDetail(request, page, coldBootstrap);
  });
}

test('completed multi-step response survives a cold history bootstrap', async ({ request, page }) => {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const toolCallCount = 16;
  const toolMarkers = Array.from({ length: toolCallCount }, (_, index) => (
    `SDK_COLD_HISTORY_TOOL_${index + 1}_${runId}`
  ));
  await openSessionView(page, sessionId);
  const prompt = [
    `E2E completed multi-step response ${runId}. Follow every step exactly.`,
    `In this one response, invoke exactly ${toolCallCount} sequential Bash tool calls.`,
    'Invoke only one call at a time and wait for its result before invoking the next call.',
    'Never combine commands and never invoke the calls concurrently.',
    ...toolMarkers.map((marker, index) => `${index + 1}. Invoke Bash with exactly: printf '${marker}'`),
    'Do not use any other tool.',
    `After all ${toolCallCount} Bash results arrive, give a brief completion report.`,
  ].join('\n');
  await sendPrompt(page, sessionId, prompt);
  const settled = await api.waitForSession(sessionId, (detail) => (
    !detail.current_turn_id && detail.state === 'READY'
    && detail.last_turn_status === 'COMPLETED' && Boolean(detail.last_turn_id)
  ));
  expect(settled.last_turn_error || '').toBe('');
  const durable = await api.getMessages(sessionId);
  expect(durable.active_turn_overlay, 'a live snapshot must not mask missing durable history').toBeFalsy();
  expect(durable.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([prompt]);
  const replies = durable.messages.filter((message) => (
    message.role === 'assistant' && message.turn_id === settled.last_turn_id
  ));
  const blocks = replies.flatMap((message) => message.blocks || []);
  const calls = blocks.filter((block) => block.type === 'tool_use');
  expect(calls).toHaveLength(toolCallCount);
  const toolIds = calls.map((call) => String(call.id || '').trim());
  expect(toolIds.every(Boolean)).toBe(true);
  expect(new Set(toolIds).size).toBe(toolCallCount);
  for (const [index, call] of calls.entries()) {
    expect(call.name).toBe('Bash');
    expect(call.input).toMatchObject({ command: `printf '${toolMarkers[index]}'` });
    const results = blocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === call.id);
    expect(results, 'each executed call must retain exactly one result').toHaveLength(1);
    expect(results[0]!.is_error).not.toBe(true);
    expect(JSON.stringify(results[0]!.content)).toContain(toolMarkers[index]);
  }
  const lastResultIndex = blocks.map((block) => block.type).lastIndexOf('tool_result');
  const terminalText = blocks.slice(lastResultIndex + 1).filter((block) => block.type === 'text')
    .map((block) => String(block.text || '')).join('').trim();
  expect(terminalText, 'the actual model must answer after all tool results').not.toBe('');
  const replyText = page.getByTestId('assistant-message').getByTestId('assistant-text');
  const textBlocks = blocks.filter((block) => block.type === 'text' && String(block.text || '').trim());
  await expect(replyText, 'the live page must include the final prose block after the tools')
    .toHaveCount(textBlocks.length);
  await expect(replyText.last()).toBeVisible();
  await expect(replyText.last()).not.toHaveText('');
  // Compare rendered prose with rendered prose: Markdown source contains
  // syntax that is intentionally absent from the visible answer.
  const renderedReply = await replyText.allTextContents();

  // A fresh route must rebuild from the database. The unified messages read
  // carries history and overlay together; no separate bootstrap is involved.
  await page.goto('about:blank', { waitUntil: 'domcontentloaded' });
  await openSessionView(page, sessionId);
  const assertColdHistory = async () => {
    // A settled multi-step response arrives on a rebuilt page as one header:
    // the prose between its tool calls and the cards themselves are fetched only
    // when the reader opens it. Everything below reads that work, so it is
    // opened first.
    await revealAssistantProcess(page);
    await expect(replyText, 'every prose block, including the terminal answer, must occur once')
      .toHaveText(renderedReply);
    await expect(replyText.last()).toBeVisible();
    await expect(page.getByTestId('assistant-message').getByRole('button', { name: /^Bash\b/ }))
      .toHaveCount(toolCallCount);
    await expect(page.getByTestId('assistant-message').getByRole('button', {
      name: /^Bash\s+(Working|Awaiting confirmation|处理中|等待确认)/,
    })).toHaveCount(0);
    const cold = await api.getMessages(sessionId);
    expect(cold.active_turn_overlay).toBeFalsy();
    expect(cold.messages, 'cold reads must retain all message identities, tool pairs and final text')
      .toEqual(durable.messages);
  };
  await assertColdHistory();
  const refreshed = page.waitForResponse((response) => (
    response.request().method() === 'GET'
    && new URL(response.url()).pathname === apiPath(`/sessions/${sessionId}/history-blocks`)
  ));
  await page.evaluate(() => { window.dispatchEvent(new Event('astrabox:manual-refresh')); });
  expect((await refreshed).status()).toBe(200);
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
  await assertColdHistory();
});

test('Cancel hands stopping state to an already queued next input', async ({ request, page }) => {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const startedPath = `/workspace/.astrabox-e2e-sdk-cancel-${runId}.started`;
  const releasePath = `/workspace/.astrabox-e2e-sdk-cancel-${runId}.release`;
  const firstMarker = `SDK_CANCEL_RUNNING_${runId}`;
  const followupMarker = `SDK_CANCEL_FOLLOWUP_${runId}`;

  try {
    await openSessionView(page, sessionId);
    const startedPrompt = blockingPrompt(firstMarker, startedPath, releasePath, `CANCELLED_RESULT_${runId}`, `NEVER_REQUIRED_${runId}`);
    await sendPrompt(page, sessionId, startedPrompt);
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. `waitForToolStart` already returns false the moment the
    // turn settles without the tool, so a declined ask costs that turn.
    const started = await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(sessionId, startedPrompt);
      },
      probe: async () =>
        (await waitForToolStart(api, sessionId, [startedPath])) ? true : null,
      what: 'the configured model did not enter the requested blocking Bash tool',
      budgetMs: TOOL_START_TIMEOUT_MS * 2,
      probeMs: TOOL_START_TIMEOUT_MS,
    });

    await queueFromComposer(
      page,
      sessionId,
      `${followupMarker}: do not use tools; reply with exactly ${followupMarker}_DONE`,
    );
    await page.getByTestId('run-composer-stop').click();

    await expect(page.getByTestId('composer-queue')).toHaveCount(0, { timeout: 60_000 });
    await expect(
      page.getByTestId('user-message').filter({ hasText: followupMarker }),
      'the queued input must replace the cancelled turn in the transcript',
    ).toHaveCount(1, { timeout: 60_000 });
    await waitForAssistantMarker(api, sessionId, `${followupMarker}_DONE`);
    const ready = await api.waitForSessionReady(sessionId, TURN_TIMEOUT_MS);
    expect(ready.last_turn_status).toBe('COMPLETED');
    await expect(page.getByText(/API Error|AGENT_RUNTIME_ERROR|Traceback/i)).toHaveCount(0);
  } finally {
    await releaseFiles(api, sessionId, [releasePath]).catch(() => {});
    // The session is not deleted here — `trackSessions()` decides in an
    // afterEach, where the test's real status is known.
  }
});

test('Cancel remains non-fatal and the next input dequeues before model response', async ({ request, page }) => {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const firstStarted = `/workspace/.astrabox-e2e-sdk-cancel-first-${runId}.started`;
  const firstRelease = `/workspace/.astrabox-e2e-sdk-cancel-first-${runId}.release`;
  const nextStarted = `/workspace/.astrabox-e2e-sdk-cancel-next-${runId}.started`;
  const nextRelease = `/workspace/.astrabox-e2e-sdk-cancel-next-${runId}.release`;
  const nextUserMarker = `SDK_CANCEL_NEXT_USER_${runId}`;
  const nextFinalMarker = `SDK_CANCEL_NEXT_FINAL_${runId}`;

  try {
    await openSessionView(page, sessionId);
    const firstDidStartPrompt = blockingPrompt(`SDK_CANCEL_FIRST_${runId}`, firstStarted, firstRelease, `SDK_CANCEL_FIRST_RESULT_${runId}`, `SDK_CANCEL_FIRST_FINAL_${runId}`);
    await sendPrompt(page, sessionId, firstDidStartPrompt);
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. `waitForToolStart` already returns false the moment the
    // turn settles without the tool, so a declined ask costs that turn.
    const firstDidStart = await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(sessionId, firstDidStartPrompt);
      },
      probe: async () =>
        (await waitForToolStart(api, sessionId, [firstStarted])) ? true : null,
      what: 'the configured model did not enter the first blocking Bash tool',
      budgetMs: TOOL_START_TIMEOUT_MS * 2,
      probeMs: TOOL_START_TIMEOUT_MS,
    });

    const nextInputId = await queueFromComposer(page, sessionId, blockingPrompt(
      nextUserMarker,
      nextStarted,
      nextRelease,
      `SDK_CANCEL_NEXT_RESULT_${runId}`,
      nextFinalMarker,
    ));
    await page.getByTestId('run-composer-stop').click();

    const nextDidStart = await waitForToolStart(
      api,
      sessionId,
      [nextStarted],
      180_000,
      nextInputId,
    );
    expect(nextDidStart, 'the consumed follow-up finished without executing its required Bash tool').toBe(true);
    await expect(page.getByTestId('composer-queue')).toHaveCount(0, { timeout: 30_000 });
    await expect(page.getByTestId('user-message').filter({ hasText: nextUserMarker })).toHaveCount(1);
    await expect(
      page.getByTestId('assistant-message').filter({ hasText: nextFinalMarker }),
      'the queued user input must dequeue before its model response exists',
    ).toHaveCount(0);
    await expect(page.getByTestId('run-composer-stop')).toBeEnabled();
    await expect(page.getByText(/API Error|AGENT_RUNTIME_ERROR|Traceback/i)).toHaveCount(0);

    await releaseFiles(api, sessionId, [nextRelease]);
    await waitForAssistantMarker(api, sessionId, nextFinalMarker);
    const ready = await api.waitForSessionReady(sessionId, TURN_TIMEOUT_MS);
    expect(ready.last_turn_status).toBe('COMPLETED');
  } finally {
    await releaseFiles(api, sessionId, [firstRelease, nextRelease]).catch(() => {});
    // The session is not deleted here — `trackSessions()` decides in an
    // afterEach, where the test's real status is known.
  }
});

test('adapter-buffered input moves once from the queue at SDK consumption', async ({ request, page }) => {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const startedPath = `/workspace/.astrabox-e2e-sdk-sequential-${runId}.started`;
  const releasePath = `/workspace/.astrabox-e2e-sdk-sequential-${runId}.release`;
  const firstUserMarker = `SDK_SEQUENTIAL_FIRST_${runId}`;
  const secondUserMarker = `SDK_SEQUENTIAL_SECOND_${runId}`;

  try {
    await openSessionView(page, sessionId);
    const startedPrompt = blockingPrompt(firstUserMarker, startedPath, releasePath, `SDK_SEQUENTIAL_FIRST_RESULT_${runId}`, `SDK_SEQUENTIAL_FIRST_DONE_${runId}`);
    await sendPrompt(page, sessionId, startedPrompt);
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. `waitForToolStart` already returns false the moment the
    // turn settles without the tool, so a declined ask costs that turn.
    const started = await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(sessionId, startedPrompt);
      },
      probe: async () =>
        (await waitForToolStart(api, sessionId, [startedPath])) ? true : null,
      what: 'the configured model did not enter the requested blocking Bash tool',
      budgetMs: TOOL_START_TIMEOUT_MS * 2,
      probeMs: TOOL_START_TIMEOUT_MS,
    });

    await queueFromComposer(
      page,
      sessionId,
      `${secondUserMarker}: do not use tools; acknowledge this input briefly`,
    );
    await expect(
      page.getByTestId('user-message').filter({ hasText: secondUserMarker }),
      'the queue exclusively owns an input until the SDK consumes it',
    ).toHaveCount(0);

    await releaseFiles(api, sessionId, [releasePath]);
    await expect(page.getByTestId('user-message').filter({ hasText: secondUserMarker })).toHaveCount(1, {
      timeout: 60_000,
    });
    await expect(
      page.getByTestId('composer-queue').filter({ hasText: secondUserMarker }),
      'SDK consumption hands the input from the queue to one transcript row',
    ).toHaveCount(0);
    await expect.poll(async () => {
      const history = visibleMessages(await api.getMessages(sessionId, 100));
      const userIndexes = history.flatMap((message, index) => (
        message.role === 'user' && messageText(message).includes(secondUserMarker)
          ? [index]
          : []
      ));
      if (userIndexes.length !== 1) {
        return { users: userIndexes.length, responses: 0, nonEmpty: false };
      }
      const nextUserIndex = history.findIndex((message, index) => (
        index > userIndexes[0] && message.role === 'user'
      ));
      const responseWindow = history.slice(
        userIndexes[0] + 1,
        nextUserIndex < 0 ? undefined : nextUserIndex,
      ).filter((message) => message.role === 'assistant');
      return {
        users: userIndexes.length,
        responses: responseWindow.length,
        nonEmpty: responseWindow.some((message) => messageText(message).trim().length > 0),
      };
    }, {
      timeout: TURN_TIMEOUT_MS,
      intervals: [250, 500, 1_000],
      message: [
        'the SDK may group streaming inputs into fewer ResultMessages, but the',
        'consumed input must own one non-empty response window',
      ].join(' '),
    }).toEqual({ users: 1, responses: 1, nonEmpty: true });
    await api.waitForSessionReady(sessionId, TURN_TIMEOUT_MS);

    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });
    await expect(page.getByTestId('user-message').filter({ hasText: firstUserMarker })).toHaveCount(1);
    const secondUser = page.getByTestId('user-message').filter({ hasText: secondUserMarker });
    await expect(secondUser).toHaveCount(1);
    await expect.poll(async () => secondUser.evaluate((element) => {
      const log = element.closest('[role="log"]');
      if (!log) throw new Error('the consumed input must belong to the conversation log');
      const messages = Array.from(log.querySelectorAll(
        '[data-testid="user-message"], [data-testid="assistant-message"]',
      ));
      const userIndex = messages.indexOf(element);
      if (userIndex < 0) throw new Error('the conversation log must contain the consumed input');
      const nextUserIndex = messages.findIndex((message, index) => (
        index > userIndex && message.getAttribute('data-testid') === 'user-message'
      ));
      const responseWindow = messages.slice(userIndex + 1, nextUserIndex < 0 ? undefined : nextUserIndex)
        .filter((message) => message.getAttribute('data-testid') === 'assistant-message');
      return {
        responses: responseWindow.length,
        nonEmpty: responseWindow.some((message) => (message.textContent ?? '').trim().length > 0),
      };
    }), {
      timeout: 60_000,
      message: 'reload must preserve the consumed input and its response window',
    }).toEqual({ responses: 1, nonEmpty: true });
    await expect(page.getByTestId('composer-queue')).toHaveCount(0);
  } finally {
    await releaseFiles(api, sessionId, [releasePath]).catch(() => {});
    // The session is not deleted here — `trackSessions()` decides in an
    // afterEach, where the test's real status is known.
  }
});

test('queued input stays visible during a native question and is consumed once after the answer', async ({ request, page }) => {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const startedPath = `/workspace/.astrabox-e2e-pending-queue-${runId}.started`;
  const releasePath = `/workspace/.astrabox-e2e-pending-queue-${runId}.release`;
  const secondPrompt = `PENDING_QUEUE_INPUT_${runId}: acknowledge this message briefly without using any tools.`;

  await openSessionView(page, sessionId);
  await sendPrompt(page, sessionId, [
    'Prepare a short project-status note. First collect the status with this Bash command, verbatim:',
    '```bash',
    blockingCommand(startedPath, releasePath, 'Status collection complete.'),
    '```',
    'Wait for Bash to finish; do not run it in the background or create the release file.',
    'Then use AskUserQuestion once to ask which audience the note is for.',
    'Use one single-select question with header AUDIENCE and options DEVELOPERS and OPERATORS.',
    'Wait for the user to choose before writing the short note. Do not use any other tools.',
  ].join('\n'));
  expect(
    await waitForToolStart(api, sessionId, [startedPath]),
    'the first input must reach the real Bash barrier before the second input is accepted',
  ).toBe(true);
  const inputId = await queueFromComposer(page, sessionId, secondPrompt);
  const queuedUser = page.getByTestId('user-message').filter({ hasText: secondPrompt });
  await expect(queuedUser, 'acceptance must not manufacture an SDK-consumed user row').toHaveCount(0);
  const consumedEvents = () => sessionEvents(sessionId).filter((event) => (
    event.event_type === 'input.consumed'
    && (event.payload as Record<string, unknown> | undefined)?.input_id === inputId
  ));
  expect(consumedEvents()).toHaveLength(0);

  await releaseFiles(api, sessionId, [releasePath]);
  const pending = await api.waitForPendingInteractionOrSettledTurn(sessionId, TOOL_START_TIMEOUT_MS);
  expect(pending, 'the original input must ask a real native question before it completes').not.toBeNull();
  expect(pending?.presentation).toBe('form');
  expect(pending?.tool_name).toBe('AskUserQuestion');
  const toolCallId = String(pending?.tool_call_id || '').trim();
  expect(toolCallId).not.toBe('');
  const panel = page.getByTestId('pending-interaction-panel');
  await expect(panel).toBeVisible();
  await expect(page.getByTestId('composer-prompt')).toHaveCount(0);
  await expect(page.getByTestId('session-conversation-shell'))
    .toHaveAttribute('data-pending-tool-call-id', toolCallId);
  const pendingQueue = panel.getByTestId('composer-queue');
  await expect(pendingQueue, 'the pending form must retain the same accepted queue item').toHaveCount(1);
  await expect(pendingQueue).toContainText(secondPrompt);
  await expect(pendingQueue).toBeInViewport({ ratio: 1 });
  await expect(pendingQueue.getByRole('button'), 'an accepted native input has no fake delete or retry action').toHaveCount(0);
  await expect(queuedUser).toHaveCount(0);
  expect(consumedEvents(), 'waiting for the answer must not consume the queued input').toHaveLength(0);
  const choice = panel.getByRole('radio', { name: /developers/i }).first();
  await expect(choice).toBeEnabled();
  await choice.click();
  const submit = panel.getByRole('button', { name: /Submit answer|提交回答/ });
  await expect(submit).toBeInViewport({ ratio: 1 });
  const answerResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && response.url().includes(`/sessions/${sessionId}/interaction-respond`)
  ));
  await submit.click();
  expect((await answerResponse).status()).toBe(200);

  await expect(queuedUser).toHaveCount(1, { timeout: TURN_TIMEOUT_MS });
  await expect(page.getByTestId('composer-queue')).toHaveCount(0);
  await expect(panel).toHaveCount(0);
  await expect.poll(async () => {
    const history = visibleMessages(await api.getMessages(sessionId, 100));
    const users = history.filter((message) => message.role === 'user' && messageText(message) === secondPrompt);
    const userIndex = history.findIndex((message) => message.message_id === `${inputId}:user`);
    const replies = userIndex < 0 ? [] : history.slice(userIndex + 1)
      .filter((message) => message.role === 'assistant' && messageText(message).trim().length > 0);
    return { userIds: users.map((message) => message.message_id), replies: replies.length };
  }, {
    timeout: TURN_TIMEOUT_MS,
    message: 'the exact SDK-consumed input must own one nonempty answer, without a wording oracle',
  })
    .toEqual({ userIds: [`${inputId}:user`], replies: 1 });
  await api.waitForSessionReady(sessionId);
  const consumed = consumedEvents();
  expect(consumed, 'the same accepted input must have exactly one durable consumption boundary').toHaveLength(1);
  expect((consumed[0].payload as Record<string, unknown>).content).toBe(secondPrompt);
  const blocks = (await api.getMessages(sessionId, 100)).messages.flatMap((message) => message.blocks || []);
  expect(blocks.filter((block) => block.type === 'tool_use' && block.id === toolCallId)).toHaveLength(1);
  expect(blocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === toolCallId)).toHaveLength(1);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(queuedUser, 'cold history must retain one input, not recreate the queue').toHaveCount(1);
  await expect(page.getByTestId('composer-queue')).toHaveCount(0);
  expect(consumedEvents()).toHaveLength(1);
  // No finally release or interrupt: trackSessions retains the still-held
  // tool/question and its accepted input when any assertion fails.
});

test('accepted input waits for the Result boundary and survives live bootstrap', async ({ request, page }) => {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const firstUserMarker = `SDK_PRE_RESULT_FIRST_USER_${runId}`;
  const secondUserMarker = `SDK_PRE_RESULT_SECOND_USER_${runId}`;
  const firstStarted = `/workspace/.astrabox-e2e-sdk-pre-result-first-${runId}.started`;
  const firstRelease = `/workspace/.astrabox-e2e-sdk-pre-result-first-${runId}.release`;
  const secondStarted = `/workspace/.astrabox-e2e-sdk-pre-result-second-${runId}.started`;
  const secondRelease = `/workspace/.astrabox-e2e-sdk-pre-result-second-${runId}.release`;
  const firstPrompt = blockingPrompt(
    firstUserMarker,
    firstStarted,
    firstRelease,
    `SDK_PRE_RESULT_FIRST_RESULT_${runId}`,
    `SDK_PRE_RESULT_FIRST_DONE_${runId}`,
  );
  const secondPrompt = [
    secondUserMarker,
    'After the current tool finishes, use the Bash tool exactly once to run this command verbatim:',
    '```bash',
    blockingCommand(
      secondStarted,
      secondRelease,
      `SDK_PRE_RESULT_SECOND_RESULT_${runId}`,
    ),
    '```',
    'Do not use any other tool. Wait for this Bash result before finishing.',
  ].join('\n');

  try {
    await mirrorSseBodies(page);
    await openSessionView(page, sessionId);
    await sendPrompt(page, sessionId, firstPrompt);
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. `waitForToolStart` already returns false the moment the
    // turn settles without the tool, so a declined ask costs that turn.
    const firstDidStart = await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(sessionId, firstPrompt);
      },
      probe: async () =>
        (await waitForToolStart(api, sessionId, [firstStarted])) ? true : null,
      what: 'the configured model did not enter the first blocking Bash tool',
      budgetMs: TOOL_START_TIMEOUT_MS * 2,
      probeMs: TOOL_START_TIMEOUT_MS,
    });

    const secondResponse = await sendPrompt(page, sessionId, secondPrompt);
    expect(
      new URL(secondResponse.url()).pathname,
      'input submitted before the first Result must enter the durable FIFO',
    ).toContain(`/sessions/${sessionId}/turn-inputs`);
    const secondBody = await secondResponse.json() as {
      data?: Record<string, unknown>;
      [key: string]: unknown;
    };
    const secondDelivery = secondBody.data ?? secondBody;
    expect(secondDelivery.status).toBe('delivered');
    const secondInputId = String(secondDelivery.input_id ?? '').trim();
    expect(secondInputId).toMatch(/^[0-9a-f-]{36}$/);
    await expect(page.getByTestId('composer-queue')).toContainText(secondUserMarker);

    await releaseFiles(api, sessionId, [firstRelease]);
    const secondDidStart = await waitForToolStart(
      api,
      sessionId,
      [secondStarted],
      180_000,
    );
    test.skip(!secondDidStart, 'the configured model did not enter the queued Bash tool');

    await expect.poll(async () => {
      const history = await api.getMessages(sessionId, 100);
      const activeMessages = history.active_turn_overlay?.messages ?? [];
      const roots = activeMessages.filter((message) => message.role === 'user');
      const blocks = activeMessages.flatMap((message) => (
        Array.isArray(message.blocks) ? message.blocks : []
      ));
      return {
        first: roots.filter((message) => messageText(message).includes(firstUserMarker)).length,
        second: roots.filter((message) => (
          String(message.message_id ?? '').trim() === `${secondInputId}:user`
          && messageText(message).includes(secondUserMarker)
        )).length,
        bash: blocks.filter((block) => (
          String(block.type ?? '') === 'tool_use'
          && String(block.name ?? '') === 'Bash'
        )).length,
        results: blocks.filter((block) => String(block.type ?? '') === 'result').length,
      };
    }, {
      timeout: 90_000,
      intervals: [250, 500, 1_000],
      message: 'the queued input must begin only after the first SDK Result boundary',
    }).toEqual({ first: 1, second: 1, bash: 2, results: 1 });

    const activeHistory = await api.getMessages(sessionId, 100);
    const activeOverlay = activeHistory.active_turn_overlay;
    const activeAssistants = (activeOverlay?.messages ?? []).filter(
      (message) => message.role === 'assistant',
    );
    expect(activeAssistants).toHaveLength(2);
    const lastAssistant = activeAssistants[activeAssistants.length - 1];
    expect(
      activeOverlay?.message,
      'the active message must contain only the last consumed input response',
    ).toMatchObject({
      message_id: lastAssistant.message_id,
      turn_id: lastAssistant.turn_id,
      role: lastAssistant.role,
      content: lastAssistant.content,
      blocks: lastAssistant.blocks,
    });
    const responseOwners = activeAssistants.map((message) => {
      const calls = (message.blocks ?? []).filter((block) => (
        block.type === 'tool_use' && block.name === 'Bash'
      ));
      expect(calls).toHaveLength(1);
      const toolCallId = String(calls[0].id ?? '').trim();
      expect(toolCallId).not.toEqual('');
      return { messageId: message.message_id, toolCallId };
    });
    expect(new Set(responseOwners.map((owner) => owner.toolCallId)).size).toBe(2);

    const browserResponseOwners = async () => {
      const bodies = await aiStreamBodies(page);
      const seenOwners = new Set<string>();
      let completedReplyCursor = -1;
      for (const body of bodies) {
        const requestedCursor = new URL(body.url).searchParams.get('after_seq');
        expect(
          requestedCursor === null ? -1 : Number(requestedCursor),
          'reply handoff must not replay a reply whose terminal cursor was received',
        ).toBeGreaterThanOrEqual(completedReplyCursor);
        let messageId = '';
        let replyFinished = false;
        for (const line of body.text.split('\n').slice(0, -1)) {
          if (!line.startsWith('data:')) continue;
          const raw = line.slice(5).trim();
          if (!raw || raw === '[DONE]') continue;
          const frame = JSON.parse(raw) as Record<string, unknown>;
          if (frame.type === 'start') messageId = String(frame.messageId ?? '');
          if (frame.type === 'finish' && frame.finishReason === 'stop') replyFinished = true;
          if (frame.type === 'data-resume-cursor' && replyFinished) {
            const cursor = frame.data as { frameSeq: number };
            completedReplyCursor = Math.max(completedReplyCursor, cursor.frameSeq);
          }
          if (frame.type !== 'tool-input-start' || frame.toolName !== 'Bash') continue;
          const toolCallId = String(frame.toolCallId ?? '');
          const owner = responseOwners.find((candidate) => candidate.toolCallId === toolCallId);
          expect(owner, 'every streamed Bash call must belong to a stored response').toBeDefined();
          expect(messageId, 'the stream must name the same reply as the history').toBe(owner?.messageId);
          seenOwners.add(messageId);
        }
      }
      return [...seenOwners].sort();
    };
    const assertBrowserResponseOwners = async (stage: string, expected: string[]) => {
      try {
        await expect.poll(browserResponseOwners, {
          message: 'the browser must receive each reply under its durable message identity',
          timeout: 30_000,
        }).toEqual([...expected].sort());
      } finally {
        await test.info().attach(`fifo-${stage}-browser-streams`, {
          body: JSON.stringify(await aiStreamBodies(page)),
          contentType: 'application/json',
        });
      }
    };
    // One response has settled and folded its call behind a header; the other is
    // still running and holds its card in a group that starts closed. Both are
    // opened, because the count below is of what the reader can reach.
    await revealAssistantProcess(page);
    await expect(page.getByRole('button', { name: /Bash/ })).toHaveCount(2);
    await assertBrowserResponseOwners('live', responseOwners.map((owner) => owner.messageId));

    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });
    await expect(page.getByTestId('user-message').filter({ hasText: firstUserMarker })).toHaveCount(1);
    await expect(page.getByTestId('user-message').filter({ hasText: secondUserMarker })).toHaveCount(1);
    await revealAssistantProcess(page);
    await expect(
      page.getByRole('button', { name: /Bash/ }),
      'cold bootstrap must retain both assistant tool cards across the Result boundary',
    ).toHaveCount(2, { timeout: 60_000 });
    await assertBrowserResponseOwners('resumed', [lastAssistant.message_id]);
    await expect(page.getByTestId('run-composer-stop')).toBeEnabled();

    await releaseFiles(api, sessionId, [secondRelease]);
    await expect.poll(async () => {
      const history = await api.getMessages(sessionId, 100);
      return {
        first: history.messages.filter((message) => (
          message.role === 'user' && messageText(message).includes(firstUserMarker)
        )).length,
        second: history.messages.filter((message) => (
          message.role === 'user' && messageText(message).includes(secondUserMarker)
        )).length,
      };
    }, {
      timeout: 180_000,
      intervals: [500, 1_000],
      message: 'the eventual Result must commit both root inputs to SessionStore',
    }).toEqual({ first: 1, second: 1 });
    await api.waitForSessionReady(sessionId, TURN_TIMEOUT_MS);
    await expect.poll(async () => {
      const history = await api.getMessages(sessionId, 100);
      const ownedBlocks = history.messages.flatMap((message) => (
        (message.blocks ?? []).map((block) => ({ messageId: message.message_id, block }))
      ));
      return responseOwners.map(({ toolCallId }) => ({
        toolCallId,
        callOwners: ownedBlocks.filter(({ block }) => (
          block.type === 'tool_use' && block.id === toolCallId
        )).map(({ messageId }) => messageId),
        resultOwners: ownedBlocks.filter(({ block }) => (
          block.type === 'tool_result' && block.tool_use_id === toolCallId
        )).map(({ messageId }) => messageId),
      }));
    }, {
      timeout: 45_000,
      message: 'settlement must retain each tool call and result only in its original response',
    }).toEqual(responseOwners.map(({ messageId, toolCallId }) => ({
      toolCallId,
      callOwners: [messageId],
      resultOwners: [messageId],
    })));
  } finally {
    await releaseFiles(api, sessionId, [firstRelease, secondRelease]).catch(() => {});
    // The session is not deleted here — `trackSessions()` decides in an
    // afterEach, where the test's real status is known.
  }
});

test('same-turn Bash calls survive cold bootstrap without replacing their sibling', async ({ request, page }) => {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const firstStarted = `/workspace/.astrabox-e2e-sdk-parallel-a-${runId}.started`;
  const firstRelease = `/workspace/.astrabox-e2e-sdk-parallel-a-${runId}.release`;
  const secondStarted = `/workspace/.astrabox-e2e-sdk-parallel-b-${runId}.started`;
  const secondRelease = `/workspace/.astrabox-e2e-sdk-parallel-b-${runId}.release`;
  const finalMarker = `SDK_PARALLEL_FINAL_${runId}`;
  const firstCommand = `touch ${firstStarted}; while [ ! -f ${firstRelease} ]; do sleep 0.1; done; printf SDK_PARALLEL_A_RESULT_${runId}`;
  const secondCommand = `touch ${secondStarted}; while [ ! -f ${secondRelease} ]; do sleep 0.1; done; printf SDK_PARALLEL_B_RESULT_${runId}`;

  try {
    await openSessionView(page, sessionId);
    await sendPrompt(page, sessionId, [
      `E2E same-turn Bash tools ${runId}.`,
      'In one assistant response, invoke exactly two Bash tools:',
      `- ${firstCommand}`,
      `- ${secondCommand}`,
      'Submit both calls in that response before waiting for either result and use no other tool.',
      `After both results arrive, reply with exactly ${finalMarker}.`,
    ].join('\n'));

    // Claude Code executes state-changing Bash calls sequentially even when
    // the model emits both tool_use blocks in one response. The first marker
    // anchors the unresolved turn; its projection must already retain both.
    const firstDidStart = await waitForToolStart(api, sessionId, [firstStarted]);
    test.skip(!firstDidStart, 'the configured model did not issue the requested same-turn Bash calls');

    const toolTargets = [firstStarted, secondStarted].sort();
    await expect.poll(async () => {
      const history = await api.getMessages(sessionId, 100);
      return activeBashEvidence(history.active_turn_overlay?.message, toolTargets);
    }, {
      timeout: 30_000,
      intervals: [250, 500, 1_000],
      message: 'the active assistant overlay must retain two distinct unresolved sibling Bash calls',
    }).toEqual({
      bashCalls: 2,
      distinctToolCallIds: 2,
      matchedTargets: toolTargets,
      resultIds: [],
    });

    // The turn is still running, so its two sibling calls sit in a process group
    // that starts closed — on this page and again on the reloaded one.
    await openLiveProcessGroup(page);
    await expect(page.getByRole('button', { name: /Bash/ })).toHaveCount(2, { timeout: 60_000 });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });
    await openLiveProcessGroup(page);
    await expect(
      page.getByRole('button', { name: /Bash/ }),
      'cold bootstrap must retain both active sibling tool calls',
    ).toHaveCount(2, { timeout: 60_000 });

    await releaseFiles(api, sessionId, [firstRelease, secondRelease]);
    await waitForAssistantMarker(api, sessionId, finalMarker);
  } finally {
    await releaseFiles(api, sessionId, [firstRelease, secondRelease]).catch(() => {});
    // The session is not deleted here — `trackSessions()` decides in an
    // afterEach, where the test's real status is known.
  }
});
