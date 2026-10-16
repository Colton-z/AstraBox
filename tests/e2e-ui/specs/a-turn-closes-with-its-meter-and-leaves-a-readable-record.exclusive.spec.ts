/**
 * E2E: a response's close, stop control, and console record agree after reload.
 *
 * The completed summary exposes duration, cost, usage, and stop reason. Raw
 * system envelopes stay suppressed whether their discriminator is `type` or
 * `__sdk_type`; the localized stop control reports its stopping state; a stop
 * ends the response cancelled and says so quietly, never as a failure; and the
 * console record's response selector replaces one response's parts with
 * another's instead of showing the previous ones under the new name. Messages
 * use the real composer against a READY API-created Session, while a
 * page-error listener covers render-time failures in the built transcript.
 */
import { test, expect, type Locator, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import {
  expectComposerEnabled,
  expectPromptDelivered,
  openSessionView,
  sendPrompt,
  startPromptDelivery,
} from '../fixtures/sessionPage';

// A short response from dispatch to the meter that closes it. On the demo
// deployment a one-line answer renders in ~2.5s and the composer frees at
// ~2.7s; the budget is sized for a slow host, not for a hung one.
const TURN_SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_METER_TURN_SETTLE_MS', 60_000);
// Dispatch → the response actually streaming. The stop control is mounted the
// moment the send is in flight but stays disabled until `isStreaming`, so this
// is the window between the two.
const STREAM_START_MS = parseTimeoutEnv('ASTRABOX_E2E_METER_STREAM_START_MS', 60_000);
// The stop click → the header settled and the composer usable again. Longer
// than a response's own settle: the engine has to be interrupted mid-stream first.
const INTERRUPT_SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_METER_INTERRUPT_SETTLE_MS', 90_000);
// How long a console record page may take to draw. Not env-tunable on purpose:
// a card that is a minute late is a defect, not a deployment.
const RECORD_RENDER_MS = 30_000;

// misc:prompt_input.stop_generating / .stopping — the two names the composer's
// stop control answers to while a response is live. The vendored
// PromptInputSubmit underneath says "Stop"; neither of these can come from it.
const STOP_GENERATING = 'Stop generating';
const STOPPING = 'Stopping';
// manage:sessions.no_parts — the other thing the Response detail panel can say.
const NO_PARTS = 'No parts for this response';
// manage:sessions.response_placeholder — the response selector's accessible name.
const RESPONSE_SELECTOR = 'Select a response';
// chat:process.stopped — the quiet line under a response its user stopped.
const STOPPED_LINE = /^(This response stopped early\.|本次回复提前结束。)$/;

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

/** One console card, addressed by the heading it puts on itself. */
function card(page: Page, heading: string): Locator {
  return page
    .locator('[data-slot="card"]')
    .filter({ has: page.getByRole('heading', { name: heading, exact: true }) });
}

/**
 * Record every accessible name the composer's stop control ever wears.
 *
 * `isStopping` is a state the product added and the page passes through in
 * about a second — the interrupt POST, the forced refresh, and the page
 * moving off the stopped response. Sampling for it after the click is a race, and a
 * spec that loses the race reads as a broken button rather than as a spec that
 * blinked. A MutationObserver installed BEFORE the send cannot miss it: it
 * fires on the attribute write itself, so what is asserted afterwards is the
 * sequence the control actually went through.
 *
 * Installed on the live document, so it does not survive a navigation — read it
 * before leaving the session view.
 */
async function recordStopControlNames(page: Page): Promise<void> {
  await page.evaluate(() => {
    const bag = window as unknown as { __astraStopNames?: string[] };
    const seen: string[] = [];
    bag.__astraStopNames = seen;
    const sample = () => {
      const name = document
        .querySelector('[data-testid="run-composer-stop"]')
        ?.getAttribute('aria-label') || '';
      // Only transitions. The control is re-rendered on every streamed token,
      // and a list of four hundred identical names says nothing.
      if (name && seen[seen.length - 1] !== name) seen.push(name);
    };
    sample();
    new MutationObserver(sample).observe(document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ['aria-label'],
    });
  });
}

function stopControlNames(page: Page): Promise<string[]> {
  return page.evaluate(
    () => (window as unknown as { __astraStopNames?: string[] }).__astraStopNames ?? [],
  );
}

/**
 * What the Response detail panel is showing, in one string.
 *
 * The first part row's sequence number and type, or the empty state when the
 * chosen response recorded none. A part's number is its place in the SESSION's
 * output (`session_output.seq`), so two responses cannot open on the same
 * number — which is what makes "this changed" a statement about the panel
 * refetching rather than about the two responses happening to differ in length.
 *
 * `''` means the panel has not answered yet: it is loading, or it is showing
 * its error note. The caller reads that as "unchanged" rather than as a change,
 * because a spec that accepted the loading state as the new response's answer
 * would pass on a panel that never refetched at all.
 */
