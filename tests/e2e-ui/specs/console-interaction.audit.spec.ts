/**
 * What the console does when it is TOUCHED — the half `visual-grammar` cannot
 * see, because every check there reads a page at rest.
 *
 * Three questions, all measured rather than modelled:
 *   · does every control do something
 *   · does a focus ring stay inside its own edges
 *   · does anything an ancestor clips lose a side of its indicator
 *
 * The activation sweep visits section and create pages, skipping disabled,
 * selected, unnamed, and destructive controls. It reloads after navigation,
 * an open overlay, or a click failure before inspecting the next control.
 *
 * "Nothing happened" means no DOM change, no URL change and no call to the
 * deployment. A Refresh over data that has not changed redraws the same bytes;
 * it is not dead, it just had nothing new to say.
 *
 * NOT focus: clicking a button focuses it, so a focus check is true for every
 * control and reports a page full of dead buttons as clean.
 *
 * One pass, not eight: a control that does nothing does nothing at every width
 * and in either theme, so this is its own spec rather than another dimension of
 * the visual-grammar walk.
 */
import { test, expect, type Page, type Request } from '@playwright/test';

import { appPath, refuseIfNotTheDeployment } from '../fixtures/env';
import { blocksConsoleSettlement } from '../fixtures/requestReadiness';
import { findClippedDecorations } from '../fixtures/clippedDecorations';

const SELECTOR = 'button:visible, [role=button]:visible';
const DESTRUCTIVE = /delete|remove|kill|archive|hibernate|stop|revoke|discard|删除|归档/i;

async function gotoSettled(page: Page, url: string) {
  // Age requests so a missing terminal event cannot hold settlement open
  // indefinitely. Entries age out at STALE_MS regardless of completion;
  // the loading-state assertion below checks rendered readiness separately.
  const pending = new Map<Request, number>();
  const STALE_MS = 10_000;
  let lastActivity = Date.now();
  const inFlight = () =>
    [...pending].filter(([, at]) => Date.now() - at < STALE_MS);
  const starts = (request: Request) => {
    if (!blocksConsoleSettlement(request.url(), request.method())) return;
    pending.set(request, Date.now());
    lastActivity = Date.now();
  };
  const finishes = (request: Request) => {
    if (!pending.delete(request)) return;
    lastActivity = Date.now();
  };

  page.on('request', starts);
  page.on('requestfinished', finishes);
  page.on('requestfailed', finishes);
  try {
    await page.goto(url);
    await page.locator('nav a[href^="/manage/"]').first().waitFor({ state: 'visible' });
    lastActivity = Date.now();
    try {
      await expect
        .poll(() => inFlight().length === 0 && Date.now() - lastActivity >= 150, {
          // Allow related-resource requests to start and settle within this
          // total budget; STALE_MS separately limits each tracked request.
          timeout: 25_000,
          intervals: [50],
        })
        .toBe(true);
    } catch {
      // Include the route and unexpired requests to distinguish a blocked
      // request from activity that repeatedly resets the quiet window.
      const names = inFlight()
        .map(([request]) => `${request.method()} ${new URL(request.url()).pathname}`)
        .join(', ');
      throw new Error(
        `console API requests did not settle for ${new URL(page.url()).pathname}; `
        + `in flight: ${names || '(none, so the quiet window never opened)'}`,
      );
    }
    // Give rendered loading states their own budget after request settlement,
    // and name the route if related-resource loading does not finish.
    await expect(
      page.locator('[data-slot="loading-state"]'),
      `${new URL(page.url()).pathname} was still loading`,
    ).toHaveCount(0, { timeout: 25_000 });
  } finally {
    page.off('request', starts);
    page.off('requestfinished', finishes);
    page.off('requestfailed', finishes);
  }
}

const snapshot = (page: Page) =>
  page.evaluate(() => ({
    url: window.location.pathname + window.location.search,
    html: document.body.innerHTML.length,
    text: document.body.innerText.length,
  }));

/** Every console route the rail offers, plus the create page behind each. */
async function routes(page: Page): Promise<string[]> {
  const sections = await page.evaluate(() => [
    ...new Set(
      [...document.querySelectorAll<HTMLAnchorElement>('nav a[href^="/manage/"]')].map(
        (a) => a.getAttribute('href') || '',
      ),
    ),
  ]);
  return sections.flatMap((s) => [s, `${s}/new`]);
}

// Before anything is measured, prove this product is behind the page. The walk
// itself cannot tell: it drives a frontend, and a frontend renders whatever it
// is given.
test.beforeAll(async ({ request }) => {
  await refuseIfNotTheDeployment(async (url, headers) => ({
    status: (await request.get(url, { failOnStatusCode: false, headers })).status(),
  }));
});

/**
 * Add the first actionable record from each selected section to the route list.
 * Record pages provide controls absent from list and create pages, including
 * back links and destructive actions.
 *
 * Only the checks that READ use this. The sweep that activates every control
 * keeps to sections and create pages on purpose: a record page's controls
 * delete the record.
 */
