/**
 * E2E: an Environment this release cannot run says so in the catalogue,
 * instead of leaving the operator to discover it on the next page.
 *
 * THE JOURNEY. A release ships without one engine — renamed, removed, or a
 * third-party `astrabox.providers.engine` plugin that is absent from it.
 * The Environments stored against it survive that, deliberately
 * (`_environment_with_engine_capabilities`, agent_config_service.py:761-783:
 * "Stored environments survive plugin removal, so the admin catalogue must
 * still render them"). An operator opens /manage/environments, reads the row,
 * opens the record, then walks to /manage/agents/new to point an Agent at it —
 * and the picker does not offer it. The catalogue is the page that has to say
 * why, because the picker is a list of what CAN be chosen and has no room to
 * explain an absence.
 *
 * WHAT THE PRODUCT ALREADY KNOWS. `engine_available` is computed on every
 * `GET /api/v1/admin/environments` (`_environment_with_engine_capabilities`,
 * agent_config_service.py:760-784) — false exactly when the engine kind the
 * row names is not in the running registry — and its docstring says it exists
 * "to explain why one cannot currently be selected". Four surfaces read it: the
 * Agent form's filter (agentEditConfig.ts:153) and the Assistant form's
 * (assistantConfig.ts:144), which drop the row from a picker, and the two this
 * spec judges — the list row (EnvironmentsListPage.tsx:110) and the record
 * status (EnvironmentDetailPage.tsx:199), each stating the missing engine ahead
 * of the enabled flag. Dropping a record from a picker without a surface that
 * accounts for the absence is the shape under test here: enabled on one page,
 * gone from the picker on the next, with no reason given on either.
 *
 * WHY THIS IS NOT A UNIT TEST'S JOB. The property is a comparison ACROSS two
 * pages fed by two different endpoints — the admin catalogue
 * (`GET /admin/environments`, which computes `engine_available`) and the Agent
 * form's narrow projection (`GET /agent-configuration/environments`, which
 * omits the row server-side via `engine_allowed_for_session_kind`). A jsdom
 * test over either page renders the props it was handed and proves nothing
 * about the other; what goes wrong in production is that one surface drops a
 * record the other still shows, with no shared fixture between them. The
 * deciding fact also has to be produced by the real registry, which only a
 * running deployment has.
 *
 * HOW THE RELEASE IS SIMULATED, AND WHY IT IS FAITHFUL. No product API can
 * create this state: `engine_kind` is an enum resolved per call from
 * `known_engine_kinds()` (environment_schema.py:59-62), so `PUT
 * /admin/environments/{name}` answers 400 INVALID_REQUEST for a kind this
 * release does not install. Unregistering an adapter inside the server would be
 * process-global and would poison every concurrent worker. So the stored
 * `engine_kind` is moved instead: in production the row keeps its kind and the
 * registry loses the adapter; here the registry keeps its adapters and the row
 * changes its kind. Every line under test reads only the predicate
 * `engine_kind ∈ registry`, so the two are indistinguishable to it — and the
 * spec machine-checks both halves of that predicate against the live enum in
 * `GET /admin/environment-schema` rather than assuming them.
 *
 * ENGINE-INDEPENDENT. The spec names no engine. It reads the installed kind off
 * whichever Environment the matrix-selected Agent runs on and derives the
 * uninstalled one from it, so it runs under every profile. No conversation, no
 * turn, no sandbox, no prepared slot: the Environment it writes is metadata that
 * never starts a box.
 *
 * WHY PARALLEL. Nothing here is exclusive to the deployment. The only mutated
 * document is a row this spec owns; the donor Environment is read, never
 * written. Once its engine kind is retired the row is inert rather than broken —
 * `engine_allowed_for_session_kind` returns False (it never raises) so
 * `/agent-configuration/environments` simply omits it, and
 * `_environment_with_engine_capabilities` catches `EngineKindNotRegistered` so
 * the admin list does not 500. No lane spec asserts an Environment count or the
 * rail's Environments badge: the one lane spec that walks /manage/environments
 * reads the AGENTS badge there
 * (a-record-created-in-the-console-is-counted-on-every-page.parallel.spec.ts:420-426),
 * and that same spec, the only other one that drives /manage/agents/new, pins
 * its picker assertions to the matrix Environment by name (:365-374).
 *
 * WHAT IT LEAVES BEHIND. The Environment name is stable, not run-scoped, for
 * the reason its siblings give: `admin.py` offers environments only GET and PUT,
 * so a run-scoped name would leave one row per run on the deployment forever
 * (idle-parked-conversation-serves-its-workspace-and-its-next-message.exclusive.spec.ts:306).
 * A pass restores the row's engine kind, leaving an ordinary enabled row that
 * the next run upserts again. A failure KEEPS the retired kind as evidence and
 * names the row in the report tail, the way sessionCleanup keeps a failed
 * session's sandbox — and a rerun heals it, because the PUT below re-establishes
 * the installed kind before anything else happens.
 *
 * PREREQUISITE. `patchDocs` reaches the deployment database through
 * `ASTRABOX_E2E_POSTGRES_CONTAINER`; unset, `requireServiceContainer` marks the
 * test skipped with the reason printed. That is the same exposure every
 * fault-injection spec carries, and the lane sets it.
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { patchDocs } from '../fixtures/dbOracle';
import { appPath } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly } from '../fixtures/sessionCleanup';

// Both status words this spec reads are localized — `common:enabled`,
// `common:disabled`, and `manage:environments.engine_missing` — and the console detects
// language as ['localStorage','navigator']. An unpinned runner locale decides
// which spelling appears, and a spec written to accept two would accept a third
// nobody wrote it against. Pin the navigator locale here and the persisted
// 'astrabox-lang' before the first navigation.
test.use({ locale: 'en-US' });

/**
 * The collection an Environment document lives in — singular, from
 * `settings.environment_collection` (common/utils/settings.py:254). `patchDocs`
 * refuses anything but exactly one matched document, so a name that ever stops
 * being right fails loudly here instead of patching nothing quietly.
 */
