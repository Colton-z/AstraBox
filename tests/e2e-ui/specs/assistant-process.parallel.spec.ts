/**
 * E2E: a settled tool turn folds into one header, and the reader can open it —
 * live, after a reload, and after a failed detail read.
 *
 * The product path is one chain, and every link in it fails silently.
 * `history_blocks.py::project_record` replaces a settled response's tool work
 * with a single `process_block` and hands the folded blocks back separately;
 * `sessions.py::get_session_history_blocks` serves that projection with a
 * checkpoint cursor, `get_session_history_block_details` reopens one header at
 * that checkpoint, and `bridge_terminal.py::_schedule_process_summary` offers
 * the label the header is named with while
 * `session_read.py::_attach_process_summaries` puts it back on the page. Each
 * of those can answer with a well-formed, wrong body — a fold that hides the
 * conclusion as well as the work, a header nobody can open, a detail read
 * repeated on every expand, a label that is an error string — and every one of
 * them renders. The console shows something either way, the HTTP contract test
 * sees a valid envelope, and `tsc` sees a string.
 *
 * What is asserted here therefore has to be the reader's outcome: the marker
 * the model answered with stays on the page, the tool cards leave it and come
 * back on one press, the detail endpoint is asked once per page load, and the
 * header's accessible name is the label rather than a failure. Two of those are
 * only true after a reload, which is where the browser has nothing but
 * `process_details` to work from.
 *
 * Visibility is the assertion, never DOM presence. `MessageParts.tsx:462` and
 * `:562` keep both panels mounted while closed (`keepMounted`), so a folded
 * tool card is in the document the whole time — counting cards would pass on a
 * fold that hides nothing. A tool card, by contrast, unmounts its own body
 * (ai-elements/tool.tsx `ToolContent`), so the output text a card stands for is
 * absent until that card is pressed as well: header, group, card, three
 * presses, and only the third one puts a tool's output on the screen. Not every
 * card is a disclosure — `ToolParts.tsx` renders TodoWrite as a `Queue` and
 * AskUserQuestion as a `Card`, neither of which has a trigger — so a card is
 * pressed only where it states its own `aria-expanded`.
 *
 * The one injected fault is a 500 on the FIRST detail read after that reload.
 * It is injected rather than provoked because there is no way to make a real
 * detail read fail on demand, and the recovery it proves — a note the reader
 * can act on, and a retry that reaches the real endpoint — is the difference
 * between a transient failure and a header that stays empty for good.
 *
 * Engine independence: tool names differ per engine, so nothing here matches
 * `Bash` or `Read`. A card is located by `data-tool-call-id`, which carries the
 * `tool_use` block's own id, and is read through its header's state word.
 *
 * This spec depends on the model reaching for a tool. When it does not, it
 * FAILS and names what the engine drove instead — it does not `test.skip()`,
 * because the budget reporter kills the whole Playwright process group on a
 * skipped result (playwright-test-budget.cjs:88-110) and would end the lane
 * rather than cost one spec.
 */
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect, test, type Locator, type Page, type Route } from '@playwright/test';

import { AstraApi, messageText, type MessageRecord } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

// Budget arithmetic for the immovable 180s cap, not deployment tuning:
// 50 + 70 + 35 = 155 leaves the reload, the injected fault and the presses
// inside the cap, and a stall inside a stage fails with that stage's message
// instead of as a bare test timeout with nothing to read. READY_MS overrides
// waitForSessionReady's own 180s default, which alone equals the whole budget.
const READY_MS = 50_000;
const TURN_SETTLE_MS = 70_000;
const SUMMARY_MS = 35_000;
const RENDER_MS = 20_000;
const TOOL_START_MS = 45_000;