async function routesWithRecords(
  page: Page,
  routeFilter: (all: string[]) => string[] = (all) => all,
): Promise<string[]> {
  const walk: string[] = [];
  for (const route of routeFilter(await routes(page))) {
    walk.push(route);
    if (route.endsWith('/new')) continue;
    await gotoSettled(page, appPath(route));
    const row = page.locator('tr[tabindex], [role="row"][tabindex]').first();
    if (!(await row.count())) continue;
    await row.click();
    await page.waitForTimeout(800);
    const landed = await page.evaluate(() => window.location.pathname);
    if (landed !== route && !walk.includes(landed)) walk.push(landed);
  }
  return walk;
}

// Split configure and operate routes at the sessions section in the rail.
// routesWithRecords applies the selected partition before record discovery,
// so discovery and inspection both share the per-test work limit.
const OPERATE_BOUNDARY = '/manage/sessions';
const configureRoutes = (all: string[]) => {
  const cut = all.findIndex((route) => route.startsWith(OPERATE_BOUNDARY));
  return cut < 0 ? all : all.slice(0, cut);
};
const operateRoutes = (all: string[]) => {
  const cut = all.findIndex((route) => route.startsWith(OPERATE_BOUNDARY));
  return cut < 0 ? [] : all.slice(cut);
};
// Halve each group to distribute route visits across the runner's per-test
// budget. Both halves retain identical per-route checks and cover the group.
const operateFirstHalf = (all: string[]) => {
  const section = operateRoutes(all);
  return section.slice(0, Math.ceil(section.length / 2));
};
const operateSecondHalf = (all: string[]) => {
  const section = operateRoutes(all);
  return section.slice(Math.ceil(section.length / 2));
};
const configureFirstHalf = (all: string[]) => {
  const section = configureRoutes(all);
  return section.slice(0, Math.ceil(section.length / 2));
};
const configureSecondHalf = (all: string[]) => {
  const section = configureRoutes(all);
  return section.slice(Math.ceil(section.length / 2));
};

async function sweepEveryControl(page: Page, routeFilter: (routes: string[]) => string[]) {
  await page.goto(appPath(process.env.ASTRABOX_UI_AUDIT_CONSOLE_ENTRY || '/manage/agents'));
  await expect(page.locator('nav a[href^="/manage/"]').first()).toBeVisible();

  const selectedRoutes = routeFilter(await routes(page));
  expect(selectedRoutes, 'the console partition selected no routes').not.toEqual([]);
  const dead: string[] = [];
  const auditedRoutes: string[] = [];
  const checkedRoutes = new Set<string>();
  let checked = 0;

  for (const route of selectedRoutes) {
    await gotoSettled(page, appPath(route));
    // A section with no create page answers /new with a record page looking for
    // a record called "new"; its controls belong to the record page and are
    // swept there.
    if (route.endsWith('/new') && !(await page.locator('[data-slot="create-page"]').count())) {
      continue;
    }
    auditedRoutes.push(route);
    const total = await page.locator(SELECTOR).count();

    // Reuse the section after an in-page change to limit reload cost. Reset
    // after navigation, an open overlay, or a click failure before continuing.
    let needsReset = false;
    for (let i = 0; i < total; i++) {
      if (needsReset) {
        await gotoSettled(page, appPath(route));
        needsReset = false;
      }
      const control = page.locator(SELECTOR).nth(i);
      if (!(await control.count())) continue;
      if (await control.isDisabled().catch(() => true)) continue;
      const already = await control
        .evaluate((el) =>
          ['aria-pressed', 'aria-selected', 'aria-checked'].some(
            (a) => el.getAttribute(a) === 'true',
          ),
        )
        .catch(() => false);
      if (already) continue;
      const name = (
        (await control.getAttribute('aria-label').catch(() => '')) ||
        (await control.innerText().catch(() => '')) ||
        ''
      )
        .trim()
        .replace(/\s+/g, ' ')
        .slice(0, 30);
      if (!name || DESTRUCTIVE.test(name)) continue;

      let calls = 0;
      const count = (r: { url(): string }) => {
        if (r.url().includes('/api/')) calls += 1;
      };
      page.on('request', count);
      const before = await snapshot(page);
      try {
        await control.click({ timeout: 2500 });
      } catch {
        page.off('request', count);
        // An unclickable control means something covers the section page;
        // the next judgement needs a reset.
        needsReset = true;
        continue;
      }
      const observationDeadline = Date.now() + 700;
      let after = await snapshot(page);
      while (
        before.url === after.url
        && before.html === after.html
        && before.text === after.text
        && calls === 0
        && Date.now() < observationDeadline
      ) {
        await page.waitForTimeout(Math.min(50, observationDeadline - Date.now()));
        after = await snapshot(page);
      }
      page.off('request', count);
      checked += 1;
      checkedRoutes.add(route);

      const moved =
        before.url !== after.url ||
        before.html !== after.html ||
        before.text !== after.text ||
        calls > 0;
      if (!moved) dead.push(`${route} \u00b7 "${name}"`);
      const overlayOpen = await page
        .locator('[role="dialog"]:visible, [role="alertdialog"]:visible, [data-state="open"][data-slot*="sheet"]')
        .count()
        .catch(() => 1);
      needsReset = before.url !== after.url || overlayOpen > 0;
    }
  }

  // eslint-disable-next-line no-console
  console.log(`dead-controls: ${checked} controls activated`);
  expect(auditedRoutes, 'the console partition had no auditable routes').not.toEqual([]);
  expect(
    [...checkedRoutes].sort(),
    'every auditable console route must activate at least one control',
  ).toEqual([...auditedRoutes].sort());
  expect(
    dead,
    'a control that changes no pixel, no URL and makes no call is telling the ' +
      'reader it does something it does not',
  ).toEqual([]);
}

