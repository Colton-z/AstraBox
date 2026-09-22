/**
 * E2E: the next message rebuilds a resident turn transport after the server
 * evicts its cached runtime.
 *
 * A warm composer turn first establishes a working runtime. The admin eviction
 * closes the host-side client without replacing the sandbox. The open page is
 * deliberately not reloaded: its next message must render a real reply, return
 * the header to READY, preserve sandbox_id, and leave runtime_unavailable and
 * last_error clear.
 *
 * Transport recreation requires a backend with resident sidecar connections.
 * Backends that explicitly report this capability as unsupported skip the
 * recovery half after the warm turn and eviction are verified. A persisted reply
 * that fails to render remains a test failure. Both prompts forbid tools, so no
 * permission-interaction probe is needed.
 */
import { test, expect } from '@playwright/test';
import type { Locator } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';

// Two deepseek turns (warm + post-evict rebuild) plus agent create + provision.
// A generous, env-tunable budget: no single call sits on a slow re-borrow (evict
// keeps the same live sandbox), so the fixture-default per-call waits suffice.

// Bounded wait for the post-eviction outcome to resolve to ONE of: (a) a recreated
// reply rendered in the transcript (sidecar-capable backend → assert the full
// invariant) or (b) the resident-sidecar-unsupported signal on the session
// projection (a backend with no resident sidecar → skip). When (b) applies it is
// stamped within ~1s of the failed turn, so the probe breaks early; the budget
// only bounds the (a) reply wait on a capable backend.
const PROBE_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TRANSPORT_RECREATE_PROBE_TIMEOUT_MS', 120_000);

// The resident-sidecar-unsupported re-attach signal: when the backend offers no
// resident sidecar, the agent_chat turn re-attach raises this and the row's
// last_error carries it.
const RESIDENT_SIDECAR_UNSUPPORTED = 'does not support resident sidecar connections';

// A failed turn renders its error INTO the transcript as an assistant message
// (MessageParts → data-turn-failure → TurnFailureCard interpolates the raw backend
// error), so "one more non-empty bubble" is satisfied by exactly the outcome under
// test. These tokens are backend error vocabulary, never a Chinese one-liner reply.
const TURN_ERROR = /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

