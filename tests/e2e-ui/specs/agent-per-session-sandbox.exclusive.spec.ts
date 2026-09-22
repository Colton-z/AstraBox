/**
 * E2E: each conversation owns a distinct sandbox while its Agent remains pure
 * metadata.
 *
 * Two conversations start from the same Agent card and send unique markers. Each
 * must render only its own history and retain it after sidebar navigation. Their
 * sandbox ids must be non-empty and different, while the Agent stays ACTIVE with
 * no sandbox before and after no-op hibernate and wake calls.
 *
 * Exact sandbox ownership stays on the API because the conversation view does not
 * render those identifiers.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';

// Two sandbox provisions + two turns + the lifecycle no-ops sit well above the
// 240s suite default; keep the lifecycle budget configurable.
// One reply, read off the page — a first turn also pays for the sandbox coming
// up, so this is the same order as the reference spec's plain turn.
const TURN_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_PER_SESSION_TURN_TIMEOUT_MS', 240_000);

// The one localized surface this spec reads is the card's state badge
// (agents:state_active → "Active" / "运行中"). The console detects language as
// ['localStorage','navigator'] with fallbackLng 'en', so an unpinned runner locale
// is what decides which of the two it says — and a spec that accepts both spellings
// is a spec that would also accept a third locale nobody wrote it against. Pin it to
// English the way the sibling lifecycle spec does (fixed navigator locale plus the
// persisted 'astrabox-lang' the app's own switch writes, which outranks navigator),
// and then assert the badge exactly.
test.use({ locale: 'en-US' });

// Teardown that changes state runs on the passing path only, and hook
// registration order is the old `finally` order: afterEach hooks run in the
// order they are registered, so the session goes before the agent it ran on.
//
// `sessionIds` IS the tracker's array, so the existing pushes are unchanged.
const sessionIds = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('per-session: each conversation gets its own sandbox and the agent holds none', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  const agentName = `__e2e_per_session_${runId}`;
  // One marker per conversation, carried in the user's own message. On the page
  // these ARE the identity of a conversation's history: S1 must never show up in
  // the second transcript, nor S2 in the first.
  const marker1 = `S1-${runId}`;
  const marker2 = `S2-${runId}`;

  try {
    // ── Create an isolated agent — pure metadata, no sandbox. ──────────────
    // Creating the agent stays on the API: it is arrangement, and a user picks
    // an agent that already exists rather than authoring one to have a chat.
    const agent = await api.createColdTestAgent(agentName);
    agentId = String(agent.agent_id || '');
    expect(agentId, 'created agent must have an id').not.toEqual('');
    expect(String(agent.state || ''), 'created agent is ACTIVE immediately').toEqual('ACTIVE');
    expect(
      String(agent.sandbox_id || '').trim(),
      'per-session agent must not hold a sandbox_id',
    ).toEqual('');

    // Pin the console language before the FIRST navigation, or the badge below
    // reads whatever the runner's locale happens to be (see test.use above).
    await page.addInitScript(() => {
      try {
        window.localStorage.setItem('astrabox-lang', 'en');
      } catch {
        /* localStorage unavailable — the en-US navigator locale still applies */
      }
    });

    // The user's entry point: the agent picker.
    await page.goto(appPath('/agents'));
    const card = page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`);
    await expect(card, 'the new agent must be offered on the picker').toBeVisible({ timeout: 30_000 });
    // The badge IS the state a user reads, so the assertion binds to the badge
    // rather than to the card's whole text — a card-wide match would also be
    // satisfied by the word turning up in a description or a model id. With the
    // language pinned, the expected string is exact; any non-ACTIVE state must
    // fail this assertion.
    const stateBadge = card.locator('[data-slot="badge"]');
    await expect(stateBadge, 'a fresh per-session agent reads Active on the picker').toHaveText('Active');

    /** Click "start conversation" on the card; returns the conversation it opened. */
    const startConversationFromCard = async (): Promise<string> => {
      // One button per card — the Badge, title and model line are all spans.
      await card.getByRole('button').click();
      // The console navigates to the conversation it just created, and that
      // route IS the user-visible identity of the conversation.
      await page.waitForURL((url) => /\/sessions\/[^/]+$/.test(url.pathname), { timeout: 120_000 });
      const sessionId = new URL(page.url()).pathname.split('/').filter(Boolean).pop() || '';
      expect(sessionId, 'starting a conversation must open its own /sessions/<id> route').not.toEqual('');
      sessionIds.push(sessionId);
      await expect(page.getByTestId('run-view')).toBeVisible();
      return sessionId;
    };

    /** Send from the composer and wait for one more rendered assistant bubble. */
    const sendAndReadReply = async (prompt: string, marker: string, budgetMs: number) => {
      const before = await page.getByTestId('assistant-message').count();
      await page.getByTestId('composer-prompt').fill(prompt);
      await page.getByTestId('composer-submit').click();
      await expect(page.getByTestId('user-message').last()).toContainText(marker, { timeout: 30_000 });
      // Count, not text: the model's wording is its own business, and a spec that
      // pins wording fails on a model that is behaving correctly.
      await expect
        .poll(() => page.getByTestId('assistant-message').count(), { timeout: budgetMs })
        .toBeGreaterThan(before);
      const reply = page.getByTestId('assistant-message').last();
      await expect(reply).not.toBeEmpty();
      // A bubble that grew the count is not yet a reply: a failed turn renders
      // its error INTO the transcript as an assistant message, so "one more
      // non-empty bubble" is satisfied by exactly the outcome a broken
      // provisioning path would produce.
      await expect(reply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
      return reply;
    };

    // ── Conversation 1: started from the card, answered in the transcript. ─
    const session1 = await startConversationFromCard();
    // The composer is disabled until the conversation has its box, so waiting
    // for READY is a precondition of typing, not an oracle of its own.
    await api.waitForSessionReady(session1);
    await sendAndReadReply(`请用一句话向我问好，直接回复文字即可，无需调用工具或提问。我的会话标签是 ${marker1}。`, marker1, TURN_BUDGET_MS);

    // Capture session1's sandbox after the first turn settles. API, not page:
    // the id has no pixel on this screen (see the header note).
    const firstReady = await api.waitForSessionReady(session1);
    const firstSandboxId = String(firstReady.sandbox_id || '').trim();
    expect(firstSandboxId, 'session1 should have a sandbox_id after its first turn').not.toEqual('');
    test.info().annotations.push({ type: 'e2e_session1_sandbox_id', description: firstSandboxId });

    // The agent still holds no sandbox even after a conversation started.
    const agentAfterFirst = await api.getAgent(agentId);
    expect(
      String(agentAfterFirst.sandbox_id || '').trim(),
      'agent must not hold a sandbox_id after a session starts',
    ).toEqual('');

    // ── wake/hibernate are no-ops: agent stays ACTIVE, never gets a sandbox.
    // The console has no control for either on an agent (only assistants do),
    // so the API both induces the cycle and reads its result; the user-visible
    // half of the claim is asserted on the picker right below.
    const afterHibernate = await api.hibernateAgent(agentId);
    expect(String(afterHibernate.state || ''), 'per-session hibernate is a no-op → ACTIVE').toEqual('ACTIVE');
    expect(
      String(afterHibernate.sandbox_id || '').trim(),
      'agent never holds a sandbox_id (hibernate)',
    ).toEqual('');
    const afterWake = await api.wakeAgent(agentId);
    expect(String(afterWake.state || ''), 'per-session wake is a no-op → ACTIVE').toEqual('ACTIVE');
    expect(
      String(afterWake.sandbox_id || '').trim(),
      'agent never holds a sandbox_id (wake)',
    ).toEqual('');

    // ── Back on the picker: the cycle must be invisible to the user. ───────
    await page.goto(appPath('/agents'));
    await expect(card, 'the agent survives a hibernate/wake cycle on the picker').toBeVisible({
      timeout: 30_000,
    });
    await expect(
      stateBadge,
      'a per-session agent still reads Active after hibernate/wake',
    ).toHaveText('Active');

    // ── Conversation 2: same agent, its own conversation. ──────────────────
    const session2 = await startConversationFromCard();
    expect(session2, 'the second start must open a DIFFERENT conversation').not.toEqual(session1);
    await api.waitForSessionReady(session2);
    await sendAndReadReply(`请用一句话向我问好，直接回复文字即可，无需调用工具或提问。我的会话标签是 ${marker2}。`, marker2, TURN_BUDGET_MS);

    // And the header settles — a second conversation that delivers its text but
    // leaves the turn looking live is still a broken screen. Scoped to the
    // run-view because the sidebar renders one status-pill PER conversation row,
    // and an unscoped .first() would read some other conversation's pill.
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute(
      'data-pulse',
      'false',
      { timeout: 60_000 },
    );

    const secondReady = await api.waitForSessionReady(session2);
    const secondSandboxId = String(secondReady.sandbox_id || '').trim();
    expect(secondSandboxId, 'session2 should have its own sandbox_id').not.toEqual('');
    test.info().annotations.push({ type: 'e2e_session2_sandbox_id', description: secondSandboxId });

    // THE titular property: each conversation owns its OWN sandbox. The
    // sandbox_id IS the backend's container/Pod id (unique per box), and both
    // conversations are concurrently live, so the ids must differ. It stays an
    // API assertion for want of a `session-sandbox-id` testid — the id renders
    // only in the operator table, on a cell with no test hook.
    expect(secondSandboxId, 'each conversation must get its own distinct sandbox').not.toEqual(firstSandboxId);

    // The agent still holds no sandbox after multiple conversations.
    const agentAfterBoth = await api.getAgent(agentId);
    expect(
      String(agentAfterBoth.sandbox_id || '').trim(),
      'agent must not hold a sandbox_id after multiple sessions',
    ).toEqual('');

    // ── The transcripts are the user's face of the isolation. ──────────────
    // Scoped to the user's own bubbles on purpose: the assistant is free to
    // quote back whatever it likes, but a message the user typed in one
    // conversation appearing in another is a real leak.
    const userBubbles = page.getByTestId('user-message');
    await expect(userBubbles.filter({ hasText: marker2 }), 'conversation 2 shows its own message').toHaveCount(1);
    await expect(
      userBubbles.filter({ hasText: marker1 }),
      "conversation 1's message must not bleed into conversation 2",
    ).toHaveCount(0);

    // ── Durable history, read where the user reads it. ─────────────────────
    // Back to the first conversation the way a user goes back: its row in the
    // sessions sidebar. This re-fetches the transcript from the server, so the
    // message AND the reply being on screen is the durable-history property —
    // and conversation 2's message still is not here.
    const row1 = page.locator(`[data-testid="session-row"][data-session-id="${session1}"]`);
    await expect(row1, 'the first conversation must still be listed').toBeVisible({ timeout: 30_000 });
    await row1.getByRole('link').click();
    await page.waitForURL((url) => url.pathname.endsWith(`/sessions/${session1}`), { timeout: 60_000 });
    await expect(page.getByTestId('run-view')).toBeVisible();
    await expect(userBubbles.filter({ hasText: marker1 }), 'session1 keeps its own message').toHaveCount(1);
    await expect(
      userBubbles.filter({ hasText: marker2 }),
      "conversation 2's message must not bleed into conversation 1",
    ).toHaveCount(0);
    // This carries the API oracle it replaced (assistant message count > 0) at
    // full strength, and deliberately: `.last()` of an EMPTY locator resolves to
    // no element, and Playwright fails a negated matcher on a missing element
    // (only to.be.visible / to.be.attached and friends are special-cased to pass),
    // so a conversation that came back with an empty transcript fails here rather
    // than passing vacuously.
    const reply1 = page.getByTestId('assistant-message').last();
    await expect(reply1, 'session1 should have durable assistant history').not.toBeEmpty();
    await expect(reply1).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);

    // And the same in the other direction — a transcript that survives only the
    // way it was last rendered is not history.
    const row2 = page.locator(`[data-testid="session-row"][data-session-id="${session2}"]`);
    await expect(row2, 'the second conversation must still be listed').toBeVisible({ timeout: 30_000 });
    await row2.getByRole('link').click();
    await page.waitForURL((url) => url.pathname.endsWith(`/sessions/${session2}`), { timeout: 60_000 });
    await expect(page.getByTestId('run-view')).toBeVisible();
    await expect(userBubbles.filter({ hasText: marker2 }), 'session2 keeps its own message').toHaveCount(1);
    // Asserted AFTER the line above on purpose: "marker1 is absent" is worth
    // nothing while the transcript is still empty mid-navigation, and is worth
    // everything once session2's own message has rendered. It is also what makes
    // the reply read below provably session2's rather than a bubble left over
    // from the conversation the page just came from.
    await expect(
      userBubbles.filter({ hasText: marker1 }),
      "conversation 1's message must not bleed into conversation 2 on reload",
    ).toHaveCount(0);
    const reply2 = page.getByTestId('assistant-message').last();
    await expect(reply2, 'session2 should have durable assistant history').not.toBeEmpty();
    await expect(reply2).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
  } finally {
    // Delete each conversation first — that releases its own sandbox — then the
    // agent (delete_agent only tears down the agent runtime, which owns nothing).
    // Nothing here is released on a failing run. `trackSessions()` and
    // `onPassOnly()` decide in afterEach hooks, where the test's real status is
    // known — see that fixture on why the unit is the whole block.
  }
});