// Every literal below is copied from the locale files that produce it, both
// languages: the console is bilingual, the runner sets no locale, and the
// browser's own choice decides which string renders. A phrase written from
// memory here would match nothing and read as a product failure.
//
// `chat:tool_state.completed` — "Done" / "已完成". `ToolPartHeader` puts it in
// the card's accessible name as `<tool> <state>` (ToolParts.tsx:102) and in the
// badge text, so it is matched as the text of whichever header states it.
const TOOL_DONE = /(Done|已完成)/;
// `chat:process.title` — the header title no summary has replaced.
const GENERIC_PROCESS_TITLE = /^(Process|执行过程)$/;
// `chat:process.stopped` — "This response stopped early." / "本次回复提前结束。"
const TURN_STOPPED = /(stopped early|提前结束)/;
// `chat:process.details_failed` — "The steps could not be loaded: {{error}}" /
// "步骤加载失败：{{error}}". Matched on the subject rather than the whole
// sentence, which carries the server's own words after the colon.
const DETAILS_FAILED = /(could not be loaded|加载失败)/i;
// `common:retry` — "Retry" / "重试".
const RETRY_BUTTON = /^(Retry|重试)$/;
// A code the platform already registers (astrabox/common/utils/errors.py), so
// the injected body is the envelope the console parses rather than a shape
// invented for this file.
const INJECTED_FAULT = JSON.stringify({
  code: 'UNEXPECTED_SERVER_ERROR',
  message: 'injected process-detail read failure',
});

