/**
 * E2E: the Diff tab answers from the conversation's durable history, not from
 * the slice of that history the page happens to be holding.
 *
 * The right panel's changed-file count and the Diff panel's body are both
 * derived from one in-memory array: `useSessionRightPanelState` passes
 * `messages` to `changedFilePaths` for the tab label
 * (`useSessionRightPanelState.ts:47`) and to `computeFileChanges` for the panel
 * (`:36`), and `fileChanges.ts:6-21,25-53` reads the `Edit`/`Write` tool inputs
 * out of those parts. That array holds a window, not the conversation:
 * `useFirstPageMessages` requests 50 durable records
 * (`AUTHORITATIVE_HISTORY_PAGE_SIZE`, `useFirstPageMessages.ts:25`), and its
 * `[sessionId]` effect empties the window on mount, so a reload starts again
 * from the newest 50 with no client cache behind it.
 *
 * On a conversation long enough for an early file change to sit older than
 * that window, the tab and the panel therefore describe the window rather than
 * the conversation. Widening the window takes a reader's gesture:
 * `VirtualizedMessageList` gates `requestOlder` on an upward wheel, a
 * downward-dragging touch, or ArrowUp / PageUp / Home on the focused scroller
 * (`VirtualizedMessageList.tsx:56-61`, `:75-111`), and nothing pages older
 * history on mount, on tab open, or on a timer. What a user who reloads is
 * shown is a smaller count — or, with no change left inside the window, "No
 * file changes yet." on a conversation whose files the agent demonstrably
 * wrote.
 *
 * Two changes rather than one, on purpose: the second lands inside the window,
 * so a window-derived reading is an affirmatively wrong `Diff (1)` and "1 file
 * changed" rather than an empty panel, and no assertion weaker than exact
 * equality could pass against it. The same page reads `Diff (2)` one step
 * before the reload, and the early change is read back out of durable history
 * by paging with `before=`, so a red after the reload belongs to the view and
 * not to the transcript store.
 *
 * This spec issues no `page.mouse.wheel`, no touch gesture and no scrolling
 * keypress on the transcript anywhere — the three ways a reader widens the
 * window. A user who has to go looking for the count has already been told the
 * wrong one. The one keypress it does make lands on a Diff panel row, which
 * reaches no history gate.
 *
 * ── EXPECTED RED, AND WHERE ─────────────────────────────────────────────────
 * The reloaded tab reads `Diff (1)`. Both readers take the same window, so the
 * count and the panel body fail together, and the first assertion to see it is
 * the one after the reload: "the reloaded tab must count every file the
 * conversation changed, not the ones inside its window". Everything before it
 * is arrangement that rules out a different story — the open page reads
 * `Diff (2)` one step earlier, and the early change is read back out of durable
 * history by paging with `before=`. `playwright.config.ts:57` sets
 * `maxFailures: 1`, so this file stops the lane every round until the panel has
 * a durable source; it belongs in the same change as that source.
 *
 * ── THE RED THAT IS NOT THE DEFECT ──────────────────────────────────────────
 * The filler stage is the tight one, and its failure looks nothing like the
 * one above: it names the turn count it reached and the window it read. A
 * no-tool turn writes two durable records, so moving a change out of a 50-record
 * window costs roughly 25 of them inside `FILLER_WALL_MS`. Read that red as
 * turn cost on this deployment, not as the Diff tab.
 *
 * Engine: `fileChanges.ts` matches the literal tool names `Edit` and `Write`,
 * and nothing else feeds the Diff tab; `getSessionRightPanelCapabilities`
 * gives an Assistant session no Diff tab at all. This belongs on the
 * claude_code profile and an Agent conversation, the same placement as
 * a-tool-call-renders-a-card-a-badge-and-a-diff.exclusive.spec.ts. Like that
 * spec it must not `test.skip()` when the model declines a tool — a runtime
 * skip SIGTERMs the whole Playwright process group and would end the lane
 * rather than cost one spec — so a decline fails here and names the tools the
 * engine drove.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, visibleMessages, type MessagePage, type MessageRecord } from '../fixtures/astraApi';
import { insist } from '../fixtures/insist';
import { trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled, openSessionView, sendPrompt } from '../fixtures/sessionPage';

// The stage caps this spec owns. Deliberately not env-tunable, following
// a-tool-call-renders-a-card-a-badge-and-a-diff.exclusive.spec.ts: they are
// budget arithmetic rather than deployment tuning. Every stage is capped and
// the caps sum to 30 + 30 + 12 + 55 + 20 + 12 = 159s, which leaves the reload
// and its assertions inside the immovable 180s cap on the 15s `expect`
// default — so no stage can consume the wall, and a stall fails with that
// stage's message instead of as a bare test timeout. READY_MS overrides
// waitForSessionReady's own 180s default, which alone equals the whole budget.
// The filler wall is the tight one: twenty-five cheap turns are the expected
// cost of moving a change out of the window, not a multiple of it.
const READY_MS = 30_000;
const EARLY_EDIT_MS = 30_000;
const TURN_SETTLE_MS = 12_000;
const FILLER_WALL_MS = 55_000;
const FILLER_TURN_MS = 20_000;
const LATE_EDIT_MS = 20_000;

// The durable window is 50 records and a no-tool turn writes two of them, so
// roughly 25 filler turns move an early change out of it. The count is never
// hardcoded: resident and background messages add records to a turn, so the
// loop stops on the oracle below and these only bound it. Reading the window
// before the twentieth turn cannot succeed and would only spend API calls.
const FILLER_ORACLE_FROM = 20;
const MAX_FILLERS = 40;
// Four pages of 50 reach back past 200 records, well beyond the ~110 this
// spec writes; the bound turns a server that keeps answering `has_more` into a
// named failure rather than an unbounded walk.
const MAX_HISTORY_PAGES = 4;

// misc:diff.files_changed at count 2 (en has the plural forms, zh one key),
// then the mint total the panel header appends. Anchored, so this is an
// assertion about that row and not about any element containing it.
const TWO_FILES_CHANGED = /2 files changed|2 个文件已变更/;
const DIFF_HEADER_LINE = /^(2 files changed|2 个文件已变更) \+\d+$/;
// misc:diff.empty_title in both locales. On a conversation that wrote two
// files this sentence is a wrong statement, not a missing one.
const NO_FILE_CHANGES = /No file changes yet\.|还没有文件变更/;

const sessions = trackSessions();

/**
 * The file paths a set of durable records asked `Edit` or `Write` to change.
 *
 * Same choice the panel makes: `fileChanges.ts` matches those two tool names
 * and reads `file_path` or `path` out of the tool input, so an oracle that
 * read anything else would be measuring a different thing from the tab.
 */
