/**
 * E2E: what the Agent page tells an operator about the next conversation's start.
 *
 * THE QUESTION, from the operator's seat. They turn on "Keep a sandbox ready"
 * for an Agent, nobody uses it for an hour, and they open the Agent page to find
 * out whether the next conversation starts warm — a 56ms claim — or cold, which
 * is a whole engine child at 17s (deepseek_harness) to 33s (pi). The page
 * answers nothing at all: `/manage/agents/{id}` renders model, environment,
 * updated_at and the edit cards, and no page under `frontend/src` reads
 * `GET /api/v1/agents/{agent_id}/prepared-runtime` — the route exists
 * (astrabox/api/routes/agents.py:274-284) and only the layout-audit mock ever
 * answers it.
 *
 * AND THE ONE ANSWER THAT EXISTS IS WRONG IN EXACTLY THIS STATE. That route
 * computes `ready = enabled and state == "prepared"`
 * (agent/agent_service.py:254), with no reference to the manifest's age, while
 * the claim path refuses a manifest older than `PREPARED_SLOT_TTL_SECONDS`
 * ("prepared unit exceeded its TTL", agent/prepared_slots.py:1004-1015). An
 * Agent holding a 31-minute-old slot is therefore advertised as warm to the one
 * surface an operator could ask, and the next conversation pays the cold start
 * anyway. `AgentPreparedRuntimeStatus` is `extra="forbid"` and carries no
 * timestamp at all (routes/agents.py:169-182), so a page cannot even work the
 * age out for itself — which is the point: the TTL rule belongs to the platform
 * and a browser re-deriving it would be a second copy of it. A4 below is
 * written against the Agent row precisely so a browser-side guess cannot satisfy
 * this spec.
 *
 * WHY THE PAGE IS THE SUBJECT AND NOT THE SWEEP. The background keeper is
 * already covered from the row's side: `absent-prepared-slot-is-rebuilt-…`
 * pins creation with no manifest, `prewarm-rebuilds-a-lost-slot-…` pins the
 * failed build, `prepared-slot-survives-a-claim-during-its-renewal` pins the
 * collision. None of them looks at a console page, because there is nothing to
 * look at. What is uncovered is the operator's own read: is the fast path armed
 * right now, and is what the product says about it true at the moment it says
 * it.
 *
 * THE SCENE. Shared (Agent) tenancy only. The TTL manifest with `prepared_at`
 * lives on the Agent row's `_prepared_slot`; under conversation tenancy the
 * status comes from `describe_client_pool` (agent_service.py:235-252) and
 * carries no age at all, so this journey cannot be posed there.
 * `ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT` and `ASTRABOX_E2E_RESEARCH_AGENT`
 * are deployment fixtures: read and asserted present, never created.
 *
 * NO API AGES A SLOT, so `backdatePreparedSlot` writes `prepared_at` an hour
 * into the past in one statement guarded down to the slot id (dbOracle.ts:501) —
 * the same mechanism `lapseSessionSandboxLease` is for a sandbox lease. An hour
 * puts the manifest past its TTL, which is both what makes the route's `ready`
 * a lie and what makes the sweep replace it.
 *
 * THE WINDOW, AND WHY IT IS A SANDWICH. An over-TTL manifest is reaped at the
 * head of the refill (`reap_slot_manifest_if_stale`, prepared_slots.py:637-645)
 * rather than kept published the way a merely-due one is, so the backdated
 * manifest survives only until the next watcher tick — the lane's watcher
 * interval. The verdict is therefore never taken from a timestamp comparison:
 * the row is read from the backdate's own `RETURNING` projection before the
 * page is asked, and again after, and the assertions hold only if the SAME slot
 * id is on the row at both ends. A manifest is only ever replaced forward, so
 * an unchanged slot id across the pair means the sample in between was
 * unambiguously about it. If the sweep wins the race the spec says so in those
 * words instead of passing; it never reads a healed row as a green page.
 *
 * WHAT IS DELIBERATELY NOT ASSERTED. That the card refreshes itself without a
 * gesture — that would pin an implementation choice, and Refresh-driven
 * re-asking is the console's existing grammar (a-sandbox-record-reports-the-box-
 * not-an-empty-card). And that the next conversation claims the slot in 56ms:
 * `console-extension-save-reschedules-prewarm` already proves a claim adopts the
 * prepared isolated session, and a conversation here would not fit the budget.
 *
 * THE SENTENCES BELOW ARE THE CONTRACT. This card does not exist yet, so the
 * five readings an operator can be given are named here rather than discovered:
 * they are counted the way the sandbox record's security card counts its three,
 * because the property is that EXACTLY one is on screen — a card that renders
 * nothing reads as a calm one, which is the failure a warm-capacity panel must
 * not have. They are localized, so the locale is pinned below; a spec accepting
 * two spellings would accept a third.
 *
 * BUDGET. Two real preparations inside the 180s cut: the first one the console
 * save starts, and the replacement the background builds. Measured builds are
 * 17s (deepseek_harness) and 33s (pi) and the two budgets below are ceilings
 * neither reaches. If the slowest matrix engine cannot hold both, split A6 —
 * the background renewal — into its own exclusive spec rather than raising a
 * timeout the runner will kill anyway.
 *
 * CAPACITY. One prepared shared box for the run, briefly two if the reaper has
 * not yet released the emptied one when the replacement is placed. A second
 * build that fails is reported with the row's `_prepared_runtime_error`, so a
 * full node is never read as a page defect.
 */
