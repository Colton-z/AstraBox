import { expect, test, type Response } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { apiPath, appPath } from '../fixtures/env';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

type StreamAuditEvent = { at: number; type: string; deltaLength: number };
type StreamAudit = {
  startedAt: number;
  events: StreamAuditEvent[];
  done: boolean;
  error: string;
};

const STREAM_AUDIT_KEY = '__astraboxE2EStreamAudit';

async function armBrowserStreamAudit(page: import('@playwright/test').Page, sessionId: string) {
  await page.evaluate(({ key, path }) => {
    type BrowserAudit = StreamAudit & { controller: AbortController };
    const store = window as unknown as Record<string, unknown>;
    const previous = store[key] as BrowserAudit | undefined;
    previous?.controller.abort();
    const audit: BrowserAudit = {
      startedAt: Date.now(),
      events: [],
      done: false,
      error: '',
      controller: new AbortController(),
    };
    store[key] = audit;
    void (async () => {
      try {
        const response = await fetch(path, {
          credentials: 'same-origin',
          signal: audit.controller.signal,
        });
        if (!response.ok || !response.body) {
          throw new Error(`session stream returned HTTP ${response.status}`);
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
          const chunk = await reader.read();
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true });
          const records = buffer.split(/\r?\n\r?\n/);
          buffer = records.pop() || '';
          for (const record of records) {
            const raw = record.split(/\r?\n/)
              .filter((line) => line.startsWith('data:'))
              .map((line) => line.slice(5).trim())
              .join('');
            if (!raw || raw === '[DONE]') continue;
            let frame: Record<string, unknown>;
            try {
              frame = JSON.parse(raw) as Record<string, unknown>;
            } catch {
              continue;
            }
            const type = String(frame.type || '');
            audit.events.push({
              at: Date.now() - audit.startedAt,
              type,
              deltaLength: typeof frame.delta === 'string' ? frame.delta.length : 0,
            });
            if (type === 'finish' || type === 'error') {
              audit.done = true;
              await reader.cancel();
              return;
            }
          }
        }
        audit.done = true;
      } catch (error) {
        if (!audit.controller.signal.aborted) audit.error = String(error);
      }
    })();
  }, {
    key: STREAM_AUDIT_KEY,
    path: apiPath(`/sessions/${sessionId}/ai-stream?follow=session`),
  });
}

async function readBrowserStreamAudit(page: import('@playwright/test').Page): Promise<StreamAudit> {
  return page.evaluate((key) => {
    const audit = (window as unknown as Record<string, unknown>)[key] as StreamAudit | undefined;
    if (!audit) throw new Error('browser stream audit is not armed');
    return {
      startedAt: audit.startedAt,
      events: [...audit.events],
      done: audit.done,
      error: audit.error,
    };
  }, STREAM_AUDIT_KEY);
}

async function stopBrowserStreamAudit(page: import('@playwright/test').Page): Promise<void> {
  await page.evaluate((key) => {
    const audit = (window as unknown as Record<string, unknown>)[key] as
      | { controller?: AbortController }
      | undefined;
    audit?.controller?.abort();
  }, STREAM_AUDIT_KEY).catch(() => undefined);
}


// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('agent_chat creation waits for READY before opening the SDK live subscription', async ({ request, page }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const sessionStates = new Map<string, string>();
  const streamOpenStates: Array<{ sessionId: string; state: string }> = [];
  const streamResponses: Array<{ sessionId: string; status: number }> = [];
  const childListResponses: Array<{ sessionId: string; response: Response }> = [];
  const creatingDetails = new Set<string>();
  let holdDetail = true;
  let releaseDetail!: () => void;
  const detailGate = new Promise<void>((resolve) => { releaseDetail = resolve; });
  let sessionId = '';

  await page.route((url) => {
    const prefix = `${apiPath('/sessions/').replace(/\/$/, '')}/`;
    if (!url.pathname.startsWith(prefix)) return false;
    return url.pathname.slice(prefix.length).split('/').filter(Boolean).length === 1;
  }, async (route) => {
    if (route.request().method() !== 'GET') {
      await route.continue();
      return;
    }
    const response = await route.fetch();
    const payload = await response.json() as Record<string, unknown>;
    const data = payload.data && typeof payload.data === 'object'
      ? payload.data as Record<string, unknown>
      : payload;
    const id = String(data.session_id || '').trim();
    const state = String(data.state || '');
    // Deliver real CREATING detail, then delay newer real responses. Never
    // rewrite a state or replay an old detail as a new server response.
    if (holdDetail && state !== 'CREATING') await detailGate;
    if (id) {
      sessionStates.set(id, state);
      if (state === 'CREATING') creatingDetails.add(id);
    }
    await route.fulfill({ response, json: payload });
  });
  page.on('request', (candidate) => {
    if (candidate.method() !== 'GET') return;
    const url = new URL(candidate.url());
    const match = url.pathname.match(/\/sessions\/([^/]+)\/ai-stream$/);
    if (!match) return;
    const id = decodeURIComponent(match[1]);
    streamOpenStates.push({ sessionId: id, state: sessionStates.get(id) || '<unobserved>' });
  });
  page.on('response', (response) => {
    if (response.request().method() !== 'GET') return;
    const path = new URL(response.url()).pathname;
    const stream = path.match(/\/sessions\/([^/]+)\/ai-stream$/);
    if (stream) {
      streamResponses.push({ sessionId: decodeURIComponent(stream[1]), status: response.status() });
    }
    const childList = path.match(/\/sessions\/([^/]+)\/child-runs$/);
    if (childList) childListResponses.push({ sessionId: decodeURIComponent(childList[1]), response });
  });

  try {
    await page.goto(appPath('/agents'), { waitUntil: 'domcontentloaded' });
    const card = page.locator(
      `[data-testid="agent-option"][data-agent-name="${agent.name}"]`,
    );
    await expect(card).toBeVisible({ timeout: 30_000 });
    await card.getByRole('button').click();
    await page.waitForURL((url) => /\/sessions\/[^/]+$/.test(url.pathname), {
      timeout: 180_000,
    });
    sessionId = new URL(page.url()).pathname.split('/').filter(Boolean).at(-1) || '';
    expect(sessionId).not.toEqual('');
    // Tracked from here: this spec reads the id off the URL the console
    // navigated to, so there is nothing to track before the assertion above.
    sessions.push(sessionId);

    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });
    await expect.poll(() => creatingDetails.has(sessionId), {
      timeout: 30_000,
      message: 'the browser must actually receive a real CREATING detail',
    }).toBe(true);
    const header = page.getByTestId('run-view').locator('header').first();
    const status = header.getByTestId('status-pill');
    await expect(status).toHaveAttribute('data-state', 'CREATING');
    expect(await page.evaluate(() => document.visibilityState)).toBe('visible');
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.waitForTimeout(300);
    expect(
      streamOpenStates.filter((item) => item.sessionId === sessionId),
      'visibility recovery while CREATING must not bypass the live-subscription gate',
    ).toEqual([]);
    await expect(status).toHaveAttribute('data-state', 'CREATING');
    holdDetail = false;
    releaseDetail();

    // The composer is NOT a readiness proxy: it enables during CREATING by
    // design (sessionRunStatus.canQueueMessage — a message typed early queues
    // and dispatches at READY). The claim under test is the SUBSCRIPTION:
    // no ai-stream may open before provisioning reaches READY. So reach READY
    // on the API's own word, then judge what the network observer recorded.
    await expect(page.getByTestId('composer-prompt')).toBeEnabled({ timeout: 60_000 });
    const ready = await api.waitForSessionReady(sessionId);
    expect(ready.state).toBe('READY');
    expect(String(ready.sandbox_id || '').trim()).not.toEqual('');
    await expect(status).toHaveAttribute('data-state', 'READY', { timeout: 30_000 });
    await expect.poll(
      () => streamResponses.filter((item) => item.sessionId === sessionId).map((item) => item.status),
      { timeout: 30_000, message: 'READY must open the actual standing subscription without a reload' },
    ).toContain(200);
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.waitForTimeout(300);
    expect(
      streamResponses.filter((item) => item.sessionId === sessionId).map((item) => item.status),
      'visibility recovery must leave the READY subscription healthy',
    ).not.toContain(409);

    await expect.poll(
      () => childListResponses.some((item) => item.sessionId === sessionId && item.response.status() === 200),
      { timeout: 30_000, message: 'the browser must finish a real child-resource listing before judging empty sync' },
    ).toBe(true);
    const childList = childListResponses.find((item) => item.sessionId === sessionId && item.response.status() === 200)!;
    const childPayload = await childList.response.json();
    expect(childPayload.data.session_id).toBe(sessionId);
    expect(childPayload.data.child_runs, 'fresh conversation has no durable children after sync').toEqual([]);
    await page.getByRole('tab', { name: /^Agents/ }).click();
    const agentsPanel = page.getByTestId('subagent-agents-panel');
    await expect(agentsPanel).toBeVisible();
    await expect(agentsPanel.getByTestId('empty-state')).toBeVisible();
    await expect(agentsPanel.getByTestId('subagent-agent-row')).toHaveCount(0);
    await expect(agentsPanel.getByRole('alert')).toHaveCount(0);
    await expect(status).toHaveAttribute('data-state', 'READY');
    await expect(status).toHaveText(/Ready|就绪/);
    await expect(header.locator('[data-slot="verbatim"]')).toHaveCount(0);
    await expect(page.getByText(/^(Runtime disconnected|运行时断开|Sandbox expired|沙箱已过期)$/)).toHaveCount(0);
    await expect(page.getByRole('button', { name: /^(Recover session|恢复会话)$/ })).toHaveCount(0);
    await expect(page.getByText('session runtime is still being created', { exact: false })).toHaveCount(0);
    expect(
      streamOpenStates
        .filter((item) => item.sessionId === sessionId)
        .every((item) => item.state === 'READY'),
      'any idle standing subscription must wait until provisioning reaches READY',
    ).toBe(true);

    const assistantCount = await api.assistantCount(sessionId);
    await sendPrompt(page, sessionId, 'Reply briefly without using tools.');
    await expect.poll(
      () => streamOpenStates.filter((item) => item.sessionId === sessionId).length,
      {
        timeout: 60_000,
        message: 'the READY conversation should open its standing output subscription',
      },
    ).toBeGreaterThan(0);
    const observedStates = streamOpenStates
      .filter((item) => item.sessionId === sessionId)
      .map((item) => item.state);
    expect(
      new Set(observedStates),
      'no output subscription may race session provisioning',
    ).toEqual(new Set(['READY']));
    await api.waitForAssistantMessageCount(sessionId, assistantCount, 180_000);
    await api.waitForSessionReady(sessionId);
  } finally {
    holdDetail = false;
    releaseDetail();
    await page.unrouteAll({ behavior: 'wait' });
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});

