/**
 * E2E: an mcp_oauth credential whose `expires_at` passed while nobody was
 * looking is reported on the record an operator manages, before the first
 * person to open a conversation discovers it.
 *
 * ── THE JOURNEY ─────────────────────────────────────────────────────────────
 * On Friday an operator adds an `mcp_oauth` credential through the console —
 * server URL, access token, expiry, and no refresh block, because that is the
 * only shape the console's own form can author
 * (CredentialVaultPage.tsx:189-197).
 * The vault is bound to an Agent, the Agent declares that MCP server, and its
 * conversations work. Over the weekend `expires_at` passes. Nothing in AstraBox
 * touches the credential while nobody is using it: it has no refresh
 * configuration, so there is nothing to refresh, and no sweep reads it. On
 * Monday the operator opens the vault record and it looks exactly as it did on
 * Friday; then the first person to open a conversation pays for the silence,
 * because `provision_engine_sandbox` resolves the MCP credential plan while
 * provisioning (engine/provisioning.py:2078 → runtime/mcp_credentials.py →
 * vault_service._headers_for:657 → VAULT_CREDENTIAL_EXPIRED at
 * vault_service.py:713). The whole Agent is out — not one broken tool call —
 * and the record an operator manages never said a word.
 *
 * ── THE ABSENT SURFACE ──────────────────────────────────────────────────────
 * The two record-page assertions fail because the SURFACE IS ABSENT, not
 * because behaviour regressed. `auth.expires_at` is already in the page's own
 * props (frontend/src/types.ts:140) and already returned by the API
 * (api/routes/vaults.py, CredentialAuthSummary.expires_at); the row renders
 * label, type and target only (CredentialVaultPage.tsx:414-419), and the
 * credentials locale namespace has no "expired" word at all. This spec claims
 * that is a product defect of the kind this repo already covers with a lane
 * spec — a record page must report its subject, the property
 * `a-sandbox-record-reports-the-box-not-an-empty-card.parallel.spec.ts` pins —
 * and not a regression guard.
 *
 * ── WHAT THIS SPEC DOES NOT CLAIM ───────────────────────────────────────────
 * That a background refresher would help. Where refresh IS configured and the
 * token endpoint is live, the product already heals on use
 * (`_refresh_oauth`); a dead refresh token is dead whoever touches it. The case
 * taken here is the one that is genuinely unhealable and is also the only one
 * the console can author: no refresh block at all.
 *
 * ── ORDER OF ACTIONS vs ORDER OF VERDICTS ───────────────────────────────────
 * The ACTIONS run in journey order (Friday record → Friday conversation →
 * the weekend → Monday record → Monday conversation). The record rows are READ
 * where the operator would read them and judged at the end, after the
 * behavioural evidence has been collected. With `maxFailures: 1` and a red
 * A2a, asserting at the point of reading would abort the run before the
 * control arm and before anything is learned about what the failure costs.
 * Reading is not asserting; `expect.soft` appears nowhere in this suite and is
 * not introduced here. Arrangement checks (the preflight, the PATCH echo, the
 * rows being present at all) still fail fast: a run built on a false
 * precondition proves nothing at all.
 *
 * ── WHY EXCLUSIVE ───────────────────────────────────────────────────────────
 * It provisions two cold sandboxes, one of which is MADE to fail provisioning,
 * and it reads deployment-wide sandbox inventory before and after that
 * failure. A parallel worker sharing the node would see the deliberate failure
 * and the capacity churn as its own POOL_UNAVAILABLE, and the inventory delta
 * would mean nothing. Same reasoning as credential-request-matching.exclusive.
 *
 * No model turn runs: both arms are decided during provisioning, which is where
 * the credential is read. The dominant cost is the control arm's cold provision;
 * the Monday arm fails before an engine starts. Every per-step wait comes from
 * `parseTimeoutEnv`, so the lane tunes them and this file states no budget.
 *
 * ── ENGINE ──────────────────────────────────────────────────────────────────
 * The mechanism is engine-agnostic: the credential plan is resolved before any
 * engine starts, from `template.mcp_servers` plus the bound vault, with no
 * engine vocabulary involved. The FIXTURE is not. The spec needs a
 * conversation-tenancy Environment with `networking.allow_mcp_servers=true`,
 * and the only one the deployment provides is
 * ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT (claude_code / open_sandbox /
 * conversation). The preflight therefore fails loudly — never skips — when the
 * matrix engine is not the one that Environment runs.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { appPath, parseTimeoutEnv, refuseIfNotTheDeployment } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled, openSessionView } from '../fixtures/sessionPage';

// Two things this spec reads are rendered in the viewer's own settings, and
// both decide what the assertions can say. LOCALE: the record page and the
// header are localized (the console detects ['localStorage','navigator']), so
// the language is pinned the way the sibling lifecycle specs pin it — a fixed
// navigator locale plus the persisted 'astrabox-lang' the app's own switch
// writes. TIMEZONE: `formatDateTime` (manage/console/format.ts:21) renders in
// the BROWSER's local zone, so an expiry sent as UTC comes back as whatever
// zone the runner happens to sit in. Pinning UTC is what lets the expected
// date be computed from the ISO string that was written.
test.use({ locale: 'en-US', timezoneId: 'UTC' });

const COLD_ENVIRONMENT = String(
  process.env.ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT || '',
).trim();
const MATRIX_AGENT = String(process.env.ASTRABOX_E2E_AGENT_NAME || 'Claude Code').trim();

const COLD_ENVIRONMENT_SETUP = [
  'set ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT to a dedicated cold Environment name.',
  'Create it in Console > Manage > Environments with enabled=true, engine_kind=claude_code,',
  'sandbox_backend=open_sandbox, sandbox_tenancy=conversation, and',
  'networking.allow_mcp_servers=true. Keep and reuse this deployment fixture: AstraBox',
  'exposes no Environment DELETE API, so the spec cannot create and remove a disposable one.',
].join(' ');

const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 300_000);
// "It reaches a verdict" is the claim; the number is the lane's to tune.
const VERDICT_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_CREDENTIAL_EXPIRY_VERDICT_MS',
  120_000,
);
// A box handed back at the end of a failed provision is handed back
// asynchronously. This is how long the inventory has to come back down.
const INVENTORY_SETTLE_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_CREDENTIAL_EXPIRY_INVENTORY_MS',
  90_000,
);

// Lowercase because `normalize_mcp_server_url` (vault_service.py:119) lowercases
// the netloc: building the host lowercase keeps the stored URL and the declared
// URL the same string, so a mismatch here would be a real mismatch.
const RUN_ID = `${Date.now().toString(36)}${Math.random().toString(16).slice(2, 8)}`;
const WEEKEND_URL = `https://mcp-weekend-${RUN_ID}.invalid/mcp`;
const NEIGHBOUR_URL = `https://mcp-neighbour-${RUN_ID}.invalid/mcp`;
// Distinctive enough that finding either anywhere in a response is a leak and
// not a coincidence — the same discipline as the vault write-only spec.
const WEEKEND_TOKEN = `e2e-weekend-oauth-${RUN_ID}-DO-NOT-ECHO`;
const NEIGHBOUR_TOKEN = `e2e-neighbour-oauth-${RUN_ID}-DO-NOT-ECHO`;
// Display names are what the row prints. Neither may contain the word this spec
// searches the row for, or the differential assertion below would read its own
// fixture back as the product's answer.
const WEEKEND_LABEL = `E2E weekend token ${RUN_ID}`;
const NEIGHBOUR_LABEL = `E2E neighbour token ${RUN_ID}`;
const SERVER_ALIAS = `e2e_weekend_${RUN_ID}`;

const DAY_MS = 24 * 60 * 60 * 1000;
/** The word a record has to carry once the expiry has passed. */
const EXPIRED = /expired/i;

