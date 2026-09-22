/**
 * E2E: a webhook delivers a native Skill slash command, and a browser enters the
 * conversation for the first time while that command's turn is still running.
 *
 * The one external input of the scenario is the webhook body: the slash command
 * of a real, cloneable Skill (`/internal-comms`, pinned from the public
 * anthropics/skills repository through `Agent.skills`) followed by a
 * self-contained request. The platform relays that body verbatim — it
 * authenticates and delivers, and does not decide whether the text is an
 * instruction or an event. Claude Code then expands the command on its own
 * side: its SessionStore gains the command envelope and the Skill's body as
 * user-role records that no person ever typed.
 *
 * What must hold:
 *
 * 1. exactly one accepted trigger, and exactly one durable user input carrying
 *    the raw body under the deployment's own receipt id;
 * 2. before any page opens: the native command envelope and the Skill's body
 *    expansion are in the database SessionStore, and the request's one real
 *    Bash call is recorded there with no result yet — held open by this spec,
 *    and observably running in the sandbox;
 * 3. the first page entry, inside that window, hydrates the one external input
 *    and nothing engine-owned, while the turn is genuinely active;
 * 4. through release and real completion the page never shows more than one
 *    user bubble, and never the Skill body or the command envelope;
 * 5. a genuine nonempty assistant answer lands after the held Bash's real
 *    native result;
 * 6. a cold reload still shows the same one external user input.
 *
 * Gates are real execution receipts, not sleeps: the request asks the Agent to
 * run one Bash command that stamps a sentinel and waits for a harness-owned
 * release file. There is no second prompt and no nudge — a model that declines
 * to run the command fails the spec rather than being asked again, because a
 * second input is the property under test.
 *
 * A failing run keeps its scene: the release file is written on the passing
 * path only, so a held gate stays held for diagnosis until the in-box ceiling
 * ends it, and an independent read-only diagnostics attachment is collected in
 * `afterEach`, where the test's real status is known.
 */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

import {
  AstraApi,
  messageText,
  visibleMessages,
  type MessagePage,
  type MessageRecord,
  type SessionRecord,
} from '../fixtures/astraApi';
import { documentsByField, sessionEvents } from '../fixtures/dbOracle';
import { parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

// The Skill fixture: a real, cloneable native Skill at an exact public commit.
// Its command name is the directory name (`skills/internal-comms` →
// `/internal-comms`), which is also the name the platform's descriptor parser
// derives (`_parse_skill_descriptor`: basename of the `#path`).
const SKILL_COMMIT = '41bbe19d1a1a7eaab5e7bb9050a417e5c6cffc8f';
const SKILL_DESCRIPTOR = `https://github.com/anthropics/skills.git@${SKILL_COMMIT}#skills/internal-comms`;
const SKILL_COMMAND_NAME = 'internal-comms';
const SLASH_COMMAND = `/${SKILL_COMMAND_NAME}`;
// A sentence from the pinned SKILL.md body. It is what Claude Code expands into
// its own user-side record; it appears in no webhook text and no reply.
const SKILL_BODY_MARKER = 'Load the appropriate guideline file';
// The vendor's command envelope, as its SessionStore records a typed command.
const COMMAND_NAME_ENVELOPE = `<command-name>${SLASH_COMMAND}</command-name>`;
const COMMAND_NAME_TAG = '<command-name>';
const COMMAND_MESSAGE_TAG = '<command-message>';
// Text that must never appear in a user bubble: the Skill's own body, the
// vendor's command envelope, and any platform wrapper around the payload.
// Matched case-insensitively on every observer sample.
const FORBIDDEN_USER_BUBBLE_TEXT = [
  SKILL_BODY_MARKER,
  COMMAND_NAME_TAG,
  COMMAND_MESSAGE_TAG,
  'Received a webhook event',
  'webhook payload',
];

// How long the real Bash gate may take to be recorded and observed running.
const GATE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_BACKGROUND_PROBE_TIMEOUT_MS', 90_000);
// The reply after the gate opens, and the whole conversation settling.
const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);

// Ceiling inside the box, so a sandbox script cannot outlive the run that owns
// it. The passing path releases the gate itself; a failing run leaves it held
// as evidence, and this bounds how long the box keeps that command alive.
const GATE_CEILING_SECONDS = 100;

const TERMINAL_STATES = new Set(['RECOVERY_REQUIRED', 'TERMINATED', 'DELETED', 'FAILED']);
// Results that keep the scene and get the diagnostics attachment.
const FAILURE_STATUSES = new Set(['failed', 'timedOut', 'interrupted']);

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * A python gate: stamp a sentinel with a unique marker, wait for the release
 * file, then print the completion marker. The sentinel carries a marker rather
 * than existing empty because the probe reads it with `cat`: a file check whose
 * answer is the word "yes" is also satisfied by an echo of the command that
 * asked.
 */