test('consecutive turns render in separate assistant messages', async ({ request, page }) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const firstMarker = `FIRST_TURN_${runId}`;
  const secondMarker = `SECOND_TURN_${runId}`;
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  await api.waitForSessionReady(sessionId);
  await openSessionView(page, sessionId);

  const assistantCountBeforeFirst = await api.assistantCount(sessionId);
  await sendPrompt(page, sessionId, [
    `Reply with exactly ${firstMarker}.`,
    'Do not call any tool and do not add any other text.',
  ].join('\n'));
  const firstAssistant = await api.waitForAssistantMessageMatching(
    sessionId,
    assistantCountBeforeFirst,
    (message) => messageText(message).includes(firstMarker),
  );
  await api.waitForSessionReady(sessionId);
  expect(messageText(firstAssistant)).not.toContain(secondMarker);

  const firstBubble = page.getByTestId('assistant-message').filter({ hasText: firstMarker });
  await expect(firstBubble).toHaveCount(1);
  await expect(firstBubble).not.toHaveAttribute('data-streaming', 'true');
  const firstAssistantText = firstBubble.getByTestId('assistant-text');
  const firstRenderedText = (await firstAssistantText.allInnerTexts()).join('\n');
  expect(firstRenderedText).toContain(firstMarker);
  const renderedCountAfterFirst = await page.getByTestId('assistant-message').count();

  const assistantCountBeforeSecond = await api.assistantCount(sessionId);
  await sendPrompt(page, sessionId, [
    `Reply with exactly ${secondMarker}.`,
    'Do not call any tool and do not add any other text.',
  ].join('\n'));
  const secondAssistant = await api.waitForAssistantMessageMatching(
    sessionId,
    assistantCountBeforeSecond,
    (message) => messageText(message).includes(secondMarker),
  );
  await api.waitForSessionReady(sessionId);
  expect(
    messageText(secondAssistant),
    'the durable second reply must not contain content from the first turn',
  ).not.toContain(firstMarker);

  const secondBubble = page.getByTestId('assistant-message').filter({ hasText: secondMarker });
  await expect(secondBubble).toHaveCount(1);
  await expect(secondBubble).not.toContainText(firstMarker);
  await expect.poll(
    async () => (await firstAssistantText.allInnerTexts()).join('\n'),
    { message: 'the first assistant text must remain unchanged after the second turn' },
  ).toBe(firstRenderedText);
  await expect(
    page.getByTestId('assistant-message'),
    'the second turn must add one assistant message instead of extending the first',
  ).toHaveCount(renderedCountAfterFirst + 1);
});

