/**
 * E2E: deleting an Agent releases the boxes its conversations hold.
 *
 * A conversation of a deleted Agent keeps its history but takes no new
 * messages, so a box it holds serves nothing. Deleting the Agent stops that box
 * at once rather than when its lease runs out; the owner still reads the
 * conversation, and a new message is refused with `AGENT_DELETED`.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, parseTimeoutEnv } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { requireSandboxHandle, sandboxRunning, waitForSandboxStopped } from '../fixtures/sandboxOps';

const KILL_CONVERGE_MS = parseTimeoutEnv('ASTRABOX_E2E_KILL_CONVERGE_MS', 30_000);

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('deleting an Agent stops the boxes of its conversations and keeps their history', async ({ request }) => {
  const api = new AstraApi(request);
  const marker = `delete-releases-${Date.now().toString(36)}`;
  const agent = await api.createColdTestAgent(`__e2e_${marker}`);
  agentId = agent.agent_id;
  const sessionId = (await api.startConversation(agentId)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  const turn = await api.sendTurn(sessionId, `Reply with exactly the word ${marker}. Do not use tools.`, 60_000);
  expect(turn.errorText).toBeNull();
  const sandboxId = String((await api.waitForSessionReady(sessionId)).sandbox_id || '');
  expect(sandboxId).not.toEqual('');
  const handle = await requireSandboxHandle(api, sandboxId);
  expect(sandboxRunning(handle)).toBe(true);

  await api.deleteAgent(agentId);
  agentId = '';

  await waitForSandboxStopped(handle, KILL_CONVERGE_MS);
  const roles = (await api.getMessages(sessionId)).messages.map((message) => message.role);
  expect(roles, 'the conversation keeps its history').toEqual(['user', 'assistant']);
  const refused = await request.fetch(apiPath(`/sessions/${sessionId}/inputs`), {
    method: 'POST', data: { content: 'Are you there?', client_message_id: `${marker}-after` },
  });
  expect(refused.status()).toEqual(409);
  expect(JSON.stringify(await refused.json())).toContain('AGENT_DELETED');
});
