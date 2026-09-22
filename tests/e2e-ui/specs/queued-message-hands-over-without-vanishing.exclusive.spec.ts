/**
 * E2E: a queued message is never on neither surface.
 *
 * The queue and the transcript divide one message between them: the queue
 * holds what this platform has accepted and the engine has not taken, the
 * transcript holds what the engine received. Consumption is the handover, and
 * a handover has no gap — at every instant the message a user sent is on one
 * of the two surfaces.
 *
 * Reported from the console and not caught by any suite: the second message
 * entered the queue, disappeared from it, and the transcript went on showing
 * the PREVIOUS answer above "Generating…" until the new answer arrived, at
 * which point the message finally appeared. A queued input is handed to the
 * engine by the backend, so the client never accepts a turn of its own for it;
 * the consumption handler cleared the queue row and skipped the projection
 * because it had no turn id to anchor on, and the message was on neither
 * surface for the length of a turn.
 *
 * The first turn is held open by a Bash command that waits on a file, so the
 * second message is genuinely queued rather than racing a fast reply.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { openLiveProcessGroup, revealAssistantProcess } from '../fixtures/assistantProcess';
import { findClippedDecorations } from '../fixtures/clippedDecorations';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';

const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 120_000);

interface HandoverSample {
  at: number;
  queued: number;
  shown: number;
}

interface HandoverRecord {
  samples: HandoverSample[];
  /** Samples where the message was on neither surface. */
  gaps: HandoverSample[];
  /** Samples where the queued row read as a failed send. */
  failures: Array<{ at: number; text: string }>;
  stop: () => void;
}

const sessions = trackSessions();

