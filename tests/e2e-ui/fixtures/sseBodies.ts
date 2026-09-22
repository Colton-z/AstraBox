import type { Page } from '@playwright/test';

/** One SSE response the browser read: its URL and the bytes that arrived on it. */
export interface SseBody {
  url: string;
  text: string;
}

/**
 * Mirror every SSE body the BROWSER reads into `window.__astraboxSseBodies`.
 *
 * Installed before the first navigation, so the console's session stream is
 * captured from its first byte. Each `text/event-stream` response is CLONED and
 * the clone drained; the app is handed the original, untouched, so this observes
 * the page's channel without changing how the page reads it.
 */
export async function mirrorSseBodies(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const bodies: { url: string; text: string }[] = [];
    (window as unknown as { __astraboxSseBodies: typeof bodies }).__astraboxSseBodies = bodies;
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async (...args: Parameters<typeof window.fetch>) => {
      const response = await nativeFetch(...args);
      const isEventStream = String(response.headers.get('content-type') || '')
        .toLowerCase()
        .includes('text/event-stream');
      if (!isEventStream || !response.body) {
        return response;
      }
      const record = { url: response.url || String(args[0]), text: '' };
      bodies.push(record);
      // Clone BEFORE anything touches the body; the app gets the original.
      const mirror = response.clone();
      void (async () => {
        const reader = mirror.body!.getReader();
        const decoder = new TextDecoder();
        for (;;) {
          const chunk = await reader.read();
          if (chunk.done) break;
          record.text += decoder.decode(chunk.value, { stream: true });
        }
      })().catch(() => {
        // The page may tear down the next idle subscription response after the
        // assertions; a half-read idle body is not a finding.
      });
      return response;
    };
  });
}

/** The mirrored bodies for the session's ai-stream channel, newest last. */
export async function aiStreamBodies(page: Page): Promise<SseBody[]> {
  const bodies = await page.evaluate(
    () => (window as unknown as { __astraboxSseBodies?: SseBody[] }).__astraboxSseBodies ?? [],
  );
  return bodies.filter((body) => body.url.includes('/ai-stream'));
}
