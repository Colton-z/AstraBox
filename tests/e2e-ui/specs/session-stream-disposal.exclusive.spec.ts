/**
 * A completed real history body delivered after departure must not open a
 * stream. The request signal still aborts; body completion precedes that abort.
 * This covers the race that cancelling an in-flight network request alone
 * cannot exercise, while retaining the actual five-second Agent-save oracle.
 */
import { expect, test, type Page, type Route } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

interface HistoryObservation {
  requests: number;
  streamAttempts: number;
  departedAt: number;
  headersAt: number;
  bodyReadAt: number;
  textReadAt: number;
  status: number | null;
  body: string;
  error: string;
  signalAborted: boolean;
}

type DisposalWindow = Window & typeof globalThis & {
  __historyDisposal: HistoryObservation & { releaseBody: () => void; disarm: () => void };
};

async function observeHistoryConsumption(page: Page, sessionId: string): Promise<void> {
  await page.addInitScript(({ historyPath, streamPath }) => {
    const originalFetch = window.fetch.bind(window);
    let releaseBody!: () => void;
    const bodyReleased = new Promise<void>((resolve) => { releaseBody = resolve; });
    const observation: DisposalWindow['__historyDisposal'] = {
      requests: 0, streamAttempts: 0, departedAt: 0, headersAt: 0, bodyReadAt: 0, textReadAt: 0,
      status: null, body: '', error: '', signalAborted: false,
      releaseBody,
      disarm: () => { releaseBody(); window.fetch = originalFetch; },
    };
    (window as DisposalWindow).__historyDisposal = observation;
    window.fetch = async (input, init) => {
      const request = input instanceof Request ? input : null;
      const method = String(init?.method ?? request?.method ?? 'GET').toUpperCase();
      const url = new URL(request?.url ?? String(input), window.location.href);
      if (method === 'GET' && url.pathname === streamPath) observation.streamAttempts += 1;
      if (method !== 'GET' || url.pathname !== historyPath) return originalFetch(input, init);
      observation.requests += 1;
      const signal = init?.signal ?? request?.signal;
      try {
        // Preserve the caller's Request/init, including its AbortSignal. A
        // rejection is failure evidence, never a late successful response.
        const response = await originalFetch(input, init);
        observation.status = response.status;
        observation.headersAt = performance.now();
        observation.signalAborted = Boolean(signal?.aborted);
        const readText = response.text.bind(response);
        response.text = async () => {
          try {
            const body = await readText();
            observation.body = body;
            observation.bodyReadAt = performance.now();
            // The real response is completely read while its route still owns
            // the request. Delay only delivery of those bytes to the caller:
            // navigation may abort its signal after network/body completion.
            await bodyReleased;
            observation.textReadAt = performance.now();
            observation.signalAborted = Boolean(signal?.aborted);
            return body;
          } catch (error) {
            observation.error = String(error);
            observation.signalAborted = Boolean(signal?.aborted);
            throw error;
          }
        };
        return response;
      } catch (error) {
        observation.error = String(error);
        observation.signalAborted = Boolean(signal?.aborted);
        throw error;
      }
    };
  }, {
    historyPath: apiPath(`/sessions/${sessionId}/history-blocks`),
    streamPath: apiPath(`/sessions/${sessionId}/ai-stream`),
  });
}

async function historyObservation(page: Page): Promise<HistoryObservation> {
  return page.evaluate(() => {
    const { disarm: _disarm, releaseBody: _releaseBody, ...observation } = (window as DisposalWindow).__historyDisposal;
    return observation;
  });
}

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
  agentId = '';
});

