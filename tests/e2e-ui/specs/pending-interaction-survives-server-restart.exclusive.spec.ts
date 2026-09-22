/**
 * E2E: a pending interaction and its turn lock survive a server restart.
 *
 * Before restart, the approval panel and durable snapshot must identify the same
 * waiting interaction. After the shared server container restarts, a full page
 * reload must restore the panel and its options. Choosing an option must clear the
 * interaction, release the busy lock, settle the original turn without a
 * turn.failed event, and return a sendable composer.
 *
 * The test is exclusive because the restart affects every session on the host.
 * A bounded precondition requires the requested tool gate before the restart;
 * a terminal turn without that gate is a failure, not an optional path.
 */
import { test, expect, type Page } from '@playwright/test';

import { AstraApi, type PendingInteraction } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath, absoluteBaseUrl } from '../fixtures/env';
import { sessionDoc, snapshotDoc, sessionEvents } from '../fixtures/dbOracle';
import { restartServerContainer } from '../fixtures/sandboxOps';

const PROBE_TIMEOUT_MS = 150_000;
// The console is served by the container that just restarted: the reloaded page
// re-fetches the bundle and its first API calls land on a freshly-booted
// backend. Generous, so a cold start is not reported as a lost interaction.
const REHYDRATE_RENDER_MS = 90_000;
// The panel hides the moment submit is pressed (the card suppresses the
// interaction id optimistically), so this is short on purpose: a panel still
// standing after it means the click never reached a live card. Note what this
// pixel does NOT prove on its own — a REFUSED answer releases the suppression
// and the panel comes back (pendingInteractionState
// .shouldReleaseSuppressedPendingInteraction), which this assertion can race.
// The assertion that a refused answer cannot survive is the composer one at the
// end: the composer only exists while no interaction is pending.
const ANSWER_ACCEPTED_MS = 60_000;
// The answered continuation is a real model turn; budget it like one.
const SETTLE_MS = 300_000;

// The questionnaire card's submit button (misc:composer.submit_answer — en/zh;
// the console is bilingual and the runner's default locale is en-US).
const SUBMIT_ANSWER = /Submit answer|提交回答/;
// The affirmative primary button of a tool-permission / plan-confirmation card
// (misc:composer.allow_continue / apply_suggestion_continue / approve_continue).
const APPROVE_CONTINUE =
  /Allow and continue|Apply suggestion and continue|Approve and continue|允许并继续|应用建议并继续|批准并继续/;
// A FAILED turn renders its error INTO the transcript as an assistant message,
// so "there is a reply bubble" is satisfied by exactly the failure under test.
const TURN_ERROR = /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

/** The questionnaire fields of a pending interaction (PendingQuestionnaireInteraction). */
type QuestionnaireFields = { questions?: Array<{ options?: Array<{ label?: string }> }> };

/** Fill the real composer and confirm dispatch by the user's own bubble. `.fill()`
 * sets the value without key events, so a multi-line prompt is not submitted
 * early by an Enter newline. */
async function sendFromComposer(page: Page, prompt: string, echo: string): Promise<void> {
  const composer = page.getByTestId('composer-prompt');
  await expect(composer, 'the composer must be enabled before sending').toBeEnabled({ timeout: 45_000 });
  await composer.fill(prompt);
  const submit = page.getByTestId('composer-submit');
  await expect(submit).toBeEnabled({ timeout: 15_000 });
  await submit.click();
  await expect(
    page.getByTestId('user-message').filter({ hasText: echo }).last(),
    'the user bubble should render — proof the turn was dispatched from the page',
  ).toBeVisible({ timeout: 30_000 });
}

/**
 * Answer the rendered pending interaction the way a user does: for a question,
 * pick the first option the agent offered (or type into the free-text box when
 * it offered none) and press submit; for a permission/plan gate, press the
 * affirmative button.
 *
 * The option labels come from the interaction the SERVER still reports after
 * the restart, so finding and clicking them on screen proves the panel is
 * rendering THIS interaction's own content — not a blank card and not one
 * without the agent's choices. It is a content check, not an identity check:
 * identity stays on interaction_id below, because no testid renders an id.
 *
 * Branching is on the interaction's SHAPE, not on a failure — a questionnaire
 * that renders no clickable option must go red here, not fall back to
 * something that passes.
 */
