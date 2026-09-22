/**
 * E2E: a deploy landing on an open Agent edit page does not take the prompt
 * being typed, and is still adopted once that work is saved.
 *
 * `startFrontendReleaseGuard` (frontend/src/frontendRelease.ts) replaces the
 * whole document as soon as the served Vite entry differs from the loaded one.
 * It fires on focus, on visibility, and on a 60s interval, so nothing the
 * operator does decides when it happens. Every draft on a console edit page is
 * component state re-seeded from the record on mount
 * (frontend/src/manage/AgentDetailPage.tsx:152), so that replacement is what
 * decides whether a half-written system prompt still exists afterwards.
 *
 * The guarantee held here is the one visible from the operator's seat: while
 * the prompt is unsaved the tab keeps it, and after Save the tab takes the new
 * release. `holdFrontendRelease` is the mechanism that delivers both halves,
 * and both are asserted, because a tab that never reloads and a tab that
 * reloads over unsaved text each satisfy one half and fail the operator.
 *
 * Pairs with app-shell.parallel.spec.ts:160, which proves a resident tab with
 * no unsaved work adopts a new hashed entry: a hold delays a release, it does
 * not refuse one. If one of these two specs is ever made green by weakening the
 * other's rule, they disagree out loud.
 *
 * The 60s interval is not exercised — the focus probe runs the same
 * `checkForRelease`, and a lane spec cannot spend a minute waiting for a
 * scheduler. frontend/src/frontendRelease.test.ts owns the timer and the hold
 * counter in isolation; what this spec adds is the product chain neither can
 * see: that typing in this card actually places a hold, and that saving
 * actually releases it.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import { onPassOnly } from '../fixtures/sessionCleanup';

// This spec reads two localized strings off the page — the card heading
// "Model and prompt" (misc:agent_form.groups.model.label) and the "Save"
// control. The console detects language as ['localStorage','navigator'], so an
// unpinned runner locale decides which spelling appears, and a spec written to
// accept two spellings would accept a third nobody wrote it against. Pin the
// navigator locale here and the persisted 'astrabox-lang' below.
test.use({ locale: 'en-US' });

// One document fetch plus the reload it triggers. Generous because the probe is
// a real same-origin round trip against the deployment, not a stub.
const RELEASE_PROBE_MS = parseTimeoutEnv('ASTRABOX_E2E_RELEASE_PROBE_MS', 20_000);

let throwawayAgentId = '';

// The Agent and its half-typed state outlive a failure on purpose
// (fixtures/sessionCleanup): a record deleted in teardown is a record nobody
// can look at afterwards.
onPassOnly(async ({ request }) => {
  if (throwawayAgentId) await new AstraApi(request).deleteAgent(throwawayAgentId);
  throwawayAgentId = '';
});

/**
 * What an operator has actually written when a deploy lands: long enough that a
 * persistence or transport limit would truncate it, and multi-paragraph so a
 * fix that keeps only the first line is not mistaken for one that keeps the
 * work.
 */
const PROMPT_BODY = [
  'You are the research desk for a hardware reliability team. Read the question'
  + ' before answering it, and say which part of it you are answering.',
  'Sources rank in this order: the vendor’s own documentation at the exact'
  + ' release in use, then the repository the code ships from, then anything'
  + ' else. Quote the source you used and name where it came from, so the reader'
  + ' can open it. When two sources disagree, say so and give the newer one,'
  + ' rather than writing a third answer that splits the difference.',
  'When you cannot confirm something, say you cannot, and name what you skipped:'
  + ' the record you did not read, the branch you did not follow, the dependency'
  + ' you could not reach. Partial work reported as complete costs more than no'
  + ' answer at all.',
  'Keep replies to the shape the question had. A yes-or-no question gets a yes'
  + ' or a no first, and the reasoning after it.',
].join('\n\n');

/** Typed as keystrokes so the last thing the field saw is real input events. */
const PROMPT_TAIL = ' Answer in the language the question was asked in.';