/** The exact ISO string written, and the local (= UTC, pinned above) day of it. */
function isoAt(offsetMs: number): string {
  return new Date(Date.now() + offsetMs).toISOString();
}
function dayOf(iso: string): string {
  return iso.slice(0, 10);
}

interface Row {
  label: string;
  text: string;
}

// Teardown that empties the scene runs on the passing path only, in
// registration order: the binding has to go before the vault, because
// `ensure_vault_unbound` refuses to delete a vault a runtime still names.
let agentId = '';
let vaultId = '';
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  if (agentId) {
    await platform
      .data('PUT', `/admin/agents/${agentId}/credential-vaults`, { vault_ids: [] })
      .catch(() => {});
    await api.deleteAgent(agentId).catch(() => {});
  }
  if (vaultId) await platform.deleteVault(vaultId).catch(() => {});
});

const sessions = trackSessions();

test('an expired MCP OAuth credential is reported on its record before a conversation dies on it', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  agentId = '';
  vaultId = '';

  // ── 0. Preflight. Fail loud, never skip. ──────────────────────────────────
  await refuseIfNotTheDeployment(async (url, headers) => ({
    status: (await request.get(url, { failOnStatusCode: false, headers })).status(),
  }));
  expect(COLD_ENVIRONMENT, COLD_ENVIRONMENT_SETUP).not.toEqual('');

  const environments = await platform.listEnvironments();
  const coldEnvironment = environments.find(
    (row) => String(row.name || '') === COLD_ENVIRONMENT,
  );
  expect(
    coldEnvironment,
    `no Environment is called ${JSON.stringify(COLD_ENVIRONMENT)}. ${COLD_ENVIRONMENT_SETUP}`,
  ).toBeTruthy();
  const coldNetworking =
    (coldEnvironment?.networking as Record<string, unknown> | undefined) ?? {};
  expect(coldEnvironment?.enabled, `${COLD_ENVIRONMENT} must be enabled`).not.toBe(false);
  // Load-bearing, not decoration: under agent tenancy the Monday conversation
  // would join Friday's already-provisioned box and never re-resolve the
  // credential, and this spec would go green for the wrong reason.
  expect(
    String(coldEnvironment?.sandbox_tenancy || ''),
    `${COLD_ENVIRONMENT} must use conversation tenancy, or the Monday conversation `
      + 'joins the Friday box and the credential is never resolved again',
  ).toEqual('conversation');
  expect(
    coldNetworking.allow_mcp_servers,
    `${COLD_ENVIRONMENT} must set networking.allow_mcp_servers=true, or provisioning `
      + 'refuses the declared MCP server with AGENT_MCP_NETWORK_ACCESS_DISABLED before '
      + 'the credential is ever read (runtime/config_resolver.py:176)',
  ).toBe(true);

  // The engine the matrix selected is a property of the Agent's Environment,
  // not of the Agent — so it is read where it lives.
  const matrixAgent = await api.defaultAgent(MATRIX_AGENT);
  const matrixEnvironmentName = String(matrixAgent.environment_name || '').trim();
  const matrixEnvironment = environments.find(
    (row) => String(row.name || '') === matrixEnvironmentName,
  );
  expect(
    matrixEnvironment,
    `the matrix Agent ${JSON.stringify(MATRIX_AGENT)} names Environment `
      + `${JSON.stringify(matrixEnvironmentName)}, which this deployment does not have`,
  ).toBeTruthy();
  expect(
    String(coldEnvironment?.engine_kind || ''),
    `this spec runs its conversations on ${COLD_ENVIRONMENT}, whose engine is `
      + `${JSON.stringify(String(coldEnvironment?.engine_kind || ''))}, while the matrix `
      + `selected ${JSON.stringify(MATRIX_AGENT)} on `
      + `${JSON.stringify(String(matrixEnvironment?.engine_kind || ''))}. The mechanism is `
      + 'engine-agnostic but the fixture is not: give the other engine its own cold, '
      + 'conversation-tenancy, allow_mcp_servers Environment, or declare claude_code in '
      + 'the matrix contract for this file.',
  ).toEqual(String(matrixEnvironment?.engine_kind || ''));

  const catalog = await platform.data<{
    credential_delivery?: Record<string, unknown>;
  }>('GET', '/admin/vaults');
  expect(
    String(catalog.credential_delivery?.mcp_credentials || ''),
    'this deployment cannot deliver MCP credentials, so an authenticated MCP server is '
      + 'refused with SANDBOX_CREDENTIAL_VAULT_DISABLED at provisioning and the journey '
      + 'is untestable rather than passing. Set ASTRABOX_SANDBOX_CREDENTIAL_VAULT=1.',
  ).toEqual('egress_injection');

  // ── 1. Friday: the operator's two credentials. ────────────────────────────
  // Two, so every record assertion below is differential. A page that stamps
  // "Expired" on everything, and a page that says nothing at all, both fail.
  const vault = await platform.createVault(`__e2e_oauth_weekend_${RUN_ID}`);
  vaultId = String(vault.vault_id || '');
  expect(vaultId, 'the created vault must have an id').not.toEqual('');

  const fridayExpiry = isoAt(DAY_MS);
  const neighbourExpiry = isoAt(30 * DAY_MS);
  const weekend = await platform.createCredential(
    vaultId,
    { type: 'mcp_oauth', mcp_server_url: WEEKEND_URL, access_token: WEEKEND_TOKEN, expires_at: fridayExpiry },
    WEEKEND_LABEL,
  );
  const neighbour = await platform.createCredential(
    vaultId,
    { type: 'mcp_oauth', mcp_server_url: NEIGHBOUR_URL, access_token: NEIGHBOUR_TOKEN, expires_at: neighbourExpiry },
    NEIGHBOUR_LABEL,
  );
  const weekendId = String(weekend.credential_id || '');
  const neighbourId = String(neighbour.credential_id || '');
  expect(weekendId, 'the weekend credential must have an id').not.toEqual('');
  expect(neighbourId, 'the neighbour credential must have an id').not.toEqual('');
  // No `refresh` block, because the console's own form cannot author one — that
  // is what makes this credential unhealable rather than merely stale.
  expect(weekend.auth.refresh, 'the console form authors no refresh block').toBeUndefined();
  expect(
    String(weekend.auth.expires_at || ''),
    'the API must carry back the expiry the operator entered',
  ).toEqual(fridayExpiry);
  expect(JSON.stringify(weekend)).not.toContain(WEEKEND_TOKEN);
  expect(JSON.stringify(neighbour)).not.toContain(NEIGHBOUR_TOKEN);

  // ── 2. Friday: the Agent that declares that server. ───────────────────────
  const created = await api.createColdTestAgent(`__e2e_oauth_weekend_${RUN_ID}`);
  agentId = String(created.agent_id || '');
  expect(agentId, 'the created Agent must have an id').not.toEqual('');
  await platform.data('PUT', `/admin/agents/${agentId}/credential-vaults`, {
    vault_ids: [vaultId],
  });
  const beforeServers = await api.getAgent(agentId);
  const configured = await api.updateAgent(agentId, {
    name: beforeServers.name,
    model: beforeServers.model,
    environment_name: beforeServers.environment_name,
    version: beforeServers.version,
    mcp_servers: { [SERVER_ALIAS]: { type: 'http', url: WEEKEND_URL } },
  });
  // The credential only bites because a target exists. If the write dropped the
  // server map, everything below would pass for the wrong reason.
  expect(
    (configured.mcp_servers as Record<string, { url?: string }> | undefined)?.[SERVER_ALIAS]?.url,
    'the Agent must really store the MCP server the credential answers for',
  ).toEqual(WEEKEND_URL);

  // ── 3. Friday: the operator opens the vault record. ───────────────────────
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });
  const credentialRow = (label: string) =>
    page.getByTestId('managed-credential-row').filter({ hasText: label });

  await page.goto(appPath('/manage/credentials'));
  const vaultRow = page.getByTestId('credential-vault-row').filter({ hasText: vaultId });
  await expect(vaultRow, 'the console must list the vault the operator just made').toBeVisible({
    timeout: 30_000,
  });
  await vaultRow.click();
  await expect(page).toHaveURL(new RegExp(`/manage/credentials/${vaultId}$`), {
    timeout: 30_000,
  });
  // Arrangement, not verdict: the record has to be showing both credentials
  // before what it says about them can mean anything.
  await expect(credentialRow(WEEKEND_LABEL)).toBeVisible({ timeout: 30_000 });
  await expect(credentialRow(NEIGHBOUR_LABEL)).toBeVisible({ timeout: 30_000 });
  const fridayRows: Row[] = [
    { label: WEEKEND_LABEL, text: await credentialRow(WEEKEND_LABEL).innerText() },
    { label: NEIGHBOUR_LABEL, text: await credentialRow(NEIGHBOUR_LABEL).innerText() },
  ];

  // ── 4. Friday: the control arm. ───────────────────────────────────────────
  // Without this, Monday proves only "it fails" — not "it fails because of the
  // expiry". The only thing that changes between the two arms is the date.
  const fridaySession = await api.startConversation(agentId);
  const fridaySessionId = String(fridaySession.session_id || '');
  expect(fridaySessionId, 'the Friday conversation must have an id').not.toEqual('');
  sessions.push(fridaySessionId);
  try {
    await api.waitForSessionReady(fridaySessionId, READY_TIMEOUT_MS);
  } catch (reason) {
    throw new Error(
      'the CONTROL arm could not reach READY, so nothing below separates "the expiry '
        + 'broke it" from "this fixture never worked". The most likely cause is the '
        + `unverified assumption in this spec: the declared MCP endpoint ${WEEKEND_URL} `
        + 'is never dialled during credential resolution, and an engine is assumed to '
        + 'tolerate an unreachable declared MCP server at startup. If that is wrong, this '
        + 'spec needs the real fixtures/nativeMcpServer.ts listener — a second sandbox and '
        + `a second conversation, which does not fit the lane's wall. Cause: `
        + `${(reason as Error).message}`,
    );
  }
  await openSessionView(page, fridaySessionId);
  // A1: with the expiry in the future, this exact Agent, MCP server and
  // credential provision and open a usable conversation.
  await expectComposerEnabled(page);

  // ── 5. The weekend passes. ────────────────────────────────────────────────
  // Deterministically, through a supported API, with no timer and no document
  // fault injection: `expires_at` is not in the immutable set
  // (vault_service.py:311-318), `_split_auth_payload(..., partial=True)` keeps
  // the stored access_token, and create-time validation never required a future
  // date. Nothing else about the credential moves.
  const pastExpiry = isoAt(-3 * DAY_MS);
  const lapsed = await platform.updateCredential(vaultId, weekendId, {
    auth: { expires_at: pastExpiry },
  });
  expect(
    String(lapsed.auth.expires_at || ''),
    'the precondition has to be proven before anything is judged on it: the PATCH must '
      + 'echo the past expiry back',
  ).toEqual(pastExpiry);
  const neighbourAfter = await platform.listCredentials(vaultId);
  expect(
    String(
      neighbourAfter.find((item) => item.credential_id === neighbourId)?.auth.expires_at || '',
    ),
    'the neighbour credential must be untouched, or the two rows stop being a contrast',
  ).toEqual(neighbourExpiry);

  // ── 6. Monday: the operator comes back to the record. ─────────────────────
  // A real navigation, which is a full document load and therefore a fresh
  // catalog fetch on mount. What the row says on Monday is the server's current
  // answer, not a client cache left over from Friday — so a red below is the
  // page dropping a fact it was given, not the page showing a stale one.
  await page.goto(appPath(`/manage/credentials/${vaultId}`));
  await expect(credentialRow(WEEKEND_LABEL)).toBeVisible({ timeout: 30_000 });
  await expect(credentialRow(NEIGHBOUR_LABEL)).toBeVisible({ timeout: 30_000 });
  const mondayRows: Row[] = [
    { label: WEEKEND_LABEL, text: await credentialRow(WEEKEND_LABEL).innerText() },
    { label: NEIGHBOUR_LABEL, text: await credentialRow(NEIGHBOUR_LABEL).innerText() },
  ];

  // ── 7. Monday: the next person opens a conversation. ──────────────────────
  const inventoryBefore = await platform.listSandboxes();
  const boxesBefore = (inventoryBefore.items || []).length;
  expect(
    boxesBefore,
    'the sandbox inventory came back a full page, so its length is a page size rather '
      + 'than a count and the leak comparison below would be meaningless',
  ).toBeLessThan(50);

  let mondaySessionId = '';
  let createRefusal = '';
  try {
    const mondaySession = await api.startConversation(agentId);
    mondaySessionId = String(mondaySession.session_id || '');
    if (mondaySessionId) sessions.push(mondaySessionId);
  } catch (reason) {
    // Both shapes are honest outcomes: a create that refuses outright, and a
    // create that is accepted and then never becomes ready.
    createRefusal = (reason as Error).message;
  }

  let mondayState = '';
  let mondayError = createRefusal;
  let mondaySandboxId = '';
  if (mondaySessionId) {
    const settled = await api.waitForSession(
      mondaySessionId,
      (s) => ['READY', 'TERMINATED', 'DELETED'].includes(String(s.state || '')),
      VERDICT_TIMEOUT_MS,
    );
    mondayState = String(settled.state || '');
    mondayError = String(settled.last_error || '');
    mondaySandboxId = String(settled.sandbox_id || '').trim();
    await openSessionView(page, mondaySessionId);
  }

  // ── 8. Cross-check oracles. ───────────────────────────────────────────────
  const agentAfter = await api.getAgent(agentId);

  // ── VERDICTS ──────────────────────────────────────────────────────────────
  // Behavioural first, so a first red run still carries the evidence for what
  // the silence costs; the absent-surface assertions come last. See the header.

  // A3 — the user's Monday. A verdict is reached, it names the credential, and
  // the page does not pretend the conversation is usable.
  expect(
    mondayState === 'TERMINATED' || createRefusal !== '',
    `the Monday conversation neither refused nor settled: state=${mondayState || '<none>'} `
      + `error=${JSON.stringify(mondayError)}. A dead credential must reach a verdict, not `
      + 'leave someone waiting.',
  ).toBe(true);
  expect(
    mondayError,
    'what the deployment tells the user must name the credential that is dead. The error '
      + 'code VAULT_CREDENTIAL_EXPIRED is not carried past the session boundary — '
      + '`_startup_failure_error_text` keeps only the APIError message '
      + '(session_kernel/workers/lifecycle/startup.py:715) — so the message from that exact '
      + 'raise site is the oracle.',
  ).toContain(weekendId);
  expect(
    mondayError,
    'the message must carry the remedy the raise site writes, not a bare runtime error',
  ).toContain('rotate the credential');
  expect(
    mondayError,
    'the LIVE credential in the same vault must not be blamed for the dead one',
  ).not.toContain(neighbourId);
  expect(mondayError, 'no access token may appear in a user-facing error').not.toContain(
    WEEKEND_TOKEN,
  );
  if (mondaySessionId) {
    // The state is already settled above, so this negative is read against a
    // finished conversation rather than a racing one.
    await expect(
      page.getByTestId('composer-prompt'),
      'the console must not leave the composer enabled on a conversation that can never run',
    ).toBeDisabled();
    // `.first()`: `data-slot="verbatim"` also marks tool-part and result-card
    // bodies, and a locator matching more than one node fails Playwright's
    // strict mode with a locator complaint instead of the product verdict. The
    // header's copy is the first one in document order.
    await expect(
      page.locator('[data-slot="verbatim"]').first(),
      'the conversation view must show the user why it is dead',
    ).toContainText(weekendId);
  }

  // A5 — a dead credential must not push the Agent into a hibernating
  // fallback. Same property agent-stays-active-on-broken-config pins for a
  // broken Environment.
  expect(String(agentAfter.state || ''), 'the Agent must still be ACTIVE').toEqual('ACTIVE');
  expect(
    String((agentAfter.sandbox_id as string | null | undefined) || '').trim(),
    'the Agent must never hold a sandbox of its own',
  ).toEqual('');

  // A4 — blast radius. On the conversation-pool branch
  // (engine/provisioning.py:2056), `provision_engine_sandbox` records a startup
  // allocation and claims a workspace (:2062-2073) BEFORE it resolves the
  // credential plan at :2078, so a box can exist before the raise. A box left
  // behind here is invisible until the pool is exhausted and then shows up as
  // somebody else's POOL_UNAVAILABLE. Off that branch the first allocation is
  // at :2355, after the raise, and these two checks are then satisfied by a
  // provision that never took a box at all.
  expect(
    mondaySandboxId,
    'the failed conversation must not still name a sandbox — `_mark_session_start_failed_direct` '
      + '(session_kernel/workers/lifecycle/startup.py:672) writes `sandbox_id` only for a box '
      + 'it could not destroy (:691-702)',
  ).toEqual('');
  // Two halves, because neither alone is enough. `session_id` is present on a
  // box only when it carries the create metadata (api/routes/sandboxes.py:223),
  // so a box claimed from the pool can leak without ever naming this session —
  // which is what the count catches. The count is asserted as "no more than
  // before" rather than equality: an unrelated idle reclaim of the Friday box
  // moves it the other way and is not this spec's subject.
  if (mondaySessionId) {
    await expect
      .poll(
        async () => {
          const items = (await platform.listSandboxes()).items || [];
          return items.filter(
            (item) => String(item.session_id || '') === mondaySessionId,
          ).length;
        },
        {
          timeout: INVENTORY_SETTLE_MS,
          message:
            'a box is still allocated to the conversation that failed to provision; an '
            + 'aborted provision must give it back',
        },
      )
      .toBe(0);
  }
  await expect
    .poll(async () => ((await platform.listSandboxes()).items || []).length, {
      timeout: INVENTORY_SETTLE_MS,
      message:
        `the deployment held ${boxesBefore} sandboxes before the failing conversation and `
        + 'has not come back down to that',
    })
    .toBeLessThanOrEqual(boxesBefore);

  // A2a — Friday's record. The row reports the expiry the operator entered and
  // does not call it expired. The surface for the first half is absent: the row
  // renders label, type and target only, while `auth.expires_at` is already in
  // its props.
  const fridayWeekend = fridayRows[0].text;
  const fridayNeighbour = fridayRows[1].text;
  expect(
    fridayWeekend,
    'the vault record must report when this credential expires. The page already receives '
      + '`auth.expires_at` (frontend/src/types.ts:140) and renders label, type and target '
      + 'only (CredentialVaultPage.tsx:414-419) — it holds the fact and drops it.',
  ).toContain(dayOf(fridayExpiry));
  expect(
    fridayWeekend,
    'a credential that has not expired must not be reported as expired',
  ).not.toMatch(EXPIRED);
  expect(
    fridayNeighbour,
    'the second credential must report its own expiry, not the first one\'s',
  ).toContain(dayOf(neighbourExpiry));

  // A2b — Monday's record, in two directions. A page that always says
  // "Expired", and a page that never does, both fail here.
  const mondayWeekend = mondayRows[0].text;
  const mondayNeighbour = mondayRows[1].text;
  expect(
    mondayWeekend,
    'once the expiry has passed, the record an operator manages must say so — this is the '
      + 'silence the conversation below pays for',
  ).toMatch(EXPIRED);
  expect(
    mondayWeekend,
    'the row must report the expiry it now carries, not the one it had on Friday',
  ).toContain(dayOf(pastExpiry));
  expect(
    mondayNeighbour,
    'the live credential in the same vault must NOT be marked expired',
  ).not.toMatch(EXPIRED);
});
