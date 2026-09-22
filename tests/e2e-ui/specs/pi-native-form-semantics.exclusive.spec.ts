/** Supplier choices and text are data, not platform-authored answer semantics. */
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { PiNativeFormScene } from '../fixtures/piNativeFormSemantics';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

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
  scene = new PiNativeFormScene(new AstraApi(request), page, [{ method: 'confirm' }, { method: 'confirm' }]);
  await scene.start(sessions);
  for (const [index, value] of [true, false].entries()) {
    const pending = await scene.question(index);
    const panel = page.getByTestId('pending-interaction-panel');
    await expect(panel.getByRole('radio'), 'a boolean dialog must not acquire a custom-text answer').toHaveCount(2);
    await expect(panel.getByRole('textbox')).toHaveCount(0);
    const label = value ? 'Yes' : 'No';
    const option = panel.getByRole('radio', { name: new RegExp(`^${value ? 1 : 2}\\.\\s+${label}$`) });
    await option.click();
    await expect(option).toBeChecked();
    await scene.submit(pending, 'option_label', label);
    await scene.result(index, value);
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
