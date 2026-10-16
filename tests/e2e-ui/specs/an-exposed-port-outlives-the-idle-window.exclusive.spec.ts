/** An exposed application must remain reachable when the idle keeper examines its quiet conversation. */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, postgresTextRows, sqlLiteral } from '../fixtures/dbOracle';
import { parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { POSTGRES_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';
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
// (`_park_sandbox` in expiration_watcher.py), during which the Pod still serves — so a
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

/**
 * What the keeper reads off this conversation's `sessions` row: the runtime's
 * last reported status, whether a response is still open, the moment quiet is
 * measured from (the latest of the last output, the last change of activity and
 * the last resume of the box — `_is_idle_past` in expiration_watcher.py), and
 * the park decision's own mark.
 */
interface KeeperRow {
  runtime_status: string | null;
  open_responses: number;
  quiet_since: string | null;
  quiet_since_epoch: number | null;
  sandbox_parked_at: string | null;
}

function keeperRow(sessionId: string): KeeperRow | null {
  return postgresRows<KeeperRow>(
    `SELECT s.runtime_status, `
      + `(SELECT count(*) FROM session_responses r `
      + `WHERE r.session_id = s.session_id AND r.status <> 'ended')::int AS open_responses, `
      + `GREATEST(s.last_output_at, s.updated_at, s.sandbox_resumed_at) AS quiet_since, `
      + `extract(epoch FROM GREATEST(s.last_output_at, s.updated_at, s.sandbox_resumed_at))::float8 `
      + `AS quiet_since_epoch, s.sandbox_parked_at `
      + `FROM sessions s WHERE s.session_id = ${sqlLiteral(sessionId)}`,
  )[0] ?? null;
}

function parkedMark(sessionId: string): string {
  return String(keeperRow(sessionId)?.sandbox_parked_at || '').trim();
}

/** Run one write against the deployment database; the key column of every row it changed. */
function writeRows(sql: string): string[] {
  return postgresTextRows(
    requireServiceContainer(POSTGRES_CONTAINER_HANDLE),
    process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox',
    sql,
  );
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
    prepared_capacity: 0,
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
  // The keeper parks only a conversation whose runtime last reported itself
  // idle (`_IDLE_RUNTIME_STATUSES`) with no response still open. Reading the row
  // here also proves it exists before the backdate touches it.
  await expect
    .poll(() => {
      const row = keeperRow(sessionId);
      return row ? `${row.runtime_status ?? '<no status>'}, open responses ${row.open_responses}` : '<no row>';
    }, {
      timeout: 45_000,
      message: `the response must settle before the user walks away (session ${sessionId})`,
    })
    .toBe('idle, open responses 0');

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
  // The keeper measures quiet from the latest of three moments on the session
  // row — its last output, its last change of activity (`updated_at`) and the
  // last resume of its box — so every one that is set moves back together, in
  // one statement; one left behind would still read as recent activity. The tab
  // stays open and the console tab is not touched again.
  const idleSince = new Date(Date.now() - (IDLE_WINDOW_SECONDS + 120) * 1_000).toISOString();
  const moment = `TIMESTAMPTZ ${sqlLiteral(idleSince)}`;
  expect(
    writeRows(
      `UPDATE sessions SET updated_at = ${moment}, `
        + `last_output_at = CASE WHEN last_output_at IS NULL THEN NULL ELSE ${moment} END, `
        + `sandbox_resumed_at = CASE WHEN sandbox_resumed_at IS NULL THEN NULL ELSE ${moment} END `
        + `WHERE session_id = ${sqlLiteral(sessionId)} AND sandbox_parked_at IS NULL RETURNING session_id`,
    ),
    'the simulated wait must land on this one unparked session row',
  ).toEqual([sessionId]);
  expect(
    keeperRow(sessionId)?.quiet_since_epoch,
    'the simulated wait must be the clock the keeper reads',
  ).toBeCloseTo(Date.parse(idleSince) / 1_000, 3);

  // ── The platform's keeper runs over this conversation ───────────────────
  const sweep = sweepCounters(await platform.idleSweep());
  const keeperView = keeperRow(sessionId);
  const parkedAfterSweep = String(keeperView?.sandbox_parked_at || '').trim();
  await test.info().attach('idle-sweep-summary', {
    body: JSON.stringify({ sweep, session_row: keeperView, sandbox_id: sandboxId }),
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
