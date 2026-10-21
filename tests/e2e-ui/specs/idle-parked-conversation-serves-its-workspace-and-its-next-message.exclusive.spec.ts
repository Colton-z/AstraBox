/**
 * A conversation parked for idleness reopens its original workspace and accepts
 * its next turn on the same sandbox. The Files panel wakes compute before the
 * next message; the conversation remains Ready throughout the return journey.
 *
 * This case changes a test RuntimeProfile and runs the deployment-wide idle sweep.
 * It retains the Session and sandbox on failure and deletes its Agent on pass.
 */
import { randomUUID } from 'node:crypto';

import { expect, test, type APIRequestContext, type BrowserContext, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, sqlLiteral } from '../fixtures/dbOracle';
import { apiPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import {
  closePageAfterAssertions,
  expectComposerEnabled,
  openSessionView,
  sendPrompt,
} from '../fixtures/sessionPage';

// This spec asserts the console's localized calm: the header must read "Ready"
// and never "Runtime disconnected" / "Recovery needed", and no "Recover session"
// button may exist. i18next detects ['localStorage','navigator'], so an unpinned
// runner locale decides which spelling those are — and a UI rendering another
// language would false-green every negative assertion, because it never emits
// these strings in any state. Pinned in both places a user's choice lands.
test.use({ locale: 'en-US' });

// The masked stand-in the server returns for a stored secret
// (agent_config_service.py:60). Cloning it under a NEW RuntimeProfile name resolves
// to "no stored value", so the clone would silently drop the credential and the
// failure would surface three steps later as an unexplained model error.
const REDACTION_SENTINEL = '••••••••';

// The product's own word for a committed box (`_PAUSED_STATES`,
// providers/open_sandbox/sandbox.py:130). Not a spelling invented here.
const PAUSED_STATES = new Set(['PAUSED', 'SUCCEED']);

// How long this Agent may sit quiet before the keeper may take its compute.
// Really waited out on the activity clock, not simulated; short because the wall
// is 180s, and read back off the stored Agent rather than trusted from here.
const IDLE_WINDOW_MS = parseTimeoutEnv('ASTRABOX_E2E_IDLE_PARK_WINDOW_MS', 5_000);
const IDLE_WINDOW_SECONDS = Math.max(1, Math.ceil(IDLE_WINDOW_MS / 1_000));

const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 90_000);
// One model turn rendering in the browser, either side of the park.
const REPLY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_IDLE_PARK_REPLY_TIMEOUT_MS', 60_000);
// The conversation settling idle and staying there past its window. Its own
// name rather than the sweep spec's `ASTRABOX_E2E_IDLE_QUIET_TIMEOUT_MS`: that
// default is derived from a different window, so one override would be wrong for
// whichever spec did not set it.
const QUIET_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_IDLE_PARK_QUIET_TIMEOUT_MS',
  IDLE_WINDOW_MS + 45_000,
);
// The sweep and snapshot share one budget. The periodic watcher may own the
// pause while the requested tick returns without visiting this marked binding.
const PARK_SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_PARK_SETTLE_MS', 90_000);
// The Files panel's own answer, whatever it turns out to be. Bounded by
// ASTRABOX_SANDBOX_CONTROL_DEADLINE_S on the failing side.
const FILE_PANEL_MS = parseTimeoutEnv('ASTRABOX_E2E_FILE_PANEL_TIMEOUT_MS', 60_000);

// Sessions are deleted only when the test passes; a failure keeps the scene and
// names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();
let agentId = '';

// Registration order is teardown order: the session goes first (trackSessions,
// above), then the Agent it ran on. The RuntimeProfile is left in place by design —
// its name is stable and there is no DELETE route for runtime_profiles.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

/** The sweep tick's own counters, or an empty tick that found nothing to do. */
function sweepCounters(payload: Record<string, unknown>): Record<string, number> {
  const summary = payload.summary;
  if (summary == null) return {};
  if (typeof summary !== 'object' || Array.isArray(summary)) {
    throw new Error(`sandbox-idle-sweep summary must be an object; got ${JSON.stringify(summary)}`);
  }
  return Object.fromEntries(
    Object.entries(summary as Record<string, unknown>).map(([key, value]) => [key, Number(value)]),
  );
}

