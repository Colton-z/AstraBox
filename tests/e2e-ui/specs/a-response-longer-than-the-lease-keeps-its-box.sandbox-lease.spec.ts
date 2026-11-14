/**
 * E2E: a response that runs longer than the conversation lease keeps its box.
 *
 * The server runs with a conversation lease of a minute
 * (ASTRABOX_SANDBOX_LEASE_SECONDS=60, ASTRABOX_SANDBOX_LEASE_RENEW_THRESHOLD_SECONDS=45),
 * and the run says so with ASTRABOX_E2E_SANDBOX_LEASE=1; without it Playwright
 * does not collect this file (playwright.config.ts).
 *
 * The conversation runs on a box of its own (conversation tenancy). An Agent's
 * shared box is renewed by the prepared-capacity sweep whatever its
 * conversations do, so it cannot show this.
 *
 * One response runs a foreground command for longer than the whole lease. The
 * runtime's status stays "working" from the response's start to its end, so
 * only a renewal that holds while the runtime works keeps the box. The box's
 * own expiry, read from the sandbox inventory, is the oracle: it is renewed
 * past anything a renewal at the response's start could give, the response
 * ends with the command's output, and the conversation is still on the same
 * running box.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, messageText, type ResponseRecord } from '../fixtures/astraApi';
import { engineProfileFor, modeName } from '../fixtures/engineProfile';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

// The deployment shape this spec needs, deliberately not env-tunable: the
// command has to outlast the lease within the suite's 180-second budget.
const LONGEST_LEASE_MS = 90_000;
const OUTLAST_BY_S = 40;
// A renewal at the response's start gives at most the lease from then; one
// later than this proves the lease was renewed while the response ran.
const RENEWED_DURING_MS = 20_000;
// How long the model may take to start the command and reply after it.
const REPLY_AFTER_COMMAND_MS = 45_000;

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

function expiry(box: Record<string, unknown>): number {
  const parsed = Date.parse(String(box.expires_at || ''));
  expect(Number.isFinite(parsed), `the box must report its expiry: ${JSON.stringify(box)}`).toBe(true);
  return parsed;
}

test('a response that runs longer than the lease keeps its box until it ends', async ({ request }) => {
  const api = new AstraApi(request);
  const profile = engineProfileFor('claude_code');
  const agent = await api.createColdTestAgent(`__e2e_long-response-lease-${Date.now().toString(36)}`);
  agentId = agent.agent_id;
  const sessionId = (await api.startConversation(agentId)).session_id;
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId, 60_000);
  await api.setPermissionMode(sessionId, modeName(profile, 'unattended'));
  const boxId = String(ready.sandbox_id || '');
  expect(boxId, 'a ready conversation names its box').not.toEqual('');

  const leaseMs = expiry(await api.getSandbox(boxId)) - Date.now();
  expect(
    leaseMs,
    'this spec needs a deployment whose conversation lease is a minute (ASTRABOX_SANDBOX_LEASE_SECONDS=60)',
  ).toBeLessThanOrEqual(LONGEST_LEASE_MS);
  const sleepS = Math.ceil(leaseMs / 1000) + OUTLAST_BY_S;
  const marker = `LEASE${Date.now().toString(36)}`;

  const sentAt = Date.now();
  await api.postTurnInput(
    sessionId,
    `Use the Bash tool once, in the foreground with timeout 600000, to run exactly: sleep ${sleepS} && echo ${marker} `
      + 'Then reply with only the line it printed.',
  );
  await expect.poll(async () => expiry(await api.getSandbox(boxId)) - sentAt, {
    message: 'the box is renewed while the response runs',
    timeout: sleepS * 1_000,
    intervals: [2_000],
  }).toBeGreaterThan(leaseMs + RENEWED_DURING_MS);

  // The conversation's only response, once it has ended.
  let ended: ResponseRecord | null | undefined;
  await expect.poll(async () => {
    const session = await api.getSession(sessionId);
    ended = session.last_response;
    return (session.current_responses?.length ?? 0) === 0 && ended?.status === 'ended';
  }, {
    message: 'the response ends',
    // The command runs sleepS from about when the input was sent; the reply follows it.
    timeout: Math.max(1_000, sentAt + sleepS * 1_000 + REPLY_AFTER_COMMAND_MS - Date.now()),
    intervals: [2_000],
  }).toBe(true);
  expect(ended, 'the response ends without losing its box').toMatchObject({ outcome: 'completed' });
  const replies = (await api.getMessages(sessionId)).messages.filter((message) => message.role === 'assistant');
  expect(replies.map(messageText).join('\n')).toContain(marker);
  const after = await api.getSession(sessionId);
  expect(after.sandbox_id, 'the conversation is still on the box it started in').toEqual(boxId);
  expect(String((await api.getSandbox(boxId)).state).toLowerCase()).toEqual('running');
});