function editedFilePaths(records: MessageRecord[]): string[] {
  const paths: string[] = [];
  for (const record of records) {
    for (const block of record.blocks ?? []) {
      if (String(block.type ?? '') !== 'tool_use') continue;
      const name = String(block.name ?? '').trim();
      if (name !== 'Edit' && name !== 'Write') continue;
      const input = (block.input ?? {}) as Record<string, unknown>;
      const filePath = String(input.file_path ?? input.path ?? '').trim();
      if (filePath) paths.push(filePath);
    }
  }
  return paths;
}

function namesFile(records: MessageRecord[], fileName: string): boolean {
  return editedFilePaths(records).some((path) => path.endsWith(fileName));
}

function oldestCreatedAt(records: MessageRecord[]): string {
  return records
    .map((record) => String(record.created_at ?? '').trim())
    .filter(Boolean)
    .sort()[0] ?? '';
}

/**
 * The tools the engine actually drove, off the durable first page.
 *
 * `tool_use.name` is the field the transcript turns into tool cards, so this
 * is the same choice the page renders. Diagnostic only, and it never throws: a
 * broken read here must not replace the failure it is describing.
 */
async function toolsTheModelDrove(api: AstraApi, sessionId: string): Promise<string> {
  const names = new Set<string>();
  try {
    for (const message of visibleMessages(await api.getMessages(sessionId))) {
      for (const block of message.blocks ?? []) {
        if (String(block.type ?? '') !== 'tool_use') continue;
        const name = String(block.name ?? '').trim();
        if (name) names.add(name);
      }
    }
  } catch (error) {
    return `<messages unreadable: ${String(error)}>`;
  }
  return [...names].sort().join(', ') || '(no tool_use block at all)';
}

/** Poll the durable first page until an Edit/Write names *fileName*. */
async function waitForChangedFile(
  api: AstraApi,
  sessionId: string,
  fileName: string,
  timeoutMs: number,
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const page = await api.getMessages(sessionId, 50);
    // The overlay carries an in-flight turn's blocks, so this sees the tool
    // call as soon as the engine makes it rather than after the turn settles.
    if (namesFile(visibleMessages(page), fileName)) return true;
    await new Promise((resolve) => setTimeout(resolve, 1_500));
  }
  return false;
}

/**
 * Ask until the engine writes *fileName*, and say what it drove instead.
 *
 * `insist`'s own message names the ask that was declined; the tools are read
 * here, after the failure, because a diagnostic built with the options object
 * would describe the conversation as it was before the first prompt was sent.
 * Asking again rather than skipping: a skip ends the round exactly as a
 * failure does, so the model's choice would decide the round.
 */
