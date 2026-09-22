/**
 * E2E: the theme control repaints the page under the reader's hand.
 *
 * The suite's other theme coverage seeds `astrabox-theme` with `addInitScript`
 * before the first `goto` (visual-grammar.audit.spec.ts:810-818), so the
 * preference arrives at first paint and the SWITCH is never operated — that
 * audit opens the user menu only to measure label overflow, then presses Escape
 * (visual-grammar.audit.spec.ts:868-884). Everything between the press and the
 * paint is therefore reachable from nowhere else: the click landing on a Base UI
 * toggle that sits inside a menu popup as a non-menuitem child, `next-themes`
 * swapping the root class, the stored value, and the second mount of the same
 * control on the console.
 *
 * So this drives the control on both of its mounts — the app rail's account row
 * (App.tsx:363) and the console's compact Settings cog
 * (manage/ManageApp.tsx:284) — and asserts the effect on the OPEN page before
 * any reload, then that it is remembered, follows the reader across a page
 * switch, reverses from the other mount, and that System keeps following the
 * desktop with nothing else touched. That last one belongs to the staleness
 * family this campaign is about: a tab left open past sunset that catches up
 * only when somebody reloads it.
 *
 * Measured on real elements — `getComputedStyle` of `body` and of the rail's
 * painted inner (`data-slot="sidebar-inner"`, ui/sidebar.tsx:243-246), two
 * independent token families — not on the custom property, which reads back
 * whatever was set whether or not anything consumes it.
 *
 * No conversation, no Agent, no sandbox, no model call: the walk needs only the
 * signed-in cookie, so it runs unchanged under every matrix engine profile and
 * creates no server-side record to clean up. Its whole blast radius is this
 * browser context's localStorage.
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

import { appPath, refuseIfNotTheDeployment } from '../fixtures/env';

// The segment labels are localized (`shell:theme_dark` is "Dark" / "深色"), and
// the console detects language as ['localStorage','navigator'], so an unpinned
// runner locale is what decides which spelling is on screen. Pinned the way the
// lifecycle specs pin it: a fixed navigator locale plus the persisted
// 'astrabox-lang' the app's own switch writes.
//
// `colorScheme` is pinned for a second reason: "System" has to resolve to a
// value this spec knows, not to whatever the runner host happens to prefer. A
// walk that started under a dark desktop would find System already painting
// dark and would read the Dark press below as a no-op.
test.use({ locale: 'en-US', colorScheme: 'light' });

/** The three segments, in the order `UserMenu` declares them. */
const SEGMENTS = ['Light', 'Dark', 'System'] as const;

/** What the page is actually painting, read in one pass so it cannot tear. */
interface Paint {
  /** Every class on `<html>`, so a root carrying both marks or neither is legible. */
  rootClasses: string[];
  /** `documentElement.style.colorScheme` — what the browser paints form controls with. */
  colorScheme: string;
  bodyBackground: string;
  bodyColor: string;
  /** The rail's painted inner (`bg-sidebar`), a token family of its own. */
  sidebarBackground: string;
  /** The preference on disk, which is what survives a reload. */
  stored: string | null;
}

async function readPaint(page: Page): Promise<Paint> {
  return page.evaluate((): Paint => {
    const root = document.documentElement;
    const sidebar = document.querySelector('[data-slot="sidebar-inner"]');
    let stored: string | null = null;
    try {
      stored = window.localStorage.getItem('astrabox-theme');
    } catch {
      /* a context without storage still paints a theme; the read is the extra */
    }
    return {
      rootClasses: [...root.classList],
      colorScheme: root.style.colorScheme,
      bodyBackground: getComputedStyle(document.body).backgroundColor,
      bodyColor: getComputedStyle(document.body).color,
      sidebarBackground: sidebar ? getComputedStyle(sidebar).backgroundColor : '',
      stored,
    };
  });
}

/**
 * The theme the root is marked with, or a description of why it is not one.
 *
 * `next-themes` with `attribute="class"` removes both marks and adds the
 * resolved one, so exactly one is the only correct state. A root carrying both
 * or neither returns a string that matches no theme and names what it found —
 * a bare boolean would report that failure as "not dark" and send the reader
 * looking at the wrong half.
 */