test('every configure-section control does something (first half)', async ({ page }) => {
  await sweepEveryControl(page, configureFirstHalf);
});

test('every configure-section control does something (second half)', async ({ page }) => {
  await sweepEveryControl(page, configureSecondHalf);
});

test('every operate-section control does something (first half)', async ({ page }) => {
  await sweepEveryControl(page, operateFirstHalf);
});

test('every operate-section control does something (second half)', async ({ page }) => {
  await sweepEveryControl(page, operateSecondHalf);
});

/**
 * A focus ring paints where it belongs to.
 *
 * Rows can touch without a gap. An outward focus ring can then paint over an
 * adjacent row and make the focused control ambiguous.
 *
 * The same walk answers a second question about the same ring: whether it can
 * be seen where it lands. A ring is one colour against whatever ground the
 * element happens to sit on, and the accent ring against an accent-tinted
 * selected row is a ring nobody can find. WCAG 1.4.11 puts the floor for a
 * control's own boundary at 3:1, so that is the floor here.
 *
 * Read computed styles because the cascade determines the final offset and
 * an ancestor can supply the background. Tab traversal exercises keyboard
 * focus indicators; each component paint variant is measured once per surface.
 */
/**
 * Up to six conversations in the order their links appear in the rail.
 *
 * Sampling several sessions can include controls absent from an empty one,
 * such as reasoning rows, tool cards, and subagent panels. Their coverage
 * depends on which conversations are present in the rail.
 */
async function conversationSurfaces(page: Page): Promise<string[]> {
  await gotoSettled(page, appPath('/'));
  return page.evaluate(() =>
    [...document.querySelectorAll<HTMLAnchorElement>('a[href^="/sessions/"]')]
      .map((a) => a.getAttribute('href') || '')
      .slice(0, 6),
  );
}

function focusSnapshot(includeCandidates: boolean) {
  const pathOf = (el: Element) => {
    const path: number[] = [];
    for (let node: Element | null = el; node?.parentElement; node = node.parentElement) {
      path.push([...node.parentElement.children].indexOf(node));
    }
    return path.reverse().join('.');
  };
  const candidates = includeCandidates
    ? [...document.querySelectorAll<HTMLElement>('*')]
        .filter(
          (el) =>
            el.tabIndex >= 0 &&
            !el.matches(':disabled') &&
            el.getClientRects().length > 0 &&
            getComputedStyle(el).visibility !== 'hidden',
        )
        .map((el) => ({
          key: `${(el.className || '').toString().slice(0, 34)}|${el.tagName}`,
          path: pathOf(el),
        }))
    : [];
  const el = document.activeElement;
  const focused =
    !el || el === document.body || !el.matches(':focus-visible')
      ? null
      : {
          key: `${(el.className || '').toString().slice(0, 34)}|${el.tagName}`,
          path: pathOf(el),
        };
  return { candidates, focused };
}

