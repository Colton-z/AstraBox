/**
 * E2E: the console's record grammar — issue, create, edit, revert, delete —
 * driven from the pages that own it, in a real build.
 *
 * Every manage page is assembled from the same six parts: a create page that
 * refuses before it writes, a card that shows Save only once something differs,
 * a crimson button with and without a question attached, and a trail that has
 * to name the record rather than repeat its id. Nothing drove any of them. The
 * audit walk loads these routes and clicks every visible control, but its
 * assertion is "the DOM, the URL or a request changed" — so a Delete that
 * removed the wrong record, a Revert that wrote instead of restoring, a Create
 * that overwrote the environment already holding that name, and a trail
 * printing a raw `vlt_…` id all pass it.
 *
 * Four properties here cannot be reached from any other layer:
 *
 *  1. The MCP client block is BUILT IN THE BROWSER (McpTokensPage.clientConfig)
 *     out of `window.location.origin`. It is the one artefact on that page a
 *     reader pastes somewhere else, and no server test can see it: pointed at
 *     the wrong host, or missing its Authorization header, every copied config
 *     fails silently in somebody's MCP client.
 *  2. The two intensities of ConsoleDangerButton are opposite promises. Revoke
 *     carries no `confirm` and must act on its own onClick; the vault's Delete
 *     carries one and must not act until the armed word is pressed. Escape is
 *     driven before the confirm, because a test that only confirms passes on a
 *     dialog whose Cancel also deletes.
 *  3. The collision guard is asserted in BOTH directions. `upsertAdminEnvironment`
 *     would overwrite the environment already holding a name, so the create page
 *     blocks on it — and "the button is disabled" is equally true of a button
 *     that is always disabled, so the same field then takes a free name and the
 *     button has to come back.
 *  4. A ConsoleCard's Save is proof of nothing on its own: the tag says "Saved"
 *     whether or not a write left the browser. The page is reloaded and the
 *     field re-read, which is the only version of that claim a card cannot fake.
 *
 * The `pageerror` guard is not about any of them. It is the part that
 * generalises: whatever a future import shape does to one of these routes, an
 * uncaught exception fails here instead of blanking a reader's screen.
 *
 * Parallel-safe: it owns one throwaway Agent and the two records it creates and
 * destroys through the UI, starts no conversation and claims no sandbox.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly } from '../fixtures/sessionCleanup';
import { appPath } from '../fixtures/env';

const runId = Date.now();
const MCP_KEY_NAME = `e2e-mcp-${runId}`;
const VAULT_NAME = `e2e-vault-${runId}`;
/** A name no environment holds — the free half of the collision check. */
const FREE_ENVIRONMENT_NAME = `e2e-env-${runId}`;
const DISCARDED_NAME = `e2e discarded ${runId}`;
const SAVED_NAME = `e2e renamed ${runId}`;

/** Any alertdialog that entered the page, counted as it arrives. */
type DialogWatch = { count: number };

let throwawayAgentId = '';

// The Agent outlives a failure on purpose (see fixtures/sessionCleanup): a
// record deleted in teardown is a record nobody can look at afterwards.
onPassOnly(async ({ request }) => {
  if (throwawayAgentId) await new AstraApi(request).deleteAgent(throwawayAgentId);
});

