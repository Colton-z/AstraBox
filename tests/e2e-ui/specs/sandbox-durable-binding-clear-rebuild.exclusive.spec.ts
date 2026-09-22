/**
 * A durable binding cleared by another process invalidates this process's
 * resident runtime. The next real turn must create fresh compute and reply in
 * the same send, even though the old runtime was still cached locally.
 */
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { patchSessionDoc, sessionDoc } from '../fixtures/dbOracle';
import { parseTimeoutEnv } from '../fixtures/env';
import {
  killSandbox,
  requireSandboxHandle,
  sandboxRunning,
  waitForSandboxStopped,
} from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const TURN_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_REBORROW_TURN_TIMEOUT_MS', 240_000);
const KILL_CONVERGE_MS = parseTimeoutEnv('ASTRABOX_E2E_KILL_CONVERGE_MS', 30_000);

let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('cross-process durable binding clear invalidates the resident runtime in the next turn', async ({
  request,
}) => {
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(
    `__e2e_binding_clear_${Date.now()}_${test.info().workerIndex}`,
  );
  agentId = agent.agent_id;
  const created = await api.startConversation(agentId);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  const ready = await api.waitForSessionReady(sessionId);
  const oldSandboxId = String(ready.sandbox_id || '').trim();
  expect(oldSandboxId, 'the conversation must own compute before convergence').not.toEqual('');

  const first = await api.sendTurn(
    sessionId,
    'E2E resident runtime baseline: do not use tools; reply briefly.',
    TURN_BUDGET_MS,
  );
  expect(first.errorText, 'the baseline turn must establish a usable resident runtime').toBeNull();
  expect(first.text.trim(), 'the baseline turn must produce an ordinary reply').not.toEqual('');

  const before = patchSessionDoc(sessionId, {
    sandbox_id: null,
    sandbox_endpoint: null,
    expires_at: null,
  });
  expect(before).toHaveLength(1);
  expect(String(before[0].sandbox_id || '').trim()).toBe(oldSandboxId);
  const cleared = sessionDoc(sessionId);
  expect(cleared, 'the out-of-process repository mutation must remain readable').toBeTruthy();
  expect('sandbox_id' in (cleared || {}), 'the durable sandbox binding must be absent').toBe(false);

  const oldSandbox = await requireSandboxHandle(api, oldSandboxId);
  expect(sandboxRunning(oldSandbox), 'the old compute must be live before the fault').toBe(true);
  killSandbox(oldSandbox);
  await waitForSandboxStopped(oldSandbox, KILL_CONVERGE_MS);
  expect(sandboxRunning(oldSandbox), 'the old compute must be gone before dispatch').toBe(false);

  const assistantsBefore = await api.assistantCount(sessionId);
  const second = await api.sendTurn(
    sessionId,
    'E2E post-convergence turn: do not use tools; reply briefly.',
    TURN_BUDGET_MS,
  );
  expect(second.errorText, 'the first post-clear send must not hit the stale runtime').toBeNull();
  const assistant = await api.waitForAssistantMessageMatching(
    sessionId,
    assistantsBefore,
    (message) => messageText(message).trim() !== '',
    TURN_BUDGET_MS,
  );
  expect(messageText(assistant).trim(), 'the same send must persist a reply').not.toEqual('');

  const rebuilt = await api.waitForSessionReady(sessionId, TURN_BUDGET_MS);
  const newSandboxId = String(rebuilt.sandbox_id || '').trim();
  expect(newSandboxId, 'the turn must publish a replacement binding').not.toEqual('');
  expect(newSandboxId, 'cleared durable truth must not reuse dead resident compute').not.toBe(oldSandboxId);
  test.info().annotations.push(
    { type: 'e2e_cleared_sandbox_id', description: oldSandboxId },
    { type: 'e2e_rebuilt_sandbox_id', description: newSandboxId },
  );
});
