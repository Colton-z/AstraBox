/** One anonymous share keeps readable history through file loading, failures and retry. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { PlatformApi } from '../fixtures/platformApi';
import { apiPath, appPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('shared history stays readable while files wait, fail and recover', async ({ request, browser }) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const id = randomUUID();
  agentId = (await api.createColdTestAgent(`__e2e_share_files_${id}`)).agent_id;
  const { session_id: sessionId } = await api.startConversation(agentId);
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);

  const prompts = Array.from({ length: 26 }, (_, index) => `Shared history ${id}-${String(index).padStart(3, '0')}.`);
  const toolId = `shared_tool_${id}`;
  const toolOutput = `SHARED_TOOL_OUTPUT_${id}`;
  const messages = prompts.flatMap((text, index) => [
    {
      source_id: `${id}-u${index}`, role: 'user',
      created_at: new Date(Date.UTC(2026, 0, 5, 9, index, 0)).toISOString(),
      blocks: [{ type: 'text', text }],
    },
    {
      source_id: `${id}-a${index}`, role: 'assistant', completion: 'complete',
      created_at: new Date(Date.UTC(2026, 0, 5, 9, index, 20)).toISOString(),
      blocks: [
        ...(index === 25 ? [
          { type: 'tool_use', id: toolId, name: 'Bash', input: { command: 'printf synthetic' } },
          { type: 'tool_result', tool_use_id: toolId, content: toolOutput, is_error: false },
        ] : []),
        { type: 'text', text: `Shared reply ${id}-${index}.` },
      ],
    },
  ]);
  expect(await api.data('POST', `/admin/sessions/${sessionId}/history-import`, { messages }))
    .toMatchObject({ imported: 52 });
  const share = await platform.createShare(sessionId, { allowDownload: true, expiresInSeconds: 86_400 });
  const token = share.token;
  if (!token) throw new Error('The real share must issue a viewer token');
  const empty = await platform.sharedFiles(token);
  expect(empty.entries, 'the new unconfigured workspace must provide a real empty-directory control').toEqual([]);

  const viewer = await browser.newContext({ storageState: { cookies: [], origins: [] }, extraHTTPHeaders: {}, locale: 'en-US' });
  const page = await viewer.newPage();
  let releaseFiles!: () => void;
  const pendingFiles = new Promise<void>((resolve) => { releaseFiles = resolve; });
  let stage: 'held503' | '409' | 'real' = 'held503';
  let fileRequests = 0;
  const olderRequests: string[] = [];
  const filesPath = apiPath(`/share/${token}/files/list`);
  const historyPath = apiPath(`/share/${token}/history-blocks`);
  page.on('request', (outgoing) => {
    const url = new URL(outgoing.url());
    if (url.pathname === filesPath) fileRequests += 1;
    if (url.pathname === historyPath && url.searchParams.has('before')) {
      olderRequests.push(url.searchParams.get('before')!);
    }
  });
  try {
    await page.addInitScript(() => window.localStorage.setItem('astrabox-lang', 'en'));
    await page.goto(appPath(`/share/${token}`));
    const newest = page.getByTestId('user-message').filter({ hasText: prompts[25] });
    const oldest = page.getByTestId('user-message').filter({ hasText: prompts[0] });
    const files = page.getByRole('complementary', { name: 'Files', exact: true });
    await expect(newest).toBeVisible();
    await expect(files).toContainText('No files available to download.');
    expect(olderRequests).toEqual([]);

    const fileName = `shared-${id}.txt`;
    await api.runTerminalCommand(sessionId, `printf '%s' 'synthetic shared file' > '${fileName}'`);
    await page.route((url) => url.pathname === filesPath, async (intercept) => {
      if (stage === 'real') {
        await intercept.continue();
        return;
      }
      const status = stage === 'held503' ? 503 : 409;
      if (stage === 'held503') {
        const real = await intercept.fetch();
        expect(real.status()).toBe(200);
        expect((await real.json()).data.entries.map((entry: { name: string }) => entry.name)).toContain(fileName);
        await pendingFiles;
      }
      // Only the file response fails; metadata, history and detail reads are real.
      await intercept.fulfill({ status, json: {
        code: `SHARE_FILES_E2E_${status}`, message: `Shared files unavailable (${status})`,
      } });
    });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(newest, 'the first history page must render before the held file response').toBeVisible();
    await expect(files.getByRole('status')).toHaveText('Loading shared files…');
    releaseFiles();
    await expect(files.getByRole('alert')).toContainText('Shared files unavailable (503)');
    await expect(newest).toBeVisible();
    expect(olderRequests).toEqual([]);

    // Real lazy detail reads still deliver the imported tool work after file failure.
    const turn = page.locator(`[data-message-id="import:${id}-a25"]`);
    await turn.getByTestId('assistant-turn-process-trigger').click();
    const group = turn.getByTestId('assistant-process-trigger');
    await expect(group).toBeVisible();
    if (await group.getAttribute('aria-expanded') !== 'true') await group.click();
    const tool = turn.locator(`[data-tool-call-id="${toolId}"]`);
    await expect(tool).toBeVisible();
    await tool.getByRole('button').first().click();
    await expect(tool.getByText(toolOutput, { exact: true })).toBeVisible();

    const scroller = page.getByTestId('session-conversation');
    await scroller.hover();
    await page.mouse.wheel(0, -100_000);
    await expect.poll(() => olderRequests.length).toBe(1);
    await page.mouse.wheel(0, -100_000);
    await expect(oldest).toBeInViewport();

    stage = '409';
    await files.getByRole('button', { name: 'Retry', exact: true }).click();
    await expect(files.getByRole('alert')).toContainText('Shared files unavailable (409)');
    await expect(oldest).toBeInViewport();
    stage = 'real';
    await files.getByRole('button', { name: 'Retry', exact: true }).click();
    const link = files.getByRole('link', { name: fileName });
    await expect(link).toBeVisible();
    await expect(files.getByRole('alert')).toHaveCount(0);
    const download = await viewer.request.get(new URL((await link.getAttribute('href'))!, page.url()).href);
    expect(download.status()).toBe(200);
    expect(await download.text()).toBe('synthetic shared file');
    await expect(oldest).toBeInViewport();

    const disabled = await platform.createShare(sessionId, { allowDownload: false });
    expect(disabled.token).toBe(token);
    const requestsBeforeDisabled = fileRequests;
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(newest).toBeVisible();
    await expect(files).toHaveCount(0);
    expect(fileRequests, 'downloads disabled means no file request').toBe(requestsBeforeDisabled);
    await expect(page.getByTestId('composer-prompt')).toHaveCount(0);
  } finally {
    releaseFiles();
    await viewer.close();
  }
});
