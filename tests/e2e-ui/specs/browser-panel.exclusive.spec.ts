import { test, expect, type Page, type TestInfo } from '@playwright/test';
import { AstraApi } from '../fixtures/astraApi';
import { appPath, apiPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { requireSandboxHandle, sandboxExec } from '../fixtures/sandboxOps';

const sessions = trackSessions();
const agents: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const id of agents.splice(0)) await api.deleteAgent(id);
});

async function controlBrowser(api: AstraApi, page: Page, sessionId: string, label: string, testInfo: TestInfo): Promise<string> {
  const ready = await api.waitForSessionReady(sessionId);
  const detail = await api.adminSessionDetail(sessionId);
  const identity = detail.runtime_identity as Record<string, string>;
  expect(identity.sandbox_tenancy).toBe('conversation');
  const box = await requireSandboxHandle(api, String(ready.sandbox_id));
  const marker = `browser-${label}-${Date.now()}`;
  const filename = `${identity.file_root_source_dir}/${marker}.html`;
  const html = `<title>loaded-${marker}</title><button style="position:fixed;inset:0" onclick="document.title='clicked';this.remove();document.querySelector('input').focus()">${marker}</button><input oninput="document.title=this.value">`;
  sandboxExec(box, `python3 - <<'PY'\nfrom pathlib import Path\nPath(${JSON.stringify(filename)}).write_text(${JSON.stringify(html)})\nPY`);
  const nativePages = (): { title: string; url: string }[] => JSON.parse(
    sandboxExec(box, 'curl --fail --silent http://127.0.0.1:9222/json/list'),
  );
  await page.goto(appPath(`/sessions/${sessionId}`));
  await page.getByRole('tab', { name: 'Browser', exact: true }).click();
  const frame = page.frameLocator('iframe[title="Browser"]');
  const canvas = frame.locator('#noVNC_container canvas');
  await expect(canvas).toBeVisible();
  await expect.poll(() => canvas.evaluate((node: HTMLCanvasElement) => node.width)).toBeGreaterThan(100);
  await canvas.click();
  await page.keyboard.press('Control+l', { delay: 100 });
  await page.keyboard.type(`file://${filename}`, { delay: 25 });
  await page.keyboard.press('Enter', { delay: 100 });
  // The native Chrome target is independent of AstraBox's iframe and transport.
  await expect.poll(() => nativePages().some((target) => target.url === `file://${filename}` && target.title === `loaded-${marker}`)).toBe(true);
  await canvas.click();
  await expect.poll(() => nativePages().some((target) => target.title === 'clicked')).toBe(true);
  await page.keyboard.type(marker, { delay: 25 });
  await expect.poll(() => nativePages().some((target) => target.title === marker && target.url === `file://${filename}`)).toBe(true);
  await testInfo.attach(`remote-browser-${label}`, { body: await canvas.screenshot(), contentType: 'image/png' });
  const firstUrl = (await page.locator('iframe[title="Browser"]').getAttribute('src'))!;
  const refreshed = page.waitForResponse((response) => (
    new URL(response.url()).pathname === apiPath(`/sessions/${sessionId}/browser`)
    && response.request().method() === 'POST'
  ));
  await page.getByRole('button', { name: 'Reconnect', exact: true }).click();
  expect((await refreshed).status()).toBe(200);
  await expect(canvas).toBeVisible();
  await canvas.click();
  await page.keyboard.press('Control+l', { delay: 100 });
  await page.keyboard.type(`file://${filename}#reconnected`, { delay: 25 });
  await page.keyboard.press('Enter', { delay: 100 });
  await expect.poll(() => nativePages().some((target) => target.title === marker && target.url === `file://${filename}#reconnected`)).toBe(true);
  return firstUrl;
}

