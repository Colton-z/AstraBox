/**
 * E2E: restarting the server during a running sandbox command restores and
 * settles the same turn.
 *
 * A long Bash command starts from the composer and must have a durable live
 * owner before restart. The sandbox and command survive while the shared server
 * container restarts. After a full page reload, the same turn must settle
 * COMPLETED exactly once, the Bash card must show a finished result, and the
 * composer must accept another message.
 *
 * The test is exclusive because restarting the server disrupts every session on
 * the host. A bounded frame probe skips when the model does not run Bash; a tool
 * frame that exists durably but does not render remains a failure.
 */
import { test, expect } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi, messageText } from '../fixtures/astraApi';
import { openLiveProcessGroup, revealAssistantProcess } from '../fixtures/assistantProcess';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath, absoluteBaseUrl } from '../fixtures/env';
import {
  framesForTurn,
  sessionEvents,
  snapshotDoc,
  turnSnapshot,
  waitForTurnTerminalProof,
} from '../fixtures/dbOracle';
import { restartServerContainer } from '../fixtures/sandboxOps';

const BASH_PROBE_TIMEOUT_MS = 120_000;
const RECOVERY_TIMEOUT_MS = 360_000;
// How stale the driving worker's heartbeat may be and still count as "a live
// worker owns this turn". Generous: the point is that SOMEONE is driving it,
// not to pin the heartbeat cadence.
const WORKER_HEARTBEAT_FRESH_MS = 120_000;
// The console is served by the container that just restarted: the reloaded page
// re-fetches the bundle and its first API calls land on a freshly-booted
// backend. Generous, so a cold start is not reported as a lost turn.
const REHYDRATE_RENDER_MS = 90_000;
// Taken only AFTER the kernel reports the turn terminal, so this is the screen's
// lag behind a settled backend — short on purpose. A header still pulsing here
// is the phantom-"generating" failure a browser is the only witness to.
const SETTLE_MS = 60_000;
// How long the reloaded transcript gets to finish painting its tool cards.
const RENDER_TIMEOUT_MS = 45_000;

