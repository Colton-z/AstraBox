/**
 * E2E: a message sent while a response runs is never off screen.
 *
 * A message is in the transcript from the moment it is sent, in the place
 * the session's history keeps it: after the response that was running when it
 * arrived. While the platform holds it for the runtime it says so ("Queued"),
 * and it stays the same message through the runtime taking it and answering
 * it — never removed, never shown twice, never reported as failed.
 *
 * Reported from the console against the queue this transcript replaced: the
 * second message left the queue before the transcript showed it, and was on
 * neither surface for the length of a reply.
 *
 * The first response is held open by a Bash command that waits on a file, so
 * the second message is genuinely held rather than racing a fast reply.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { openLiveProcessGroup, revealAssistantProcess } from '../fixtures/assistantProcess';
import { findClippedDecorations } from '../fixtures/clippedDecorations';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';

const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 120_000);

interface HandoverSample {
  at: number;
  shown: number;
}

interface HandoverRecord {
  samples: HandoverSample[];
  /** Samples where the message was not exactly once in the transcript. */
  gaps: HandoverSample[];
  /** Samples where the message read as a failed send. */
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
    await expect(page.locator('[data-tool-call-id]').first()).toBeAttached({ timeout: 60_000 });
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

    const userBubble = page
      .getByTestId('user-message')
      .filter({ hasText: queuedMarker });
    await expect(userBubble, 'the message is in the transcript the moment it is sent').toHaveCount(1, { timeout: 30_000 });
    await expect(
      userBubble,
      'a message the platform holds for the runtime says it is queued',
    ).toContainText(/Queued|等待发送/, { timeout: 30_000 });
    // It comes after the response that was running when it was sent, where
    // the session's history keeps it.
    const order = await page.evaluate((marker) => Array.from(
      document.querySelectorAll('[data-testid="user-message"], [data-testid="assistant-message"]'),
    ).map((node) => (
      node.getAttribute('data-testid') === 'user-message' && (node as HTMLElement).innerText.includes(marker)
        ? 'queued' : node.getAttribute('data-testid')
    )), queuedMarker);
    expect(order.slice(-2), 'the held message follows the running response').toEqual(['assistant-message', 'queued']);

    // A held turn is the only place the streaming indicators stand still long
    // enough to be measured, so this spec answers §11's second question about
    // them while it has the chance: the live dot in front of "Generating" sits
    // against the left edge of the message body, and a body that clips cuts the
    // halo off on that side. Reported from the console, and invisible to a
    // keyboard walk — a pulsing dot takes no focus.
    await expect(page.getByTestId('assistant-message').last()).toBeVisible({ timeout: 60_000 });
    expect(
      await findClippedDecorations(page),
      'an indicator an ancestor clips is drawn with a side missing',
    ).toEqual([]);

    // Watch from inside the page, before the handover can start.
    await page.evaluate((marker) => {
      const bubbles = () => Array.from(
        document.querySelectorAll('[data-testid="user-message"]'),
      ).filter((node) => (node as HTMLElement).innerText?.includes(marker)) as HTMLElement[];
      const record: HandoverRecord = {
        samples: [],
        gaps: [],
        failures: [],
        stop: () => {},
      };
      const sample = () => {
        const shown = bubbles();
        const at = performance.now();
        record.samples.push({ at, shown: shown.length });
        if (shown.length !== 1) record.gaps.push({ at, shown: shown.length });
        // An error that appears and goes away on its own: a message the
        // platform has receipted is not an unconfirmed send, so nothing here
        // should ever read as failed.
        const failed = shown
          .filter((node) => node.querySelector('[data-testid="user-message-failure"]'))
          .map((node) => node.innerText.trim());
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

    // Let the first response finish; the runtime then takes the held message
    // and answers it, with nothing this page sends.
    await api.runTerminalCommand(sessionId, `touch ${release}`, '/tmp', 30_000);

    // The invariant: at no instant is the message off screen or doubled.
    // Sampling from the driver cannot state that — it only sees the DOM between
    // polls, and a gap shorter than one interval passes unseen. So the sampling
    // runs IN the page, on every mutation, and the run is judged from the
    // record afterwards.
    await expect(
      userBubble,
      'once the runtime takes it the message stops saying it is queued',
    ).not.toContainText(/Queued|等待发送/, { timeout: TURN_TIMEOUT_MS });
    await expect(
      page.getByTestId('assistant-message').last(),
      'the held message gets its own answer, after it',
    ).toBeVisible({ timeout: TURN_TIMEOUT_MS });
    const answeredOrder = await page.evaluate((marker) => Array.from(
      document.querySelectorAll('[data-testid="user-message"], [data-testid="assistant-message"]'),
    ).map((node) => (
      node.getAttribute('data-testid') === 'user-message' && (node as HTMLElement).innerText.includes(marker)
        ? 'queued' : node.getAttribute('data-testid')
    )), queuedMarker);
    expect(answeredOrder.indexOf('queued'), 'the held message is in the transcript').toBeGreaterThanOrEqual(0);
    expect(
      answeredOrder.slice(answeredOrder.indexOf('queued') + 1),
      'its answer follows it',
    ).toContain('assistant-message');

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
      'a sent message is in the transcript exactly once at every instant',
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

    // And it is one message, the one history keeps.
    await expect(userBubble).toHaveCount(1);
    const history = await api.getMessages(sessionId, 50);
    expect(
      history.messages.filter((message) => message.role === 'user' && messageText(message) === queuedMarker),
      'history holds the held message once',
    ).toHaveLength(1);
  } finally {
    await api.runTerminalCommand(sessionId, `touch ${release}`, '/tmp', 15_000).catch(() => {});
  }
});
