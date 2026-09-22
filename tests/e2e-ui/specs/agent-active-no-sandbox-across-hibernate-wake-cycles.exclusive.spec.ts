/**
 * E2E: a per-session agent remains ACTIVE and never owns a sandbox_id across
 * repeated hibernate/wake calls. The sandbox belongs to each conversation.
 *
 * The agent card supplies the user-visible state oracle after every lifecycle
 * call. The lifecycle endpoints and sandbox identifiers stay API assertions
 * because the console exposes neither. Starting a conversation from the card,
 * reaching READY, and rendering one reply proves that the no-op calls leave the
 * agent usable while the new session receives its own runtime.
 *
 * This state-mutating spec creates and removes an agent and a session, so it is
 * exclusive and must run with one worker.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, type AgentRecord } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';

// Agent create + repeated no-op cycles + one eager sandbox provision + one short
// turn sit above the 240s suite default; keep the lifecycle-sized, env-tunable
// budget in line with the sibling per-session specs.
const HIBERNATE_WAKE_CYCLES = 2;
// The conversation started from the card provisions its own sandbox before the
// header can read READY. Keep the budget tunable for cold or contended runtimes.
const PAGE_READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_AGENT_LIFECYCLE_READY_TIMEOUT_MS', 180_000);
// One short, tool-free reply rendered into the transcript.
const REPLY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_AGENT_LIFECYCLE_REPLY_TIMEOUT_MS', 240_000);

// The card's state badge is the console's only rendering of an agent's state, and
// it is a LOCALIZED label (agents:state_active → "Active" / "运行中"). The console
// detects language as ['localStorage','navigator'] with fallbackLng 'en', so an
// unpinned runner locale decides what the badge says. Pin it to English — a fixed
// navigator locale plus the persisted 'astrabox-lang' written by the language
// switch (localStorage is the highest-priority source).
test.use({ locale: 'en-US' });

/** The no-pixel half of the no-op, read from the poke's own response: the agent
 *  never acquires a sandbox_id. The state is asserted here too — this is THAT
 *  poke's direct evidence — and again on the card, which is where the user sees
 *  it; the sandbox_id has no rendering anywhere in the console. */
function expectActiveNoSandbox(agent: AgentRecord, label: string): void {
  expect(String(agent.state || ''), `${label}: agent must be ACTIVE`).toEqual('ACTIVE');
  expect(
    String((agent.sandbox_id as string | null | undefined) || '').trim(),
    `${label}: agent must never hold a sandbox_id`,
  ).toEqual('');
}

