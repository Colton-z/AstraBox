/**
 * E2E: the folded history read is the same transcript, paged by record, with
 * the work behind one addressable header.
 *
 * `useFirstPageMessages.ts:34` moved the console's history read from
 * `/sessions/{id}/messages` to `/sessions/{id}/history-blocks`, so this page is
 * now what a reader sees. Everything that can go wrong on it answers 200 with a
 * well-formed body: `history_blocks.py::project_record` can fold a record's
 * conclusion away with its work, `list_history_blocks_page`
 * (`session_message_view.py:748`) can lose a record between two pages or serve
 * the second page against a history the first one never saw, and
 * `get_session_history_block_details` can reopen a header at the wrong
 * checkpoint and hand back blocks that belong to a later turn. A record that
 * silently disappears between page one and page two is not visible in the
 * browser at all — the reader simply never scrolls to it.
 *
 * So the page is checked against the raw read of the same session: the same
 * records, in the same order, with the tool work removed from the page and
 * still present in `/messages` and in the detail read. The cursor is walked at
 * `limit=2`, which is the only way a three-turn session crosses a page boundary
 * at all, and the boundary is re-asked to prove the checkpoint pins it.
 *
 * The browser half is one assertion the API half cannot make: that the page the
 * console receives carries no tool output, and that the output appears only
 * after the reader opens the header. A body that still carried the folded work
 * would render identically.
 *
 * Engine independence: tool names differ per engine, so nothing here matches
 * `Bash` or `Read`. The tool turn is found by the `tool_use` blocks the durable
 * record carries, and cards are located by `data-tool-call-id`.
 *
 * This spec depends on the model reaching for a tool. When it does not, it
 * FAILS and names what the engine drove instead — it does not `test.skip()`,
 * because a runtime skip SIGTERMs the whole Playwright process group
 * (playwright-test-budget.cjs:48-70) and would end the lane rather than cost
 * one spec.
 */
import { expect, test, type Locator } from '@playwright/test';

