/**
 * Product E2E: every writable Files-panel action completes against the session
 * sandbox, and the downloaded bytes match the browser upload exactly.
 *
 * The backend implementation for these routes uses only OpenSandbox execd's
 * Filesystem API. This browser journey therefore covers mkdir -> upload -> list
 * -> download -> rename -> delete through the same UI and API a user operates.
 */
import fs from 'node:fs';

import { test, expect, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { PlatformApi } from '../fixtures/platformApi';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';

const PANEL_READY_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_FILE_PANEL_TIMEOUT_MS',
  60_000,
);

function treeItem(page: Page, name: string) {
  return page
    .getByText(name, { exact: true })
    .locator('xpath=ancestor::*[@role="treeitem"][1]');
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

test('Files panel creates, uploads, downloads, renames, and deletes', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = Date.now().toString(36);
  const folderName = `e2e-files-${runId}`;
  const originalName = `before-${runId}.bin`;
  const renamedName = `after-${runId}.bin`;
  const body = Buffer.from(`\x00AstraBox\r\n${runId}\n`, 'utf8');

  let sessionId = '';
  try {
    const base = await api.defaultAgent();
    const environmentName = String(base.environment_name || '').trim();
    expect(environmentName, 'seeded default agent must name an environment').not.toEqual('');
    let model = String(base.model || '').trim();
    if (!model || model.includes('*')) {
      const models = await api.listEnvironmentModels(environmentName);
      model = models.find((candidate) => candidate && !candidate.includes('*')) || 'deepseek-chat';
    }

    const agent = await api.createAgent({
      name: `__e2e_file_journey_${runId}`,
      model,
      environment_name: environmentName,
    });
    agentId = String(agent.agent_id || '');
    const created = await api.startConversation(agentId);
    sessionId = created.session_id;
    sessions.push(sessionId);
    test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
    const ready = await api.waitForSessionReady(sessionId);
    test.info().annotations.push({
      type: 'e2e_sandbox_id',
      description: String(ready.sandbox_id || ''),
    });

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 30_000 });
    await page.getByRole('tab', { name: 'Files' }).click();
    await expect(page.getByText('Current directory', { exact: true })).toBeVisible({
      timeout: PANEL_READY_TIMEOUT_MS,
    });
    const uploadButton = page.getByRole('button', { name: 'Upload', exact: true });
    await expect(uploadButton).toBeEnabled({ timeout: PANEL_READY_TIMEOUT_MS });

    // Create a folder through the visible toolbar and dialog.
    await page.getByRole('button', { name: 'New', exact: true }).click();
    const createDialog = page.getByRole('dialog', { name: 'New folder' });
    await createDialog.getByPlaceholder('Folder name').fill(folderName);
    const mkdirResponsePromise = page.waitForResponse(
      (response) =>
        response.url().includes(apiPath(`/sessions/${sessionId}/files/mkdir`))
        && response.request().method() === 'POST',
    );
    await createDialog.getByRole('button', { name: 'Confirm', exact: true }).click();
    expect((await mkdirResponsePromise).status()).toBe(200);
    await expect(page.getByText(folderName, { exact: true })).toBeVisible();

    // Select and expand it so the upload targets this directory and the new
    // file is rendered as a child in the tree.
    await page.getByText(folderName, { exact: true }).click();
    const folderItem = treeItem(page, folderName);
    const folderToggle = folderItem.getByRole('button').first();
    if ((await folderToggle.getAttribute('aria-expanded')) !== 'true') {
      await folderToggle.click();
    }

    const uploadResponsePromise = page.waitForResponse(
      (response) =>
        response.url().includes(apiPath(`/sessions/${sessionId}/files/upload`))
        && response.request().method() === 'POST',
      { timeout: 120_000 },
    );
    const chooserPromise = page.waitForEvent('filechooser');
    await uploadButton.click();
    const chooser = await chooserPromise;
    await chooser.setFiles({
      name: originalName,
      mimeType: 'application/octet-stream',
      buffer: body,
    });
    const uploadResponse = await uploadResponsePromise;
    expect(uploadResponse.status()).toBe(200);
    await expect(page.getByText(originalName, { exact: true })).toBeVisible({
      timeout: 60_000,
    });

    const folderListing = await platform.listFiles(sessionId, folderName);
    const uploaded = (folderListing.entries || []).find(
      (entry) => entry.name === originalName,
    );
    expect(uploaded?.kind).toBe('file');
    expect(uploaded?.size).toBe(body.length);

    // Download through the row action, not a direct fixture API call.
    const originalItem = treeItem(page, originalName);
    const downloadPromise = page.waitForEvent('download');
    await originalItem.getByTitle('Download').click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe(originalName);
    const downloadedPath = test.info().outputPath(originalName);
    await download.saveAs(downloadedPath);
    expect(fs.readFileSync(downloadedPath).equals(body)).toBe(true);

    // Rename from the row menu and verify both the screen and durable listing.
    await originalItem.getByTitle('More actions').click();
    await page.getByRole('menuitem', { name: 'Rename', exact: true }).click();
    const renameDialog = page.getByRole('dialog', { name: 'Rename' });
    await renameDialog.getByPlaceholder('New name').fill(renamedName);
    await renameDialog.getByRole('button', { name: 'Confirm', exact: true }).click();
    await expect(page.getByText(originalName, { exact: true })).toHaveCount(0);
    await expect(page.getByText(renamedName, { exact: true })).toBeVisible();
    const renamedListing = await platform.listFiles(sessionId, folderName);
    expect((renamedListing.entries || []).map((entry) => entry.name)).toEqual([
      renamedName,
    ]);

    // Delete the file, then its empty folder, through their confirmation dialogs.
    const renamedItem = treeItem(page, renamedName);
    await renamedItem.getByTitle('More actions').click();
    await page.getByRole('menuitem', { name: 'Delete', exact: true }).click();
    let deleteDialog = page.getByRole('alertdialog', { name: 'Delete' });
    await deleteDialog.getByRole('button', { name: 'Delete', exact: true }).click();
    await expect(page.getByText(renamedName, { exact: true })).toHaveCount(0);

    let folderDeleteAttempts = 0;
    await page.route(
      `**${apiPath(`/sessions/${sessionId}/files/delete`)}`,
      async (route) => {
        folderDeleteAttempts += 1;
        if (folderDeleteAttempts === 1) {
          const committed = await route.fetch();
          expect(committed.status(), 'the server must commit before the response is lost').toBe(200);
          await route.abort('connectionreset');
          return;
        }
        await route.continue();
      },
    );
    await folderItem.getByTitle('More actions').click();
    await page.getByRole('menuitem', { name: 'Delete', exact: true }).click();
    deleteDialog = page.getByRole('alertdialog', { name: 'Delete' });
    await deleteDialog.getByRole('button', { name: 'Delete', exact: true }).click();
    await expect(page.getByText(folderName, { exact: true })).toHaveCount(0);
    expect(folderDeleteAttempts, 'a lost delete response must replay the convergent command').toBe(2);

    const rootListing = await platform.listFiles(sessionId);
    expect((rootListing.entries || []).map((entry) => entry.name)).not.toContain(folderName);
  } finally {
    // The session and the agent are not released here. `trackSessions()` and
    // `onPassOnly()` decide in afterEach hooks, where the test's real status is
    // known — see that fixture on why a `finally` cannot tell it is unwinding
    // from a failure, and on why the unit is the whole block.
    await page.close().catch(() => {});
  }
});