interface ProcessSummaryState {
  status: string;
  summary?: string | null;
  error?: string | null;
  turn_completed?: boolean | null;
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

function blocksOf(record: MessageRecord | null): Array<Record<string, unknown>> {
  return Array.isArray(record?.blocks) ? record.blocks : [];
}

function toolUseIds(record: MessageRecord | null): string[] {
  return blocksOf(record)
    .filter((block) => String(block.type ?? '') === 'tool_use')
    .map((block) => String(block.id ?? '').trim())
    .filter(Boolean);
}

/** Each `tool_use` id in *record* mapped to its result's `tool_result_state`. */
function toolResultStates(record: MessageRecord | null): Map<string, string> {
  const states = new Map<string, string>();
  for (const id of toolUseIds(record)) states.set(id, '');
  for (const block of blocksOf(record)) {
    if (String(block.type ?? '') !== 'tool_result') continue;
    const owner = String(block.tool_use_id ?? '').trim();
    if (owner) states.set(owner, String(block.tool_result_state ?? '').trim());
  }
  return states;
}

/**
 * The calls the reader has to be able to see on a turn that stopped short.
 *
 * `tool_result_state` is the platform's own durable word for how a call ended,
 * and `history_blocks.py::process_atoms` folds a call away only while that word
 * is `output-available`. A refused or errored call is what the fold leaves
 * outside, so those ids are what this spec looks for on screen.
 */
function failedToolIds(record: MessageRecord | null): string[] {
  const states = toolResultStates(record);
  return [...states]
    .filter(([, state]) => state === 'output-error' || state === 'output-denied')
    .map(([id]) => id);
}

/**
 * The calls a stop landed on before any result came back.
 *
 * A call with no result is not work that ran: `history_blocks.py::process_atoms`
 * folds a call away only while its result is `output-available`, and the open
 * page applies the same rule to a settled turn (`MessageParts.tsx`
 * `isProcessPart` / `isAlertPart`). These ids are therefore asserted outside
 * the fold, by id, exactly like a call that reported a failure.
 */
function resultlessToolIds(record: MessageRecord | null): string[] {
  return [...toolResultStates(record)]
    .filter(([, state]) => state === '')
    .map(([id]) => id);
}

/** The calls that ran to completion, and so belong behind the header. */
function settledToolIds(record: MessageRecord | null): string[] {
  return [...toolResultStates(record)]
    .filter(([, state]) => state === 'output-available')
    .map(([id]) => id);
}

/**
 * The engine's own tool names off the durable record — diagnostic only.
 *
 * What the model reached for is the model's business; what the page does with
 * it is this spec's. It never throws, so a broken read here cannot replace the
 * failure it is describing.
 */
function toolsDriven(record: MessageRecord | null): string {
  const names = new Set<string>();
  for (const block of blocksOf(record)) {
    if (String(block.type ?? '') !== 'tool_use') continue;
    const name = String(block.name ?? '').trim();
    if (name) names.add(name);
  }
  return [...names].sort().join(', ') || '(no tool_use block at all)';
}

/** Every tool card a reader can currently see inside `scope`. */
function visibleCards(scope: Locator): Locator {
  return scope.locator('[data-tool-call-id]:visible');
}

/** Press a collapsible trigger that states its own state, and wait for it. */
async function ensureExpanded(trigger: Locator): Promise<void> {
  if ((await trigger.getAttribute('aria-expanded')) === 'true') return;
  await trigger.click();
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');
}

/**
 * Open a folded turn and every group inside it.
 *
 * Both levels are the same Base UI collapsible, which marks its trigger
 * `aria-expanded` — the state is read off that rather than off what is on
 * screen, because a closed panel keeps its cards mounted and invisible.
 */
async function openProcess(turn: Locator): Promise<void> {
  await ensureExpanded(turn.getByTestId('assistant-turn-process-trigger'));
  const groups = turn.getByTestId('assistant-process');
  await expect(
    groups.first(),
    'an opened header must show at least one group of the work it stands for',
  ).toBeVisible({ timeout: RENDER_MS });
  const count = await groups.count();
  for (let index = 0; index < count; index += 1) {
    const trigger = groups.nth(index).getByTestId('assistant-process-trigger');
    await expect(trigger, 'an inner group names its content, not the whole execution process')
      .toHaveAccessibleName(/^(Tool calls|工具调用|Reasoning|思考)\s/);
    await ensureExpanded(trigger);
  }
}

/** Collapse a folded turn through its own header. */
async function closeProcess(turn: Locator): Promise<void> {
  const trigger = turn.getByTestId('assistant-turn-process-trigger');
  await trigger.click();
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
}

/**
 * Open every tool card on screen, so what the tools returned is readable.
 *
 * A card that is a disclosure starts collapsed and does not keep its body
 * mounted, so the output it stands for is not in the page at all until it is
 * pressed. A card that is not one — `ToolParts.tsx` builds TodoWrite on `Queue`
 * and AskUserQuestion on `Card` — shows its body already, and pressing for a
 * trigger it never had would spend the budget waiting for nothing. The
 * `aria-expanded` the card states about itself is what separates the two.
 */
async function openVisibleCards(scope: Locator): Promise<number> {
  const cards = visibleCards(scope);
  const count = await cards.count();
  for (let index = 0; index < count; index += 1) {
    const trigger = cards.nth(index).locator('button[aria-expanded]').first();
    if (await trigger.count() === 0) continue;
    await ensureExpanded(trigger);
  }
  return count;
}

/** Ask the API for the label until it stops being claimed by another writer. */
async function settledSummary(
  api: AstraApi,
  sessionId: string,
  messageId: string,
): Promise<ProcessSummaryState> {
  const deadline = Date.now() + SUMMARY_MS;
  let last: ProcessSummaryState = { status: 'unknown' };
  while (Date.now() < deadline) {
    last = await api.data<ProcessSummaryState>(
      'POST',
      `/sessions/${sessionId}/messages/${messageId}/process-summary`,
    );
    if (last.status === 'completed' || last.status === 'failed') return last;
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
  throw new Error(
    `process summary for ${messageId} stayed ${JSON.stringify(last.status)} for ` +
      `${SUMMARY_MS}ms; a claim that never finishes leaves the header with a ` +
      'title nobody can act on',
  );
}

/** The live transcript bubble carrying `marker`, before any reload rebinds ids. */
function liveTurn(page: Page, marker: string): Locator {
  return page.getByTestId('assistant-message').filter({ hasText: marker }).last();
}

test('a settled tool turn folds, names itself, and reopens once per page load', async ({
  page,
  request,
  browser,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const label = `PROCESS_FOLD_${runId}`;
  const readFile = `/workspace/process-source-${runId}.txt`;
  const writeFile = `/workspace/process-copy-${runId}.txt`;
  const toolOutputMarker = `PROCESS_TOOL_OUTPUT_${runId}`;
  const writtenMarker = `PROCESS_WRITTEN_${runId}`;
  const answerMarker = `PROCESS_ANSWER_${runId}`;

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId, READY_MS);
  // The UI dispatches under the SESSION's permission mode, so it is armed
  // before the page opens: a tool parked on an approval never settles, and a
  // turn that never settles never folds.
  const armed = await api.setPermissionMode(sessionId, 'bypassPermissions');
  expect(armed.permission_mode, 'the folded turn must run its tools unattended').toBe(
    'bypassPermissions',
  );
  // Placed by the platform's own file API rather than by a first tool call, so
  // the content the tool card must later show is fixed before the model runs.
  await api.uploadFileText(
    sessionId,
    '/workspace',
    `process-source-${runId}.txt`,
    `${toolOutputMarker}\n`,
  );

  const messagesPrefix = apiPath(`/sessions/${sessionId}/messages/`);
  const detailsPrefix = `${apiPath(`/sessions/${sessionId}/history-blocks`)}/`;
  const isSummaryCall = (url: string, method: string): boolean => {
    if (method !== 'POST') return false;
    const path = new URL(url).pathname;
    return path.startsWith(messagesPrefix) && path.endsWith('/process-summary');
  };
  const detailStatuses: number[] = [];
  page.on('response', (response) => {
    if (response.request().method() !== 'GET') return;
    if (!new URL(response.url()).pathname.startsWith(detailsPrefix)) return;
    detailStatuses.push(response.status());
  });

  await openSessionView(page, sessionId);

  // ── ACT: two tool calls, then a conclusion. The fold needs both — a response
  //    with no closing answer is left whole by `_deferred_groups`, because
  //    hiding its work would leave the page with nothing at all. ──────────────
  await sendPrompt(
    page,
    sessionId,
    [
      label,
      `1. Open the file ${readFile} with a tool and show its contents.`,
      `2. Create the file ${writeFile} containing exactly ${writtenMarker}.`,
      `3. After both steps finish, reply with exactly ${answerMarker} and nothing else.`,
      'Do not ask for confirmation.',
    ].join('\n'),
  );
  const summaryRequest = page.waitForRequest(
    (candidate) => isSummaryCall(candidate.url(), candidate.method()),
    { timeout: TURN_SETTLE_MS + SUMMARY_MS },
  );
  const summaryResponse = page.waitForResponse(
    (candidate) => isSummaryCall(candidate.url(), candidate.request().method()),
    { timeout: TURN_SETTLE_MS + SUMMARY_MS },
  );
  await expect(
    page.getByTestId('user-message').filter({ hasText: label }).last(),
    'the user bubble should render — proof the turn was dispatched from the page',
  ).toBeVisible({ timeout: RENDER_MS });

  // The live activity line is best effort on purpose. It reports the tool
  // currently running, so a turn whose tools return quickly can settle without
  // it ever being painted; failing on that would make the model's speed the
  // verdict. The race is resolved against the turn ending, so a turn that never
  // shows it costs nothing but the annotation below.
  const stopControl = page.getByTestId('run-composer-stop');
  const sawActivity = await Promise.race([
    page
      .getByTestId('assistant-process-activity')
      .first()
      .waitFor({ state: 'visible', timeout: TURN_SETTLE_MS })
      .then(() => true),
    stopControl
      .waitFor({ state: 'detached', timeout: TURN_SETTLE_MS })
      .then(() => false),
  ]).catch(() => false);
  test.info().annotations.push({
    type: 'e2e_process_activity_seen',
    description: String(sawActivity),
  });

  await expect(
    stopControl,
    'the turn must settle inside its budget — everything after this reads a static transcript',
  ).toHaveCount(0, { timeout: TURN_SETTLE_MS });
  const settled = await api.waitForAssistantMessageMatching(
    sessionId,
    0,
    (message) => messageText(message).includes(answerMarker),
    TURN_SETTLE_MS,
  );
  const messageId = String(settled.message_id);
  const durableToolIds = toolUseIds(settled);
  expect(
    durableToolIds.length,
    'the folded header stands for tool work, so the response must carry tool calls; ' +
      `the engine drove: ${toolsDriven(settled)}. Re-shape the prompt here rather ` +
      'than anywhere else, and do not convert this into a runtime skip: ' +
      'playwright-test-budget.cjs:48-70 SIGTERMs the whole process group on a ' +
      'skipped result and would end the lane instead of costing this one spec.',
  ).toBeGreaterThan(0);
  test.info().annotations.push({
    type: 'e2e_tools_driven',
    description: `${toolsDriven(settled)} (${durableToolIds.length} call(s))`,
  });

  // ── 1. The settled turn reads as one header with the answer beside it. ────
  const live = liveTurn(page, answerMarker);
  await expect(
    live.getByTestId('assistant-turn-process'),
    'a settled tool response folds into exactly one header',
  ).toHaveCount(1, { timeout: RENDER_MS });
  await expect(
    live.getByTestId('assistant-text').filter({ hasText: answerMarker }).first(),
    'the conclusion stays outside the fold — hiding it would empty the page',
  ).toBeVisible();
  await expect(
    visibleCards(live),
    'every completed tool call is behind the header until the reader opens it',
  ).toHaveCount(0);
  const liveTrigger = live.getByTestId('assistant-turn-process-trigger');
  await expect(liveTrigger).toHaveAttribute('aria-expanded', 'false');

  await openProcess(live);
  await expect(
    live.getByTestId('assistant-turn-process-panel'),
    'the opened header must show the work it stands for',
  ).toBeVisible();
  await expect(
    visibleCards(live).first(),
    'opening the header must bring the tool cards back',
  ).toBeVisible({ timeout: RENDER_MS });
  await expect(
    visibleCards(live).filter({ hasText: TOOL_DONE }).first(),
    'a folded call is a settled one, so at least one card behind the header reads as done',
  ).toBeVisible({ timeout: RENDER_MS });
  await openVisibleCards(live);
  await expect(
    live,
    'an opened card must carry the real tool output, not a placeholder',
  ).toContainText(toolOutputMarker, { timeout: RENDER_MS });

  // ── 2. The label is asked for once, by the browser, for this response. ────
  const observed = await summaryRequest;
  expect(
    new URL(observed.url()).pathname,
    'the console asks for the label of the response it is rendering',
  ).toBe(apiPath(`/sessions/${sessionId}/messages/${messageId}/process-summary`));
  const summaryReply = await summaryResponse;
  expect(summaryReply.status(), await summaryReply.text()).toBe(200);
  const summary = await settledSummary(api, sessionId, messageId);
  test.info().annotations.push({
    type: 'e2e_process_summary_status',
    description: `${summary.status}: ${String(summary.summary ?? summary.error ?? '')}`,
  });
  // This is the success path, and it accepts only success: a label that the
  // title model, its credential, or the row store could not produce is a red
  // here, not a fallback to the generic title. A failed label has its own
  // treatment (the generic title, never the error text), but proving that
  // needs a fault injected on purpose, not a model failure taken as one.
  expect(
    summary.status,
    `the platform must write a real label for this response; it answered ${JSON.stringify(summary)}`,
  ).toBe('completed');
  const generated = String(summary.summary ?? '').trim();
  expect(generated, 'a completed label must not be empty').not.toBe('');
  expect(generated, 'a label is a title, not the generic placeholder').not.toMatch(GENERIC_PROCESS_TITLE);
  // The open page keeps asking while the row is claimed (LazyProcessBlock and
  // MessageBubble both poll at 1s), so once the row settles the header this
  // reader is looking at has to settle on the label.
  await expect(
    liveTrigger,
    'the header on the open page takes the label the platform wrote',
  ).toHaveAccessibleName(generated, { timeout: SUMMARY_MS });
  await closeProcess(live);

  // ── 3. A cold page has only `process_details`, and must ask once. ─────────
  // The fault is armed before the reload: the first detail read of the new page
  // answers 500, and the reader has to be able to get past it.
  let detailAttempts = 0;
  const matchesDetails = (url: URL) => url.pathname.startsWith(detailsPrefix);
  const faultFirstDetailRead = async (route: Route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    detailAttempts += 1;
    if (detailAttempts === 1) {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: INJECTED_FAULT,
      });
      return;
    }
    await route.continue();
  };
  await page.route(matchesDetails, faultFirstDetailRead);
  try {
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: RENDER_MS });
    const cold = page.locator(`[data-message-id="${messageId}"]`);
    await expect(
      cold.getByTestId('assistant-turn-process'),
      'the reloaded page must fold the same response',
    ).toHaveCount(1, { timeout: RENDER_MS });
    await expect(
      cold.getByTestId('assistant-text').filter({ hasText: answerMarker }).first(),
      'and must still carry its conclusion',
    ).toBeVisible();
    await expect(
      cold.getByTestId('assistant-turn-process'),
      'a cold header identifies the block a detail read reopens',
    ).toHaveAttribute('data-process-block-id', /.+/);
    expect(
      detailAttempts,
      'a folded header must cost no detail read until the reader opens it',
    ).toBe(0);

