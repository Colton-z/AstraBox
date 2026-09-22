/**
 * E2E: a page the agent exposed is still there when the user comes back to it.
 *
 * The journey: an agent starts a web server in its box, the platform mints a
 * browser-reachable URL for that port, the user opens it in its own tab and
 * reads it for twenty minutes without saying anything to the console. While
 * they read, the platform's own background keeper runs over this conversation
 * and decides what to do with the box that is serving the page. Coming back to
 * the tab must still reach what the agent exposed.
 *
 * The only activity clock the keeper reads is turn activity: `_is_idle_past`
 * (`expiration_watcher.py:549-575`) requires the conversation snapshot to read
 * IDLE and then measures the age of that snapshot's `updated_at`, falling back
 * to the session row's when the snapshot carries none. Nothing in either clock
 * moves when a user loads an exposed page, so twenty minutes of a user actually
 * using it counts as twenty minutes of silence. Nothing records that a port was
 * exposed at all — `expose_port` resolves a URL and stores nothing
 * (`expose_port_service.py:43-77`) — and the one route that could hand the user
 * a fresh link, `GET /api/v1/exposed-ports/{id}/{port}/url`
 * (`platform_mcp.py:88-116`), has no product caller: `frontend/src` knows it
 * only from the generated `api/schema.d.ts`, and `exposed_port_url_path()`
 * (`runtime/mcp_servers.py:308`), written to hand that path to the agent, has
 * no callers anywhere including tests.
 *
 * WHAT IS DELIBERATELY NOT PINNED. The verdict is the tab, not the sweep's
 * outcome. `idle_parked === 1` would be the wrong assertion: refusing to park a
 * box whose exposed port is live is one of the honest fixes — the direct
 * analogue of renewing a prepared slot ahead of its TTL rather than at the next
 * arrival — and an assertion that pinned one of the two outcomes could not
 * survive its own fix. Handing the user a live link (and the wake behind it)
 * satisfies the same verdict. So the sweep is only asked to prove it CONSIDERED
 * this conversation; what it decided is the product's business.
 *
 * DRIVEN THROUGH THE PRODUCT'S OWN ROUTE. `POST /admin/sandbox-idle-sweep` runs
 * `_expiration_watcher.scan_once()` — the identical tick the timer runs
 * (`sandboxes.py:468-492`) — so the spec neither re-implements the sweep nor
 * waits out an interval. A deployment that shortens
 * `ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS` from its 300s default
 * (`settings.py:491-497`) also ticks on its own while this runs, which is why
 * the run-recognition check accepts either answer: once a background tick has
 * parked this session, its row leaves the candidate query
 * (`session_repository.py:544-548`) and a later sweep truthfully reports no
 * candidate.
 *
 * THE PREMISE IS OPERATOR CONFIGURATION, NOT A DEPLOYMENT DEFAULT. `idle_action`
 * decides whether there is a keeper at all: anything but `pause` makes
 * `_idle_window_if_parking` return None (`expiration_watcher.py:530-539`) and
 * the sweep leaves the box alone, so the journey has no failing configuration
 * to run. The spec therefore writes its own Environment with
 * `idle_action: 'pause'` through the real admin route, which is a shipped,
 * documented configuration, and FAILS LOUDLY if the backend refuses it
 * (`environment_schema.py:365-392` refuses pause on a backend that cannot
 * snapshot) rather than degrading into a terminate-mode run that proves
 * nothing. A red here describes what happens to a deployment that turns
 * parking on.
 *
 * WHAT A GREEN DOES NOT PROVE. A keeper that saw this conversation, tried to
 * park it and failed leaves the page working too. The counters are attached and
 * named in the failure text so that outcome is legible; the spec does not
 * assert on `idle_failed`, which is a deployment-wide counter no session can
 * claim. What a green does prove is that the keeper reached this conversation
 * and the exposed page survived it.
 *
 * WHAT THIS SPEC CANNOT REACH. The signed-URL half of the same wound — the
 * 15-minute `sandbox_endpoint_url_ttl_seconds` deadline — only exists when
 * `ASTRABOX_SANDBOX_SECURE_ACCESS=true` (`settings.py:208`, default false),
 * which no deploy script, compose file or suite configures, and which the
 * runner's own `--endpoint-kind` validation has no value for. With it off,
 * `_resolve_browser_endpoint` passes `expires_at=None`
 * (`expose_port_service.py:119-123`) and the minted URL never expires, so no
 * browser spec can age a signature out. This spec proves the
 * deployment-independent half: the link has no keeper, and the remedy has no
 * caller.
 *
 * EXCLUSIVE, AND NOT DEFENSIVELY. `scan_once()` sweeps the whole deployment —
 * startup allocations, abandoned Agent boxes, ownerless boxes, prepared-slot
 * renewal, dead bindings, then idle bindings — over every session and every
 * box. Run beside parallel workers it would reap and park theirs. It belongs in
 * the suite contract's one-worker serial group for the same reason the sibling
 * park/wake specs do; adding the file also moves `playwright.exclusive.files`.
 *
 * WHAT A FAILURE LEAVES BEHIND: `trackSessions` keeps a failed session, so the
 * box stays — PAUSED if the keeper got to it, and a parked box is retained for
 * `sandbox_parked_retention_seconds` (7 days by default). The spec's own
 * Environment stays enabled on a failure and is disabled on a pass; its name is
 * stable, so a rerun rewrites that one row rather than adding another, which is
 * what `admin.py` offering environments only GET and PUT requires.
 */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { patchSnapshotDoc, sessionDoc, snapshotDoc } from '../fixtures/dbOracle';
import { parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

interface ExposedPortUrl {
  url: string;
  port: number;
  [key: string]: unknown;
}

// The masked stand-in the server returns for a stored model key
// (`agent_config_service.py:60`). Cloning it into a NEW environment name would
// resolve to "no stored value" and silently drop the credential, so the spec
// refuses the clone instead of running an Agent that cannot reach a model.
const ENV_API_KEY_SENTINEL = '••••••••';

// The journey's literal twenty minutes, set through the Agent's own field
// (`agent_schema.py:183`) rather than the deployment default, so the wait this
// spec simulates is the one this Agent asked for.
const IDLE_WINDOW_SECONDS = 1_200;

const FETCH_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_EXPOSE_PORT_FETCH_TIMEOUT_MS', 15_000);
const TURN_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 60_000);
const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 90_000);
// How long the keeper's decision may take to reach the BOX. The parked mark is
// written before the pause begins and the commit itself takes tens of seconds
// (`expiration_watcher.py:358-425`), during which the Pod still serves — so a
// tab asked too early answers for a box the platform is in the middle of taking
// away. Spending this window is what makes a pass mean "the keeper left it
// alone" instead of "we looked first".
const KEEPER_DECISION_MS = parseTimeoutEnv('ASTRABOX_E2E_IDLE_KEEPER_DECISION_MS', 45_000);

function resolveExposePort(): number {
  const raw = process.env.ASTRABOX_E2E_EXPOSE_PORT;
  if (!raw) return 5173;
  const value = Number.parseInt(raw, 10);
  if (!Number.isInteger(value) || value <= 0 || value > 65535) {
    throw new Error('ASTRABOX_E2E_EXPOSE_PORT must be a TCP port (1-65535)');
  }
  return value;
}

const PORT = resolveExposePort();

/**
 * The page the agent leaves running in its box.
 *
 * Deliberately not the sibling's program
 * (`expose-port-in-box-web-server-reachable.parallel.spec.ts`): that one proves
 * the endpoint's HTTP + stylesheet + WebSocket data plane, and this one asks
 * only whether the same address still answers after the platform has had its
 * turn. The idiom is the shared one — a base64'd program started through the
 * terminal API — and the marker is what both read.
 */
function inBoxServer(marker: string): string {
  return String.raw`
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
BODY = ('<!doctype html><html><body>' + ${JSON.stringify(marker)} + '</body></html>').encode()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(BODY)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass

HTTPServer.allow_reuse_address = True
HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
`;
}