test('a console record is created, edited and destroyed from the page that owns it', async ({
  page,
  request,
}) => {
  const uncaught: string[] = [];
  // Before the first navigation: these crashes happen during a render, and a
  // listener added afterwards would miss the one that mattered.
  page.on('pageerror', (error) => uncaught.push(error.message));

  const api = new AstraApi(request);

  // ── Setup. The API is allowed here and nowhere below ─────────────────────
  const seeded = await api.defaultAgent();
  const environmentName = String(seeded.environment_name || '').trim();
  expect(environmentName, 'the seeded agent must name an environment to borrow').not.toEqual('');
  // The seeded agent pins no model — a null there means "whatever this
  // environment defaults to", which is what every deployment ships. So the
  // throwaway agent's model is resolved the way the rest of the tree resolves
  // it (deployment-trigger-runs-the-agent…:52): the agent's own pin when it has
  // one, otherwise the first concrete entry from the environment's listing.
  // Wildcards are skipped because they are a policy pattern, not a model.
  let model = String(seeded.model || '').trim();
  if (!model || model.includes('*')) {
    const models = await api.listEnvironmentModels(environmentName);
    model = models.find((candidate) => candidate && !candidate.includes('*')) ?? '';
  }
  expect(model, 'the throwaway agent needs a concrete model to name').not.toEqual('');

  // The name the create page must refuse. Taken from the same listing the page
  // reads its collision set from — and the seeded agent's environment rather
  // than whichever row sorts first, because another spec's throwaway record
  // must not be able to become the name this one collides with.
  const environments = await api.data<Array<Record<string, unknown>>>('GET', '/admin/environments');
  const takenEnvironmentName = environments
    .map((item) => String(item.name || '').trim())
    .find((name) => name === environmentName) ?? '';
  expect(
    takenEnvironmentName,
    'the environment the seeded agent runs on must be in the admin listing the create page reads',
  ).toEqual(environmentName);

  const agent = await api.createAgent({
    name: `__e2e_card_${runId}`,
    model,
    environment_name: environmentName,
  });
  throwawayAgentId = String(agent.agent_id || '').trim();
  expect(throwawayAgentId, 'the throwaway Agent must have an id').not.toEqual('');

  // ── 1. Issue an MCP key, and read the block the reader would paste ───────
  // Copy: manage:mcp_tokens.{field_name,field_scope,scope_read,issue,issued_title,
  // issue_title,issued_done}.
  await page.goto(appPath('/manage/mcp-tokens'));

  // By accessible name rather than `getByLabel`: a required console field puts
  // an `aria-hidden` asterisk inside its label, and `getByLabel` reads the
  // label's raw text — so an exact "Name" there matches nothing at all. The
  // name a reader is announced is the one to locate by.
  await page.getByRole('textbox', { name: 'Name', exact: true }).fill(MCP_KEY_NAME);
  await page.getByRole('combobox', { name: 'What it may do' }).selectOption({ label: 'Read only' });
  await page.getByRole('button', { name: 'Issue key' }).click();

  // The secret card REPLACES the form rather than sitting beside it, so the
  // form's absence is part of what is being asserted.
  await expect(page.getByRole('heading', { name: `${MCP_KEY_NAME} is ready` })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Issue a key' })).toHaveCount(0);

  // Read as raw text rather than matched as a locator: this block is the thing
  // a reader copies, and the two claims worth making about it are exact
  // substrings of it. The page renders one `<pre>`, and only while a key is on
  // screen.
  const clientConfig = page.locator('pre');
  await expect(clientConfig).toBeVisible();
  const clientConfigText = (await clientConfig.textContent()) ?? '';
  expect(
    clientConfigText,
    'the pasted block must point at the deployment serving this console',
  ).toContain(`${new URL(page.url()).origin}/api/v1/mcp`);
  expect(
    clientConfigText,
    'the pasted block must carry the key as a bearer credential',
  ).toContain('"Authorization": "Bearer ');

  await page.getByRole('button', { name: 'I have copied it' }).click();
  await expect(page.getByRole('heading', { name: 'Issue a key' })).toBeVisible();
  await expect(
    page.locator('pre'),
    'the secret must leave the page with the card that held it',
  ).toHaveCount(0);

  // ── 2. Revoke it — the danger button WITHOUT a question ──────────────────
  // "No dialog" has to be watched rather than sampled: a `confirm` on this
  // button would mount one, and a check made after the row is gone can only
  // ever see a dialog that is still open.
  await page.evaluate(() => {
    const watch = { count: 0 };
    (window as unknown as { __e2eAlertDialogs: DialogWatch }).__e2eAlertDialogs = watch;
    new MutationObserver(() => {
      if (document.querySelector('[role="alertdialog"]')) watch.count += 1;
    }).observe(document.body, { childList: true, subtree: true });
  });

  const issuedRow = page.getByRole('row').filter({ hasText: MCP_KEY_NAME });
  await expect(issuedRow).toHaveCount(1);
  // The scope the select was driven to, read back off the row: without this the
  // "Read only" choice is made and never checked.
  await expect(issuedRow).toContainText('Read only');

  await issuedRow.getByRole('button', { name: 'Revoke' }).click();
  await expect(issuedRow, 'Revoke must act on its own onClick, unprompted').toHaveCount(0);
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __e2eAlertDialogs?: DialogWatch }).__e2eAlertDialogs?.count ?? -1,
    ),
    'revoking a key must not open a confirmation dialog',
  ).toBe(0);

  // ── 3. Create a vault, and check the trail names the record ──────────────
  // Copy: manage:credentials.{create,name}, common:create.
  await page.goto(appPath('/manage/credentials'));

  // The empty state offers this action under the same words as the page
  // header, and either is the same click — hence `.first()` rather than a
  // locator that is ambiguous on a deployment with no vaults yet.
  await page.getByRole('button', { name: 'New credential vault' }).first().click();
  const createVault = page.getByRole('dialog', { name: 'New credential vault' });
  await createVault.getByRole('textbox', { name: 'Name', exact: true }).fill(VAULT_NAME);
  await createVault.getByRole('button', { name: 'Create', exact: true }).click();

  // A vault is keyed by an opaque id (`vlt_` + 32 hex, vault_repository.py), not
  // by its name: landing anywhere else means the dialog navigated somewhere
  // other than the record it just made.
  await expect(page).toHaveURL(/\/manage\/credentials\/vlt_[0-9a-f]{32}$/);
  const vaultId = new URL(page.url()).pathname.split('/').pop() ?? '';

  // RecordCrumbProvider's whole reason for existing: the last segment of the
  // trail falls back to the URL segment, so a record page that forgets to name
  // itself puts that id where a reader expects a name.
  const trail = page.getByRole('navigation', { name: 'Breadcrumb' }).getByRole('listitem');
  await expect(trail).toHaveCount(3);
  await expect(trail.nth(2)).toHaveText(VAULT_NAME);
  expect(
    (await trail.nth(2).textContent()) ?? '',
    'the record crumb must name the vault, not repeat its id',
  ).not.toContain(vaultId);

  // ── 4. Delete it — the danger button WITH a question ─────────────────────
  // Copy: common:delete, manage:credentials.confirm_delete_vault, common:confirm_delete.
  // The vault holds no credentials, so the heading's Delete is the only one.
  await page.getByRole('button', { name: 'Delete', exact: true }).click();
  const confirmDelete = page.getByRole('alertdialog');
  await expect(confirmDelete).toBeVisible();
  await expect(confirmDelete).toContainText(
    `Delete “${VAULT_NAME}” and every credential in it? `
      + 'This erases the stored secrets and cannot be undone.',
  );

  // The way out has to be a way out. A spec that only presses the armed word
  // would pass on a dialog whose Cancel deletes too.
  await page.keyboard.press('Escape');
  await expect(confirmDelete).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`/manage/credentials/${vaultId}$`));

  await page.getByRole('button', { name: 'Delete', exact: true }).click();
  await expect(page.getByRole('alertdialog')).toBeVisible();
  // By id, not by label: the credentials under this heading carry a Delete of
  // their own, and the id sits on the armed word specifically so arming the
  // wrong control fails here instead of deleting the wrong record.
  await page.getByTestId('credential-vault-delete').click();

  await expect(page).toHaveURL(/\/manage\/credentials$/);
  await expect(
    page.getByRole('row').filter({ hasText: VAULT_NAME }),
    'the deleted vault must be gone from the listing it returns to',
  ).toHaveCount(0);

  // ── 5. A create page that refuses, and then stops refusing ───────────────
  // Copy: manage:env_form.fields.name.label, manage:environments.err_name_exists,
  // common:create, common:cancel.
  await page.goto(appPath('/manage/environments/new'));
  const newEnvironmentName = page.getByRole('textbox', { name: 'Name', exact: true });
  const createEnvironment = page.getByRole('button', { name: 'Create', exact: true });

  await newEnvironmentName.fill(takenEnvironmentName);
  await expect(
    page.getByText(`An environment named “${takenEnvironmentName}” already exists. Pick another name.`),
  ).toBeVisible();
  await expect(createEnvironment).toBeDisabled();

  // The control half. Without it this proves the button is disabled, not that
  // the collision is what disabled it.
  await newEnvironmentName.fill(FREE_ENVIRONMENT_NAME);
  await expect(createEnvironment).toBeEnabled();

  await page.getByRole('button', { name: 'Cancel' }).click();
  await expect(page).toHaveURL(/\/manage\/environments$/);
  expect(
    (await api.data<Array<Record<string, unknown>>>('GET', '/admin/environments')).map((item) =>
      String(item.name || ''),
    ),
    'walking away from the create page must write nothing',
  ).not.toContain(FREE_ENVIRONMENT_NAME);

  // ── 6. A card that saves, and a Revert that restores ─────────────────────
  // Copy: misc:agent_form.fields.display_name.label, common:{save,revert,saved}.
  await page.goto(appPath(`/manage/agents/${throwawayAgentId}`));
  const displayName = page.getByRole('textbox', { name: 'Display name', exact: true });
  // The form is up before anything is asserted absent — otherwise "no Save on
  // screen" is also true of a page that has not rendered its cards yet.
  await expect(displayName).toBeVisible();
  // The throwaway Agent was created without one, so the stored value is the
  // empty field. Reverting to it is still the assertion that matters: a Revert
  // that did nothing would leave the discarded text behind.
  const storedDisplayName = await displayName.inputValue();

  const save = page.getByRole('button', { name: 'Save', exact: true });
  const revert = page.getByRole('button', { name: 'Revert', exact: true });
  await expect(save, 'a card with nothing to write offers no Save').toHaveCount(0);
  await expect(revert).toHaveCount(0);

  await displayName.fill(DISCARDED_NAME);
  await expect(save).toHaveCount(1);
  await expect(revert).toHaveCount(1);

  await revert.click();
  await expect(displayName).toHaveValue(storedDisplayName);
  await expect(save, 'a reverted card is no longer dirty').toHaveCount(0);
  await expect(revert).toHaveCount(0);

  await displayName.fill(SAVED_NAME);
  await save.click();
  // The tag and the disappearance of the two buttons land together (`saved &&
  // !dirty`), and the tag clears itself after 2.6s — so it is asserted first.
  await expect(page.getByText('Saved', { exact: true })).toBeVisible();
  await expect(save).toHaveCount(0);

  // The tag alone would pass on a card that never wrote.
  await page.reload();
  await expect(page.getByRole('textbox', { name: 'Display name', exact: true })).toHaveValue(
    SAVED_NAME,
  );

  expect(
    uncaught,
    `uncaught exception while driving the console's record pages:\n${uncaught.join('\n')}`,
  ).toEqual([]);
});
