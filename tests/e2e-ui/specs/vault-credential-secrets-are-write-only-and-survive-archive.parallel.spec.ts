/**
 * E2E: vaults — the user-owned credential store. Secrets go in and never come
 * back out; archive keeps the record while retiring the payload; and a
 * credential type this deployment cannot honour is refused at the door rather
 * than accepted and silently ignored.
 *
 * Nothing exercised vaults. That is the wrong thing to leave untested: this is
 * the one surface where a regression means printing somebody's token into an API
 * response. The write-only property is stated in the route module's own
 * docstring and enforced in the service; there was no test that would notice it
 * being lost.
 *
 * ── THE DURABLE PROPERTIES ──────────────────────────────────────────────────
 *  1. Secret fields are WRITE-ONLY. `token` is accepted on create and appears in
 *     NO response: not the create echo, not the list, not the get, not after a
 *     rotate. The spec searches whole serialized responses for the secret's
 *     literal text rather than checking named fields, because a leak that
 *     mattered would arrive under a field nobody thought to check.
 *  2. The OPEN half is readable. `type` and `mcp_server_url` come back, because
 *     a credential you cannot identify is a credential you cannot manage.
 *  3. Archive retires without erasing: the credential keeps its id and its open
 *     metadata and gains `archived_at`. Same for the vault.
 *  4. An archived vault is read-only — adding to it is refused.
 *  5. A credential type the deployment has no backend for is refused AT CREATE
 *     with the reason, not accepted into a vault where it could never work.
 *     (`environment_variable` needs a backend with egress credential
 *     substitution; the service capability-gates it deliberately.)
 *
 * ── WHERE THE ORACLE LIVES ──────────────────────────────────────────────────
 * Bearer credentials use the HTTP surface; HTTP Basic is created through the
 * real console editor and read back after reload. A second oracle
 * starts a fresh Python process inside the deployed server container and asks
 * the selected SecretStore to decrypt the exact slot. It returns only a SHA-256
 * digest. That proves local AES-GCM or AWS KMS can be read by another stateless
 * process without ever bringing the plaintext back into Playwright output.
 *
 * Parallel-safe: it owns only the vault it creates, touches no sandbox and
 * starts no conversation.
 */
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';

import { test, expect } from '@playwright/test';

import { PlatformApi } from '../fixtures/platformApi';
import { appPath } from '../fixtures/env';
import {
  requireServiceContainer,
  SERVER_CONTAINER_HANDLE,
} from '../fixtures/serviceContainer';

const RUN_ID = new Date().toISOString().replace(/[:.]/g, '-');
// Distinctive enough that finding it anywhere in a response body is proof of a
// leak and not a coincidence.
const SECRET_TOKEN = `e2e-vault-secret-${RUN_ID}-DO-NOT-ECHO`;
const ROTATED_TOKEN = `e2e-vault-rotated-${RUN_ID}-DO-NOT-ECHO`;
const BASIC_TOKEN = `e2e-basic-secret-${RUN_ID}-DO-NOT-ECHO`;
const MCP_SERVER_URL = 'https://mcp.example.invalid/sse';
const BASIC_REPOSITORY_URL = 'https://github.com/example/private-skills.git';

type SecretStoreProbe = { store: string; digest: string | null };

const SECRET_STORE_PROBE = String.raw`
import asyncio
import hashlib
import json
import sys

from astrabox.providers import secret_store as _local_store
from astrabox.providers import secret_store_aws_kms as _aws_kms_store
from astrabox.deploy.onebox import ensure_database_wiring
from astrabox.seams.secrets import secret_store_for_name

async def main():
    ensure_database_wiring()
    store = secret_store_for_name(None)
    value = await store.get(scope=sys.argv[1], key=sys.argv[2])
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest() if value is not None else None
    print(json.dumps({"store": store.name, "digest": digest}, sort_keys=True))

asyncio.run(main())
`;