async function walkFocusRings(
  page: Page,
  routeFilter: (all: string[]) => string[],
  extraSurfaces: string[] = [],
) {
  await page.goto(appPath(process.env.ASTRABOX_UI_AUDIT_CONSOLE_ENTRY || '/manage/agents'));
  await expect(page.locator('nav a[href^="/manage/"]').first()).toBeVisible();

  const bleeding: string[] = [];
  const sliced: string[] = [];
  const invisible: string[] = [];
  let stops = 0;
  let variants = 0;

  for (const route of [...(await routesWithRecords(page, routeFilter)), ...extraSurfaces]) {
    await gotoSettled(page, appPath(route));
    if (route.endsWith('/new') && !(await page.locator('[data-slot="create-page"]').count())) {
      continue;
    }
    const seen = new Set<string>();
    const visited = new Set<string>();
    let focusAuditCompleted = false;
    const initial = await page.evaluate(focusSnapshot, true);
    const knownCandidates = new Set(initial.candidates.map(({ path }) => path));
    const knownVariants = new Set(initial.candidates.map(({ key }) => key));
    let focusStepLimit = knownCandidates.size + 2;
    for (let i = 0; i < focusStepLimit; i++) {
      await page.keyboard.press('Tab');
      // Re-enumerating a page with hundreds of rows on every Tab is quadratic.
      // A focused path is direct evidence of one new candidate; scan the whole
      // DOM again only when the observed order is about to exhaust its bound.
      const snapshot = await page.evaluate(focusSnapshot, i + 2 >= focusStepLimit);
      for (const candidate of snapshot.candidates) {
        knownCandidates.add(candidate.path);
        knownVariants.add(candidate.key);
      }
      const { focused } = snapshot;
      if (focused === null) {
        if (visited.size) {
          focusAuditCompleted = true;
          break;
        }
        continue;
      }
      knownCandidates.add(focused.path);
      knownVariants.add(focused.key);
      focusStepLimit = Math.max(focusStepLimit, knownCandidates.size + 2);
      if (visited.has(focused.path)) {
        focusAuditCompleted = true;
        break;
      }
      visited.add(focused.path);
      stops += 1;
      // The finding oracle has always kept only the first result for this key.
      // Skip before its transition wait and colour/geometry work, rather than
      // compute and discard the same table-row result dozens of times.
      if (seen.has(focused.key)) continue;
      const at = await page.evaluate(async () => {
        const el = document.activeElement;
        if (!el || el === document.body || !el.matches(':focus-visible')) return null;
        // A ring that fades in is not the ring until it has arrived. Reading
        // the frame after Tab caught the settings button at alpha 0.516 and
        // the select with its glow still at zero — two findings that were
        // both this, and neither of which a reader would ever see. Transitions
        // are animations, so the element can say when it has finished — but
        // only transitions: a loading skeleton shimmers forever, and waiting
        // on THAT to finish spends the whole timeout on every stop of a page
        // that happens to be loading.
        const settled = (node: Element) =>
          Promise.race([
            Promise.all(
              node
                .getAnimations()
                .filter((a) => a instanceof CSSTransition)
                .map((a) => a.finished.catch(() => undefined)),
            ),
            new Promise((resolve) => {
              setTimeout(resolve, 400);
            }),
          ]);
        await settled(el);
        const cs = getComputedStyle(el);
        // A canvas is the only resolver that answers in channels. Computed
        // style hands back whatever notation the author wrote — these themes
        // are oklch, and colour-mix stays colour-mix — so parsing the string
        // means writing a colour engine. Painting one pixel asks the one that
        // is already there.
        const probe = document.createElement('canvas').getContext('2d', {
          willReadFrequently: true,
        });
        const rgba = (colour: string): [number, number, number, number] => {
          if (!probe) return [0, 0, 0, 0];
          probe.clearRect(0, 0, 1, 1);
          probe.fillStyle = colour;
          probe.fillRect(0, 0, 1, 1);
          const [r, g, b, a] = probe.getImageData(0, 0, 1, 1).data;
          return [r, g, b, a / 255];
        };
        const over = (
          top: [number, number, number, number],
          under: [number, number, number],
        ): [number, number, number] => [
          top[0] * top[3] + under[0] * (1 - top[3]),
          top[1] * top[3] + under[1] * (1 - top[3]),
          top[2] * top[3] + under[2] * (1 - top[3]),
        ];
        const luminance = ([r, g, b]: [number, number, number]) => {
          const lin = (c: number) => {
            const v = c / 255;
            return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
          };
          return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
        };
        // The ring is painted outside the border box, so its ground is the
        // element's surroundings, not its own fill: the nearest ancestor that
        // paints anything, with every translucent layer above it composited
        // back down in the order they stack.
        const layers: [number, number, number, number][] = [];
        for (let node = el.parentElement; node; node = node.parentElement) {
          const c = rgba(getComputedStyle(node).backgroundColor);
          if (c[3] === 0) continue;
          layers.push(c);
          if (c[3] === 1) break;
        }
        let ground: [number, number, number] = [255, 255, 255];
        for (let i = layers.length - 1; i >= 0; i -= 1) ground = over(layers[i], ground);
        // A ring is not always an outline, and rarely only one layer.
        // Twenty controls here carry shadcn's `focus-visible:ring-*`, which
        // paints a box-shadow; Chrome composes those into a list whose first
        // entry is the transparent placeholder Tailwind reserves. Reading only
        // the first layer therefore reads a fully transparent colour, which
        // over the ground IS the ground — a perfect 1:1 that looks like the
        // worst defect on the page while hiding the real ring sitting behind
        // it. So every painted layer is measured and the strongest one stands
        // for the indicator: a ring is findable if any part of it is.
        const candidates: [number, number, number, number][] = [];
        if (cs.outlineStyle !== 'none') candidates.push(rgba(cs.outlineColor));
        if (cs.boxShadow && cs.boxShadow !== 'none') {
          for (const layer of cs.boxShadow.matchAll(
            /(?:rgba?|oklab|oklch|hsla?|color)\([^)]*\)|#[0-9a-f]{3,8}/gi,
          )) {
            candidates.push(rgba(layer[0]));
          }
        }
        const painted = candidates.filter((c) => c[3] > 0);
        let shadowReach = 0;
        if (cs.boxShadow && cs.boxShadow !== 'none') {
          for (const layer of cs.boxShadow.split(/,(?![^(]*\))/)) {
            if (layer.includes('inset')) continue;
            const lengths = [...layer.matchAll(/(-?\d+(?:\.\d+)?)px/g)].map((m) => Number(m[1]));
            if (lengths.length < 2) continue;
            const [, offsetY, blur = 0, spread = 0] = lengths;
            shadowReach = Math.max(shadowReach, Math.abs(offsetY) + blur + spread);
          }
        }
        // Nothing painted is not the same as nothing wrong — it is the worse
        // half, and a check that only rates the rings it finds waves it
        // through. Some controls legitimately signal with a border instead of
        // a ring, so the question is not "is there a ring" but "does focus
        // change anything at all": read the element's paint, drop focus, read
        // it again. Only on the suspicious branch, because it costs a blur.
        //
        // Two levels up, because the paint is not always on the element that
        // took the focus: a field wrapped in an input group rings the WRAPPER
        // through `has-[…]:ring`, and asking only the input whether anything
        // changed gets a truthful no for a control the reader can plainly see
        // light up.
        // What is PAINTED, not what is declared. An outline whose style is
        // `none` still carries a colour, and that colour changes on blur —
        // from `currentColor` to the token the base layer sets — so comparing
        // declarations reports a difference for a ring that is not drawn, and
        // a control showing nothing at all passes. Reading only what renders
        // is what makes the comparison mean what it says.
        const signature = () => {
          const parts: string[] = [];
          let node: Element | null = el;
          for (let up = 0; up < 3 && node; up += 1, node = node.parentElement) {
            const c = getComputedStyle(node);
            parts.push(
              c.outlineStyle === 'none'
                ? 'no-outline'
                : `${c.outlineStyle} ${c.outlineWidth} ${c.outlineColor} ${c.outlineOffset}`,
              c.boxShadow,
              c.borderStyle === 'none' ? 'no-border' : `${c.borderWidth} ${c.borderColor}`,
              c.backgroundColor,
              c.color,
              c.textDecorationLine,
            );
          }
          return parts.join('|');
        };
        let unmarked = false;
        if (!painted.length && el instanceof HTMLElement) {
          const focused = signature();
          el.blur();
          await settled(el);
          unmarked = signature() === focused;
          // Restore it, or the walk loses its place and Tab starts over.
          el.focus();
        }
        const groundLum = luminance(ground);
        const ratio = (layer: [number, number, number, number]) => {
          const lum = luminance(over(layer, ground));
          return (Math.max(lum, groundLum) + 0.05) / (Math.min(lum, groundLum) + 0.05);
        };
        const contrast = painted.length ? Math.max(...painted.map(ratio)) : null;
        const rect = el.getBoundingClientRect();
        // The other half of §11's geometry. A ring reaching past its own edges
        // either lands on a neighbour or lands on nothing, and "nothing" is an
        // ancestor that clips: the reach is simply not painted, so the reader
        // sees an indicator with a side sliced off. Only `hidden` and `clip`
        // count — a scroll container clipping what is scrolled away is the
        // whole point of one — and only for an element that is fully inside
        // the clip box already, so a row scrolled out of view is not a
        // finding about its ring.
        const outlineReach =
          cs.outlineStyle === 'none'
            ? 0
            : (Number.parseFloat(cs.outlineWidth) || 0) +
              (Number.parseFloat(cs.outlineOffset) || 0);
        const side = { left: outlineReach, right: outlineReach, top: outlineReach, bottom: outlineReach };
        if (cs.boxShadow && cs.boxShadow !== 'none') {
          for (const layer of cs.boxShadow.split(/,(?![^(]*\))/)) {
            if (layer.includes('inset')) continue;
            const lengths = [...layer.matchAll(/(-?\d+(?:\.\d+)?)px/g)].map((m) => Number(m[1]));
            if (lengths.length < 2) continue;
            const [offsetX, offsetY, blur = 0, spread = 0] = lengths;
            side.left = Math.max(side.left, blur + spread - offsetX);
            side.right = Math.max(side.right, blur + spread + offsetX);
            side.top = Math.max(side.top, blur + spread - offsetY);
            side.bottom = Math.max(side.bottom, blur + spread + offsetY);
          }
        }
        const clipped: string[] = [];
        for (let node = el.parentElement; node; node = node.parentElement) {
          const ancestor = getComputedStyle(node);
          const clipsX = ancestor.overflowX === 'hidden' || ancestor.overflowX === 'clip';
          const clipsY = ancestor.overflowY === 'hidden' || ancestor.overflowY === 'clip';
          if (!clipsX && !clipsY) continue;
          const margin =
            ancestor.overflow === 'clip' || ancestor.overflowX === 'clip' || ancestor.overflowY === 'clip'
              ? Number.parseFloat(ancestor.overflowClipMargin) || 0
              : 0;
          const box = node.getBoundingClientRect();
          if (rect.left < box.left || rect.right > box.right) continue;
          if (rect.top < box.top || rect.bottom > box.bottom) continue;
          const room = {
            left: rect.left - box.left + margin,
            right: box.right - rect.right + margin,
            top: rect.top - box.top + margin,
            bottom: box.bottom - rect.bottom + margin,
          };
          for (const edge of ['left', 'right', 'top', 'bottom'] as const) {
            const axisClips = edge === 'left' || edge === 'right' ? clipsX : clipsY;
            if (!axisClips) continue;
            if (side[edge] > room[edge] + 0.5) {
              clipped.push(
                `${edge} ${Math.round(side[edge])}px into ${Math.round(room[edge])}px`,
              );
            }
          }
          break;
        }
        const gaps: number[] = [];
        for (const sib of [el.previousElementSibling, el.nextElementSibling]) {
          const s = sib?.getBoundingClientRect();
          if (!s || s.width < 2 || s.height < 2) continue;
          const gap =
            s.top >= rect.bottom
              ? s.top - rect.bottom
              : rect.top >= s.bottom
                ? rect.top - s.bottom
                : null;
          if (gap !== null) gaps.push(gap);
        }
        return {
          key: `${(el.className || '').toString().slice(0, 34)}|${el.tagName}`,
          // Where the ring's far edge lands, relative to the border box —
          // over both forms it can take. A shadcn control rings with a 3px
          // box-shadow and no outline at all, so an outline-only reading of
          // the geometry gives every one of them a clean 0.
          reach: Math.max(
            cs.outlineStyle === 'none'
              ? 0
              : (Number.parseFloat(cs.outlineWidth) || 0) +
                (Number.parseFloat(cs.outlineOffset) || 0),
            shadowReach,
          ),
          gaps,
          clipped,
          contrast: contrast === null ? null : Math.round(contrast * 100) / 100,
          ground: `rgb(${ground.map((c) => Math.round(c)).join(',')})`,
          unmarked,
        };
      });
      if (!at) continue;
      seen.add(focused.key);
      variants += 1;
      for (const gap of at.gaps) {
        if (at.reach > gap) {
          bleeding.push(`${route} · ${at.key} reaches ${at.reach}px into a ${gap}px gap`);
        }
      }
      for (const cut of at.clipped) {
        sliced.push(`${route} · ${at.key} ring is cut ${cut}`);
      }
      if (at.unmarked) {
        invisible.push(`${route} · ${at.key} looks identical focused and unfocused`);
      }
      if (at.contrast !== null && at.contrast < 3) {
        invisible.push(
          `${route} · ${at.key} ring is ${at.contrast}:1 against ${at.ground}`,
        );
      }

      // The oracle measures one instance of each DOM-observed paint variant.
      // Once all are seen, rescan because focusing a control can reveal another
      // one; if the set stays covered, later Tab stops are duplicate instances.
      if ([...knownVariants].every((key) => seen.has(key))) {
        const confirmation = await page.evaluate(focusSnapshot, true);
        for (const candidate of confirmation.candidates) {
          knownCandidates.add(candidate.path);
          knownVariants.add(candidate.key);
        }
        if ([...knownVariants].every((key) => seen.has(key))) {
          focusAuditCompleted = true;
          break;
        }
      }
    }
    expect(
      focusAuditCompleted,
      `${route} did not cover every DOM-observed focus variant within ${focusStepLimit} steps`,
    ).toBe(true);
  }

  // eslint-disable-next-line no-console
  console.log(`focus-ring: ${stops} focus stops traversed; ${variants} ring variants measured`);
  expect(stops, 'nothing took keyboard focus — nothing was measured').toBeGreaterThan(10);
  expect(variants, 'no focus-ring variant was measured').toBeGreaterThan(10);
  expect(
    [...new Set(bleeding)],
    'a ring drawn past its own edge into a neighbour reads as the neighbour\u2019s',
  ).toEqual([]);
  expect(
    [...new Set(sliced)],
    'a ring an ancestor clips is an indicator with a side missing',
  ).toEqual([]);
  expect(
    [...new Set(invisible)],
    'a ring below 3:1 against what it sits on cannot be found by the reader it is for',
  ).toEqual([]);
}