    const coldTrigger = cold.getByTestId('assistant-turn-process-trigger');
    // The cold page has only `process_details.summary` to name the header
    // with: the same label, read back from the row the live page wrote.
    await expect(
      coldTrigger,
      'the generated label survives the reload as the header title',
    ).toHaveAccessibleName(generated, { timeout: RENDER_MS });

    // The failed read and the recovery from it.
    await coldTrigger.click();
    const note = cold.getByRole('alert');
    await expect(
      note,
      'a failed detail read must say so where the reader is looking',
    ).toBeVisible({ timeout: RENDER_MS });
    await expect(note, 'and must name what could not be loaded').toHaveText(DETAILS_FAILED);
    await note.getByRole('button', { name: RETRY_BUTTON }).click();
    await expect(
      cold.getByTestId('process-block-details'),
      'the retry must reach the real endpoint and render the work',
    ).toBeVisible({ timeout: RENDER_MS });

    await openProcess(cold);
    await expect(visibleCards(cold).first()).toBeVisible({ timeout: RENDER_MS });
    expect(
      await visibleCards(cold).count(),
      `the reopened header must hold every folded call (${durableToolIds.length})`,
    ).toBeGreaterThanOrEqual(durableToolIds.length);
    await openVisibleCards(cold);
    await expect(
      cold,
      'the detail read carries the real tool output, not a summary of it',
    ).toContainText(toolOutputMarker, { timeout: RENDER_MS });
    // A reasoning card is the engine's choice; assert its shape only when the
    // engine produced one, and record which way it went.
    const reasoning = cold.getByTestId('reasoning-part');
    const reasoningCount = await reasoning.count();
    test.info().annotations.push({
      type: 'e2e_process_reasoning_cards',
      description: String(reasoningCount),
    });
    if (reasoningCount > 0) {
      await expect(
        reasoning.first(),
        'reasoning folded into the header must come back with it',
      ).toBeVisible();
    }
    // Counted at the route, which sees every attempt, and confirmed against
    // the statuses the page was served. One expand is one read.
    expect(
      detailAttempts,
      'one failed read, one real read — and no repeat behind them',
    ).toBe(2);
    expect(
      detailStatuses.filter((status) => status === 200),
      'exactly one detail read reached the real endpoint',
    ).toEqual([200]);

