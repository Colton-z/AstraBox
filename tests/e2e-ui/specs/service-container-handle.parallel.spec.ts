import { expect, test } from '@playwright/test';

import { postgresTextRows } from '../fixtures/dbOracle';
import {
  POSTGRES_SUPERUSER_ENV_VAR,
  resolvePostgresSuperuser,
  resolveServiceContainer,
  type ServiceContainerHandle,
} from '../fixtures/serviceContainer';

const HANDLE: ServiceContainerHandle = {
  service: 'casdoor',
  envVar: 'ASTRABOX_E2E_CASDOOR_CONTAINER',
  setup:
    'Run ASTRABOX_E2E_CASDOOR_CONTAINER=<running-casdoor-container> ' +
    'npx playwright test specs/postgresql-runtime-services.exclusive.spec.ts.',
};

test('an explicit deployment handle is validated without Compose discovery', () => {
  const calls: string[][] = [];
  const resolution = resolveServiceContainer(HANDLE, {
    env: { ASTRABOX_E2E_CASDOOR_CONTAINER: 'bare-casdoor' },
    runDocker: (args) => {
      calls.push(args);
      return { status: 0, stdout: 'true\n', stderr: '' };
    },
  });

  expect(resolution).toEqual({
    kind: 'ready',
    container: 'bare-casdoor',
    source: 'configured',
  });
  expect(calls).toEqual([
    ['inspect', '--format', '{{.State.Running}}', 'bare-casdoor'],
  ]);
});

test('a unique running Compose service remains an opt-in discovery convenience', () => {
  const resolution = resolveServiceContainer(HANDLE, {
    env: {},
    runDocker: () => ({ status: 0, stdout: 'local-casdoor-1\n', stderr: '' }),
  });

  expect(resolution).toEqual({
    kind: 'ready',
    container: 'local-casdoor-1',
    source: 'compose',
  });
});

test('an unavailable service produces an actionable skip reason', () => {
  const resolution = resolveServiceContainer(HANDLE, {
    env: {},
    runDocker: () => ({ status: 0, stdout: '', stderr: '' }),
  });

  expect(resolution.kind).toBe('unavailable');
  if (resolution.kind === 'unavailable') {
    expect(resolution.reason).toContain('Compose discovery found 0 running containers (none)');
    expect(resolution.reason).toContain(
      'ASTRABOX_E2E_CASDOOR_CONTAINER=<running-casdoor-container>',
    );
    expect(resolution.reason).toContain(
      'npx playwright test specs/postgresql-runtime-services.exclusive.spec.ts',
    );
  }
});

test('a configured but nonexistent handle fails instead of skipping', () => {
  expect(() => resolveServiceContainer(HANDLE, {
    env: { ASTRABOX_E2E_CASDOOR_CONTAINER: 'missing-casdoor' },
    runDocker: () => ({
      status: 1,
      stdout: '',
      stderr: 'Error: No such object: missing-casdoor',
    }),
  })).toThrow(
    'ASTRABOX_E2E_CASDOOR_CONTAINER="missing-casdoor" does not name a running Docker container',
  );
});

test('an explicit PostgreSQL superuser is authoritative', () => {
  const user = resolvePostgresSuperuser('bare-postgres', {
    env: { [POSTGRES_SUPERUSER_ENV_VAR]: 'deployment-admin' },
    runDocker: () => {
      throw new Error('an explicit superuser must not inspect container environment');
    },
  });

  expect(user).toBe('deployment-admin');
});

test('an implicit PostgreSQL superuser is read with printenv from the selected container', () => {
  const calls: string[][] = [];
  const user = resolvePostgresSuperuser('bare-postgres', {
    env: {},
    runDocker: (args) => {
      calls.push(args);
      if (args[2] === 'env') {
        return {
          status: 127,
          stdout: '',
          stderr: "env: can't execute 'POSTGRES_USER': No such file or directory",
        };
      }
      return { status: 0, stdout: 'astrabox\n', stderr: '' };
    },
  });

  expect(user).toBe('astrabox');
  expect(calls).toEqual([
    ['exec', 'bare-postgres', 'printenv', 'POSTGRES_USER'],
  ]);
});

test('an unreadable PostgreSQL superuser fails with setup instructions', () => {
  expect(() => resolvePostgresSuperuser('bare-postgres', {
    env: {},
    runDocker: () => ({
      status: 1,
      stdout: '',
      stderr: 'Error response from daemon: container is not running',
    }),
  })).toThrow(
    'ASTRABOX_E2E_POSTGRES_SUPERUSER is unset and ' +
      '`docker exec bare-postgres printenv POSTGRES_USER` failed ' +
      '(Error response from daemon: container is not running). ' +
      'Set ASTRABOX_E2E_POSTGRES_SUPERUSER=<postgres-superuser>',
  );
});

test('an empty PostgreSQL superuser fails instead of guessing a role', () => {
  expect(() => resolvePostgresSuperuser('bare-postgres', {
    env: {},
    runDocker: () => ({ status: 0, stdout: '\n', stderr: '' }),
  })).toThrow('`docker exec bare-postgres printenv POSTGRES_USER` returned an empty value');
});

test('a wrong configured PostgreSQL superuser fails with the role and server diagnostic', () => {
  const calls: string[][] = [];
  const runDocker = (args: string[]) => {
    calls.push(args);
    return {
      status: 2,
      stdout: '',
      stderr: 'psql: error: role "not-a-role" does not exist',
    };
  };
  const user = resolvePostgresSuperuser('bare-postgres', {
    env: { [POSTGRES_SUPERUSER_ENV_VAR]: 'not-a-role' },
    runDocker,
  });

  expect(() => postgresTextRows(
    'bare-postgres',
    'postgres',
    user,
    'SELECT 1;',
    { runDocker },
  )).toThrow(
    'PostgreSQL probe failed in container "bare-postgres" on database "postgres" ' +
      'as role "not-a-role": exit 2: psql: error: role "not-a-role" does not exist',
  );
  expect(calls).toEqual([[
    'exec', 'bare-postgres', 'psql', '-X', '-A', '-t', '-q',
    '-v', 'ON_ERROR_STOP=1', '-U', 'not-a-role', '-d', 'postgres', '-c', 'SELECT 1;',
  ]]);
});