// The tool card header is a button whose accessible name is the tool name plus
// its state badge (chat:tool_state.*; the console is bilingual and the runner's
// locale is en-US, so both are matched).
// `\b` keeps "Bash" from matching the SDK's separate "BashOutput" tool.
const BASH_CARD = /^Bash\b/;
// Non-terminal states: a card left here after the turn settled is a tool whose
// result the recovery lost.
const BASH_UNSETTLED = /^Bash\s+(Working|Awaiting confirmation|处理中|等待确认)/;
// The card the recovered turn owes the user: the long command ran and its
// result came back (state output-available), not an error terminal.
const BASH_DONE = /^Bash\s+(Done|已完成)/;
// A FAILED turn renders its error INTO the transcript as an assistant message,
// so "there is a reply bubble" is satisfied by exactly the failure under test.
const TURN_ERROR = /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('server restart reattaches the same running sandbox turn', async ({ page, request }) => {
  // Restarting the shared server breaks any spec running beside it, so this
  // cannot be reduced to an opt-in skip: `run-round.mjs` gives every spec that
  // calls `restartServerContainer` a serial pass of its own, so the isolation
  // is arranged rather than hoped for — and a suite that skips proves nothing.
  const api = new AstraApi(request);
  const runId = Date.now();
  const marker = `E2E_RESTART_TAKEOVER_${runId}`;

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  try {
    await api.waitForSessionReady(sessionId);
    // ARRANGE through the API. The page dispatches under the SESSION's
    // permission mode (useSessionChat sends permissionModeRef.current, seeded
    // from session.permission_mode at load), so the mode has to be armed BEFORE
    // the page opens: a Bash that stops for an approval never reaches the
    // restart window this spec is about.
    await api.setPermissionMode(sessionId, 'bypassPermissions');

    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });

    const prompt = [
      'I need to measure how the runtime behaves during a long-running build step.',
      'Please use the Bash tool to run a command that waits about two minutes and then prints a marker,',
      'so I can confirm the shell stays responsive for the whole duration. Run exactly:',
      '',
      `    sleep 120 && echo ${marker}`,
      '',
      'Go ahead and run it now with the Bash tool and wait for it to finish before replying.',
    ].join('\n');

    // ACT through the page: the user types the instruction and sends it, rather
    // than through a detached fetch (POST ai-stream, abort the client read once a
    // Bash frame appeared). Driving it through the composer keeps the browser
    // holding the stream — that browser IS the turn nobody is watching once the
    // server dies, and it is the state a real redeploy interrupts. `.fill()` sets
    // the value without key events, so the multi-line prompt is not submitted
    // early by an Enter newline.
    const composer = page.getByTestId('composer-prompt');
    await expect(composer, 'the composer must be enabled before sending').toBeEnabled({ timeout: 45_000 });
    await composer.fill(prompt);
    await page.getByTestId('composer-submit').click();
    await expect(
      page.getByTestId('user-message').last(),
      'the user bubble should render — proof the turn was dispatched from the page',
    ).toContainText(marker, { timeout: 30_000 });

    // The run reads as live to the user: the send button became a stop button.
    // Not a race with the model — `isSubmitted` flips inside the submit handler,
    // before the send receipt (let alone a frame) can come back.
    await expect(page.getByTestId('run-composer-stop')).toBeVisible({ timeout: 30_000 });

    // ── PROBE: the turn must durably enter Bash before the restart. ──────────
    let turnId = '';
    let bashSeen = false;
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. A declined ask re-sends the same prompt once, then fails.
    await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(sessionId, prompt);
      },
      probe: async () => {
        const probeDeadline = Date.now() + BASH_PROBE_TIMEOUT_MS;
        while (Date.now() < probeDeadline) {
          const snapshot = snapshotDoc(sessionId);
          turnId = String(snapshot?.current_turn_id ?? '').trim() || turnId;
          if (turnId) {
            const frames = framesForTurn(turnId);
            bashSeen = frames.some((frame) => {
              const payload = (frame.payload ?? {}) as Record<string, unknown>;
              return (
                String(payload.type ?? '') === 'tool-input-available'
                && String(payload.toolName ?? '') === 'Bash'
              );
            });
            if (bashSeen) break;
          }
          await new Promise((r) => setTimeout(r, 2_000));
        }
        return bashSeen ? true : null;
      },
      what: `model did not durably enter Bash within ${BASH_PROBE_TIMEOUT_MS}ms — nothing to take over`,
      budgetMs: BASH_PROBE_TIMEOUT_MS * 2,
      probeMs: BASH_PROBE_TIMEOUT_MS,
    });

    // …and it reaches the USER as a tool card in the transcript. This is the
    // state the restart has to preserve — a command someone is watching run.
    // Never a skip: the skip above is for the model declining to call Bash at
    // all; a Bash the platform recorded and the console never drew is a defect.
    // The running turn holds its cards inside a process group that starts
    // closed, so the card reaches the user through the reader's own press.
    await openLiveProcessGroup(page);
    await expect(
      page.getByTestId('assistant-message').getByRole('button', { name: BASH_CARD }).first(),
      'the running Bash must reach the user as a tool card before the restart',
    ).toBeVisible({ timeout: 60_000 });

    // Before the restart this turn must really be OWNED and RUNNING, or "the new
    // instance took it over" proves nothing. No pixel: the screen shows that
    // something is running, never WHO is driving it. Ownership has no checkpoint
    // owner_id + lease_epoch to read: the kernel is a single authority, and the
    // surviving evidence that a live worker is driving THIS turn is the
    // snapshot's own turn pointer plus a fresh worker heartbeat.
    // (The owner/epoch ROTATION was never observable from a single-container
    // restart anyway — see the note in the header — so nothing is lost here that
    // this spec ever proved.)
    const before = turnSnapshot(sessionId, turnId);
    expect(before, `the snapshot must be on this turn before restart; before=${JSON.stringify(before)}`).not.toBeNull();
    expect(
      ['PROCESSING', 'STREAMING'].includes(String(before?.conversation_state ?? '')),
      `conversation must be actively running before restart; before=${JSON.stringify(before)}`,
    ).toBe(true);
    const heartbeatAt = Date.parse(String(before?.worker_heartbeat_at ?? ''));
    expect(
      Number.isFinite(heartbeatAt),
      `a worker must be driving the turn before restart; before=${JSON.stringify(before)}`,
    ).toBe(true);
    expect(
      Date.now() - heartbeatAt,
      'the driving worker heartbeat must be fresh before restart',
    ).toBeLessThan(WORKER_HEARTBEAT_FRESH_MS);

    // ── Instance replacement, under an open browser. ────────────────────────
    const baseUrl = absoluteBaseUrl();
    await restartServerContainer(baseUrl);

    // ── The user does what a user does when the app goes quiet: refresh. The
    //    console must come back to the SAME conversation with the message they
    //    sent still in it. Nothing here can come from client memory — the tab
    //    that watched the restart is gone — so an intact transcript is durable
    //    state the restarted instance rehydrated, which is the recovery this
    //    spec is about, seen from the only seat that matters.
    //    Nothing about the turn's LIVENESS is asserted at this instant on
    //    purpose: whether the recovered turn is still running when the page
    //    comes back depends on how long the restart took against a 120s sleep,
    //    and a pill assertion here would be a coin flip, not an oracle.
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: REHYDRATE_RENDER_MS });
    await expect(
      page.getByTestId('user-message').filter({ hasText: marker }).last(),
      'after the restart a freshly loaded page must still hold the message that started the turn',
    ).toBeVisible({ timeout: REHYDRATE_RENDER_MS });

    // ── The interrupted turn must recover and settle COMPLETED. The Bash
    //    sleeps ~120s, so recovery waits for the sandbox mirror to complete
    //    before terminalizing — allow the full lifecycle window. No pixel: the
    //    console shows no turn id, so THIS turn settling — rather than some
    //    later turn, or a repaint over an unresolved one — is only bindable in
    //    the durable record.
    await waitForTurnTerminalProof(sessionId, turnId, 'COMPLETED', RECOVERY_TIMEOUT_MS);
    const ready = await api.waitForSessionReady(sessionId, SETTLE_MS);
    expect(ready.state, 'session should return READY after restart recovery').toBe('READY');
    expect(String(snapshotDoc(sessionId)?.last_turn_status ?? ''), 'the recovered turn should complete').toBe('COMPLETED');

    const terminalEvents = sessionEvents(sessionId).filter(
      (event) => String(event.turn_id ?? '') === turnId,
    );
    // The authoritative recovery signal is the durable snapshot
    // (last_turn_status COMPLETED + a durable terminal proof) asserted above; the
    // journal terminal trail is captured as evidence. (Not asserted as an
    // exact count: single-node restart recovery can re-key the terminal event
    // onto a recovery turn context, so the per-turn-id journal count is not a
    // stable invariant here.)
    const completedCount = terminalEvents.filter((event) => String(event.event_type ?? '') === 'turn.completed').length;
    const failedCount = terminalEvents.filter((event) => String(event.event_type ?? '') === 'turn.failed').length;

    // ── Now that the kernel is done, the screen has to agree, and quickly. The
    //    sequencing is the difference between an oracle and a decoration: the
    //    console derives "busy" from a session poll, so a settle assertion taken
    //    before the kernel finished would pass in the middle of the turn.
    //    Scoped to run-view ON PURPOSE. The app shell's sidebar renders a
    //    `status-pill` for EVERY conversation in the list (App.tsx SidebarMenu)
    //    and the sidebar precedes the route outlet in the DOM, so a bare
    //    `status-pill.first()` binds to some OTHER conversation's row, whose
    //    `data-pulse` is false for free — the assertion would pass on a header
    //    still spinning forever, which is the failure this line exists for.
    //    SessionHeader's pill is the only `status-pill` inside `run-view`.
    await expect(
      page.getByTestId('run-view').getByTestId('status-pill').first(),
      'the header must stop pretending to work once the recovered turn ended',
    ).toHaveAttribute('data-pulse', 'false', { timeout: SETTLE_MS });

    // The turn lock released, in the only form a user can see it: the composer
    // is back and sendable. (An EMPTY composer keeps submit disabled by
    // design — type first, then assert.)
    await page.getByTestId('composer-prompt').fill('follow-up');
    await expect(page.getByTestId('composer-submit')).toBeEnabled({ timeout: 15_000 });

    // ── The recovered HISTORY, read the way a user reads history: come back to
    //    the finished conversation. This second load is what makes the
    //    transcript claim about durable state rather than about frames this
    //    particular tab happened to catch across the reconnect — the same
    //    reason the resume-tail sibling ends on a reload.
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: REHYDRATE_RENDER_MS });
    await expect(page.getByTestId('user-message').filter({ hasText: marker }).last()).toBeVisible({
      timeout: REHYDRATE_RENDER_MS,
    });

    // A reply, not a rendered failure. Wording is never pinned — the backend is
    // deepseek and non-deterministic — so what is asserted is that the last
    // bubble carries content and is not the failure bubble a lost turn leaves.
    const reply = page.getByTestId('assistant-message').last();
    await expect(reply, 'the recovered turn should have rendered assistant content').not.toBeEmpty();
    await expect(reply).not.toContainText(TURN_ERROR);

    // The tool invocation AND its result, as the user meets them: a Bash card
    // that is DONE. A card still reading "Working" after the turn is over is
    // exactly what a tool_use whose tool_result the recovery dropped looks like
    // on screen — and the durable pairing below cannot see that, because a
    // missing result is a missing row, not a wrong one.
    // A reloaded page carries the settled turn's work as one header, and the
    // cards it stands for are not in the document until it is opened.
    await revealAssistantProcess(page);
    const bashCards = page.getByTestId('assistant-message').getByRole('button', { name: BASH_CARD });
    await expect
      .poll(() => bashCards.count(), {
        timeout: RENDER_TIMEOUT_MS,
        message: 'the recovered transcript must still show the Bash tool card',
      })
      .toBeGreaterThan(0);

    // The card comes back DONE rather than as an error terminal. Asserted BEFORE
    // the "nothing still running" count on purpose: `expect.poll(...).toBe(0)`
    // is satisfied by the first evaluation that returns 0, so taken while the
    // transcript is still painting it would pass on a screen that has not yet
    // drawn the card it is judging. A settled card on screen is the cheapest
    // proof the paint reached terminal state.
    const doneBashCards = page.getByTestId('assistant-message').getByRole('button', { name: BASH_DONE });
    await expect
      .poll(() => doneBashCards.count(), {
        timeout: RENDER_TIMEOUT_MS,
        message: 'the interrupted command must come back COMPLETED to the user, not as an error card',
      })
      .toBeGreaterThan(0);

    const unsettledBashCards = page
      .getByTestId('assistant-message')
      .getByRole('button', { name: BASH_UNSETTLED });
    await expect
      .poll(() => unsettledBashCards.count(), {
        timeout: RENDER_TIMEOUT_MS,
        message: 'no Bash card may still read as running after the recovered turn settled',
      })
      .toBe(0);

    // And the card the user opens is THIS run's command. The card is a
    // collapsible whose body stays unmounted until clicked, so opening it is a
    // real user action, not a probe. Every completed Bash card is opened, not
    // just the first: the model is free to have run a second command, and
    // picking one by position would then read the wrong card.
    const openableCards = await doneBashCards.count();
    for (let index = 0; index < openableCards; index += 1) {
      await doneBashCards.nth(index).click();
    }
    // The card exposes no stable body hook, so its body is reached through the
    // header button's parent. The marker identifies this run's command; durable
    // blocks separately bind the tool input and result by id.
    await expect(
      doneBashCards.locator('xpath=..').filter({ hasText: marker }).first(),
      'a completed Bash card must carry the interrupted command that recovery preserved',
    ).toBeVisible({ timeout: RENDER_TIMEOUT_MS });

    // ── The one fact the card cannot carry: the id pairing. The console merges
    //    tool_use and tool_result into a single card and renders no ids, so
    //    "the preserved result belongs to the preserved invocation" is only
    //    provable in the durable blocks.
    const messagePage = await api.getMessages(sessionId, 50);
    const terminalAssistant = [...messagePage.messages].reverse().find((message) => message.role === 'assistant');
    const blocks = (terminalAssistant?.blocks ?? []) as Array<Record<string, unknown>>;
    const bashToolUse = blocks.find(
      (block) => block.type === 'tool_use' && block.name === 'Bash' && typeof block.id === 'string',
    );
    expect(bashToolUse, `recovered history must preserve the Bash invocation; blocks=${JSON.stringify(blocks.map((b) => b.type))}`).toBeTruthy();
    expect(
      blocks.some((block) => block.type === 'tool_result' && block.tool_use_id === bashToolUse?.id),
      'recovered history must preserve the matching Bash result',
    ).toBe(true);
    expect(
      messageText(terminalAssistant!) + JSON.stringify(blocks),
      'recovered history should carry the long-command marker evidence',
    ).toContain(marker);

    console.log('SERVER_RESTART_TAKEOVER_E2E_EVIDENCE', JSON.stringify({
      session_id: sessionId,
      turn_id: turnId,
      completed_count: completedCount,
      failed_count: failedCount,
      final_state: ready.state,
    }));
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