// Teardown that changes state runs on the passing path only, and hook
// registration order is the old `finally` order: afterEach hooks run in the
// order they are registered, so the session goes before the agent it ran on.
let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('per-session agent stays ACTIVE with no sandbox_id across repeated hibernate/wake cycles', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  const agentName = `__e2e_agent_lifecycle_${runId}`;

  // The agent's card on the console home, and the state badge inside it.
  const card = page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`);
  const stateBadge = card.locator('[data-slot="badge"]');

  /**
   * Re-open the console home and read the card. A fresh load is not a test
   * convenience: AgentHome fetches the agent list once on mount, so re-opening
   * the page IS how a user sees the state after an out-of-band lifecycle poke.
   */
  const expectCardOffersActiveAgent = async (label: string) => {
    await page.goto(appPath('/agents'));
    await expect(card, `${label}: the console must still offer the agent`).toBeVisible({ timeout: 30_000 });
    await expect(stateBadge, `${label}: the card must read Active`).toHaveText('Active');
  };

  let sessionId = '';
  try {
    // ── 1. Create agent → immediately ACTIVE, no sandbox. ──────────────────
    const agent = await api.createColdTestAgent(agentName);
    agentId = String(agent.agent_id || '');
    expect(agentId, 'created agent must have an id').not.toEqual('');
    expectActiveNoSandbox(agent, 'on creation');

    // Pin the console language before the first navigation so the badge assertion
    // reads the label it was written against (see the test.use note above).
    await page.addInitScript(() => {
      try {
        window.localStorage.setItem('astrabox-lang', 'en');
      } catch {
        /* localStorage unavailable — the en-US navigator locale still applies */
      }
    });
    // A freshly created agent is offered to the user straight away, reading Active.
    await expectCardOffersActiveAgent('on creation');

    // ── 2. hibernate → no-op: stays ACTIVE, no sandbox. ────────────────────
    // hibernateAgent/wakeAgent are the shared fixture helpers over the public
    // POST routes (both return the sanitized agent doc under the {data} envelope).
    // They stay on the API because no console control hibernates an AGENT — that
    // is an operator action, like the sibling specs' out-of-band sandbox kill.
    const afterHibernate = await api.hibernateAgent(agentId);
    expectActiveNoSandbox(afterHibernate, 'after hibernate');
    await expectCardOffersActiveAgent('after hibernate');

    // ── 3. wake → no-op: stays ACTIVE, no sandbox. ─────────────────────────
    const afterWake = await api.wakeAgent(agentId);
    expectActiveNoSandbox(afterWake, 'after wake');
    await expectCardOffersActiveAgent('after wake');

    // ── 4. Multiple hibernate↔wake cycles all return ACTIVE with no sandbox.
    // Each poke's own response is checked; the card is read once at the end of
    // the cycles — the property is that the REPEATED pokes leave the agent as it
    // was, so the state after the last one is what the user's screen must show.
    for (let i = 0; i < HIBERNATE_WAKE_CYCLES; i += 1) {
      const cycleHibernate = await api.hibernateAgent(agentId);
      expectActiveNoSandbox(cycleHibernate, `hibernate cycle ${i + 1}`);
      const cycleWake = await api.wakeAgent(agentId);
      expectActiveNoSandbox(cycleWake, `wake cycle ${i + 1}`);
    }
    await expectCardOffersActiveAgent('after all hibernate/wake cycles');

    // A GET re-read still shows the pure-metadata shape after all the pokes — the
    // sandbox_id half, which no screen renders.
    const agentReread = await api.getAgent(agentId);
    expectActiveNoSandbox(agentReread, 'after all hibernate/wake cycles (GET)');

    // ── 5. Start a conversation FROM THE CARD; the sandbox is the SESSION's. ──
    // The card's only button is "Start conversation": AgentHome creates the
    // conversation and the shell navigates to /sessions/{id}, so the session id
    // is read back off the URL the click produced.
    await card.getByRole('button').click();
    await page.waitForURL(/\/sessions\/[^/?#]+/, { timeout: 60_000 });
    sessionId = (page.url().split('/sessions/')[1] || '').split(/[?#]/)[0];
    if (sessionId) sessions.push(sessionId);
    expect(sessionId, 'starting a conversation from the card must open a session').not.toEqual('');

    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
    // Scoped to the run view: the sidebar renders a status pill per conversation
    // row, and this assertion is about the header of the one just opened.
    const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
    // The conversation acquires its OWN runtime and says so: headerRunState is the
    // session's state verbatim, and the backend does not project READY without a
    // sandbox. CREATING → READY is the user-visible provisioning of that sandbox.
    await expect(
      pill,
      'the conversation the user started must reach READY on its own runtime',
    ).toHaveAttribute('data-state', 'READY', { timeout: PAGE_READY_TIMEOUT_MS });

    // …and it genuinely serves. After four agent-level lifecycle pokes, "the
    // sandbox belongs to the session" means the session's box answers.
    const assistantsBefore = await page.getByTestId('assistant-message').count();
    const prompt = '请简短回复一句话，不要使用工具。';
    await page.getByTestId('composer-prompt').fill(prompt);
    await page.getByTestId('composer-submit').click();
    const queuedPrompt = page.getByTestId('composer-queue').filter({ hasText: prompt });
    const userMessage = page.getByTestId('user-message').filter({ hasText: prompt }).last();
    await expect(
      queuedPrompt.or(userMessage).first(),
      'the accepted input remains visible in the queue or transcript',
    ).toBeVisible({ timeout: 30_000 });
    // Count, not text: the model's wording is its own business, and a spec that
    // pins wording fails on a model that is behaving correctly.
    await expect
      .poll(() => page.getByTestId('assistant-message').count(), { timeout: REPLY_TIMEOUT_MS })
      .toBeGreaterThan(assistantsBefore);
    const reply = page.getByTestId('assistant-message').last();
    await expect(reply).not.toBeEmpty();
    // A bubble that grew the count is not yet a reply: a FAILED turn renders its
    // error INTO the transcript as an assistant message, so "one more non-empty
    // bubble" would be satisfied by exactly the outcome this spec must rule out.
    await expect(reply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
    await expect(
      userMessage,
      'engine consumption hands the queued input to the transcript',
    ).toContainText(prompt.slice(0, 8), { timeout: 30_000 });
    await expect(
      queuedPrompt,
      'the queue stops owning the input after the transcript receives it',
    ).toHaveCount(0, { timeout: 30_000 });
    // And the header settles — a turn that delivers text but leaves the screen
    // looking live is still broken.
    await expect(pill).toHaveAttribute('data-pulse', 'false', { timeout: 60_000 });

    // The sandbox ids have NO PIXEL — the console shows one nowhere, on the card
    // or in the run view — so the ownership half of the titular invariant stays on
    // the API: the SESSION holds a sandbox, the agent record still holds none.
    const readySession = await api.waitForSessionReady(sessionId);
    const sessionSandboxId = String((readySession.sandbox_id as string | null | undefined) || '').trim();
    expect(sessionSandboxId, 'the session must own a sandbox_id in the per-session model').not.toEqual('');
    test.info().annotations.push({ type: 'e2e_session_sandbox_id', description: sessionSandboxId });

    const agentAfterSession = await api.getAgent(agentId);
    expectActiveNoSandbox(agentAfterSession, 'after a session provisions its own sandbox');

    // The user-visible half of the same fact: back on the home page the agent is
    // still simply Active — starting a conversation took a box for the SESSION and
    // changed nothing about the agent the user picks from.
    await expectCardOffersActiveAgent('after a session provisions its own sandbox');
  } finally {
    // Delete the conversation first (releases its own sandbox), then the agent.
    // Nothing here is released on a failing run. `trackSessions()` and
    // `onPassOnly()` decide in afterEach hooks, where the test's real status is
    // known — see that fixture on why the unit is the whole block.
  }
});
