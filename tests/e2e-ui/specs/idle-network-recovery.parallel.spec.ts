import { expect, test } from '@playwright/test';
import { randomUUID } from 'node:crypto';

import { AstraApi } from '../fixtures/astraApi';
import { appendChildFrames } from '../fixtures/childMessageReplay';
import { apiPath, appPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled, openSessionView } from '../fixtures/sessionPage';

const sessions = trackSessions();
test.describe.configure({ mode: 'parallel' });

  test('idle network loss preserves the page and reconnects without a reload', async ({ page, context }) => {
    let sessionId = '';
    const isStreamPath = (path: string) => path.startsWith(apiPath('/sessions/')) && path.endsWith('/ai-stream');
    const railPath = apiPath('/sessions');
    let disconnected = true;
    let railLoaded = false;
    let failedStream = 0;
    let failedRail = 0;
    let restoredStream = 0;
    let restoredRail = 0;
    const uncaught: string[] = [];
    page.on('pageerror', error => uncaught.push(error.message));
    page.on('response', response => {
      if (!response.ok()) return;
      const path = new URL(response.url()).pathname;
      if (path === railPath) railLoaded = true;
      if (disconnected) return;
      if (isStreamPath(path)) restoredStream += 1;
      if (path === railPath) restoredRail += 1;
    });
    // A broken route can precede the browser's offline notification. Exercise
    // failed real reads while navigator.onLine still says true, then go offline.
    await page.route('**/api/v1/sessions**', async route => {
      const req = route.request();
      const path = new URL(req.url()).pathname;
      if (req.method() === 'GET' && disconnected && isStreamPath(path)) {
        failedStream += 1;
        await route.fulfill({ status: 502, contentType: 'text/plain', body: 'Gateway unavailable' });
      } else if (req.method() === 'GET' && disconnected && path === railPath && railLoaded) {
        failedRail += 1;
        await route.fulfill({ status: 503, contentType: 'text/plain', body: 'Service unavailable' });
      } else {
        await route.continue();
      }
    });
    try {
      await page.goto(appPath('/agents'));
      await expect.poll(() => railLoaded).toBe(true);
      await page.evaluate(() => {
        const state = window as typeof window & { networkErrorFlashes: string[]; networkObserver: MutationObserver };
        state.networkErrorFlashes = [];
        state.networkObserver = new MutationObserver(() => {
          const matches = document.body.innerText.match(/NETWORK_ERROR|network error|HTTP_50[234]|Request failed|请求失败|backend is temporarily unavailable|后端服务暂时不可用/g);
          if (matches) state.networkErrorFlashes.push(...matches);
        });
        state.networkObserver.observe(document.body, { subtree: true, childList: true, characterData: true });
      });
      // Install the fault before opening the subscription: a brief offline
      // toggle does not reliably close an existing Chromium SSE connection.
      const agent = page.getByTestId('agent-option').and(page.locator('[data-agent-name="Investment Research"]'));
      await agent.getByRole('button', { name: /^(Start conversation|开始对话)$/ }).click();
      await page.waitForURL(/\/sessions\/[^/]+$/);
      await expectComposerEnabled(page);
      sessionId = new URL(page.url()).pathname.split('/').pop()!;
      test.info().annotations.push({ type: 'retained-session', description: page.url() });
      const composer = page.getByTestId('composer-prompt');
      const draft = `UNSENT_${Date.now()}`;
      await composer.fill(draft);
      await expect.poll(() => failedStream, { timeout: 15_000 }).toBeGreaterThan(0);
      await expect.poll(() => failedRail, { timeout: 25_000 }).toBeGreaterThan(0);
      await context.setOffline(true);
      // Exceeds the existing delayed stream notice, not merely its quiet grace.
      await page.waitForTimeout(11_000);
      await expect(composer).toHaveValue(draft);
      await expect(page.getByTestId('run-view')).toBeVisible();
      await expect(page.getByText(/Request failed|请求失败|backend is temporarily unavailable|后端服务暂时不可用|NETWORK_ERROR/)).toHaveCount(0);
      await expect(page.getByTestId('run-view').getByTestId('status-pill').first())
        .toHaveAttribute('data-pulse', 'false');

      disconnected = false;
      await context.setOffline(false);
      await expect.poll(() => restoredRail, { timeout: 20_000 }).toBeGreaterThan(0);
      await expect.poll(() => restoredStream, { timeout: 20_000 }).toBeGreaterThan(0);
      await expect(composer).toHaveValue(draft);
      const flashes = await page.evaluate(() => {
        const state = window as typeof window & { networkErrorFlashes: string[]; networkObserver: MutationObserver };
        state.networkObserver.disconnect();
        return state.networkErrorFlashes;
      });
      expect(flashes, 'including errors that appeared briefly and disappeared').toEqual([]);
      // A failed explicit read remains actionable; it is not a background poll.
      let failedManualRead = 0;
      await page.route(`**${apiPath(`/sessions/${sessionId}/files/list`)}`, async route => {
        failedManualRead += 1;
        await route.fulfill({ status: 504, contentType: 'text/plain', body: 'Gateway timed out' });
      });
      await page.getByRole('button', { name: 'Refresh', exact: true }).click();
      await expect.poll(() => failedManualRead).toBeGreaterThan(0);
      await expect(page.getByText(/HTTP_504/)).toBeVisible();
      expect(uncaught).toEqual([]);
    } finally {
      disconnected = false;
      await context.setOffline(false);
      await test.info().attach('network-recovery-evidence', {
        body: JSON.stringify({ failedStream, failedRail, restoredStream, restoredRail, uncaught }),
        contentType: 'application/json',
      });
    }
  });