test('Browser controls an exclusive sandbox and refuses shared access', async ({ page, request, playwright }, testInfo) => {
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(`__e2e_browser_${Date.now()}`);
  agents.push(agent.agent_id);
  const environment = String(process.env.ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT || '').trim();
  expect(environment, 'the deployment must provide a conversation prewarm Environment').not.toBe('');
  expect(await api.listEnvironmentModels(environment)).toContain(agent.model);
  const warm = await api.createAgent({
    name: `__e2e_browser_warm_${Date.now()}`, model: agent.model,
    environment_name: environment, prepared_capacity: 1,
  });
  agents.push(warm.agent_id);
  const conversation = await api.startConversation(agent.agent_id);
  sessions.push(conversation.session_id);
  const firstUrl = await controlBrowser(api, page, conversation.session_id, 'cold', testInfo);

  type Prepared = { ready: boolean; prepared_count: number; sandbox_id: string };
  const preparedPath = apiPath(`/agents/${warm.agent_id}/prepared-runtime`);
  const readPrepared = () => page.waitForResponse((response) => (
    new URL(response.url()).pathname === preparedPath && response.request().method() === 'GET'
  ));
  const read = readPrepared();
  await page.goto(appPath(`/manage/agents/${warm.agent_id}`));
  const status = await read;
  expect(status.status()).toBe(200);
  let prepared = (await status.json()).data as Prepared;
  await expect.poll(async () => {
    if (!prepared.ready || prepared.prepared_count < 1) {
      const next = readPrepared();
      await page.getByTestId('agent-prewarm-status').getByRole('button', { name: 'Refresh', exact: true }).click();
      const response = await next;
      expect(response.status()).toBe(200);
      prepared = (await response.json()).data as Prepared;
    }
    return prepared.ready && prepared.prepared_count > 0;
  }, { timeout: 60_000, intervals: [1_000, 2_000] }).toBe(true);
  await expect(page.getByTestId('prewarm-count')).toContainText(`Available: ${prepared.prepared_count}`);
  const preparedBox = await requireSandboxHandle(api, prepared.sandbox_id);
  // The desktop must be running before a conversation claims the prepared box.
  sandboxExec(preparedBox, "python3 -c 'import socket; socket.create_connection((\"127.0.0.1\", 6080), timeout=3).close()'");
  const version = JSON.parse(sandboxExec(preparedBox, 'curl --fail --silent http://127.0.0.1:9222/json/version'));
  expect(version.Browser).toMatch(/Chrome/);
  const claimed = await api.startConversation(warm.agent_id);
  sessions.push(claimed.session_id);
  const ready = await api.waitForSessionReady(claimed.session_id);
  expect(ready.sandbox_id).toBe(prepared.sandbox_id);
  await controlBrowser(api, page, claimed.session_id, 'prewarmed', testInfo);

  const anonymous = await playwright.request.newContext({
    storageState: { cookies: [], origins: [] },
    extraHTTPHeaders: {},
  });
  try {
    expect((await anonymous.storageState()).cookies).toHaveLength(0);
    const origin = new URL(page.url()).origin;
    const denied = await anonymous.post(`${origin}${apiPath(`/sessions/${conversation.session_id}/browser`)}`);
    expect([401, 403]).toContain(denied.status());
    const management = new URL(firstUrl);
    management.pathname = management.pathname.replace(/vnc\.html$/, 'v1/shell/exec');
    management.search = '';
    expect((await anonymous.get(management.toString())).ok()).toBe(false);
    const forged = new URL(firstUrl);
    forged.pathname = forged.pathname.replace(/\/b\/([0-9]+)\/[A-Za-z0-9_-]+\//, '/b/$1/invalid/');
    expect((await anonymous.get(forged.toString())).status()).toBe(403);
  } finally {
    await anonymous.dispose();
  }
  const sharedAgent = await api.defaultAgent();
  const shared = await api.startConversation(sharedAgent.agent_id);
  sessions.push(shared.session_id);
  await api.waitForSessionReady(shared.session_id);
  const refused = await request.post(apiPath(`/sessions/${shared.session_id}/browser`));
  expect(refused.status()).toBe(409);
  expect(JSON.stringify(await refused.json())).toContain('BROWSER_TENANCY_UNSUPPORTED');
  await page.goto(appPath(`/sessions/${shared.session_id}`));
  await page.getByRole('tab', { name: 'Browser', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('only in exclusive sandboxes');
  await expect(page.locator('iframe[title="Browser"]')).toHaveCount(0);
});