const ENVIRONMENT_COLLECTION = 'environment';

/** Results that mean "keep the scene", matching fixtures/sessionCleanup.ts. */
const FAILURE_STATUSES = new Set(['failed', 'timedOut', 'interrupted']);

/** What teardown has to put back, populated only once the release is injected. */
const retired: { environmentName: string; installedKind: string; uninstalledKind: string } = {
  environmentName: '',
  installedKind: '',
  uninstalledKind: '',
};

// A pass puts the row's engine back, by the same single-field write that took it
// away: restoring through `PUT /admin/environments/{name}` would re-run
// normalization and validation, which is a second thing that can fail in
// teardown and a second thing to read when it does.
onPassOnly(async () => {
  if (!retired.environmentName) return;
  patchDocs(
    ENVIRONMENT_COLLECTION,
    { '$.name': retired.environmentName },
    { engine_kind: retired.installedKind },
  );
});

// Registered as an `afterEach` because that is where the result is real; inside
// the body a failing test still reads as passing.
test.afterEach(async ({}, testInfo) => {
  if (!retired.environmentName || !FAILURE_STATUSES.has(String(testInfo.status || ''))) return;
  // eslint-disable-next-line no-console -- the report tail is where an operator looks
  console.log(
    `KEPT for diagnosis — this Environment row still names an engine this release does not\n`
      + `install, so the catalogue will go on marking it until someone clears it:\n`
      + `  environment=${retired.environmentName} engine_kind=${retired.uninstalledKind}\n`
      + `  restore: PUT /api/v1/admin/environments/${retired.environmentName} `
      + `{"engine_kind": "${retired.installedKind}"} — or rerun this spec, which upserts it back.`,
  );
});

/**
 * One catalogue row, addressed by the record name printed under its title.
 *
 * `NameCell` puts `envDisplayName(e)` on the first line and `e.name` on the
 * second (EnvironmentsListPage.tsx:102-117), so an exact match on the name is
 * the row's identity rather than a substring of whatever else it renders. The
 * table is laid out as CSS grid, which is why `console-interaction.audit.spec.ts`
 * and `a-sandbox-record-reports-the-box-not-an-empty-card.parallel.spec.ts:129-131`
 * address rows the same way.
 */
