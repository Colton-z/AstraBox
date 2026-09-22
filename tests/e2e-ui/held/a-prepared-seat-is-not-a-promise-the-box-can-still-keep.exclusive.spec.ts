/**
 * E2E: a seat prepared while a shared box had room is still handed out after
 * the box filled up.
 *
 * THE JOURNEY. One Agent on Agent tenancy. A user starts a conversation, which
 * claims the Agent's prepared seat and lands in the Agent's box. The platform
 * refills, publishing the NEXT seat into that same box — a promise made while
 * there was room. That conversation then does real work in its own Terminal
 * panel and grows well past the footprint it was admitted at. A second user
 * arrives and starts a conversation on the same Agent.
 *
 * WHAT THE PRODUCT DOES, read in the code rather than inferred.
 * The room question is asked exactly once, when the seat is BUILT:
 * `_place_initial_shared_slot` goes through `lease.place_in_agent_box`
 * (prepared_slots.py:485-498), whose admission predicate is `_has_room` —
 * "whether this box can carry one more conversation without dying"
 * (shared_sandbox_lease.py:1117-1185). It is never asked again. `claim_prepared_slot`
 * gates on state, runtime generation and the 30-minute TTL and nothing else
 * (prepared_slots.py:1004-1018), and the claim's `placement == "shared_slot"`
 * branch simply does `backend_adapter.connect(sandbox_id)` and proceeds
 * (engine/provisioning.py:1693-1714) — `place_in_agent_box` is not on that path,
 * so `_has_room` is not on it either. A seat approved ten minutes ago is cashed
 * now, in 56ms, against a box that has meanwhile stopped being able to honour it.
 *
 * That is the same family as the prepared-slot TTL that only healed when a user
 * showed up: the one reading of a shared box's memory is taken when somebody
 * arrives, and nothing between then and the next arrival is allowed to change
 * the answer the Agent row is advertising.
 *
 * WHERE THE VERDICT SITS, and what was deliberately NOT built. The visible
 * consequence of a shared box crossing its limit is an OOM, and the kernel picks
 * its victim by badness — the biggest allocator, which here is the conversation
 * that grew. A spec resting on that would pass by accident and prove nothing,
 * and rigging `oom_score_adj` would be the harness manufacturing its own red. So
 * the verdict is the PLACEMENT decision, which is boolean, observable and caused
 * by the same root. The memory is still spent for real and both turns are still
 * run; the OOM consequence is exercised, it is just not what decides the test.
 *
 * ORDERING, and why the headline is judged before the two turns. A full box is
 * exactly the condition under which a turn can hang, and a hang inside the
 * lane's wall converts a deterministic boolean into a timeout whose cause an
 * operator has to reconstruct. The placement assertion is therefore taken the
 * moment user two is READY, and the concurrent turns run after it. With the
 * defect present the run stops at the headline and the turns do not execute;
 * with it fixed they do, and they are the journey's closing promise.
 *
 * WHAT IS NOT ASSERTED, named rather than implied. Nothing here proves that a
 * background reading of a shared box's memory exists or runs, because none does.
 * `ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS` is the tick a future sampler
 * would ride and its default (300s) exceeds the whole test budget, so a "survives
 * a watcher tick" assertion cannot be written. There is also no
 * operator-visible reading of any box's memory: the memory block on
 * `GET /admin/system/overview` is the API process's own RSS, not a sandbox's.
 *
 * DESTRUCTIVENESS, and why this is an exclusive spec on its own Agent. It
 * deliberately leaves a box with less free memory than one conversation's floor
 * and then puts a second conversation into it, so the box may die. The box is
 * this test's own — its Agent is created here — and it must never run on the
 * matrix Agent, whose box is shared with every co-resident conversation. A failed
 * run KEEPS the box (that is the scene); the holder self-expires after
 * HOLD_SECONDS so a kept box does not reserve node memory indefinitely.
 *
 * ENGINE. No engine vocabulary is read: the growth is spent through the
 * conversation's own Terminal panel and every oracle is platform-side, so this
 * runs under whichever profile the matrix selected. It does require the
 * deployment's Agent-tenancy prewarm Environment, which is read and never
 * written — a conversation-tenancy Agent has no shared box to fill.
 *
 * BUDGET is the real operational risk and it is not verifiable without running.
 * Two prepared-slot builds dominate (the first seat, then the refill). Every
 * wait below is `parseTimeoutEnv`-tunable so the lane can tighten it, user one
 * takes no model turn before the growth, and the pressure window is short. If a
 * round still overruns, the honest cut is the two concurrent turns at the end;
 * the headline does not depend on them.
 */