function gateCommand(options: {
  sentinelPath: string;
  sentinelMarker: string;
  releasePath: string;
  ceilingSeconds: number;
  donePrint: string;
  timeoutMessage: string;
}): string {
  return [
    "python3 - <<'PY'",
    'from pathlib import Path',
    'import time',
    `Path(${JSON.stringify(options.sentinelPath)}).write_text(${JSON.stringify(options.sentinelMarker)})`,
    `release = Path(${JSON.stringify(options.releasePath)})`,
    `deadline = time.monotonic() + ${options.ceilingSeconds}`,
    'while not release.exists():',
    '    if time.monotonic() >= deadline:',
    `        raise TimeoutError(${JSON.stringify(options.timeoutMessage)})`,
    '    time.sleep(0.2)',
    `print(${JSON.stringify(options.donePrint)})`,
    'PY',
  ].join('\n');
}

/**
 * The sentinel's own marker, read out of the sandbox rather than inferred.
 * Nothing is caught: a transport failure or a 5xx from the terminal endpoint
 * is a broken probe, and swallowing it would report the sandbox as quiet when
 * the question never reached it.
 */
async function gateIsRunning(
  api: AstraApi,
  sessionId: string,
  sentinelPath: string,
  sentinelMarker: string,
): Promise<boolean> {
  const output = await api.runTerminalCommand(
    sessionId,
    `cat ${sentinelPath} 2>/dev/null || true`,
    '/workspace',
    30_000,
  );
  return output.includes(sentinelMarker);
}

// ── The native SessionStore mirror ─────────────────────────────────────────
//
// The vendor's own transcript, as the platform durably holds it: one row per
// SDK JSONL line in `transcript_entries`, keyed by `platform_session_id`, with
// the raw line in `entry_json`. Read through the document oracle rather than
// the admin export so the read is the database's, ordered by native `seq`.

interface NativeEntry {
  seq: number;
  entry: Record<string, unknown>;
}

function mainScopeEntries(sessionId: string): NativeEntry[] {
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .filter((row) => row.subpath == null)
    .map((row) => ({
      seq: Number(row.seq),
      entry: JSON.parse(String(row.entry_json)) as Record<string, unknown>,
    }))
    .sort((left, right) => left.seq - right.seq);
}

/** Whether an entry belongs to the conversation's own lane, not a subagent's. */
function isParentLane(entry: Record<string, unknown>): boolean {
  if (entry.isSidechain === true) return false;
  const parentToolUseId = entry.parentToolUseId ?? entry.parent_tool_use_id;
  return String(parentToolUseId ?? '').trim() === '';
}

function messageContentBlocks(entry: Record<string, unknown>): Record<string, unknown>[] {
  const message = entry.message;
  if (!message || typeof message !== 'object') return [];
  const content = (message as Record<string, unknown>).content;
  if (!Array.isArray(content)) return [];
  return content.filter(
    (block): block is Record<string, unknown> => Boolean(block) && typeof block === 'object',
  );
}

/**
 * The visible text of a native line, block by block: a string `message.content`
 * counts as one text block, a list contributes its string and `text`-typed
 * entries.
 */
function messageTextBlocks(entry: Record<string, unknown>): string[] {
  const message = entry.message;
  if (!message || typeof message !== 'object') return [];
  const content = (message as Record<string, unknown>).content;
  if (typeof content === 'string') return [content];
  if (!Array.isArray(content)) return [];
  const texts: string[] = [];
  for (const block of content) {
    if (typeof block === 'string') {
      texts.push(block);
      continue;
    }
    if (!block || typeof block !== 'object') continue;
    const typed = block as Record<string, unknown>;
    if (String(typed.type ?? '').trim().toLowerCase() !== 'text') continue;
    if (typeof typed.text === 'string') texts.push(typed.text);
  }
  return texts;
}

function entryRole(entry: Record<string, unknown>): string {
  const entryType = String(entry.type ?? '').trim().toLowerCase();
  const message = entry.message;
  const role = message && typeof message === 'object'
    ? String((message as Record<string, unknown>).role ?? '').trim().toLowerCase()
    : '';
  return role || entryType;
}

/**
 * USER-role records in the conversation's lane carrying `marker`. Counted over
 * user records alone: the command envelope and the Skill body are both things
 * the CLI writes on the user side, which is the fact under test; an assistant
 * record quoting the marker would not be.
 */
function userRecordsCarrying(
  entries: NativeEntry[],
  marker: string,
): { seq: number; text: string }[] {
  const matches: { seq: number; text: string }[] = [];
  for (const item of entries) {
    if (!isParentLane(item.entry)) continue;
    const entryType = String(item.entry.type ?? '').trim().toLowerCase();
    if (entryType !== 'user' && entryRole(item.entry) !== 'user') continue;
    for (const text of messageTextBlocks(item.entry)) {
      if (text.includes(marker)) matches.push({ seq: item.seq, text });
    }
  }
  return matches;
}