import { execFileSync } from 'node:child_process';

import { expect, test, type Locator, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { backdatePreparedSlot, documentsByField } from '../fixtures/dbOracle';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

// Deployment fixtures: read and asserted present, never created or rewritten.
const SHARED_ENVIRONMENT = String(
  process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT || '',
).trim();
const RESEARCH_AGENT = String(
  process.env.ASTRABOX_E2E_RESEARCH_AGENT || '',
).trim();

// Every per-step wait is tunable so the lane can trade budget against a loaded
// deployment. The test budget itself is never stated here — the runner owns it.
const PREPARED_SLOT_MS = parseTimeoutEnv('ASTRABOX_E2E_PREPARED_SLOT_MS', 75_000);
const PREPARED_RENEWAL_MS = parseTimeoutEnv('ASTRABOX_E2E_PREPARED_RENEWAL_MS', 75_000);

// The product's own number, named so a drift fails here rather than silently
// making the invariant vacuous: prepared_slots.py:118.
const PREPARED_SLOT_TTL_MS = 30 * 60 * 1_000;
// How far the manifest is aged. An hour is past the TTL by a wide margin, so no
// clock skew between the runner and the server can leave it merely due for
// renewal — a state in which `ready: true` would be the truth.
const IDLE_HOUR_MS = 60 * 60 * 1_000;
// How closely the card's machine-readable instant must match the row's
// `prepared_at`. Both are the same stamp travelling through JSON, so this is
// rounding room, not tolerance for a different value.
const FRESHNESS_TOLERANCE_MS = 2_000;

// How many watcher ticks must fit inside the renewal budget for a red result to
// mean anything. Fewer than this and the sweep never got its chance, so the spec
// would be measuring its own impatience.
const REQUIRED_WATCHER_TICKS = 4;

/** The heading the warm-capacity card puts on itself, which is how it is addressed. */
const WARM_CAPACITY_HEADING = 'Warm capacity';

/**
 * The five readings the warm-capacity card can give, and the opening clause of
 * each. Anchored prefixes rather than whole sentences: the tail of a sentence is
 * copy and may be reworded, but which of the five states an operator is being
 * told is the product fact under test.
 */
const READINGS = [
  { state: 'off', opening: /^Warm start is off for this Agent/ },
  { state: 'absent', opening: /^No sandbox is standing by/ },
  { state: 'warm', opening: /^The next conversation starts warm/ },
  { state: 'renewing', opening: /^The prepared sandbox has expired/ },
  { state: 'failed', opening: /^AstraBox could not prepare a sandbox/ },
] as const;

type WarmState = (typeof READINGS)[number]['state'];

/** The manifest's PUBLIC coordinates, plus the row pointers the sweeps key on.
 *
 * `_prepared_slot` also carries the activation token and the model credential,
 * and a failed `expect()` retains and prints both sides of its comparison, so
 * only these are ever lifted out of the row. */
interface SlotFacts {
  slotId: string;
  state: string;
  placement: string;
  sandboxId: string;
  preparedAt: string;
  hasManifest: boolean;
  residentSandboxId: string | null;
  lastError: string | null;
}

function preparedSlotFacts(agentId: string): SlotFacts {
  const rows = documentsByField('agents', '$.agent_id', agentId);
  expect(rows, 'the preparation under test must belong to exactly one Agent row').toHaveLength(1);
  const row = rows[0];
  const raw = row._prepared_slot;
  const manifest = (raw || {}) as Record<string, unknown>;
  return {
    slotId: String(manifest.slot_id || '').trim(),
    state: String(manifest.state || '').trim(),
    placement: String(manifest.placement || '').trim(),
    sandboxId: String(manifest.sandbox_id || '').trim(),
    preparedAt: String(manifest.prepared_at || '').trim(),
    hasManifest: Boolean(raw) && typeof raw === 'object' && !Array.isArray(raw),
    residentSandboxId: String(row.sandbox_id || '').trim() || null,
    lastError: String(row._prepared_runtime_error || '').trim() || null,
  };
}

/**
 * The deployed expiration watcher's tick interval, read from the server rather
 * than assumed.
 *
 * `printenv` exits non-zero when the variable is unset, which `execFileSync`
 * raises. That is the right outcome — an unset interval means the deployment
 * runs the 300s default, no sweep can tick inside this spec's budget and the
 * backdated window would stay open for minutes — but it has to say so in words,
 * because "the spec threw" and "this deployment cannot host this journey" send a
 * reader to different places.
 */
function deployedWatcherIntervalSeconds(server: string): number {
  let raw = '';
  try {
    raw = execFileSync('docker', [
      'exec', server, 'printenv', 'ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS',
    ], { encoding: 'utf8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  } catch (error) {
    throw new Error(
      'HARNESS PREREQUISITE: ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS is not set on server '
      + `container ${JSON.stringify(server)}, so the deployment runs the 300s default and no `
      + "background sweep can tick inside this spec's renewal budget. Set a few-second interval "
      + `on the deployment, or raise ASTRABOX_E2E_PREPARED_RENEWAL_MS above `
      + `${REQUIRED_WATCHER_TICKS} ticks. This is not a product defect. `
      + `(${String((error as Error).message).slice(0, 200)})`,
    );
  }
  const seconds = Number.parseInt(raw, 10);
  expect(
    Number.isFinite(seconds) && seconds > 0,
    `the deployed expiration-watcher interval must be a positive integer, got ${JSON.stringify(raw)}`,
  ).toBe(true);
  return seconds;
}

/** One console card, addressed by the heading it puts on itself. */
function card(page: Page, heading: string): Locator {
  return page
    .locator('[data-slot="card"]')
    .filter({ has: page.getByRole('heading', { name: heading, exact: true }) });
}

/** What the warm-capacity card is saying right now. */
interface CardReading {
  /** Which of the five named readings are on screen. Exactly one must be. */
  states: WarmState[];
  /** The card's whole text, so a failure prints what the operator would read. */
  text: string;
  /** Every machine-readable instant the card carries. */
  stamps: string[];
}

async function readWarmCapacity(panel: Locator): Promise<CardReading> {
  const states: WarmState[] = [];
  for (const reading of READINGS) {
    if ((await panel.getByText(reading.opening).count()) > 0) states.push(reading.state);
  }
  return {
    states,
    text: (await panel.innerText()).trim(),
    stamps: await panel
      .locator('time[datetime]')
      .evaluateAll((nodes) => nodes.map((node) => node.getAttribute('datetime') || '')),
  };
}

/** The route's answer, as the ENVELOPE the page itself received. */
interface PreparedRuntimeAnswer {
  enabled?: boolean;
  ready?: boolean;
  prepared_count?: number;
  state?: string | null;
  sandbox_id?: string | null;
  last_error?: string | null;
  [key: string]: unknown;
}

/**
 * THE INVARIANT, checked at every read.
 *
 * Whenever the card promises warmth, the manifest on the row is one the claim
 * path would actually hand over: prepared, in the box the card named, and
 * younger than the TTL. It compares two samples taken at the same moment and so
 * has no timing dependence of its own — the watcher healing early cannot flake
 * it, and a page that derived expiry from a raw timestamp in the browser cannot
 * satisfy it, because the row is the thing being compared against.
 */
function neverPromisesAStaleSlot(reading: CardReading, row: SlotFacts, where: string): void {
  if (!reading.states.includes('warm')) return;
  expect(
    row.state,
    `${where}: the card says the next conversation starts warm while the Agent row holds no `
    + `claimable manifest (state=${JSON.stringify(row.state)}). The card read:\n${reading.text}`,
  ).toBe('prepared');
  expect(
    reading.text.includes(row.sandboxId.slice(0, 8)),
    `${where}: the card promises warmth but does not name the box the row is holding `
    + `(${row.sandboxId}). The card read:\n${reading.text}`,
  ).toBe(true);
  const ageMs = Date.now() - Date.parse(row.preparedAt);
  expect(
    Number.isFinite(ageMs),
    `${where}: the row's prepared_at is unreadable (${JSON.stringify(row.preparedAt)}), so the `
    + 'card\'s promise cannot be checked against the claim path\'s rule at all',
  ).toBe(true);
  expect(
    ageMs,
    `${where}: the card promises the next conversation starts warm on a slot prepared `
    + `${Math.round(ageMs / 1_000)}s ago, and claim_prepared_slot refuses anything past `
    + `${PREPARED_SLOT_TTL_MS / 1_000}s ("prepared unit exceeded its TTL", prepared_slots.py:1004). `
    + 'The console must not promise a slot the next conversation will not get.',
  ).toBeLessThan(PREPARED_SLOT_TTL_MS);
}

// The card's five sentences are localized (the console detects language as
// ['localStorage','navigator'] with fallbackLng 'en'), so an unpinned runner
// locale would decide which spelling appears. Pin the navigator locale here and
// the persisted 'astrabox-lang' before the first navigation.
test.use({ locale: 'en-US' });

// This journey deliberately starts no conversation — the whole point of A6 is
// that nobody arrives — so nothing is ever pushed onto the tracker, and the test
// reads it back as evidence of that rather than as bookkeeping.
const sessions = trackSessions();
let agentId = '';
// Registered after the tracker, because afterEach hooks run in registration
// order. A failed run keeps the Agent and whatever box it holds: that row, its
// manifest and its `_prepared_runtime_error` are the scene.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('the Agent page says whether the next conversation starts warm, and never promises a slot that has gone stale', async ({
  page,
  request,
}) => {
  expect(
    SHARED_ENVIRONMENT,
    'ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT must name the deployed Agent-tenancy prewarm Environment',
  ).not.toEqual('');
  expect(
    RESEARCH_AGENT,
    'ASTRABOX_E2E_RESEARCH_AGENT must name the deployed Agent whose model route was proven',
  ).not.toEqual('');

  // ── Premise, made visible before anything is spent ───────────────────────
  // A watcher that is not running produces a timeout at A6 indistinguishable
  // from the defect, so "we waited long enough" has to be a fact about this
  // deployment rather than a hope about it.
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const watcherIntervalSeconds = deployedWatcherIntervalSeconds(server);
  expect(
    watcherIntervalSeconds * REQUIRED_WATCHER_TICKS * 1_000,
    `HARNESS PREREQUISITE: ${REQUIRED_WATCHER_TICKS} expiration-watcher ticks of `
    + `${watcherIntervalSeconds}s must fit inside the ${PREPARED_RENEWAL_MS}ms renewal budget, or a `
    + 'red result would only prove the sweep had no chance to run',
  ).toBeLessThanOrEqual(PREPARED_RENEWAL_MS);

  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  agentId = '';

  const uncaught: string[] = [];
  // Attached before the first navigation: a render crash happens during a
  // render, and a listener added afterwards misses the one that mattered. This
  // card is new render code reading a route no page has ever read.
  page.on('pageerror', (error) => uncaught.push(error.message));

  // Every GET the page issues, in order. Each Refresh below is asserted as a
  // DELTA around its own click rather than as a total: the page re-mounts after
  // a save, and a total would count that mount as a refresh.
  const gets: string[] = [];
  page.on('request', (req) => {
    if (req.method() === 'GET') gets.push(new URL(req.url()).pathname);
  });
  const hits = (pathname: string) => gets.filter((seen) => seen === pathname).length;

  // ── An Agent that has NOT been told to keep a sandbox ready ──────────────
  // Prewarm starts off so that turning it on is a real user action on the page,
  // and so the first thing the card is asked is the empty question.
  const agentName = `__e2e_warm_capacity_${Date.now()}_${test.info().workerIndex}`;
  const created = await api.createAgent({
    name: agentName,
    model: await api.configuredAgentModel(RESEARCH_AGENT, SHARED_ENVIRONMENT),
    environment_name: SHARED_ENVIRONMENT,
    prewarm_enabled: false,
  });
  agentId = String(created.agent_id || '').trim();
  expect(agentId, 'the probe Agent must have an id').not.toEqual('');
  test.info().annotations.push({
    type: 'warm_capacity_scene',
    description: JSON.stringify({ agentId, agentName, environment: SHARED_ENVIRONMENT }),
  });
  expect(
    preparedSlotFacts(agentId).hasManifest,
    'an Agent created with prewarm off must hold no prepared manifest, or A1 would be asking the '
    + 'card about a slot that exists',
  ).toBe(false);

  const preparedRuntimePath = apiPath(`/agents/${agentId}/prepared-runtime`);
  const warmCard = card(page, WARM_CAPACITY_HEADING);

  /**
   * Click Refresh and take the card, the wire and the row as one sample.
   *
   * The answer is captured from the page's OWN response rather than fetched
   * separately, so "what the route said" and "what the card rendered" cannot
   * drift apart between two calls — which is what lets a red split cleanly into
   * the route lied and the page lied.
   */
  const refreshAndSample = async (): Promise<{
    reading: CardReading;
    answer: PreparedRuntimeAnswer;
    row: SlotFacts;
  }> => {
    const asked = hits(preparedRuntimePath);
    const answered = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === preparedRuntimePath
        && response.request().method() === 'GET',
    );
    await warmCard.getByRole('button', { name: 'Refresh' }).click();
    const response = await answered;
    const envelope = (await response.json()) as { data?: PreparedRuntimeAnswer };
    const answer = (envelope.data ?? envelope) as PreparedRuntimeAnswer;
    const reading = await readWarmCapacity(warmCard);
    const row = preparedSlotFacts(agentId);
    // A7 — a changed card is a re-asked question, counted as a delta around
    // this click alone.
    await expect
      .poll(() => hits(preparedRuntimePath), {
        message: `Refresh must re-ask ${preparedRuntimePath} once, not redraw the answer it had`,
      })
      .toBe(asked + 1);
    neverPromisesAStaleSlot(reading, row, 'after a Refresh click');
    return { reading, answer, row };
  };

  // ── A1: honest emptiness ─────────────────────────────────────────────────
  // Pin the console language before the FIRST navigation (see test.use above):
  // the persisted 'astrabox-lang' outranks navigator in the detection order.
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });
  // `.catch` so a missing card fails on the visibility assertion below with its
  // own sentence rather than on a bare Playwright response timeout, and so the
  // pending wait cannot outlive the test as an unhandled rejection.
  const mounted = page
    .waitForResponse(
      (response) =>
        new URL(response.url()).pathname === preparedRuntimePath
        && response.request().method() === 'GET',
    )
    .catch(() => null);
  await page.goto(appPath(`/manage/agents/${encodeURIComponent(agentId)}`));
  await expect(
    warmCard,
    'the Agent page must carry a warm-capacity card: an operator asking whether the next '
    + 'conversation starts warm has nowhere else to ask, and nothing under frontend/src reads '
    + `GET ${preparedRuntimePath} today`,
  ).toBeVisible();
  expect(
    await mounted,
    `the card must ask ${preparedRuntimePath} when it mounts — a panel that renders from the Agent `
    + 'record alone can only repeat the switch, which is the setting and not the capacity',
  ).not.toBeNull();

  const empty = await readWarmCapacity(warmCard);
  const emptyRow = preparedSlotFacts(agentId);
  neverPromisesAStaleSlot(empty, emptyRow, 'on first paint');
  expect(
    empty.states,
    'with warm start off and no manifest on the row, the card must say exactly that and nothing '
    + `else — an empty card reads as a calm one. The card read:\n${empty.text}`,
  ).toEqual(['off']);

  // ── The user action: keep a sandbox ready ────────────────────────────────
  // The Agent config write has always been one of the few triggers that build a
  // slot at all, and it is the one an operator actually has.
  const runtimeCard = card(page, 'Runtime and availability');
  await runtimeCard.getByRole('switch', { name: 'Keep a sandbox ready' }).click();
  const saved = page.waitForResponse(
    (response) =>
      response.url().includes(`/agents/${agentId}`)
      && response.request().method() === 'PUT'
      && response.ok(),
  );
  await runtimeCard.getByRole('button', { name: 'Save', exact: true }).click();
  await saved;

  // ── The preparation itself, waited for on the row, not on the page ───────
  // An engine adapter with no input-free prepare seam never publishes a manifest
  // (prepared_slots.py:628-637). That is a deployment fact and it belongs here,
  // before the card is judged, rather than read back later as a missing report.
  await expect.poll(async () => {
    const status = await platform.preparedRuntime(agentId);
    return Boolean(
      status.ready === true
      && Number(status.prepared_count || 0) > 0
      && String(status.sandbox_id || '').trim(),
    );
  }, {
    timeout: PREPARED_SLOT_MS,
    intervals: [2_000],
    message: `Agent ${agentId} must prepare a claimable slot on ${SHARED_ENVIRONMENT} after the `
      + 'console save; without one there is nothing for the card to report',
  }).toBe(true);

  const fresh = preparedSlotFacts(agentId);
  expect(fresh.state, 'the prepared manifest must be claimable before the card is asked').toBe('prepared');
  expect(
    fresh.placement,
    'Agent tenancy prepares a shared slot; a conversation-tenancy pool carries no age and this '
    + 'journey cannot be posed against it',
  ).toBe('shared_slot');
  expect(fresh.slotId, 'the manifest must name its slot').not.toEqual('');
  expect(fresh.preparedAt, 'the manifest must carry the instant its TTL is measured from').not.toEqual('');

  // ── A2 and A3: it reports, it reports the truth, and it reports freshness ─
  const warm = await refreshAndSample();
  expect(
    warm.reading.states,
    'with a claimable slot on the row the card must say the next conversation starts warm, and say '
    + `only that. The card read:\n${warm.reading.text}`,
  ).toEqual(['warm']);
  expect(
    warm.reading.text.includes(fresh.sandboxId.slice(0, 8)),
    `the card must name the box that is standing by (${fresh.sandboxId}) — a card that only echoed `
    + `the switch would read the same with no box at all. The card read:\n${warm.reading.text}`,
  ).toBe(true);
  expect(
    String(warm.answer.sandbox_id || '').trim(),
    'and the answer the card rendered must be about that same box',
  ).toBe(fresh.sandboxId);
  const stamped = warm.reading.stamps
    .map((value) => Date.parse(value))
    .filter((value) => Number.isFinite(value));
  expect(
    stamped.length,
    'the card must carry the slot\'s freshness as a machine-readable instant (a <time datetime>), '
    + 'not only as a boolean: "warm start is on" and "the NEXT conversation starts warm" are '
    + `different answers, and only the second one is the operator's question. The card read:\n${warm.reading.text}`,
  ).toBeGreaterThanOrEqual(1);
  const preparedAtMs = Date.parse(fresh.preparedAt);
  expect(
    stamped.map((value) => Math.abs(value - preparedAtMs)).sort((a, b) => a - b)[0],
    'one of the card\'s instants must be the moment the standing slot was prepared '
    + `(${fresh.preparedAt}) — that is the instant the platform measures the TTL from, so it is `
    + 'the only one an operator can check the promise against',
  ).toBeLessThanOrEqual(FRESHNESS_TOLERANCE_MS);
  test.info().annotations.push({
    type: 'first_prepared_slot',
    description: JSON.stringify({ slotId: fresh.slotId, sandboxId: fresh.sandboxId, preparedAt: fresh.preparedAt }),
  });

  // ── The hour nobody used this Agent ──────────────────────────────────────
  // One statement guarded down to the slot id, and its RETURNING projection is
  // also the read-back: the aged value is asserted from the statement that wrote
  // it, so a lost CAS can never be read as a pass. This is the near end of the
  // sandwich described in the header.
  const agedAtIso = new Date(Date.now() - IDLE_HOUR_MS).toISOString();
  expect(
    backdatePreparedSlot(agentId, fresh.slotId, agedAtIso),
    'the backdate must land on exactly the manifest the card just promised',
  ).toEqual([{
    agent_id: agentId,
    sandbox_id: fresh.residentSandboxId,
    slot_id: fresh.slotId,
    state: 'prepared',
    prepared_at: agedAtIso,
  }]);

  // ── A5: it stops promising ───────────────────────────────────────────────
  const stale = await refreshAndSample();
  // The far end of the sandwich. A manifest is only ever replaced forward, so
  // the same slot id on the row here means the sample above was unambiguously
  // about the backdated one. Losing this race is a fact about the deployment's
  // sweep cadence and is reported in those words — never quietly dropped, which
  // would leave the seed defect unasserted while the run went green.
  expect(
    stale.row.slotId,
    `HARNESS: the ${watcherIntervalSeconds}s expiration watcher replaced slot ${fresh.slotId} `
    + 'before a single Refresh could be sampled, so this run never observed its own backdated '
    + 'manifest and the stale-promise assertions below would be about a healthy slot. Re-run; if '
    + 'it recurs, the deployment\'s watcher interval is too short to hold the window open.',
  ).toBe(fresh.slotId);
  expect(
    stale.row.preparedAt,
    'and it must still be the aged manifest, not a restamped one',
  ).toBe(agedAtIso);
  expect(
    stale.reading.states,
    'an hour-old slot is one the claim path refuses ("prepared unit exceeded its TTL", '
    + 'prepared_slots.py:1004-1015), so the card must say the prepared sandbox has expired and is '
    + `being renewed — never that the next conversation starts warm. The card read:\n${stale.reading.text}`,
  ).toEqual(['renewing']);
  expect(
    stale.answer.ready,
    `GET ${preparedRuntimePath} answered ready=true for a manifest prepared ${agedAtIso}. `
    + '`ready` is computed as `enabled and state == "prepared"` (agent_service.py:254) with no '
    + 'reference to the manifest\'s age, while the claim path refuses it — so the route itself is '
    + 'what promised warmth the next conversation will not get, and no page can be written against it.',
  ).toBe(false);

  // ── A6: the background keeps it fresh with nobody in the product ─────────
  // From here to the verdict nothing is touched: a conversation, an Agent or
  // extension write and a server restart are each a refill trigger, and any one
  // of them would rebuild the slot for a harness reason.
  let renewed = preparedSlotFacts(agentId);
  try {
    await expect.poll(() => {
      renewed = preparedSlotFacts(agentId);
      return Boolean(
        renewed.hasManifest
        && renewed.state === 'prepared'
        && renewed.slotId !== fresh.slotId
        && Date.now() - Date.parse(renewed.preparedAt) < PREPARED_SLOT_TTL_MS,
      );
    }, {
      timeout: PREPARED_RENEWAL_MS,
      intervals: [2_000],
      message:
        'an Agent whose slot went stale must be re-armed in the background, not at the next '
        + `user's expense: slot ${fresh.slotId} has been an hour old for `
        + `${Math.round(PREPARED_RENEWAL_MS / 1_000)}s and with no conversation, no config write `
        + 'and no restart the row still carries it',
    }).toBe(true);
  } catch (error) {
    // A failed second build is a capacity fact, not a page defect, and the row
    // is where the product wrote the reason (agent_service.py:487-493).
    const row = preparedSlotFacts(agentId);
    const diagnosis = `agent=${agentId} stale_slot=${fresh.slotId} box=${fresh.sandboxId} `
      + `watcher_interval=${watcherIntervalSeconds}s row_now=${JSON.stringify(row)} `
      + `prepared_runtime_error=${JSON.stringify(row.lastError)}`;
    await test.info().attach('why-the-renewal-never-came', {
      body: diagnosis,
      contentType: 'text/plain',
    });
    throw new Error(`${String((error as Error).message)}\n\n${diagnosis}`);
  }
  expect(
    sessions,
    'the renewal must be background-paid: this spec starts no conversation, which is the whole '
    + 'point of asking whether the NEXT one starts warm',
  ).toHaveLength(0);
  expect(
    documentsByField('sessions', '$.agent_id', agentId),
    'and none exists on the row either — a Session that missed its claim schedules the identical '
    + 'reconciliation (prepared_slots.py:927) and would turn this green for the wrong reason',
  ).toHaveLength(0);

  // ── The operator comes back and asks again ───────────────────────────────
  const again = await refreshAndSample();
  expect(
    again.reading.states,
    `after the background renewal the card must report the current slot, not a cached answer. The `
    + `card read:\n${again.reading.text}`,
  ).toEqual(['warm']);
  expect(
    again.reading.text.includes(renewed.sandboxId.slice(0, 8)),
    `the card must name the box the REPLACEMENT slot occupies (${renewed.sandboxId})`,
  ).toBe(true);
  const renewedStamps = again.reading.stamps
    .map((value) => Date.parse(value))
    .filter((value) => Number.isFinite(value));
  expect(
    renewedStamps.length,
    'the renewed card must still carry a machine-readable instant; asserted separately from the '
    + `value below so "no <time> at all" and "the wrong <time>" read differently. The card read:\n${again.reading.text}`,
  ).toBeGreaterThanOrEqual(1);
  const renewedPreparedAtMs = Date.parse(renewed.preparedAt);
  expect(
    renewedStamps.map((value) => Math.abs(value - renewedPreparedAtMs)).sort((a, b) => a - b)[0],
    `the card must name the NEW freshness instant (${renewed.preparedAt}); still showing the aged `
    + `one (${agedAtIso}) would mean the page is reporting a manifest that no longer exists`,
  ).toBeLessThanOrEqual(FRESHNESS_TOLERANCE_MS);
  await test.info().attach('warm-capacity-across-a-renewal', {
    body: JSON.stringify({
      agentId,
      watcherIntervalSeconds,
      staleSlotId: fresh.slotId,
      staleSandboxId: fresh.sandboxId,
      agedAt: agedAtIso,
      renewedSlotId: renewed.slotId,
      renewedSandboxId: renewed.sandboxId,
      renewedPreparedAt: renewed.preparedAt,
    }),
    contentType: 'application/json',
  });

  // ── A8 ───────────────────────────────────────────────────────────────────
  expect(
    uncaught,
    `uncaught exception while reading an Agent's warm capacity:\n${uncaught.join('\n')}`,
  ).toEqual([]);
});
