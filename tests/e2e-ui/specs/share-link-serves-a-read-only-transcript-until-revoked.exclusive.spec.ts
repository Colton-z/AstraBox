/**
 * E2E: a share link opens a read-only conversation until its owner revokes it.
 *
 * The owner generates the link from the Share dialog with file downloads enabled.
 * A separate browser context must open the token without a session, render the
 * transcript and shared file list, and expose no composer. An invalid token must
 * fail. After revocation, the same valid token must return SHARE_NOT_FOUND and the
 * viewer page must report that the share cannot be opened.
 *
 * The token is the viewer credential; ownership checks still protect link creation
 * and revocation.
 */
import { test, expect, type Browser } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { PlatformApi } from '../fixtures/platformApi';
import { appPath, parseTimeoutEnv } from '../fixtures/env';

const RUN_ID = new Date().toISOString().replace(/[:.]/g, '-');
// One conversation reaching READY plus one model turn; sized like the sibling
// lifecycle specs rather than the 240s suite default.
const REPLY_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_SHARE_REPLY_TIMEOUT_MS', 240_000);

// Placed in the box before the link is minted, so the viewer's file sidebar has
// a known file to list rather than whatever the runtime happens to seed.
const SHARED_FILE_NAME = 'shared-artifact.txt';
const SHARED_FILE_TEXT = 'visible to the share link';

