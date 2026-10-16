/**
 * E2E: the folded history read is the same transcript, paged by message, with
 * the work behind one addressable header.
 *
 * The console reads its history from `/sessions/{id}/history-blocks`
 * (`historyPages.ts::readHistoryPage`), so this page is what a reader sees.
 * Everything that can go wrong on it answers 200 with a well-formed body:
 * `history_blocks.py::fold_parts` can fold a response's conclusion away with
 * its work, `load_history_page` can lose a message between two pages or serve
 * the second page against a history the first one never saw, and
 * `load_block_parts` can reopen a header at the wrong output position and hand
 * back parts that belong to a later response. A message that silently
 * disappears between page one and page two is not visible in the browser at
 * all — the reader simply never scrolls to it.
 *
 * So the page is checked against the raw read of the same session: the same
 * messages, in the same order, with the tool work removed from the page and
 * still present in `/messages` and in the detail read. The cursor is walked at
 * `limit=2`, which is the only way a three-response session crosses a page
 * boundary at all, and the boundary is re-asked to prove the cursor pins it.
 *
 * The browser half is one assertion the API half cannot make: that the page the
 * console receives carries no tool output, and that the output appears only
 * after the reader opens the header. A body that still carried the folded work
 * would render identically.
 *
 * Engine independence: tool names differ per engine, so nothing here matches
 * `Bash` or `Read`. The tool response is found by the tool parts its history
 * message carries, and cards are located by `data-tool-call-id`.
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
  messageToolCalls,
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

/** The data of a `data-process-block` part (`history_blocks.py::fold_parts`). */
interface ProcessDetails {
  block_id: string;
  cursor: string;
  session_id: string;
  message_id: string;
  tool_count: number;
  summarize: boolean;
  summary?: unknown;
}

function partsOf(record: MessageRecord | null | undefined): Array<Record<string, unknown>> {
  return Array.isArray(record?.parts) ? record.parts : [];
}

function partsOfType(record: MessageRecord | null | undefined, type: string): Array<Record<string, unknown>> {
  return partsOf(record).filter((part) => String(part.type ?? '') === type);
}

function toolCallIds(record: MessageRecord | null | undefined): string[] {
  return record ? messageToolCalls(record).map((call) => call.toolCallId).filter(Boolean) : [];
}

/** The headers this message carries in place of the work they stand for. */
function processDetails(record: MessageRecord | null | undefined): ProcessDetails[] {
  return partsOfType(record, 'data-process-block')
    .map((part) => part.data as ProcessDetails | undefined)
    .filter((details): details is ProcessDetails => Boolean(details));
}

function messageIds(records: MessageRecord[]): string[] {
  return records.map((record) => String(record.id));
}

/**
 * The engine's own tool names off the history messages — diagnostic only.
 *
 * What the model reached for is the model's business; what the read does with
 * it is this spec's. It never throws, so a broken read here cannot replace the
 * failure it is describing.
 */