    // ── 4. The second expand is served from what the page already holds. ────
    await closeProcess(cold);
    await openProcess(cold);
    await expect(visibleCards(cold).first()).toBeVisible({ timeout: RENDER_MS });
    expect(
      detailAttempts,
      'reopening a header the page has already read must not ask again',
    ).toBe(2);
    expect(detailStatuses.filter((status) => status === 200)).toEqual([200]);
  } finally {
    await page.unroute(matchesDetails, faultFirstDetailRead);
  }

  // A token-only viewer reads the very same saved label and folded tool work.
  // Comparing both pages catches a separate renderer that silently loses it.
  const platform = new PlatformApi(request);
  const share = await platform.createShare(sessionId);
  expect(share.token).toBeTruthy();
  const viewer = await browser.newContext();
  try {
    const sharedPage = await viewer.newPage();
    const privateCalls: string[] = [];
    const sharedDetails: string[] = [];
    sharedPage.on('request', (call) => {
      const path = new URL(call.url()).pathname;
      if (path.startsWith(apiPath('/sessions/')) || (
        path.startsWith(apiPath('/share/')) && call.method() !== 'GET'
      )) privateCalls.push(`${call.method()} ${path}`);
      if (path.startsWith(apiPath(`/share/${share.token}/history-blocks/`))) {
        sharedDetails.push(call.url());
      }
    });
    await sharedPage.goto(appPath(`/share/${share.token}`));
    const sharedTurn = liveTurn(sharedPage, answerMarker);
    await expect(sharedTurn.getByTestId('assistant-turn-process-trigger'))
      .toHaveAccessibleName(generated, { timeout: RENDER_MS });
    await expect(sharedPage.getByTestId('composer-prompt')).toHaveCount(0);
    await openProcess(sharedTurn);
    await openVisibleCards(sharedTurn);
    await expect(sharedTurn).toContainText(toolOutputMarker, { timeout: RENDER_MS });
    expect(sharedDetails, 'one token-authorized read opens the folded work').toHaveLength(1);
    await closeProcess(sharedTurn);
    await openProcess(sharedTurn);
    expect(sharedDetails, 'shared readers reuse the same details cache').toHaveLength(1);
    expect(privateCalls, 'a public reader neither calls owner APIs nor generates summaries').toEqual([]);

    await platform.revokeShare(sessionId);
    const deniedDetails = await viewer.request.get(sharedDetails[0]);
    expect(deniedDetails.status(), 'revocation closes details as well as the page').toBe(404);
    expect((await deniedDetails.json()).code).toBe('SHARE_NOT_FOUND');
    await sharedPage.reload();
    await expect(sharedPage.getByTestId('assistant-message')).toHaveCount(0);
    await expect(sharedPage.getByText(/Can't open this share|无法打开分享/)).toBeVisible();
  } finally {
    await viewer.close();
  }
  await test.step('real title and process-summary responses contain no reasoning', async () => {
    const output = execFileSync('docker', [
      'exec', '-i', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-',
    ], {
      input: readFileSync(resolve(__dirname, '../fixtures/platformLabels.py'), 'utf8'),
      encoding: 'utf8',
      timeout: 45_000,
    });
    await test.info().attach('platform-label-responses', { body: output, contentType: 'text/plain' });
    expect(output).toContain('PLATFORM_LABELS_NO_REASONING_PASS');
  });
});

