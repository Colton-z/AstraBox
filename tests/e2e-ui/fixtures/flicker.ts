/**
 * A state that is shown and taken back before anyone could read it.
 *
 * Each of these is small on its own: a header that says Ready for two frames
 * between Sending and Creating, a status line that empties for half a second
 * between two steps, a pulse that mounts for 50ms. Together they are what makes
 * a page look unsure of itself. None of them fails anything, and none can be
 * seen in a screenshot or by an assertion that waits for the final state, so
 * they are recorded frame by frame while a journey runs and judged afterwards.
 *
 * A flicker is a value held for less than {@link FLICKER_MS} between two equal
 * neighbours: A, then B, then A again. Absence is a value, so something that
 * appears and is gone, or vanishes and comes back, is the same shape. A state
 * that moves on to a third value was a real step, however short.
 */
import type { Page } from '@playwright/test';

/** Shorter than this, a state between two equal neighbours was never read. */
export const FLICKER_MS = 400;

/** One state that came and went. */
export interface Flicker {
  /** Which surface: its test id or role, and the message it belongs to. */
  surface: string;
  /** `text` for what it says, `top` for where it sits. */
  what: 'text' | 'top';
  before: string;
  during: string;
  heldMs: number;
  /** Milliseconds after the recording started. */
  at: number;
}

declare global {
  interface Window {
    __flicker?: { stop: () => void; timeline: Map<string, Array<{ t: number; text: string; top: string }>> };
  }
}

/** Runs in the page: samples every status-bearing surface once per frame. */
const install = () => {
  window.__flicker?.stop();
  // What a reader takes as the page's account of its own state.
  const SURFACES = [
    '[data-testid="run-view"] header [data-testid="status-pill"]',
    '[role="status"]',
    '[role="alert"]',
    '[data-testid="response-pending"]',
    '[data-testid="assistant-process-trigger"]',
    '[data-testid="assistant-turn-process-trigger"]',
    '[data-testid="assistant-process-activity"]',
    '[data-testid="composer-submit"]',
    '[data-testid="run-composer-stop"]',
    '[data-testid="session-row"][data-active] [data-slot="sidebar-menu-button"]',
  ].join(', ');
  const timeline = new Map<string, Array<{ t: number; text: string; top: string }>>();
  const start = performance.now();
  let running = true;
  const tick = () => {
    if (!running) return;
    const now = Math.round(performance.now() - start);
    const seen = new Map<string, { text: string; top: string }>();
    const counts = new Map<string, number>();
    for (const el of Array.from(document.querySelectorAll<HTMLElement>(SURFACES))) {
      const box = el.getBoundingClientRect();
      // Mounted and unpainted is absent: a line still inside its appearance
      // delay shows nothing, whether the delay is its own or a wrapper's.
      if (!el.checkVisibility({ opacityProperty: true, visibilityProperty: true })
        || box.width < 1 || box.height < 1) continue;
      const name = el.getAttribute('data-testid') ?? el.getAttribute('role') ?? el.getAttribute('data-slot') ?? el.tagName;
      const scope = el.closest('[data-message-id]')?.getAttribute('data-message-id') ?? 'page';
      const base = `${name}@${scope}`;
      const index = counts.get(base) ?? 0;
      counts.set(base, index + 1);
      const label = el.getAttribute('aria-label') ?? '';
      seen.set(`${base}#${index}`, {
        // Counters and clocks advance; they are not what flickers.
        text: `${label}${(el.textContent ?? '').replace(/\d+/g, 'N').replace(/\s+/g, ' ').trim()}`.slice(0, 80),
        top: String(Math.round(box.top)),
      });
    }
    for (const key of new Set([...timeline.keys(), ...seen.keys()])) {
      const value = seen.get(key) ?? { text: '∅', top: '∅' };
      const frames = timeline.get(key) ?? [];
      const last = frames[frames.length - 1];
      if (!last || last.text !== value.text || last.top !== value.top) frames.push({ t: now, ...value });
      timeline.set(key, frames);
    }
    requestAnimationFrame(afterPaint);
  };
  // Read what the frame painted, not what it started from. An animation-frame
  // callback runs before the frame's layout and resize observers, so a list
  // that grows and is pinned back within one frame would be read mid-way, as
  // a jump nobody saw. A task queued from the callback runs once the frame is
  // on screen.
  const afterPaint = () => { setTimeout(tick, 0); };
  requestAnimationFrame(afterPaint);
  window.__flicker = { stop: () => { running = false; }, timeline };
};

/** Start recording the page as it stands. */
export async function startFlickerRecording(page: Page): Promise<void> {
  await page.evaluate(install);
}

/**
 * Record from the first frame of every document this page loads from now on:
 * what a reload paints before it settles is gone by the time a recording could
 * be started on it.
 */
export async function recordFlickerFromLoad(page: Page): Promise<void> {
  await page.addInitScript(install);
}

/** Stop recording and return every state that came and went. */
export async function stopFlickerRecording(page: Page): Promise<Flicker[]> {
  return page.evaluate((limit) => {
    const recording = window.__flicker;
    if (!recording) return [];
    recording.stop();
    const found: Array<{ surface: string; what: 'text' | 'top'; before: string; during: string; heldMs: number; at: number }> = [];
    for (const [surface, frames] of recording.timeline) {
      for (const what of ['text', 'top'] as const) {
        // One series per property: a change of place is not a change of words.
        const series: Array<{ t: number; value: string }> = [];
        for (const frame of frames) {
          if (series[series.length - 1]?.value !== frame[what]) series.push({ t: frame.t, value: frame[what] });
        }
        for (let i = 1; i < series.length - 1; i += 1) {
          const held = series[i + 1].t - series[i].t;
          if (held >= limit || series[i - 1].value !== series[i + 1].value) continue;
          // A surface scrolling by a pixel or two is layout settling, not a jump.
          if (what === 'top' && series[i].value !== '∅' && series[i - 1].value !== '∅'
            && Math.abs(Number(series[i].value) - Number(series[i - 1].value)) < 4) continue;
          found.push({ surface, what, before: series[i - 1].value, during: series[i].value, heldMs: held, at: series[i].t });
        }
      }
    }
    return found.sort((a, b) => a.at - b.at);
  }, FLICKER_MS);
}
