/** Deployment transport reaches real form and multipart requests without changing replay policy. */
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect, test } from '@playwright/test';
import type { ConsoleTransport } from '../../../frontend/src/consoleTransport';
import { AstraApi } from '../fixtures/astraApi';
import { sessionInteractionRows } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => { if (agentId) await new AstraApi(request).deleteAgent(agentId); });

function questionnaire(scene: Record<string, unknown>) {
  return execFileSync('docker', [
    'exec', '-i', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-', JSON.stringify(scene),
  ], {
    input: readFileSync(resolve(__dirname, '../fixtures/transport_questionnaire.py'), 'utf8'),
    encoding: 'utf8', timeout: 30_000,
  });
}

type TransportObservation = { paths: string[]; probes: number; aborted: number; recovering: boolean };
declare global {
  interface Window { transportObservation: TransportObservation }
}

test('deployment recovery retains answers and files while ordinary failures and cancellation do not replay', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(`__e2e_transport_${Date.now()}`);
  agentId = agent.agent_id;
  const { session_id: sid } = await api.startConversation(agent.agent_id);
  sessions.push(sid);
  await api.waitForSessionReady(sid);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.addInitScript(({ probePath }) => {
    localStorage.setItem('astrabox-lang', 'en');
    const observation = { paths: [] as string[], probes: 0, aborted: 0, recovering: false };
    window.transportObservation = observation;
    let flight: Promise<void> | undefined;
    const wait = (pending: Promise<void>, signal: AbortSignal) => new Promise<void>((resolve, reject) => {
      const aborted = () => {
        observation.aborted += 1;
        signal.removeEventListener('abort', aborted);
        reject(signal.reason);
      };
      if (signal.aborted) return aborted();
      signal.addEventListener('abort', aborted, { once: true });
      pending.then(() => {
        signal.removeEventListener('abort', aborted);
        resolve();
      }, (error) => {
        signal.removeEventListener('abort', aborted);
        reject(error);
      });
    });
    const transport: ConsoleTransport = async (original) => {
      observation.paths.push(new URL(original.url).pathname);
      let outgoing = original;
      for (;;) {
        outgoing.signal.throwIfAborted();
        const replay = outgoing.clone();
        const response = await fetch(outgoing);
        // This fixture's exact pre-controller protocol is deployment-owned.
        const payload = response.headers.get('content-type')?.includes('application/json')
          ? await response.clone().json() : null;
        if (!(response.status === 500 && payload?.code === 'FIXTURE_AUTH_BEFORE_CONTROLLER')
          && !(response.status === 200 && payload?.action === 'FIXTURE_LOGIN')) return response;
        if (!flight) {
          observation.probes += 1;
          observation.recovering = true;
          flight = fetch(probePath, { credentials: 'include', cache: 'no-store' })
            .then((probe) => { if (!probe.ok) throw new Error('fixture auth probe failed'); })
            .finally(() => { flight = undefined; observation.recovering = false; });
        }
        await wait(flight, outgoing.signal);
        outgoing = replay;
      }
    };
    globalThis.astraboxConsoleTransport = transport;
  }, { probePath: apiPath('/user/current?transport_probe=1') });

  // Only this test's requests are faulted; route.continue sends the final attempt to the real service.
  const uploadPath = apiPath(`/sessions/${sid}/files/upload`);
  const uploadBodies: Buffer[] = [];
  await page.route(`**${uploadPath}`, async (route) => {
    uploadBodies.push(route.request().postDataBuffer()!);
    if (uploadBodies.length === 1) await route.fulfill({
      status: 500, contentType: 'application/json', body: JSON.stringify({ code: 'FIXTURE_AUTH_BEFORE_CONTROLLER' }),
    });
    else await route.continue();
  });
  await openSessionView(page, sid);
  await page.getByRole('tab', { name: 'Files', exact: true }).click();
  const upload = page.getByRole('button', { name: 'Upload', exact: true });
  await expect(upload).toBeEnabled();
  const filename = `transport-${sid}.txt`;
  const content = 'AstraBox transport binary \u0000 and Unicode 验证\n';
  const uploaded = page.waitForResponse((response) => response.url().endsWith(uploadPath) && response.status() === 200);
  const chooser = page.waitForEvent('filechooser');
  await upload.click();
  await (await chooser).setFiles({ name: filename, mimeType: 'text/plain', buffer: Buffer.from(content) });
  expect((await uploaded).status()).toBe(200);
  expect(uploadBodies).toHaveLength(2);
  expect(uploadBodies[0].equals(uploadBodies[1]), 'the cloned multipart body keeps its framing and bytes').toBe(true);
  expect(await api.downloadFileText(sid, `/workspace/${filename}`)).toBe(content);

  const form = JSON.parse(questionnaire({ session_id: sid })) as { interaction_id: string; lease: unknown };
  await page.reload({ waitUntil: 'domcontentloaded' });
  const panel = page.getByTestId('pending-interaction-panel');
  await expect(panel).toBeVisible();
  await panel.getByRole('radio', { name: /Red/ }).click();
  await panel.getByRole('button', { name: /Multiple/ }).click();
  await panel.getByRole('checkbox', { name: /Fast/ }).click();
  await panel.getByRole('checkbox', { name: /Quiet/ }).click();
  await panel.getByRole('button', { name: /Note/ }).click();
  const note = panel.locator('[data-slot="questionnaire-item"]').filter({ hasText: 'Add a note' });
  await note.getByRole('radio', { name: /Write your own/ }).click();
  await note.getByRole('textbox').fill('保留多选和自由文本');
  const submit = panel.getByRole('button', { name: /Submit answer/ });
  const answerPath = apiPath(`/sessions/${sid}/interactions/${encodeURIComponent(form.interaction_id)}/answer`);
  const bodies: string[] = [];
  let mode: 'business' | 'html' | 'denied' | 'cancel' | 'recover' = 'business';
  let recoveryAttempts = 0;
  let releaseProbe = () => {};
  let probeHeld = false;
  await page.route(`**${apiPath('/user/current?transport_probe=1')}`, async (route) => {
    if (mode === 'cancel') {
      probeHeld = true;
      await new Promise<void>((resolve) => { releaseProbe = resolve; });
    }
    await route.continue();
  });
  await page.route(`**${answerPath}`, async (route) => {
    bodies.push(route.request().postData()!);
    if (mode === 'recover' && ++recoveryAttempts > 2) return route.continue();
    const status = mode === 'html' ? 502 : mode === 'denied' ? 403
      : mode === 'recover' && recoveryAttempts === 2 ? 200 : 500;
    await route.fulfill({
      status, contentType: mode === 'html' ? 'text/html' : 'application/json',
      body: mode === 'html' ? '<h1>502 Gateway</h1>' : JSON.stringify(
        mode === 'cancel' || mode === 'recover'
          ? status === 200 ? { action: 'FIXTURE_LOGIN' } : { code: 'FIXTURE_AUTH_BEFORE_CONTROLLER' }
          : { code: mode === 'denied' ? 'FORBIDDEN' : 'FIXTURE_BUSINESS_FAILURE', message: 'not authentication' },
      ),
    });
  });
  const documentTime = await page.evaluate(() => performance.timeOrigin);
  let documents = 0;
  page.on('request', (outgoing) => { if (outgoing.resourceType() === 'document') documents += 1; });
  const row = () => sessionInteractionRows(sid).find((entry) => entry.interaction_id === form.interaction_id)!;
  const probes = () => page.evaluate(() => window.transportObservation.probes);

  try {
    for (const failure of ['business', 'html', 'denied'] as const) {
      mode = failure;
      const before = bodies.length;
      const failed = page.waitForResponse((response) => response.url().endsWith(answerPath));
      await submit.click();
      await failed;
      await expect(submit).toBeEnabled();
      expect(bodies).toHaveLength(before + 1);
      expect(await probes()).toBe(0);
      expect(row().answer).toBeNull();
      await expect(note.getByRole('textbox')).toHaveValue('保留多选和自由文本');
    }
    mode = 'cancel';
    const beforeCancel = bodies.length;
    await submit.click();
    await expect.poll(() => probeHeld).toBe(true);
    // The real API's 15-second answer deadline must cancel an authentication wait too.
    await expect(submit).toBeEnabled({ timeout: 20_000 });
    releaseProbe();
    await expect.poll(() => page.evaluate(() => window.transportObservation.aborted)).toBe(1);
    await expect.poll(() => page.evaluate(() => window.transportObservation.recovering)).toBe(false);
    expect(bodies).toHaveLength(beforeCancel + 1);
    expect(row().answer).toBeNull();

    mode = 'recover';
    const accepted = page.waitForResponse(async (response) => response.url().endsWith(answerPath)
      && response.status() === 200 && (await response.json()).code === 'OK');
    await submit.click();
    expect((await (await accepted).json()).data.answered).toBe(true);
    await expect(panel).toHaveCount(0);
    expect(recoveryAttempts).toBe(3);
    expect(new Set(bodies).size, 'every attempt preserves the same answer').toBe(1);
    expect(await probes()).toBe(3);
    const user = await api.data<{ user_id: string }>('GET', '/user/current');
    expect(row()).toMatchObject({ answered_by: user.user_id, answer: { answers: [
      { question_id: 'single', option_label: 'Red' },
      { question_id: 'multiple', option_labels: ['Fast', 'Quiet'] },
      { question_id: 'note', free_text: '保留多选和自由文本' },
    ] } });
    expect(documents).toBe(0);
    expect(await page.evaluate(() => performance.timeOrigin)).toBe(documentTime);
    const paths = await page.evaluate(() => window.transportObservation.paths);
    expect(paths).toContain(answerPath);
    expect(paths.some((path) => path.endsWith(`/sessions/${sid}/stream`))).toBe(true);
    questionnaire({ session_id: sid, lease: form.lease });
  } finally {
    releaseProbe();
  }
});