test('a focus ring is visible and contained across the configure sections (first half)', async ({ page }) => {
  await walkFocusRings(page, configureFirstHalf);
});

test('a focus ring is visible and contained across the configure sections (second half)', async ({ page }) => {
  await walkFocusRings(page, configureSecondHalf);
});

// The conversation surface rides with the operate half. Its controls are the
// ones no manage route has — the reasoning trigger that fills its card, the
// subagent rows in the right panel — and each of them sits inside a box that
// clips, which is the geometry the third question above asks about.
test('a focus ring is visible and contained across the operate sections (first half)', async ({ page }) => {
  await walkFocusRings(page, operateFirstHalf);
});

// The conversation surfaces ride with the second half, so the pair still
// covers every route and every extra surface between them.
test('a focus ring is visible and contained across the operate sections (second half)', async ({ page }) => {
  await walkFocusRings(page, operateSecondHalf, await conversationSurfaces(page));
});

/**
 * Anything that looks pressable is a control.
 *
 * `cursor: pointer` is the promise a page makes to a reader — this responds to
 * being pressed. An element that makes it without being a button, a link or
 * anything focusable cannot be reached by Tab and is announced as nothing, and
 * the sweep above walks straight past it because that sweep enumerates
 * buttons.
 *
 * Not `[onclick]`: React attaches handlers through its own event system, so a
 * JSX `onClick` puts no attribute in the DOM and a selector for one matches
 * nothing at all. The cursor is the part that reaches the DOM, and it is also
 * the part the reader sees.
 */
