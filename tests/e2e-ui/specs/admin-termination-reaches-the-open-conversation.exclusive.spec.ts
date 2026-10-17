/** An idle reader hears administrator termination without refreshing or waking compute. */
import { execFileSync } from 'node:child_process';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';
import { requireSandboxHandle, sandboxRunning, waitForSandboxStopped } from '../fixtures/sandboxOps';
import { mirrorSseBodies, sessionStreamBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => { if (agentId) await new AstraApi(request).deleteAgent(agentId); });

test('administrator termination reaches an idle open header and preserves readable history', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(`__e2e_admin_notice_${Date.now()}`);
  agentId = agent.agent_id;
  const { session_id: sid } = await api.startConversation(agentId);
  sessions.push(sid);
  const ready = await api.waitForSessionReady(sid);
  const sandbox = await requireSandboxHandle(api, String(ready.sandbox_id));
  expect(sandboxRunning(sandbox)).toBe(true);
  if (sandbox.runtime !== 'kubernetes') throw new Error('The read-only allocation oracle requires the selected k3s testbed');
  const userText = `Synthetic history before termination ${sid}`;
  const replyText = `Saved reply ${sid}`;
  expect(await api.data('POST', `/admin/sessions/${sid}/history-import`, { messages: [
    { source_id: 'user', role: 'user', created_at: '2026-01-01T00:00:00Z', blocks: [{ type: 'text', text: userText }] },
    { source_id: 'assistant', role: 'assistant', completion: 'complete', created_at: '2026-01-01T00:00:01Z',
      blocks: [{ type: 'thinking', thinking: 'Inspecting the synthetic note.' }, { type: 'text', text: replyText }] },
  ] })).toMatchObject({ imported: 2, unchanged: 0 });
  const before = await api.getMessages(sid);
  await page.context().addInitScript(() => window.localStorage.setItem('astrabox-lang', 'en'));
  await mirrorSseBodies(page);
  await openSessionView(page, sid);
  const header = page.getByTestId('run-view').locator('header');
  await expect(header.getByText('Ready', { exact: true })).toBeVisible();
  await expect(page.getByText(userText, { exact: true })).toBeVisible();
  await expect(page.getByTestId('assistant-message')).toContainText(replyText);
  await expect.poll(async () => (await sessionStreamBodies(page)).some((body) => body.text.includes('data-transcript-changed')))
    .toBe(true);
  const noticeCount = async () => (await sessionStreamBodies(page))
    .reduce((count, body) => count + (body.text.match(/data-session-changed/g) ?? []).length, 0);
  const noticesBefore = await noticeCount();
  const admin = await page.context().newPage();
  try {
    await admin.goto(appPath(`/manage/sessions/${sid}`));
    await admin.getByRole('button', { name: 'Kill', exact: true }).click();
    const killed = admin.waitForResponse((response) => response.request().method() === 'POST'
      && new URL(response.url()).pathname === apiPath(`/admin/sessions/${sid}/kill`));
    await admin.getByRole('button', { name: 'End Session', exact: true }).click();
    const response = await killed;
    expect(response.status()).toBe(200);
    expect((await response.json()).data.killed).toBe(true);
    await expect.poll(noticeCount).toBeGreaterThan(noticesBefore);
    // The idle reader must update from the session notice without a focus event or reload.
    await expect(header.getByText('Terminated', { exact: true })).toBeVisible();
    await expect(header).toContainText('Terminated by admin');
    await expect(page.getByTestId('session-recover')).toBeVisible();
    await expect(page.getByTestId('composer-prompt')).toBeDisabled();
    await expect(page.getByText(userText, { exact: true })).toBeVisible();
    await expect(page.getByTestId('assistant-message')).toContainText(replyText);
    await waitForSandboxStopped(sandbox, 30_000);

    const kubeconfig = String(process.env.ASTRABOX_E2E_KUBECONFIG || '');
    expect(kubeconfig, 'the selected worker must provide its native observation credentials').not.toBe('');
    const kube = (args: string[]) => execFileSync('kubectl', ['--kubeconfig', kubeconfig, ...args], {
      encoding: 'utf8', timeout: 15_000,
    });
    const inventory = JSON.parse(kube(['get', 'batchsandbox', '-n', sandbox.namespace, '-o', 'json']));
    expect(inventory.apiVersion).toContain('/');
    expect(inventory.metadata.resourceVersion).toBeTruthy();
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('session-recover')).toBeVisible();
    await expect(page.getByText(userText, { exact: true })).toBeVisible();
    await expect(page.getByTestId('assistant-message')).toContainText(replyText);
    expect((await api.getMessages(sid)).messages).toEqual(before.messages);
    expect((await api.getSession(sid)).state).toBe('TERMINATED');
    expect(sandboxRunning(sandbox)).toBe(false);
    const events = kube(['get', '--raw', `/apis/${inventory.apiVersion}/namespaces/${sandbox.namespace}/batchsandboxes`
      + `?watch=1&resourceVersion=${inventory.metadata.resourceVersion}&timeoutSeconds=1`])
      .split('\n').filter(Boolean).map((line) => JSON.parse(line));
    expect(events.filter((event) => event.type === 'ERROR'), 'an unavailable native watch cannot prove read-only behavior').toEqual([]);
    expect(events.filter((event) => event.type === 'ADDED'
      && event.object.metadata?.labels?.['astrabox.session-id'] === sid), 'reading terminated history must not allocate compute')
      .toEqual([]);
  } finally {
    await admin.close();
  }
});