test('a queued message hands over to the transcript without vanishing', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  const runId = `${Date.now()}`;
  // The Agent and platform terminal run in sibling isolated sessions. Their
  // /tmp mounts are private; the conversation workspace is the shared surface.
  const rendezvous = `/workspace/.astrabox-e2e-queue-handover-${runId}`;
  const started = `${rendezvous}.started`;
  const release = `${rendezvous}.release`;
  const queuedMarker = `QUEUE_HANDOVER_${runId}`;

  try {
    await api.waitForSessionReady(sessionId);
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible();

    // Turn one, held open until this spec releases it.
    const composer = page.getByTestId('composer-prompt');
    await expect(composer).toBeEnabled({ timeout: 60_000 });
    await composer.fill([
      `E2E queue handover ${runId}.`,
      'Run exactly this Bash command and nothing else:',
      `touch ${started}; while [ ! -f ${release} ]; do sleep 0.2; done; printf HELD_${runId}`,
      'Then reply with the command output.',
    ].join('\n'));
    await page.getByTestId('composer-submit').click();

    // The held command is running, so the next message can only be queued.
    await expect.poll(
      async () => (await api.runTerminalCommand(
        sessionId, `test -f ${started} && echo yes || echo no`, '/tmp', 30_000,
      )).includes('yes'),
      { message: 'the first turn must reach its held Bash command', timeout: TURN_TIMEOUT_MS, intervals: [1_000, 2_000] },
    ).toBe(true);

    // The tool card, while the command it names is still running. This spec
    // holds a real Bash call open, which is the only place in the suite where
    // a tool's RUNNING state stands still long enough to be asserted — and
    // nothing asserted the card at all before this: a probe that enumerated
    // `data-testid` descendants reported it missing, because the card carries
    // none, and the card was rendering the whole time.
    //
    // Located the way a reader finds it, per Playwright's own guidance: the
    // card's header is a disclosure button whose accessible name is the tool
    // and its state. A test id would pass while the label said something no
    // reader recognises.
    // The running turn keeps that card inside a process group that starts
    // closed, so the reader opens the group before the card is on screen.
    await openLiveProcessGroup(page);
    const toolCard = page.getByRole('button', { name: /Bash/ });
    await expect(
      toolCard.first(),
      'a tool call the reader is waiting on has a card naming it',
    ).toBeVisible({ timeout: 60_000 });
    await expect(
      toolCard.first(),
      'a held command reads as working, not as finished',
    ).toHaveText(/Working|处理中/, { timeout: 30_000 });

    await expect(composer, 'a busy conversation still accepts a queued message').toBeEnabled({ timeout: 30_000 });
    await composer.fill(queuedMarker);
    await page.getByTestId('composer-submit').click();

    const queueRow = page.getByTestId('composer-queue').filter({ hasText: queuedMarker });
    await expect(queueRow, 'the queued message belongs to the queue until the engine takes it').toHaveCount(1, { timeout: 30_000 });

    // A held turn is the only place the streaming indicators stand still long
    // enough to be measured, so this spec answers §11's second question about
    // them while it has the chance: the live dot in front of "Generating" sits
    // against the left edge of the message body, and a body that clips cuts the
    // halo off on that side. Reported from the console, and invisible to the
    // console-interaction walk, which reaches indicators through the keyboard —
    // a pulsing dot takes no focus.
    await expect(page.getByTestId('assistant-message').last()).toBeVisible({ timeout: 60_000 });
    expect(
      await findClippedDecorations(page),
      'an indicator an ancestor clips is drawn with a side missing',
    ).toEqual([]);

    // Watch from inside the page, before the handover can start.
    await page.evaluate((marker) => {
      const count = (testId: string) => Array.from(
        document.querySelectorAll(`[data-testid="${testId}"]`),
      ).filter((node) => (node as HTMLElement).innerText?.includes(marker)).length;
      const record: HandoverRecord = {
        samples: [],
        gaps: [],
        failures: [],
        stop: () => {},
      };
      const sample = () => {
        const queued = count('composer-queue');
        const shown = count('user-message');
        const at = performance.now();
        record.samples.push({ at, queued, shown });
        if (queued + shown === 0) record.gaps.push({ at, queued, shown });
        // The other half of the same report: an error appeared and went away
        // on its own. A queued message that the platform has receipted is not
        // an unconfirmed send, so nothing here should ever read as failed.
        const failed = Array.from(document.querySelectorAll('[data-testid="composer-queue"]'))
          .filter((node) => (node as HTMLElement).innerText?.includes(marker))
          .map((node) => (node as HTMLElement).innerText.trim())
          .filter((text) => /失败|failed|Failed/.test(text));
        if (failed.length) record.failures.push({ at, text: failed[0].slice(0, 160) });
      };
      const observer = new MutationObserver(sample);
      observer.observe(document.body, { childList: true, subtree: true, characterData: true });
      // A floor under the mutation stream: a gap opened by a render that
      // mutates nothing observable still gets sampled.
      const ticker = window.setInterval(sample, 20);
      record.stop = () => { observer.disconnect(); window.clearInterval(ticker); };
      sample();
      (window as unknown as { __handover?: HandoverRecord }).__handover = record;
    }, queuedMarker);

    // The first turn ending is NOT this message's handover: it was queued
    // behind that turn and the engine has not read it yet. A sweep keyed on
    // "the turn this send was bound to has settled" empties the queue here,
    // and nothing shows the message until the next turn's history lands.
    // Let the first turn finish; the backend then hands the queued input to the
    // engine without this client accepting a turn for it.
    await api.runTerminalCommand(sessionId, `touch ${release}`, '/tmp', 30_000);

    // The invariant: at no instant is the message on neither surface. Sampling
    // from the driver cannot state that — it only sees the DOM between polls,
    // and a handover gap shorter than one interval passes unseen. This spec
    // passed once against a build that had the defect for exactly that reason.
    // So the sampling runs IN the page, on every mutation, and the run is
    // judged from the record afterwards.
    const userBubble = page
      .getByTestId('user-message')
      .filter({ hasText: queuedMarker });
    const deadline = Date.now() + TURN_TIMEOUT_MS;
    let landed = false;
    while (Date.now() < deadline) {
      if (await userBubble.count() > 0) {
        landed = true;
        break;
      }
      await page.waitForTimeout(150);
    }
    expect(landed, 'the queued message must reach the transcript').toBe(true);

    const record = await page.evaluate(() => {
      const observed = (window as unknown as { __handover?: HandoverRecord }).__handover;
      observed?.stop();
      return {
        samples: observed?.samples ?? [],
        gaps: observed?.gaps ?? [],
        failures: observed?.failures ?? [],
      };
    });
    // A sampler that never ran would report a clean run, so prove it sampled.
    expect(record.samples.length, 'the in-page sampler must have observed the handover').toBeGreaterThan(5);
    expect(
      record.gaps,
      'a sent message is always on one of the two surfaces — queue or transcript',
    ).toEqual([]);
    expect(
      record.failures,
      'a receipted message waiting its turn is not a failed send',
    ).toEqual([]);

    // The same card, once its command has been released: the state a reader
    // reads off it has to follow the command, not stay where it started.
    // Its turn has settled by now, and settling folds the group open above shut
    // again — behind the turn's own header when the turn also concluded — so the
    // reader's press comes first.
    await revealAssistantProcess(page);
    await expect(
      page.getByRole('button', { name: /Bash/ }).first(),
      'a finished command stops reading as working',
    ).toHaveText(/Done|已完成/, { timeout: TURN_TIMEOUT_MS });

    // And it lands as its own message, before its answer — not folded into the
    // authoritative history that arrives with the reply.
    await expect(userBubble).toHaveCount(1);
  } finally {
    await api.runTerminalCommand(sessionId, `touch ${release}`, '/tmp', 15_000).catch(() => {});
  }
});
