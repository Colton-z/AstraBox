/**
 * E2E: an agent-chat session remains readable after its Agent is deleted.
 *
 * Deleting the Agent must preserve the session identity and durable messages.
 * The session projection reports a DELETED Agent runtime without marking the
 * session unavailable or attaching an error. In the browser, the existing
 * transcript remains visible, the composer is disabled, and the header shows a
 * settled terminal state rather than a recovery warning.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { appPath } from '../fixtures/env';

// One sandbox provision + one turn + the agent-deletion projection + the UI
// re-open sit above the 240s suite default; mirror the lifecycle budget.

// Teardown that changes state runs on the passing path only, and hook
// registration order is the old `finally` order: afterEach hooks run in the
// order they are registered, so the session goes before the agent it ran on.
// A kept session pointing at a deleted agent is half a scene.
let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('agent_chat history remains readable after agent deletion', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  // A deterministic marker carried in the USER prompt — the durable-readability
  // oracle keys off this (exactly what the test sent), not a model-produced phrase.
  const marker = `E2E-DEL-AGENT-HISTORY-${runId}`;

  let sessionId = '';
  try {
    // ── Isolated agent (pure metadata, no sandbox). Source a concrete,
    //    non-wildcard model from the seeded default agent's environment
    //    catalogue — the create validator rejects a blank model. (Same
    //    pattern as the per-session-sandbox spec.) ────────────────────────────
    const base = await api.defaultAgent();
    const environmentName = String(base.environment_name || '').trim();
    expect(environmentName, 'seeded default agent must name an environment').not.toEqual('');
    const models = await api.listEnvironmentModels(environmentName);
    const model = models.find((m) => m && !m.includes('*')) || 'deepseek-chat';

    const agent = await api.createAgent({
      name: `__e2e_del_history_${runId}`,
      model,
      environment_name: environmentName,
    });
    agentId = String(agent.agent_id || '');
    expect(agentId, 'created agent must have an id').not.toEqual('');

    // ── One real turn so the conversation has durable history. ──────────────
    const created = await api.startConversation(agentId);
    sessionId = created.session_id;
    sessions.push(sessionId);
    await api.waitForSessionReady(sessionId);
    const before = await api.assistantCount(sessionId);
    await api.streamPrompt(sessionId, `${marker}: 不要使用工具，请简短回复一句话。`);
    const assistant = await api.waitForAssistantMessageMatching(
      sessionId,
      before,
      (message) => messageText(message).trim() !== '',
    );
    const assistantText = messageText(assistant).trim();
    expect(assistantText, 'the pre-deletion turn should produce durable assistant text').not.toEqual('');

    // Settle back to READY (with the conversation's own live sandbox) BEFORE
    // deleting the agent, so the post-deletion binding resolves READY and the
    // session reads runtime_unavailable=false.
    await api.waitForSessionReady(sessionId);

    // ── Delete the agent. In the per-session model this tears down only the
    //    agent runtime (which owns nothing); the conversation keeps its own
    //    sandbox and its whole history. ────────────────────────────────────────
    const deleted = await api.deleteAgent(agentId);
    expect(String(deleted.state || ''), 'delete should soft-delete the agent → DELETED').toEqual('DELETED');

    // ── Durable-state oracle: the session record survives, keeps its identity,
    //    and is projected as a NORMAL terminal runtime (not abnormal). ─────────
    const historical = await api.getSession(sessionId);
    expect(historical.session_id, 'session_id survives agent deletion').toBe(sessionId);
    expect(String(historical.agent_id || ''), 'agent_id survives agent deletion').toBe(agentId);
    expect(String(historical.session_kind || ''), 'session stays an agent_chat record').toBe('agent_chat');
    expect(
      Boolean(historical.runtime_unavailable),
      'deleted-agent history must NOT be marked runtime_unavailable',
    ).toBe(false);
    expect(
      String(historical.last_error || '').trim(),
      'deleted-agent history must NOT expose a last_error',
    ).toBe('');

    const runtime = (historical.agent_runtime || {}) as Record<string, unknown>;
    expect(
      String(runtime.state || ''),
      'the deleted agent is projected as a normal terminal DELETED runtime',
    ).toBe('DELETED');
    expect(
      Boolean(runtime.runtime_unavailable),
      'the deleted-agent runtime view must NOT be abnormal',
    ).toBe(false);
    expect(
      String(runtime.last_error || '').trim(),
      'the deleted-agent runtime view must NOT expose a last_error',
    ).toBe('');

    // ── History remains READABLE after deletion: both the user marker (the
    //    text the test sent — deterministic) and the captured assistant reply
    //    persist. ─────────────────────────────────────────────────────────────
    const afterPage = await api.getMessages(sessionId);
    const joined = afterPage.messages.map((m) => messageText(m)).join('\n');
    expect(joined, 'the user message must remain readable after agent deletion').toContain(marker);
    expect(joined, 'the assistant reply must remain readable after agent deletion').toContain(assistantText);

    // ── In the browser: the transcript still renders, the composer is
    //    read-only, and the header presents a calm terminal state. ─────────────
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });

    await expect(
      page.getByTestId('user-message').filter({ hasText: marker }).last(),
      'the historical user message renders after agent deletion',
    ).toBeVisible({ timeout: 45_000 });
    await expect(
      page.getByTestId('assistant-message').last(),
      'the historical assistant reply renders after agent deletion',
    ).toBeVisible({ timeout: 45_000 });

    // A DELETED agent runtime forces canSend=false. With no pending interaction,
    // the disabled textarea is the page's read-only history state.
    const textarea = page.getByTestId('composer-prompt');
    await expect(textarea, 'the composer is read-only for deleted-agent history').toBeDisabled({ timeout: 30_000 });
    await expect(
      textarea,
      'the read-only composer still shows a placeholder hint',
    ).toHaveAttribute('placeholder', /\S/);

    // The run-view header presents a calm terminal state: TERMINATED with no
    // pulse distinguishes archived history from a live or recoverable session.
    const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
    await expect(
      pill,
      'deleted-agent history reads as a terminal record',
    ).toHaveAttribute('data-state', 'TERMINATED', { timeout: 30_000 });
    await expect(
      pill,
      'deleted-agent history is not live/generating',
    ).toHaveAttribute('data-pulse', 'false');
  } finally {
    // The session and the agent are not released here. `trackSessions()` and
    // `onPassOnly()` decide in afterEach hooks, where the test's real status is
    // known — see that fixture on why a `finally` cannot tell it is unwinding
    // from a failure, and on why the unit is the whole block.
    // Delete the conversation FIRST — that releases the per-session sandbox that
    // delete_agent deliberately left alive — then best-effort delete the agent
    // (already soft-deleted mid-test; harmless if it 404s / no-ops).
  }
});