function paintedTheme(paint: Paint): string {
  const marks = paint.rootClasses.filter((name) => name === 'light' || name === 'dark');
  if (marks.length === 1) return marks[0];
  return `<html class="${paint.rootClasses.join(' ')}">`;
}

/**
 * Relative luminance of a computed colour string (WCAG's formula).
 *
 * The point is the RELATION between background and text, not a pinned hex: a
 * palette edit must not turn this spec red, while "the class flipped and
 * nothing repainted" must. It throws on anything it cannot read rather than
 * returning zero, because a silent zero makes both comparisons below pass.
 */
function luminance(color: string): number {
  const parsed = /^rgba?\(([^)]+)\)$/.exec(color.trim());
  if (!parsed) {
    throw new Error(`computed colour ${JSON.stringify(color)} is not an rgb() the spec can read`);
  }
  const channels = parsed[1].split(/[\s,/]+/).filter(Boolean).slice(0, 3).map(Number);
  if (channels.length !== 3 || channels.some((value) => !Number.isFinite(value))) {
    throw new Error(`computed colour ${JSON.stringify(color)} has no three channels`);
  }
  const [r, g, b] = channels.map((channel) => {
    const unit = channel / 255;
    return unit <= 0.04045 ? unit / 12.92 : ((unit + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

test('choosing Dark repaints the page at once, is remembered across reload and page switch, and reverses', async ({
  page,
  request,
}) => {
  const uncaught: string[] = [];
  // Attached before the first navigation: a shell that threw during a render
  // paints no theme either, and "the class never changed" must not be reported
  // when the real story is a crash.
  page.on('pageerror', (error) => uncaught.push(error.message));

  // This is the one lane spec that asserts nothing server-side, so the probe is
  // the only thing proving the page in front of it belongs to this deployment.
  // A browser pointed at a stale dev proxy would otherwise pass every step.
  await refuseIfNotTheDeployment(async (url, headers) => ({
    status: (await request.get(url, { failOnStatusCode: false, headers })).status(),
  }));

  // Before the FIRST navigation (see test.use above). Note what is NOT seeded
  // here: `astrabox-theme` is exactly what this walk has to write by hand.
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });

  const menu = page.locator('[data-slot="dropdown-menu-content"]');
  const rail = page.locator('[data-slot="sidebar"]');
  // Held structurally: the account row's accessible name is the reader's own
  // display name, which is not a string a spec can write down.
  const railTrigger = rail.locator('[data-slot="sidebar-footer"] [data-slot="dropdown-menu-trigger"]');
  // The console has no user identity in its rail, so the same menu hangs off a
  // settings cog instead — `shell:settings`.
  const consoleTrigger = page.getByRole('button', { name: 'Settings' });

  /**
   * Open a menu and hand back its theme control.
   *
   * Reopened before every press rather than held across one: whether the panel
   * survives a selection is the kit's business, and assuming it in either
   * direction is how a spec starts testing the popup instead of the switch.
   */
  async function openThemeControl(trigger: Locator): Promise<Locator> {
    await trigger.click();
    await expect(menu, 'the settings menu must open — a control nobody can reach is no control').toBeVisible();
    // Base UI's ToggleGroup is a `role="group"`, and `Segmented` passes the
    // label through, so the control names itself.
    const group = menu.getByRole('group', { name: 'Theme' });
    await expect(group, 'the menu must expose the theme control').toBeVisible();
    await expect(group.getByRole('button'), 'three themes, no more').toHaveCount(SEGMENTS.length);
    await expect(
      group.getByRole('button'),
      'the segments name the three themes, in the order the menu declares them',
    ).toHaveText([...SEGMENTS]);
    await expect(
      group.locator('[aria-pressed="true"]'),
      'exactly one theme is in force — a group with none pressed is the wipe this control guards against, '
        + 'and a group with two is a multiple-selection regression',
    ).toHaveCount(1);
    return group;
  }

  async function closeMenu(): Promise<void> {
    await page.keyboard.press('Escape');
    await expect(menu).toBeHidden();
  }

  /** Press a segment through the control — never through storage. */
  async function choose(group: Locator, label: (typeof SEGMENTS)[number]): Promise<void> {
    await group.getByRole('button', { name: label, exact: true }).click();
  }

  // ── 1. The control reports the theme in force, before anything is pressed ─
  await page.goto(appPath('/'));
  await expect(page.getByTestId('sessions-page')).toBeVisible();

  const opening = await readPaint(page);
  let group = await openThemeControl(railTrigger);
  const openingPressed = (await group.locator('[aria-pressed="true"]').innerText()).trim();
  // System is the default every new reader gets, and under the pinned light
  // scheme it resolves to light. Light and Dark stand for themselves.
  const honest = openingPressed === 'System' ? 'light' : openingPressed.toLowerCase();
  expect(
    paintedTheme(opening),
    `the menu shows ${openingPressed} pressed, so that is what the page must be painting — `
      + 'a control reporting a default it invented is wrong before anyone touches it',
  ).toBe(honest);
  await closeMenu();

  // ── 2. An explicit Light, so the Dark press below is a real change ────────
  // Whatever the context started at, the baseline is now a theme this spec
  // chose through the control, not one it wrote into storage.
  group = await openThemeControl(railTrigger);
  await choose(group, 'Light');
  await closeMenu();
  await expect
    .poll(async () => {
      const paint = await readPaint(page);
      return `${paintedTheme(paint)}/${paint.stored}`;
    }, { message: 'choosing Light must reach both the page and the preference' })
    .toBe('light/light');

  const light = await readPaint(page);
  expect(light.colorScheme, 'the root must tell the browser which scheme to paint its own widgets in').toBe('light');
  expect(
    light.sidebarBackground,
    'the rail\'s painted inner must be on the page — the second token family this walk measures',
  ).not.toBe('');

  // ── 3. The press reaches the painted page, with no reload and no navigation ─
  // The half the seeded path structurally cannot reach.
  const documentOrigin = await page.evaluate(() => performance.timeOrigin);
  group = await openThemeControl(railTrigger);
  await choose(group, 'Dark');
  await expect
    .poll(async () => paintedTheme(await readPaint(page)), {
      message: 'Dark must repaint the open page',
    })
    .toBe('dark');

  const dark = await readPaint(page);
  expect(
    await page.evaluate(() => performance.timeOrigin),
    'this must be the same document — a theme that only arrives with a fresh load is the defect, not the proof',
  ).toBe(documentOrigin);
  expect(dark.colorScheme).toBe('dark');
  expect(dark.stored, 'the control writes the preference; the reload below reads it back').toBe('dark');
  expect(
    dark.bodyBackground,
    'the page has to repaint, not only re-class: --background is what body is painted with',
  ).not.toBe(light.bodyBackground);
  expect(dark.bodyColor, '--foreground has to move with it').not.toBe(light.bodyColor);
  expect(
    dark.sidebarBackground,
    'the rail is its own token family (--sidebar); a swap that reaches only --background leaves it stranded',
  ).not.toBe(light.sidebarBackground);
  // The relation, not a pinned hex: this survives a palette edit and still
  // catches a root that flipped over a stylesheet that did not.
  expect(
    luminance(light.bodyBackground),
    'in Light the page is paper and the text is ink',
  ).toBeGreaterThan(luminance(light.bodyColor));
  expect(
    luminance(dark.bodyBackground),
    'in Dark that relation inverts — if it does not, the class flipped and nothing repainted',
  ).toBeLessThan(luminance(dark.bodyColor));
  // Measured with the panel still open — that is the most immediate reading —
  // and closed only now, because the next press reopens it from the same
  // trigger and a trigger clicked twice toggles rather than opens.
  await closeMenu();

  // ── 4. Pressing the pressed segment is a no-op, not a wipe ────────────────
  // Pressing the pressed item empties Base UI's ToggleGroup value, and
  // `Segmented` drops that empty selection (LanguageSwitcher.tsx:40-46).
  // Without that guard this path calls `setTheme(undefined)`: next-themes
  // returns early so the page keeps its dark paint, but the menu jumps to
  // System and the preference takes the string "undefined" — a visibly wrong
  // control, and a LIGHT page on the next reload, in a theme the reader never
  // chose.
  group = await openThemeControl(railTrigger);
  await choose(group, 'Dark');
  await expect(
    group.locator('[aria-pressed="true"]'),
    'pressing the pressed segment must leave the control in exactly one state',
  ).toHaveText(['Dark']);
  await closeMenu();
  const afterRepress = await readPaint(page);
  expect(paintedTheme(afterRepress), 'and must not disturb the page').toBe('dark');
  expect(afterRepress.stored, 'and must not disturb the preference').toBe('dark');

  // ── 5. Remembered across a reload ─────────────────────────────────────────
  await page.reload();
  await expect(page.getByTestId('sessions-page')).toBeVisible();
  const reloaded = await readPaint(page);
  expect(
    paintedTheme(reloaded),
    'the stored preference has to be read back at load — and it cannot have come from the desktop, '
      + 'which this context pins to light',
  ).toBe('dark');
  expect(reloaded.bodyBackground).toBe(dark.bodyBackground);
  expect(reloaded.sidebarBackground).toBe(dark.sidebarBackground);
  expect(reloaded.stored).toBe('dark');
  group = await openThemeControl(railTrigger);
  await expect(
    group.locator('[aria-pressed="true"]'),
    'and the control has to come back saying so',
  ).toHaveText(['Dark']);
  await closeMenu();

  // ── 6. It follows the reader to the other surface, and to the other mount ─
  await rail.getByRole('link', { name: 'Console', exact: true }).click();
  await expect(page).toHaveURL((url) => url.pathname === appPath('/manage/agents'));
  const onConsole = await readPaint(page);
  expect(paintedTheme(onConsole), 'a page switch is not a reason to lose the theme').toBe('dark');
  expect(onConsole.bodyBackground).toBe(dark.bodyBackground);
  expect(
    onConsole.sidebarBackground,
    'both shells wear the same rail; a console painting its own is a second answer to one question',
  ).toBe(dark.sidebarBackground);

  group = await openThemeControl(consoleTrigger);
  await expect(
    group.locator('[aria-pressed="true"]'),
    'the second mount of the control must agree with the first',
  ).toHaveText(['Dark']);

  // ── 7. It reverses, from the mount that did not set it ────────────────────
  // A switch that can only leave the default passes a one-way check.
  await choose(group, 'Light');
  await closeMenu();
  await expect
    .poll(async () => {
      const paint = await readPaint(page);
      return `${paintedTheme(paint)}/${paint.stored}`;
    }, { message: 'Light from the console menu must undo Dark' })
    .toBe('light/light');
  const reversed = await readPaint(page);
  expect(reversed.colorScheme).toBe('light');
  expect(
    reversed.bodyBackground,
    'the way back has to land exactly where the page started, not somewhere near it',
  ).toBe(light.bodyBackground);
  expect(reversed.bodyColor).toBe(light.bodyColor);
  expect(reversed.sidebarBackground).toBe(light.sidebarBackground);

  // ── 8. System keeps following the desktop, with nobody touching the tab ───
  // The default every new reader gets, and the one part of this journey that
  // belongs to the staleness family: a resident tab whose desktop goes dark at
  // sunset must not wait for a reload to notice.
  group = await openThemeControl(consoleTrigger);
  await choose(group, 'System');
  await closeMenu();
  await expect
    .poll(async () => {
      const paint = await readPaint(page);
      return `${paintedTheme(paint)}/${paint.stored}`;
    }, { message: 'System under a light desktop paints light, and is stored as System' })
    .toBe('light/system');

  await page.emulateMedia({ colorScheme: 'dark' });
  await expect
    .poll(async () => paintedTheme(await readPaint(page)), {
      message: 'the desktop went dark — no click, no reload, no navigation; the open tab has to follow',
    })
    .toBe('dark');
  const followed = await readPaint(page);
  expect(followed.bodyBackground, 'and the page has to repaint, not only re-class').toBe(dark.bodyBackground);
  expect(
    followed.stored,
    'following the desktop is not the reader choosing a theme — the preference stays System',
  ).toBe('system');

  expect(
    uncaught,
    `uncaught exception while driving the theme switch:\n${uncaught.join('\n')}`,
  ).toEqual([]);
});
