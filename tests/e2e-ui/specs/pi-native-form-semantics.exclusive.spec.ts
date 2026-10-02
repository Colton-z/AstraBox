/** Supplier choices and text are data, not platform-authored answer semantics. */
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { framesForTurn, sessionEvents, waitForTurnTerminalProof } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { nativeText, object } from '../fixtures/piChildFailure';
import { PiNativeFormScene } from '../fixtures/piNativeFormSemantics';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { sendPrompt } from '../fixtures/sessionPage';
import { aiStreamBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
let scene: PiNativeFormScene | undefined;
test.beforeEach(() => { scene = undefined; });
onPassOnly(async ({ request }) => {
  if (scene?.agentId) await new AstraApi(request).deleteAgent(scene.agentId);
});
test.afterEach(async ({}, info) => {
  if (scene && ['failed', 'timedOut', 'interrupted'].includes(String(info.status))) {
    await scene.attachFailure(info);
  }
});

test('Pi answers a new message after stopping a native dialog', async ({ page, request }, info) => {
  const api = new AstraApi(request);
  const current = new PiNativeFormScene(api, page, [{ method: 'confirm' }]);
  scene = current;
  await current.start(sessions);
  const pending = await current.question(0);
  const panel = page.getByTestId('pending-interaction-panel');
  const interrupt = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === apiPath(`/sessions/${current.sessionId}/interrupt`));
  await panel.getByRole('button', { name: /^(Stop generating|Stopping|停止生成|停止中)$/i }).click();
  expect((await interrupt).ok()).toBe(true);
  const stopped = await api.waitForSession(current.sessionId, (value) => value.state === 'READY'
    && !value.current_turn_id && !value.pending_interaction, 30_000);
  expect(stopped.last_turn_id).toBe(pending.turn_id);
  expect(stopped.last_turn_status).toBe('COMPLETED');
  await waitForTurnTerminalProof(current.sessionId, String(pending.turn_id), 'COMPLETED', 5_000);
  expect(framesForTurn(String(pending.turn_id)).map((row) => object(row.payload))
    .filter((frame) => frame.type === 'finish').map((frame) => frame.finishReason))
    .toEqual(['tool-calls', 'stop']);
  const stoppedHistory = await api.getMessages(current.sessionId);
  const stoppedMessages = stoppedHistory.messages.filter((message) => message.turn_id === pending.turn_id);
  expect(stoppedMessages.flatMap((message) => message.blocks ?? [])
    .filter((block) => block.type === 'result').map((block) => block.finish_reason)).toEqual(['cancelled']);
  // Pi dismisses confirm with false. The real handler must return and save
  // that value rather than remain blocked after the platform closes the turn.
  await current.result(0, false);
  const stoppedWire = current.nativeOutput();
  expect(stoppedWire.filter((row) => row.type === 'response' && row.command === 'abort')).toEqual([
    expect.objectContaining({ success: true }),
  ]);
  expect(stoppedWire.filter((row) => row.type === 'response' && row.command === 'prompt'))
    .toHaveLength(2);
  await expect(panel).toHaveCount(0);
  const answer = `AFTER_STOP_${current.marker}`;
  const prompt = `Reply exactly ${answer}. Do not use tools.`;
  await sendPrompt(page, current.sessionId, prompt);
  const completed = await api.waitForSession(current.sessionId, (value) => value.state === 'READY'
    && !value.current_turn_id && Boolean(value.last_turn_id && value.last_turn_id !== pending.turn_id), 30_000);
  expect(completed.last_turn_status).toBe('COMPLETED');
  expect(completed.last_error).toBeFalsy();
  const wire = current.nativeOutput();
  const ownInput = wire.filter((row) => row.type === 'message_start'
    && object(row.message).role === 'user' && nativeText(object(row.message).content) === prompt);
  expect(ownInput).toHaveLength(1);
  const replies = wire.slice(wire.indexOf(ownInput[0]!)).filter((row) => row.type === 'message_end'
    && object(row.message).role === 'assistant');
  expect(replies.map((row) => nativeText(object(row.message).content))).toEqual([answer]);
  const history = await api.getMessages(current.sessionId);
  const visible = history.messages.filter((message) => message.role === 'assistant' && messageText(message) === answer);
  expect(visible).toHaveLength(1);
  expect(visible[0]!.turn_id).toBe(completed.last_turn_id);
  expect(sessionEvents(current.sessionId).filter((event) => event.event_type === 'turn.failed')).toEqual([]);
  await expect(page.getByTestId('assistant-text').filter({ hasText: answer })).toBeVisible();
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(panel).toHaveCount(0);
  await expect(page.getByTestId('assistant-text').filter({ hasText: answer })).toBeVisible();
  await expect(page.getByText(/^(This response stopped early\.|本次回复提前结束。)$/)).toBeVisible();
  await info.attach('pi-stopped-dialog-next-input', {
    body: JSON.stringify({ sessionId: current.sessionId, pending, stopped, stoppedHistory, stoppedWire, completed, wire, history }),
    contentType: 'application/json',
  });
});

test('Pi select preserves Other and Another option as literal choices', async ({ page, request }) => {
  const options = ['Other', 'Another option'];
  scene = new PiNativeFormScene(new AstraApi(request), page, [
    { method: 'select', options }, { method: 'select', options },
  ]);
  await scene.start(sessions);
  for (const [index, label] of options.entries()) {
    const pending = await scene.question(index);
    const panel = page.getByTestId('pending-interaction-panel');
    const option = panel.getByRole('radio', { name: new RegExp(`^${index + 1}\\.\\s+${label}$`) });
    await option.click();
    await expect(option, 'select the literal supplier choice instead of switching to a custom answer').toBeChecked();
    await expect(panel.getByRole('textbox')).toHaveCount(0);
    await scene.submit(pending, 'option_label', label);
    await scene.result(index, label);
  }
  await scene.finish();
});

