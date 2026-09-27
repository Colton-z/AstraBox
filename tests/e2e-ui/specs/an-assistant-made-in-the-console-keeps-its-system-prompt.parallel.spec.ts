/**
 * E2E: an operator gives an Assistant its system prompt in the console, and
 * changes it later from the Assistant's record page.
 *
 * The journey is two forms. New assistant: a name, the Assistant Environment,
 * a system prompt, Create. Then, on the record page Create lands on, the system
 * prompt is edited and its section saved. Each write must land on the server
 * exactly as typed, read back through the API rather than from the page that
 * sent it.
 *
 * WHAT IT GUARDS. Hermes, the bundled Assistant program, declares no
 * permission modes, and the server refuses any value for a program without
 * them. A form that sent one would fail Create and every section's Save for a
 * Hermes Assistant. A form may offer the field only for a program that
 * declares modes, which the Environment listing carries (`permission_modes`).
 * This spec reads that declaration and requires the field to be absent exactly
 * when it is empty, so it holds under whichever Assistant program the
 * deployment runs.
 *
 * WHAT IT DOES NOT COVER. Whether the program then answers as the prompt says
 * is the model's side, proven by tests/e2e/test_assistant_system_prompt.py; no
 * workspace is started here and no token is spent, which is why this runs in
 * the parallel lane.
 *
 * Deployment fixtures this spec reads and never creates, failing loudly when
 * absent: an enabled Environment running an Assistant program
 * (`api.assistantEnvironmentName()`) and `ASTRABOX_E2E_ASSISTANT_MODEL`.
 */
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { onPassOnly } from '../fixtures/sessionCleanup';

test.use({ locale: 'en-US' });

let assistantId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

test('an Assistant created in the console keeps its system prompt, and its record page saves an edit to it', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  const environmentName = await api.assistantEnvironmentName();
  expect(environmentName, 'an Assistant Environment must exist').not.toEqual('');

  const environments = await request.get(apiPath('/admin/environments'));
  expect(environments.ok(), 'the Environment listing must answer').toBeTruthy();
  const listed = ((await environments.json()).data as Array<Record<string, unknown>>).find(
    (environment) => environment.name === environmentName,
  );
  expect(listed, `the listing must include ${environmentName}`).toBeTruthy();
  const declaredModes = (listed?.permission_modes as string[] | undefined) ?? [];

  const name = `__e2e_assistant_system_prompt_${runId}`;
  const firstPrompt = `You are Quill ${runId}. Answer in one sentence.`;
  const secondPrompt = `You are Wren ${runId}. Answer in two sentences.`;

  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* the en-US navigator locale still applies */
    }
  });

  // ── New assistant ─────────────────────────────────────────────────────────
  await page.goto(appPath('/manage/assistants/new'));
  const nameField = page.getByRole('textbox', { name: 'Name', exact: true });
  await expect(nameField, 'the create form must render').toBeVisible();
  await nameField.fill(name);
  await page
    .getByRole('combobox', { name: 'Runtime environment', exact: true })
    .selectOption(environmentName);

  const permission = page.getByRole('combobox', { name: 'Default permission', exact: true });
  await expect(
    permission,
    `the form offers a permission mode only for a program that declares some (${environmentName} `
      + `declares ${JSON.stringify(declaredModes)})`,
  ).toHaveCount(declaredModes.length > 0 ? 1 : 0);

  await page.getByRole('textbox', { name: 'System prompt', exact: true }).fill(firstPrompt);
  const created = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' && new URL(response.url()).pathname === apiPath('/assistants'),
  );
  await page.getByRole('button', { name: 'Create', exact: true }).click();
  const createResponse = await created;
  expect(
    createResponse.status(),
    `Create must be accepted: ${(await createResponse.text()).slice(0, 300)}`,
  ).toBe(200);
  await expect(page).toHaveURL(/\/manage\/assistants\/asst_[0-9a-f]+$/);
  assistantId = new URL(page.url()).pathname.split('/').pop() ?? '';
  test.info().annotations.push({ type: 'e2e_assistant_id', description: assistantId });

  const stored = await api.getAssistant(assistantId);
  expect(stored.system, 'the server must hold the system prompt the form sent').toEqual(firstPrompt);

  // ── The record page: the prompt is shown, edited and saved ───────────────
  const systemField = page.getByRole('textbox', { name: 'System prompt', exact: true });
  await expect(systemField, 'the record page shows the stored system prompt').toHaveValue(
    firstPrompt,
  );
  await expect(
    page.getByRole('combobox', { name: 'Default permission', exact: true }),
    'the record page offers a permission mode only when the Assistant has one',
  ).toHaveCount(stored.permission_mode_default ? 1 : 0);

  await systemField.fill(secondPrompt);
  const saved = page.waitForResponse(
    (response) =>
      response.request().method() === 'PATCH'
      && new URL(response.url()).pathname === apiPath(`/assistants/${assistantId}`),
  );
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  const saveResponse = await saved;
  expect(
    saveResponse.status(),
    `Save must be accepted: ${(await saveResponse.text()).slice(0, 300)}`,
  ).toBe(200);
  expect(
    (await api.getAssistant(assistantId)).system,
    'the server must hold the edited system prompt',
  ).toEqual(secondPrompt);
  await expect(systemField).toHaveValue(secondPrompt);
});