import { execFileSync } from 'node:child_process';

import { expect, test, type Locator, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField } from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { expectPromptDelivered, startPromptDelivery } from '../fixtures/sessionPage';

// Deployment fixtures. Read and asserted present, never created or rewritten:
// the lane resolves the Agent-tenancy prewarm Environment per engine profile and
// validates it against the running agent image, and the research Agent is where
// a model route this deployment has actually proven comes from.
const SHARED_ENVIRONMENT = String(
  process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT || '',
).trim();
const RESEARCH_AGENT = String(
  process.env.ASTRABOX_E2E_RESEARCH_AGENT || '',
).trim();

// Every per-step wait is tunable so the lane can trade budget against a loaded
// node. The test budget itself is never stated here — the runner owns it.
const PREPARED_SLOT_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_PREPARED_SLOT_TIMEOUT_MS', 90_000);
const BOX_PRESSURE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_BOX_PRESSURE_TIMEOUT_MS', 45_000);
const PRESSURE_WINDOW_MS = parseTimeoutEnv('ASTRABOX_E2E_SHARED_BOX_PRESSURE_WINDOW_MS', 15_000);
const SHARED_TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_SHARED_TURN_TIMEOUT_MS', 120_000);
const CONVERSATION_START_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_AGENT_START_TIMEOUT_MS', 120_000);
const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 60_000);
const TERMINAL_READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TERMINAL_READY_TIMEOUT_MS', 60_000);
const BOX_WORKER_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_BOX_MEMORY_WORKER_TIMEOUT_MS', 60_000);

// The holder outlives the whole test on purpose — a failed run keeps the box —
// and then releases the node's memory without anyone returning to it.
const HOLD_SECONDS = 600;
const HOLD_LOG = '/tmp/astrabox-e2e-seat-hold.log';

/** The box's own numbers, and the platform's own verdict over them. */
interface BoxMemory {
  limit: number;
  current: number;
  reserve: number;
  /** `SharedSandboxLease._has_room` — the exact predicate the claim path skips. */
  hasRoom: boolean;
}

/**
 * Ask the running server what the shared box can still take.
 *
 * The established worker shape for machinery a browser cannot reach: `docker
 * exec` the deployment's server container and call the REAL production
 * functions (ownerless-sandbox-reaping-preserves-prepared-capacity:103-111).
 * Nothing is re-implemented and no threshold is copied into this file —
 * `CONVERSATION_MEMORY_RESERVE_BYTES` is imported, the limit and current use come
 * from `read_memory_headroom`, and the admission answer comes from `_has_room`
 * itself. A private is called deliberately: a re-derivation of its arithmetic
 * here would be a second copy of the rule under test, free to drift away from
 * the one the platform actually applies.
 */
