/**
 * E2E: one Claude conversation crosses TWO real native SessionStore compact
 * boundaries, and the platform loses nothing.
 *
 * The vendor owns compaction. Claude Code's `/compact` replaces the model's
 * working history with a summary; in its own SessionStore that is recorded as a
 * `system` line of subtype `compact_boundary` whose `parentUuid` is null and
 * whose `logicalParentUuid` points back at the pre-compaction tail, followed by
 * a user-role line flagged `isCompactSummary` that carries the summary as the
 * only in-context representation of what came before (SDK 0.2.152,
 * `_internal/sessions.py`: `_build_conversation_chain`, `_is_visible_message`).
 * The transcript file itself is append-only: nothing before the boundary is
 * removed.
 *
 * The platform owns the conversation the person sees. Its user rows are built
 * from its own accepted and consumed inputs; the summary is engine context and
 * never a person's message. So after two compactions, with an ordinary exchange
 * before, between and after them, all of this must hold:
 *
 * 1. the database SessionStore mirror (`transcript_entries`, main scope) holds
 *    exactly two `compact_boundary` records, each answered by exactly one
 *    `isCompactSummary` record chained to it, followed by the typed
 *    `/compact` command envelope in the new chain, around the five
 *    real exchanges — which are all still there, complete;
 * 2. the history API keeps every submitted input as a user row, with the
 *    original message ids, roles and chronological order, and each real
 *    exchange's assistant row with the text the completed response settled on;
 * 3. a cold page renders exactly those rows, in that order, under those ids,
 *    with no user bubble carrying either summary.
 *
 * `/compact` is submitted through the composer like any other input, and the
 * product's own contract makes it a user input: it is accepted, consumed and
 * projected as a user row (proven live by `webhook-slash-command`). The
 * user rows are therefore all seven things typed, including both commands.
 *
 * Two real exchanges precede each compact command. The pinned vendor's manual
 * path requires at least two conversation groups; e0226 proved that one short
 * exchange returns "Not enough messages to compact." A refusal fails promptly
 * with the same retained scene, rather than waiting for a boundary that the
 * vendor has explicitly declined to produce.
 */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

import {
  AstraApi,
  answersInput,
  messageText,
  type InputReceipt,
  type MessageRecord,
} from '../fixtures/astraApi';
import { oracleDbPath, postgresRows, sqlLiteral } from '../fixtures/dbOracle';
import { parseTimeoutEnv } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { expectPromptDelivered, openSessionView, startPromptDelivery } from '../fixtures/sessionPage';
import { sessionStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

const COMPACT_COMMAND = '/compact';
// The vendor's command envelope, as its SessionStore records a typed command.
const COMMAND_NAME_ENVELOPE = `<command-name>${COMPACT_COMMAND}</command-name>`;
// One real turn, and one compaction landing in the mirror, share the lane budget.
const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
// Results that keep the scene and get the diagnostics attachment.
const FAILURE_STATUSES = new Set(['failed', 'timedOut', 'interrupted']);

// ── The native SessionStore mirror ─────────────────────────────────────────
//
// One row per SDK JSONL line in `transcript_entries`, in the transcript file
// (`transcript_scopes`) of this platform session it belongs to; the main
// transcript has an empty subpath. Raw line in `entry_json`, native position
// in `seq`.

interface NativeEntry {
  seq: number;
  uuid: string;
  entry: Record<string, unknown>;
}

/**
 * The conversation's main-scope native lines, parsed strictly and ordered by
 * native `seq`. A line that does not parse is a broken mirror, not a missing
 * one, so it throws with its position instead of vanishing from the list.
 */
function mainScopeEntries(sessionId: string): NativeEntry[] {
  const rows = postgresRows<{ scope_id: number; seq: number; entry_json: string }>(
    `SELECT e.scope_id, e.seq, e.entry_json FROM transcript_entries e `
      + `JOIN transcript_scopes s ON s.scope_id = e.scope_id `
      + `WHERE s.platform_session_id = ${sqlLiteral(sessionId)} AND s.subpath = '' `
      + `ORDER BY e.scope_id, e.seq`,
  );
  const scopeIds = new Set(rows.map((row) => String(row.scope_id)));
  if (scopeIds.size > 1) {
    throw new Error(
      `session ${sessionId} holds ${scopeIds.size} main SessionStore scopes; `
        + `a compacted conversation keeps its one native session: ${JSON.stringify([...scopeIds])}`,
    );
  }
  return rows
    .map((row) => {
      const seq = Number(row.seq);
      const raw = String(row.entry_json ?? '');
      let entry: unknown;
      try {
        entry = JSON.parse(raw);
      } catch (error) {
        throw new Error(
          `transcript_entries seq=${seq} for ${sessionId} is not JSON (${String(error)}): `
            + JSON.stringify(raw.slice(0, 200)),
        );
      }
      if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
        throw new Error(`transcript_entries seq=${seq} for ${sessionId} is not an object`);
      }
      const record = entry as Record<string, unknown>;
      return { seq, uuid: String(record.uuid ?? '').trim(), entry: record };
    })
    .sort((left, right) => left.seq - right.seq);
}