test('the browser renders incremental text before the session stream finishes', async ({ request, page }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  try {
    await api.waitForSessionReady(sessionId);
    await page.goto(appPath(`/sessions/${sessionId}`), { waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });
    await expect(page.getByTestId('composer-prompt')).toBeEnabled({ timeout: 60_000 });
    await armBrowserStreamAudit(page, sessionId);

    await sendPrompt(
      page,
      sessionId,
      '不要使用工具。请写二十个编号短段，逐条说明实时流式界面的设计原则，每段一句。',
    );

    let renderedPrefix = '';
    let auditAtPrefix: StreamAudit | null = null;
    const prefixDeadline = Date.now() + 60_000;
    while (Date.now() < prefixDeadline) {
      const current = await readBrowserStreamAudit(page);
      renderedPrefix = String(await page.getByTestId('assistant-text').last().textContent().catch(() => ''));
      if (renderedPrefix.trim().length >= 10 || current.done || current.error) {
        auditAtPrefix = current;
        break;
      }
      await page.waitForTimeout(50);
    }
    expect(auditAtPrefix?.error || '', 'the browser session stream must remain healthy').toBe('');
    expect(renderedPrefix.trim().length, 'assistant text must become visible during the live turn')
      .toBeGreaterThanOrEqual(10);
    expect(
      auditAtPrefix?.done,
      'a visible text prefix must render before the terminal finish, not arrive all at once',
    ).toBe(false);

    await expect.poll(async () => (await readBrowserStreamAudit(page)).done, {
      timeout: 120_000,
      message: 'the audited session stream must reach its terminal frame',
    }).toBe(true);
    const finalAudit = await readBrowserStreamAudit(page);
    expect(finalAudit.error).toBe('');
    const textDeltas = finalAudit.events.filter((event) => event.type === 'text-delta');
    const finish = finalAudit.events.find((event) => event.type === 'finish');
    expect(textDeltas.length, 'the browser must receive multiple live text deltas').toBeGreaterThan(2);
    expect(finish, 'the browser stream must expose a finish frame').toBeTruthy();
    expect(
      textDeltas.at(-1)!.at - textDeltas[0].at,
      'text deltas must arrive over time rather than as one coalesced terminal burst',
    ).toBeGreaterThan(200);
    expect(finish!.at - textDeltas[0].at, 'the first text delta must precede finish observably')
      .toBeGreaterThan(200);
  } finally {
    await stopBrowserStreamAudit(page);
  }
});