// Teardown that changes state runs on the passing path only, and hook
// registration order is the old `finally` order: afterEach hooks run in the
// order they are registered, so the session goes before the agent it ran on.
// A kept session pointing at a deleted agent is half a scene.
let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('a share link serves a read-only transcript until it is revoked', async ({ page, request, browser }) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);

  let sessionId = '';
  let viewer: Awaited<ReturnType<Browser['newContext']>> | null = null;
  try {
    // ── Arrange: an isolated agent and one conversation with real content ───
    const base = await api.defaultAgent();
    const environmentName = String(base.environment_name || '').trim();
    expect(environmentName, 'seeded default agent must name an environment').not.toEqual('');
    let model = String(base.model || '').trim();
    if (!model || model.includes('*')) {
      const models = await api.listEnvironmentModels(environmentName);
      model = models.find((m) => m && !m.includes('*')) || 'deepseek-chat';
    }

    const agent = await api.createAgent({
      name: `__e2e_share_${RUN_ID}`,
      model,
      environment_name: environmentName,
    });
    agentId = String(agent.agent_id || '');
    const created = await api.startConversation(agentId);
    sessionId = created.session_id;
    sessions.push(sessionId);
    test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
    const ready = await api.waitForSessionReady(sessionId);
    test.info().annotations.push({ type: 'e2e_sandbox_id', description: String(ready.sandbox_id || '') });

    // A file for the viewer's sidebar to list. Written through the session
    // terminal so it is a real file on the sandbox disk, not a fixture the API
    // could satisfy from memory.
    await api.runTerminalCommand(sessionId, `printf '%s' ${JSON.stringify(SHARED_FILE_TEXT)} > ${SHARED_FILE_NAME}`);

    // ── A real exchange to share. An empty transcript would let a broken
    //    viewer page pass by rendering nothing. ──────────────────────────────
    const prompt = `Reply with one short sentence. Marker ${RUN_ID}.`;
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
    await page.getByTestId('composer-prompt').fill(prompt);
    await page.getByTestId('composer-submit').click();
    await expect(page.getByTestId('user-message').last()).toContainText(RUN_ID, { timeout: 30_000 });
    await expect
      .poll(() => page.getByTestId('assistant-message').count(), { timeout: REPLY_BUDGET_MS })
      .toBeGreaterThan(0);
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first())
      .toHaveAttribute('data-pulse', 'false', { timeout: 60_000 });

    // ── Mint the link from the dialog, with downloads allowed ───────────────
    await page.getByRole('button', { name: 'Share', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 15_000 });
    // The download toggle is the switch next to its label; ticking it is what
    // makes the viewer's file sidebar reachable at all.
    await page.getByRole('switch').first().click();
    const shareResponsePromise = page.waitForResponse(
      (response) => (
        response.url().includes(`/sessions/${sessionId}/share`)
        && response.request().method() === 'POST'
      ),
      { timeout: 60_000 },
    );
    await page.getByRole('button', { name: 'Generate share link' }).click();
    const shareResponse = await shareResponsePromise;
    expect(shareResponse.status(), 'the dialog should mint the link through POST /share').toBe(200);

    // The token under test is the one the DIALOG is showing, read off its field.
    const linkField = page.getByRole('dialog').locator('input').first();
    await expect(linkField, 'the dialog should present the minted link').toBeVisible({ timeout: 30_000 });
    const shareUrl = String(await linkField.inputValue()).trim();
    expect(shareUrl, 'the dialog must show a share URL').toContain('/share/');
    const token = shareUrl.split('/share/').pop() || '';
    expect(token, 'the share URL must carry a token').not.toEqual('');

    const config = await platform.getShare(sessionId);
    expect(config.enabled, 'share should read enabled after generating').toBe(true);
    expect(config.token, 'the stored token should be the one the dialog showed').toBe(token);
    expect(config.allow_download, 'the download toggle should have reached the server').toBe(true);

    // ── The viewer: a different browser context, the link and nothing else ──
    viewer = await browser.newContext();
    const viewerPage = await viewer.newPage();
    await viewerPage.goto(appPath(`/share/${token}`));

    await expect(
      viewerPage.getByTestId('user-message').last(),
      'the shared page should render the conversation the link points at',
    ).toContainText(RUN_ID, { timeout: 60_000 });
    await expect(
      viewerPage.getByTestId('assistant-message').last(),
      'the shared page should render the reply too',
    ).toBeVisible({ timeout: 60_000 });

    await expect(
      viewerPage.getByText('Read-only', { exact: true }),
      'the shared page should tell the viewer it is read-only',
    ).toBeVisible();

    // Read-only: the shared page mounts no composer. This is the assertion that
    // would catch a share link that quietly became a write grant.
    await expect(
      viewerPage.getByTestId('composer-prompt'),
      'a shared conversation must expose no composer',
    ).toHaveCount(0);
    await expect(
      viewerPage.getByTestId('composer-submit'),
      'a shared conversation must expose no send button',
    ).toHaveCount(0);

    // allow_download was ticked, so the file this spec wrote is offered.
    await expect(
      viewerPage.getByText(SHARED_FILE_NAME, { exact: true }),
      'the viewer should be offered the sandbox file the share allows downloading',
    ).toBeVisible({ timeout: 60_000 });

    // The same token over the API — the contract the page is a rendering of.
    const sharedFiles = await platform.sharedFiles(token);
    expect(
      (sharedFiles.entries || []).map((entry) => entry.name),
      'the shared file listing should carry the file',
    ).toContain(SHARED_FILE_NAME);
    const sharedMessages = await platform.sharedMessages(token);
    expect(
      (sharedMessages.messages || []).length,
      'the shared transcript should not be empty',
    ).toBeGreaterThan(0);

    // ── A token that was never minted opens nothing ─────────────────────────
    const forged = await platform.refusal('GET', `/share/${'0'.repeat(token.length)}`);
    expect(forged.status, 'a forged token must not resolve a session').toBe(404);
    expect(forged.code, 'a forged token should be refused as a missing share').toBe('SHARE_NOT_FOUND');

    // ── Revoke closes it, for the page and for the API ──────────────────────
    const revoked = await platform.revokeShare(sessionId);
    expect(revoked.enabled, 'revoke should disable the share').toBe(false);

    const afterRevoke = await platform.refusal('GET', `/share/${token}`);
    expect(afterRevoke.status, 'a revoked token must stop resolving').toBe(404);
    expect(afterRevoke.code, 'a revoked token should be refused as a missing share').toBe('SHARE_NOT_FOUND');

    await viewerPage.reload();
    await expect(
      viewerPage.getByText("Can't open this share"),
      'the viewer page should say the link cannot be opened after revocation',
    ).toBeVisible({ timeout: 60_000 });
    await expect(
      viewerPage.getByTestId('user-message'),
      'a revoked link must render none of the transcript',
    ).toHaveCount(0);
  } finally {
    // The session and the agent are not released here. `trackSessions()` and
    // `onPassOnly()` decide in afterEach hooks, where the test's real status is
    // known — see that fixture on why a `finally` cannot tell it is unwinding
    // from a failure, and on why the unit is the whole block.
    if (viewer) await viewer.close().catch(() => {});
  }
});