function toolsDriven(records: MessageRecord[]): string {
  const names = new Set<string>();
  for (const record of records) {
    for (const call of messageToolCalls(record)) {
      if (call.toolName) names.add(call.toolName);
    }
  }
  return [...names].sort().join(', ') || '(no tool call at all)';
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
 *
 * Groups and cards are found inside the header, never anywhere in the turn.
 * The header shows a loading line until its detail read lands, and a group or
 * card the response kept beside its answer would otherwise stand in for the
 * work behind the header and be counted before that work arrived.
 */
async function openEverythingInside(turn: Locator): Promise<void> {
  const header = turn.getByTestId('assistant-turn-process');
  const groups = header.getByTestId('assistant-process');
  await expect(
    groups.first(),
    'an opened header must show at least one group of the work it stands for',
  ).toBeVisible({ timeout: RENDER_MS });
  const groupCount = await groups.count();
  for (let index = 0; index < groupCount; index += 1) {
    await ensureExpanded(groups.nth(index).getByTestId('assistant-process-trigger'));
  }
  const cards = header.locator('[data-tool-call-id]:visible');
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

  // ── ACT: three responses, with the one that ran a tool in the middle, so
  //    a `limit=2` walk has to carry it across a page boundary. ─────────────
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

  // The raw read is the control: the same history messages, unfolded.
  const raw = (await api.getMessages(sessionId, 50)).messages;
  const rawAssistants = raw.filter((record) => record.role === 'assistant');
  const toolRecord = rawAssistants.find((record) => toolCallIds(record).length > 0) ?? null;
  expect(
    toolRecord,
    'the folded page needs a response that ran a tool, and the engine drove: ' +
      `${toolsDriven(rawAssistants)}. Re-shape the prompt here rather than ` +
      'anywhere else, and do not convert this into a runtime skip: ' +
      'playwright-test-budget.cjs:48-70 SIGTERMs the whole process group on a ' +
      'skipped result and would end the lane instead of costing this one spec.',
  ).not.toBeNull();
  const toolMessageId = String(toolRecord?.id);
  const rawToolIds = toolCallIds(toolRecord);
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
    'the folded read must carry the same messages, in the same order, as the raw read',
  ).toEqual(messageIds(raw));
  expect(folded.block_count, 'block_count counts what the page carries').toBe(
    folded.messages.length,
  );
  expect(folded.has_more, 'a whole short session fits on one default page').toBe(false);
  expect(folded.next_cursor ?? null, 'and a last page issues no cursor').toBeNull();

  const foldedTool = folded.messages.find(
    (record) => String(record.id) === toolMessageId,
  );
  const headers = processDetails(foldedTool);
  expect(headers, 'a settled tool response folds into exactly one header').toHaveLength(1);
  const header = headers[0];
  expect(header.summarize, 'a whole-response fold is the one a label is written for').toBe(true);
  expect(Number(header.tool_count), 'the header counts the calls it stands for').toBeGreaterThanOrEqual(1);
  expect(String(header.session_id)).toBe(sessionId);
  expect(String(header.message_id)).toBe(toolMessageId);
  expect(String(header.block_id), 'a header a reader can open must be named').not.toBe('');
  expect(String(header.cursor), 'and must carry the output position it was issued at').not.toBe('');
  expect(
    toolCallIds(foldedTool),
    'the folded page must not carry the calls it folded away, nor the results they hold',
  ).toEqual([]);
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
  // A response that ran no tool folds only its reasoning, and only if the
  // model reasoned: `history_blocks.py::fold_parts` hides each settled run of
  // reasoning behind a reasoning-only header (no label, no tool count).
  // Whether the model reasons before a one-line answer is its business, so
  // the raw message decides which way each response goes.
  for (const record of folded.messages) {
    if (String(record.id) === toolMessageId || record.role !== 'assistant') continue;
    const rawRecord = raw.find((item) => String(item.id) === String(record.id));
    const thought = partsOfType(rawRecord, 'reasoning').length > 0;
    const headers = processDetails(record);
    if (!thought) {
      expect(headers, `response ${record.id} neither reasoned nor ran a tool, so it has nothing to fold`)
        .toHaveLength(0);
    }
    for (const item of headers) {
      expect(
        item.tool_count,
        `response ${record.id} ran no tool, so its header can stand only for its reasoning`,
      ).toBe(0);
      expect(item.summarize, 'a reasoning-only fold is not the labelled whole-response one').toBe(false);
    }
    expect(partsOfType(record, 'reasoning'), 'reasoning the page shows is reasoning it failed to fold')
      .toHaveLength(0);
    expect(
      messageText(record).trim(),
      `response ${record.id} keeps its answer on the page, word for word`,
    ).toBe(messageText(rawRecord as MessageRecord).trim());
  }

  // ── 2. Two messages at a time: same order, no gaps, no repeats. ──────────
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
      'a page of two must hold one or two messages, never none and never more',
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
  expect(new Set(walked).size, 'and must not hand the same message out twice').toBe(walked.length);

  const pagesHoldingTheToolResponse = pages.filter((walkedPage) => (
    messageIds(walkedPage.messages).includes(toolMessageId)
  ));
  expect(
    pagesHoldingTheToolResponse,
    'a response is one message, so a page boundary can never cut through it',
  ).toHaveLength(1);
  const walkedTool = pagesHoldingTheToolResponse[0].messages.find(
    (record) => String(record.id) === toolMessageId,
  );
  expect(toolCallIds(walkedTool), 'the walked page folds it the same way').toEqual([]);
  expect(processDetails(walkedTool).map((item) => item.block_id)).toEqual([header.block_id]);

  // The cursor's pinned position is what makes the second page belong to the
  // first read. Re-asking on a still transcript proves nothing — a read that
  // pins nothing answers the same — so a real response is written first, and
  // only then is the old cursor asked again: it must answer the page it
  // answered before, while a fresh read must show the new messages that page
  // must not.
  expect(pages.length, 'six messages at two per page must take more than one page').toBeGreaterThan(1);
  const boundaryCursor = String(pages[0].next_cursor || '');
  const laterMarker = `HISTORY_BLOCKS_LATER_${runId}`;
  const later = await api.sendTurn(
    sessionId,
    `Reply with exactly ${laterMarker} and nothing else. Do not use any tool.`,
    QUICK_TURN_MS,
  );
  expect(later.errorText, 'the response written after the read must answer').toBeNull();
  await api.waitForAssistantMessageMatching(sessionId, 3, () => true, RENDER_MS);
  const fresh = await api.getHistoryBlocks(sessionId);
  expect(
    fresh.messages.length,
    'a fresh read after the new response carries its input and its reply',
  ).toBe(raw.length + 2);
  expect(
    fresh.messages.some((record) => record.role === 'user' && messageText(record).includes(laterMarker)),
    'and the new input is among them',
  ).toBe(true);
  expect(
    messageIds(fresh.messages).slice(0, raw.length),
    'the messages the first read saw keep their order in front of the new ones',
  ).toEqual(messageIds(raw));
  const reasked = await api.getHistoryBlocks(sessionId, 2, boundaryCursor);
  expect(
    messageIds(reasked.messages),
    're-asking for a page at the old cursor must answer with the same messages, after the transcript grew',
  ).toEqual(messageIds(pages[1].messages));
  expect(String(reasked.next_cursor || '')).toBe(String(pages[1].next_cursor || ''));
  expect(reasked.has_more).toBe(pages[1].has_more);
  expect(
    messageIds(reasked.messages).some((id) => !messageIds(raw).includes(id)),
    'the pinned page never leaks a message written after its position',
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
  expect(opened.messages, 'a header stands for one response\'s work').toHaveLength(1);
  expect(opened.has_more, 'and a header\'s contents are whole').toBe(false);
  expect(
    toolCallIds(opened.messages[0]),
    'the reopened header must carry exactly the calls the raw message holds',
  ).toEqual(rawToolIds);
  expect(String(opened.messages[0].id)).toBe(header.block_id);
  expect(String(opened.messages[0].metadata?.history_block_id)).toBe(header.block_id);

  const withoutCursor = await request.get(apiPath(detailRoute));
  expect(
    withoutCursor.status(),
    'without its position the parts would be read as the response stands now, not as the page folded them',
  ).toBe(400);
  const withPageCursor = await request.get(
    apiPath(`${detailRoute}?cursor=${encodeURIComponent(boundaryCursor)}`),
  );
  expect(
    withPageCursor.status(),
    'a page cursor names a page, not a header, and must be refused as one',
  ).toBe(400);
  const unknownBlock = await request.get(
    apiPath(
      `/sessions/${sessionId}/history-blocks/${encodeURIComponent(`${toolMessageId}:p9999`)}`
      + `?cursor=${encodeURIComponent(header.cursor)}`,
    ),
  );
  expect(
    unknownBlock.status(),
    'a header the pinned response does not produce is missing, not empty — the two send a reader to different places',
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
    (record) => String(record.id) === toolMessageId,
  );
  expect(toolCallIds(delivered), 'the page the browser receives carries no calls').toEqual([]);
  // The model may quote what it read in its own answer, and its answer belongs
  // on the page. What the fold hides is the tools' output, so the answer is
  // held to the raw message word for word and the output is looked for in
  // everything else the body carries.
  expect(
    messageText(delivered as MessageRecord),
    'the answer the browser receives is the response\'s own words',
  ).toBe(messageText(toolRecord as MessageRecord));
  const rawAnswer = new Set(partsOfType(toolRecord, 'text').map((part) => String(part.text ?? '')));
  for (const part of partsOfType(delivered, 'text')) {
    expect(rawAnswer.has(String(part.text ?? '')), 'every text part on the page is one the response wrote')
      .toBe(true);
  }
  expect(
    JSON.stringify({
      ...(delivered ?? {}),
      parts: partsOf(delivered).filter((part) => String(part.type ?? '') !== 'text'),
    }),
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
    turnHeader,
    'the tool output reaches the reader only through that read, behind the header',
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
