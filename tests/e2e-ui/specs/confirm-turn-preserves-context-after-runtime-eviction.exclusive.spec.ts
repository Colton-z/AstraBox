/**
 * E2E: a follow-up turn preserves conversation context after the server evicts
 * its cached runtime.
 *
 * The first composer turn establishes a unique marker and persists both a
 * sandbox_id and engine_session_key. The admin eviction drops only the in-memory
 * runtime. A second composer turn must remember the marker while retaining the
 * same sandbox and native engine session id. Stable identifiers provide the
 * deterministic continuity check; the visible reply confirms the user-facing
 * behavior.
 *
 * Both prompts forbid tools, so this scenario does not depend on an interaction
 * capability probe.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, messageText, type AdminSessionRecord } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { apiPath } from '../fixtures/env';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

// The opaque native resume handle is available only to the operator detail seam.
const engineSessionKey = (session: AdminSessionRecord): string =>
  String(session.engine_session_key || '').trim();

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('confirm turn preserves prior context after injected runtime eviction', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const marker = `E2E_AGENT_MEMORY_${runId}`;

  // Per-session sandbox: the seeded default agent is pure metadata, so one
  // conversation on it is fully isolated (no shared-agent sandbox to collide with).
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  try {
    await api.waitForSessionReady(sessionId);

    await page.setViewportSize({ width: 1280, height: 720 });
    await openSessionView(page, sessionId);

    // Lose the first response only after the production backend accepted it.
    // The console must replay the same durable command identity: a new id would
    // duplicate the user message and start a second turn.
    const observedClientMessageIds: string[] = [];
    let discardedFirstReceipt = false;
    await page.route(`**${apiPath(`/sessions/${sessionId}/turn-inputs`)}`, async (route) => {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      observedClientMessageIds.push(String(body.client_message_id || '').trim());
      if (!discardedFirstReceipt) {
        discardedFirstReceipt = true;
        await route.fetch();
        await route.abort('connectionreset');
        return;
      }
      await route.continue();
    });

    // ── Turn 1: establish context with a verifiable marker. ─────────────────
    const firstBefore = await api.assistantCount(sessionId);
    await sendPrompt(
      page,
      sessionId,
      [
        `这是一个会话记忆回归测试。请记住上下文标记 ${marker}。`,
        `你第一轮回复必须包含 ${marker}。`,
        `下一轮我只会发送“确认”;收到“确认”时,你必须继续基于当前上下文回复,并再次包含 ${marker}。`,
        '不要使用工具。',
      ].join('\n'),
    );
    expect(observedClientMessageIds.length).toBeGreaterThanOrEqual(2);
    expect(observedClientMessageIds[0]).not.toEqual('');
    expect(new Set(observedClientMessageIds)).toEqual(new Set([observedClientMessageIds[0]]));
    await page.unroute(`**${apiPath(`/sessions/${sessionId}/turn-inputs`)}`);
    // The user's own bubble renders immediately — the composer submit registered.
    await expect(page.getByTestId('user-message').last()).toContainText(marker, { timeout: 15_000 });

    const firstAssistant = await api.waitForAssistantMessageMatching(
      sessionId,
      firstBefore,
      (message) => messageText(message).includes(marker),
    );
    const firstText = messageText(firstAssistant);
    expect(firstText, 'turn 1 assistant should acknowledge the memory marker').toContain(marker);

    const firstReady = await api.waitForSessionReady(sessionId);
    const sessionSandboxId = String(firstReady.sandbox_id || '').trim();
    expect(sessionSandboxId, 'session should have a sandbox_id after turn 1').not.toEqual('');

    // The native resume handle is private runtime state. Wait for it through the
    // operator detail seam rather than widening the owner-facing Session DTO.
    const firstSession = await api.waitForAdminSession(
      sessionId,
      (s) => engineSessionKey(s) !== '',
    );
    const firstEngineSessionKey = engineSessionKey(firstSession);
    expect(
      firstEngineSessionKey,
      'turn 1 should persist the native engine session id for later resume',
    ).not.toEqual('');

    // ── FAULT: evict the in-memory runtime before the confirm turn. ─────────
    await test.step('inject runtime eviction before confirm turn', async () => {
      await api.adminEvictRuntime(sessionId);
      test.info().annotations.push({
        type: 'injected_fault',
        description: 'in-memory runtime evicted before confirm turn',
      });
    });
    // Eviction drops the cached runtime only — the session keeps its sandbox and
    // stays reachable (no persisted runtime_unavailable / state flip).
    const afterEvict = await api.getSession(sessionId);
    expect(
      String(afterEvict.sandbox_id || '').trim(),
      'eviction must not drop the session sandbox',
    ).toBe(sessionSandboxId);

    // ── Turn 2: "确认" — must cold-load and resume the SAME conversation. ────
    const secondBefore = await api.assistantCount(sessionId);
    await sendPrompt(page, sessionId, '确认');

    const secondAssistant = await api.waitForAssistantMessageMatching(
      sessionId,
      secondBefore,
      (message) => messageText(message).includes(marker),
    );
    const secondText = messageText(secondAssistant);
    expect(
      secondText,
      'confirm turn should remember the marker from the previous user turn (context preserved across eviction), not start fresh',
    ).toContain(marker);
    // The marker is the user-visible continuity proof. Free-form wording can
    // still offer a next step after recalling it; the stable identifiers below
    // decide whether this was a resumed engine conversation rather than a guess.
    // The assistant reply rendered in the browser (this is a UI spec).
    await expect(page.getByTestId('assistant-message').last()).toBeVisible({ timeout: 15_000 });

    // Same sandbox: runtime eviction dropped the cached process, not the sandbox.
    const secondReady = await api.waitForSessionReady(sessionId);
    expect(
      String(secondReady.sandbox_id || '').trim(),
      'confirm turn should stay on the same session sandbox',
    ).toBe(sessionSandboxId);

    // Deterministic proof of resume: the operator-only native engine session id
    // is unchanged, while the public path above proves the user-visible result.
    const secondSession = await api.adminSessionDetail(sessionId);
    expect(
      engineSessionKey(secondSession),
      'confirm turn must resume the same native conversation id',
    ).toBe(firstEngineSessionKey);
  } finally {
    // The session is not deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known.
    await page.close({ runBeforeUnload: false }).catch(() => {});
  }
});