// Split the complete route list into equal-sized halves to distribute visits
// across the per-test budget, independent of the configure/operate boundary.
const evenFirstHalf = (all: string[]) => all.slice(0, Math.ceil(all.length / 2));
const evenSecondHalf = (all: string[]) => all.slice(Math.ceil(all.length / 2));
for (const half of [
  { name: 'first half', pick: evenFirstHalf },
  { name: 'second half', pick: evenSecondHalf },
] as const) {
test(`anything that looks pressable can be reached by a keyboard (${half.name})`, async ({ page }) => {
  const FOCUSABLE =
    // `summary` is a native keyboard control even without an explicit role or
    // tabindex: Chromium exposes tabIndex=0 and Enter toggles its details.
    // Keep native controls in the oracle instead of forcing redundant ARIA
    // onto otherwise-correct product markup.
    'button, a[href], input, select, textarea, label, summary, [role=button], [role=link], [role=tab], [role=menuitem], [tabindex]';

  await page.goto(appPath(process.env.ASTRABOX_UI_AUDIT_CONSOLE_ENTRY || '/manage/agents'));
  await expect(page.locator('nav a[href^="/manage/"]').first()).toBeVisible();
  const surfaces = await routesWithRecords(page, half.pick);

  // Sample up to six conversations from the rail: a single empty session
  // cannot exercise reasoning rows, tool rows, or result cards. Coverage
  // depends on the content of the selected conversations.
  await gotoSettled(page, appPath('/'));
  const conversations = await page.evaluate(() =>
    [...document.querySelectorAll<HTMLAnchorElement>('a[href^="/sessions/"]')]
      .map((a) => a.getAttribute('href') || '')
      .slice(0, 6),
  );
  surfaces.push(...conversations);

  const unreachable: string[] = [];
  let looked = 0;

  for (const route of surfaces) {
    await gotoSettled(page, appPath(route));
    if (route.endsWith('/new') && !(await page.locator('[data-slot="create-page"]').count())) {
      continue;
    }
    looked += 1;
    const hits = await page.evaluate((focusable) => {
      const out: string[] = [];
      for (const el of document.querySelectorAll('*')) {
        if (getComputedStyle(el).cursor !== 'pointer') continue;
        if (el.matches(focusable) || el.closest(focusable)) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width < 6 || rect.height < 6) continue;
        out.push(
          `${el.tagName}.${(el.className || '').toString().slice(0, 34)} "${(el.textContent || '')
            .trim()
            .slice(0, 20)}"`,
        );
      }
      return [...new Set(out)];
    }, FOCUSABLE);
    for (const hit of hits) unreachable.push(`${route} · ${hit}`);
  }

  // eslint-disable-next-line no-console
  console.log(`pressable: ${looked} surfaces inspected`);
  expect(looked, 'no surface was inspected').toBeGreaterThan(5);
  expect(
    [...new Set(unreachable)],
    'a page that offers the pointer and no keyboard path is offering it to some readers only',
  ).toEqual([]);
});
}