function catalogueRow(page: Page, name: string): Locator {
  return page
    .locator('tr[tabindex], [role="row"][tabindex]')
    .filter({ has: page.getByText(name, { exact: true }) });
}

/** Every status marking inside one element, as the reader sees it. */
async function pillTexts(scope: Locator): Promise<string[]> {
  const texts = await scope.getByTestId('status-pill').allTextContents();
  return texts.map((text) => text.trim()).filter((text) => text !== '');
}

test('an environment whose engine this release no longer installs is marked in the catalogue, not only dropped from the Agent form', async ({
  page,
  request,
}) => {
  const uncaught: string[] = [];
  // Attached before the first navigation: an `engine_kind` no adapter claims
  // reaching a render is exactly the crash a listener added afterwards misses.
  page.on('pageerror', (error) => uncaught.push(error.message));

  const api = new AstraApi(request);
  const platform = new PlatformApi(request);

  // ── Premise: which engine this deployment installs, proven not assumed ────
  const laneAgent = await api.defaultAgent();
  const donorEnvironment = String(laneAgent.environment_name || '').trim();
  expect(
    donorEnvironment,
    `the lane's Agent ${JSON.stringify(laneAgent.name)} must name the Environment whose engine `
      + 'this reads; without it the spec has no installed kind to retire',
  ).not.toEqual('');

  const environments = await platform.listEnvironments();
  const donor = environments.find((row) => String(row.name || '') === donorEnvironment);
  expect(
    donor,
    `Environment ${JSON.stringify(donorEnvironment)} is not on this deployment; have: `
      + environments.map((row) => String(row.name || '')).join(', '),
  ).toBeTruthy();
  const installedKind = String((donor as Record<string, unknown>).engine_kind || '').trim();
  expect(installedKind, 'the lane Environment must name the engine it runs on').not.toEqual('');
  // Derived from the installed kind rather than spelled out, so the spec stays
  // engine-independent and the absence below is obvious rather than lucky.
  const uninstalledKind = `${installedKind}_not_installed`;

  // The form schema resolves `engine_kind`'s enum from `known_engine_kinds()`
  // on every request (environment_schema.py:59-62), so this is the running
  // registry answering — the one authority on what "installed" means here.
  const schema = await platform.environmentSchema();
  const engineField = (schema.fields || []).find(
    (field) => String(field.key || '') === 'engine_kind',
  );
  expect(
    engineField,
    'the Environment form schema must declare engine_kind; without it there is no live '
      + 'registry reading and the premise below would be an assumption',
  ).toBeTruthy();
  const declaredEnum = (engineField as Record<string, unknown>).enum;
  const installedKinds = Array.isArray(declaredEnum) ? declaredEnum.map((k) => String(k)) : [];
  expect(
    installedKinds,
    'engine_kind must offer the kinds this release registered',
  ).not.toEqual([]);
  expect(
    installedKinds,
    `this deployment must actually install ${JSON.stringify(installedKind)} for the control `
      + 'arm to mean anything',
  ).toContain(installedKind);
  expect(
    installedKinds,
    `${JSON.stringify(uninstalledKind)} must NOT be installed here, or the "release that removed `
      + 'it" is not a release that removed anything',
  ).not.toContain(uninstalledKind);

  // ── The row under test: metadata only, never a box ───────────────────────
  // Stable, not run-scoped: environments have GET and PUT and no DELETE. It
  // carries no runtime fields and no credential, unlike the clones the lifecycle
  // specs make, because it exists to be READ in the catalogue — cloning the
  // donor would copy a masked provider secret into a row with no stored key
  // behind it, which is a lie in a record nothing will ever run.
  const environmentName = `__e2e-engine-retired-${installedKind}`;
  const stored = await platform.putEnvironment(environmentName, {
    display_name: environmentName,
    description:
      'E2E fixture: how the catalogue reads when this release stops installing its engine. '
      + 'Runs nothing.',
    engine_kind: installedKind,
    enabled: true,
  });
  expect(
    String(stored.engine_kind || ''),
    'the stored Environment must be the one the catalogue will read',
  ).toEqual(installedKind);

  const catalogueEntry = async (): Promise<Record<string, unknown>> => {
    const rows = await platform.listEnvironments();
    const match = rows.find((row) => String(row.name || '') === environmentName);
    expect(
      match,
      `the admin catalogue must still list ${JSON.stringify(environmentName)}; stored `
        + 'environments outlive plugin removal by design, so a row missing here is the '
        + 'catalogue hiding a record rather than marking it',
    ).toBeTruthy();
    return match as Record<string, unknown>;
  };

  const runnable = await catalogueEntry();
  expect(
    runnable.engine_available,
    'the control reading: the catalogue must report this Environment as runnable before '
      + 'its engine leaves',
  ).toBe(true);

  // ── Control arm, catalogue: nothing is wrong with this Environment ───────
  // Pin the console language before the FIRST navigation (see test.use above):
  // the persisted 'astrabox-lang' outranks navigator in the detection order.
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });
  await page.goto(appPath('/manage/environments'));

  // The search field is deliberately NOT driven: `ConsoleSearch` does not render
  // below twelve rows (ConsoleControls.tsx:40), so a spec that typed into it
  // would fail on a small deployment for a reason that has nothing to do with
  // engines. The list renders every row it loaded, so the row is on screen.
  const runnableRow = catalogueRow(page, environmentName);
  await expect(
    runnableRow,
    `exactly one catalogue row must be ${JSON.stringify(environmentName)}`,
  ).toHaveCount(1);
  const runnableRowPills = await pillTexts(runnableRow);

  // Opened by clicking the row, the way a reader opens it.
  await runnableRow.click();
  const recordPath = appPath(`/manage/environments/${environmentName}`);
  await expect(page, 'the row must open the record it names').toHaveURL(
    (url) => url.pathname === recordPath,
  );

  // One verdict per record page: `ConsoleRecordPage` is the only thing on this
  // page that renders a StatusPill (ConsoleRecordPage.tsx:53-57).
  const recordStatus = page.getByTestId('status-pill');
  await expect(
    recordStatus,
    'the Environment record must state exactly one status for the record',
  ).toHaveCount(1);
  const runnableStatus = {
    text: (await recordStatus.innerText()).trim(),
    // The tone token rather than the class, so the comparison survives a
    // restyling and does not rest on English copy alone
    // (AstraConsole.tsx:73-82, 121-123).
    tone: await recordStatus.getAttribute('data-tone'),
  };
  expect(
    runnableStatus,
    'a runnable, enabled Environment reads as enabled, in the healthy tone',
  ).toEqual({ text: 'Enabled', tone: 'mint' });

  // ── Control arm, the other half of the journey: the Agent form offers it ──
  await page.goto(appPath('/manage/agents/new'));
  // `env_ref` renders as the platform's native select (agentEditConfig.ts:38-40
  // → ConsoleSelect, ConsoleForm.tsx:598), and the control's id is minted from
  // the page's prefix and the schema key (`agent-new` + `environment_name`,
  // ConsoleEditSections.tsx:68-78).
  const environmentPicker = page.locator('#agent-new-environment_name');
  await expect(
    environmentPicker.locator(`option[value="${environmentName}"]`),
    'an Agent author must be offered an Environment the product can run — without this the '
      + 'disappearance below proves nothing, because it would never have been there',
  ).toHaveCount(1);
  await expect(
    environmentPicker.locator(`option[value="${donorEnvironment}"]`),
    `the picker must also offer the lane's own Environment ${JSON.stringify(donorEnvironment)}`,
  ).toHaveCount(1);

  // ── The release: this deployment stops installing that engine ────────────
  retired.environmentName = environmentName;
  retired.installedKind = installedKind;
  retired.uninstalledKind = uninstalledKind;
  patchDocs(
    ENVIRONMENT_COLLECTION,
    { '$.name': environmentName },
    { engine_kind: uninstalledKind },
  );

  const orphaned = await catalogueEntry();
  expect(
    orphaned.engine_available,
    'the deciding fact must be on the wire: the catalogue computes engine_available on every '
      + 'list request, and this is the state the console is handed',
  ).toBe(false);
  expect(
    orphaned.enabled,
    'nobody disabled this Environment — its engine left. A product that conflates the two '
      + 'sends the operator to flip a switch that is already on',
  ).not.toBe(false);

  // ── Comparison arm, Agent form: the product's verdict has flipped ────────
  await page.goto(appPath('/manage/agents/new'));
  await expect(
    environmentPicker.locator(`option[value="${donorEnvironment}"]`),
    `the picker must still work — ${JSON.stringify(donorEnvironment)} is the lane's own `
      + 'Environment, and an empty picker would make the next assertion pass for the wrong reason',
  ).toHaveCount(1);
  await expect(
    environmentPicker.locator(`option[value="${environmentName}"]`),
    'an Agent can no longer be pointed at an Environment whose engine is not installed. '
      + '(ConsoleSelect surfaces an unknown CURRENT value as a real option, but a new Agent '
      + "draft holds no environment_name, so zero here means the picker dropped it.)",
  ).toHaveCount(0);

  // ── Comparison arm, catalogue: the page that has to say why ──────────────
  await page.goto(appPath('/manage/environments'));
  const orphanedRow = catalogueRow(page, environmentName);
  await expect(
    orphanedRow,
    'the record must still be listed after its engine leaves: stored environments survive '
      + 'plugin removal on purpose, so hiding the row is not an answer to this',
  ).toHaveCount(1);

  // Both rows off the SAME render, so the comparison is between two records on
  // one screen rather than between two page loads.
  const donorRow = catalogueRow(page, donorEnvironment);
  await expect(
    donorRow,
    `exactly one catalogue row must be the lane's Environment ${JSON.stringify(donorEnvironment)}`,
  ).toHaveCount(1);
  const orphanedRowPills = await pillTexts(orphanedRow);
  const donorRowPills = await pillTexts(donorRow);

  expect(
    orphanedRowPills,
    'THE POINT OF THIS SPEC: the catalogue may not present an Environment the product cannot '
      + `run exactly as it presents one it can. The Agent form refuses ${JSON.stringify(environmentName)} `
      + `and offers ${JSON.stringify(donorEnvironment)}, so their rows must not read the same.`,
  ).not.toEqual(donorRowPills);
  expect(
    orphanedRowPills.length,
    'the refused row must carry a marking of its own, not merely differ by what it lacks',
  ).toBeGreaterThanOrEqual(1);
  expect(
    orphanedRowPills,
    'the marking must not be the word the console uses for the operator-set off state: the '
      + 'catalogue reports this row as enabled on the same request, and calling it Disabled '
      + 'would send the reader to a switch that is already on',
  ).not.toContain('Disabled');
  expect(
    runnableRowPills,
    'the control row carried no marking while its engine was installed, so the marking above '
      + 'is the engine leaving and not a decoration every row wears',
  ).not.toEqual(orphanedRowPills);

  // ── Comparison arm, the record: the same row, before and after ───────────
  await orphanedRow.click();
  await expect(page, 'the row must still open its record').toHaveURL(
    (url) => url.pathname === recordPath,
  );
  await expect(
    recordStatus,
    'the record must still state exactly one status',
  ).toHaveCount(1);
  const orphanedStatus = {
    text: (await recordStatus.innerText()).trim(),
    tone: await recordStatus.getAttribute('data-tone'),
  };
  expect(
    orphanedStatus,
    'the record page must not read the same after its engine left as it did while the engine '
      + 'was installed — this is the page an operator opens to find out what is wrong',
  ).not.toEqual(runnableStatus);
  expect(
    orphanedStatus.tone,
    'and it must not still be toned as a healthy record: the tone is what a reader scanning '
      + 'the page takes in before any word',
  ).not.toEqual('mint');
  expect(
    orphanedStatus.text,
    'nor may the record call it Disabled while the catalogue reports it enabled',
  ).not.toEqual('Disabled');

  // The fault landed where an operator edits, verbatim — proof of injection
  // rather than part of the finding. `ConsoleSelect` surfaces an unknown current
  // value as a real option instead of silently rewriting it
  // (ConsoleForm.tsx:619-635), so the form states the kind the row actually
  // names.
  await expect(
    page.locator('#environment-engine_kind'),
    'the record form must show the engine kind the row stores, not the nearest installed one',
  ).toHaveValue(uninstalledKind);

  expect(
    uncaught,
    'an engine_kind no adapter claims must not crash a render in the built bundle:\n'
      + uncaught.join('\n'),
  ).toEqual([]);
});