function deployedSecretStoreName(serverContainer: string): string {
  const result = spawnSync(
    'docker',
    ['exec', serverContainer, 'printenv', 'ASTRABOX_SECRET_STORE'],
    { encoding: 'utf8', timeout: 10_000, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  if (result.error) throw new Error('could not inspect the deployed SecretStore selection');
  if (result.status === 1 && !String(result.stdout || '').trim()) return 'local';
  if (result.status !== 0) throw new Error('could not inspect the deployed SecretStore selection');
  return String(result.stdout || '').trim() || 'local';
}

function probeSecretStore(
  serverContainer: string,
  vaultId: string,
  credentialId: string,
  secretField: 'token' | 'password' = 'token',
): SecretStoreProbe {
  const result = spawnSync(
    'docker',
    [
      'exec',
      serverContainer,
      'python',
      '-c',
      SECRET_STORE_PROBE,
      `vault/${vaultId}`,
      `${credentialId}/${secretField}`,
    ],
    { encoding: 'utf8', timeout: 40_000, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  if (result.error || result.status !== 0) {
    throw new Error('the fresh server process could not read the selected SecretStore');
  }
  const line = String(result.stdout || '')
    .split('\n')
    .map((candidate) => candidate.trim())
    .filter(Boolean)
    .reverse()
    .find((candidate) => candidate.startsWith('{'));
  if (!line) throw new Error('the fresh SecretStore process returned no JSON evidence');
  const parsed = JSON.parse(line) as Partial<SecretStoreProbe>;
  if (
    typeof parsed.store !== 'string'
    || (parsed.digest !== null && typeof parsed.digest !== 'string')
  ) {
    throw new Error('the fresh SecretStore process returned malformed evidence');
  }
  return parsed as SecretStoreProbe;
}

function digest(secret: string): string {
  return createHash('sha256').update(secret).digest('hex');
}

/** Fail if the secret's literal text appears anywhere in a payload. */
function expectNoSecret(payload: unknown, where: string): void {
  const serialized = JSON.stringify(payload ?? null);
  expect(serialized, `${where} must not echo the secret`).not.toContain(SECRET_TOKEN);
  expect(serialized, `${where} must not echo the rotated secret`).not.toContain(ROTATED_TOKEN);
  expect(serialized, `${where} must not echo the Basic secret`).not.toContain(BASIC_TOKEN);
}

test('vault credential secrets are write-only and archive keeps the record', async ({ page, request }) => {
  const platform = new PlatformApi(request);
  const serverContainer = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const selectedStore = deployedSecretStoreName(serverContainer);
  expect(['local', 'aws_kms'], 'the official image must select a built-in SecretStore').toContain(
    selectedStore,
  );
  test.info().annotations.push({ type: 'secret-store', description: selectedStore });

  let vaultId = '';
  try {
    // ── Vault creation ──────────────────────────────────────────────────────
    const vault = await platform.createVault(`__e2e_vault_${RUN_ID}`, { purpose: 'e2e' });
    vaultId = String(vault.vault_id || '');
    expect(vaultId, 'creating a vault must return its id').not.toEqual('');
    expect(vault.display_name, 'the vault should keep the name it was given').toBe(`__e2e_vault_${RUN_ID}`);
    expect(vault.archived_at, 'a fresh vault should not be archived').toBeFalsy();

    expect(
      (await platform.listVaults()).map((item) => item.vault_id),
      'the new vault should appear in the organization admin listing',
    ).toContain(vaultId);

    // A vault name is required — an unnamed store is unmanageable, and the
    // service refuses rather than inventing one.
    const unnamed = await platform.refusal('POST', '/admin/vaults', { display_name: '   ' });
    expect(unnamed.status, 'a blank vault name must be refused').toBe(400);
    expect(unnamed.message, 'the refusal should name the missing field').toContain('display_name');

    // ── Secret storage ──────────────────────────────────────────────────────
    const created = await platform.createCredential(
      vaultId,
      { type: 'static_bearer', mcp_server_url: MCP_SERVER_URL, token: SECRET_TOKEN },
      'e2e bearer',
    );
    const credentialId = String(created.credential_id || '');
    expect(credentialId, 'creating a credential must return its id').not.toEqual('');
    // The create response is the FIRST place a leak would show, and the one an
    // implementation is most likely to get wrong by echoing its own input.
    expectNoSecret(created, 'the credential create response');
    expect(created.auth?.type, 'the open half should carry the credential type').toBe('static_bearer');
    expect(
      String(created.auth?.mcp_server_url || ''),
      'the open half should carry the server this credential is for',
    ).toContain('mcp.example.invalid');
    expect(
      Object.keys(created.auth || {}),
      'the open credential document must not carry a token field at all',
    ).not.toContain('token');
    expect(
      probeSecretStore(serverContainer, vaultId, credentialId),
      'a fresh process must decrypt the created value through the deployed SecretStore',
    ).toEqual({ store: selectedStore, digest: digest(SECRET_TOKEN) });

    // ── Read-path redaction ─────────────────────────────────────────────────
    const listed = await platform.listCredentials(vaultId);
    expectNoSecret(listed, 'the credential listing');
    expect(listed.map((item) => item.credential_id), 'the credential should be listed').toContain(credentialId);
    expectNoSecret(await platform.getVault(vaultId), 'the vault document');

    // ── Secret rotation and response redaction ──────────────────────────────
    const rotated = await platform.updateCredential(vaultId, credentialId, {
      display_name: 'e2e bearer rotated',
      auth: { type: 'static_bearer', mcp_server_url: MCP_SERVER_URL, token: ROTATED_TOKEN },
    });
    expectNoSecret(rotated, 'the credential rotate response');
    expect(rotated.display_name, 'the rotate should apply the new display name').toBe('e2e bearer rotated');
    expectNoSecret(await platform.listCredentials(vaultId), 'the credential listing after a rotate');
    expect(
      probeSecretStore(serverContainer, vaultId, credentialId),
      'a fresh process must see the rotated value rather than a cached plaintext',
    ).toEqual({ store: selectedStore, digest: digest(ROTATED_TOKEN) });

    // This inert credential proves authoring and custody, not Git access.
    // Real private repository acceptance requires separately authorized access.
    await page.goto(appPath(`/manage/credentials/${vaultId}`));
    await page.getByTestId('credential-add').click();
    await page.getByTestId('credential-type').click();
    await page.getByRole('option', { name: /HTTP Basic/ }).click();
    await page.getByTestId('credential-target').fill(BASIC_REPOSITORY_URL);
    await page.getByTestId('credential-username').fill('x-access-token');
    const secretInput = page.getByTestId('credential-secret');
    await expect(secretInput).toHaveAttribute('type', 'password');
    await secretInput.fill(BASIC_TOKEN);
    const createdBasicResponse = page.waitForResponse((response) => (
      response.request().method() === 'POST'
      && new URL(response.url()).pathname === `/api/v1/admin/vaults/${vaultId}/credentials`
    ));
    await page.getByTestId('credential-save').click();
    const basicResponse = await createdBasicResponse;
    expect(basicResponse.status(), 'the real form must create the HTTP Basic credential').toBe(200);
    const basicEnvelope = await basicResponse.json();
    expectNoSecret(basicEnvelope, 'the Basic create response');
    expect(basicEnvelope.code).toBe('OK');
    const basicCredentialId = String(basicEnvelope.data?.credential_id || '');
    expect(basicCredentialId).not.toEqual('');
    const basicAuth = {
      type: 'http_basic', url: BASIC_REPOSITORY_URL, username: 'x-access-token',
    };
    expect(basicEnvelope.data?.auth, 'the response must contain only non-secret Basic metadata')
      .toEqual(basicAuth);
    await expect(page.getByRole('dialog')).toHaveCount(0);
    const basicRow = page.getByTestId('managed-credential-row').filter({ hasText: BASIC_REPOSITORY_URL });
    await expect(basicRow).toHaveCount(1);
    await expect(basicRow).toContainText('HTTP Basic');
    await page.reload();
    await expect(basicRow).toHaveCount(1);
    await expect(basicRow).toContainText('HTTP Basic');
    expectNoSecret(await page.locator('body').innerText(), 'the reloaded Vault page');
    const basicListing = await platform.listCredentials(vaultId);
    expectNoSecret(basicListing, 'the Basic credential listing');
    expect(basicListing.find((item) => item.credential_id === basicCredentialId)?.auth).toEqual(basicAuth);
    expectNoSecret(await platform.getVault(vaultId), 'the Vault containing the Basic credential');
    expect(
      probeSecretStore(serverContainer, vaultId, basicCredentialId, 'password'),
      'the Basic password must reach the selected encrypted SecretStore',
    ).toEqual({ store: selectedStore, digest: digest(BASIC_TOKEN) });
    await page.getByTestId('credential-add').click();
    await expect(page.getByTestId('credential-secret'), 'a new dialog must not restore the saved secret')
      .toHaveValue('');
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);

    // ── Capability rejection ────────────────────────────────────────────────
    // `environment_variable` needs a sandbox backend with egress credential
    // substitution. Accepting it where none is registered would produce a
    // credential that can never work — the service refuses with the reason.
    // `networking` is REQUIRED for this type and is validated before the
    // capability question is ever asked. Omitting it made this probe answer
    // INVALID_REQUEST ("auth.networking is required for type=environment_variable")
    // and the spec then judged the wrong refusal — a payload complete enough to
    // reach the check under test is part of the check.
    const unsupported = await platform.refusal('POST', `/admin/vaults/${vaultId}/credentials`, {
      auth: {
        type: 'environment_variable',
        secret_name: 'E2E_KEY',
        secret_value: 'x',
        networking: { type: 'limited', allowed_hosts: ['api.example.invalid'] },
      },
    });
    if (unsupported.status === 200) {
      // A deployment that DOES register such a backend is entitled to accept it;
      // the property under test is that the answer is never a silent no-op.
      test.info().annotations.push({
        type: 'note',
        description:
          'this deployment supports egress credential substitution, so the environment_variable ' +
          'credential was accepted rather than capability-refused',
      });
    } else {
      expect(unsupported.status, 'an unsupported credential type should be refused as a client error').toBe(400);
      expect(
        unsupported.code,
        'the refusal should name the missing capability rather than a generic error',
      ).toBe('VAULT_ENV_CREDENTIAL_UNSUPPORTED');
    }

    const badType = await platform.refusal('POST', `/admin/vaults/${vaultId}/credentials`, {
      auth: { type: 'not_a_real_type', token: 'x' },
    });
    expect(badType.status, 'an unknown credential type must be refused').toBe(400);
    expect(badType.message, 'the refusal should list the types that exist').toContain('auth.type');

    // ── Credential archival ─────────────────────────────────────────────────
    const archivedCredential = await platform.archiveCredential(vaultId, credentialId);
    expect(archivedCredential.credential_id, 'archiving should keep the credential id').toBe(credentialId);
    expect(
      String(archivedCredential.archived_at || ''),
      'archiving should stamp archived_at',
    ).not.toEqual('');
    expect(
      archivedCredential.auth?.type,
      'an archived credential should keep its open metadata for the audit trail',
    ).toBe('static_bearer');
    expectNoSecret(archivedCredential, 'the credential archive response');
    expect(
      probeSecretStore(serverContainer, vaultId, credentialId),
      'archiving must purge the encrypted payload from the selected SecretStore',
    ).toEqual({ store: selectedStore, digest: null });

    const archivedBasic = await platform.archiveCredential(vaultId, basicCredentialId);
    expect(archivedBasic.credential_id).toBe(basicCredentialId);
    expect(archivedBasic.archived_at).toBeTruthy();
    expect(archivedBasic.auth).toEqual(basicAuth);
    expectNoSecret(archivedBasic, 'the Basic archive response');
    expect(
      probeSecretStore(serverContainer, vaultId, basicCredentialId, 'password'),
      'archiving Basic authentication must also remove its encrypted password',
    ).toEqual({ store: selectedStore, digest: null });

    // ── Vault archival ──────────────────────────────────────────────────────
    const archivedVault = await platform.archiveVault(vaultId);
    expect(String(archivedVault.archived_at || ''), 'archiving should stamp the vault').not.toEqual('');
    expect(archivedVault.vault_id, 'archiving should keep the vault id').toBe(vaultId);

    const intoArchive = await platform.refusal('POST', `/admin/vaults/${vaultId}/credentials`, {
      auth: { type: 'static_bearer', mcp_server_url: MCP_SERVER_URL, token: 'late' },
    });
    expect(intoArchive.status, 'an archived vault must refuse new credentials').toBe(400);
    expect(intoArchive.message, 'the refusal should say the vault is archived').toContain('archived');

    // ── Deletion ────────────────────────────────────────────────────────────
    expect(
      await platform.deleteCredential(vaultId, credentialId),
      'deleting a credential should answer 204',
    ).toBe(204);
    expect(await platform.deleteVault(vaultId), 'deleting a vault should answer 204').toBe(204);
    const gone = await platform.refusal('GET', `/admin/vaults/${vaultId}`);
    expect(gone.status, 'a deleted vault must not resolve').toBe(404);
    vaultId = '';
  } finally {
    if (vaultId) await platform.deleteVault(vaultId).catch(() => {});
  }
});