/**
 * A hover paints inside its own edges.
 *
 * A hover shadow that reaches into an adjacent item's box can make the two
 * items appear selected together. Compare its reach with the rendered gap.
 *
 * Measured, not modelled, for the same reason the focus ring is: how far a
 * shadow reaches is `|offset| + blur + spread`, and whether that matters
 * depends on the gap to the neighbour — a number the stylesheet does not hold.
 * So this hovers for real and reads what the element computes to.
 */
async function walkHovers(
  page: Page,
  routeFilter: (all: string[]) => string[],
  extraSurfaces: string[] = [],
) {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(appPath(process.env.ASTRABOX_UI_AUDIT_CONSOLE_ENTRY || '/manage/agents'));
  await expect(page.locator('nav a[href^="/manage/"]').first()).toBeVisible();

  const surfaces = [...(await routesWithRecords(page, routeFilter)), ...extraSurfaces];
  const bleeding: string[] = [];
  let hovered = 0;

  for (const route of surfaces) {
    await gotoSettled(page, appPath(route));
    const targets = page.locator(
      'nav a:visible, tr[tabindex]:visible, [role="row"][tabindex]:visible, a[href^="/sessions/"]:visible',
    );
    const total = Math.min(await targets.count(), 14);
    for (let i = 0; i < total; i += 1) {
      const target = targets.nth(i);
      try {
        await target.hover({ timeout: 1500 });
      } catch {
        continue;
      }
      await page.waitForTimeout(120);
      hovered += 1;
      const bleed = await target.evaluate((node) => {
        const shadow = getComputedStyle(node).boxShadow;
        if (!shadow || shadow === 'none') return null;
        let reach = 0;
        for (const layer of shadow.matchAll(
          /(-?\d+(?:\.\d+)?)px\s+(-?\d+(?:\.\d+)?)px(?:\s+(-?\d+(?:\.\d+)?)px)?(?:\s+(-?\d+(?:\.\d+)?)px)?/g,
        )) {
          const [, , offsetY, blur, spread] = layer;
          reach = Math.max(reach, Math.abs(Number(offsetY)) + (Number(blur) || 0) + (Number(spread) || 0));
        }
        const rect = node.getBoundingClientRect();
        for (const sib of [node.previousElementSibling, node.nextElementSibling]) {
          const s = sib?.getBoundingClientRect();
          if (!s || s.height < 2) continue;
          const gap =
            s.top >= rect.bottom
              ? s.top - rect.bottom
              : rect.top >= s.bottom
                ? rect.top - s.bottom
                : null;
          if (gap !== null && reach > gap) {
            return `${(node.className || '').toString().slice(0, 30)} reaches ${
              Math.round(reach * 10) / 10
            }px into a ${gap}px gap`;
          }
        }
        return null;
      });
      if (bleed) bleeding.push(`${route} \u00b7 ${bleed}`);
    }
  }

  // eslint-disable-next-line no-console
  console.log(`hover: ${hovered} elements hovered`);
  expect(hovered, 'nothing was hovered — nothing was measured').toBeGreaterThan(10);
  expect(
    [...new Set(bleeding)],
    'a hover that paints past its own edge runs together with its neighbour',
  ).toEqual([]);
}