async function responsePartsShowing(responseDetail: Locator): Promise<string> {
  const rows = responseDetail.locator('[data-slot="collapsible-trigger"]');
  if ((await rows.count()) > 0) {
    return (await rows.first().innerText()).replace(/\s+/g, ' ').trim();
  }
  if ((await responseDetail.getByText(NO_PARTS).count()) > 0) return NO_PARTS;
  return '';
}

test('a turn closes with its meter, stops when asked, and reads back the same from the console', async ({
  page,
  request,
}) => {
  const uncaught: string[] = [];
  // Attached before the first navigation: an interop crash happens during a
  // render, and a listener added afterwards misses the one that mattered.
  page.on('pageerror', (error) => uncaught.push(error.message));

  const api = new AstraApi(request);
  const runId = Date.now();
  // Carried by the interrupted message's prompt so its row can be found again
  // in the console's digest — which is what makes "the panel opened THIS
  // document" an assertion rather than "a document opened".
  const marker = `E2E-METER-${runId}`;
  const longPrompt = `Count slowly from 1 to 300, one number per line. (${marker})`;

  // ── setup: one conversation, no permission-mode change ────────────────────
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);

  await openSessionView(page, sessionId);
  await expectComposerEnabled(page);

  const runView = page.getByTestId('run-view');
  // Scoped to `run-view`, never a bare `.first()`: the sidebar renders a
  // `status-pill` for EVERY conversation and precedes the route outlet, so a
  // bare first() binds to somebody else's row and fails as a product defect.
  const pill = runView.getByTestId('status-pill').first();
  const settled = (why: string) =>
    expect(pill, why).toHaveAttribute('data-state', 'READY', { timeout: TURN_SETTLE_MS });
  const assertQuietStop = async () => {
    // Waited for, not read once: the header can turn READY from the stream's
    // own end before the session's reader has recorded the response as ended.
    const detail = await api.waitForSession(
      sessionId,
      (session) => session.state === 'READY' && (session.current_responses ?? []).length === 0,
      INTERRUPT_SETTLE_MS,
    );
    expect(detail.last_response?.outcome, 'stopping must end the response cancelled, never failed').toBe('cancelled');
    expect(detail.last_response?.error ?? null, 'a stopped response carries no failure').toBeNull();
    await expect(runView.getByText(/this turn failed|本轮执行失败|上一条消息已送达沙箱，但这一轮执行失败了/i)).toHaveCount(0);
    await expect(
      runView.getByText(STOPPED_LINE),
      'the stop must say it took, quietly, under the stopped response',
    ).toBeVisible({ timeout: RECORD_RENDER_MS });
  };

  // ── 1. before anything has been said ──────────────────────────────────────
  // chat:empty.start_conversation / chat:empty.start_hint. Both, because the
  // hint is the half that tells a first-time reader what to do, and an empty
  // state that lost it still looks finished.
  await expect(
    runView.getByText('Start a conversation'),
    'a conversation with no messages must say so, not show an empty pane',
  ).toBeVisible();
  await expect(
    runView.getByText('Type a message below · Shift+Enter for a new line'),
    'the empty state must keep the line that says what to do next',
  ).toBeVisible();

  // The header names THIS run. RunId strips the dashes before taking eight
  // characters, so a UUID shows its first group.
  await expect(
    runView.getByText(`Run · ${sessionId.replace(/-/g, '').slice(0, 8)}`),
    'the header must identify the run it is showing',
  ).toBeVisible();

  // The Diff tab's empty branch. Its populated branch belongs to the tool
  // journey — this one never asks for a tool, so it owns the half that says
  // "nothing has changed" rather than the half that says what did.
  // `sessionCapabilities.ts` builds this label as a literal, not through i18n:
  // "Diff" while nothing has changed, "Diff (N)" once something has.
  await runView.getByRole('tab', { name: 'Diff', exact: true }).click();
  await expect(
    runView.getByRole('tabpanel').getByText('No file changes yet.'),
    'the Diff panel must state that nothing changed, not render an empty panel',
  ).toBeVisible();
  await runView.getByRole('tab', { name: 'Files', exact: true }).click();

  // ── 2. one short response, and the line that closes it ────────────────────
  await sendPrompt(page, sessionId, 'What is 137 + 42? Reply with only the number.');

  // The closing meter, on the response's own message. Four values nobody acts
  // on, which is why nothing else would report them missing: a response that
  // streamed text but lost its meter is one the reader cannot account for.
  const closing = page.getByTestId('assistant-message').last();
  await expect(
    closing.getByText(/^\d+(\.\d+)?s$/),
    'the response must close on a duration',
  ).toBeVisible({ timeout: TURN_SETTLE_MS });
  await expect(
    closing.getByText(/In: \d/),
    'the response must close on its input token count (chat:result.input_tokens)',
  ).toBeVisible({ timeout: TURN_SETTLE_MS });
  await expect(
    closing.getByText(/Out: \d/),
    'the response must close on its output token count (chat:result.output_tokens)',
  ).toBeVisible();

  // In the same breath, because this is the only place it can be seen: the
  // envelope the suppression gate was widened for must not be on screen. It
  // renders as a card holding raw JSON, so the literal key is the evidence.
  await expect(
    runView.getByText('__sdk_type'),
    "the CLI's SystemMessage envelope must never reach the transcript — RawEventCard suppresses it",
  ).toHaveCount(0);

  await settled('the header must return to Ready once the response closes');

  // ── 3. a second message, so the session owns more than one response ──────
  // ResponseParts renders its selector only above one response (§5: one
  // response is not a choice), and step 6 is about what the selector does.
  const firstResponseId = (await api.getSession(sessionId)).last_response?.response_id;
  await sendPrompt(page, sessionId, 'What is 8 times 9? Reply with only the number.');
  await expect(
    page.getByTestId('user-message'),
    'both prompts must be in the transcript the page dispatched them from',
  ).toHaveCount(2, { timeout: TURN_SETTLE_MS });
  await api.waitForSession(sessionId, (detail) => (
    detail.state === 'READY' && detail.last_response?.status === 'ended'
    && detail.last_response.response_id !== firstResponseId
  ), TURN_SETTLE_MS);
  await settled('the header must return to Ready once the second response closes');

  // ── 4. a third message, its response stopped from the composer ───────────
  await recordStopControlNames(page);
  const delivery = await startPromptDelivery(page, sessionId, longPrompt);

  const stop = page.getByTestId('run-composer-stop');
  // Named immediately, before any streaming: the submit flips the status to
  // 'submitted', and the control is already the stop control there. Both names
  // below come from the wrapper — the vendored button says "Stop".
  await expect(
    stop,
    'the composer must offer a stop control the moment a message is in flight',
  ).toHaveAccessibleName(STOP_GENERATING, { timeout: RECORD_RENDER_MS });
  await expectPromptDelivered(delivery);
  await expect(
    pill,
    'the header must read as running while the response is live',
  ).toHaveAttribute('data-state', 'PROCESSING', { timeout: RECORD_RENDER_MS });

  // Enabled only once the response is actually streaming — there is nothing to
  // interrupt while the input is still being delivered.
  await expect(
    stop,
    'the stop control must become pressable once the response is streaming',
  ).toBeEnabled({ timeout: STREAM_START_MS });
  await stop.click();

  await expect(
    pill,
    'an interrupted response must settle the header, not leave it running',
  ).toHaveAttribute('data-state', 'READY', { timeout: INTERRUPT_SETTLE_MS });
  await expectComposerEnabled(page);
  await assertQuietStop();

  // The three-state assertion. Asserting only "Stop generating" tests the
  // vendored button's generating/not, and would pass with StopGenerationButton's
  // own state deleted; "Stopping" exists nowhere else.
  const names = await stopControlNames(page);
  expect(
    names,
    `the stop control must have been named "${STOP_GENERATING}" while the response ran; saw [${names.join(' → ')}]`,
  ).toContain(STOP_GENERATING);
  expect(
    names,
    `the stop control must have been named "${STOPPING}" while the interrupt was in flight; ` +
      `saw [${names.join(' → ')}]`,
  ).toContain(STOPPING);
  expect(
    names.indexOf(STOPPING),
    `"${STOPPING}" must come after "${STOP_GENERATING}", not instead of it; saw [${names.join(' → ')}]`,
  ).toBeGreaterThan(names.indexOf(STOP_GENERATING));

  // ── 5. the console's record of the same session: Recent events ────────────
  await page.goto(appPath(`/manage/sessions/${sessionId}`));
  const technical = card(page, 'Technical events');
  await expect(
    technical,
    'the session record must carry its technical events card',
  ).toBeVisible({ timeout: RECORD_RENDER_MS });

  // A row is collapsible only when the server sent `raw` for that message; one
  // without it is deliberately a plain div with no control. So the chevron IS
  // the button — locating by role is what picks a row that has one.
  const digestRow = technical.getByRole('button').filter({ hasText: marker }).first();
  await expect(
    digestRow,
    'the digest must show the prompt this spec sent, as a row that opens',
  ).toBeVisible({ timeout: RECORD_RENDER_MS });
  await digestRow.click();

  const opened = technical.locator('pre');
  await expect(opened, 'opening one row must open exactly one document').toHaveCount(1);
  await expect(
    opened,
    'the opened document must be the one that row summarised, not another message',
  ).toContainText(longPrompt);

  // ── 6. the console's record: Response detail, switched response by response
  const responseDetail = card(page, 'Response detail');
  await expect(
    responseDetail,
    'a session with recorded parts must offer the part-by-part card',
  ).toBeVisible({ timeout: RECORD_RENDER_MS });

  const responseSelect = responseDetail.getByRole('combobox', { name: RESPONSE_SELECTOR });
  await expect(
    responseSelect,
    'a session with three responses must offer the response selector (it renders only above one response)',
  ).toBeVisible({ timeout: RECORD_RENDER_MS });

  await expect
    .poll(() => responsePartsShowing(responseDetail), {
      timeout: RECORD_RENDER_MS,
      message: 'the Response detail card must be showing a response before another one is chosen',
    })
    .not.toEqual('');
  const before = await responsePartsShowing(responseDetail);

  // The selector's refetch, identified by the query it adds: the page's own
  // first read of the trace carries no response_id. Caught rather than left to
  // reject, so a failure below reports the missing request instead of an
  // unhandled rejection from a promise nobody is waiting on any more.
  const refetched = page.waitForResponse(
    (response) =>
      response.request().method() === 'GET'
      && response.url().includes(apiPath(`/admin/sessions/${sessionId}/trace`))
      && new URL(response.url()).searchParams.get('response_id') !== null,
    { timeout: RECORD_RENDER_MS },
  ).catch(() => null);

  await responseSelect.click();
  // Picked by aria-selected rather than by label. The option label is the
  // response's start time to the MINUTE plus its outcome, and three responses a
  // few seconds apart can render two identical labels — clicking one by its
  // text could re-pick the response already showing and assert nothing.
  const options = page.getByRole('option');
  await expect(options.first(), 'the selector must open its menu').toBeVisible();
  const optionCount = await options.count();
  expect(optionCount, 'the selector must offer every response the session recorded').toBeGreaterThan(1);
  let picked = false;
  for (let index = 0; index < optionCount; index += 1) {
    const option = options.nth(index);
    if ((await option.getAttribute('aria-selected')) === 'true') continue;
    await option.click();
    picked = true;
    break;
  }
  expect(picked, 'the selector must offer a response other than the one already showing').toBe(true);

  expect(
    (await refetched)?.status(),
    'choosing a response must ask the server for THAT response (GET .../trace?response_id=…)',
  ).toBe(200);

  await expect
    .poll(
      async () => {
        const showing = await responsePartsShowing(responseDetail);
        // A panel that has not answered yet reads as unchanged, so the poll
        // waits it out instead of accepting the loading state as the answer.
        return showing === '' ? before : showing;
      },
      {
        timeout: RECORD_RENDER_MS,
        message:
          'choosing another response must replace the part list. It still opens on ' +
          `"${before}" — the previous response's parts under the new response's name is the ` +
          'exact failure ResponseParts clears its state to avoid. (A panel stuck on its error ' +
          'note reaches this message too; check the trace request.)',
      },
    )
    .not.toEqual(before);

  // ── 7. back to the conversation, rebuilt from the recorded history ────────
  // A second full mount of the transcript, from history rather than from the
  // live stream. This is where the suppressed-envelope regression showed
  // itself last time — the conversation changed on reload — and it is the
  // cheapest place to catch an import shape that only a built bundle refuses.
  await openSessionView(page, sessionId);
  await expect(
    runView.getByText(`Run · ${sessionId.replace(/-/g, '').slice(0, 8)}`),
    'the reloaded header must still name this run',
  ).toBeVisible({ timeout: RECORD_RENDER_MS });
  await expect(
    runView.getByText(/In: \d/).first(),
    'the meter must survive the reload — it is rebuilt from the recorded result part',
  ).toBeVisible({ timeout: RECORD_RENDER_MS });
  await expect(
    runView.getByText('__sdk_type'),
    'the reloaded transcript must not differ from the live one by a suppressed card',
  ).toHaveCount(0);
  await expectComposerEnabled(page);
  await assertQuietStop();

  expect(
    uncaught,
    `uncaught exception while reading a conversation and its record:\n${uncaught.join('\n')}`,
  ).toEqual([]);
});
