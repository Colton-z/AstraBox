/**
 * E2E: the tool card names its state in the product's words, and the file it
 * wrote reaches the diff panel.
 *
 * Three failures live on this path and not one of them raises anything. They
 * are all "the markup rendered, and it was wrong".
 *
 * `ToolPartHeader` exists only because the vendored `ToolHeader` prints the raw
 * part state in English. What belongs on the card is the label
 * `getToolStateLabelKey` derives — and that derivation ends in a passthrough
 * (`return String(state)`) for a state it does not recognise, which `t()` then
 * renders verbatim. So both ways of losing it look identical to every other
 * gate: a reader sees `Completed` (the vendored word) or `output-available`
 * (the SDK's) where the product says "Done". `tsc` sees a string, `vitest` sees
 * a string, the build is clean.
 *
 * The chevron is the same silence one attribute down. Base UI marks the
 * collapsible ROOT `data-open` and the TRIGGER `data-panel-open`; the Radix
 * spelling `data-[state=open]` matched neither and the arrow simply never
 * turned. A `group-*` variant that matches nothing throws nothing — so this
 * asserts the attribute the chevron on THIS card actually reads, off the
 * element that actually carries the `group` class, rather than one spelling
 * for both card kinds.
 *
 * `DiffPanel`'s file rows were `div`s with an `onClick` and a `cursor-pointer`:
 * a press no Tab could reach and no screen reader was told about. A spec that
 * clicks with the mouse passes on exactly that, so the row is driven from the
 * keyboard here. The `+N` is read through its mint class AND its computed
 * colour, because a class that survives `tsc`, `vitest` and jsdom and dies in
 * the stylesheet is the failure mode this branch was opened for.
 *
 * The `pageerror` guard is the part that generalises: `<Ansi>` reached the
 * terminal panel as a module namespace rather than a component and blanked the
 * page on its first line of output, in a BUILD only. Tool cards, diff lines and
 * the vendored code block are the same kind of import on the same page.
 *
 * This is the one spec in the suite that depends on the model reaching for a
 * tool. When it does not, this FAILS and names what it did instead — it does
 * not `test.skip()`, because a runtime skip SIGTERMs the whole Playwright
 * process group (playwright-test-budget.cjs:48-70) and would end the lane
 * rather than cost one spec.
 */
import { test, expect, type Locator } from '@playwright/test';

import { AstraApi, visibleMessages } from '../fixtures/astraApi';
import { openLiveProcessGroup, revealAssistantProcess } from '../fixtures/assistantProcess';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

// The three waits this spec owns. Deliberately not env-tunable, the same way
// the approval specs pin their render window: they are budget arithmetic, not
// deployment tuning. 60 + 65 + 35 = 160s, which leaves the page open and the
// clicking inside the immovable 180s cap — so a stall in a stage this spec
// owns fails with that stage's message instead of as a bare test timeout with
// nothing to read. Every one is many times its measured cost on the campaign
// testbed: cold Docker sandbox ~11s, send-to-tool-card ~4s, turn tail ~5s.
// READY_MS therefore overrides waitForSessionReady's own 180s default, which
// alone equals the whole test budget.
const READY_MS = 60_000;
const TOOL_CARD_MS = 65_000;
const TURN_SETTLE_MS = 35_000;

// chat:tool_state.completed. Both locales, for the reason
// The console is bilingual and the runner sets no locale, so the browser's
// choice decides. What is under test is that the
// badge says one of THESE and not `Completed` (ai-elements/tool.tsx
// `statusLabels['output-available']`) or the raw `output-available`.
const TOOL_DONE = /^(Done|已完成)$/;
// misc:diff.files_changed at count 1 (en has the plural forms, zh one key).
const ONE_FILE_CHANGED = /1 file changed|1 个文件已变更/;
// The whole of the Diff panel's header row: the count, then the mint total.
// Anchored, so it is an assertion about that row rather than about any element
// that happens to contain it.
const DIFF_HEADER_LINE = /^(1 file changed|1 个文件已变更) \+\d+$/;
// A tool card is a collapsible whose HEADER carries a state badge. The
// reasoning card beside it is a collapsible too and its trigger has none; the
// plan card has a badge but outside its trigger. Both card kinds this spec can
// meet — the generic `Tool` and the `Task` a file change renders as — put the
// badge inside the trigger.
const TOOL_STATE_BADGE = '[data-slot="collapsible-trigger"] [data-slot="badge"]';