/**
 * What the keeper and its candidate query read off this conversation's
 * `sessions` row. The quiet age is measured by the database from the latest of
 * the last output, the last change of activity and the last resume of the box,
 * which is the moment `_is_idle_past` (expiration_watcher.py) measures from.
 */
interface KeeperRow {
  runtime_status: string | null;
  open_responses: number;
  quiet_ms: number | null;
  sandbox_parked_at: string | null;
  deleted: boolean;
  runtime_unavailable: boolean;
  sandbox_id: string | null;
  state: string;
  expires_at: string | null;
  lease_live: boolean | null;
}

function keeperRow(sessionId: string): KeeperRow | null {
  return postgresRows<KeeperRow>(
    `SELECT s.runtime_status, `
      + `(SELECT count(*) FROM session_responses r `
      + `WHERE r.session_id = s.session_id AND r.status <> 'ended')::int AS open_responses, `
      + `(extract(epoch FROM now() - GREATEST(s.last_output_at, s.updated_at, s.sandbox_resumed_at)) `
      + `* 1000)::float8 AS quiet_ms, `
      + `s.sandbox_parked_at, s.deleted, s.runtime_unavailable, s.sandbox_id, s.state, s.expires_at, `
      + `s.expires_at > now() AS lease_live `
      + `FROM sessions s WHERE s.session_id = ${sqlLiteral(sessionId)}`,
  )[0] ?? null;
}

/**
 * The platform's own statement that this binding is parked.
 *
 * Read from the database rather than over HTTP, because between the walk-away
 * and the return this spec must not touch the session through anything that
 * could ensure or attach a runtime — it would then be undoing the condition it
 * came to observe (the discipline
 * reconcile-is-present-for-a-parked-session-without-recovering-it.exclusive.spec.ts
 * states). The keeper writes this before the asynchronous commit and clears it
 * on refusal. Completion also requires the supplier's paused state.
 */
function parkedMark(sessionId: string): string {
  return String(keeperRow(sessionId)?.sandbox_parked_at || '').trim();
}

/**
 * The runtime's last reported status, whether a response is still open, and
 * the milliseconds since the activity clock `_is_idle_past` reads last moved.
 */
function quietForMs(sessionId: string): { status: string; openResponses: number; quietMs: number | null } {
  const row = keeperRow(sessionId);
  if (!row) return { status: '<no session row>', openResponses: 0, quietMs: null };
  return {
    status: String(row.runtime_status ?? ''),
    openResponses: Number(row.open_responses),
    quietMs: row.quiet_ms === null ? null : Number(row.quiet_ms),
  };
}

/**
 * Restate the candidate query's predicate against this session's own row.
 *
 * Without it, "it was never parked" is a failure with no direction: a row the
 * sweep could not have looked at reads exactly like a keeper that refused. This
 * mirrors `list_idle_reclaim_candidates` in session_repository.py and names the
 * field that disqualified the row.
 */
function disqualifiedFromTheSweep(sessionId: string): string {
  const row = keeperRow(sessionId);
  if (!row) return 'the session row is gone';
  if (row.deleted) return 'the row is marked deleted';
  if (row.runtime_unavailable) return 'the row is marked runtime_unavailable';
  if (!String(row.sandbox_id || '').trim()) return 'the row no longer names a sandbox';
  const state = String(row.state || '');
  if (state === 'TERMINATED' || state === 'DELETED') return `the row reached ${state}`;
  if (row.lease_live === null) return 'the row has no sandbox lease (expires_at is empty)';
  if (!row.lease_live) {
    return `the sandbox lease lapsed at ${String(row.expires_at)} — the dead-binding sweep owns `
      + 'this row, not the idle sweep';
  }
  if (String(row.sandbox_parked_at || '').trim()) return 'the row is already parked';
  return '';
}

/** Whether any value carried by the cloned payload is a server-side redaction. */
function carriesRedactedSecret(value: unknown): boolean {
  if (typeof value === 'string') return value.includes(REDACTION_SENTINEL);
  if (Array.isArray(value)) return value.some(carriesRedactedSecret);
  if (value && typeof value === 'object') {
    return Object.values(value as Record<string, unknown>).some(carriesRedactedSecret);
  }
  return false;
}

