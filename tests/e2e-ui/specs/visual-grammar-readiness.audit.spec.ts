import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import type { BrowserContext } from '@playwright/test';
import { test, expect } from '@playwright/test';

import { openSessionView } from '../fixtures/sessionPage';
import { blocksConsoleSettlement } from '../fixtures/requestReadiness';

test('a rendered conversation does not wait for its live stream to close', async ({ browser }) => {
  expect(blocksConsoleSettlement('http://astrabox.test/api/v1/sessions/live/history-blocks')).toBe(true);
  expect(blocksConsoleSettlement('http://astrabox.test/api/v1/sessions/live')).toBe(false);
  expect(blocksConsoleSettlement('http://astrabox.test/api/v1/sessions/live', 'POST')).toBe(true);
  expect(blocksConsoleSettlement('http://astrabox.test/api/v1/sessions/live/ai-stream')).toBe(false);
  let streamRequests = 0;
  const remoteBrowser = Boolean(process.env.ASTRABOX_E2E_BROWSER_WS_ENDPOINT);
  const fixtureHost = remoteBrowser
    ? process.env.ASTRABOX_E2E_BROWSER_HOST_GATEWAY
    : '127.0.0.1';
  if (!fixtureHost) {
    throw new Error('the isolated browser runner did not publish its Docker bridge host gateway');
  }
  expect(
    fixtureHost,
    'the isolated browser runner must publish its Docker bridge host gateway',
  ).toMatch(/^\d{1,3}(\.\d{1,3}){3}$/);
  const server = createServer((request, response) => {
    const pathname = new URL(request.url || '/', 'http://127.0.0.1').pathname;
    if (pathname.endsWith('/sessions/live-stream')) {
      response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      response.end(`<!doctype html>
        <main data-testid="run-view">Rendered conversation</main>
        <script>
          void (async () => {
            const response = await fetch('/events');
            const reader = response.body.getReader();
            await reader.read();
            document.body.dataset.stream = 'open';
            await reader.read();
          })();
        </script>`);
      return;
    }
    if (pathname === '/events') {
      streamRequests += 1;
      response.writeHead(200, {
        'Cache-Control': 'no-cache',
        'Content-Type': 'text/event-stream',
      });
      response.write('data: stream-open\n\n');
      return;
    }
    response.writeHead(404).end();
  });

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, fixtureHost, () => {
      server.off('error', reject);
      resolve();
    });
  });

  const address = server.address() as AddressInfo;
  let context: BrowserContext | undefined;
  try {
    context = await browser.newContext({ baseURL: `http://${fixtureHost}:${address.port}` });
    const page = await context.newPage();
    page.setDefaultNavigationTimeout(2_000);

    await openSessionView(page, 'live-stream');

    await expect(page.getByTestId('run-view')).toHaveText('Rendered conversation');
    await expect(page.locator('body')).toHaveAttribute('data-stream', 'open');
    expect(streamRequests).toBe(1);
  } finally {
    await context?.close();
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
    });
  }
});