async function insistOnAChangedFile(
  api: AstraApi,
  sessionId: string,
  fileName: string,
  options: { ask: (attempt: number) => Promise<void>; what: string; budgetMs: number },
): Promise<void> {
  const probeMs = Math.floor(options.budgetMs / 2);
  try {
    await insist<true>({
      ask: options.ask,
      probe: async () => (
        (await waitForChangedFile(api, sessionId, fileName, probeMs)) ? true : null
      ),
      what: options.what,
      budgetMs: options.budgetMs,
      probeMs,
    });
  } catch (error) {
    throw new Error(
      `${(error as Error).message} The engine drove: ${await toolsTheModelDrove(api, sessionId)}. `
        + 'Re-shape the prompt rather than converting this into a runtime skip: a skipped result '
        + 'SIGTERMs the whole Playwright process group and ends the lane.',
    );
  }
}

/**
 * Walk durable history backwards until an Edit/Write names *fileName*.
 *
 * `getMessages` takes no cursor, so the older pages go through `data()` with
 * the `before=` the oldest `created_at` on the page supplies — the same cursor
 * the console pages with.
 */
async function findChangedFileInDurableHistory(
  api: AstraApi,
  sessionId: string,
  fileName: string,
): Promise<{ found: boolean; pages: number; records: number; oldest: string }> {
  let current = await api.getMessages(sessionId, 50);
  let pages = 0;
  let records = 0;
  let cursor = '';
  while (pages < MAX_HISTORY_PAGES) {
    pages += 1;
    records += current.messages.length;
    if (namesFile(current.messages, fileName)) {
      return { found: true, pages, records, oldest: oldestCreatedAt(current.messages) };
    }
    if (current.has_more !== true) break;
    const next = oldestCreatedAt(current.messages);
    expect(next, 'a page answering has_more=true must carry a pagination timestamp').not.toBe('');
    expect(
      cursor === '' || next < cursor,
      `the history cursor must move backwards (${next} after ${cursor})`,
    ).toBe(true);
    cursor = next;
    current = await api.data<MessagePage>(
      'GET',
      `/sessions/${sessionId}/messages?limit=50&before=${encodeURIComponent(cursor)}`,
    );
  }
  return { found: false, pages, records, oldest: cursor || oldestCreatedAt(current.messages) };
}

