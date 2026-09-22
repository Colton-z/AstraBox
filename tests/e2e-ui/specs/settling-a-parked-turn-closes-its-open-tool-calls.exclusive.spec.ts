/**
 * E2E: interrupting a turn parked at a permission prompt leaves no tool call
 * without a terminal.
 *
 * A gated tool call has announced its input and is waiting for a decision that
 * never comes. Settling the turn from outside — an interrupt, a reclaim — ends
 * the turn, and the tool call it was blocked on has to end with it: a durable
 * `tool-input-available` with no matching output is a card that spins for as
 * long as anyone looks at the conversation, on a turn that finished.
 *
 * The assertion is on durable frames rather than on the rendered card. The
 * card is a projection of these frames, so a reader that renders the missing
 * terminal as "finished" would hide the defect from a visual check while every
 * later reader — a resume, an export, a reload — still sees the open call.
 *
 * The parked state is reached the way a user reaches it: an unanswered
 * permission prompt. Nothing is written to the store.
 */
import { test, expect } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi, type PendingInteraction } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { parseTimeoutEnv } from '../fixtures/env';
import { framesForTurn } from '../fixtures/dbOracle';

/** What a user says when a model answers instead of acting. */
const INSIST_NUDGE =
  "You answered without using the tool. Do the tool call now, exactly as asked above, before replying again. \u8bf7\u73b0\u5728\u5c31\u6309\u4e0a\u9762\u7684\u8981\u6c42\u8c03\u7528\u5de5\u5177\uff0c\u4e0d\u8981\u53ea\u7528\u6587\u5b57\u56de\u7b54\u3002";

const PROMPT_MS = parseTimeoutEnv('ASTRABOX_E2E_PARKED_TOOL_PROMPT_TIMEOUT_MS', 90_000);
const SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_PARKED_TOOL_SETTLE_TIMEOUT_MS', 180_000);
// The interaction authority commits before its queued frames finish writing.
// This budget covers only that projection interval, not model time, which has
// already elapsed by the point of use.
const FRAME_PROJECTION_MS = 60_000;

const sessions = trackSessions();

type ToolCallState = { announced: Set<string>; terminal: Set<string> };
type FrameReader = (turnId: string) => Record<string, unknown>[];

/** The canonical engine frame stored inside a `session_events` document. */
function framePayload(frame: Record<string, unknown>): Record<string, unknown> {
  const payload = frame.payload;
  return payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {};
}

/** Tool call ids that announced an input, and those that reached a terminal. */
function toolCallState(frames: Record<string, unknown>[]): ToolCallState {
  const announced = new Set<string>();
  const terminal = new Set<string>();
  for (const frame of frames) {
    const payload = framePayload(frame);
    const type = String(payload.type || '');
    const id = String(payload.toolCallId || '').trim();
    if (!id) continue;
    if (type.startsWith('tool-input-available')) announced.add(id);
    if (type.startsWith('tool-output-')) terminal.add(id);
  }
  return { announced, terminal };
}

/** Wait until the pending gate's exact tool call is durably observable. */
async function waitForAnnouncedToolCall(
  turnId: string,
  toolCallId: string,
  timeoutMs: number,
  readFrames: FrameReader = framesForTurn,
  pollMs = 1_000,
): Promise<ToolCallState> {
  const deadline = Date.now() + timeoutMs;
  let seenTypes: string[] = [];
  let seenAnnounced: string[] = [];
  for (;;) {
    const frames = readFrames(turnId);
    const state = toolCallState(frames);
    if (state.announced.has(toolCallId)) return state;
    seenTypes = frames.map((frame) => String(framePayload(frame).type || ''));
    seenAnnounced = [...state.announced];
    if (Date.now() >= deadline) break;
    await new Promise((resolve) => setTimeout(resolve, pollMs));
  }
  throw new Error(
    `turn ${turnId} did not durably announce gated tool call ${toolCallId} within ${timeoutMs}ms; ` +
      `durable frame types were [${seenTypes.join(', ')}], announced ids were ` +
      `[${seenAnnounced.join(', ')}]`,
  );
}

test('the durable-frame oracle waits for the gated call in the stored payload', async () => {
  let reads = 0;
  const readFrames = (): Record<string, unknown>[] => {
    reads += 1;
    if (reads === 1) {
      return [
        {
          // A top-level lookalike must not satisfy an oracle for the stored payload.
          type: 'tool-input-available',
          toolCallId: 'toolu_gated',
          payload: { type: 'tool-input-available', toolCallId: 'toolu_other' },
        },
      ];
    }
    return [{ payload: { type: 'tool-input-available', toolCallId: 'toolu_gated' } }];
  };

  const observed = await waitForAnnouncedToolCall(
    'turn-1',
    'toolu_gated',
    100,
    readFrames,
    0,
  );
  expect(reads, 'the first pre-write sample must not satisfy the wait').toBe(2);
  expect(observed.announced).toEqual(new Set(['toolu_gated']));

  const settled = toolCallState([
    { payload: { type: 'tool-input-available', toolCallId: 'toolu_gated' } },
    { payload: { type: 'tool-output-denied', toolCallId: 'toolu_gated' } },
  ]);
  expect([...settled.announced].filter((id) => !settled.terminal.has(id))).toEqual([]);
});

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
  // platform. The probe returns the moment the turn settles ungated,
  // so a declined ask costs that turn rather than the whole budget.
  const pending = await insist<PendingInteraction>({
    ask: async (attempt) => {
      if (attempt > 1) await api.postTurnInput(sessionId, INSIST_NUDGE);
    },
    probe: () =>
      api.waitForPendingInteractionOrSettledTurn(sessionId, PROMPT_MS),
    what: 'the model did not call a gated tool, so no turn was parked',
    budgetMs: PROMPT_MS * 2,
    probeMs: PROMPT_MS,
  });

  // A parked turn is the CURRENT turn, not the last one: `last_turn_id` is
  // written when a turn reaches a terminal, which is precisely what has not
  // happened yet and what this test is about to cause. Waiting for it here
  // waits for a field whose meaning excludes the state the session is in, so
  // the session sits in WAITING_INPUT until the budget runs out.
  const parked = await api.waitForSession(sessionId, (s) => Boolean(s.current_turn_id), SETTLE_MS);
  const turnId = String(parked.current_turn_id || '').trim();
  expect(turnId, 'the parked turn must have an id').not.toEqual('');

  const gatedToolCallId = String(pending?.tool_call_id || '').trim();
  expect(
    gatedToolCallId,
    'the pending interaction must name the exact tool call it gates',
  ).not.toEqual('');
  const before = await waitForAnnouncedToolCall(
    turnId,
    gatedToolCallId,
    FRAME_PROJECTION_MS,
  );
  expect(
    before.terminal.has(gatedToolCallId),
    'the gated call must still be open before the interrupt, or there is no open call to close',
  ).toBe(false);

  // Settle the turn from outside, with the decision still outstanding.
  await api.interruptSession(sessionId);
  await api.waitForSessionState(sessionId, 'READY', SETTLE_MS);

  const after = toolCallState(framesForTurn(turnId));
  const open = [...after.announced].filter((id) => !after.terminal.has(id));
  expect(
    open,
    `tool calls ${JSON.stringify(open)} announced an input and never reached a ` +
      'terminal on a turn that has finished — every later reader of this ' +
      'conversation sees a call still running',
  ).toEqual([]);
});
