/**
 * PostgreSQL is the deployment's source of truth, not merely a configured URL.
 *
 * This test crosses both sides of the persistence boundary: it creates and
 * reads a record through the public API, then asks the real PostgreSQL service
 * for that same document. The direct query catches a server that reports the
 * PostgreSQL setting while still writing to SQLite or an in-memory fallback.
 */
import { expect, test } from '@playwright/test';

import { documentsByField } from '../fixtures/dbOracle';
import { PlatformApi } from '../fixtures/platformApi';

test('API writes are durable in the real PostgreSQL document store', async ({ request }) => {
  const platform = new PlatformApi(request);
  const overview = await platform.systemOverview();
  expect(
    overview.persistence_backend,
    'the operator overview must report the active store instead of hiding a SQLite fallback',
  ).toBe('postgresql');

  const marker = `${Date.now()}_${Math.random().toString(16).slice(2)}`;
  let vaultId = '';
  try {
    const created = await platform.createVault(`__e2e_postgresql_${marker}`, {
      purpose: 'postgresql-persistence-e2e',
    });
    vaultId = created.vault_id;
    expect(vaultId).toBeTruthy();

    await expect
      .poll(() => documentsByField('vaults', '$.vault_id', vaultId).length, {
        timeout: 15_000,
        message: 'the API-created vault must exist in PostgreSQL JSONB storage',
      })
      .toBe(1);

    const readBack = await platform.getVault(vaultId);
    expect(readBack.display_name).toBe(`__e2e_postgresql_${marker}`);
  } finally {
    if (vaultId) {
      await platform.deleteVault(vaultId).catch(() => undefined);
      await expect
        .poll(() => documentsByField('vaults', '$.vault_id', vaultId).length, { timeout: 15_000 })
        .toBe(0);
    }
  }
});