/**
 * The tools the engine actually drove, off the durable message blocks.
 *
 * `tool_use.name` is the field `blocksToSDKParts` turns into the transcript's
 * tool cards, so this is the same choice the page is rendering. Diagnostic
 * only: what the model reached for is the model's business, what the CARD says
 * about it is this spec's. It never throws — a broken read here must not
 * replace the failure it is describing.
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

const sessions = trackSessions();

test('a completed tool call reads Done, opens on its header, and lands in the diff panel', async ({
  page,
  request,
}) => {
  const uncaught: string[] = [];
  // Before the first navigation: the crash class this guards against happens
  // during a render, and a listener attached afterwards would miss the one that
  // mattered.
  page.on('pageerror', (error) => uncaught.push(error.message));

  const api = new AstraApi(request);
  const runId = Date.now();
  const marker = `E2E tool card ${runId}`;
  const fileName = `e2e-tool-${runId}.txt`;
  const contentMarker = `TOOLCARD-${runId}`;
  // The row and the pane below it both name the file; the pane shows the full
  // path and the row the last three segments, so both end with the file name.
  const FILE_TAIL = new RegExp(`e2e-tool-${runId}\\.txt$`);

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  expect(sessionId, 'conversation created').toBeTruthy();

  await api.waitForSessionReady(sessionId, READY_MS);
  // No `setPermissionMode` here, unlike the approval specs: the conversation is
  // left in the `bypassPermissions` it was created with (astraApi.startConversation)
  // so the tool RUNS instead of parking on an approval card. This spec is about
  // what a settled tool call renders, and a gate in front of it would only ever
  // let it reach "Awaiting confirmation".
  await openSessionView(page, sessionId);

  // ── ACT: one write, asked for from the real composer, in the forcing shape
  //    the interaction specs already coax this deployment with — immediate,
  //    named tool, relative path in the working directory, one file, stop. The
  //    "tell me you are done, do not ask for confirmation" tail is this spec's:
  //    those specs want the turn to PARK, this one wants it to SETTLE. ────────
  await sendPrompt(
    page,
    sessionId,
    `${marker}：请立即使用 Write 工具，在当前工作目录下创建相对路径文件 ${fileName}，`
      + `文件内容只有一行 ${contentMarker}。只创建这一个文件，创建完成后直接告诉我已完成，`
      + `不要询问确认，不要做别的事。`,
  );
  await expect(
    page.getByTestId('user-message').filter({ hasText: marker }).last(),
    'the user bubble should render — proof the turn was dispatched from the page',
  ).toBeVisible({ timeout: 30_000 });

  const transcript = page.getByTestId('run-view').getByTestId('assistant-message');
  // The card root carries the call's own id. A collapsible-shaped locator would
  // also match the turn fold and the process group the card now sits inside.
  const toolCards = transcript.locator('[data-tool-call-id]');

  try {
    // A tool call lands inside the running turn's process group, which starts
    // closed, so the card is on screen only once the reader opens it. The group
    // is waited for on the card's own budget: opening it is the step that can be
    // instant, reaching for a tool is the step the model takes its time over.
    await expect(transcript.getByTestId('assistant-process').first())
      .toBeAttached({ timeout: TOOL_CARD_MS });
    await openLiveProcessGroup(page, { within: transcript });
    await expect(toolCards.first()).toBeVisible({ timeout: TOOL_CARD_MS });
  } catch {
    throw new Error(
      `no tool card reached the transcript within ${TOOL_CARD_MS}ms; the engine drove: `
        + `${await toolsTheModelDrove(api, sessionId)}. This file is the ONE place the suite's `
        + 'model dependence lives — re-shape the prompt here rather than anywhere else, and do '
        + 'not convert this into a runtime skip: playwright-test-budget.cjs:48-70 SIGTERMs the '
        + 'whole process group on a skipped result and would end the lane instead of costing '
        + 'this one spec.',
    );
  }

  // Settle before touching the card. A turn still streaming re-renders the
  // transcript under the locators, and `toolCards.last()` could bind to a card
  // that appeared between resolving it and pressing it.
  await expect(
    page.getByTestId('run-composer-stop'),
    'the turn must settle inside its budget — everything after this drives a static transcript',
  ).toHaveCount(0, { timeout: TURN_SETTLE_MS });
  // A parked turn also has no stop control, and it would reach the badge check
  // below as "Awaiting confirmation" — a confusing red for a legible cause. In
  // bypassPermissions nothing should gate; if the engine raised an interaction
  // anyway (AskUserQuestion is raised whatever the mode), say so here.
  await expect(
    page.getByTestId('pending-interaction-panel'),
    'a bypassPermissions turn must not park on an interaction — the tool should just run',
  ).toHaveCount(0);

  // A settled turn that ran tools folds its work behind one header, and folding
  // rebuilds the groups it folds — so the group opened above is closed again.
  // The card assertions below are about the CARD, so the fold in front of it is
  // opened first, the way a reader opens it, and waited for rather than assumed:
  // this turn ran a tool and then said it was done, which is exactly the shape
  // that folds.
  await expect(
    transcript.getByTestId('assistant-turn-process'),
    'a settled tool response must fold into one header the reader can open',
  ).toHaveCount(1, { timeout: TURN_SETTLE_MS });
  await revealAssistantProcess(page, { within: transcript });

  // The card under test is the one that names the file in its header: that is
  // the Edit/Write card, and Edit/Write is the only thing `computeFileChanges`
  // feeds the Diff panel from. Falling back to the last card keeps steps 1-2
  // driving something real when the model reached for a different tool; the
  // diff assertions below then fail and say which tool that was.
  const named = toolCards.filter({ hasText: fileName });
  const card: Locator = (await named.count()) > 0 ? named.last() : toolCards.last();
  const header = card.locator('[data-slot="collapsible-trigger"]').first();
  const badge = header.locator('[data-slot="badge"]').first();
  const panel = card.locator('[data-slot="collapsible-content"]');

  // ── 1. The badge speaks the product's vocabulary. ────────────────────────
  await expect(
    badge,
    'a settled tool call must read as Done — not the vendored "Completed", not the raw SDK state',
  ).toHaveText(TOOL_DONE);
  test.info().annotations.push({
    type: 'e2e_tool_card_header',
    description: (await header.innerText()).replace(/\s+/g, ' ').trim(),
  });
  test.info().annotations.push({
    type: 'e2e_tools_driven',
    description: await toolsTheModelDrove(api, sessionId),
  });

  // ── 2. The header is the disclosure. ─────────────────────────────────────
  // Base UI does not keep a closed panel mounted, so "collapsed" is countable.
  await expect(panel, 'a settled tool card starts collapsed').toHaveCount(0);
  await header.click();
  await expect(panel, 'pressing the card header must open it').toBeVisible();
  await expect(
    panel,
    'and the opened card must show what the tool was given to write',
  ).toContainText(contentMarker);
  await expect(card, 'the card must name the file the tool wrote').toContainText(fileName);

  // The chevron turns off a `group-*` variant, and the two card kinds put the
  // `group` class in different places: the `Task` TRIGGER for a file change
  // (whose chevron reads `group-data-panel-open`), the `Tool` ROOT for a
  // generic call (`group-data-open`). Assert the attribute this card's chevron
  // actually reads, off the element that actually carries the class — asserting
  // one spelling for both is how the arrow stopped turning in the first place.
  const headerIsTheGroup = await header.evaluate((el) => el.classList.contains('group'));
  const chevronGroup = headerIsTheGroup ? header : card;
  const openAttribute = headerIsTheGroup ? 'data-panel-open' : 'data-open';
  await expect(
    chevronGroup,
    `the chevron's group must carry ${openAttribute} while the card is open, or the arrow never turns`,
  ).toHaveAttribute(openAttribute);

  await header.click();
  await expect(panel, 'pressing the header again must close the card').toHaveCount(0);
  await expect(
    chevronGroup,
    `${openAttribute} must be gone once the card is closed`,
  ).not.toHaveAttribute(openAttribute);

  // ── 3. The write reaches the Diff panel. ─────────────────────────────────
  // "Diff"/"Diff (N)" is a hardcoded English literal in sessionCapabilities.ts
  // (getRightPanelTabLabel), unlike every sibling tab which reads a locale key
  // — locating it by text is reading the real string, but it is an i18n gap
  // worth reporting, and it is why this line has no zh alternative.
  const diffTab = page.getByTestId('run-view').getByRole('tab', { name: /^Diff\b/ });
  await expect(
    diffTab,
    'the tab must count the file the turn changed, not just say "Diff"',
  ).toHaveText('Diff (1)');
  await diffTab.click();

  const diffPanel = page
    .getByTestId('run-view')
    .locator('[data-slot="tabs-content"]')
    .filter({ hasText: ONE_FILE_CHANGED });
  // `.last()` throughout this block: a text match reaches the ancestors that
  // contain the text as well as the element that owns it, and the owner is the
  // deepest — therefore last in document order.
  const diffHeader = diffPanel.getByText(ONE_FILE_CHANGED).last();
  await expect(
    diffHeader,
    'clicking the Diff tab must reveal the panel, headed by what changed',
  ).toBeVisible();
  await expect(
    diffHeader,
    'the header must say one file changed and count its added lines',
  ).toHaveText(DIFF_HEADER_LINE);

  const addedTotal = diffHeader.locator('span').filter({ hasText: /^\s*\+\d+\s*$/ });
  // Locate the gain by its rendered role in the summary. The Tailwind token
  // name is an implementation detail; the contract is one coloured `+N` span
  // beside the changed-file count.
  await expect(
    addedTotal,
    'and the count must be the element the mint §7 assigns to gains is on',
  ).toHaveText(/^\s*\+\d+\s*$/);
  // The class is not the paint. A stylesheet that fails to define the utility
  // leaves the class on the element and every other gate green — jsdom loads no
  // CSS and tsc reads none — so the colour itself is the assertion.
  const [addedColour, headerColour] = await Promise.all([
    addedTotal.evaluate((el) => getComputedStyle(el).color),
    diffHeader.evaluate((el) => getComputedStyle(el).color),
  ]);
  expect(
    addedColour,
    'the mint utility must resolve to a colour of its own, not inherit the header text',
  ).not.toBe(headerColour);

  // The row is a button and sits in the sequential focus order. It was a div
  // with an onClick and a cursor-pointer: focusable by nothing, announced as
  // nothing, and a mouse-only assertion passes on exactly that. `tabIndex`
  // rather than a Tab walk from the tablist, because a walk measures the kit's
  // panel focus order and would go red for something that is not this row.
  const fileRows = diffPanel.getByRole('button');
  await expect(
    fileRows,
    'one changed file, one row — and a row that is a button, not a div with an onClick',
  ).toHaveCount(1);
  const fileRow = fileRows.first();
  await expect(fileRow, 'the row must name the file it selects').toContainText(fileName);
  await expect(fileRow, 'and must sit in the sequential focus order').toHaveJSProperty('tabIndex', 0);
  await fileRow.focus();
  await expect(fileRow, 'a div with an onClick cannot take focus; this row must').toBeFocused();

  // Pressed from the keyboard, which is the half that was missing. Be exact
  // about what this proves: with one changed file the row is ALREADY the
  // selection (DiffPanel falls back to files[0]), so the press cannot flip
  // anything and `aria-current` below would hold without it. The press is here
  // because a real button is what makes it possible at all. The role, tabIndex,
  // focus, and pageerror assertions carry that contract.
  await page.keyboard.press('Enter');
  await expect(
    fileRow,
    'the selected row must be marked as the current one',
  ).toHaveAttribute('aria-current', 'true');

  // The pane below the list, not the row above it. `.last()` picks the deeper,
  // later element when both name the file — but if the pane were missing it
  // would fall back to the row and pass, so the ancestry is checked rather than
  // assumed.
  const shownPath = diffPanel.getByText(FILE_TAIL).last();
  await expect(shownPath, 'the pane below the list must name the file it is showing').toBeVisible();
  expect(
    await shownPath.evaluate((el) => el.closest('button') !== null),
    'the file path under test must be the diff pane\'s heading, not the list row',
  ).toBe(false);
  await expect(
    diffPanel.locator('div', { hasText: contentMarker }).last(),
    'and must print the written line as an addition, + in the gutter',
  ).toHaveText(new RegExp(`^\\+\\s*${contentMarker}$`));

  // Leaving the panel releases its detailed diff; the cheap tab count and
  // selected file must still survive the next visible calculation.
  const originalDiffHeader = await diffHeader.innerText();
  await page.getByTestId('run-view').getByRole('tab', { name: /^(Files|文件)$/ }).click();
  await expect(diffHeader).not.toBeVisible();
  await expect(diffTab).toHaveText('Diff (1)');
  await diffTab.click();
  await expect(diffHeader).toHaveText(originalDiffHeader);
  await expect(fileRow).toHaveAttribute('aria-current', 'true');
  await expect(diffPanel.locator('div', { hasText: contentMarker }).last())
    .toHaveText(new RegExp(`^\\+\\s*${contentMarker}$`));

  // ── 4. Nothing threw on any of it. ───────────────────────────────────────
  expect(
    uncaught,
    `uncaught exception while rendering a tool call, its diff, or the diff panel:\n${uncaught.join('\n')}`,
  ).toEqual([]);
});
