import type { Page, Response } from '@playwright/test';
import { expect } from '@playwright/test';

import { apiPath, appPath, parseTimeoutEnv } from './env';

const composerTimeoutMs = () => parseTimeoutEnv(
  'ASTRABOX_E2E_SESSION_COMPOSER_TIMEOUT_MS',
  180_000,
);
const turnInputTimeoutMs = () => parseTimeoutEnv(
  'ASTRABOX_E2E_TURN_INPUT_RESPONSE_TIMEOUT_MS',
  60_000,
);

export interface PendingPromptDelivery {
  sessionId: string;
  clientMessageId: string;
  response: Promise<Response>;
}

export async function openSessionView(page: Page, sessionId: string): Promise<void> {
  await page.goto(appPath(`/sessions/${sessionId}`), { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });
  expect(new URL(page.url()).pathname.endsWith(`/sessions/${sessionId}`)).toBe(true);
}

export async function expectComposerEnabled(page: Page): Promise<void> {
  await expect(page.getByTestId('composer-prompt')).toBeEnabled({
    timeout: composerTimeoutMs(),
  });
}

export async function closePageAfterAssertions(page: Page): Promise<void> {
  await page.goto('about:blank', {
    waitUntil: 'domcontentloaded',
    timeout: 5_000,
  }).catch(() => undefined);
  await page.close({ runBeforeUnload: false }).catch(() => undefined);
}

export async function startPromptDelivery(
  page: Page,
  sessionId: string,
  prompt: string,
): Promise<PendingPromptDelivery> {
  const composer = page.getByTestId('composer-prompt');
  await expect(composer).toBeEnabled({ timeout: composerTimeoutMs() });
  await composer.fill(prompt);
  const response = page.waitForResponse((candidate) => (
    candidate.request().method() === 'POST'
    && candidate.url().includes(apiPath(`/sessions/${sessionId}/turn-inputs`))
  ), { timeout: turnInputTimeoutMs() });
  const request = page.waitForRequest((candidate) => (
    candidate.method() === 'POST'
    && candidate.url().includes(apiPath(`/sessions/${sessionId}/turn-inputs`))
  ), { timeout: turnInputTimeoutMs() });
  await page.getByTestId('composer-submit').click();
  const body = (await request).postDataJSON() as Record<string, unknown>;
  const clientMessageId = String(body.client_message_id || '').trim();
  expect(clientMessageId, `turn-inputs client_message_id for ${sessionId}`).not.toEqual('');
  return { sessionId, clientMessageId, response };
}

export async function expectPromptDelivered(
  pending: PendingPromptDelivery,
): Promise<Response> {
  const response = await pending.response;
  expect(response.status(), `turn-inputs response status for ${pending.sessionId}`).toBe(200);
  return response;
}

export async function sendPrompt(
  page: Page,
  sessionId: string,
  prompt: string,
): Promise<Response> {
  return expectPromptDelivered(await startPromptDelivery(page, sessionId, prompt));
}
