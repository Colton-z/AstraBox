/**
 * E2E: the sign-in card forwards only a same-deployment `next` destination.
 *
 * This file opts out of shared authentication state so `LoginPage` renders the
 * card. A legitimate management path proves `next` is preserved; absolute and
 * protocol-relative destinations must be reduced to a local fallback. The
 * identity handoff request is intercepted because only its address is under
 * test. A page-error listener covers the unauthenticated UI components.
 */
import { expect, test } from '@playwright/test';

import { appPath } from '../fixtures/env';

/**
 * Signed out, and on a browser that asks for English.
 *
 * The storage state is emptied rather than dropped — an inline empty state is
 * the form Playwright documents for opting a file out of a shared cookie, and
 * it is what `casdoor-scoped-api-client-credentials` already uses for its
 * unauthenticated request context. The locale is pinned because i18next detects
 * from `navigator` when localStorage holds nothing, and this context starts
 * with nothing: on a Chinese host the card would open in Chinese and the
 * language section would be asserting its two ends the wrong way round.
 */
test.use({ storageState: { cookies: [], origins: [] }, locale: 'en-US' });

/**
 * The handoff `signInHref` builds. Matched on the query as well as the path, so
 * this cannot swallow some other route that merely starts the same way.
 */
const SIGN_IN_HANDOFF = /\/api\/v1\/auth\/login\?/;

/** `next` values that are not this deployment's to send anybody to. */
const OFF_SITE: ReadonlyArray<{ next: string; why: string }> = [
  {
    next: 'https://evil.example/',
    why: 'an absolute URL is the plain form of the attack',
  },
  {
    next: '//evil.example/',
    why:
      'protocol-relative: it starts with a slash, so it satisfies half the guard on its own — '
      + 'this is the form the second half of `startsWith("/") && !startsWith("//")` exists for',
  },
];

test('the sign-in card renders for a signed-out reader and will not carry an off-site next', async ({
  page,
}) => {
  const uncaught: string[] = [];
  // Attached before the first navigation: what this guards is a render, and a
  // listener added afterwards misses the one that mattered.
  page.on('pageerror', (error) => uncaught.push(error.message));

  const handoffs: string[] = [];
  await page.route(SIGN_IN_HANDOFF, async (route) => {
    handoffs.push(route.request().url());
    // 'aborted' rather than the default 'failed': ERR_ABORTED cancels the trip
    // and leaves the card standing, where ERR_FAILED paints Chromium's own
    // error page over it. Either way the browser never leaves the deployment.
    await route.abort('aborted');
  });

  const signIn = page.getByRole('button', { name: 'Continue with your organization account' });

  // ── 1. The card comes up at all ──────────────────────────────────────────
  await page.goto(appPath('/login'));
  await expect(
    page.getByRole('heading', { name: 'Sign in to AstraBox' }),
    'a blank frame here is the aria-busy branch — the probe answered something other than signed-out',
  ).toBeVisible();
  // The other three, because a heading alone passes on a card that came up
  // empty under it. What this deployment is, the way in, and who does and does
  // not see the password are each a separate reason the page exists.
  await expect(
    page.getByText('AstraBox runs the selected Agent program in an isolated sandbox.'),
  ).toBeVisible();
  await expect(signIn).toBeVisible();
  await expect(
    page.getByText(
      "AstraBox does not receive your password. Your organization's identity service handles sign-in.",
    ),
  ).toBeVisible();

  // ── 2. The language switch at the foot reaches the card ──────────────────
  // Both directions. A switch that holds its own new value and changes nothing
  // else passes a one-way check, and so does one that can only leave English.
  // The choice persists to localStorage `astrabox-lang`, which is this test's
  // own context and dies with it.
  await page.getByRole('combobox', { name: 'Language' }).selectOption('zh');
  // `shell:signin.title` in zh. The control taking the value would prove only
  // that a select selects; the card's own words are where the choice has to
  // land.
  await expect(
    page.getByRole('heading', { name: '登录 AstraBox' }),
    'choosing 中文 must change the card, not only the control',
  ).toBeVisible();

  // The way back runs through a control that is now labelled in Chinese itself:
  // its aria-label is `shell:language`, one of the strings that just moved.
  const switcher = page.getByRole('combobox', { name: '语言' });
  await expect(
    switcher,
    'the control must name the language actually in force',
  ).toHaveValue('zh');
  await switcher.selectOption('en');
  await expect(
    page.getByRole('heading', { name: 'Sign in to AstraBox' }),
    'English must undo it — every section below reads the English card',
  ).toBeVisible();

  /**
   * Where the sign-in control WOULD send the browser, read without going there.
   *
   * `next` is percent-encoded into the address bar so that what the page parses
   * is exactly the string under test, with no URL resolution in between that
   * could quietly normalise it into something else.
   */
  const handoffFor = async (next: string): Promise<URL> => {
    const before = handoffs.length;
    await page.goto(`${appPath('/login')}?next=${encodeURIComponent(next)}`);
    await expect(signIn, `the card must render for next=${next}`).toBeVisible();
    await signIn.click();
    await expect
      .poll(() => handoffs.length, {
        message: `the sign-in button must ask for the handoff route (next=${next})`,
      })
      .toBeGreaterThan(before);
    // Compared as a path, not as a substring: `/api/v1/auth/login` contains
    // `/login`, so a `toContain` here would pass on the very trip it forbids.
    expect(
      new URL(page.url()).pathname,
      'the click is read at the door and never followed',
    ).toBe(appPath('/login'));
    return new URL(handoffs[handoffs.length - 1]);
  };

  // ── 3. The handoff carries a destination this page is willing to write ───
  // The control leg, and it is not decoration: a button wired to a constant
  // `?next=/` passes both refusals below while dropping every real destination.
  // The value is `RequireAuth`'s own: percent-encoded `/manage/agents` is what
  // the gate writes when it sends a signed-out reader to this page, so the leg
  // that has to keep working is driven with the production caller's input.
  const carried = await handoffFor('/manage/agents');
  expect(carried.pathname, "the handoff goes to the deployment's own login route").toMatch(
    /\/api\/v1\/auth\/login$/,
  );
  expect(
    carried.searchParams.get('next'),
    "a same-origin path is the reader's own destination and has to survive",
  ).toBe('/manage/agents');

  // ── 4. … and refuses one it did not ──────────────────────────────────────
  for (const { next, why } of OFF_SITE) {
    const refused = await handoffFor(next);
    expect(refused.searchParams.get('next'), `${next} — ${why}`).toBe('/');
    // Not only in `next`: any surviving copy of that host, in any parameter and
    // in any encoding, is the open redirect.
    expect(refused.toString(), `${next} reached the handoff as ${refused.search}`).not.toContain(
      'evil.example',
    );
  }

  expect(
    uncaught,
    `uncaught exception on the sign-in page:\n${uncaught.join('\n')}`,
  ).toEqual([]);
});