async function gotoWithRetry(page: Page, url: string, budgetMs = FETCH_TIMEOUT_MS): Promise<void> {
  const deadline = Date.now() + budgetMs;
  let lastErr = 'no attempt completed';
  for (;;) {
    try {
      const response = await page.goto(url, { timeout: FETCH_TIMEOUT_MS });
      if (response?.ok()) return;
      lastErr = response ? `HTTP ${response.status()}` : 'no navigation response';
    } catch (error) {
      lastErr = error instanceof Error ? error.message : String(error);
    }
    if (Date.now() >= deadline) {
      throw new Error(`GET ${url} did not become reachable within ${budgetMs}ms: ${lastErr}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
}

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

function parkedMark(sessionId: string): string {
  return String(sessionDoc(sessionId)?.sandbox_parked_at || '').trim();
}

const sessions = trackSessions();
let agentId = '';
let parkingEnvironment = '';
let parkingEnvironmentPayload: Record<string, unknown> = {};

// Registration order is teardown order: the session goes first (trackSessions),
// then its Agent, then the Environment nothing references any more.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});
onPassOnly(async ({ request }) => {
  if (!parkingEnvironment) return;
  await new PlatformApi(request).putEnvironment(parkingEnvironment, {
    ...parkingEnvironmentPayload,
    enabled: false,
  });
});

test('an exposed port outlives the idle window the user spent browsing it', async ({
  page,
  context,
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  const marker = `EXPOSED_PORT_IDLE_E2E_${runId}`;

  // ── A deployment that parks idle boxes ───────────────────────────────────
  // Cloned from the deployment's own conversation-tenancy cold Environment so
  // the box belongs to this conversation alone — which is what the keeper
  // requires before it will park anything (`_box_is_this_session_s_alone`).
  const sourceName = String(process.env.ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT || '').trim();
  expect(
    sourceName,
    'this journey needs the deployment-provisioned conversation-tenancy Environment; '
      + 'set ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT',
  ).not.toEqual('');
  const environments = await platform.listEnvironments();
  const source = environments.find((item) => String(item.name || '') === sourceName);
  expect(
    source,
    `Environment ${JSON.stringify(sourceName)} is not on this deployment; have: `
      + environments.map((item) => String(item.name || '')).join(', '),
  ).toBeTruthy();

  // Project through the product's own schema rather than a list kept here: a
  // second copy of the editable surface would silently stop carrying whatever
  // field is added to the first.
  const schema = await platform.environmentSchema();
  const editableKeys = (schema.fields || [])
    .map((field) => String(field.key || ''))
    .filter((key) => key && key !== 'name');
  expect(editableKeys.length, 'the environment schema must declare its editable fields').toBeGreaterThan(0);
  const access = (source as Record<string, unknown>).provider_access;
  if (access && typeof access === 'object') {
    expect(
      String((access as Record<string, unknown>).api_key || ''),
      'the source Environment stores a model key that a clone cannot carry — the server '
        + 'returns it masked and a clone under a new name would store nothing. Point this '
        + 'spec at an Environment using deployment model access.',
    ).not.toEqual(ENV_API_KEY_SENTINEL);
  }

  // Environments are named as lowercase dashed resources and the name reaches a
  // URL path, so the `__e2e_` spelling that marks throwaway Agents is not used
  // here. Stable rather than run-scoped, and derived from the source so two
  // source Environments cannot collide: `admin.py` offers environments GET and
  // PUT and no DELETE, so a run-scoped name would leave one undeletable row per
  // run on the deployment. A rerun upserts this one instead.
  parkingEnvironment = `astrabox-e2e-tmp-idle-port-${sourceName}`;
  parkingEnvironmentPayload = {
    ...Object.fromEntries(
      editableKeys
        .filter((key) => key in (source as Record<string, unknown>))
        .map((key) => [key, (source as Record<string, unknown>)[key]]),
    ),
    display_name: 'Exposed port over an idle window (E2E)',
    description: `Created by ${test.info().titlePath.join(' › ')}.`,
    enabled: true,
    idle_action: 'pause',
  };
  let stored: Record<string, unknown>;
  try {
    stored = await platform.putEnvironment(parkingEnvironment, parkingEnvironmentPayload);
  } catch (error) {
    parkingEnvironment = '';
    throw new Error(
      'this deployment refused an Environment that parks idle boxes, so it has no '
        + 'configuration in which this journey can fail or pass. Under idle_action '
        + '"terminate" the keeper leaves every box alone and the box instead dies at its '
        + `lease, which no 180s spec can reach.\n${error instanceof Error ? error.message : String(error)}`,
    );
  }
  expect(
    String(stored.idle_action || ''),
    'the stored Environment must be the one the keeper will read',
  ).toEqual('pause');

  // ── An Agent that may sit quiet for exactly twenty minutes ───────────────
  const base = await api.defaultAgent();
  const model = String(base.model || '').trim();
  expect(model, 'the deployment-proven Agent must name a model route').not.toEqual('');
  const models = await api.listEnvironmentModels(parkingEnvironment);
  expect(
    models,
    `the parking Environment must expose the deployment-proven model ${JSON.stringify(model)}`,
  ).toContain(model);

  const agent = await api.createAgent({
    name: `__e2e_idle_port_${runId}`,
    model,
    environment_name: parkingEnvironment,
    prewarm_enabled: false,
    idle_hibernate_seconds: IDLE_WINDOW_SECONDS,
  });
  agentId = String(agent.agent_id || '');
  expect(agentId, 'the Agent must be created').not.toEqual('');
  // `createAgent` projects its payload through the authoring schema and drops
  // anything the schema does not declare. A dropped window is invisible: the
  // sweep would fall back to the deployment default and judge this Agent by a
  // clock the spec never set.
  expect(
    Number((await api.getAgent(agentId)).idle_hibernate_seconds),
    'the Agent must carry the idle window this journey simulates',
  ).toBe(IDLE_WINDOW_SECONDS);

  const created = await api.startConversation(agentId);
  const sessionId = String(created.session_id || '').trim();
  expect(sessionId, 'starting a conversation must open a session').not.toEqual('');
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId, READY_TIMEOUT_MS);
  const sandboxId = String(ready.sandbox_id || '').trim();
  expect(sandboxId, 'a READY conversation must name its box').not.toEqual('');
  test.info().annotations.push({
    type: 'idle_exposed_port_scene',
    description: JSON.stringify({ sessionId, sandboxId, agentId, environment: parkingEnvironment, port: PORT }),
  });

  // ── The user is in the console, working ──────────────────────────────────
  await openSessionView(page, sessionId);
  const turn = await api.sendTurn(
    sessionId,
    'E2E idle-window baseline: do not use tools; reply briefly.',
    TURN_BUDGET_MS,
  );
  expect(turn.errorText, 'the baseline turn must not fail').toBeNull();
  expect(turn.text.trim(), 'the baseline must produce an ordinary reply').not.toEqual('');
  expect(turn.text.trim()).not.toMatch(/^API Error:\s*\d+\b/);
  // The snapshot is the keeper's only evidence of quiet, and IDLE is the state
  // it requires (`_IDLE_CONVERSATION_STATES`). Reading it here also proves the
  // row exists before the backdate touches it.
  await expect
    .poll(() => String(snapshotDoc(sessionId)?.conversation_state || '<no snapshot>'), {
      timeout: 45_000,
      message: `the turn must settle before the user walks away (session ${sessionId})`,
    })
    .toBe('IDLE');

  // ── The agent puts something on a port, and the platform mints its link ──
  const source64 = Buffer.from(inBoxServer(marker), 'utf8').toString('base64');
  await api.runTerminalCommand(
    sessionId,
    `printf '%s' '${source64}' | base64 -d > /tmp/astrabox_idle_port_e2e.py && `
      + `nohup python3 /tmp/astrabox_idle_port_e2e.py ${PORT} `
      + '>/tmp/astrabox_idle_port_e2e.log 2>&1 & sleep 2',
  );
  const minted = await api.data<ExposedPortUrl>('GET', `/exposed-ports/${sessionId}/${PORT}/url`);
  expect(minted.port, 'the mint must answer for the port that was asked for').toBe(PORT);
  expect(minted.url).toMatch(/^https?:\/\//);
  const exposedUrl = `${minted.url.replace(/\/+$/, '')}/`;

  // ── The user opens it in its own tab, and reads ─────────────────────────
  const portTab = await context.newPage();
  await gotoWithRetry(portTab, exposedUrl);
  await expect(
    portTab.locator('body'),
    'the minted link must reach the in-box server before the wait — otherwise a later '
      + 'failure cannot be told apart from a server that never started',
  ).toContainText(marker, { useInnerText: true });

  const servingState = String((await api.getSandbox(sandboxId)).state || '').toLowerCase();
  expect(servingState, 'the box serving the page must be running').toEqual('running');

  // ── Twenty minutes pass, and the user says nothing to the console ────────
  // Backdating the snapshot's `updated_at` is exactly the clock the keeper
  // reads, and the only one; the tab stays open and the console tab is not
  // touched again. Nothing else writes this row while the turn is settled, so
  // the read-modify-write in `patchSnapshotDoc` cannot undo a concurrent write.
  const idleSince = new Date(Date.now() - (IDLE_WINDOW_SECONDS + 120) * 1_000).toISOString();
  patchSnapshotDoc(sessionId, { updated_at: idleSince });
  expect(
    String(snapshotDoc(sessionId)?.updated_at || ''),
    'the simulated wait must be the snapshot clock the keeper reads',
  ).toEqual(idleSince);

  // ── The platform's keeper runs over this conversation ───────────────────
  const sweep = sweepCounters(await platform.idleSweep());
  const parkedAfterSweep = parkedMark(sessionId);
  await test.info().attach('idle-sweep-summary', {
    body: JSON.stringify({ sweep, sandbox_parked_at: parkedAfterSweep, sandbox_id: sandboxId }),
    contentType: 'application/json',
  });
  expect(
    Number(sweep.idle_candidates || 0) >= 1 || parkedAfterSweep !== '',
    'the keeper must have reached this conversation: the tick either listed it as an idle '
      + 'candidate, or a tick that already ran parked it (which removes the row from the '
      + `candidate query). Neither happened — sweep=${JSON.stringify(sweep)}. Check that the `
      + `Environment stored idle_action "pause" and that session ${sessionId} still holds a `
      + 'live, unparked lease.',
  ).toBe(true);

  // Give the decision time to reach the box before asking the tab. Leaving the
  // state it reported while serving is the backend's own word for "the platform
  // moved this box"; no state vocabulary is invented here.
  const decisionDeadline = Date.now() + KEEPER_DECISION_MS;
  let boxState = servingState;
  while (Date.now() < decisionDeadline) {
    boxState = String((await api.getSandbox(sandboxId)).state || '').toLowerCase();
    if (boxState !== servingState) break;
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
  const parkedAt = parkedMark(sessionId);
  const evidence = [
    'the exposed page did not survive the idle window the user spent reading it.',
    `url=${exposedUrl}`,
    `minted=${JSON.stringify(minted)}`,
    `sweep=${JSON.stringify(sweep)}`,
    `sandbox=${sandboxId} state_while_serving=${servingState} state_now=${boxState}`,
    `session=${sessionId} sandbox_parked_at=${parkedAt || '<unset>'}`,
    'A parked box means the platform took the compute away from a page a user was on: '
      + 'nothing records that a port is exposed, so the keeper cannot know. A box still '
      + 'running here means the in-box server died on its own — a different fault.',
    `The one route that could have handed the user a live link again is `
      + `GET /api/v1/exposed-ports/${sessionId}/${PORT}/url, and no console surface calls it.`,
  ].join('\n');

  // ── The user comes back to the tab ───────────────────────────────────────
  // The reload is checked before the body: a navigation that fails leaves the
  // previous render in place, and asserting the marker first would read the
  // page the user had BEFORE they came back.
  let reloadError = '';
  try {
    await portTab.reload({ timeout: FETCH_TIMEOUT_MS });
  } catch (error) {
    reloadError = error instanceof Error ? error.message : String(error);
  }
  expect(reloadError, evidence).toEqual('');
  await expect(portTab.locator('body'), evidence).toContainText(marker, {
    useInnerText: true,
    timeout: FETCH_TIMEOUT_MS,
  });
});