test('a hover paints inside its own edges across the configure sections (first half)', async ({ page }) => {
  await walkHovers(page, configureFirstHalf);
});

test('a hover paints inside its own edges across the configure sections (second half)', async ({ page }) => {
  await walkHovers(page, configureSecondHalf);
});

// The app home rides with the operate half: its session rail is the hover
// surface the operate group's records open from.
test('a hover paints inside its own edges across the operate sections (first half)', async ({ page }) => {
  await walkHovers(page, operateFirstHalf);
});

test('a hover paints inside its own edges across the operate sections (second half)', async ({ page }) => {
  await walkHovers(page, operateSecondHalf, ['/']);
});

/**
 * An indicator is painted whole, or it is not the indicator.
 *
 * §11's geometry has two failure directions and the focus walk above only
 * covers one. A ring or a halo that reaches past the border box either lands on
 * a neighbour — read as the neighbour's — or lands inside an ancestor that
 * clips, where it is not painted at all. The reported shape of the second was
 * the live dot in front of "Generating": a 6px dot with a 7px halo against the
 * left edge of a message body that clipped, so the halo read as bitten off.
 *
 * This walk is not the focus walk because the decorations it looks for take no
 * focus: a pulsing dot is painted whether or not anybody tabs to it, so nothing
 * a keyboard does would ever visit one.
 */
async function walkIndicators(
  page: Page,
  routeFilter: (all: string[]) => string[],
  extraSurfaces: string[] = [],
) {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(appPath(process.env.ASTRABOX_UI_AUDIT_CONSOLE_ENTRY || '/manage/agents'));
  await expect(page.locator('nav a[href^="/manage/"]').first()).toBeVisible();

  const surfaces = [...(await routesWithRecords(page, routeFilter)), ...extraSurfaces];

  const sliced: string[] = [];
  let looked = 0;
  for (const route of surfaces) {
    await gotoSettled(page, appPath(route));
    if (route.endsWith('/new') && !(await page.locator('[data-slot="create-page"]').count())) {
      continue;
    }
    looked += 1;
    for (const hit of await findClippedDecorations(page)) sliced.push(`${route} · ${hit}`);
  }

  // eslint-disable-next-line no-console
  console.log(`clipped-indicator: ${looked} surfaces scanned`);
  expect(looked, 'no surface was scanned').toBeGreaterThan(5);
  expect(
    [...new Set(sliced)],
    'an indicator an ancestor clips is drawn with a side missing',
  ).toEqual([]);
}

test('no indicator is clipped by an ancestor across configure sections', async ({ page }) => {
  await walkIndicators(page, configureRoutes);
});

test('no indicator is clipped by an ancestor across operate sections', async ({ page }) => {
  await walkIndicators(page, operateRoutes);
});

// Always-on conversation indicators — the run badge, status pill and reasoning
// row — do not exist on manage routes, so they retain their own bounded sweep.
test('no indicator is clipped by an ancestor across conversation surfaces', async ({ page }) => {
  await walkIndicators(page, () => [], await conversationSurfaces(page));
});