/** The conversation's own Bash call for `commandFragment`, by native tool_use id. */
function bashToolUse(
  entries: NativeEntry[],
  commandFragment: string,
): { seq: number; toolUseId: string } | undefined {
  for (const item of entries) {
    if (!isParentLane(item.entry)) continue;
    if (String(item.entry.type ?? '') !== 'assistant') continue;
    for (const block of messageContentBlocks(item.entry)) {
      if (String(block.type ?? '') !== 'tool_use') continue;
      if (String(block.name ?? '') !== 'Bash') continue;
      if (!JSON.stringify(block.input ?? null).includes(commandFragment)) continue;
      const toolUseId = String(block.id ?? '').trim();
      if (toolUseId) return { seq: item.seq, toolUseId };
    }
  }
  return undefined;
}

/** That same call's native result, still in the conversation's lane. */
function bashToolResult(
  entries: NativeEntry[],
  toolUseId: string,
): { seq: number; block: Record<string, unknown> } | undefined {
  for (const item of entries) {
    if (!isParentLane(item.entry)) continue;
    for (const block of messageContentBlocks(item.entry)) {
      if (String(block.type ?? '') !== 'tool_result') continue;
      if (String(block.tool_use_id ?? '') !== toolUseId) continue;
      return { seq: item.seq, block };
    }
  }
  return undefined;
}

/** The conversation's assistant text carrying `marker`, at its native position. */
function assistantTextCarrying(
  entries: NativeEntry[],
  marker: string,
): { seq: number } | undefined {
  for (const item of entries) {
    if (!isParentLane(item.entry)) continue;
    if (String(item.entry.type ?? '') !== 'assistant') continue;
    for (const block of messageContentBlocks(item.entry)) {
      if (String(block.type ?? '') !== 'text') continue;
      if (String(block.text ?? '').includes(marker)) return { seq: item.seq };
    }
  }
  return undefined;
}

/** The overlay's messages, exactly as `visibleMessages` folds them for the console. */
function overlayMessages(messagePage: MessagePage): MessageRecord[] {
  const overlay = messagePage.active_turn_overlay;
  if (!overlay) return [];
  return overlay.messages?.length ? overlay.messages : [overlay.message];
}

/**
 * The accepted `StartTurn` commands of the session, from the platform journal.
 *
 * This is the durable receipt of an input the platform accepted for delivery.
 * One trigger must leave exactly one, carrying the raw body and the
 * deployment's receipt id.
 */
function acceptedStartTurns(sessionId: string): { content: string; client_message_id: string }[] {
  return sessionEvents(sessionId)
    .filter((event) => String(event.event_type ?? '') === 'command.accepted')
    .map((event) => (event.payload && typeof event.payload === 'object'
      ? (event.payload as Record<string, unknown>)
      : {}))
    .filter((payload) => String(payload.command_type ?? '') === 'StartTurn')
    .map((payload) => ({
      content: String(payload.content ?? ''),
      client_message_id: String(payload.client_message_id ?? ''),
    }));
}

async function waitForAssistantTextMatching(
  api: AstraApi,
  sessionId: string,
  pattern: RegExp,
  timeoutMs: number,
): Promise<MessageRecord> {
  const deadline = Date.now() + timeoutMs;
  let lastTexts: string[] = [];
  while (Date.now() < deadline) {
    const messagePage = await api.getMessages(sessionId, 50);
    const messages = visibleMessages(messagePage);
    lastTexts = messages
      .filter((message) => message.role === 'assistant')
      .map((message) => messageText(message).slice(0, 200));
    const hit = messages.find(
      (message) => message.role === 'assistant' && pattern.test(messageText(message)),
    );
    if (hit) return hit;
    await sleep(1_500);
  }
  throw new Error(
    `session ${sessionId} produced no assistant text matching ${pattern} within ${timeoutMs}ms; `
    + `assistant texts=${JSON.stringify(lastTexts)}`,
  );
}

/** A session read that refuses to keep waiting on a conversation that already died. */
async function sessionStillAlive(api: AstraApi, sessionId: string): Promise<SessionRecord> {
  const session = await api.getSession(sessionId);
  const state = String(session.state || '');
  if (TERMINAL_STATES.has(state)) {
    throw new Error(
      `session ${sessionId} reached terminal ${state} before the webhook turn finished; `
      + `last_error=${String(session.last_error ?? '')}; detail=${JSON.stringify(session)}`,
    );
  }
  return session;
}

// ── The user-bubble observer ───────────────────────────────────────────────

interface UserBubbleViolation {
  sampleIndex: number;
  marker: string;
  users: string[];
}

interface UserBubbleProjection {
  /** The most user bubbles ever rendered at once. */
  maxUserCount: number;
  /** How many samples the observer took in total. */
  sampleCount: number;
  /** Bounded evidence: the first distinct sample sets, for reading. */
  samples: string[][];
  /** Samples in which any user bubble carried forbidden text. Never bounded. */
  violationCount: number;
  /** The first such sample, kept whole. */
  firstViolation: UserBubbleViolation | null;
}

/**
 * Watch every change of the user bubbles from the first paint of the page.
 *
 * Installed as an init script because the page is entered cold and later
 * reloaded. Every sample is judged: the count is folded into `maxUserCount`
 * and the text is checked against the forbidden markers, so a bubble that
 * briefly carried engine-owned text is counted as a violation even when the
 * bounded `samples` evidence is already full. The reading is taken before the
 * reload resets the script's state.
 */
