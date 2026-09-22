/**
 * E2E: a console nobody touched stops claiming the Assistant workspace is Ready.
 *
 * Both sibling workspace specs drive recovery with a user action — one clicks
 * the card, the other opens the conversation — and
 * `assistant-workspace-oob-death-recovers.exclusive.spec.ts:90` even calls what
 * it clicks "the stale READY card". Neither asserts that the card ever stops
 * being stale. This spec removes the action. The box goes, the durable lease
 * lapses, the platform's own background sweep converges the workspace row with
 * nobody looking (`expiration_watcher._sweep_dead_bindings` →
 * `converge_dead_sandbox_owners` → workspace RECOVERY_REQUIRED), and the page
 * the user walked away from must not still be offering Ready when they come
 * back to it.
 *
 * The load-bearing pair is the card attribute: it must stop reading READY, and
 * it must then EQUAL the `workspace_state` the server publishes. Equality is
 * what keeps the check fix-agnostic — a poll, a push, or a refetch all satisfy
 * it — and what keeps it removal-proof, because deleting the state pill leaves
 * the attribute absent, which fails the equality instead of quietly satisfying
 * a negative.
 *
 * HOW THE RETURN IS DELIVERED. The card's page re-reads on the two events a
 * browser gives a returning reader: `window` `focus` and `document`
 * `visibilitychange` (`frontend/src/hooks/useKeepCurrent.ts:56-70`, wired at
 * `frontend/src/assistant/AssistantsPage.tsx:80-82`). Both are ordinary DOM
 * events, and the return is dispatched as such. `page.bringToFront()` is not
 * it: one tab is already the frontmost tab, Chromium emits neither event for
 * activating it, and a wait that ended in a timeout would then be describing
 * the harness rather than the console.
 *
 * The re-read is throttled to `RETURN_THROTTLE_MS` from the page's last read,
 * so the elapsed gap is asserted before the return is dispatched. A return
 * inside that window is dropped by the hook, and the timeout would again be the
 * harness talking.
 *
 * Ordering: the lease is lapsed only after the box is confirmed stopped. Lapsed
 * first, a watcher tick can probe a still-live box, take the realignment path
 * (`expiration_watcher.py:233-246`) and write the provider's real multi-hour
 * expiry back over the backdate — the wait would then time out and blame the
 * sweep for a setup that undid itself.
 *
 * Deployment fixtures this spec reads and never creates: an enabled Environment
 * running the `assistant` engine (`api.assistantEnvironmentName()` fails loudly
 * without one), `ASTRABOX_E2E_ASSISTANT_MODEL`, and the e2e deployment's
 * `ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS=5`, without which the real
 * sweep cannot tick inside a 180-second budget.
 *
 * This journey provisions a workspace and a conversation on it. It must stay in
 * the suite contract's one-worker serial group, as both sibling Assistant specs
 * are, so unrelated cold workspaces cannot consume its fixed 180-second budget.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { lapseAssistantWorkspaceLease, snapshotDoc } from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import {
  killSandbox,
  requireSandboxHandle,
  sandboxRunning,
  waitForSandboxStopped,
} from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const KILL_CONVERGE_MS = parseTimeoutEnv('ASTRABOX_E2E_KILL_CONVERGE_MS', 30_000);
const ASSISTANT_TURN_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_ASSISTANT_TURN_TIMEOUT_MS',
  120_000,
);
// How long the platform may take to notice on its own, at the deployment's
// watcher interval. This is the budget for the server side of the journey; the
// page's own catch-up is measured separately below.
const CONVERGE_MS = parseTimeoutEnv('ASTRABOX_E2E_ASSISTANT_CONVERGE_MS', 60_000);
const STALE_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_CONSOLE_STALE_BUDGET_MS', 30_000);
// The window the console drops a return in, exported as `RETURN_THROTTLE_MS`
// from `frontend/src/hooks/useKeepCurrent.ts:8`. Mirrored rather than imported:
// this suite compiles against its own tsconfig and does not reach into the app
// bundle. A return dispatched inside this window is discarded, so the spec
// proves the gap before spending its budget on the wait.
const RETURN_THROTTLE_MS = 5_000;

// The one localized string this spec reads is the card's action ("Start
// conversation"). The console detects language as ['localStorage','navigator'],
// so an unpinned runner locale decides which spelling it gets; pin both halves
// the way the sibling lifecycle specs do and assert the English name exactly.
test.use({ locale: 'en-US' });

const sessions = trackSessions();
let assistantId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

test('the Assistant card on a page left open stops reading READY once the platform has released its box', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  const environmentName = await api.assistantEnvironmentName();
  expect(environmentName, 'an assistant environment must exist').not.toEqual('');
  const assistantModel = await api.assistantModelName(environmentName);

  const assistant = await api.createAssistant({
    display_name: `__e2e_assistant_stale_card_${runId}`,
    environment_name: environmentName,
    model_config_override: { model_name: assistantModel },
  });
  assistantId = String(assistant.assistant_id || '');
  expect(assistantId, 'created assistant must have an id').not.toEqual('');

  const initialWorkspace = await api.waitForWorkspaceReady(assistantId);
  const deadSandboxId = String(initialWorkspace.current_sandbox_id || '').trim();
  expect(deadSandboxId, 'a READY workspace must name its sandbox').not.toEqual('');
  test.info().annotations.push({
    type: 'e2e_assistant_dead_sandbox_id',
    description: deadSandboxId,
  });

  // Pin the console language before the FIRST navigation, or the action name
  // read at the end is whatever the runner's locale happens to be.
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });

  // ── Use the Assistant the way a user does, so the card's READY is earned. ──
  // Arranging the conversation stays on the API; the message itself goes
  // through the real composer. A box that answered once cannot be blamed later
  // for a workspace that never worked.
  const created = await api.startAssistantConversation(assistantId);
  const sessionId = String(created.session_id || '').trim();
  expect(sessionId, 'starting a conversation must open a session').not.toEqual('');
  sessions.push(sessionId);
  const initialSession = await api.waitForSessionReady(sessionId);
  expect(String(initialSession.sandbox_id || '')).toEqual(deadSandboxId);

  await openSessionView(page, sessionId);
  const marker = `WARM-${runId}`;
  const repliesBefore = await page.getByTestId('assistant-message').count();
  await sendPrompt(page, sessionId, `请简短回复一句话，不要使用工具。标记 ${marker}`);
  await expect(page.getByTestId('user-message').last()).toContainText(marker, {
    timeout: 30_000,
  });
  // Count, not wording: the model's phrasing is its own business.
  await expect
    .poll(() => page.getByTestId('assistant-message').count(), { timeout: ASSISTANT_TURN_MS })
    .toBeGreaterThan(repliesBefore);
  const reply = page.getByTestId('assistant-message').last();
  await expect(reply).not.toBeEmpty();
  // A failed turn renders its error INTO the transcript as an assistant
  // message, so "one more non-empty bubble" is satisfied by exactly the outcome
  // a broken workspace would produce.
  await expect(reply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);

  // Walk away only from a settled turn. A turn still in flight when its box
  // dies hands the platform a second, unrelated reason to act on this
  // workspace: recovering that turn can republish a box and carry the card back
  // to READY for a reason this journey is not about.
  await expect
    .poll(() => String(snapshotDoc(sessionId)?.conversation_state || '<no snapshot>'), {
      timeout: 45_000,
      message: `the turn must settle before the box is taken away (session ${sessionId})`,
    })
    .toBe('IDLE');

  // ── The user leaves the console here, and never touches it again. ─────────
  await page.goto(appPath('/assistants'));
  const assistantCard = page.locator(
    `[data-testid="assistant-option"][data-assistant-id="${assistantId}"]`,
  );
  await expect(assistantCard, 'the new Assistant must be offered on the picker').toBeVisible({
    timeout: 30_000,
  });
  await expect(
    assistantCard,
    'the card the user walks away from reads READY',
  ).toHaveAttribute('data-assistant-state', 'READY');
  // The page's read is already settled here, so this is at or after the mark
  // the re-read throttles against — which makes the gap asserted at the return
  // a lower bound rather than an estimate.
  const pageReadAt = Date.now();

  // ── The afternoon, part 1: the box goes. ─────────────────────────────────
  const deadHandle = await requireSandboxHandle(api, deadSandboxId);
  expect(sandboxRunning(deadHandle), 'workspace sandbox must be running at fault time').toBe(true);
  killSandbox(deadHandle);
  await waitForSandboxStopped(deadHandle, KILL_CONVERGE_MS);
  expect(sandboxRunning(deadHandle), 'the removed workspace sandbox must be gone').toBe(false);

  // ── The afternoon, part 2: the workspace lease over that box runs out. ───
  // An hour rather than a minute: this clock is the runner host's and the
  // comparison is made against the server container's, so the margin has to
  // survive skew between them. Same margin as the Session-lease precedent in
  // `dispatch-renews-transport-attached-sandbox-lease`.
  const expiredAt = new Date(Date.now() - 3_600_000).toISOString();
  const lapsed = lapseAssistantWorkspaceLease(assistantId, deadSandboxId, expiredAt);
  expect(
    lapsed,
    'the fault must lapse exactly the workspace binding the user just used',
  ).toEqual([
    {
      assistant_id: assistantId,
      current_sandbox_id: deadSandboxId,
      current_sandbox_expires_at: expiredAt,
    },
  ]);

  // ── The platform notices with nobody looking. ────────────────────────────
  // GET, never wake: waking IS the repair, and this half of the journey is
  // about what happens when no one asks for anything.
  await expect
    .poll(
      async () => String((await api.getAssistant(assistantId)).workspace_state || ''),
      {
        timeout: CONVERGE_MS,
        message:
          'the background sweep must release a workspace whose box is gone and whose '
          + 'lease has lapsed, with no user action. A red here is the sweep, not the '
          + 'page: check ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS on the deployment '
          + `and the dead-binding convergence for sandbox ${deadSandboxId}.`,
      },
    )
    .not.toBe('READY');

  // ── The user comes back to the tab they left. ────────────────────────────
  // No reload, no navigation, no click: the return is the only input, and it is
  // delivered as the two DOM events the console listens for.
  expect(
    Date.now() - pageReadAt,
    `the return must land at least ${RETURN_THROTTLE_MS}ms after the page's last read, `
      + 'or the console drops it and this wait measures the throttle instead of the card',
  ).toBeGreaterThanOrEqual(RETURN_THROTTLE_MS);
  const visibilityAtReturn = await page.evaluate(() => {
    if (document.visibilityState !== 'visible') return document.visibilityState;
    window.dispatchEvent(new Event('focus'));
    document.dispatchEvent(new Event('visibilitychange'));
    return 'visible';
  });
  expect(
    visibilityAtReturn,
    'a return is only delivered to a visible document: the re-read returns without '
      + 'reading when `document.visibilityState` is anything else',
  ).toEqual('visible');

  await expect
    .poll(() => assistantCard.getAttribute('data-assistant-state'), {
      timeout: STALE_BUDGET_MS,
      message:
        'a console left open must stop offering a workspace the platform has already '
        + 'released. The card prints the `workspace_state` of the list read that filled '
        + 'it (`AssistantsPage.tsx:134-141`), so a return that leaves this attribute on '
        + 'READY is the console still offering a box the platform has let go.',
    })
    .not.toBe('READY');

  // Nothing can move the workspace now — the box is dead and no one is waking
  // it — so a server read and a card read taken here describe the same moment.
  // Equality, not "anything but READY": a card that stopped at some third
  // state would also pass a negative.
  const publishedState = String((await api.getAssistant(assistantId)).workspace_state || '');
  expect(
    publishedState,
    'the server must publish a state for the equality below to mean anything',
  ).not.toEqual('');
  const cardState = String((await assistantCard.getAttribute('data-assistant-state')) || '');
  expect(
    cardState,
    'the card must read back the workspace state the server publishes',
  ).toEqual(publishedState);

  // A card that tells the truth must not become a dead end. Nothing is clicked,
  // so no replacement box is built and no budget is spent proving it.
  const startButton = assistantCard.getByRole('button', { name: 'Start conversation' });
  await expect(startButton, 'a released workspace still offers its action').toBeVisible();
  await expect(startButton).toBeEnabled();
});