/** The visible text of a native line: a string content, or its `text` blocks. */
function nativeText(entry: Record<string, unknown>): string {
  const message = entry.message;
  if (!message || typeof message !== 'object') return '';
  const content = (message as Record<string, unknown>).content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  const texts: string[] = [];
  for (const block of content) {
    if (typeof block === 'string') {
      texts.push(block);
      continue;
    }
    if (!block || typeof block !== 'object') continue;
    const typed = block as Record<string, unknown>;
    if (String(typed.type ?? '') === 'text' && typeof typed.text === 'string') texts.push(typed.text);
  }
  return texts.join('\n');
}

function isCompactBoundary(entry: Record<string, unknown>): boolean {
  return String(entry.type ?? '') === 'system' && String(entry.subtype ?? '') === 'compact_boundary';
}

function isCompactSummary(entry: Record<string, unknown>): boolean {
  return String(entry.type ?? '') === 'user' && entry.isCompactSummary === true;
}

/** A user line a person could have typed: not meta, not a summary, not a sidechain. */
function isPlainUserLine(entry: Record<string, unknown>): boolean {
  return String(entry.type ?? '') === 'user'
    && entry.isMeta !== true
    && entry.isCompactSummary !== true
    && entry.isSidechain !== true;
}

function isAssistantLine(entry: Record<string, unknown>): boolean {
  return String(entry.type ?? '') === 'assistant' && entry.isSidechain !== true;
}

interface CompactionRecord {
  boundary: NativeEntry;
  summary: NativeEntry;
}

/**
 * Every native compact boundary paired with the summary chained to it. A
 * boundary whose summary has not landed yet is not a pair, so a poll on the
 * pair count waits for the whole recorded compaction, not its first line.
 */
function compactionRecords(entries: NativeEntry[]): CompactionRecord[] {
  const boundaries = entries.filter((item) => isCompactBoundary(item.entry));
  const summaries = entries.filter((item) => isCompactSummary(item.entry));
  const pairs: CompactionRecord[] = [];
  for (const boundary of boundaries) {
    const chained = summaries.filter(
      (summary) => String(summary.entry.parentUuid ?? '') === boundary.uuid && boundary.uuid !== '',
    );
    if (chained.length === 1) pairs.push({ boundary, summary: chained[0] });
  }
  return pairs;
}

/**
 * The typed `/compact` lines, as the CLI records a command it received. A
 * summary that quotes the envelope is recovery context, not a typed command,
 * so summary and sidechain lines are excluded before matching the text.
 */
function commandEnvelopeLines(entries: NativeEntry[]): NativeEntry[] {
  return entries.filter((item) => (
    String(item.entry.type ?? '') === 'user'
    && item.entry.isCompactSummary !== true
    && item.entry.isSidechain !== true
    && nativeText(item.entry).includes(COMMAND_NAME_ENVELOPE)
  ));
}

/** A bounded, readable view of the mirror for a failure message or attachment. */
function mirrorSummary(entries: NativeEntry[]): Record<string, unknown>[] {
  return entries.map((item) => ({
    seq: item.seq,
    uuid: item.uuid,
    type: item.entry.type ?? null,
    subtype: item.entry.subtype ?? null,
    parentUuid: item.entry.parentUuid ?? null,
    logicalParentUuid: item.entry.logicalParentUuid ?? null,
    isMeta: item.entry.isMeta ?? null,
    isCompactSummary: item.entry.isCompactSummary ?? null,
    compactMetadata: item.entry.compactMetadata ?? null,
    text: excerpt(nativeText(item.entry), 160),
  }));
}

function excerpt(value: unknown, limit = 240): string {
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  return text.length > limit ? `${text.slice(0, limit)}…(${text.length} chars)` : text;
}

/**
 * Rendered text and settled text are not the same string: emphasis markers
 * disappear under Markdown and line wrapping moves with the panel. Compare
 * content, not bytes, across that boundary.
 */
