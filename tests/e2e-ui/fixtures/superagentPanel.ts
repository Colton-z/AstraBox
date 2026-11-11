/** The dsh-superagent bundle's panel inside an Assistant's page, and its API as that page calls it. */
import { expect, type Frame, type Page } from '@playwright/test';

import type { AstraApi } from './astraApi';
import { postgresRows, sqlLiteral } from './dbOracle';
import { appPath } from './env';
import { killSandbox, requireSandboxHandle, waitForSandboxStopped } from './sandboxOps';

export type Ticket = { id: string; title: string; directory?: string; threads: Array<{ url?: string }> };

/** The conversations of `agentId` that A2A started, oldest first: how an Assistant's Tickets reach its owner's Agents. */
export function a2aConversations(agentId: string): Array<{ session_id: string; user_id: string }> {
  return postgresRows<{ session_id: string; user_id: string }>(
    `SELECT session_id, user_id FROM sessions WHERE agent_id = ${sqlLiteral(agentId)} AND source_type = 'a2a' `
      + 'AND NOT deleted ORDER BY created_at',
  );
}

/** The harness page inside the console's frame, once it has an address. */
export async function panelFrame(page: Page): Promise<Frame> {
  // The frame element is shown before it has navigated to the panel's address.
  const panel = () => page.frames().find((candidate) => candidate !== page.mainFrame() && candidate.url().startsWith('http'));
  await expect.poll(() => Boolean(panel()), { message: 'the Assistant page shows its panel', timeout: 300_000 }).toBe(true);
  await expect(page.locator('iframe')).toBeVisible();
  return panel()!;
}

/** Call the bundle's panel API as its page does: same origin, the page's login, its request marker. */
export async function panelApi<T>(frame: Frame, method: string, path: string, body?: unknown): Promise<T> {
  return frame.evaluate(async ({ method, path, body }) => {
    const response = await fetch(`/orchestrator/api/${path}`, {
      method,
      credentials: 'same-origin',
      headers: { 'x-orchestrator-request': '1', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`panel ${method} ${path} returned HTTP ${response.status}: ${await response.text()}`);
    return response.json();
  }, { method, path, body }) as Promise<T>;
}

/**
 * Open the Assistant and require the bundle's panel: its page body, every harness
 * request answered, and the harness's live connection open and not refused.
 */
export async function openOnPanel(page: Page, assistantId: string): Promise<Frame> {
  const refusals: Array<Promise<string | undefined>> = [];
  const sockets: string[] = [];
  page.on('response', (response) => {
    const url = new URL(response.url());
    if (response.status() < 400 || !/^\/(api|orchestrator\/api)\//.test(url.pathname)) return;
    refusals.push((async () => {
      // While the workspace restarts, as when an installed Bundle restarts the harness, the console asks for the
      // panel's address, hears that the workspace is not ready, and reads the Assistant again until it is.
      const body = await response.json().catch(() => undefined) as { code?: string } | undefined;
      if (response.status() === 409 && url.pathname.endsWith('/panel') && body?.code === 'ASSISTANT_WORKSPACE_NOT_READY') return undefined;
      return `${response.status()} ${url.pathname}`;
    })());
  });
  page.on('websocket', (socket) => {
    const path = new URL(socket.url()).pathname;
    sockets.push(`open ${path}`);
    socket.on('socketerror', (error) => sockets.push(`error ${path} ${error}`));
  });
  await page.goto(appPath(`/assistants/${encodeURIComponent(assistantId)}`));
  const frame = await panelFrame(page);
  // The bundle's main tab panel exists only on its own page: nothing was clicked to get there.
  await expect(frame.locator('#dt-page')).toBeVisible({ timeout: 60_000 });
  await expect.poll(() => sockets.some((entry) => entry === 'open /api/remote.mux'), { timeout: 30_000 }).toBe(true);
  await page.waitForTimeout(5_000);
  expect(sockets.filter((entry) => entry.startsWith('error')), 'the harness connection is refused').toEqual([]);
  const refused = (await Promise.all(refusals)).filter((entry) => entry !== undefined);
  expect(refused, 'the harness refuses the page through its address').toEqual([]);
  return frame;
}

/**
 * Delete the Assistant's box behind the platform's back, with nobody looking, and
 * wait until the Assistant is READY on a new one by itself.
 */
export async function loseTheBoxAndRecover(api: AstraApi, assistantId: string): Promise<void> {
  const lost = String((await api.getAssistant(assistantId)).current_sandbox_id || '');
  expect(lost).not.toEqual('');
  const handle = await requireSandboxHandle(api, lost);
  killSandbox(handle);
  await waitForSandboxStopped(handle, 60_000);
  const workspace = () => postgresRows<{ state: string; current_sandbox_id: string | null }>(
    `SELECT state, current_sandbox_id FROM assistant_workspaces WHERE assistant_id = ${sqlLiteral(assistantId)}`,
  )[0] ?? { state: '', current_sandbox_id: null };
  await expect.poll(() => {
    const row = workspace();
    return row.state === 'READY' && Boolean(row.current_sandbox_id) && row.current_sandbox_id !== lost;
  }, { message: 'the Assistant is READY on a new box', timeout: 300_000, intervals: [2_000] }).toBe(true);
}