import {
  AstraApi,
  messageText,
  type HistoryBlockPage,
  type MessageRecord,
} from '../fixtures/astraApi';
import { apiPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

// Budget arithmetic for the immovable 180s cap, not deployment tuning:
// 45 + 30 + 45 + 30 + 30 = 180 is the sum of the stage ceilings; each real
// turn here answers in a few seconds, so the reads and the browser fit.
// The three turns are driven through the API rather than the composer for the
// same reason — what is under test is the read afterwards, and a turn watched
// in the browser costs the budget twice.
const READY_MS = 45_000;
const QUICK_TURN_MS = 30_000;
const TOOL_TURN_MS = 45_000;
const RENDER_MS = 20_000;

/** How many pages a `limit=2` walk may take before it is looping. */
const MAX_PAGES = 12;

const sessions = trackSessions();

interface ProcessDetails {
  block_id: string;
  cursor: string;
  session_id: string;
  message_id: string;
  turn_id: string;
  tool_count: number;
  summarize: boolean;
  summary?: unknown;
}

function blocksOf(record: MessageRecord | null | undefined): Array<Record<string, unknown>> {
  return Array.isArray(record?.blocks) ? record.blocks : [];
}

function blocksOfType(record: MessageRecord | null | undefined, type: string): Array<Record<string, unknown>> {
  return blocksOf(record).filter((block) => String(block.type ?? '') === type);
}

function toolUseIds(record: MessageRecord | null | undefined): string[] {
  return blocksOfType(record, 'tool_use')
    .map((block) => String(block.id ?? '').trim())
    .filter(Boolean);
}

/** The headers this record carries in place of the work they stand for. */
function processDetails(record: MessageRecord | null | undefined): ProcessDetails[] {
  return blocksOfType(record, 'process_block')
    .map((block) => block.process_details as ProcessDetails | undefined)
    .filter((details): details is ProcessDetails => Boolean(details));
}

function messageIds(records: MessageRecord[]): string[] {
  return records.map((record) => String(record.message_id));
}

/**
 * The engine's own tool names off the durable records — diagnostic only.
 *
 * What the model reached for is the model's business; what the read does with
 * it is this spec's. It never throws, so a broken read here cannot replace the
 * failure it is describing.
 */
function toolsDriven(records: MessageRecord[]): string {
  const names = new Set<string>();
  for (const record of records) {
    for (const block of blocksOfType(record, 'tool_use')) {
      const name = String(block.name ?? '').trim();
      if (name) names.add(name);
    }
  }
  return [...names].sort().join(', ') || '(no tool_use block at all)';
}

/** Press a collapsible trigger that states its own state, and wait for it. */
async function ensureExpanded(trigger: Locator): Promise<void> {
  if ((await trigger.getAttribute('aria-expanded')) === 'true') return;
  await trigger.click();
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');
}

/**
 * Open every group and card inside an opened header.
 *
 * Three disclosures sit between a reader and a tool's output: the turn header,
 * the group, and the card itself. The first two keep their contents mounted and
 * hidden (`MessageParts.tsx:453`, `:553`); the card unmounts its body
 * (ai-elements/tool.tsx `ToolContent`), so the output is not in the page at all
 * until it is pressed.
 */
async function openEverythingInside(turn: Locator): Promise<void> {
  const groups = turn.getByTestId('assistant-process');
  await expect(
    groups.first(),
    'an opened header must show at least one group of the work it stands for',
  ).toBeVisible({ timeout: RENDER_MS });
  const groupCount = await groups.count();
  for (let index = 0; index < groupCount; index += 1) {
    await ensureExpanded(groups.nth(index).getByTestId('assistant-process-trigger'));
  }
  const cards = turn.locator('[data-tool-call-id]:visible');
  await expect(
    cards.first(),
    'the work behind the header has to include the tool cards it folded',
  ).toBeVisible({ timeout: RENDER_MS });
  const cardCount = await cards.count();
  for (let index = 0; index < cardCount; index += 1) {
    await ensureExpanded(cards.nth(index).getByRole('button').first());
  }
}

test('the folded history pages by record and carries its tool work only on demand', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const markerOne = `HISTORY_BLOCKS_ONE_${runId}`;
  const markerTwo = `HISTORY_BLOCKS_TWO_${runId}`;
  const answerMarker = `HISTORY_BLOCKS_ANSWER_${runId}`;
  const toolOutputMarker = `HISTORY_BLOCKS_OUTPUT_${runId}`;
  const sourceName = `history-blocks-source-${runId}.txt`;
  const sourcePath = `/workspace/${sourceName}`;

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId, READY_MS);
  const armed = await api.setPermissionMode(sessionId, 'bypassPermissions');
  expect(armed.permission_mode, 'the tool turn must run its tool unattended').toBe(
    'bypassPermissions',
  );
  // Placed by the platform's own file API rather than by a first tool call, so
  // the text the detail read must carry is fixed before the model runs.
  await api.uploadFileText(sessionId, '/workspace', sourceName, `${toolOutputMarker}\n`);

  // ── ACT: three turns, with the one that ran a tool in the middle, so a
  //    `limit=2` walk has to carry it across a page boundary. ────────────────
  const first = await api.sendTurn(
    sessionId,
    `Reply with exactly ${markerOne} and nothing else. Do not use any tool.`,
    QUICK_TURN_MS,
  );
  expect(first.errorText, 'the first tool-free turn must answer').toBeNull();
  const toolTurn = await api.sendTurn(
    sessionId,
    [
      `Open the file ${sourcePath} with a tool and show its contents.`,
      `Then reply with exactly ${answerMarker} and nothing else.`,
      'Do not ask for confirmation.',
    ].join('\n'),
    TOOL_TURN_MS,
  );
  expect(toolTurn.errorText, 'the tool turn must answer').toBeNull();
  const last = await api.sendTurn(
    sessionId,
    `Reply with exactly ${markerTwo} and nothing else. Do not use any tool.`,
    QUICK_TURN_MS,
  );
  expect(last.errorText, 'the second tool-free turn must answer').toBeNull();
  await api.waitForAssistantMessageMatching(sessionId, 2, () => true, RENDER_MS);

  // The raw read is the control: the same durable records, unfolded.
  const raw = (await api.getMessages(sessionId, 50)).messages;
  const rawAssistants = raw.filter((record) => record.role === 'assistant');
  const toolRecord = rawAssistants.find((record) => toolUseIds(record).length > 0) ?? null;
  expect(
    toolRecord,
    'the folded page needs a response that ran a tool, and the engine drove: ' +
      `${toolsDriven(rawAssistants)}. Re-shape the prompt here rather than ` +
      'anywhere else, and do not convert this into a runtime skip: ' +
      'playwright-test-budget.cjs:48-70 SIGTERMs the whole process group on a ' +
      'skipped result and would end the lane instead of costing this one spec.',
  ).not.toBeNull();
  const toolMessageId = String(toolRecord?.message_id);
  const rawToolIds = toolUseIds(toolRecord);
  test.info().annotations.push({
    type: 'e2e_history_blocks_shape',
    description: `records=${raw.length} tools=${toolsDriven(rawAssistants)} ` +
      `calls=${rawToolIds.length}`,
  });

  // ── 1. The default page is the same transcript with the work removed. ────
  const folded = await api.getHistoryBlocks(sessionId);
  expect(folded.paging_mode, 'the console reads a block page, not a timestamp page').toBe('blocks');
  expect(
    messageIds(folded.messages),
    'the folded read must carry the same records, in the same order, as the raw read',
  ).toEqual(messageIds(raw));
  expect(folded.block_count, 'block_count counts what the page carries').toBe(
    folded.messages.length,
  );
  expect(folded.has_more, 'a whole short session fits on one default page').toBe(false);
  expect(folded.next_cursor ?? null, 'and a last page issues no cursor').toBeNull();
  for (const record of folded.messages) {
    expect(
      String(record.history_block_id ?? ''),
      `record ${record.message_id} must be addressable on the page it is shown on`,
    ).toBe(String(record.message_id));
  }

  const foldedTool = folded.messages.find(
    (record) => String(record.message_id) === toolMessageId,
  );
  const headers = processDetails(foldedTool);
  expect(headers, 'a settled tool response folds into exactly one header').toHaveLength(1);
  const header = headers[0];
  expect(header.summarize, 'a whole-response fold is the one a label is written for').toBe(true);
  expect(Number(header.tool_count), 'the header counts the calls it stands for').toBeGreaterThanOrEqual(1);
  expect(String(header.session_id)).toBe(sessionId);
  expect(String(header.message_id)).toBe(toolMessageId);
  expect(String(header.turn_id)).toBe(String(toolRecord?.turn_id));
  expect(String(header.block_id), 'a header a reader can open must be named').not.toBe('');
  expect(String(header.cursor), 'and must carry the checkpoint it was issued at').not.toBe('');
  expect(
    toolUseIds(foldedTool),
    'the folded page must not carry the calls it folded away',
  ).toEqual([]);
  expect(
    blocksOfType(foldedTool, 'tool_result'),
    'nor their results — that is the whole point of the fold',
  ).toHaveLength(0);
  expect(
    rawToolIds.length,
    'while the raw read still carries them for whoever wants the whole record',
  ).toBeGreaterThan(0);
  const conclusion = messageText(foldedTool as MessageRecord).trim();
  expect(conclusion, 'the conclusion stays on the page — folding it away would empty it').not.toBe('');
  expect(
    messageText(toolRecord as MessageRecord),
    'and it is the response\'s own words, not a rewrite of them',
  ).toContain(conclusion);
  for (const record of folded.messages) {
    if (String(record.message_id) === toolMessageId) continue;
    expect(
      processDetails(record),
      `record ${record.message_id} ran no tool, so it has nothing to fold`,
    ).toHaveLength(0);
  }

  // ── 2. Two records at a time: same order, no gaps, no repeats. ───────────
  const pages: HistoryBlockPage[] = [];
  let cursor: string | null = null;
  for (let guard = 0; guard < MAX_PAGES; guard += 1) {
    const walkedPage: HistoryBlockPage = await api.getHistoryBlocks(
      sessionId,
      2,
      cursor ?? undefined,
    );
    pages.push(walkedPage);
    expect(walkedPage.paging_mode).toBe('blocks');
    expect(
      walkedPage.messages.length,
      'a page of two must hold one or two records, never none and never more',
    ).toBeGreaterThan(0);
    expect(walkedPage.messages.length).toBeLessThanOrEqual(2);
    if (!walkedPage.has_more) {
      cursor = null;
      break;
    }
    const next = String(walkedPage.next_cursor || '');
    expect(next, 'a page with history behind it must issue the cursor for it').not.toBe('');
    expect(next, 'and that cursor has to move, or the reader pages forever').not.toBe(cursor);
    cursor = next;
  }
  expect(
    cursor,
    `the walk did not reach the start of the session within ${MAX_PAGES} pages`,
  ).toBeNull();
  // Pages arrive newest-first; the transcript reads the other way.
  const walked = pages
    .slice()
    .reverse()
    .flatMap((walkedPage) => messageIds(walkedPage.messages));
  expect(
    walked,
    'walking the cursor must rebuild the raw transcript exactly — no gap, no repeat',
  ).toEqual(messageIds(raw));
  expect(new Set(walked).size, 'and must not hand the same record out twice').toBe(walked.length);

  const pagesHoldingTheToolTurn = pages.filter((walkedPage) => (
    messageIds(walkedPage.messages).includes(toolMessageId)
  ));
  expect(
    pagesHoldingTheToolTurn,
    'a tool turn is one record, so a page boundary can never cut through it',
  ).toHaveLength(1);
  const walkedTool = pagesHoldingTheToolTurn[0].messages.find(
    (record) => String(record.message_id) === toolMessageId,
  );
  expect(toolUseIds(walkedTool), 'the walked page folds it the same way').toEqual([]);
  expect(processDetails(walkedTool).map((item) => item.block_id)).toEqual([header.block_id]);

  // The checkpoint is what makes the second page belong to the first read.
  // Re-asking on a still transcript proves nothing — a read with no checkpoint
  // at all answers the same — so a real turn is written first, and only then
  // is the old cursor asked again: it must answer the page it answered before,
  // while a fresh read must show the new records that page must not.
  expect(pages.length, 'six records at two per page must take more than one page').toBeGreaterThan(1);
  const boundaryCursor = String(pages[0].next_cursor || '');
  const laterMarker = `HISTORY_BLOCKS_LATER_${runId}`;
  const later = await api.sendTurn(
    sessionId,
    `Reply with exactly ${laterMarker} and nothing else. Do not use any tool.`,
    QUICK_TURN_MS,
  );
  expect(later.errorText, 'the turn written after the read must answer').toBeNull();
  await api.waitForAssistantMessageMatching(sessionId, 3, () => true, RENDER_MS);
  const fresh = await api.getHistoryBlocks(sessionId);
  expect(
    fresh.messages.length,
    'a fresh read after the new turn carries its two new records',
  ).toBe(raw.length + 2);
  expect(
    fresh.messages.some((record) => record.role === 'user' && String(record.content).includes(laterMarker)),
    'and the new input is among them',
  ).toBe(true);
  expect(
    messageIds(fresh.messages).slice(0, raw.length),
    'the records the first read saw keep their order in front of the new ones',
  ).toEqual(messageIds(raw));
  const reasked = await api.getHistoryBlocks(sessionId, 2, boundaryCursor);
  expect(
    messageIds(reasked.messages),
    're-asking for a page at the old cursor must answer with the same records, after the transcript grew',
  ).toEqual(messageIds(pages[1].messages));
  expect(String(reasked.next_cursor || '')).toBe(String(pages[1].next_cursor || ''));
  expect(reasked.has_more).toBe(pages[1].has_more);
  expect(
    messageIds(reasked.messages).some((id) => !messageIds(raw).includes(id)),
    'the pinned page never leaks a record written after its checkpoint',
  ).toBe(false);

  const bogus = await request.get(
    apiPath(`/sessions/${sessionId}/history-blocks?limit=2&before=not-a-cursor`),
  );
  expect(
    bogus.status(),
    'a cursor this service did not write must be refused, not served from the tail',
  ).toBe(400);
  expect(String(((await bogus.json()) as { code?: string }).code)).toBe('INVALID_REQUEST');

  // ── 3. The detail read is the only place the folded work comes back. ─────
  const detailRoute = `/sessions/${sessionId}/history-blocks/${encodeURIComponent(header.block_id)}`;
  const opened = await api.data<{ messages: MessageRecord[]; has_more: boolean }>(
    'GET',
    `${detailRoute}?cursor=${encodeURIComponent(header.cursor)}`,
  );
  expect(opened.messages, 'a header stands for one record\'s work').toHaveLength(1);
  expect(opened.has_more, 'and a header\'s contents are whole').toBe(false);
  expect(
    toolUseIds(opened.messages[0]),
    'the reopened header must carry exactly the calls the raw record holds',
  ).toEqual(rawToolIds);
  expect(String(opened.messages[0].history_block_id)).toBe(header.block_id);

  const withoutCursor = await request.get(apiPath(detailRoute));
  expect(
    withoutCursor.status(),
    'without a checkpoint the blocks would be read as the record stands now, not as the page folded them',
  ).toBe(400);
  const withPageCursor = await request.get(
    apiPath(`${detailRoute}?cursor=${encodeURIComponent(boundaryCursor)}`),
  );
  expect(
    withPageCursor.status(),
    'a page cursor names a position, not a checkpoint, and must be refused as one',
  ).toBe(400);
  const unknownBlock = await request.get(
    apiPath(
      `/sessions/${sessionId}/history-blocks/${encodeURIComponent(`${toolMessageId}:p9999`)}`
      + `?cursor=${encodeURIComponent(header.cursor)}`,
    ),
  );
  expect(
    unknownBlock.status(),
    'a header the pinned record does not produce is missing, not empty — the two send a reader to different places',
  ).toBe(404);

  // ── 4. The browser receives a page with no tool work on it. ──────────────
  const historyPath = apiPath(`/sessions/${sessionId}/history-blocks`);
  const detailsPrefix = `${historyPath}/`;
  const detailReads: string[] = [];
  page.on('response', (response) => {
    if (response.request().method() !== 'GET') return;
    const path = new URL(response.url()).pathname;
    if (path.startsWith(detailsPrefix)) detailReads.push(path);
  });
  const firstRead = page.waitForResponse(
    (response) => (
      response.request().method() === 'GET'
      && new URL(response.url()).pathname === historyPath
    ),
    { timeout: RENDER_MS },
  );
  await openSessionView(page, sessionId);
  const firstResponse = await firstRead;
  expect(
    new URL(firstResponse.url()).searchParams.get('limit'),
    'the console asks for the page size its own history window is built on',
  ).toBe('50');
  expect(firstResponse.status(), await firstResponse.text()).toBe(200);
  const body = (await firstResponse.json()) as { data: HistoryBlockPage };
  expect(body.data.paging_mode).toBe('blocks');
  const delivered = body.data.messages.find(
    (record) => String(record.message_id) === toolMessageId,
  );
  expect(toolUseIds(delivered), 'the page the browser receives carries no calls').toEqual([]);
  expect(
    JSON.stringify(delivered ?? {}),
    'and none of the output they produced — a body that still carried it would render identically',
  ).not.toContain(toolOutputMarker);

  const row = page.locator(`[data-message-id="${toolMessageId}"]`);
  const turnHeader = row.getByTestId('assistant-turn-process');
  await expect(
    turnHeader,
    'the reader sees one header where the work was',
  ).toHaveCount(1, { timeout: RENDER_MS });
  await expect(
    turnHeader,
    'and it is the block the page named, so a detail read can reopen it',
  ).toHaveAttribute('data-process-block-id', header.block_id);
  expect(detailReads, 'a folded header costs no read until it is opened').toEqual([]);

  await ensureExpanded(row.getByTestId('assistant-turn-process-trigger'));
  await expect(
    row.getByTestId('process-block-details'),
    'opening the header must deliver the work it stands for',
  ).toBeVisible({ timeout: RENDER_MS });
  await expect
    .poll(() => detailReads.length, { timeout: RENDER_MS })
    .toBe(1);
  await openEverythingInside(row);
  await expect(
    row,
    'the tool output reaches the reader only through that read',
  ).toContainText(toolOutputMarker, { timeout: RENDER_MS });
  expect(detailReads, 'and one opened header is one read').toHaveLength(1);

  // Ownership is not asserted here, and that is a gap rather than a decision:
  // the browser lane signs in as one person (global-setup.ts through
  // fixtures/oidcLogin.ts, one ASTRABOX_E2E_OIDC_USERNAME), so there is no
  // second reader to refuse. The only other identity the suite can obtain is
  // the machine token in casdoor-scoped-api-client-credentials.parallel.spec.ts,
  // which needs a Docker handle on the server container — not something a
  // parallel spec can take. Covering "another person's session is not readable"
  // needs a second configured user in global setup.
  test.info().annotations.push({
    type: 'e2e_history_blocks_authorization',
    description: 'not covered: the lane has one signed-in identity',
  });
});