function normalizeRendered(text: string): string {
  return text.replace(/\s+/g, '').replace(/[*_`#>|~\-–—·•]/g, '');
}

/** Parsed `data:` frames of every session stream body the browser has read so far. */
async function browserStreamFrames(page: Page): Promise<Record<string, unknown>[]> {
  const bodies = await sessionStreamBodies(page);
  const frames: Record<string, unknown>[] = [];
  for (const body of bodies) {
    for (const line of body.text.split('\n')) {
      if (!line.startsWith('data: ') || line.trim() === 'data: [DONE]') continue;
      try {
        frames.push(JSON.parse(line.slice(6)) as Record<string, unknown>);
      } catch {
        frames.push({ type: 'unparsed', line: excerpt(line, 200) });
      }
    }
  }
  return frames;
}

// ── One settled input ──────────────────────────────────────────────────────

interface SettledInput {
  content: string;
  /** The platform's id for the accepted input. */
  inputId: string;
  /** The user row's id: the `client_message_id` the composer posted. */
  userMessageId: string;
  /** The assistant rows answering the input, as the completed response left them. */
  assistantRows: { messageId: string; text: string }[];
}

/**
 * Submit `content` through the composer and wait for the response to it to
 * end, then read what the durable history holds for that input. The user row
 * is located by the `client_message_id` the composer actually posted, so the
 * second `/compact` finds its own row and not the first one's: two
 * submissions with the same text are two external inputs, and each must keep
 * its own identity.
 */
async function submitAndSettle(
  api: AstraApi,
  page: Page,
  sessionId: string,
  content: string,
): Promise<SettledInput> {
  const knownUserIds = new Set(
    ((await api.getMessages(sessionId, 50)).messages || [])
      .filter((row) => row.role === 'user')
      .map((row) => row.id),
  );
  const delivery = await startPromptDelivery(page, sessionId, content);
  const accepted = await expectPromptDelivered(delivery);
  const receipt = ((await accepted.json()) as { data: InputReceipt }).data;
  const { clientMessageId } = delivery;
  expect(receipt.client_message_id, 'the receipt must name the message the composer posted').toBe(clientMessageId);
  expect(receipt.duplicate, `${JSON.stringify(content)} must be accepted as a new input`).toBe(false);

  let userRow: MessageRecord | undefined;
  await expect.poll(async () => {
    const rows = (await api.getMessages(sessionId, 50)).messages || [];
    const matches = rows.filter((row) => row.role === 'user' && row.id === clientMessageId);
    if (matches.length > 1) {
      throw new Error(
        `client_message_id ${clientMessageId} projected ${matches.length} user rows: `
          + JSON.stringify(matches.map((row) => ({ id: row.id, input_id: row.metadata?.input_id ?? null }))),
      );
    }
    userRow = matches[0];
    return userRow ? userRow.id : '';
  }, {
    timeout: TURN_TIMEOUT_MS,
    intervals: [500, 1_000, 1_500],
    message: `the durable history must project submission ${clientMessageId} (${JSON.stringify(content)}) as a user row`,
  }).toMatch(/\S/);
  const userMessageId = userRow!.id;
  expect(messageText(userRow!), `user row ${userMessageId} must carry the submitted text`).toBe(content);
  expect(knownUserIds.has(userMessageId), `user row ${userMessageId} must be new to this submission`).toBe(false);
  expect(userRow!.metadata?.input_id, `user row ${userMessageId} must be the accepted input`).toBe(receipt.input_id);

  const reply = await api.waitForReplyTo(sessionId, receipt.input_id, { timeoutMs: TURN_TIMEOUT_MS });
  const settled = await api.waitForSession(sessionId, (session) => session.state === 'READY', TURN_TIMEOUT_MS);
  expect(
    { state: settled.state, outcome: reply.metadata?.outcome ?? null, last_error: settled.last_error || null },
    `the response to ${JSON.stringify(content)} must complete and leave the session READY`,
  ).toEqual({ state: 'READY', outcome: 'completed', last_error: null });

  const history = await api.getMessages(sessionId, 50);
  const assistantRows = (history.messages || [])
    .filter((row) => answersInput(row, receipt.input_id))
    .map((row) => ({ messageId: row.id, text: messageText(row) }));
  return { content, inputId: receipt.input_id, userMessageId, assistantRows };
}

/** A real exchange: exactly one assistant row with text the completed response settled on. */
function onlyReply(input: SettledInput): { messageId: string; text: string } {
  expect(
    input.assistantRows,
    `the exchange ${JSON.stringify(input.content)} must settle exactly one assistant row`,
  ).toHaveLength(1);
  const reply = input.assistantRows[0];
  expect(reply.messageId, 'the assistant row must carry its identity').not.toEqual('');
  expect(reply.text.trim(), 'the assistant row must carry the completed text').not.toEqual('');
  return reply;
}

/**
 * The native lines of one real exchange, without judging them: every typed
 * user line carrying `marker`, and — for the last of them — the assistant
 * lines that answer it: every assistant line before the next typed or command
 * line or boundary, since Claude Code writes one JSONL line per content block.
 * Answer lines are located after that user line's own native position, so an
 * earlier reply with the same words cannot stand in for this one.
 */
function locateNativeExchange(
  entries: NativeEntry[],
  marker: string,
): { users: NativeEntry[]; assistants: NativeEntry[] } {
  // A native UUID identifies a message, not one append. Compaction can append
  // an old UUID again; keep its original position and require unchanged content.
  // The SDK reader also indexes messages by UUID (_build_conversation_chain).
  const originals = new Map<string, NativeEntry>();
  for (const item of entries) {
    if (!item.uuid) continue;
    const original = originals.get(item.uuid);
    if (original) {
      expect(item.entry.type, `reappended native ${item.uuid} must keep its type`).toBe(original.entry.type);
      expect(item.entry.message, `reappended native ${item.uuid} must keep its message`).toEqual(original.entry.message);
    } else {
      originals.set(item.uuid, item);
    }
  }
  const messages = [...originals.values()];
  const users = messages.filter((item) => isPlainUserLine(item.entry) && nativeText(item.entry).includes(marker));
  const user = users.at(-1);
  const assistants: NativeEntry[] = [];
  if (user) {
    for (const item of messages) {
      if (item.seq <= user.seq) continue;
      if (String(item.entry.type ?? '') === 'user' && item.entry.isMeta !== true) break;
      if (isCompactBoundary(item.entry)) break;
      if (isAssistantLine(item.entry)) assistants.push(item);
    }
  }
  return { users, assistants };
}

/** One real exchange: exactly one typed user line, and the lines answering it. */
function nativeExchange(
  entries: NativeEntry[],
  marker: string,
): { user: NativeEntry; assistants: NativeEntry[] } {
  const { users, assistants } = locateNativeExchange(entries, marker);
  expect(
    users.map((item) => ({ seq: item.seq, text: excerpt(nativeText(item.entry)) })),
    `the mirror must hold exactly one native user identity carrying ${marker}`,
  ).toHaveLength(1);
  return { user: users[0], assistants };
}

// ── Scene, cleanup and failure diagnostics ─────────────────────────────────

interface Scene {
  sessionId: string;
  agentId: string;
  inputs: SettledInput[];
  /** Browser-observed stream frames captured after each compact response ended. */
  compactTurnStreams: Record<string, unknown>[];
}

const scene: Scene = { sessionId: '', agentId: '', inputs: [], compactTurnStreams: [] };

const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (scene.agentId) await new AstraApi(request).deleteAgent(scene.agentId);
  scene.agentId = '';
});

type Probe<T> = { ok: true; value: T } | { ok: false; error: string };

async function probe<T>(read: () => T | Promise<T>): Promise<Probe<T>> {
  try {
    return { ok: true, value: await read() };
  } catch (error) {
    return { ok: false, error: String((error as Error)?.stack || error) };
  }
}

// Registered as an `afterEach` because that is where the result is real; inside
// the body a failing test still reads as passing. Every read is independent of
// the assertion that failed: the session, the mirror, the history API and the
// browser's own stream are each reported as either their value or the exact
// error that prevented reading them.
test.afterEach(async ({ page, request }, testInfo) => {
  if (!FAILURE_STATUSES.has(String(testInfo.status || ''))) return;
  const api = new AstraApi(request);
  const { sessionId } = scene;
  const noSession: Probe<never> = { ok: false, error: 'no session was created before the failure' };
  const diagnostics = {
    collected_at: new Date().toISOString(),
    status: testInfo.status,
    scene: { ...scene, compactTurnStreams: scene.compactTurnStreams.map((frame) => excerpt(frame, 400)) },
    database: oracleDbPath(),
    session: sessionId ? await probe(() => api.getSession(sessionId)) : noSession,
    history: sessionId
      ? await probe(async () => {
        const messagePage = await api.getMessages(sessionId, 50);
        return {
          has_more: messagePage.has_more ?? null,
          pending_interaction: messagePage.pending_interaction ?? null,
          rows: (messagePage.messages || []).map((row) => ({
            role: row.role,
            id: row.id,
            input_id: row.metadata?.input_id ?? null,
            input_ids: row.metadata?.input_ids ?? null,
            outcome: row.metadata?.outcome ?? null,
            text: excerpt(messageText(row)),
          })),
        };
      })
      : noSession,
    native_mirror: sessionId ? await probe(() => mirrorSummary(mainScopeEntries(sessionId))) : noSession,
    browser_stream_frames: await probe(async () => (await browserStreamFrames(page))
      .map((frame) => excerpt(frame, 400))),
  };
  try {
    await testInfo.attach('session-store-repeated-compaction-diagnostics', {
      body: JSON.stringify(diagnostics, null, 2),
      contentType: 'application/json',
    });
  } catch (error) {
    testInfo.annotations.push({
      type: 'diagnostics_attach_error',
      description: `${String(error)}; diagnostics=${excerpt(diagnostics, 4000)}`,
    });
  }
});

// ── The case ───────────────────────────────────────────────────────────────

test('two real native compactions keep the complete history, its identities and order, with no summary bubble', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = `${Date.now()}_${test.info().workerIndex}`;
  const oldMarker = `COMPACT_HISTORY_OLD_USER_${runId}`;
  const oldContextMarker = `COMPACT_HISTORY_OLD_CONTEXT_${runId}`;
  const betweenMarker = `COMPACT_HISTORY_BETWEEN_USER_${runId}`;
  const betweenContextMarker = `COMPACT_HISTORY_BETWEEN_CONTEXT_${runId}`;
  const afterMarker = `COMPACT_HISTORY_AFTER_USER_${runId}`;
  const exchangePrompt = (marker: string) => (
    `${marker}. Do not use tools. Reply with one short plain sentence.`
  );

  // The conversation owns a cold, isolated sandbox: the whole SessionStore
  // under test belongs to this one conversation and is kept with it on failure.
  const agent = await api.createColdTestAgent(`__e2e_repeated_compaction_${runId}`);
  scene.agentId = agent.agent_id;
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  scene.sessionId = sessionId;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });

  await mirrorSseBodies(page);
  await openSessionView(page, sessionId);
  await api.waitForSessionReady(sessionId);

  // ── Exchange, compact, exchange, compact, exchange ────────────────────────

  const oldInput = await submitAndSettle(api, page, sessionId, exchangePrompt(oldMarker));
  scene.inputs.push(oldInput);
  const oldReply = onlyReply(oldInput);
  const oldContextInput = await submitAndSettle(api, page, sessionId, exchangePrompt(oldContextMarker));
  scene.inputs.push(oldContextInput);
  const oldContextReply = onlyReply(oldContextInput);

  const firstCompact = await submitAndSettle(api, page, sessionId, COMPACT_COMMAND);
  scene.inputs.push(firstCompact);
  scene.compactTurnStreams = await browserStreamFrames(page);
  expect(firstCompact.assistantRows.map((row) => row.text),
    'the vendor must accept compaction before waiting for its durable boundary')
    .not.toContain('Not enough messages to compact.');
  await expect.poll(() => {
    const entries = mainScopeEntries(sessionId);
    return { compactions: compactionRecords(entries).length, envelopes: commandEnvelopeLines(entries).length };
  }, {
    timeout: TURN_TIMEOUT_MS,
    intervals: [500, 1_000, 1_500],
    message: 'the first /compact must land one native compact boundary with its chained summary in the mirror',
  }).toEqual({ compactions: 1, envelopes: 1 });

  const betweenInput = await submitAndSettle(api, page, sessionId, exchangePrompt(betweenMarker));
  scene.inputs.push(betweenInput);
  const betweenReply = onlyReply(betweenInput);
  const betweenContextInput = await submitAndSettle(api, page, sessionId, exchangePrompt(betweenContextMarker));
  scene.inputs.push(betweenContextInput);
  const betweenContextReply = onlyReply(betweenContextInput);

  const secondCompact = await submitAndSettle(api, page, sessionId, COMPACT_COMMAND);
  scene.inputs.push(secondCompact);
  scene.compactTurnStreams = await browserStreamFrames(page);
  expect(secondCompact.assistantRows.map((row) => row.text),
    'the vendor must accept the second compaction before waiting for its boundary')
    .not.toContain('Not enough messages to compact.');
  await expect.poll(() => {
    const entries = mainScopeEntries(sessionId);
    return { compactions: compactionRecords(entries).length, envelopes: commandEnvelopeLines(entries).length };
  }, {
    timeout: TURN_TIMEOUT_MS,
    intervals: [500, 1_000, 1_500],
    message: 'the second /compact must land a second native compact boundary with its chained summary in the mirror',
  }).toEqual({ compactions: 2, envelopes: 2 });

  const afterInput = await submitAndSettle(api, page, sessionId, exchangePrompt(afterMarker));
  scene.inputs.push(afterInput);
  const afterReply = onlyReply(afterInput);

  const inputs = [oldInput, oldContextInput, firstCompact, betweenInput, betweenContextInput, secondCompact, afterInput];
  const exchanges = [
    { input: oldInput, marker: oldMarker, reply: oldReply },
    { input: oldContextInput, marker: oldContextMarker, reply: oldContextReply },
    { input: betweenInput, marker: betweenMarker, reply: betweenReply },
    { input: betweenContextInput, marker: betweenContextMarker, reply: betweenContextReply },
    { input: afterInput, marker: afterMarker, reply: afterReply },
  ];

  // ── 1. The native SessionStore mirror ─────────────────────────────────────

  // The after-exchange has settled; its lines reach the mirror shortly after.
  // Wait for THAT exchange: its typed user line by marker, and the answer
  // located after that line's own native position, so an earlier reply with
  // the same words cannot satisfy this wait.
  await expect.poll(
    () => {
      const located = locateNativeExchange(mainScopeEntries(sessionId), afterMarker);
      return {
        typed_user_lines: located.users.length,
        answered: located.assistants.length > 0
          && normalizeRendered(located.assistants.map((item) => nativeText(item.entry)).join(''))
            === normalizeRendered(afterReply.text),
      };
    },
    {
      timeout: TURN_TIMEOUT_MS,
      intervals: [500, 1_000, 1_500],
      message: 'the after-exchange, typed user line and its settled answer, must reach the mirror before the native history is judged',
    },
  ).toEqual({ typed_user_lines: 1, answered: true });
  const entries = mainScopeEntries(sessionId);
  const boundaries = entries.filter((item) => isCompactBoundary(item.entry));
  const summaries = entries.filter((item) => isCompactSummary(item.entry));
  const compactions = compactionRecords(entries);
  const envelopes = commandEnvelopeLines(entries);
  expect(boundaries.map((item) => item.seq), 'exactly two native compact boundaries').toHaveLength(2);
  expect(summaries.map((item) => item.seq), 'exactly two native compact summaries').toHaveLength(2);
  expect(compactions, 'each boundary must be answered by exactly one summary chained to it').toHaveLength(2);
  expect(envelopes.map((item) => item.seq), 'exactly two typed /compact command envelopes').toHaveLength(2);

  const byUuid = new Map(entries.filter((item) => item.uuid).map((item) => [item.uuid, item]));
  for (const [index, { boundary, summary }] of compactions.entries()) {
    const label = `compaction ${index + 1} (boundary seq=${boundary.seq}, summary seq=${summary.seq})`;
    expect(boundary.uuid, `${label}: the boundary must carry its native uuid`).not.toEqual('');
    expect(
      boundary.entry.parentUuid ?? null,
      `${label}: a compact boundary starts a new parent chain`,
    ).toBeNull();
    const logicalParent = String(boundary.entry.logicalParentUuid ?? '').trim();
    expect(logicalParent, `${label}: the boundary must name the pre-compaction tail`).not.toEqual('');
    const tail = byUuid.get(logicalParent);
    expect(
      tail ? tail.seq : null,
      `${label}: logicalParentUuid ${logicalParent} must be an earlier line of this mirror`,
    ).not.toBeNull();
    expect(tail!.seq, `${label}: the pre-compaction tail precedes the boundary`).toBeLessThan(boundary.seq);
    expect(summary.seq, `${label}: the summary follows its boundary`).toBeGreaterThan(boundary.seq);
    expect(
      nativeText(summary.entry).trim(),
      `${label}: the native summary must carry the vendor's recovery context`,
    ).not.toEqual('');
    const envelope = envelopes[index];
    expect(envelope.seq, `${label}: its typed /compact follows the summary`).toBeGreaterThan(summary.seq);
    let parent = String(envelope.entry.parentUuid ?? '');
    const visited = new Set<string>();
    while (parent && parent !== summary.uuid && !visited.has(parent)) {
      visited.add(parent);
      parent = String(byUuid.get(parent)?.entry.parentUuid ?? '');
    }
    expect(parent, `${label}: its command belongs to this summary's native parent chain`).toBe(summary.uuid);
  }

  // Every real exchange is still complete in the mirror, in native order around
  // the boundaries: old < compact 1 < between < compact 2 < after.
  const nativeOld = nativeExchange(entries, oldMarker);
  const nativeOldContext = nativeExchange(entries, oldContextMarker);
  const nativeBetween = nativeExchange(entries, betweenMarker);
  const nativeBetweenContext = nativeExchange(entries, betweenContextMarker);
  const nativeAfter = nativeExchange(entries, afterMarker);
  for (const [exchange, native] of [
    [exchanges[0], nativeOld],
    [exchanges[1], nativeOldContext],
    [exchanges[2], nativeBetween],
    [exchanges[3], nativeBetweenContext],
    [exchanges[4], nativeAfter],
  ] as const) {
    expect(
      native.assistants.map((item) => item.seq),
      `the mirror must still hold the assistant lines answering ${exchange.marker}`,
    ).not.toHaveLength(0);
    expect(
      normalizeRendered(native.assistants.map((item) => nativeText(item.entry)).join('')),
      `the native answer to ${exchange.marker} must be the text the completed response settled on`,
    ).toBe(normalizeRendered(exchange.reply.text));
  }
  const nativeOrder = [
    nativeOld.user.seq,
    nativeOld.assistants[0].seq,
    nativeOldContext.user.seq,
    nativeOldContext.assistants[0].seq,
    compactions[0].boundary.seq,
    compactions[0].summary.seq,
    envelopes[0].seq,
    nativeBetween.user.seq,
    nativeBetween.assistants[0].seq,
    nativeBetweenContext.user.seq,
    nativeBetweenContext.assistants[0].seq,
    compactions[1].boundary.seq,
    compactions[1].summary.seq,
    envelopes[1].seq,
    nativeAfter.user.seq,
    nativeAfter.assistants[0].seq,
  ];
  expect(
    nativeOrder,
    'native sequence order must be old exchange, first compaction, between exchange, second compaction, after exchange',
  ).toEqual([...nativeOrder].sort((left, right) => left - right));
  expect(new Set(nativeOrder).size, 'the sixteen native positions must be distinct lines').toBe(nativeOrder.length);

  const summaryHeads = compactions.map(({ summary }) => nativeText(summary.entry).trim().slice(0, 60));
  test.info().annotations.push({
    type: 'e2e_repeated_compaction',
    description: JSON.stringify({
      session_id: sessionId,
      inputs: inputs.map((input) => ({
        content: input.content,
        input_id: input.inputId,
        user_message_id: input.userMessageId,
        assistant_rows: input.assistantRows.map((row) => ({ message_id: row.messageId, text: excerpt(row.text) })),
      })),
      compactions: compactions.map(({ boundary, summary }) => ({
        boundary: { seq: boundary.seq, uuid: boundary.uuid, logicalParentUuid: boundary.entry.logicalParentUuid ?? null, compactMetadata: boundary.entry.compactMetadata ?? null },
        summary: { seq: summary.seq, uuid: summary.uuid, parentUuid: summary.entry.parentUuid ?? null, text: excerpt(nativeText(summary.entry)) },
      })),
      envelopes: envelopes.map((item) => item.seq),
    }),
  });

  // ── 2. The history API ────────────────────────────────────────────────────

  const history = await api.getMessages(sessionId, 50);
  expect(history.has_more, 'the whole conversation fits one page').toBe(false);
  expect(
    (await api.getSession(sessionId)).current_responses ?? [],
    'no response is running after the last settle',
  ).toEqual([]);
  const rows = history.messages || [];

  expect(
    rows.filter((row) => row.role === 'user').map((row) => ({
      id: row.id, input_id: row.metadata?.input_id ?? null, text: messageText(row),
    })),
    'user rows must be exactly all seven submitted inputs, under their original ids, in order',
  ).toEqual(inputs.map((input) => ({
    id: input.userMessageId, input_id: input.inputId, text: input.content,
  })));
  for (const { input, reply, marker } of exchanges) {
    expect(
      rows.filter((row) => answersInput(row, input.inputId)).map((row) => ({
        id: row.id, text: messageText(row),
      })),
      `the assistant row answering ${marker} must keep its id and settled text across both compactions`,
    ).toEqual([{ id: reply.messageId, text: reply.text }]);
  }
  // The submitted input a history row belongs to: its own for a user row, the
  // one it answers for an assistant row; empty for a row no submission explains.
  const inputOrder = inputs.map((input) => input.inputId);
  const rowInput = (row: MessageRecord): string => (row.role === 'user'
    ? String(row.metadata?.input_id || '')
    : String((row.metadata?.input_ids ?? []).find((id) => inputOrder.includes(String(id))) ?? ''));
  expect(
    rows.filter((row) => !inputOrder.includes(rowInput(row))).map((row) => ({
      role: row.role, id: row.id, input_ids: row.metadata?.input_ids ?? null, text: excerpt(messageText(row)),
    })),
    'history must hold no row outside the seven submitted inputs',
  ).toEqual([]);
  const rowInputIndexes = rows.map((row) => inputOrder.indexOf(rowInput(row)));
  expect(
    rowInputIndexes,
    'history rows must stay in chronological input order through both compactions',
  ).toEqual([...rowInputIndexes].sort((left, right) => left - right));
  for (const input of inputs) {
    const inputRows = rows.filter((row) => rowInput(row) === input.inputId);
    expect(inputRows[0]?.role, `input ${input.inputId} must start with its user row`).toBe('user');
  }
  for (const head of summaryHeads) {
    expect(
      rows.filter((row) => messageText(row).includes(head)).map((row) => ({ role: row.role, id: row.id })),
      'a native compact summary is engine recovery context, never a history row',
    ).toEqual([]);
  }

  // ── 3. The cold browser ───────────────────────────────────────────────────

  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });
  const expectedIds = rows.map((row) => row.id);
  const scroller = page.getByTestId('session-conversation');
  const readDomRows = () => scroller.evaluate((element) => {
    const viewport = element.getBoundingClientRect();
    return {
      top: element.scrollTop,
      height: element.clientHeight,
      rows: Array.from(element.querySelectorAll<HTMLElement>('[data-message-id]')).map((row) => {
        const rect = row.getBoundingClientRect();
        const user = row.querySelector<HTMLElement>('[data-testid="user-message"]');
        const assistant = row.querySelector<HTMLElement>('[data-testid="assistant-message"]');
        return {
          id: String(row.dataset.messageId || ''),
          index: Number(row.dataset.messageVirtualIndex),
          visible: rect.bottom > viewport.top && rect.top < viewport.bottom,
          role: user ? 'user' : assistant ? 'assistant' : 'other',
          userText: user ? user.innerText : '',
          assistantText: Array.from(
            row.querySelectorAll<HTMLElement>('[data-testid="assistant-text"]'),
          ).map((node) => node.innerText).join('\n'),
        };
      }),
    };
  });
  const seen = new Map<string, Awaited<ReturnType<typeof readDomRows>>['rows'][number]>();
  // Walk overlapping viewports: cold history starts at the bottom, and rows
  // outside the viewport and overscan are intentionally unmounted.
  await expect.poll(async () => {
    const current = await readDomRows();
    // Virtuoso initially hides its measured rows until the first scroll settles.
    if (!current.rows.some((row) => row.visible && (row.userText || row.assistantText))) return [];
    const mountedIds = current.rows.map((row) => row.id);
    expect(new Set(mountedIds).size, 'mounted history must not duplicate a message').toBe(mountedIds.length);
    const positions = mountedIds.map((id) => {
      expect(expectedIds, `unexpected cold history row ${id}`).toContain(id);
      return expectedIds.indexOf(id);
    });
    expect(positions, 'mounted cold rows must retain history order')
      .toEqual([...positions].sort((left, right) => left - right));
    for (const row of current.rows.filter((item) => item.visible)) {
      const expected = rows[expectedIds.indexOf(row.id)];
      expect(row.role, `cold row ${row.id} must retain its role`).toBe(expected.role);
      if (row.role === 'user') {
        expect(row.userText.trim(), `cold input ${row.id} must be readable`).toBe(messageText(expected));
      }
      const exchange = exchanges.find(({ reply }) => reply.messageId === row.id);
      if (exchange) {
        expect(normalizeRendered(row.assistantText), `cold reply ${row.id} must be readable`)
          .toBe(normalizeRendered(exchange.reply.text));
      }
      seen.set(row.id, row);
    }
    if (seen.size < rows.length) {
      await scroller.hover();
      await page.mouse.wheel(0, -Math.max(1, Math.floor(current.height / 2)));
      await expect.poll(() => scroller.evaluate((element) => element.scrollTop), {
        message: 'the reader must advance through older cold history',
      }).not.toBe(current.top);
    }
    return [...seen.values()].sort((left, right) => left.index - right.index).map((row) => row.id);
  }, {
    timeout: 30_000,
    message: 'the cold page must render every history row under its original id, in history order',
  }).toEqual(expectedIds);
  const domRows = [...seen.values()].sort((left, right) => left.index - right.index);
  expect(domRows.filter((row) => row.role === 'user'),
    'the cold page must render exactly all seven submitted inputs as user bubbles').toHaveLength(inputs.length);
  expect(
    domRows.map((row) => ({ id: row.id, role: row.role })),
    'each rendered row must keep the role its history row has',
  ).toEqual(rows.map((row) => ({ id: row.id, role: row.role })));
  expect(
    domRows.filter((row) => row.role === 'user').map((row) => row.userText.trim()),
    'user bubbles must read exactly the submitted inputs, /compact included',
  ).toEqual(inputs.map((input) => input.content));
  for (const { reply, marker } of exchanges) {
    const rendered = domRows.find((row) => row.id === reply.messageId);
    expect(rendered, `the cold page must render the assistant row answering ${marker}`).toBeDefined();
    expect(
      normalizeRendered(rendered!.assistantText),
      `the assistant bubble answering ${marker} must show the settled text`,
    ).toBe(normalizeRendered(reply.text));
  }
  for (const head of summaryHeads) {
    await expect(
      domRows.filter((row) => row.role === 'user' && row.userText.includes(head)),
      'SDK compact summaries are recovery context, not user messages',
    ).toHaveLength(0);
  }
});
