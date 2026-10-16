/**
 * E2E: interrupting a response paused at a permission prompt leaves no tool
 * call without a terminal.
 *
 * A gated tool call has announced its input and is waiting for a decision that
 * never comes. Settling the response from outside — an interrupt — ends it, and
 * the tool call it was blocked on has to end with it: a recorded
 * `tool-input-available` with no matching output is a card that spins for as
 * long as anyone looks at the conversation, on a response that finished.
 *
 * The assertion is on the parts the session's reader recorded for the response
 * (`session_output`) rather than on the rendered card. History and the stream
 * are built from those parts, so a page that renders the missing terminal as
 * "finished" would hide the defect from a visual check while every later
 * reader — a reload, a share, an export — still sees the open call.
 *
 * The paused state is reached the way a user reaches it: an unanswered
 * permission prompt. Nothing is written to the store.
 */
import { test, expect } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi, type PendingInteraction } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { parseTimeoutEnv } from '../fixtures/env';
import { sessionOutputRows } from '../fixtures/dbOracle';

/** What a user says when a model answers instead of acting. */
const INSIST_NUDGE =
  "You answered without using the tool. Do the tool call now, exactly as asked above, before replying again. \u8bf7\u73b0\u5728\u5c31\u6309\u4e0a\u9762\u7684\u8981\u6c42\u8c03\u7528\u5de5\u5177\uff0c\u4e0d\u8981\u53ea\u7528\u6587\u5b57\u56de\u7b54\u3002";

const PROMPT_MS = parseTimeoutEnv('ASTRABOX_E2E_PARKED_TOOL_PROMPT_TIMEOUT_MS', 90_000);
const SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_PARKED_TOOL_SETTLE_TIMEOUT_MS', 180_000);
// The reader commits parts in the order it read them, and a tool's input comes
// before the interaction that gates it, so the announced call is recorded by
// the time the interaction is visible. This budget only bounds that read; model
// time has already elapsed by the point of use.
const ANNOUNCE_RECORD_MS = 60_000;

const sessions = trackSessions();

type ToolCallState = { announced: Set<string>; terminal: Set<string> };

/** The parts the session's reader recorded for one response, in order. */
function recordedParts(sessionId: string, responseId: string): Record<string, unknown>[] {
  return sessionOutputRows(sessionId, responseId).map((row) => row.part);
}

/** Tool call ids that announced an input, and those that reached a terminal. */
function toolCallState(parts: Record<string, unknown>[]): ToolCallState {
  const announced = new Set<string>();
  const terminal = new Set<string>();
  for (const part of parts) {
    const type = String(part.type || '');
    const id = String(part.toolCallId || '').trim();
    if (!id) continue;
    if (type.startsWith('tool-input-available')) announced.add(id);
    if (type.startsWith('tool-output-')) terminal.add(id);
  }
  return { announced, terminal };
}

/** Wait until the pending gate's exact tool call is recorded for its response. */
async function waitForAnnouncedToolCall(
  sessionId: string,
  responseId: string,
  toolCallId: string,
  timeoutMs: number,
  pollMs = 1_000,
): Promise<ToolCallState> {
  const deadline = Date.now() + timeoutMs;
  let seenTypes: string[] = [];
  let seenAnnounced: string[] = [];
  for (;;) {
    const parts = recordedParts(sessionId, responseId);
    const state = toolCallState(parts);
    if (state.announced.has(toolCallId)) return state;
    seenTypes = parts.map((part) => String(part.type || ''));
    seenAnnounced = [...state.announced];
    if (Date.now() >= deadline) break;
    await new Promise((resolve) => setTimeout(resolve, pollMs));
  }
  throw new Error(
    `response ${responseId} did not record gated tool call ${toolCallId} within ${timeoutMs}ms; ` +
      `recorded part types were [${seenTypes.join(', ')}], announced ids were ` +
      `[${seenAnnounced.join(', ')}]`,
  );
}

test('interrupting a parked turn terminates the tool call it was blocked on', async ({
  request,
}) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'default');
  await api
    .streamPrompt(sessionId, 'Use the Write tool to create parked.txt containing WAIT.')
    .catch(() => undefined);

  // Ask again rather than skip: a skip ends the round exactly as a
  // failure does, so the model's choice decided it instead of the
  // platform. The probe returns the moment the response settles ungated,
  // so a declined ask costs that response rather than the whole budget.
  const pending = await insist<PendingInteraction>({
    ask: async (attempt) => {
      if (attempt > 1) await api.postTurnInput(sessionId, INSIST_NUDGE);
    },
    probe: () =>
      api.waitForPendingInteractionOrSettledTurn(sessionId, PROMPT_MS),
    what: 'the model did not call a gated tool, so no response was paused',
    budgetMs: PROMPT_MS * 2,
    probeMs: PROMPT_MS,
  });

  // The paused response is the one the interaction names: it is still open,
  // and ending it is what this test is about to cause.
  const responseId = String(pending?.response_id || '').trim();
  expect(responseId, 'the pending interaction must name the response it pauses').not.toEqual('');

  const gatedToolCallId = String(pending?.tool_call_id || '').trim();
  expect(
    gatedToolCallId,
    'the pending interaction must name the exact tool call it gates',
  ).not.toEqual('');
  const before = await waitForAnnouncedToolCall(
    sessionId,
    responseId,
    gatedToolCallId,
    ANNOUNCE_RECORD_MS,
  );
  expect(
    before.terminal.has(gatedToolCallId),
    'the gated call must still be open before the interrupt, or there is no open call to close',
  ).toBe(false);

  // Settle the response from outside, with the decision still outstanding.
  const interrupted = await api.interruptSession(sessionId) as { status?: string };
  expect(interrupted.status, 'a paused response is running work the interrupt reaches').toBe('accepted');
  const settled = await api.waitForSessionState(sessionId, 'READY', SETTLE_MS);
  expect(
    settled.last_response?.response_id,
    'the interrupted response is the last one that ended',
  ).toBe(responseId);

  const after = toolCallState(recordedParts(sessionId, responseId));
  const open = [...after.announced].filter((id) => !after.terminal.has(id));
  expect(
    open,
    `tool calls ${JSON.stringify(open)} announced an input and never reached a ` +
      'terminal on a response that has finished — every later reader of this ' +
      'conversation sees a call still running',
  ).toEqual([]);
});