function readBoxMemory(input: { backend: string; sandboxId: string }): BoxMemory {
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const program = `
import asyncio
import json
import sys

from astrabox.bootstrap import bootstrap
from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import (
    CONVERSATION_MEMORY_RESERVE_BYTES,
    SharedSandboxLease,
)
from astrabox.deploy.onebox import ensure_database_wiring, needs_sandbox_server, _export_backend_wiring
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.seams.sandbox import sandbox_for_name

async def main():
    ensure_database_wiring()
    if needs_sandbox_server():
        _export_backend_wiring()
    bootstrap()
    given = json.loads(sys.argv[1])
    provider = sandbox_for_name(given['backend'])
    box = given['sandboxId']
    limit, current = await provider.read_memory_headroom(box)
    lease = SharedSandboxLease(agent_repo=AgentRepository(), provider=provider)
    has_room = await lease._has_room(box)
    print('E2E_BOX_MEMORY=' + json.dumps({
        'limit': int(limit),
        'current': int(current),
        'reserve': int(CONVERSATION_MEMORY_RESERVE_BYTES),
        'hasRoom': bool(has_room),
    }))

asyncio.run(main())
`;
  const raw = execFileSync('docker', ['exec', server, 'python', '-c', program, JSON.stringify(input)], {
    encoding: 'utf8',
    timeout: BOX_WORKER_TIMEOUT_MS,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const line = raw.split('\n').find((candidate) => candidate.startsWith('E2E_BOX_MEMORY='));
  expect(line, 'the production memory reading must publish its result').toBeTruthy();
  return JSON.parse(line!.slice('E2E_BOX_MEMORY='.length)) as BoxMemory;
}

/** The manifest's PUBLIC coordinates — what identifies a seat, and nothing else. */
interface SeatFacts {
  slotId: string;
  state: string;
  placement: string;
  sandboxId: string;
  isolatedSessionId: string;
}

/**
 * Read one Agent's prepared seat without letting its secrets into an assertion.
 *
 * `_prepared_slot` also carries the activation token and the model credential,
 * and a failed `expect()` retains and prints both sides of its comparison. Only
 * the named coordinates are lifted out of it.
 */
function preparedSeat(agentId: string): SeatFacts {
  const rows = documentsByField('agents', '$.agent_id', agentId);
  expect(rows, 'the preparation under test must belong to exactly one Agent row').toHaveLength(1);
  const manifest = (rows[0]._prepared_slot || {}) as Record<string, unknown>;
  return {
    slotId: String(manifest.slot_id || '').trim(),
    state: String(manifest.state || '').trim(),
    placement: String(manifest.placement || '').trim(),
    sandboxId: String(manifest.sandbox_id || '').trim(),
    isolatedSessionId: String(manifest.isolated_session_id || '').trim(),
  };
}

/**
 * Start a conversation the way a person does: from the Agent's card.
 *
 * The picker card carries exactly one button (AgentHome.tsx:182), so the action
 * is addressable without pinning a locale for its label.
 */
async function startConversationFromCard(page: Page, agentName: string): Promise<string> {
  await page.goto(appPath('/agents'));
  const card = page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`);
  await expect(card, `${agentName} must appear in the Agent picker`).toBeVisible({
    timeout: READY_TIMEOUT_MS,
  });
  await card.getByRole('button').click();
  await page.waitForURL((url) => /\/sessions\/[^/]+$/.test(url.pathname), {
    timeout: CONVERSATION_START_TIMEOUT_MS,
  });
  const sessionId = new URL(page.url()).pathname.split('/').filter(Boolean).pop() || '';
  expect(sessionId, 'the Agent card must open a concrete conversation').not.toEqual('');
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: READY_TIMEOUT_MS });
  return sessionId;
}

/** Open the conversation's own Terminal panel and return its command input. */
async function openTerminalPanel(page: Page): Promise<Locator> {
  await page.getByRole('tab', { name: 'Terminal' }).click();
  // The panel says it is ready before it will accept anything; without this the
  // command lands in a disabled input and the growth never happens.
  await expect(
    page.getByText('Terminal ready — type a command below.'),
    'the Terminal panel must announce readiness before a command is typed',
  ).toBeVisible({ timeout: TERMINAL_READY_TIMEOUT_MS });
  return page.getByPlaceholder('Type a command…');
}

async function runInTerminal(input: Locator, command: string): Promise<void> {
  await input.fill(command);
  await input.press('Enter');
}

// The one localized surface this spec reads is the Terminal panel's readiness
// line. The console detects language as ['localStorage','navigator'] with
// fallbackLng 'en', so an unpinned runner locale is what decides the spelling —
// and a spec that accepts two spellings would also accept a third nobody wrote
// it against. Pin the navigator locale here and the persisted 'astrabox-lang'
// below, which outranks it.
test.use({ locale: 'en-US' });

const sessions = trackSessions();
let agentId = '';
// Registered after the tracker, because afterEach hooks run in registration
// order: the conversations go before the Agent they ran on. A failure keeps
// both — including the filled box, which IS the scene.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('a conversation is not handed a prepared seat in a shared box that filled up after the seat was built', async ({
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

  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const uncaught: string[] = [];
  page.on('pageerror', (error) => uncaught.push(`tab1: ${error.message}`));

  // Pin the console language before the FIRST navigation, and on the context so
  // that user two's tab inherits it (see test.use above).
  await page.context().addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });

  // ── An Agent with real warm capacity ─────────────────────────────────────
  // Authoring stays on the API: a user picks an Agent that already exists rather
  // than creating one in order to have a conversation. The Agent is this spec's
  // own because its box is about to be filled — see DESTRUCTIVENESS above.
  const agentName = `__e2e_stale_seat_${Date.now()}_${test.info().workerIndex}`;
  const created = await api.createAgent({
    name: agentName,
    model: await api.configuredAgentModel(RESEARCH_AGENT, SHARED_ENVIRONMENT),
    environment_name: SHARED_ENVIRONMENT,
    prewarm_enabled: true,
  });
  agentId = String(created.agent_id || '').trim();
  expect(agentId, 'the probe Agent must have an id').not.toEqual('');

  let readout: Record<string, unknown> = {};
  await expect.poll(async () => {
    readout = await platform.preparedRuntime(agentId);
    return Boolean(
      readout.ready === true
      && String(readout.sandbox_id || '').trim()
      && String(readout.client_pool_name || '').trim(),
    );
  }, {
    timeout: PREPARED_SLOT_TIMEOUT_MS,
    intervals: [2_000],
    message:
      `Agent ${agentId} must prepare real claimable capacity on ${SHARED_ENVIRONMENT} before a `
      + 'user can be handed a seat in it',
  }).toBe(true);
  const firstSeat = preparedSeat(agentId);
  expect(
    firstSeat.placement,
    'this journey needs Agent tenancy — a conversation-tenancy Agent has no shared box to fill',
  ).toBe('shared_slot');
  expect(firstSeat.slotId, 'the first prepared seat must name its slot').not.toEqual('');

  // ── User one arrives and takes that seat ─────────────────────────────────
  const sessionA = await startConversationFromCard(page, agentName);
  sessions.push(sessionA);
  await api.waitForSessionReady(sessionA, READY_TIMEOUT_MS);
  const detailA = await api.adminSessionDetail(sessionA);
  const boxX = String(detailA.sandbox_id || '').trim();
  const backend = String(detailA.sandbox_backend || '').trim();
  expect(boxX, 'the first conversation must name the shared box it was placed in').not.toEqual('');
  expect(backend, 'the first conversation must name its sandbox backend').not.toEqual('');

  // ── The promise is made while there is still room ────────────────────────
  // The refill publishes the NEXT seat. Asserting it is a DIFFERENT slot in the
  // SAME box is the premise of everything below: this seat was approved after
  // boxX already held user one, by the one predicate that asks about room.
  // A seat published somewhere else means the fixture is not the scene this spec
  // is about, and that must stop the run here rather than be discovered later.
  let seat: SeatFacts = firstSeat;
  await expect.poll(() => {
    seat = preparedSeat(agentId);
    return seat.state === 'prepared' && seat.sandboxId === boxX && seat.slotId !== firstSeat.slotId;
  }, {
    timeout: PREPARED_SLOT_TIMEOUT_MS,
    intervals: [2_000],
    message:
      `the refill must publish a NEW prepared seat into box ${boxX} — the box that already holds `
      + `conversation ${sessionA}. Without that this run proves nothing about a seat approved while `
      + 'there was room',
  }).toBe(true);
  const seatIsolatedId = seat.isolatedSessionId;
  expect(seatIsolatedId, 'the prepared seat must name its isolation session').not.toEqual('');

  // ── The platform's own eyes on the box, before it is touched ─────────────
  const beforeGrowth = readBoxMemory({ backend, sandboxId: boxX });
  expect(
    beforeGrowth.limit,
    'a box with no memory limit is never packed, so this journey cannot be built on one',
  ).toBeGreaterThan(0);
  expect(
    beforeGrowth.hasRoom,
    `box ${boxX} must still be able to take another conversation at the moment its seat is `
    + `advertised — that is what "the promise was made while there was room" means `
    + `(${JSON.stringify(beforeGrowth)})`,
  ).toBe(true);
  expect(
    beforeGrowth.limit - beforeGrowth.current,
    'the same statement in the platform\'s own numbers',
  ).toBeGreaterThanOrEqual(beforeGrowth.reserve);

  // ── User one does real work that grows the box ───────────────────────────
  // Through the conversation's own Terminal panel, so the growth is a real user
  // action on a real product surface rather than something done to the box from
  // outside. Sized from a reading taken with the panel already open, so the
  // panel's own footprint is inside the measurement rather than drift after it.
  const terminal = await openTerminalPanel(page);
  const sizing = readBoxMemory({ backend, sandboxId: boxX });
  const hold = (sizing.limit - sizing.current) - Math.floor(sizing.reserve * 0.45);
  expect(
    hold,
    `box ${boxX} has more free memory than this spec can spend while still leaving it short of one `
    + `conversation's floor (${JSON.stringify(sizing)}); the journey cannot be built on this `
    + 'deployment\'s box size',
  ).toBeGreaterThan(0);
  // Anonymous, resident memory: a file would be page cache, which the kernel can
  // reclaim, so `memory.current` would rise without the box actually being
  // unable to carry another conversation — a number that does not mean what the
  // assertion below says it means. `setsid` + `nohup` keep the holder alive past
  // the panel, and it self-expires so a kept box releases the node's memory.
  await runInTerminal(
    terminal,
    `nohup setsid python3 -u -c 'import sys,time; n=int(sys.argv[1]); b=bytearray(n); `
    + `[b.__setitem__(i,1) for i in range(0,n,4096)]; print("HELD",n,flush=True); `
    + `time.sleep(${HOLD_SECONDS})' ${hold} >${HOLD_LOG} 2>&1 &`,
  );

  // ── The condition is confirmed with the same oracle the platform uses ────
  let pressured: BoxMemory = sizing;
  await expect.poll(() => {
    pressured = readBoxMemory({ backend, sandboxId: boxX });
    return pressured.hasRoom;
  }, {
    timeout: BOX_PRESSURE_TIMEOUT_MS,
    intervals: [2_000],
    message:
      `box ${boxX} must cross the platform's own refusal line once user one's work is resident; `
      + `sized from ${JSON.stringify(sizing)}`,
  }).toBe(false);
  expect(
    pressured.limit - pressured.current,
    `box ${boxX} must be past the refusal line WITHOUT being pushed into an OOM: the failure this `
    + `spec is about is a placement decision, not a kernel kill (${JSON.stringify(pressured)})`,
  ).toBeGreaterThan(Math.floor(pressured.reserve * 0.25));
  await test.info().attach('shared-box-memory', {
    body: JSON.stringify({ boxX, backend, beforeGrowth, sizing, hold, pressured }),
    contentType: 'application/json',
  });
  // The product's own surface agreeing that the work is the conversation's: the
  // holder reports its allocation and the panel renders it.
  await runInTerminal(terminal, `cat ${HOLD_LOG}`);
  await expect(
    page.getByText(`HELD ${hold}`).first(),
    'the Terminal panel must show that the conversation\'s own command holds the memory',
  ).toBeVisible({ timeout: TERMINAL_READY_TIMEOUT_MS });

  // ── The stale promise is still standing ─────────────────────────────────
  // The finding stated directly: the only reading of a shared box's memory is
  // taken when a user arrives, so the seat keeps saying yes. Read twice across a
  // bounded window, because "it was advertised at one instant" is a weaker claim
  // than "it stays advertised".
  const advertisedNow = await platform.preparedRuntime(agentId);
  expect(advertisedNow.ready, 'the seat is still advertised the moment the box went over').toBe(true);
  expect(String(advertisedNow.sandbox_id || '').trim()).toBe(boxX);
  expect(preparedSeat(agentId)).toEqual(seat);
  await new Promise((resolve) => setTimeout(resolve, PRESSURE_WINDOW_MS));
  const advertisedStill = await platform.preparedRuntime(agentId);
  expect(
    advertisedStill.ready,
    `after ${PRESSURE_WINDOW_MS}ms over the refusal line, nothing has withdrawn the seat in box `
    + `${boxX}: no reading of this box's memory happens between one user arriving and the next`,
  ).toBe(true);
  expect(String(advertisedStill.sandbox_id || '').trim()).toBe(boxX);
  expect(
    preparedSeat(agentId),
    'the same seat, unchanged — a refill replacing it would make the claim below a different test',
  ).toEqual(seat);

  // ── User two arrives ────────────────────────────────────────────────────
  const tab2 = await page.context().newPage();
  tab2.on('pageerror', (error) => uncaught.push(`tab2: ${error.message}`));
  const sessionB = await startConversationFromCard(tab2, agentName);
  sessions.push(sessionB);
  await api.waitForSessionReady(sessionB, READY_TIMEOUT_MS);
  const detailB = await api.adminSessionDetail(sessionB);
  const boxB = String(detailB.sandbox_id || '').trim();
  const isolatedB = String(detailB.runtime_identity?.isolated_session_id || '').trim();
  expect(
    isolatedB,
    'Agent tenancy places every conversation in its own isolation session, so user two must have one',
  ).not.toEqual('');
  // Diagnostic, not an assertion: on the fixed path user two correctly gets its
  // own box and the two conversations do NOT cohabit, so cohabitation cannot be
  // required — but which way it went is exactly what a reader of a red run wants.
  test.info().annotations.push({
    type: 'shared_box_placement',
    description: JSON.stringify({
      boxX, boxB, cohabiting: boxB === boxX, seatIsolatedId, isolatedB, sessionA, sessionB,
    }),
  });

  // ── HEADLINE ────────────────────────────────────────────────────────────
  // Both halves are asserted, and the first is what removes the false green: had
  // user two simply MISSED the claim for an unrelated reason (a TTL, a
  // generation, a concurrent claimer) it would fall through to
  // `place_in_agent_box` → `_has_room`, be refused, and get its own box — and the
  // box-id assertion alone would then pass for the wrong reason. Naming the
  // seat's isolation session proves which path user two actually took.
  const verdict =
    `seat ${seat.slotId} was prepared in box ${boxX} while it had room (${JSON.stringify(beforeGrowth)}) `
    + `and the box then went over the platform's own refusal line (${JSON.stringify(pressured)})`;
  expect(
    isolatedB,
    `PREPARED SEAT: user two must not be seated in a shared box that has no room for them — ${verdict}`,
  ).not.toBe(seatIsolatedId);
  expect(
    boxB,
    `PREPARED SEAT: a box the platform would refuse must not receive another conversation — ${verdict}`,
  ).not.toBe(boxX);

  // ── The user's actual promise ───────────────────────────────────────────
  // Two turns genuinely in flight at once, which is what this journey is: where
  // a box that went over its limit takes down the conversation that did nothing
  // wrong. Judged last and never relied on for the red — its outcome depends on
  // which process a kernel picks.
  const beforeA = await page.getByTestId('assistant-message').count();
  const beforeB = await tab2.getByTestId('assistant-message').count();
  const deliveryA = await startPromptDelivery(page, sessionA, 'Reply with the single word ALPHA.');
  const deliveryB = await startPromptDelivery(tab2, sessionB, 'Reply with the single word BETA.');
  await expectPromptDelivered(deliveryA);
  await expectPromptDelivered(deliveryB);
  for (const [tab, before, who] of [[page, beforeA, sessionA], [tab2, beforeB, sessionB]] as const) {
    // Count, not wording: the model's phrasing is its own business. The text is
    // then read back, because a failed turn renders its error INTO the
    // transcript as an assistant message and would otherwise satisfy the count.
    await expect
      .poll(() => tab.getByTestId('assistant-message').count(), { timeout: SHARED_TURN_TIMEOUT_MS })
      .toBeGreaterThan(before);
    const reply = tab.getByTestId('assistant-message').last();
    await expect(reply, `conversation ${who} must render a reply`).not.toBeEmpty();
    expect(
      (await reply.innerText()).trim(),
      `conversation ${who} must answer rather than report the box dying underneath it`,
    ).not.toMatch(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
    await expect(
      tab.getByTestId('run-view').getByTestId('status-pill').first(),
      `conversation ${who} must settle`,
    ).toHaveAttribute('data-pulse', 'false', { timeout: SHARED_TURN_TIMEOUT_MS });
  }

  expect(uncaught, `uncaught exception in the console:\n${uncaught.join('\n')}`).toEqual([]);
});
