import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, sqlLiteral } from '../fixtures/dbOracle';
import { apiPath, appPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

type Timeline = {
  click: number | null;
  creating: number | null;
  ready: number | null;
  queueable: number | null;
  details: number[];
  notices: number[];
};
type ObservedWindow = { startupTimeline: Timeline };

test('startup reaches visible READY from committed notices and catches a READY change missed while disconnected', async ({ page, request }, info) => {
  const api = new AstraApi(request);
  const name = `Startup notifications ${randomUUID()}`;
  agentId = (await api.createColdTestAgent(name)).agent_id;
  await page.addInitScript(() => {
    localStorage.setItem('astrabox-lang', 'en');
    const timeline: Timeline = { click: null, creating: null, ready: null, queueable: null, details: [], notices: [] };
    (window as unknown as ObservedWindow).startupTimeline = timeline;
    document.addEventListener('click', (event) => {
      if ((event.target as Element).closest('[data-testid="agent-start"]')) timeline.click = performance.now();
    }, true);
    const observe = () => {
      if (timeline.click === null) return;
      const state = document.querySelector('[data-testid="run-view"] [data-testid="status-pill"]')?.getAttribute('data-state');
      if (state === 'CREATING') timeline.creating ??= performance.now();
      if (state === 'READY') timeline.ready ??= performance.now();
      const composer = document.querySelector<HTMLTextAreaElement>('[data-testid="composer-prompt"]');
      if (composer && !composer.disabled) timeline.queueable ??= performance.now();
    };
    new MutationObserver(observe).observe(document, { childList: true, subtree: true, attributes: true });
    const original = window.fetch;
    window.fetch = async (...args: Parameters<typeof fetch>) => {
      const path = new URL(args[0] instanceof Request ? args[0].url : String(args[0]), location.href).pathname;
      if (/\/sessions\/[^/]+$/.test(path) && (!args[1]?.method || args[1].method === 'GET')) timeline.details.push(performance.now());
      const response = await original(...args);
      if (!/\/sessions\/[^/]+\/stream$/.test(path) || !response.body) return response;
      const [observed, product] = response.body.tee();
      void (async () => {
        const reader = observed.getReader();
        const decoder = new TextDecoder();
        let buffered = '';
        try {
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buffered += decoder.decode(value, { stream: true });
            const lines = buffered.split('\n');
            buffered = lines.pop() ?? '';
            for (const line of lines) {
              if (line.startsWith('data: ') && line.includes('"data-session-changed"')) timeline.notices.push(performance.now());
            }
          }
        } catch { /* The second startup deliberately drops this connection. */ }
      })();
      return new Response(product, { status: response.status, statusText: response.statusText, headers: response.headers });
    };
  });
  const evidence: unknown[] = [];
  let recoveryRequests = 0;
  page.on('request', (outbound) => {
    if (outbound.method() === 'POST' && /\/sessions\/[^/]+\/recover$/.test(new URL(outbound.url()).pathname)) recoveryRequests += 1;
  });
  try {
    for (const disconnect of [false, true]) {
      await page.goto(appPath('/agents'));
      await page.getByRole('tab', { name: 'All', exact: true }).click();
      const start = page.getByTestId('agent-option').filter({ hasText: name }).getByTestId('agent-start');
      await expect(start).toBeEnabled();
      const streamPattern = '**/sessions/*/stream?*';
      if (disconnect) await page.route(streamPattern, (route) => route.abort('connectionreset'));
      const creation = page.waitForResponse((response) => response.request().method() === 'POST'
        && new URL(response.url()).pathname === apiPath(`/agents/${agentId}/conversations`));
      await start.click();
      const sid = String((await (await creation).json()).data.session_id);
      sessions.push(sid);
      const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
      await expect(pill).toHaveAttribute('data-state', 'CREATING');
      let source: { state: string; sandbox_id: string; created_to_ready_sample_ms: number } | undefined;
      await expect.poll(() => {
        source = postgresRows<typeof source & object>(`SELECT state, sandbox_id,
          extract(epoch FROM (clock_timestamp() - created_at)) * 1000 AS created_to_ready_sample_ms
          FROM sessions WHERE session_id = ${sqlLiteral(sid)}`)[0];
        return source?.state;
      }, { timeout: 90_000, intervals: [100] }).toBe('READY');
      expect(source?.sandbox_id, 'READY must have a real persisted runtime binding').toBeTruthy();
      if (disconnect) {
        await expect(pill).toHaveAttribute('data-state', 'CREATING');
        expect((await page.evaluate(() => (window as unknown as ObservedWindow).startupTimeline)).details).toHaveLength(1);
        await page.unroute(streamPattern);
      }
      await expect(pill).toHaveAttribute('data-state', 'READY');
      const timeline = await page.evaluate(() => (window as unknown as ObservedWindow).startupTimeline);
      expect(timeline.creating).not.toBeNull();
      expect(timeline.ready).not.toBeNull();
      expect(timeline.click).not.toBeNull();
      expect(timeline.queueable).not.toBeNull();
      expect(timeline.notices.length).toBeGreaterThan(0);
      expect(timeline.details.length, 'initial read plus at most one read per invalidation, without timer reads')
        .toBeLessThanOrEqual(1 + timeline.notices.length);
      for (const at of timeline.details.slice(1)) {
        // The stream triggers a read immediately, rather than adding the old 500ms sleep.
        expect(timeline.notices.some((notice) => Math.abs(at - notice) < 250)).toBe(true);
      }
      evidence.push({ disconnected_during_startup: disconnect, session_id: sid, source,
        browser_click_to_visible_ready_ms: timeline.ready! - timeline.click!,
        browser_click_to_queueable_ms: timeline.queueable! - timeline.click!, timeline,
        server_timing: 'created to first READY SQL sample, not atomic commit time',
        timing_limit: disconnect ? 'includes deliberate stream outage and reconnect backoff' : 'cold conversation startup; downstream warm-stock scene not rechecked' });
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(pill).toHaveAttribute('data-state', 'READY');
      expect((await api.getSession(sid)).sandbox_id).toBe(source?.sandbox_id);
      expect(recoveryRequests, 'reading history never requests runtime recovery').toBe(0);
    }
  } finally {
    await info.attach('startup-state-evidence', { body: JSON.stringify(evidence), contentType: 'application/json' });
  }
});