async function installUserBubbleObserver(page: Page): Promise<void> {
  await page.addInitScript(({ forbidden }: { forbidden: string[] }) => {
    const state: UserBubbleProjection = {
      maxUserCount: 0,
      sampleCount: 0,
      samples: [],
      violationCount: 0,
      firstViolation: null,
    };
    Object.assign(window, { __webhookSkillUserProjection: state });
    const lowered = forbidden.map((marker) => marker.toLowerCase());
    const start = () => {
      const sample = () => {
        const users = [...document.querySelectorAll<HTMLElement>('[data-testid="user-message"]')]
          .map((node) => String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim());
        const sampleIndex = state.sampleCount;
        state.sampleCount += 1;
        state.maxUserCount = Math.max(state.maxUserCount, users.length);
        const hitIndex = lowered.findIndex((marker) => (
          users.some((text) => text.toLowerCase().includes(marker))
        ));
        if (hitIndex !== -1) {
          state.violationCount += 1;
          if (!state.firstViolation) {
            state.firstViolation = { sampleIndex, marker: forbidden[hitIndex]!, users };
          }
        }
        const serialized = JSON.stringify(users);
        if (
          state.samples.length < 50
          && state.samples.every((existing) => JSON.stringify(existing) !== serialized)
        ) {
          state.samples.push(users);
        }
      };
      const observer = new MutationObserver(sample);
      observer.observe(document.body, { childList: true, subtree: true, characterData: true });
      Object.assign(window, { __webhookSkillUserProjectionObserver: observer });
      sample();
    };
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', start, { once: true });
    } else {
      start();
    }
  }, { forbidden: FORBIDDEN_USER_BUBBLE_TEXT });
}

/**
 * The observer's state. `disconnect` stops it first, which is what an
 * assertion wants (the reading is final) and a diagnostic read does not.
 */
async function readUserBubbleProjection(page: Page, disconnect: boolean): Promise<UserBubbleProjection> {
  return page.evaluate((stop: boolean) => {
    const target = window as typeof window & {
      __webhookSkillUserProjection?: UserBubbleProjection;
      __webhookSkillUserProjectionObserver?: MutationObserver;
    };
    if (stop) target.__webhookSkillUserProjectionObserver?.disconnect();
    if (!target.__webhookSkillUserProjection) {
      throw new Error('the user-bubble observer was never installed on this page');
    }
    return target.__webhookSkillUserProjection;
  }, disconnect);
}

// ── Failure diagnostics ────────────────────────────────────────────────────
//
// What the test knows about its scene, published as it becomes known so a
// failing run can be read without re-deriving any of it.
interface Scene {
  sessionId: string;
  agentId: string;
  deploymentId: string;
  receiptId: string;
  sentinelPath: string;
  sentinelMarker: string;
  releasePath: string;
  gateDoneMarker: string;
  announcedMarker: string;
  heldBashToolUseId: string;
  released: boolean;
}

function emptyScene(): Scene {
  return {
    sessionId: '',
    agentId: '',
    deploymentId: '',
    receiptId: '',
    sentinelPath: '',
    sentinelMarker: '',
    releasePath: '',
    gateDoneMarker: '',
    announcedMarker: '',
    heldBashToolUseId: '',
    released: false,
  };
}

let scene: Scene = emptyScene();

/** One read that either answered or failed, with the failure kept as such. */
type Probe<T> = { ok: true; value: T } | { ok: false; error: string };

async function probe<T>(read: () => T | Promise<T>): Promise<Probe<T>> {
  try {
    return { ok: true, value: await read() };
  } catch (error) {
    const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    return { ok: false, error: detail };
  }
}

