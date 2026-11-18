/**
 * E2E: a message its conversation's sandbox cannot be reconnected for fails
 * with the reason instead of waiting.
 *
 * Revision 0029 gives a conversation prepared before it the version its Agent
 * has at the upgrade. When the Agent was edited after the conversation was
 * prepared, that version is not the one its sandbox installed: here the
 * sandbox holds Plugin A and the recorded version declares Plugin B. Attaching
 * to that sandbox is refused with PACKAGE_INVALID, and attaching again fails
 * the same way.
 *
 * The spec reads
 *
 *   · the message sent after the conversation's runtime is evicted: its input
 *     fails with PACKAGE_INVALID within one attach, and no response answers it;
 *   · the conversation's history, where that message carries the failure the
 *     console shows under it, and the first exchange is kept;
 *   · the conversation, which reads as needing recovery and keeps its sandbox.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, messageText, visibleMessages } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { postgresTextRows, sessionInputRows, sessionResponseRows, sqlLiteral } from '../fixtures/dbOracle';
import { parseTimeoutEnv } from '../fixtures/env';
import { POSTGRES_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';

const RUN_ID = new Date().toISOString().replace(/[:.]/g, '-');
const REPLY_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 90_000);
// One attach and its refusal; no model call is involved.
const FAILURE_BUDGET_MS = 60_000;

// Two public plugins pinned to reviewed revisions.
const PLUGIN_A = {
  kind: 'claude-plugin',
  url: 'https://github.com/reidsolon/claude-session-stats-plugin.git',
  protocol: 'https',
  branch: 'main',
  depth: 1,
  sha: '84433f7d3740b3b1c4a238a705f3cfb17d8bd3d8',
  paths: ['.'],
};
const PLUGIN_B = {
  kind: 'claude-plugin',
  url: 'https://github.com/anthropics/financial-services.git',
  protocol: 'https',
  branch: 'main',
  depth: 1,
  sha: '3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f',
  paths: ['plugins/vertical-plugins/financial-analysis'],
};

/** Record `version` on the conversation's capability plan, the value revision 0029 writes; the ids of the rows it changed. */
function recordPlanVersion(sessionId: string, version: number): string[] {
  if (!Number.isInteger(version)) throw new Error(`an Agent version is an integer, not ${version}`);
  return postgresTextRows(
    requireServiceContainer(POSTGRES_CONTAINER_HANDLE),
    process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox',
    `UPDATE sessions SET runtime_identity = `
      + `jsonb_set(runtime_identity, '{capability_plan,agent_version}', '${version}'::jsonb) `
      + `WHERE session_id = ${sqlLiteral(sessionId)} RETURNING session_id`,
  );
}

const sessionIds = trackSessions();
const agentIds: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const agentId of agentIds.splice(0, agentIds.length)) await api.deleteAgent(agentId);
});

test('a message its sandbox cannot be reconnected for fails with the reason instead of waiting', async ({
  request,
}) => {
  const api = new AstraApi(request);
  // Each conversation of the cold RuntimeProfile owns its sandbox, so the
  // attach below reaches the sandbox that installed Plugin A.
  const runtimeProfileName = String(process.env.ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT || '').trim();
  expect(runtimeProfileName, 'ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT names the cold RuntimeProfile').not.toEqual('');
  const model = String((await api.defaultAgent()).model || '').trim();
  expect(model, 'the deployment-proven Agent names its model').not.toEqual('');

  const agent = await api.createAgent({
    name: `e2e attach refused ${RUN_ID}`,
    model,
    runtime_profile_name: runtimeProfileName,
    prepared_capacity: 0,
    packages: [PLUGIN_A],
  });
  agentIds.push(agent.agent_id);

  const sessionId = (await api.startConversation(agent.agent_id)).session_id;
  sessionIds.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  const sandboxId = String((await api.waitForSessionReady(sessionId)).sandbox_id || '');
  expect(sandboxId, 'the conversation has its sandbox').not.toEqual('');

  const firstPrompt = `E2E attach refused ${RUN_ID} first: reply with one short sentence and do not use tools.`;
  const firstInput = (await api.postTurnInput(sessionId, firstPrompt)).input_id;
  const firstReply = await api.waitForReplyTo(sessionId, firstInput, { timeoutMs: REPLY_BUDGET_MS });
  expect(firstReply.metadata?.outcome, 'the first exchange completes').toBe('completed');

  const current = await api.getAgent(agent.agent_id);
  const edited = await api.updateAgent(agent.agent_id, {
    name: current.name,
    model: current.model,
    runtime_profile_name: current.runtime_profile_name,
    packages: [PLUGIN_B],
    version: current.version,
  });
  const editedVersion = Number(edited.version);
  expect(editedVersion, 'replacing the Plugin makes a new Agent version').toBe(Number(agent.version) + 1);
  expect(recordPlanVersion(sessionId, editedVersion), 'the plan names the version that declares Plugin B')
    .toEqual([sessionId]);

  expect((await api.adminEvictRuntime(sessionId)).evicted, 'the server drops the conversation runtime').toBe(sessionId);
  const prompt = `E2E attach refused ${RUN_ID} after edit: reply with one short sentence and do not use tools.`;
  const inputId = (await api.postTurnInput(sessionId, prompt)).input_id;
  const inputRow = () => sessionInputRows(sessionId).find((row) => row.input_id === inputId);
  await expect
    .poll(() => inputRow()?.state, { timeout: FAILURE_BUDGET_MS, message: 'the message stops waiting for the sandbox' })
    .toBe('failed');
  expect(inputRow(), 'the message fails with the reason the attach was refused, and nothing answers it')
    .toEqual(expect.objectContaining({ failure: expect.objectContaining({ code: 'PACKAGE_INVALID' }), response_id: null }));
  expect(sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(inputId)), 'no response answers it')
    .toEqual([]);

  const history = visibleMessages(await api.getMessages(sessionId, 50));
  const sent = history.filter((message) => message.role === 'user');
  expect(sent.map((message) => messageText(message).trim()), 'history keeps both messages').toEqual([firstPrompt, prompt]);
  expect(
    (sent[1].metadata as Record<string, unknown> | undefined)?.failure,
    'the failed message carries the failure the console shows under it',
  ).toEqual(expect.objectContaining({ code: 'PACKAGE_INVALID' }));
  expect(
    history.filter((message) => message.role === 'assistant').map((message) => message.id),
    'history keeps the first reply',
  ).toEqual([firstReply.id]);

  expect((await api.getSession(sessionId)).state, 'the conversation reads as needing recovery').toBe('RECOVERY_REQUIRED');
  expect((await api.adminSessionDetail(sessionId)).sandbox_id, 'the conversation keeps its sandbox').toBe(sandboxId);
});