test('Pi confirm offers only native boolean answers', async ({ page, request }) => {
  const api = new AstraApi(request);
  scene = new PiNativeFormScene(api, page, [{ method: 'confirm' }, { method: 'confirm' }]);
  await scene.start(sessions);
  for (const [index, value] of [true, false].entries()) {
    let releaseReplay!: () => void;
    const replayGate = new Promise<void>((resolve) => { releaseReplay = resolve; });
    const streamPath = apiPath(`/sessions/${scene.sessionId}/ai-stream`);
    const streamUrl = (url: URL) => url.pathname === streamPath;
    let heldRequests = 0;
    if (index === 1) {
      // Delay only the real subscription request, so the current questionnaire
      // is read before older interaction frames can replay into the page.
      await page.route(streamUrl, async (route) => {
        heldRequests += 1;
        await replayGate;
        await route.continue();
      });
    }
    try {
      const pending = await scene.question(index);
      const panel = page.getByTestId('pending-interaction-panel');
      await expect(panel.getByRole('radio'), 'a boolean dialog must not acquire a custom-text answer').toHaveCount(2);
      await expect(panel.getByRole('textbox')).toHaveCount(0);
      const label = value ? 'Yes' : 'No';
      const option = panel.getByRole('radio', { name: new RegExp(`^${value ? 1 : 2}\\.\\s+${label}$`) });
      await option.click();
      await expect(option).toBeChecked();
      if (index === 1) {
        await expect.poll(() => heldRequests).toBeGreaterThan(0);
        const checkpoint = (await api.getHistoryBlocks(scene.sessionId)).session_frame_seq;
        expect(Number.isInteger(checkpoint)).toBe(true);
        await page.evaluate(({ interactionId, label }) => {
          const state = { lost: false, observations: 0, stop: () => {} };
          const inspect = () => {
            const choices = [...document.querySelectorAll<HTMLInputElement>(
              '[data-testid="pending-interaction-panel"] input[type="radio"]',
            )];
            state.observations += 1;
            if (!choices.some((input) => input.name === interactionId && input.value === label && input.checked)) {
              state.lost = true;
            }
          };
          const observer = new MutationObserver(inspect);
          observer.observe(document.body, { subtree: true, childList: true, attributes: true });
          state.stop = () => { inspect(); observer.disconnect(); };
          inspect();
          (window as unknown as { __piAnswerStability: typeof state }).__piAnswerStability = state;
        }, { interactionId: pending.interaction_id, label });
        releaseReplay();
        await expect.poll(async () => {
          const bodies = await aiStreamBodies(page);
          return bodies.some((body) => body.text.slice(0, body.text.lastIndexOf('\n') + 1).split('\n').some((line) => {
            if (!line.startsWith('data:') || line.slice(5).trim() === '[DONE]') return false;
            const frame = JSON.parse(line.slice(5));
            return frame.type === 'data-resume-cursor' && frame.data.frameSeq >= Number(checkpoint);
          }));
        }, { message: 'the real replay must reach the questionnaire snapshot before checking the choice' }).toBe(true);
        // The mirrored bytes can arrive ahead of React committing their updates.
        // Keep observing the draft through those commits, including transient loss.
        await page.waitForTimeout(1_000);
        const stability = await page.evaluate(() => {
          const state = (window as unknown as {
            __piAnswerStability: { lost: boolean; observations: number; stop: () => void };
          }).__piAnswerStability;
          state.stop();
          return { lost: state.lost, observations: state.observations };
        });
        await test.info().attach('pi-answer-replay-stability', {
          body: JSON.stringify({ checkpoint, heldRequests, ...stability }), contentType: 'application/json',
        });
        expect(stability.lost, 'replaying older dialogs must never discard the current selected answer').toBe(false);
        await expect(option).toBeChecked();
      }
      await scene.submit(pending, 'option_label', label);
      await scene.result(index, value);
    } finally {
      releaseReplay();
      if (index === 1) await page.unroute(streamUrl);
    }
  }
  await scene.finish();
});

test('Pi input preserves whitespace and empty text without cancellation', async ({ page, request }) => {
  scene = new PiNativeFormScene(new AstraApi(request), page, [{ method: 'input' }, { method: 'input' }]);
  await scene.start(sessions);
  for (const [index, value] of ['  exact input value  ', ''].entries()) {
    const pending = await scene.question(index);
    const field = page.getByTestId('pending-interaction-panel').getByRole('textbox');
    await field.fill(value);
    await expect(field).toHaveValue(value);
    await scene.submit(pending, 'free_text', value);
    await scene.result(index, value);
  }
  await scene.finish();
});

test('Pi editor preserves whitespace and empty text without cancellation', async ({ page, request }) => {
  scene = new PiNativeFormScene(new AstraApi(request), page, [{ method: 'editor' }, { method: 'editor' }]);
  await scene.start(sessions);
  for (const [index, value] of ['\n  indented first line\n\tsecond line  \n', ''].entries()) {
    const pending = await scene.question(index);
    const field = page.getByTestId('pending-interaction-panel').getByRole('textbox');
    await field.fill(value);
    await expect(field).toHaveValue(value);
    await scene.submit(pending, 'free_text', value);
    await scene.result(index, value);
  }
  await scene.finish();
});
