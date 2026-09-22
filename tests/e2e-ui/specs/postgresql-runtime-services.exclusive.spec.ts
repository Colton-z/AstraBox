/**
 * The production deployment gives AstraBox and Casdoor isolated databases in
 * the product PostgreSQL service. The external LiteLLM control plane owns a
 * second PostgreSQL service, so model/MCP/Plugin state is not coupled to the
 * product database lifecycle.
 *
 * This is intentionally a real-service test. It checks the deployed Casdoor
 * OIDC discovery endpoint and queries PostgreSQL for the schemas and privilege
 * matrix the three services actually created. The harness does not own either
 * service's lifecycle.
 */
import { execFileSync, spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

import { expect, test } from '@playwright/test';

import { postgresTextRows } from '../fixtures/dbOracle';
import {
  POSTGRES_CONTAINER_HANDLE,
  requireServiceContainer,
  resolvePostgresSuperuser,
  type ServiceContainerHandle,
} from '../fixtures/serviceContainer';

function requiredEnvironment(name: string): string {
  const value = String(process.env[name] || '').trim();
  if (!value) throw new Error(`${name} is required for the production identity oracle`);
  return value;
}

// Read where it is used, not at module scope. Playwright loads every file in
// the lane to enumerate its tests, so a throw out here is not this spec
// failing — it is the whole lane collecting nothing, and the round reports a
// count mismatch rather than the missing variable.
function casdoorOrigin(): string {
  return requiredEnvironment('ASTRABOX_E2E_OIDC_ISSUER').replace(/\/+$/, '');
}
const databaseSecretDir = String(
  process.env.ASTRABOX_E2E_DATABASE_SECRET_DIR || '',
).trim();

const CASDOOR_CONTAINER_HANDLE: ServiceContainerHandle = {
  service: 'casdoor',
  envVar: 'ASTRABOX_E2E_CASDOOR_CONTAINER',
  setup: [
    'Start Casdoor as part of the deployment before this spec; the harness will not start or stop it.',
    'Then, from tests/e2e-ui, run',
    '`ASTRABOX_E2E_CASDOOR_CONTAINER=<running-casdoor-container>',
    'ASTRABOX_E2E_OIDC_ISSUER=https://<casdoor-origin>',
    'npx playwright test specs/postgresql-runtime-services.exclusive.spec.ts`.',
    'The container must be visible to this host\'s Docker daemon.',
  ].join(' '),
};

const LITELLM_CONTAINER_HANDLE: ServiceContainerHandle = {
  service: 'external LiteLLM',
  envVar: 'ASTRABOX_E2E_LITELLM_CONTAINER',
};

const LITELLM_POSTGRES_CONTAINER_HANDLE: ServiceContainerHandle = {
  service: 'external LiteLLM PostgreSQL',
  envVar: 'ASTRABOX_E2E_LITELLM_POSTGRES_CONTAINER',
};

function docker(args: string[], timeout = 120_000): string {
  return execFileSync('docker', args, {
    encoding: 'utf8',
    timeout,
    stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
}

function dockerOutput(args: string[], timeout = 120_000): string {
  const result = spawnSync('docker', args, {
    encoding: 'utf8',
    timeout,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`docker ${args.join(' ')} failed (${result.status})`);
  }
  return `${result.stdout || ''}\n${result.stderr || ''}`.trim();
}

function databaseIdentity(value: string): URL {
  try {
    return new URL(value);
  } catch {
    // A DATABASE_URL may contain a password. Never attach the input or the
    // native URL exception to Playwright's retained error report.
    throw new Error('external LiteLLM DATABASE_URL is not a valid absolute URL');
  }
}

test('AstraBox, LiteLLM, and Casdoor use isolated production PostgreSQL databases', async ({
  request,
}) => {
  const casdoorContainer = requireServiceContainer(CASDOOR_CONTAINER_HANDLE);
  const postgresContainer = requireServiceContainer(POSTGRES_CONTAINER_HANDLE);
  const litellmContainer = requireServiceContainer(LITELLM_CONTAINER_HANDLE);
  const litellmPostgresContainer = requireServiceContainer(
    LITELLM_POSTGRES_CONTAINER_HANDLE,
  );
  const postgresSuperuser = resolvePostgresSuperuser(postgresContainer);
  const litellmPostgresSuperuser = resolvePostgresSuperuser(litellmPostgresContainer);

  expect(litellmContainer).not.toBe(litellmPostgresContainer);
  expect(litellmPostgresContainer).not.toBe(postgresContainer);

  // Casdoor applies init data by replacing its first server process. A Compose
  // health probe can briefly pass before that handoff closes the listener, so
  // the live oracle still waits for discovery at the public issuer.
  const casdoorUrl = casdoorOrigin();
  await expect
    .poll(
      async () => {
        try {
          const discovery = await request.get(
            `${casdoorUrl}/.well-known/openid-configuration`,
          );
          if (!discovery.ok()) return '';
          const oidc = await discovery.json();
          return String(oidc.issuer || '');
        } catch {
          return '';
        }
      },
      { timeout: 30_000, message: 'Casdoor OIDC discovery must settle on its public issuer' },
    )
    .toBe(casdoorUrl);

  if (databaseSecretDir) {
    const casdoorSecret = readFileSync(`${databaseSecretDir}/casdoor_password`, 'utf8').trim();
    expect(
      /^[0-9a-f]{64}$/.test(casdoorSecret),
      'Casdoor must receive a generated 64-character database password',
    ).toBe(true);
    const inspect = docker(['inspect', casdoorContainer]);
    const logs = dockerOutput(['logs', '--tail', '1000', casdoorContainer]);
    expect(
      inspect.includes(casdoorSecret) || logs.includes(casdoorSecret),
      'Casdoor database value must stay out of Docker metadata and logs',
    ).toBe(false);
    expect(inspect).toContain('CASDOOR_DATABASE_PASSWORD_FILE=/run/secrets/casdoor_database_password');
  }

  expect(
    postgresTextRows(
      postgresContainer,
      'postgres',
      postgresSuperuser,
      "SELECT rolname || '|' || rolsuper || '|' || rolcreatedb || '|' || rolcreaterole "
        + "FROM pg_roles WHERE rolname IN ('astrabox','casdoor') ORDER BY rolname;",
    ),
  ).toEqual([
    'astrabox|false|false|false',
    'casdoor|false|false|false',
  ]);

  expect(
    postgresTextRows(
      postgresContainer,
      'postgres',
      postgresSuperuser,
      "SELECT r.rolname || '->' || d.datname || '|' "
        + "|| has_database_privilege(r.rolname, d.datname, 'CONNECT') "
        + 'FROM pg_roles r CROSS JOIN pg_database d '
        + "WHERE r.rolname IN ('astrabox','casdoor') "
        + "AND d.datname IN ('astrabox','casdoor') "
        + 'ORDER BY r.rolname, d.datname;',
    ),
  ).toEqual([
    'astrabox->astrabox|true',
    'astrabox->casdoor|false',
    'casdoor->astrabox|false',
    'casdoor->casdoor|true',
  ]);

  expect(
    Number(
      postgresTextRows(
        postgresContainer,
        'astrabox',
        'astrabox',
        "SELECT to_regclass('public.astrabox_documents') IS NOT NULL;",
      )[0] === 't',
    ),
  ).toBe(1);
  expect(
    Number(postgresTextRows(
      postgresContainer,
      'casdoor',
      'casdoor',
      'SELECT count(*) FROM "user";',
    )[0]),
  ).toBeGreaterThan(0);

  expect(
    postgresTextRows(
      litellmPostgresContainer,
      'postgres',
      litellmPostgresSuperuser,
      "SELECT rolname || '|' || rolsuper || '|' || rolcreatedb || '|' || rolcreaterole "
        + "FROM pg_roles WHERE rolname='litellm';",
    ),
  ).toEqual(['litellm|false|false|false']);
  expect(
    postgresTextRows(
      litellmPostgresContainer,
      'litellm',
      'litellm',
      'SELECT table_name FROM information_schema.tables '
        + "WHERE table_schema='public' AND table_name IN "
        + "('LiteLLM_ClaudeCodePluginTable','LiteLLM_MCPServerTable',"
        + "'LiteLLM_ModelTable','LiteLLM_SkillsTable','LiteLLM_VerificationToken') "
        + 'ORDER BY table_name;',
    ),
  ).toEqual([
    'LiteLLM_ClaudeCodePluginTable',
    'LiteLLM_MCPServerTable',
    'LiteLLM_ModelTable',
    'LiteLLM_SkillsTable',
    'LiteLLM_VerificationToken',
  ]);

  const litellmInspect = JSON.parse(docker(['inspect', litellmContainer]))[0] as {
    Config?: { Env?: string[] };
    NetworkSettings?: { Ports?: Record<string, Array<{ HostIp?: string; HostPort?: string }>> };
  };
  const databaseUrl = litellmInspect.Config?.Env?.find((value) =>
    value.startsWith('DATABASE_URL='),
  )?.slice('DATABASE_URL='.length);
  if (!databaseUrl) {
    throw new Error('external LiteLLM must declare its PostgreSQL connection');
  }
  const parsedDatabaseUrl = databaseIdentity(databaseUrl);
  expect(parsedDatabaseUrl.username).toBe('litellm');
  expect(parsedDatabaseUrl.pathname).toBe('/litellm');
  expect(['host.docker.internal', '127.0.0.1', 'localhost']).toContain(
    parsedDatabaseUrl.hostname,
  );
  const databaseInspect = JSON.parse(docker(['inspect', litellmPostgresContainer]))[0] as {
    Config?: { Image?: string };
    NetworkSettings?: { Ports?: Record<string, Array<{ HostIp?: string; HostPort?: string }>> };
  };
  expect(databaseInspect.Config?.Image).toMatch(/^postgres:17/);
  expect(databaseInspect.NetworkSettings?.Ports?.['5432/tcp']).toContainEqual(
    expect.objectContaining({ HostIp: '127.0.0.1', HostPort: parsedDatabaseUrl.port }),
  );
});