test('leaving during initial history load prevents a late stream from blocking Agent save', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(`__e2e_stream_disposal_${Date.now()}`);
  agentId = agent.agent_id;
  const created = await api.startConversation(agentId);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId);
  expect(ready.state).toBe('READY');
  expect(ready.current_turn_id).toBeFalsy();

  let heldRequests = 0;
  let heldResponseReady = false;
  let originalBody = '';
  const historyPath = apiPath(`/sessions/${sessionId}/history-blocks`);
  const streamPath = apiPath(`/sessions/${sessionId}/ai-stream`);
  const agentPath = apiPath(`/agents/${agentId}`);
  const historyRoute = async (route: Route) => {
    if (route.request().method() !== 'GET') return route.continue();
    heldRequests += 1;
    const response = await route.fetch();
    expect(response.status(), 'the held history must be a successful real backend response').toBe(200);
    originalBody = await response.text();
    expect(JSON.parse(originalBody)).toMatchObject({ code: 'OK' });
    heldResponseReady = true;
    // Deliver the successful backend response unchanged. The browser's text
    // reader holds application delivery only after it has consumed this body.
    await route.fulfill({ response });
  };
  const oldStreamRequests: string[] = [];
  let saveRequests = 0;
  page.on('request', (sent) => {
    const pathname = new URL(sent.url()).pathname;
    if (sent.method() === 'GET' && pathname === streamPath) oldStreamRequests.push(sent.url());
    if (sent.method() === 'PUT' && pathname === agentPath) saveRequests += 1;
  });

  await observeHistoryConsumption(page, sessionId);
  await page.route((url) => url.pathname === historyPath, historyRoute);
  try {
    await page.goto(appPath(`/sessions/${sessionId}`), { waitUntil: 'domcontentloaded' });
    await expect.poll(async () => heldResponseReady && (await historyObservation(page)).bodyReadAt > 0,
      { timeout: 30_000, message: 'the real history body must finish reading before navigation' }).toBe(true);
    expect(heldRequests).toBe(1);
    await expect(page.getByText('Syncing messages…', { exact: true })).toBeVisible();
    await expect(page.getByTestId('run-view')).toHaveCount(0);
    expect(await historyObservation(page)).toMatchObject({
      requests: 1, streamAttempts: 0, textReadAt: 0, error: '', signalAborted: false,
    });
    const completedBody = await historyObservation(page);
    expect(completedBody.status).toBe(200);
    expect(completedBody.headersAt).toBeGreaterThan(0);
    expect(completedBody.bodyReadAt).toBeGreaterThanOrEqual(completedBody.headersAt);
    expect(completedBody.body).toBe(originalBody);
    expect(oldStreamRequests).toEqual([]);
    const documentStartedAt = await page.evaluate(() => performance.timeOrigin);

    await page.getByRole('link', { name: 'Console', exact: true }).click();
    await expect(page).toHaveURL(appPath('/manage/agents'));
    await page.evaluate(() => {
      (window as DisposalWindow).__historyDisposal.departedAt = performance.now();
    });
    await page.evaluate(() => (window as DisposalWindow).__historyDisposal.releaseBody());
    await expect.poll(async () => {
      const observation = await historyObservation(page);
      return observation.textReadAt > 0 || observation.error !== '';
    }, { timeout: 10_000, message: 'the successful real history body must reach the application after departure' }).toBe(true);
    const consumed = await historyObservation(page);
    expect(consumed.error, 'the completed fetch/body read must succeed before late application delivery').toBe('');
    expect(consumed.signalAborted, 'route disposal must still abort its real request signal').toBe(true);
    expect(consumed.status).toBe(200);
    expect(consumed.headersAt).toBeLessThan(consumed.departedAt);
    expect(consumed.bodyReadAt).toBeLessThan(consumed.departedAt);
    expect(consumed.textReadAt).toBeGreaterThan(consumed.departedAt);
    expect(consumed.body, 'the application must consume exactly the held real response body').toBe(originalBody);
    expect(consumed.requests).toBe(1);
    expect(consumed.streamAttempts).toBe(0);
    expect(oldStreamRequests).toEqual([]);

    await page.getByRole('row').filter({ hasText: agent.name }).click();
    await expect(page).toHaveURL(appPath(`/manage/agents/${encodeURIComponent(agentId)}`));
    const marker = `Stream disposal saved ${Date.now()}`;
    await page.getByRole('textbox', { name: 'Display name', exact: true }).fill(marker);
    const save = page.getByRole('button', { name: 'Save', exact: true });
    await expect(save).toBeEnabled();
    const savedResponse = page.waitForResponse((response) => (
      response.request().method() === 'PUT' && new URL(response.url()).pathname === agentPath
    ), { timeout: 5_000 });
    const saveStartedAt = Date.now();
    await save.click();
    expect((await savedResponse).status()).toBe(200);
    await expect(page.getByText('Saved', { exact: true })).toBeVisible({ timeout: 5_000 });
    expect(Date.now() - saveStartedAt, 'real Agent save must finish within the source five-second budget').toBeLessThan(5_000);
    expect(saveRequests).toBe(1);
    const savedAgent = await api.data<{ display_meta?: { display_name?: string } }>('GET', `/agents/${agentId}`);
    expect(savedAgent.display_meta?.display_name).toBe(marker);
    expect((await historyObservation(page)).streamAttempts).toBe(0);
    expect(oldStreamRequests).toEqual([]);
    expect(await page.evaluate(() => performance.timeOrigin), 'departure must remain a single SPA document').toBe(documentStartedAt);
    const idle = await api.getSession(sessionId);
    expect(idle.state).toBe('READY');
    expect(idle.current_turn_id).toBeFalsy();
  } finally {
    if (!page.isClosed()) {
      await page.evaluate(() => (window as DisposalWindow).__historyDisposal?.disarm());
    }
    await page.unrouteAll({ behavior: 'wait' });
  }
});