async function answerFromPanel(page: Page, pending: PendingInteraction): Promise<void> {
  const panel = page.getByTestId('pending-interaction-panel');
  if (String(pending.presentation ?? '').trim() !== 'form') {
    const approve = panel.getByRole('button', { name: APPROVE_CONTINUE }).last();
    await expect(approve, 'the pending panel should offer the affirmative button').toBeEnabled({ timeout: 30_000 });
    await approve.click();
    return;
  }

  const questions = ((pending as QuestionnaireFields).questions ?? []);
  for (let index = 0; index < questions.length; index += 1) {
    // Multi-question questionnaires render a step switcher (buttons under a
    // named group — the tab strip died with the tabpanel it never had, §10)
    // and keep submit disabled until EVERY question is answered;
    // single-question ones render no strip.
    if (questions.length > 1) {
      await panel
        .getByRole('group', { name: /Questions to answer|待回答问题/ })
        .getByRole('button')
        .nth(index)
        .click();
    }
    const label = String(questions[index]?.options?.[0]?.label ?? '').trim();
    if (label) {
      const option = panel.getByRole('radio', { name: label }).or(panel.getByRole('checkbox', { name: label })).first();
      await expect(
        option,
        `the panel should render the agent's own option ${JSON.stringify(label)} after the restart`,
      ).toBeVisible({ timeout: 30_000 });
      await option.click();
    } else {
      // The agent asked an open question: the card opens straight into the
      // free-text box, which is what the user types into.
      await panel.locator('textarea').first().fill(`E2E restart answer ${index + 1}`);
    }
  }

  const submit = panel.getByRole('button', { name: SUBMIT_ANSWER });
  await expect(submit, 'the answered questionnaire should offer an enabled submit').toBeEnabled({ timeout: 30_000 });
  await submit.click();
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('pending interaction and turn lock survive a server restart', async ({ page, request }) => {
  // Restarting the shared server would break any spec running beside it, so
  // this must not be a skip — that would just be the scheduling problem
  // wearing a disguise. `run-round.mjs` gives every spec that calls
  // `restartServerContainer` a serial pass of its own, so the isolation is
  // arranged rather than hoped for.
  const api = new AstraApi(request);
  const runId = Date.now();
  // A gated Write is the provocation, not AskUserQuestion: the invariant under
  // test is that a PENDING INTERACTION survives the restart and resolves — the
  // interaction's kind is incidental.
  const pendingFile = `e2e-restart-lock-${runId}.txt`;

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  try {
    await api.waitForSessionReady(sessionId);
    // The page dispatches under the session's permission_mode (useSessionChat
    // sends permissionModeRef.current, seeded from session.permission_mode at
    // load), so this must be set BEFORE the page opens.
    await api.setPermissionMode(sessionId, 'default');

    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });

    const terminalBefore = snapshotDoc(sessionId)?.last_turn_terminal_frame as
      | Record<string, unknown>
      | null
      | undefined;
    const terminalBeforeKey = JSON.stringify(terminalBefore ?? null);

    // The turn is fired from the composer and parks on the interaction. No
    // detached fetch / client-side abort is needed any more: the page owns the
    // send, which is also what makes the restart below land on a real browser
    // session rather than on a request nobody was watching.
    await sendFromComposer(
      page,
      `E2E restart interaction lock ${runId}. Call the Write tool now to create the relative path ` +
        `${pendingFile} with one sentence of content. Do not describe the operation or use another tool. ` +
        'Wait when the platform asks the user to approve the Write.',
      `E2E restart interaction lock ${runId}`,
    );

    // ── PRECONDITION: the requested Write must park on its permission gate. ─
    // A new terminal frame proves the turn ended without producing the state
    // this restart test is meant to exercise. Report that terminal immediately
    // instead of spending the rest of the probe budget and misclassifying it.
    let pending: Awaited<ReturnType<AstraApi['getPendingInteraction']>> = null;
    const probeDeadline = Date.now() + PROBE_TIMEOUT_MS;
    while (Date.now() < probeDeadline) {
      pending = await api.getPendingInteraction(sessionId);
      if (pending) break;
      const terminalNow = snapshotDoc(sessionId)?.last_turn_terminal_frame as
        | Record<string, unknown>
        | null
        | undefined;
      if (JSON.stringify(terminalNow ?? null) !== terminalBeforeKey) {
        const rendered = (await page.getByTestId('assistant-message').allInnerTexts()).join('\n').trim();
        throw new Error(
          'the requested gated Write reached a terminal result without a pending interaction; ' +
            `terminal=${JSON.stringify(terminalNow)} rendered=${JSON.stringify(rendered)}`,
        );
      }
      await new Promise((r) => setTimeout(r, 2_000));
    }
    expect(
      pending,
      `the requested gated Write must expose a pending interaction within ${PROBE_TIMEOUT_MS}ms`,
    ).not.toBeNull();
    const interactionId = String(pending!.interaction_id ?? '').trim();
    const pendingTurnId = String(pending!.turn_id ?? '').trim();
    expect(interactionId, 'pending interaction must expose an id').toBeTruthy();

    // It reaches the USER: the composer is replaced by the pending panel. This
    // is the state the restart has to preserve — a question someone is looking at.
    const panel = page.getByTestId('pending-interaction-panel');
    await expect(
      panel,
      'the raised interaction must reach the user as the composer pending panel before the restart',
    ).toBeVisible({ timeout: 60_000 });

    // `busy_token` is the cross-instance turn lock and is not rendered.
    const sessionBefore = sessionDoc(sessionId);
    const busyTokenBefore = String(sessionBefore?.busy_token ?? '').trim();

    // ── Instance replacement while the interaction is pending. ──────────────
    const baseUrl = absoluteBaseUrl();
    await restartServerContainer(baseUrl);

    // ── The interaction must survive the bootstrap sweep — and survive it all
    //    the way to the screen. The reload is what makes this a real oracle:
    //    the tab that watched the restart is refreshed, so nothing the panel
    //    draws afterwards can come from client memory of the pre-restart turn.
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: REHYDRATE_RENDER_MS });
    await expect(
      panel,
      'after the restart a freshly loaded page must still show the pending question — ' +
        'rehydrated from durable state, or the user is left with an unanswerable session',
    ).toBeVisible({ timeout: REHYDRATE_RENDER_MS });

    // Single sample, not a poll: durable state that survived a restart is
    // already there or it is lost. No pixel — no testid renders an interaction id.
    const pendingAfter = await api.getPendingInteraction(sessionId);
    expect(
      String(pendingAfter?.interaction_id ?? '').trim(),
      `the SAME pending interaction must survive the restart; after=${JSON.stringify(pendingAfter)}`,
    ).toBe(interactionId);
    if (busyTokenBefore) {
      const sessionAfter = sessionDoc(sessionId);
      expect(
        String(sessionAfter?.busy_token ?? '').trim(),
        'bootstrap must not clear the cross-instance turn lock (busy_token)',
      ).toBe(busyTokenBefore);
    }

    // ── Answering post-restart, from the panel, must be accepted and settle
    //    the turn. Which option was chosen is not the claim — that answers
    //    RESOLVE the turn across the restart is. One answer is not guaranteed
    //    to be the last: the model may gate a further tool call on the same
    //    turn (measured: deepseek re-parked 2s after the first approval, a
    //    fresh interaction id behind the same turn), and the user's move is
    //    simply to keep answering. So answer in a bounded loop keyed on the
    //    SERVER's own pending row — with a hard cap, because a model re-asking
    //    for the SAME thing forever is a defect this loop must not launder.
    await answerFromPanel(page, pendingAfter as PendingInteraction);
    const answeredIds = new Set([interactionId]);
    const resolveDeadline = Date.now() + SETTLE_MS;
    while (Date.now() < resolveDeadline) {
      const stateNow = String((await api.getSession(sessionId)).state ?? '').trim();
      if (stateNow === 'READY') break;
      const again = await api.getPendingInteraction(sessionId);
      const againId = String(again?.interaction_id ?? '').trim();
      if (againId && !answeredIds.has(againId)) {
        expect(
          answeredIds.size,
          'the provoked single-Write turn should not need more than 4 gates — a longer chain is the model looping, not a user flow',
        ).toBeLessThan(4);
        await expect(
          panel,
          `the further gate ${againId} must reach the user as the pending panel`,
        ).toBeVisible({ timeout: 30_000 });
        await answerFromPanel(page, again as PendingInteraction);
        answeredIds.add(againId);
      }
      await new Promise((r) => setTimeout(r, 1_000));
    }

    // ── The turn resolves where the user watches it. ────────────────────────
    await expect(panel, 'the answered question should clear off the composer').toBeHidden({
      timeout: ANSWER_ACCEPTED_MS,
    });
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first(), 'the header must settle once the resumed turn ends')
      .toHaveAttribute('data-pulse', 'false', { timeout: SETTLE_MS });

    // A reply, not a rendered failure: a turn that dies after the restart puts
    // its error INTO the transcript as an assistant message, which satisfies
    // every "there is a bubble" oracle. Wording is not part of this invariant.
    const reply = page.getByTestId('assistant-message').last();
    await expect(reply, 'the resumed turn should have rendered assistant content').not.toBeEmpty();
    await expect(reply).not.toContainText(TURN_ERROR);

    // The turn lock released, in the only form a user can see it: the composer
    // is back and sendable. (An EMPTY composer keeps submit disabled by
    // design — type first, then assert.)
    await page.getByTestId('composer-prompt').fill('follow-up');
    await expect(page.getByTestId('composer-submit')).toBeEnabled({ timeout: 15_000 });

    // ── The two facts the screen cannot bind: turn identity, and the durable
    //    projection behind the settled pill. ─────────────────────────────────
    const ready = await api.waitForSessionReady(sessionId);
    expect(ready.state, 'session should settle READY after the answered continuation').toBe('READY');
    if (pendingTurnId) {
      // The failure pixel is asserted above; what has no pixel is WHICH turn a
      // failure belonged to — the journal is the only place that binds them.
      const failed = sessionEvents(sessionId).filter(
        (event) =>
          String(event.turn_id ?? '') === pendingTurnId
          && String(event.event_type ?? '') === 'turn.failed',
      );
      expect(failed.length, 'the answered turn must not fail after restart').toBe(0);
    }
    expect(
      String(snapshotDoc(sessionId)?.conversation_state ?? ''),
      'conversation should be IDLE after settle',
    ).toBe('IDLE');

    console.log('RESTART_INTERACTION_LOCK_E2E_EVIDENCE', JSON.stringify({
      session_id: sessionId,
      interaction_id: interactionId,
      turn_id: pendingTurnId,
      busy_token_before: Boolean(busyTokenBefore),
      final_state: ready.state,
    }));
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