async function exerciseParkedConversation(
  { page, context, request }: { page: Page; context: BrowserContext; request: APIRequestContext },
  disableEnvironment: boolean,
): Promise<void> {
  // Leave time to disable the RuntimeProfile before this new scene becomes idle.
  const idleWindowSeconds = disableEnvironment ? Math.max(15, IDLE_WINDOW_SECONDS) : IDLE_WINDOW_SECONDS;
  const idleWindowMs = disableEnvironment ? idleWindowSeconds * 1_000 : IDLE_WINDOW_MS;
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');

  // Both tabs, and every navigation of either, get the language the negative
  // assertions are written against. On the context rather than the page because
  // the return leg opens a second tab.
  await context.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });

  // ── PRECONDITION: this deployment can park at all ────────────────────────
  const capability = await platform.idleAction();
  const supported = (capability.supported_actions as unknown[] | undefined) || [];
  expect(
    supported.map((action) => String(action)),
    'this deployment\'s sandbox backend cannot snapshot, so `idle_action: "pause"` is refused '
      + 'at the RuntimeProfile form and this journey has no configuration in which it can pass or '
      + `fail. The route's own reason: ${String(capability.detail ?? '<none given>')} `
      + `(backend=${String(capability.backend ?? '<unnamed>')})`,
  ).toContain('pause');

  // ── A RuntimeProfile that parks, cloned from whichever engine the matrix chose ──
  const laneAgent = await api.defaultAgent();
  const sourceName = String(laneAgent.runtime_profile_name || '').trim();
  expect(
    sourceName,
    `the lane's Agent ${JSON.stringify(laneAgent.name)} must name the RuntimeProfile this clones`,
  ).not.toEqual('');
  const runtime_profiles = await platform.listEnvironments();
  const source = runtime_profiles.find((item) => String(item.name || '') === sourceName);
  expect(
    source,
    `RuntimeProfile ${JSON.stringify(sourceName)} is not on this deployment; have: `
      + runtime_profiles.map((item) => String(item.name || '')).join(', '),
  ).toBeTruthy();
  const sourceEnvironment = source as Record<string, unknown>;

  // Project through the product's own schema rather than a field list kept here:
  // a second copy of the editable surface silently stops carrying whatever field
  // is added to the first, and the validator only checks keys the schema
  // declares — so projecting also drops computed reads like `engine_available`.
  const schema = await platform.environmentSchema();
  const editableKeys = (schema.fields || [])
    .map((field) => String(field.key || ''))
    .filter((key) => key && key !== 'name');
  expect(
    editableKeys.length,
    'the environment schema must declare its editable fields',
  ).toBeGreaterThan(0);

  // Stable, not run-scoped: runtime_profiles have GET and PUT and no DELETE, so a
  // run-scoped name would leave one row per run on the deployment forever.
  const parkEnvironment = `__e2e-idle-park-${sourceName}`;
  const parkPayload = {
    ...Object.fromEntries(
      editableKeys
        .filter((key) => key in sourceEnvironment)
        .map((key) => [key, sourceEnvironment[key]]),
    ),
    // The only changed field. Everything else — engine_kind,
    // runtime_template_name, tenancy, networking — stays the profile's own, which
    // is what lets this spec run under every engine in the matrix.
    idle_action: 'pause',
  };
  expect(
    carriesRedactedSecret(parkPayload),
    'the source RuntimeProfile stores a secret the server returns masked, and a clone under a new '
      + 'name stores nothing for it. Point this lane at a RuntimeProfile that uses deployment '
      + 'model access, or the park RuntimeProfile will fail its first model call for a reason that '
      + 'has nothing to do with parking.',
  ).toBe(false);

  let storedEnvironment: Record<string, unknown>;
  try {
    storedEnvironment = await platform.putEnvironment(parkEnvironment, parkPayload);
  } catch (error) {
    throw new Error(
      'this deployment refused a RuntimeProfile that parks idle boxes, so the journey has no '
        + 'configuration in which it can pass or fail. Under idle_action "terminate" the keeper '
        + 'leaves every box alone and the box dies at its lease with the workspace instead.\n'
        + `${error instanceof Error ? error.message : String(error)}`,
    );
  }
  expect(
    String(storedEnvironment.idle_action || ''),
    'the stored RuntimeProfile must be the one the keeper will read',
  ).toEqual('pause');

  const model = String(laneAgent.model || '').trim();
  expect(model, 'the lane-selected Agent must name a model route').not.toEqual('');
  expect(
    await api.listEnvironmentModels(parkEnvironment),
    `the parking RuntimeProfile must expose the lane's model ${JSON.stringify(model)}`,
  ).toContain(model);

  // ── The conversation a user would leave ──────────────────────────────────
  const agent = await api.createAgent({
    name: `__e2e_idle_park_${runId}`,
    model,
    runtime_profile_name: parkEnvironment,
    // No prepared slot: this Agent must not spend its setup warming a box it
    // never claims, and a pooled box is not this conversation's alone.
    prepared_capacity: 0,
    idle_hibernate_seconds: idleWindowSeconds,
  });
  agentId = String(agent.agent_id || '');
  expect(agentId, 'the Agent must be created').not.toEqual('');
  // `createAgent` projects its payload through the authoring schema and drops
  // anything the schema does not declare. A dropped window is invisible: the
  // keeper would fall back to the 1800s deployment default and judge this Agent
  // by a clock nothing in this spec set, and nothing would park inside the wall.
  expect(
    Number((await api.getAgent(agentId)).idle_hibernate_seconds),
    'the Agent must carry the idle window this journey waits out',
  ).toBe(idleWindowSeconds);

  const created = await api.startConversation(agentId);
  const sessionId = String(created.session_id || '').trim();
  expect(sessionId, 'starting a conversation must open a session').not.toEqual('');
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId, READY_TIMEOUT_MS);
  const boxBefore = String(ready.sandbox_id || '').trim();
  expect(boxBefore, 'a READY conversation must name its box').not.toEqual('');
  test.info().annotations.push({
    type: 'idle_park_scene',
    description: JSON.stringify({
      session: sessionId,
      sandbox: boxBefore,
      agent: agentId,
      environment: parkEnvironment,
      window_seconds: idleWindowSeconds,
    }),
  });

  // ── The user talks to it once, from the real composer ────────────────────
  await page.setViewportSize({ width: 1440, height: 900 });
  await openSessionView(page, sessionId);
  await expectComposerEnabled(page);
  const memoryToken = `PARK-${randomUUID()}`;
  const firstPrompt = `Idle-park E2E ${runId}. Remember this exact token for later: ${memoryToken}. `
    + 'Acknowledge briefly without using tools.';
  const repliesBeforeFirst = await page.getByTestId('assistant-message').count();
  await sendPrompt(page, sessionId, firstPrompt);
  // Count, not wording: the model's phrasing is its own business, and this spec
  // runs under whichever engine the matrix selected.
  await expect
    .poll(() => page.getByTestId('assistant-message').count(), { timeout: REPLY_TIMEOUT_MS })
    .toBeGreaterThan(repliesBeforeFirst);
  const firstReply = page.getByTestId('assistant-message').last();
  await expect(
    firstReply,
    'the pre-park turn must produce a reply, or there is no conversation to come back to',
  ).not.toBeEmpty();
  // A failed turn renders its error INTO the transcript as an assistant message,
  // so "one more non-empty bubble" is satisfied by exactly the outcome a broken
  // box would produce.
  await expect(firstReply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
  await expect(
    page.getByTestId('run-view').getByTestId('status-pill').first(),
    'the pre-park turn must settle before the conversation can be called quiet',
  ).toHaveAttribute('data-pulse', 'false', { timeout: REPLY_TIMEOUT_MS });

  // ── …and leaves a file behind ────────────────────────────────────────────
  // Planted through the API: setup, not journey. Asking the model to write it
  // would make a durability assertion fail whenever the model declined a tool.
  // The workspace root comes from the product, never from a literal — the
  // short-cwd profile can move it.
  const rootListing = await platform.listFiles(sessionId);
  const root = String(rootListing.root_path || '').trim();
  expect(root, 'the files listing must name the workspace root').not.toEqual('');
  const markerName = `idle-park-note-${runId}.md`;
  const markerBody = `left here before the park at ${new Date().toISOString()}\n`;
  await api.uploadFileText(sessionId, root, markerName, markerBody);
  const markerPath = `${root}/${markerName}`;
  expect(
    await api.downloadFileText(sessionId, markerPath),
    'the marker must be in the workspace before the park, or its later absence proves nothing',
  ).toEqual(markerBody);

  // ── The user closes the tab ──────────────────────────────────────────────
  // From here until the return, nothing touches the session over an endpoint
  // that could attach a runtime: every reading is the database or the operator
  // sandbox read.
  await closePageAfterAssertions(page);

  if (disableEnvironment) {
    const disabled = await platform.putEnvironment(parkEnvironment, { ...parkPayload, enabled: false });
    expect(disabled.enabled).toBe(false);
    expect(parkedMark(sessionId), 'the disabled RuntimeProfile must precede the parking decision').toBe('');
    expect(quietForMs(sessionId).quietMs, 'the idle window must still be open after disabling').toBeLessThan(idleWindowMs);
    const refused = await request.post(apiPath(`/agents/${agentId}/conversations`));
    expect(refused.status()).toBe(409);
    expect((await refused.json()).code).toBe('AGENT_RUNTIME_PROFILE_DISABLED');
  }

  // ── The conversation goes quiet past the window it was given ─────────────
  const quietDeadline = Date.now() + QUIET_TIMEOUT_MS + idleWindowMs - IDLE_WINDOW_MS;
  for (;;) {
    const quiet = quietForMs(sessionId);
    if (
      quiet.status === 'idle'
      && quiet.openResponses === 0
      && (quiet.quietMs ?? -1) >= idleWindowMs
    ) break;
    if (Date.now() >= quietDeadline) {
      throw new Error(
        `the conversation must be idle past its ${idleWindowSeconds}s window before the keeper `
          + `can be asked about it, and it was not within ${QUIET_TIMEOUT_MS}ms: `
          + `${JSON.stringify(quiet)}. A conversation that never settles means the first response `
          + `is still running or its runtime never reported idle (session=${sessionId} `
          + `sandbox=${boxBefore}).`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }

  // ── The platform's keeper parks it ───────────────────────────────────────
  // `POST /admin/sandbox-idle-sweep` runs the identical `scan_once()` the 300s
  // timer runs, so this neither re-implements the sweep nor waits out a watcher
  // interval. More than one tick may be needed: the scan takes at most
  // `_IDLE_SWEEP_SCAN_LIMIT` rows per tick with no ordering, so a deployment
  // holding other live conversations can truncate the first tick before it
  // reaches this one.
  const ticks: Array<Record<string, number>> = [];
  const parkObservations: Array<{ at: string; marked: boolean; state: string }> = [];
  const parkDeadline = Date.now() + PARK_SETTLE_MS;
  let parkedAt = '';
  let boxAfterPark = '';
  for (;;) {
    if (!parkedMark(sessionId)) ticks.push(sweepCounters(await platform.idleSweep()));
    parkedAt = parkedMark(sessionId);
    boxAfterPark = String((await api.getSandbox(boxBefore)).state || '').toUpperCase();
    parkObservations.push({ at: new Date().toISOString(), marked: Boolean(parkedAt), state: boxAfterPark });
    if (parkedAt && PAUSED_STATES.has(boxAfterPark)) break;
    if (Date.now() >= parkDeadline) break;
    await new Promise((resolve) => setTimeout(resolve, 3_000));
  }
  await test.info().attach('idle-sweep-ticks', {
    body: JSON.stringify(
      {
        ticks,
        parkObservations,
        session: sessionId,
        sandbox: boxBefore,
        sandbox_state: boxAfterPark,
        sandbox_parked_at: parkedAt,
        quiet: quietForMs(sessionId),
        still_a_candidate: disqualifiedFromTheSweep(sessionId) || '<yes>',
      },
      null,
      2,
    ),
    contentType: 'application/json',
  });

  expect(
    ticks.some((tick) => Number(tick.idle_candidates || 0) >= 1) || parkedAt !== '',
    'the keeper never reached this conversation: no tick listed an idle candidate and no tick '
      + `parked it. session=${sessionId} sandbox=${boxBefore} `
      + `disqualified=${disqualifiedFromTheSweep(sessionId) || '<no, it still qualifies>'} `
      + `ticks=${JSON.stringify(ticks)}`,
  ).toBe(true);
  expect(
    parkedAt,
    'the keeper did not park this conversation, so nothing below can be said about coming back '
      + `to a parked one. session=${sessionId} sandbox=${boxBefore} state=${boxAfterPark} `
      + `quiet=${JSON.stringify(quietForMs(sessionId))} `
      + `disqualified=${disqualifiedFromTheSweep(sessionId) || '<no, it still qualifies>'} `
      + `ticks=${JSON.stringify(ticks)}. Either the RuntimeProfile did not store idle_action `
      + '"pause", or the retention renew did not take, or the snapshot commit did not settle on '
      + 'this cluster (docs/providers/opensandbox.md names what pausing needs).',
  ).not.toEqual('');
  expect(
    [...PAUSED_STATES],
    `the box must be committed, not merely marked: the control plane reads ${boxAfterPark} `
      + `(sandbox=${boxBefore}, session=${sessionId})`,
  ).toContain(boxAfterPark);
  expect(
    String((await api.getSession(sessionId)).sandbox_id || '').trim(),
    'parking is not a replacement: the conversation must still point at the box it filled',
  ).toEqual(boxBefore);

  if (disableEnvironment) {
    const disabled = (await platform.listEnvironments()).find((row) => row.name === parkEnvironment);
    expect(disabled?.enabled, 'the real sandbox must have parked while its RuntimeProfile was disabled').toBe(false);
    await platform.putEnvironment(parkEnvironment, { ...parkPayload, enabled: true });
  }

  // ── The user comes back, in a fresh tab ──────────────────────────────────
  // A new page, not a reload of the tab that was left open: returning to a
  // conversation is a cold load, and a live tab carries warm client state the
  // returning user does not have.
  const returning = await context.newPage();
  await returning.setViewportSize({ width: 1440, height: 900 });
  await openSessionView(returning, sessionId);

  // THE RETURN READS CALM. Park writes no `runtime_unavailable` and no state
  // change, so the session still projects READY — and if a fix ever makes the
  // Files panel honest by making the whole conversation look broken, this is
  // what refuses it.
  const pill = returning.getByTestId('run-view').getByTestId('status-pill').first();
  await expect(
    pill,
    'a conversation whose box was parked must still read Ready — the park is the platform\'s '
      + 'business, not the user\'s',
  ).toContainText('Ready', { timeout: 30_000 });
  await expect(pill).not.toContainText('Runtime disconnected');
  await expect(pill).not.toContainText('Recovery needed');
  await expect(returning.getByText('Runtime disconnected')).toHaveCount(0);
  await expect(returning.getByText('Recovery needed')).toHaveCount(0);
  await expect(
    returning.getByRole('button', { name: 'Recover session' }),
    'a parked conversation is not a broken one: nothing may ask the user to recover it',
  ).toHaveCount(0);
  await expectComposerEnabled(returning);
  await expect(
    returning.getByTestId('user-message').filter({ hasText: firstPrompt }).last(),
    'the fresh load must show the message the user sent before leaving',
  ).toBeVisible({ timeout: 30_000 });

  // ── They look at the workspace ───────────────────────────────────────────
  // Files is already the default tab (sessionCapabilities.ts:49); the click
  // states the journey without remounting the panel.
  await returning.getByRole('tab', { name: 'Files', exact: true }).click();
  const filesPanel = returning
    .getByRole('tabpanel')
    .filter({ has: returning.getByText('Current directory', { exact: true }) })
    .first();
  await expect(
    filesPanel,
    'the Files panel must be the one the returning user lands on',
  ).toBeVisible({ timeout: FILE_PANEL_MS });

  // The file listing wakes the parked box. One verdict function rather than a
  // bare visibility wait, because a failure can take three shapes — a spinner to
  // the control deadline, a fast refusal, or an unexpected answer — and a
  // timeout on a locator would report none of them. The error strip is the
  // panel's `ErrorNote`, which is an `Alert` and so carries role="alert".
  const filesVerdict = async (): Promise<string> => {
    if ((await filesPanel.getByText(markerName, { exact: true }).count()) > 0) {
      return 'the workspace is listed';
    }
    const strip = filesPanel.getByRole('alert');
    if ((await strip.count()) > 0) {
      return `the panel refused: ${(await strip.first().innerText()).replace(/\s+/g, ' ').trim()}`;
    }
    // Each branch is a string only this panel emits, and the console is pinned to
    // en-US above — so a verdict names the state the panel is actually in rather
    // than collapsing three different failures into "it did not appear".
    if ((await filesPanel.getByText('Directory unavailable', { exact: true }).count()) > 0) {
      return 'the panel gave up on the listing without an error: Directory unavailable';
    }
    if ((await filesPanel.getByText('Loading directory…', { exact: true }).count()) > 0) {
      return 'the panel is still loading';
    }
    return 'the panel shows neither the file, an error, nor a loading state';
  };
  await expect
    .poll(filesVerdict, {
      timeout: FILE_PANEL_MS,
      intervals: [1_000, 2_000, 3_000],
      message:
        'the Files panel does not serve the workspace of a conversation whose box the platform '
        + `parked. session=${sessionId} sandbox=${boxBefore} marker=${markerPath}. Parking leaves `
        + 'the conversation READY with its box named, so the panel is enabled over this row; the '
        + 'listing must wake the box and show the file, and a panel that cannot serve the box must '
        + 'at least say so — enabled and silent is the defect either way.',
    })
    .toEqual('the workspace is listed');

  // ── They send the next message ───────────────────────────────────────────
  const repliesBefore = await returning.getByTestId('assistant-message').count();
  await sendPrompt(
    returning,
    sessionId,
    'What exact token did I ask you to remember before the pause? Reply with that token only. Do not use tools.',
  );
  await expect
    .poll(() => returning.getByTestId('assistant-message').count(), { timeout: REPLY_TIMEOUT_MS })
    .toBeGreaterThan(repliesBefore);
  const secondReply = returning.getByTestId('assistant-message').last();
  await expect(
    secondReply,
    'the message sent after the park must be answered, not left as an empty bubble',
  ).not.toBeEmpty();
  // The same reason as the pre-park turn, and here it is the sharper edge: a wake
  // that failed renders its refusal as an assistant message, which a bare "one
  // more bubble" check would accept as a reply.
  await expect(secondReply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
  await expect(
    pill,
    'the post-park turn must settle like any other',
  ).toHaveAttribute('data-pulse', 'false', { timeout: REPLY_TIMEOUT_MS });
  // The token appears only in the first input, never in the Agent name,
  // workspace marker or resumed prompt. A fresh conversation cannot infer it.
  await expect(secondReply, 'snapshot resume must preserve the native conversation').toContainText(memoryToken);

  // ── It continued on the SAME box, not a fresh one ────────────────────────
  // The only assertion that can tell a resume from silent workspace loss: a
  // re-borrow after a resume that did not take produces an equally good-looking
  // reply on a box that has none of these files.
  const afterTurn = await api.getSession(sessionId);
  expect(
    String(afterTurn.sandbox_id || '').trim(),
    'the turn continued on a DIFFERENT box: the parked one did not resume, so this conversation '
      + `re-borrowed and its workspace is not in the new box. was=${boxBefore} `
      + `now=${String(afterTurn.sandbox_id || '')}`,
  ).toEqual(boxBefore);
  expect(
    await api.downloadFileText(sessionId, markerPath),
    'the file the user left before the park must come back byte for byte — a rebuilt box would '
      + 'have neither these bytes nor this file',
  ).toEqual(markerBody);
  await expect(
    filesPanel.getByText(markerName, { exact: true }),
    'the Files panel must still be serving the same workspace once the box is awake',
  ).toBeVisible({ timeout: FILE_PANEL_MS });

  // ── And the conversation is parkable again ───────────────────────────────
  // Both ways out of a parked row clear the mark — a resume that took and a
  // re-borrow that replaced the box — so this says nothing about which one
  // happened; the same-box
  // assertion above is what separates them. What it does say is that the row
  // did not keep a mark neither path cleared, which would exclude this
  // conversation from the sweep for good
  // (`list_idle_reclaim_candidates` excludes marked rows).
  await expect
    .poll(() => parkedMark(sessionId), {
      timeout: 30_000,
      intervals: [1_000, 2_000],
      message:
        'the conversation still calls itself parked after a turn woke it, so it is excluded from '
        + `the idle sweep forever and its box will never be parked again (session=${sessionId})`,
    })
    .toEqual('');
}

test('a conversation parked for idleness opens on its own workspace and continues on the same box', async ({ page, context, request }) => {
  await exerciseParkedConversation({ page, context, request }, false);
});

test('a disabled RuntimeProfile still parks existing idle compute and re-enabling preserves its workspace', async ({ page, context, request }) => {
  await exerciseParkedConversation({ page, context, request }, true);
});