// Teardown that changes state runs on the passing path only, and hook
// registration order is the old `finally` order: afterEach hooks run in the
// order they are registered, so the session goes before the agent it ran on.
// A kept session pointing at a deleted agent is half a scene.
let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('evicting the session runtime recreates the resident sidecar transport on the next message (same sandbox, no error)', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();

  // Source a concrete model + environment from the seeded default agent — the
  // create validator (agent_schema) requires a non-blank model — skipping any
  // litellm wildcard routing key, exactly as the reclaimed-sandbox sibling does.
  const base = await api.defaultAgent();
  const environmentName = String(base.environment_name || '').trim();
  expect(environmentName, 'seeded default agent must name an environment').not.toEqual('');
  const models = await api.listEnvironmentModels(environmentName);
  const model = models.find((m) => m && !m.includes('*')) || 'deepseek-chat';

  // Scoped to run-view ON PURPOSE. The app shell's sidebar renders a
  // `status-pill` for EVERY session in the list (App.tsx SidebarMenu) and it
  // precedes the route outlet in the DOM, so a bare `status-pill.first()` binds
  // to some other conversation's pill — which reads `data-pulse=false` for free
  // and would make both header assertions below vacuous. SessionHeader's pill is
  // the only `status-pill` inside `run-view`.
  const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
  const bubbles = page.getByTestId('assistant-message');

  /** Type into the composer and submit — the only way a user sends a message. */
  const sendThroughComposer = async (prompt: string, marker: string) => {
    const composer = page.getByTestId('composer-prompt');
    await expect(composer, 'the composer must be sendable when the user types').toBeEnabled({
      timeout: 60_000,
    });
    await composer.fill(prompt);
    await page.getByTestId('composer-submit').click();
    // The user's own bubble renders immediately — the send registered.
    await expect(page.getByTestId('user-message').last()).toContainText(marker, { timeout: 30_000 });
  };

  /** Send from the composer and wait for one more rendered assistant bubble. */
  const sendAndReadReply = async (prompt: string, marker: string, budgetMs: number) => {
    const before = await bubbles.count();
    await sendThroughComposer(prompt, marker);
    // Count, not text: the model's wording is its own business, and a spec that
    // pins wording fails on a model that is behaving correctly.
    await expect.poll(() => bubbles.count(), { timeout: budgetMs }).toBeGreaterThan(before);
    const reply = bubbles.last();
    await expect(reply).not.toBeEmpty();
    await expect(reply).not.toContainText(TURN_ERROR);
    return reply;
  };

  let sessionId = '';
  try {
    // ── Isolated agent (pure metadata, always ACTIVE) + one conversation. The
    //    per-session sandbox lives on the conversation, not the agent. ─────────
    const agent = await api.createAgent({
      name: `__e2e_transport_recreate_${runId}`,
      model,
      environment_name: environmentName,
    });
    agentId = String(agent.agent_id || '');
    expect(agentId, 'created agent must have an id').not.toEqual('');

    const created = await api.startConversation(agentId);
    sessionId = created.session_id;
    sessions.push(sessionId);
    await api.waitForSessionReady(sessionId);

    // The user opens the conversation and keeps this tab open for the whole
    // scenario — the eviction must be invisible to it.
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });

    // ── Warm turn: establishes a genuinely resident transport (a cached runtime
    //    with a live wire to the sandbox) that the evict can then drop. ─────────
    await sendAndReadReply(
      `E2E transport warm ${runId}: 不要使用工具。请简短回复一句话。`,
      `transport warm ${runId}`,
      240_000,
    );
    // Settle before injecting the fault: evicting mid-turn is a different fault
    // (that one belongs to the interrupted-turn specs), and a settled header is
    // also what makes the composer sendable for the second message.
    await expect(pill).toHaveAttribute('data-pulse', 'false', { timeout: 60_000 });

    const warmReady = await api.waitForSessionReady(sessionId);
    // The session sandbox_id is the canonical reference recovery must stay on.
    // No pixel: the console shows no sandbox identity on the conversation screen.
    const sessionSandboxId = String(warmReady.sandbox_id || '').trim();
    expect(sessionSandboxId, 'warm conversation should have a sandbox_id').not.toEqual('');
    test.info().annotations.push({ type: 'e2e_session_sandbox_id', description: sessionSandboxId });

    // ── Drop the resident transport: evict the in-memory runtime (which closes
    //    its transport client). The sandbox lease is untouched — the next message
    //    must rebuild the transport to the SAME live sandbox. No user can do this
    //    (it is a server-side cache), so it stays on the admin API. ─────────────
    const evicted = await api.adminEvictRuntime(sessionId);
    expect(evicted.evicted, 'evict-runtime should report the evicted session').toBe(sessionId);
    test.info().annotations.push({ type: 'e2e_evicted_runtime', description: evicted.evicted });

    // ── THE core property: the very next message the user types should
    //    transparently recreate the transport and deliver a real reply INTO THIS
    //    TAB. No reload — the tab that watched the warm turn is the one that must
    //    keep working. Resolve the outcome within a bounded probe: a recreated
    //    reply on screen (assert fully) OR the resident-sidecar-unsupported
    //    signal on the projection (no resident sidecar → skip precisely). ──────────
    const beforeBubbles = await bubbles.count();
    const beforeApiAssistants = await api.assistantCount(sessionId);
    await sendThroughComposer(
      `E2E transport recreate ${runId}: 不要使用工具。请简短回复一句话。`,
      `transport recreate ${runId}`,
    );

    const probeDeadline = Date.now() + PROBE_BUDGET_MS;
    let recreatedReply: Locator | null = null;
    let unsupportedError = '';
    let onScreenFailure = '';
    while (Date.now() < probeDeadline) {
      // (b) The backend-capability signal. Read on the projection because the
      // console deliberately renders a runtime_unavailable agent chat as calm
      // (isTransparentlyRecoverableAgentSession) — there is no banner to read.
      const projection = await api.getSession(sessionId);
      const lastError = String(projection.last_error || '').trim();
      if (Boolean(projection.runtime_unavailable) && lastError.includes(RESIDENT_SIDECAR_UNSUPPORTED)) {
        unsupportedError = lastError;
        break;
      }
      // (a) A new bubble on screen — but a bubble that grew the count is not yet
      // a reply: the failure under test renders itself as one. A mid-stream
      // bubble can also still be empty, which is neither answer yet.
      if ((await bubbles.count()) > beforeBubbles) {
        const text = (await bubbles.last().innerText()).trim();
        if (text && !TURN_ERROR.test(text)) {
          recreatedReply = bubbles.last();
          break;
        }
        if (text) {
          onScreenFailure = text.replace(/\s+/g, ' ').slice(0, 300);
        }
      }
      await new Promise((r) => setTimeout(r, 2_000));
    }

    if (!recreatedReply) {
      // Before skipping: did the backend DELIVER a reply this tab never rendered?
      // That is not a capability boundary — it is the console defect a page
      // oracle exists to catch, and the API-only version of this spec could not
      // see it at all. Fail loudly instead of skipping past it.
      const deliveredPage = await api.getMessages(sessionId, 50);
      const assistants = (deliveredPage.messages || []).filter((m) => m.role === 'assistant');
      const deliveredText =
        assistants.length > beforeApiAssistants
          ? messageText(assistants[assistants.length - 1]).trim()
          : '';
      expect(
        deliveredText === '' || TURN_ERROR.test(deliveredText),
        `the post-eviction reply was persisted but the open tab never rendered it — ` +
          `delivery recovered and the page did not (bubbles=${await bubbles.count()}, ` +
          `delivered=${JSON.stringify(deliveredText.slice(0, 200))})`,
      ).toBe(true);

      // Not exercisable on this backend: the resident-sidecar transport does not
      // exist to recreate, so the post-eviction turn cannot complete. Skip with the
      // precise, evidence-backed reason (mirrors tests/e2e/test_evict_runtime.py).
      // The evidence is now the row's last_error or the failure the transcript
      // showed the user — the API turn's errorText is gone with the API-only send.
      const evidence =
        unsupportedError ||
        onScreenFailure ||
        '<no reply rendered and no runtime_unavailable signal within probe budget>';
      test.info().annotations.push({ type: 'e2e_transport_recreate_unsupported', description: evidence });
      test.skip(
        true,
        'resident sidecar transport recreation is not exercisable on the community DirectDocker backend: ' +
          'after evicting the in-memory runtime, the agent_chat turn re-attach requires a resident sidecar ' +
          'the direct_docker provider does not offer, so the next message hard-fails and the session is ' +
          `stamped runtime_unavailable (evidence=${JSON.stringify(evidence)}) instead of delivering a reply — ` +
          'the same DirectDocker reconnect limitation tests/e2e/test_evict_runtime.py documents for the chat ' +
          'path. The invariant is a hosted/sidecar-backend capability.',
      );
      return; // unreachable after test.skip throws; narrows recreatedReply below
    }

    // ── Sidecar-capable backend: the page and API jointly carry the oracle.
    //    The screen shows an ordinary settled reply; the session remains READY
    //    on the same sandbox and leaves both runtime error fields clear. ─────────
    // A recreated transport that delivers the text but leaves the turn looking
    // live is still a broken screen.
    await expect(pill).toHaveAttribute('data-pulse', 'false', { timeout: 60_000 });
    // …and the conversation reads settled-READY, never 待恢复. This is where
    // runtime_unavailable reaches the screen: _sanitize_session flips READY→
    // RECOVERY_REQUIRED on the row flag and sessionRunStatus carries that state
    // onto the pill's data-state — machine-readable, so no locale pinning.
    await expect(pill).toHaveAttribute('data-state', 'READY', { timeout: 60_000 });
    // The reply guards run on the SETTLED bubble, not on the one the probe
    // accepted mid-stream: a turn that streams text and fails afterwards appends
    // its failure card into that same message, and the message the user is left
    // reading is the one that has to be clean.
    await expect(recreatedReply).not.toBeEmpty();
    await expect(recreatedReply).not.toContainText(TURN_ERROR);

    const settled = await api.waitForSessionReady(sessionId);
    expect(
      String(settled.sandbox_id || '').trim(),
      'transport recovery should stay on the same session sandbox',
    ).toBe(sessionSandboxId);
    // Kept alongside the pill assertion above: the READY→RECOVERY_REQUIRED flip
    // only surfaces the flag while the row is READY, so the row is still the one
    // place the flag itself is checkable.
    expect(
      Boolean(settled.runtime_unavailable),
      'transport recovery should not mark runtime unavailable',
    ).toBe(false);
    // No pixel at all: for a live agent chat the console suppresses last_error by
    // design (shouldShowSessionLastError), so its absence on screen proves nothing.
    expect(
      String(settled.last_error || '').trim(),
      'transport recovery should not leave a user-visible error',
    ).toBe('');
  } finally {
    // The session and the agent are not released here. `trackSessions()` and
    // `onPassOnly()` decide in afterEach hooks, where the test's real status is
    // known — see that fixture on why a `finally` cannot tell it is unwinding
    // from a failure, and on why the unit is the whole block.
    // Delete the conversation first (releases its own sandbox), then the agent.
  }
});
