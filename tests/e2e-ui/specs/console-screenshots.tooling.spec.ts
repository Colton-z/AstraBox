/**
 * Capture the console images embedded by the README and documentation site.
 *
 * The spec creates its own presentable Agent and real tool-using conversation.
 * Every capture waits for the element that communicates the image's purpose, so
 * an empty frame or spinner fails instead of becoming documentation. Output is
 * written to assets/screenshots/.
 *
 * Run `make screenshots` with ASTRABOX_E2E_BASE_URL and a credentialed
 * ASTRABOX_E2E_SCREENSHOT_MODEL.
 */
import path from 'node:path';

import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { appPath, repoRoot } from '../fixtures/env';

const SHOT_DIR = path.join(repoRoot, 'assets', 'screenshots');

// A turn that provisions a sandbox and calls a tool runs well past the 240 s
// suite default.

// 1440x900 is the width the live-turn config pins and the one the console
// layout gate verifies, so it is the width the console is known to look right
// at. README images are downscaled from it.
const VIEWPORT = { width: 1440, height: 900 };

// Writes a file, reads it back, and says what it saw: tool cards whose visual
// shape is the point of the conversation screenshot. The READY record is the
// runtime contract for the workspace; asking a model to infer "the current
// directory" is not.
function demoPrompt(filePath: string): string {
  return (
    `Use the Write tool to create the exact file ${filePath} containing the single line ` +
    '"Hello from AstraBox", then use the Read tool on that same exact path and tell me what it says.'
  );
}

// Teardown that changes state runs on the passing path only, and hook
// registration order is the old `finally` order: afterEach hooks run in the
// order they are registered, so the session goes before the agent it ran on.
// A kept session pointing at a deleted agent is half a scene.
let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('capture the console screenshots used by the README and docs', async ({ page, request }) => {
  const api = new AstraApi(request);

  // The environment's catalogue lists what is ROUTABLE, not what has a working
  // credential behind it, and its order is not a ranking. Picking from it
  // gambles: a model group whose key is rejected answers 401 ten times over
  // ~180 s and produces a screenshot of an authentication error. So the model
  // is named explicitly, or inherited from the seeded agent that demonstrably
  // runs on this deployment — and if neither is available this fails now with
  // the variable to set, rather than after three minutes of retries.
  const base = await api.defaultAgent();
  const environmentName = String(base.environment_name || '').trim();
  expect(environmentName, 'seeded default agent must name an environment').not.toEqual('');
  const model = (process.env.ASTRABOX_E2E_SCREENSHOT_MODEL || String(base.model || '')).trim();
  expect(
    model,
    'no model to capture with: set ASTRABOX_E2E_SCREENSHOT_MODEL to one this ' +
      'deployment holds a credential for (the seeded agent names none)',
  ).not.toEqual('');

  await page.setViewportSize(VIEWPORT);

  let sessionId = '';
  try {
    const agent = await api.createAgent({
      name: 'Docs Demo',
      model,
      environment_name: environmentName,
    });
    agentId = String(agent.agent_id || '');
    expect(agentId, 'created agent must have an id').not.toEqual('');

    const created = await api.startConversation(agentId);
    sessionId = created.session_id;
    sessions.push(sessionId);
    const ready = await api.waitForSessionReady(sessionId);
    const workspace = ready.terminal_cwd?.trim() ?? '';
    expect(workspace, 'a READY session must publish its workspace as terminal_cwd').toMatch(/^\/.+/);
    const demoFile = path.posix.join(workspace, 'hello.txt');

    // ── 1. The conversation: a streamed reply with tool cards ──────────────
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible();

    const composer = page.locator('textarea');
    await composer.fill(demoPrompt(demoFile));
    await page.locator('button[type="submit"]').first().click();

    await expect(page.getByTestId('user-message').last()).toContainText('hello.txt', {
      timeout: 30_000,
    });

    // The turn has to have really run, and "an assistant message appeared" does
    // not prove that — a turn that fails authentication also renders one, full
    // of API-error text. The file is the model-independent proof: it exists
    // only if the tool call actually executed in the box.
    await expect
      .poll(() => api.downloadFileText(sessionId, 'hello.txt').catch(() => ''), {
        timeout: 300_000,
        message: 'the agent never wrote hello.txt — the turn did not do the work',
      })
      .toContain('Hello from AstraBox');

    // And the console has to have caught up: a reply on screen, header settled.
    // Capturing before this shows a half-streamed message under a spinner.
    await expect(page.getByTestId('assistant-message').last()).toBeVisible({ timeout: 60_000 });
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute('data-pulse', 'false', {
      timeout: 60_000,
    });

    // Found by a person reading a transcript, not by a spec: the vendor emits
    // thinking blocks with no visible text (one of three on this very prompt)
    // and the console rendered each as an empty collapsed "Reasoning" card.
    // The renderer suppresses settled-empty reasoning parts now; this pins it
    // on a real conversation, where the empty blocks actually occur.
    const emptyReasoningCards = await page
      .locator('[data-testid="reasoning-part"][data-chars="0"]')
      .count();
    expect(
      emptyReasoningCards,
      'a settled transcript must not render an empty Reasoning card',
    ).toBe(0);

    await page.screenshot({ path: path.join(SHOT_DIR, 'conversation.png'), fullPage: false });

    // ── 2. The agents list ────────────────────────────────────────────────
    await page.goto(appPath('/agents'));
    await expect(page.getByText('Docs Demo').first()).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ path: path.join(SHOT_DIR, 'agents.png'), fullPage: false });

    // ── 3. The operator surface: sessions the deployment is running ───────
    await page.goto(appPath('/manage/sessions'));
    await expect(page.getByTestId('console-table').or(page.getByTestId('sessions-table'))).toBeVisible({
      timeout: 30_000,
    });
    await page.screenshot({ path: path.join(SHOT_DIR, 'console-sessions.png'), fullPage: false });
  } finally {
    // The session and the agent are not released here. `trackSessions()` and
    // `onPassOnly()` decide in afterEach hooks, where the test's real status is
    // known — see that fixture on why a `finally` cannot tell it is unwinding
    // from a failure, and on why the unit is the whole block.
  }
});