function excerpt(value: unknown, length = 300): string {
  const text = typeof value === 'string' ? value : JSON.stringify(value ?? null);
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

/** The facts this spec asserts on, summarized from the native records. */
function nativeRecordSummary(entries: NativeEntry[], current: Scene): Record<string, unknown> {
  const heldBash = current.sentinelPath ? bashToolUse(entries, current.sentinelPath) : undefined;
  const heldResult = heldBash ? bashToolResult(entries, heldBash.toolUseId) : undefined;
  return {
    entry_count: entries.length,
    command_envelope_seqs: userRecordsCarrying(entries, COMMAND_NAME_ENVELOPE).map((item) => item.seq),
    skill_expansion_seqs: userRecordsCarrying(entries, SKILL_BODY_MARKER).map((item) => item.seq),
    held_bash: heldBash ?? null,
    held_bash_result: heldResult
      ? {
          seq: heldResult.seq,
          is_error: heldResult.block.is_error ?? null,
          content: excerpt(heldResult.block.content),
        }
      : null,
    announcement_seq: current.announcedMarker
      ? assistantTextCarrying(entries, current.announcedMarker)?.seq ?? null
      : null,
    tail: entries.slice(-6).map((item) => ({
      seq: item.seq,
      type: String(item.entry.type ?? ''),
      role: entryRole(item.entry),
      text: excerpt(messageTextBlocks(item.entry).join('\n'), 160),
    })),
  };
}

/**
 * Attach a read-only diagnostic record after a failed, timed-out or
 * interrupted run.
 *
 * Registered as an `afterEach` because that is where the result is real: inside
 * the test body the status still reads as passing, and a body that timed out
 * is not waited for. Every read is a separate probe whose failure is recorded
 * as its own error rather than as an empty answer, and nothing here can throw
 * past the hook, so the original assertion stays the reported failure.
 */
function attachDiagnosticsOnFailure(): void {
  test.afterEach(async ({ page, request }, testInfo) => {
    if (!FAILURE_STATUSES.has(String(testInfo.status || ''))) return;
    const api = new AstraApi(request);
    const current = { ...scene };
    const sessionId = current.sessionId;
    const noSession: Probe<never> = { ok: false, error: 'no session was accepted before the failure' };
    const gateProbe = sessionId && current.sentinelPath
      ? probe(() => gateIsRunning(api, sessionId, current.sentinelPath, current.sentinelMarker))
      : Promise.resolve<Probe<never>>({ ok: false, error: 'no gate rendezvous was established' });
    const diagnostics = {
      collected_at: new Date().toISOString(),
      status: testInfo.status,
      scene: current,
      session: sessionId ? await probe(() => api.getSession(sessionId)) : noSession,
      messages: sessionId
        ? await probe(async () => {
          const messagePage = await api.getMessages(sessionId, 50);
          return {
            durable_users: (messagePage.messages || [])
              .filter((message) => message.role === 'user')
              .map((message) => ({
                client_message_id: message.client_message_id ?? null,
                text: excerpt(messageText(message)),
              })),
            durable_assistant_lengths: (messagePage.messages || [])
              .filter((message) => message.role === 'assistant')
              .map((message) => messageText(message).length),
            active_turn_overlay: messagePage.active_turn_overlay ?? null,
            visible_user_texts: visibleMessages(messagePage)
              .filter((message) => message.role === 'user')
              .map((message) => excerpt(messageText(message))),
          };
        })
        : noSession,
      accepted_start_turns: sessionId
        ? await probe(() => acceptedStartTurns(sessionId).map((item) => ({
          client_message_id: item.client_message_id,
          content: excerpt(item.content),
        })))
        : noSession,
      native_records: sessionId
        ? await probe(() => nativeRecordSummary(mainScopeEntries(sessionId), current))
        : noSession,
      gate_running: await gateProbe,
      user_bubble_projection: await probe(() => readUserBubbleProjection(page, false)),
    };
    try {
      await testInfo.attach('webhook-skill-cold-entry-diagnostics', {
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
}

// Hook registration order is execution order. Diagnostics read the scene
// first; then session tracking and the pass-only teardown decide on it.
attachDiagnosticsOnFailure();
let agentId = '';
let deploymentId = '';
const sessions = trackSessions();
// The deployment binding is released before the Agent that owns it, on a
// passing run only.
onPassOnly(async ({ request }) => {
  if (deploymentId && agentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
  deploymentId = '';
  agentId = '';
});

test('webhook-delivered native Skill command keeps one user input through a cold first entry', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = Date.now();
  const rendezvous = `/workspace/.astrabox-e2e-webhook-skill-${runId}`;
  const sentinelPath = `${rendezvous}.started`;
  const releasePath = `${rendezvous}.release`;
  const sentinelMarker = `WEBHOOK_SKILL_GATE_STARTED_${runId}`;
  const gateDoneMarker = `WEBHOOK_SKILL_GATE_${runId}_DONE`;
  // What the reply must carry. The command's own output is not the reply.
  const announcedMarker = `WEBHOOK_SKILL_ANNOUNCED_${runId}`;
  scene = {
    ...emptyScene(),
    sentinelPath,
    sentinelMarker,
    releasePath,
    gateDoneMarker,
    announcedMarker,
  };

  const command = gateCommand({
    sentinelPath,
    sentinelMarker,
    releasePath,
    ceilingSeconds: GATE_CEILING_SECONDS,
    donePrint: gateDoneMarker,
    timeoutMessage: 'the E2E harness did not release the webhook Skill gate',
  });

  // The ONE webhook body: the Skill's slash command, then a request that states
  // audience, tone and format so the Skill's own guide needs no follow-up
  // question, and the one real Bash the harness gates on. Everything after this
  // body is the engine's own doing.
  const body = [
    `${SLASH_COMMAND} Write a short developer welcome announcement. Reference ${runId}.`,
    'Audience: the software developers joining our platform engineering team this week.',
    'Purpose: welcome them and tell them where the team works day to day.',
    'Tone: warm and informational.',
    'Format: one short title line and two short paragraphs of plain prose, under 120 words, no headings, no bullet lists, no links.',
    'Before drafting the announcement, run Bash exactly once, with this command verbatim:',
    command,
    `Wait for that Bash command to finish, then write the announcement. Its final sentence must contain the token ${announcedMarker} copied verbatim.`,
    'Copy that token character for character. Do not rewrite it as prose and do not replace its underscores with spaces.',
    'Every detail you need is stated here; do not ask a follow-up question.',
    'Do not create the release file yourself; the E2E harness owns it.',
  ].join('\n');
  const bodyFirstLine = body.split('\n')[0]!;

  // ── An Agent with the real Skill, on the deployment-proven route. ──────────
  //    The model is the deployed Agent's own; a deployment that does not carry
  //    a concrete one is a failed precondition, not a reason to pick another.
  const base = await api.defaultAgent();
  const environmentName = String(base.environment_name || '').trim();
  expect(environmentName, 'the deployed Agent must expose its Environment').not.toEqual('');
  const model = String(base.model || '').trim();
  if (!model || model.includes('*')) {
    throw new Error(
      `the deployed Agent ${JSON.stringify(base.name)} carries no concrete model route `
      + `(model=${JSON.stringify(base.model ?? null)}); this spec runs on the deployment-proven model only`,
    );
  }
  const environmentModels = await api.listEnvironmentModels(environmentName);
  if (!environmentModels.includes(model)) {
    throw new Error(
      `Environment ${JSON.stringify(environmentName)} does not expose the deployment-proven model `
      + `${JSON.stringify(model)}; have: ${environmentModels.join(', ')}`,
    );
  }
  const agent = await api.createAgent({
    name: `__e2e_webhook_skill_${runId}`,
    model,
    environment_name: environmentName,
    skills: [SKILL_DESCRIPTOR],
  });
  agentId = agent.agent_id;
  scene.agentId = agentId;
  test.info().annotations.push({ type: 'e2e_agent_id', description: agentId });

  const deployment = await platform.createDeployment(agentId, {
    scene: 'hmac',
    prompt_prefix: '',
  });
  deploymentId = String(deployment.deployment_id || '');
  scene.deploymentId = deploymentId;
  const secret = String(deployment.secret || '');
  expect(deploymentId).not.toEqual('');
  expect(secret).not.toEqual('');
  test.info().annotations.push({ type: 'e2e_deployment_id', description: deploymentId });

  // ── The single accepted trigger. ───────────────────────────────────────────
  const accepted = await platform.triggerHmacAccepted(deploymentId, secret, Buffer.from(body, 'utf-8'));
  expect(accepted.status).toBe('accepted');
  const sessionId = String(accepted.session_id || '').trim();
  if (sessionId) sessions.push(sessionId);
  scene.sessionId = sessionId;
  expect(sessionId, 'an accepted trigger names the Session it started').not.toEqual('');
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  // The receipt id the relay stamps on the input it delivers
  // (`deployment_service._fire_when_ready`). It is asserted, not searched for:
  // a second identity here would mean the input was re-minted on the way.
  const receiptId = `deployment:${deploymentId}:${sessionId}`;
  scene.receiptId = receiptId;

  // ── Before any page opens: the vendor's own evidence, in the database. ─────
  //    The command envelope and the Skill body are user-side records the CLI
  //    wrote, the request's Bash is recorded with no result, and that command
  //    is observably running in the box. Only then is the window open.
  let heldBashToolUseId = '';
  await expect
    .poll(async () => {
      await sessionStillAlive(api, sessionId);
      const entries = mainScopeEntries(sessionId);
      if (entries.length === 0) return 'no native records mirrored yet';
      const envelope = userRecordsCarrying(entries, COMMAND_NAME_ENVELOPE);
      if (envelope.length === 0) return `no user record carries ${COMMAND_NAME_ENVELOPE} yet`;
      const expansion = userRecordsCarrying(entries, SKILL_BODY_MARKER);
      if (expansion.length === 0) {
        return `command envelope at seq ${envelope[0]!.seq}, Skill body not expanded on the user side yet`;
      }
      const bash = bashToolUse(entries, sentinelPath);
      if (!bash) return `Skill expanded at seq ${expansion[0]!.seq}, the requested Bash tool_use is absent`;
      if (bashToolResult(entries, bash.toolUseId)) {
        return `Bash ${bash.toolUseId} already carries its result; the window has closed`;
      }
      if (!(await gateIsRunning(api, sessionId, sentinelPath, sentinelMarker))) {
        return `Bash ${bash.toolUseId} is recorded but its command is not running`;
      }
      heldBashToolUseId = bash.toolUseId;
      scene.heldBashToolUseId = heldBashToolUseId;
      return 'held';
    }, {
      timeout: GATE_TIMEOUT_MS,
      intervals: [1_000, 2_000],
      message:
        'the webhook Skill turn must reach its held Bash with the native command envelope and '
        + "Skill expansion already in the database SessionStore; this spec's window is that held command",
    })
    .toBe('held');
  expect(heldBashToolUseId, 'the held Bash must have a native tool_use id').not.toEqual('');

  const windowEntries = mainScopeEntries(sessionId);
  const envelopeRecords = userRecordsCarrying(windowEntries, COMMAND_NAME_ENVELOPE);
  const expansionRecords = userRecordsCarrying(windowEntries, SKILL_BODY_MARKER);
  test.info().annotations.push({
    type: 'e2e_native_skill_records',
    description: JSON.stringify({
      command_envelope: envelopeRecords.map((item) => item.seq),
      skill_expansion: expansionRecords.map((item) => item.seq),
      held_bash_tool_use_id: heldBashToolUseId,
    }),
  });
  expect(
    envelopeRecords,
    'the native SessionStore must retain the command envelope as user-side truth exactly once',
  ).toHaveLength(1);
  expect(
    expansionRecords.length,
    'Claude Code must expand the webhook slash command as a user-side Skill envelope',
  ).toBeGreaterThan(0);

  // The turn is genuinely active: a current turn, and no completed terminal
  // for it. Read from the session record, not inferred from the held command.
  const active = await api.getSession(sessionId);
  test.info().annotations.push({ type: 'e2e_active_session', description: JSON.stringify(active) });
  expect(
    String(active.current_turn_id || ''),
    'the webhook turn must still be current while its Bash is held',
  ).not.toEqual('');
  expect(
    active.last_turn_id === active.current_turn_id && active.last_turn_status === 'COMPLETED',
    'the held turn must not already carry a completed terminal',
  ).toBe(false);

  // ── One accepted invocation, one delivered input: the durable receipt. ─────
  const startTurns = acceptedStartTurns(sessionId);
  expect(
    startTurns,
    'one webhook trigger must accept exactly one StartTurn carrying the raw body under the deployment receipt id',
  ).toEqual([{ content: body, client_message_id: receiptId }]);

  // ── First page entry, cold, inside the window. ─────────────────────────────
  await installUserBubbleObserver(page);
  await openSessionView(page, sessionId);

  const cold = await api.getMessages(sessionId, 50);
  test.info().annotations.push({
    type: 'e2e_cold_entry_overlay',
    description: JSON.stringify(cold.active_turn_overlay ?? null),
  });

  // (1) The durable history holds the one external input, byte-for-byte,
  //     under the relay's own receipt id — never the Skill's expansion.
  const durableUsers = (cold.messages || []).filter((message) => message.role === 'user');
  expect(
    durableUsers.map(messageText),
    'the Skill expansion is engine-owned; only the webhook body belongs to user history',
  ).toEqual([body]);
  expect(
    String(durableUsers[0]!.client_message_id || '').trim(),
    'the surviving user message must still be the relay delivery, not a re-minted one',
  ).toEqual(receiptId);

  // (2) The active FIFO carries the same external input, never Skill context.
  expect(
    cold.active_turn_overlay,
    'a running webhook turn must be published as an active turn, '
    + 'otherwise a cold entry reads a working conversation as settled',
  ).toBeTruthy();
  expect(
    overlayMessages(cold).filter((message) => message.role === 'user').map((message) => ({
      message_id: message.message_id,
      turn_id: message.turn_id,
      client_message_id: message.client_message_id,
      content: messageText(message),
    })),
    'the active FIFO must retain exactly the original webhook input and its identity, not Skill context',
  ).toEqual([{
    message_id: durableUsers[0]!.message_id,
    turn_id: active.current_turn_id,
    client_message_id: receiptId,
    content: body,
  }]);

  // (3) The console's own fold of durable + overlay still shows one user.
  expect(
    visibleMessages(cold).filter((message) => message.role === 'user').map(messageText),
    'the console projection must not gain a user message while the Skill turn runs',
  ).toEqual([body]);

  // (4) The browser agrees: one bubble, the command, and nothing engine-owned.
  const userBubbles = page.getByTestId('user-message');
  await expect(userBubbles, 'cold entry must render only the webhook input').toHaveCount(1);
  await expect(userBubbles.first()).toContainText(bodyFirstLine);
  await expect(userBubbles.first()).not.toContainText(SKILL_BODY_MARKER);
  await expect(userBubbles.first()).not.toContainText(COMMAND_MESSAGE_TAG);
  await expect(userBubbles.first()).not.toContainText(COMMAND_NAME_TAG);
  await expect(userBubbles.first()).not.toContainText(/Received a webhook event|webhook payload/i);

  // (5) The header reads the run as active. Asserted on the machine-readable
  //     state, not the label: the label is translated.
  //     Scoped to the run view: the sidebar renders one pill per conversation.
  const headerPill = page.getByTestId('run-view').getByTestId('status-pill').first();
  await expect(
    headerPill,
    'cold entry must not label a running webhook turn as ready',
  ).toHaveAttribute('data-state', 'PROCESSING', { timeout: 30_000 });
  await expect(headerPill).toHaveAttribute('data-pulse', 'true');

  // ── Release the gate and let the turn finish for real. ─────────────────────
  //    This is the only release. A run that fails before this point keeps its
  //    gate held for diagnosis; the in-box ceiling ends it.
  await api.runTerminalCommand(sessionId, `touch ${releasePath}`, '/workspace', 30_000);
  scene.released = true;
  const answer = await waitForAssistantTextMatching(
    api,
    sessionId,
    new RegExp(announcedMarker),
    TURN_TIMEOUT_MS,
  );
  expect(messageText(answer).trim(), 'the Skill turn must produce an assistant response').not.toEqual('');

  // The held command really executed, and the reply came after its result.
  await expect
    .poll(() => {
      const entries = mainScopeEntries(sessionId);
      const result = bashToolResult(entries, heldBashToolUseId);
      if (!result) return 'the held Bash has no native result yet';
      if (result.block.is_error === true) {
        return `the held Bash failed: ${JSON.stringify(result.block.content).slice(0, 200)}`;
      }
      if (!JSON.stringify(result.block.content ?? null).includes(gateDoneMarker)) {
        return 'the held Bash result does not carry the command output';
      }
      const reply = assistantTextCarrying(entries, announcedMarker);
      if (!reply) return 'the announcement has not been written yet';
      if (reply.seq <= result.seq) {
        return `the reply at seq ${reply.seq} precedes its own tool result at seq ${result.seq}`;
      }
      return 'answered after its result';
    }, {
      timeout: TURN_TIMEOUT_MS,
      intervals: [1_000, 2_000],
      message: 'the webhook Skill turn must finish its real Bash and answer after it',
    })
    .toBe('answered after its result');

  const settled = await api.waitForSession(sessionId, (row) => (
    String(row.state || '') === 'READY'
    && !row.current_turn_id
    && String(row.last_turn_status || '') === 'COMPLETED'
  ), TURN_TIMEOUT_MS);
  expect(settled.pending_interaction ?? null, 'a finished Skill turn leaves nothing pending').toBeNull();
  const commandNames = (Array.isArray(settled.slash_commands) ? settled.slash_commands : [])
    .map((item) => {
      if (typeof item === 'string') return item;
      if (!item || typeof item !== 'object') return '';
      const detail = item as Record<string, unknown>;
      return String(detail.name || detail.command || '');
    })
    .map((name) => name.replace(/^\/+/, '').trim())
    .filter(Boolean);
  expect(
    commandNames,
    `the completed webhook session must expose the Skill command it executed; have ${JSON.stringify(commandNames)}`,
  ).toContain(SKILL_COMMAND_NAME);

  // Still exactly one accepted input after completion: the release, the
  // Skill's expansion and the reply added nothing to the platform's queue.
  expect(
    acceptedStartTurns(sessionId),
    'completion must not add a second accepted input',
  ).toEqual([{ content: body, client_message_id: receiptId }]);
  expect(
    (settled.pending_inputs as unknown[] | undefined) ?? [],
    'a settled webhook session holds no pending input',
  ).toEqual([]);

  // ── Every user-bubble change the page saw, from cold entry to completion. ───
  await expect(headerPill).toHaveAttribute('data-pulse', 'false', { timeout: 30_000 });
  await expect(page.getByTestId('assistant-message').last()).toContainText(announcedMarker, { timeout: 30_000 });
  const liveProjection = await readUserBubbleProjection(page, true);
  test.info().annotations.push({
    type: 'e2e_live_user_projection',
    description: JSON.stringify(liveProjection),
  });
  expect(
    liveProjection.maxUserCount,
    `the first live page must never render internal Skill context as extra user messages; evidence=${JSON.stringify(liveProjection)}`,
  ).toBe(1);
  expect(
    liveProjection.violationCount,
    'no sample of the user bubbles may carry the Skill body, the command envelope or a platform wrapper; '
    + `first violation=${JSON.stringify(liveProjection.firstViolation)}`,
  ).toBe(0);
  const sampledText = JSON.stringify(liveProjection.samples);
  expect(sampledText, 'no sampled user bubble may carry the Skill body').not.toContain(SKILL_BODY_MARKER);
  expect(sampledText, 'no sampled user bubble may carry the command envelope').not.toContain(COMMAND_NAME_TAG);
  expect(sampledText, 'no sampled user bubble may carry the command message').not.toContain(COMMAND_MESSAGE_TAG);

  // ── A cold reload still shows the same one external user input. ────────────
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await expect(page.getByTestId('user-message')).toHaveCount(1);
  await expect(page.getByTestId('user-message').first()).toContainText(bodyFirstLine);
  await expect(page.getByTestId('user-message').first()).not.toContainText(SKILL_BODY_MARKER);
  await expect(page.getByTestId('user-message').first()).not.toContainText(COMMAND_NAME_TAG);
  const reloaded = await api.getMessages(sessionId, 50);
  expect(
    visibleMessages(reloaded).filter((message) => message.role === 'user').map(messageText),
    'the settled history must still carry the one webhook input, unchanged',
  ).toEqual([body]);
  expect(reloaded.active_turn_overlay ?? null, 'a settled conversation publishes no active overlay').toBeNull();
  // The vendor's expansion is still in its own Store — retained, never shown.
  const finalEntries = mainScopeEntries(sessionId);
  expect(userRecordsCarrying(finalEntries, COMMAND_NAME_ENVELOPE)).toHaveLength(1);
  expect(userRecordsCarrying(finalEntries, SKILL_BODY_MARKER).length).toBeGreaterThan(0);
});