test('a half-typed system prompt survives the reload a landing release forces on the open tab', async ({
  baseURL,
  page,
  request,
}) => {
  const uncaught: string[] = [];
  // Before the first navigation: a crash while the page comes back after the
  // release happens during a render, and a listener added afterwards misses it.
  page.on('pageerror', (error) => uncaught.push(error.message));

  // ── Setup. The API is allowed here and for the record oracle below ────────
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(`__e2e_release_draft_${Date.now()}`);
  throwawayAgentId = agent.agent_id;
  const baseline = await api.data<Record<string, unknown>>('GET', `/agents/${throwawayAgentId}`);
  const baselinePrompt = String(baseline.system ?? '');

  // Pin the console language before the FIRST navigation (see test.use above).
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });

  const agentPageUrl = new URL(
    appPath(`/manage/agents/${encodeURIComponent(throwawayAgentId)}`),
    baseURL,
  );
  await page.goto(agentPageUrl.href, { waitUntil: 'domcontentloaded' });

  const card = page.locator('[data-slot="card"]').filter({ has: page.locator('#agent-system') });
  await expect(card).toBeVisible();
  await expect(card.getByRole('heading', { name: 'Model and prompt', exact: true })).toBeVisible();

  // The guard compares hashed entry URLs and returns inert when the document
  // has none (a dev server, or a build whose entry is not named
  // `assets/main-[hash].js` as frontend/vite.config.ts:31 pins it). Read it
  // here so such a deployment fails the spec instead of passing it vacuously:
  // every assertion below would hold trivially on a tab nothing can reload.
  const loadedEntry = await page
    .locator('script[type="module"][src*="/assets/main-"]')
    .getAttribute('src');
  expect(loadedEntry, 'the release guard only runs against a hashed production entry').toBeTruthy();

  const systemPrompt = page.locator('#agent-system');
  const save = card.getByRole('button', { name: 'Save', exact: true });
  await expect(systemPrompt).toHaveValue(baselinePrompt);
  // ConsoleCard renders Save only for a dirty section, so its absence is the
  // page's own statement that there is nothing unsaved here yet.
  await expect(save).toHaveCount(0);

  // ── Arm the deploy at the guard's only input: the served document ─────────
  // While `armed`, the stub answers every probe: a landed release does not
  // un-land, and the tab must still see it after the hold ends. Arming is
  // dropped the moment the tab takes the release, because a tab back on the
  // real entry while the stub advertises a different one would reload forever.
  let armed = false;
  let probesServed = 0;
  let documentRequests = 0;
  page.on('request', (outgoing) => {
    if (outgoing.resourceType() !== 'document') return;
    if (new URL(outgoing.url()).href.startsWith(agentPageUrl.href)) documentRequests += 1;
  });
  await page.route(
    (url) => url.href.startsWith(agentPageUrl.href),
    async (route) => {
      const probe = route.request();
      // The guard's probe is a same-origin `fetch` asking for text/html
      // (frontendRelease.ts:79). Matching its shape rather than its URL keeps
      // the reload navigation — a `document` request for the same URL — on the
      // real server, so the tab comes back as the real application. A probe
      // whose shape changes stops matching and fails the poll below rather
      // than quietly serving nothing.
      if (
        armed
        && probe.resourceType() === 'fetch'
        && probe.headers().accept?.includes('text/html')
      ) {
        probesServed += 1;
        const nextEntry = new URL(appPath('/assets/main-E2ENextRelease.js'), agentPageUrl).pathname;
        await route.fulfill({
          status: 200,
          contentType: 'text/html; charset=utf-8',
          headers: { 'Cache-Control': 'no-store, no-cache, must-revalidate' },
          body: [
            '<!doctype html>',
            '<html><head>',
            `<script type="module" src="${nextEntry}"></script>`,
            '</head><body></body></html>',
          ].join(''),
        });
        return;
      }
      await route.continue();
    },
  );

  // ── The operator types, and is still mid-sentence ─────────────────────────
  const typed = `${PROMPT_BODY}${PROMPT_TAIL}`;
  await systemPrompt.fill(PROMPT_BODY);
  await systemPrompt.pressSequentially(PROMPT_TAIL);
  await expect(systemPrompt).toHaveValue(typed);
  await expect(systemPrompt).toBeFocused();
  // The same dirtiness this waits on is what AgentDetailPage.tsx:208 hands the
  // release hold, so a visible Save means the hold for this draft is placed.
  await expect(save).toBeVisible();

  const documentBeforeRelease = await page.evaluate(() => performance.timeOrigin);

  // ── The deploy lands while the prompt is unsaved ──────────────────────────
  armed = true;
  // The operator tabbing back after copying prompt text from another window:
  // this runs the production focus listener (frontendRelease.ts:117) and the
  // real release decision behind it, not a test-only entry point.
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect
    .poll(() => probesServed, {
      timeout: RELEASE_PROBE_MS,
      intervals: [100, 250, 500],
      message: 'the resident tab must observe the newly served entry',
    })
    .toBeGreaterThanOrEqual(1);

  // The guard decides and reloads on the same continuation that reads the
  // probe body, so by the time the served count is visible here a replacement
  // is already in flight if one is coming. These three checks then re-assert
  // over real round trips, each of which a replacement would break.
  expect(
    await page.evaluate(() => performance.timeOrigin),
    'the document must not be replaced while the prompt is unsaved',
  ).toBe(documentBeforeRelease);
  await expect(systemPrompt, 'the typed prompt must still be in the field').toHaveValue(typed);
  await expect(save, 'the restored text must still read as unsaved work').toBeVisible();
  expect(documentRequests, 'no reload may be requested over unsaved work').toBe(0);

  // Holding a release must not turn into writing for the operator: an autosave
  // would publish a half-written system prompt to every conversation this Agent
  // starts from now on.
  const untouched = await api.data<Record<string, unknown>>('GET', `/agents/${throwawayAgentId}`);
  expect(untouched.system, 'an unsaved draft must not reach the record').toEqual(baseline.system);
  expect(untouched.version, 'an unsaved draft must not write a new version').toEqual(baseline.version);

  // ── The work is saved, which is what makes the tab safe to replace ────────
  const saved = page.waitForResponse((response) => (
    response.request().method() === 'PUT'
    && new URL(response.url()).pathname === apiPath(`/agents/${throwawayAgentId}`)
  ));
  await save.click();
  expect((await saved).status(), 'the kept prompt must be savable, not only visible').toBe(200);
  await expect(save, 'a saved section offers no Save').toHaveCount(0);
  const stored = await api.data<Record<string, unknown>>('GET', `/agents/${throwawayAgentId}`);
  expect(stored.system, 'the record must hold the whole prompt, keystroke tail included').toBe(typed);
  expect(stored.version, 'saving must move the record version').not.toEqual(baseline.version);

  // ── The release is adopted, not refused ──────────────────────────────────
  const documentReplaced = page.waitForEvent('framenavigated', {
    predicate: (frame) => frame === page.mainFrame(),
    timeout: RELEASE_PROBE_MS,
  });
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect
    .poll(() => documentRequests, {
      timeout: RELEASE_PROBE_MS,
      intervals: [100, 250, 500],
      message: 'the tab must take the new release once the work is saved',
    })
    .toBe(1);
  armed = false;
  await documentReplaced;
  await page.waitForLoadState('domcontentloaded');
  expect(
    await page.evaluate((before) => performance.timeOrigin !== before, documentBeforeRelease),
    'the reload must replace the document, not re-render it',
  ).toBe(true);

  // ── What the operator finds in the tab afterwards ─────────────────────────
  await expect(card).toBeVisible();
  await expect(systemPrompt, 'the prompt is on the record and on the page').toHaveValue(typed);
  // A draft kept somewhere durable must not re-offer itself over the record it
  // was already written to; the operator would be looking at unsaved work that
  // has nowhere newer to go.
  await expect(save, 'the stored prompt must not come back as a pending edit').toHaveCount(0);
  expect(uncaught, 'the page must come back without an uncaught error').toEqual([]);
});
