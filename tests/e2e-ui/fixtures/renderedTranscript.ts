/**
 * Read the answer the way the person reading it does, and say when it repeats.
 *
 * Three specs carry private copies of the first two helpers
 * (`live-stream-reconnect-...`, `a-stream-lost-mid-reply-says-so`,
 * `a-stream-that-dies-without-ending-...`). This is the shared copy new specs
 * use; adopting it in those three is a separate cleanup.
 */
import { expect, type Page } from '@playwright/test';

import type { MessageRecord } from './astraApi';

/** Rendered reply parts only; reasoning can collapse across reload or settlement. */
export async function assistantTranscript(page: Page): Promise<string> {
  return (await page.getByTestId('assistant-text').allInnerTexts()).join('\n');
}

/**
 * Rendered prose with layout and markdown decoration removed.
 *
 * Comparisons across a route change and across the stream/settled boundary run
 * over text that is the same to a reader and a different string to a matcher: a
 * half-arrived `**` is literal mid-stream and gone once the emphasis closes, and
 * line wrapping moves with the panel. Normalizing keeps the assertion about
 * content.
 */
export function normalizeRendered(text: string): string {
  return text.replace(/\s+/g, '').replace(/[*_`#>|~\-–—·•]/g, '');
}

/**
 * The first stretch of `text` that appears twice, with both neighbourhoods, or null.
 *
 * The page-side reading of "no duplicated assistant text": a page that
 * rehydrates the prefix and then replays it AGAIN off the resumed stream shows
 * the user the same sentences twice. A window this long cannot recur in prose by
 * accident, so a hit is a repaint defect and not a wordy model.
 *
 * The window alone cannot be diagnosed, so the return value says WHERE both
 * occurrences sit and what surrounds them — otherwise the failure is a scavenger
 * hunt through a transcript nobody kept.
 */
export function firstRepeatedWindow(text: string, size: number): string | null {
  if (text.length < size * 2) return null;
  const seen = new Map<string, number>();
  for (let i = 0; i + size <= text.length; i += 1) {
    const chunk = text.slice(i, i + size);
    const first = seen.get(chunk);
    if (first !== undefined) {
      return [
        `window=${JSON.stringify(chunk)}`,
        `first@${first}: ${JSON.stringify(text.slice(Math.max(0, first - 120), first + size + 120))}`,
        `second@${i}: ${JSON.stringify(text.slice(Math.max(0, i - 120), i + size + 120))}`,
      ].join('\n');
    }
    seen.set(chunk, i);
  }
  return null;
}

/**
 * No persisted assistant text block written twice.
 *
 * Stands BESIDE the rendered scan rather than replacing it: the two cover
 * separate writers. The page can replay a prefix it already painted; the worker
 * can materialize the same block twice into the durable message. Short duplicate
 * blocks also fall below the rendered sliding-window threshold.
 */
export function expectNoDuplicateTextBlocks(message: MessageRecord, label: string): void {
  const textBlocks = (message.blocks || [])
    .filter((block) => String(block.type || '') === 'text')
    .map((block) => String(block.text || block.content || '').trim())
    .filter((text) => text.length > 20);
  const duplicates = textBlocks.filter((text, index) => textBlocks.indexOf(text) !== index);
  expect(duplicates, `${label} should not duplicate persisted assistant text blocks`).toEqual([]);
}