test('the Diff tab still counts every file the agent changed after a reload of a long conversation', async ({
  page,
  request,
}) => {
  const uncaught: string[] = [];
  // Attached before the first navigation: rebuilding the window on mount runs
  // `mergeLatestDurableRecords`, which raises SESSION_HISTORY_WINDOWS_DO_NOT_OVERLAP
  // rather than rendering, and an uncaught throw would otherwise reach the
  // assertions below as a blank panel.
  page.on('pageerror', (error) => uncaught.push(error.message));

  const api = new AstraApi(request);
  const runId = Date.now();
  const earlyFile = `e2e-diff-early-${runId}.txt`;
  const earlyMarker = `DIFF-EARLY-${runId}`;
  const lateFile = `e2e-diff-late-${runId}.txt`;
  const lateMarker = `DIFF-LATE-${runId}`;
  // The forcing shape the sibling diff spec coaxes this deployment with:
  // immediate, named tool, relative path in the working directory, one file,
  // and a settled turn rather than a confirmation question.
  const writeRequest = (fileName: string, contentMarker: string) => (
    `请立即使用 Write 工具，在当前工作目录下创建相对路径文件 ${fileName}，`
    + `文件内容只有一行 ${contentMarker}。只创建这一个文件，创建完成后直接告诉我已完成，`
    + `不要询问确认，不要做别的事。`
  );

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  expect(sessionId, 'conversation created').toBeTruthy();

  await api.waitForSessionReady(sessionId, READY_MS);
  // The conversation keeps the `bypassPermissions` startConversation gives it,
  // so both writes run instead of parking on an approval card.
  await openSessionView(page, sessionId);
  await expectComposerEnabled(page);

  const runView = page.getByTestId('run-view');
  const diffTab = runView.getByRole('tab', { name: /^Diff\b/ });

  // ── the early change ─────────────────────────────────────────────────────
  // Typed into the real composer, because this is the change the reader will
  // later be told never happened. Asking again rather than skipping: a skip
  // ends the round exactly as a failure does, so the model's choice would have
  // decided the round instead of the platform.
  await insistOnAChangedFile(api, sessionId, earlyFile, {
    ask: async (attempt) => {
      const prompt = writeRequest(earlyFile, earlyMarker);
      if (attempt === 1) {
        await sendPrompt(page, sessionId, prompt);
        return;
      }
      await api.postTurnInput(sessionId, prompt);
    },
    what: `the configured model never wrote ${earlyFile}, so there is no early file change to lose`,
    budgetMs: EARLY_EDIT_MS,
  });

  await expect(
    page.getByTestId('run-composer-stop'),
    'the early change turn must settle before its count is read',
  ).toHaveCount(0, { timeout: TURN_SETTLE_MS });
  await expect(
    page.getByTestId('pending-interaction-panel'),
    'a bypassPermissions turn must not park on an interaction — the write should just run',
  ).toHaveCount(0);

  // ── the count while the change is still inside the window ────────────────
  // The half of the impact that says the number was right while the user was
  // working: everything after this is the same conversation, read again.
  test.info().annotations.push({
    type: 'e2e_early_changed_paths',
    description: editedFilePaths(visibleMessages(await api.getMessages(sessionId, 50))).join(', '),
  });
  await expect(
    diffTab,
    'the tab must count the file the early turn changed while that turn is still in the window',
  ).toHaveText('Diff (1)');
  await diffTab.click();
  // Base UI mounts only the selected panel, so `aria-selected` on the tab is
  // what makes the single mounted panel the Diff panel and not the Files one.
  await expect(diffTab).toHaveAttribute('aria-selected', 'true');
  const livePanel = runView.locator('[data-slot="tabs-content"]');
  await expect(
    livePanel.getByRole('button').filter({ hasText: earlyFile }),
    'the panel must list the file the early turn changed',
  ).toHaveCount(1);

  // ── real turns until the early change is older than the window ───────────
  // Real normal inputs, no substituted responses and no imported rows: the
  // window is by record COUNT, so backdating cannot produce this state and a
  // truncated first page would stop proving the server's own page shape.
  const filled = await test.step('write real history until the early change leaves the durable window', async () => {
    const deadline = Date.now() + FILLER_WALL_MS;
    let sent = 0;
    let window: MessagePage | null = null;
    while (sent < MAX_FILLERS && Date.now() < deadline) {
      sent += 1;
      const prompt = `History note ${runId}-${String(sent).padStart(3, '0')}. `
        + 'Acknowledge briefly in one word; do not use tools.';
      const result = await api.sendTurn(sessionId, prompt, FILLER_TURN_MS).catch((error: unknown) => {
        throw new Error(`filler turn ${sent} did not settle within ${FILLER_TURN_MS}ms: ${String(error)}`);
      });
      expect(result.errorText, `filler turn ${sent} must complete without an engine error`).toBeNull();
      expect(result.text.trim(), `filler turn ${sent} must receive a real reply`).not.toBe('');
      if (sent < FILLER_ORACLE_FROM) continue;
      window = await api.getMessages(sessionId, 50);
      if (window.has_more === true && !namesFile(window.messages, earlyFile)) break;
    }
    return { sent, window };
  });

  const windowAfterFillers = filled.window;
  if (!windowAfterFillers) {
    throw new Error(
      `the filler loop stopped after ${filled.sent} turn(s) without reading the durable window: `
        + `a turn costs more than the ${FILLER_WALL_MS}ms this stage budgets for roughly 25 of them`,
    );
  }
  expect(
    windowAfterFillers.has_more === true && !namesFile(windowAfterFillers.messages, earlyFile),
    'the early change must be older than the page\'s 50-record window before a reload proves anything: '
      + `${filled.sent} filler turn(s) left ${windowAfterFillers.messages.length} record(s) on the first page, `
      + `has_more=${String(windowAfterFillers.has_more)}, oldest created_at=${oldestCreatedAt(windowAfterFillers.messages) || '<none>'}, `
      + `Edit/Write paths still on that page: ${editedFilePaths(windowAfterFillers.messages).join(', ') || '(none)'}`,
  ).toBe(true);

  // ── the late change, inside the window ───────────────────────────────────
  // This one stays in the reloaded window on purpose, so a window-derived
  // reading is a wrong non-zero count rather than an empty panel.
  // Posted rather than streamed: `insist` catches only what the probe raises,
  // so an `ask` that reads an SSE body to completion under a cap would leave
  // the lane a transport timeout instead of the named decline below.
  await insistOnAChangedFile(api, sessionId, lateFile, {
    ask: async () => { await api.postTurnInput(sessionId, writeRequest(lateFile, lateMarker)); },
    what: `the configured model never wrote ${lateFile}, so the reloaded window would hold no `
      + 'change at all and the broken reading would be an empty panel rather than a wrong number',
    budgetMs: LATE_EDIT_MS,
  });
  await api.waitForSession(
    sessionId,
    (session) => session.state === 'READY' && session.last_turn_status === 'COMPLETED',
    TURN_SETTLE_MS,
  );

  const windowAfterLate = await api.getMessages(sessionId, 50);
  expect(
    namesFile(windowAfterLate.messages, lateFile),
    'the late change must be inside the durable window the reload will load',
  ).toBe(true);
  expect(
    namesFile(windowAfterLate.messages, earlyFile),
    'the late change must not have pulled the early one back into the window',
  ).toBe(false);

  // ── the early change in durable history ──────────────────────────────────
  // Asserted before the reload, so a red at the tab is attributable to the
  // view rather than read as the platform having lost the write.
  const durable = await findChangedFileInDurableHistory(api, sessionId, earlyFile);
  expect(
    durable.found,
    `the early change must be readable from durable history: paged ${durable.pages} page(s) / `
      + `${durable.records} record(s) back to ${durable.oldest || '<none>'} without finding an `
      + `Edit or Write naming ${earlyFile}`,
  ).toBe(true);

  // ── the count on the page that never unloaded ────────────────────────────
  // The still-open page holds every record since the conversation started, so
  // both changes are in its window. A red here is the same defect found one
  // step earlier: the live window lost the early change before any reload.
  await expect(
    diffTab,
    'the open page must count both changed files before the reload',
  ).toHaveText('Diff (2)');

  // ── the same conversation, reloaded ──────────────────────────────────────
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(runView).toBeVisible();
  await expect(
    page.getByTestId('user-message').filter({ hasText: lateFile }).last(),
    'the reloaded first page must render the newest turn',
  ).toBeVisible();
  await expectComposerEnabled(page);

  // No wheel, no touch, no scroll: `requestOlder` is gated on an upward
  // gesture, and the count a reader is shown before they gesture is the
  // subject of this spec.
  await expect(
    diffTab,
    'the reloaded tab must count every file the conversation changed, not the ones inside its window',
  ).toHaveText('Diff (2)');
  await diffTab.click();
  await expect(diffTab).toHaveAttribute('aria-selected', 'true');

  const diffPanel = runView.locator('[data-slot="tabs-content"]');
  await expect(
    diffPanel,
    'one panel is mounted at a time, so this locator must resolve to the selected Diff panel',
  ).toHaveCount(1);
  // The header and the tab count read the same messages through different
  // gates — the body is computed only while the Diff tab is selected — so
  // asserting one leaves the other half uncovered. `.last()`: a text match
  // reaches the ancestors containing the text as well as the element owning
  // it, and the owner is the deepest.
  const diffHeader = diffPanel.getByText(TWO_FILES_CHANGED).last();
  await expect(
    diffHeader,
    'the panel must be headed by both changed files and their added lines',
  ).toHaveText(DIFF_HEADER_LINE);
  await expect(
    diffPanel.getByText(NO_FILE_CHANGES),
    'a conversation that wrote two files must not be told it has no file changes',
  ).toHaveCount(0);

  const fileRows = diffPanel.getByRole('button');
  await expect(fileRows, 'two changed files, two rows').toHaveCount(2);
  await expect(fileRows.filter({ hasText: earlyFile }), 'the early change must have a row')
    .toHaveCount(1);
  await expect(fileRows.filter({ hasText: lateFile }), 'the late change must have a row')
    .toHaveCount(1);

  // Driven from the keyboard, because the row is a button and sits in the
  // sequential focus order. Selecting the EARLY row proves the panel recovered
  // the change's content: a fix that listed paths from an index without the
  // tool input would reach this line with nothing to print.
  const earlyRow = fileRows.filter({ hasText: earlyFile }).first();
  await expect(earlyRow, 'the row must sit in the sequential focus order').toHaveJSProperty('tabIndex', 0);
  await earlyRow.focus();
  await expect(earlyRow, 'a row a Tab cannot reach is not a press').toBeFocused();
  await page.keyboard.press('Enter');
  await expect(earlyRow, 'the selected row must be marked as the current one')
    .toHaveAttribute('aria-current', 'true');
  await expect(
    diffPanel.locator('div', { hasText: earlyMarker }).last(),
    'the pane below the list must print the early change\'s line as an addition',
  ).toHaveText(new RegExp(`^\\+\\s*${earlyMarker}$`));

  expect(
    uncaught,
    `uncaught exception while rebuilding, merging or rendering the reloaded window:\n${uncaught.join('\n')}`,
  ).toEqual([]);
});