test('an interrupted tool turn folds with its stop reason and keeps the failed call visible', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const label = `PROCESS_STOP_${runId}`;
  const firstMarker = `PROCESS_STOP_FIRST_${runId}`;
  const doneMarker = `PROCESS_STOP_DONE_${runId}`;

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId, READY_MS);
  const armed = await api.setPermissionMode(sessionId, 'bypassPermissions');
  expect(armed.permission_mode, 'the stopped turn must enter its tool unattended').toBe(
    'bypassPermissions',
  );

  await openSessionView(page, sessionId);
  // Two commands, in order, because both halves of the shape under test need
  // one: the first settles and is what the header stands for, the second is
  // still running when the reader presses stop and is what has to stay outside
  // it. A turn with only the long command folds nothing at all.
  await sendPrompt(
    page,
    sessionId,
    [
      label,
      'Run these two shell commands with your command tool, one at a time, in order:',
      '```',
      `echo ${firstMarker}`,
      'sleep 300',
      '```',
      `Do not use any other tool. After both return, reply with exactly ${doneMarker}.`,
    ].join('\n'),
  );

  // Two cards mean the second command is under way, which is the moment there
  // is something to interrupt. They are counted rather than waited on as
  // visible: a live group starts closed and keeps its cards mounted and
  // hidden (MessageParts.tsx:562), so nothing is on screen to wait for.
  const cards = page.locator('[data-tool-call-id]');
  try {
    // Polled for "at least two" rather than asserted as exactly two: an engine
    // that reaches for a third tool would otherwise fail here for being fast.
    await expect
      .poll(() => cards.count(), { timeout: TOOL_START_MS })
      .toBeGreaterThanOrEqual(2);
  } catch {
    const latest = await api
      .waitForAssistantMessageMatching(sessionId, 0, () => true, RENDER_MS)
      .catch(() => null);
    throw new Error(
      `the engine did not enter a second tool call within ${TOOL_START_MS}ms, so ` +
        `there was nothing to interrupt; it drove: ${toolsDriven(latest)}. Re-shape ` +
        'the prompt here rather than anywhere else, and do not convert this into a ' +
        'runtime skip: the budget reporter kills the whole process group on a skipped ' +
        'result (playwright-test-budget.cjs:88-110) and would end the lane instead of ' +
        'costing this spec.',
    );
  }

  // The reader's own control, not the API: what is under test is the shape the
  // console settles into after the person on the page presses stop.
  await page.getByTestId('run-composer-stop').click();
  await expect(
    page.getByTestId('run-composer-stop'),
    'the stopped turn must reach a verdict inside its budget',
  ).toHaveCount(0, { timeout: TURN_SETTLE_MS });
  const stopped = await api.waitForSession(
    sessionId,
    (session) => !String(session.current_turn_id || '').trim(),
    TURN_SETTLE_MS,
  );
  const record = await api.waitForAssistantMessageMatching(
    sessionId,
    0,
    (message) => toolUseIds(message).length > 0,
    RENDER_MS,
  );
  const messageId = String(record.message_id);
  const settledCalls = settledToolIds(record);
  const failedCalls = failedToolIds(record);
  const resultlessCalls = resultlessToolIds(record);
  test.info().annotations.push({
    type: 'e2e_stopped_turn_status',
    description: `${String(stopped.last_turn_status ?? '')} message=${messageId} ` +
      `available=${settledCalls.length} failed_or_denied=${failedCalls.length} ` +
      `no_result=${resultlessCalls.length} tools=${toolsDriven(record)}`,
  });
  expect(
    settledCalls.length,
    'the stopped response must carry one call that finished, or there is nothing to fold',
  ).toBeGreaterThan(0);
  // The shape under test is a stop that lands on a running call, which the
  // platform records as a `tool_use` with no `tool_result`. An engine that
  // answered the stop with a failed result instead is named here and fails
  // this spec: that is a different scene, and the fold's rule for it is
  // covered by `failedCalls` only as an addition to this one.
  expect(
    resultlessCalls.length,
    'the stop must land on a running call and leave it without a result; the record ' +
      `holds available=${settledCalls.length} failed_or_denied=${failedCalls.length} ` +
      `no_result=${resultlessCalls.length} (${toolsDriven(record)})`,
  ).toBeGreaterThan(0);
  const outsideCalls = [...resultlessCalls, ...failedCalls];

  // An interruption folds the work that ran to completion and leaves the calls
  // that reported a failure outside — `history_blocks.py::process_atoms` folds
  // a call away only while its result is `output-available`, because nothing
  // came after a failed one to explain what happened.
  //
  // Both stages assert the same shape and reach it by different routes: the
  // open page folds from the parts it streamed (`MessageParts.tsx`
  // `isProcessPart` / `canCollapseTurn`), the reloaded page from the projection
  // above. A red on one stage alone is those two disagreeing.
  //
  // The single-header assertion is the one most likely to catch that
  // disagreement, and it is deliberate: `canCollapseTurn`
  // (MessageParts.tsx:292) is computed from the parts of the record it is
  // given, and a reloaded record already carries its fold as a
  // `data-process-block` part. Wrapping a second header around one the server
  // already folded shows the reader two nested "Process" rows for one turn.
  const expectStoppedShape = async (turn: Locator, stage: string) => {
    await expect(
      turn.getByTestId('assistant-turn-process'),
      `${stage}: a stopped tool response folds into exactly one header — two means the ` +
        'page folded a record the server had already folded',
    ).toHaveCount(1, { timeout: RENDER_MS });
    await expect(
      turn.getByTestId('assistant-turn-process-trigger'),
      `${stage}: the fold starts closed`,
    ).toHaveAttribute('aria-expanded', 'false');
    await expect(
      turn,
      `${stage}: a stopped turn has to say that it was stopped`,
    ).toContainText(TURN_STOPPED);
    const fold = turn.getByTestId('assistant-turn-process');
    for (const toolCallId of outsideCalls) {
      const card = turn.locator(`[data-tool-call-id="${toolCallId}"]`);
      await expect(
        card,
        `${stage}: call ${toolCallId} never ran to completion, so its card stays where ` +
          'a reader sees it without opening anything',
      ).toBeVisible({ timeout: RENDER_MS });
      await expect(
        fold.locator(`[data-tool-call-id="${toolCallId}"]`),
        `${stage}: call ${toolCallId} is outside the fold, not merely rendered somewhere inside it`,
      ).toHaveCount(0);
    }
    for (const toolCallId of settledCalls) {
      await expect(
        turn.locator(`[data-tool-call-id="${toolCallId}"]`),
        `${stage}: call ${toolCallId} ran to completion, so it belongs behind the header`,
      ).toBeHidden();
      await expect(
        fold.locator(`[data-tool-call-id="${toolCallId}"]`),
        `${stage}: call ${toolCallId} is behind the header, not dropped from the page`,
      ).toHaveCount(1);
    }
  };

  // The open page is read through the newest bubble rather than through
  // `messageId`: what a live row is keyed by is the stream's business, and the
  // durable id is only guaranteed to address a row on the reloaded page.
  await expectStoppedShape(page.getByTestId('assistant-message').last(), 'live');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: RENDER_MS });
  const cold = page.locator(`[data-message-id="${messageId}"]`);
  await expectStoppedShape(cold, 'reloaded');
  await expect(
    cold.getByTestId('assistant-turn-process'),
    'reloaded: the fold is the lazy header, opened from the checkpoint it carries',
  ).toHaveAttribute('data-process-block-id', /.+/);
});