test('background child reads retain their transcript and catch up after reconnect', async ({ page, context, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent('Claude Code');
  const sessionId = (await api.startConversation(agent.agent_id)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  const engineRef = `network-${randomUUID()}`;
  const before = `BEFORE_DISCONNECT_${randomUUID()}`;
  const after = `AFTER_RECONNECT_${randomUUID()}`;
  const frame = (data: Record<string, unknown>) => ({
    type: 'data-subagent', id: `subagent:${randomUUID()}`, transient: true,
    data: { engineKind: 'claude_code', engineRef, ...data },
  });
  // Seed the existing durable journal, not a mocked HTTP response. This case
  // tests the platform's reader; it does not ask an engine to generate work.
  appendChildFrames(sessionId, [
    frame({ kind: 'lifecycle', event: 'opened', engineEvent: 'task_started', operations: [] }),
    frame({ kind: 'message', role: 'assistant', content: [{ type: 'text', text: before }] }),
  ]);
  const children = (await api.listChildRuns(sessionId)).child_runs;
  expect(children).toHaveLength(1);
  expect(children[0]!.active).toBe(true);
  const childId = children[0]!.child_run_id;
  await openSessionView(page, sessionId);
  await page.getByRole('tab', { name: /^Agents/ }).click();
  const row = page.locator(`[data-child-run-id="${childId}"]`);
  await row.click();
  const column = page.getByTestId('subagent-transcript-column');
  await expect(column).toContainText(before);
  let blocked = true;
  let failedCatalog = 0;
  let failedTranscript = 0;
  await page.route(`**${apiPath(`/sessions/${sessionId}/child-runs`)}**`, async route => {
    if (!blocked) return route.continue();
    if (new URL(route.request().url()).pathname.endsWith('/child-runs')) failedCatalog += 1;
    else failedTranscript += 1;
    await route.abort('connectionreset');
  });
  try {
    await expect.poll(() => failedCatalog, { timeout: 15_000 }).toBeGreaterThan(0);
    await expect.poll(() => failedTranscript, { timeout: 15_000 }).toBeGreaterThan(0);
    await page.waitForTimeout(1500);
    await expect(row).toBeVisible();
    await expect(column).toContainText(before);
    await expect(page.getByText(/NETWORK_ERROR/)).toHaveCount(0);
    await context.setOffline(true);
    appendChildFrames(sessionId, [
      frame({ kind: 'message', role: 'assistant', content: [{ type: 'text', text: after }] }),
      frame({ kind: 'lifecycle', event: 'closed', engineEvent: 'task_notification', engineStatus: 'completed', operations: [] }),
    ]);
    blocked = false;
    await context.setOffline(false);
    await expect(column).toContainText(after);
    await expect(column).toContainText(before);
    await expect(row).toContainText('completed');
  } finally {
    blocked = false;
    await context.setOffline(false);
    await test.info().attach('background-child-network-evidence', {
      body: JSON.stringify({ failedCatalog, failedTranscript, childId }), contentType: 'application/json',
    });
  }
});
