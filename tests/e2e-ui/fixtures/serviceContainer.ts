import { spawnSync } from 'node:child_process';

import { test } from '@playwright/test';

export interface ServiceContainerHandle {
  service: string;
  envVar: string;
  setup?: string;
}

export interface DockerResult {
  status: number | null;
  stdout: string;
  stderr: string;
  error?: Error;
}

export type DockerRunner = (args: string[], timeoutMs?: number) => DockerResult;

interface ResolveOptions {
  env?: NodeJS.ProcessEnv;
  runDocker?: DockerRunner;
}

export type ServiceContainerResolution =
  | { kind: 'ready'; container: string; source: 'configured' | 'compose' }
  | { kind: 'unavailable'; reason: string };

export const POSTGRES_CONTAINER_HANDLE: ServiceContainerHandle = {
  service: 'postgres',
  envVar: 'ASTRABOX_E2E_POSTGRES_CONTAINER',
  setup:
    'From tests/e2e-ui, rerun the selected spec as ' +
    '`ASTRABOX_E2E_POSTGRES_CONTAINER=<running-postgres-container> ' +
    'npx playwright test <spec-file>`; replace <spec-file> with the spec being run.',
};

export const POSTGRES_SUPERUSER_ENV_VAR = 'ASTRABOX_E2E_POSTGRES_SUPERUSER';

export const SERVER_CONTAINER_HANDLE: ServiceContainerHandle = {
  service: 'server',
  envVar: 'ASTRABOX_E2E_SERVER_CONTAINER',
  setup:
    'From tests/e2e-ui, rerun the selected spec as ' +
    '`ASTRABOX_E2E_SERVER_CONTAINER=<running-server-container> ' +
    'npx playwright test <spec-file>`; replace <spec-file> with the spec being run.',
};

function docker(args: string[], timeoutMs = 30_000): DockerResult {
  const result = spawnSync('docker', args, {
    encoding: 'utf8',
    timeout: timeoutMs,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  return {
    status: result.status,
    stdout: result.stdout || '',
    stderr: result.stderr || '',
    error: result.error,
  };
}

function diagnostic(result: DockerResult): string {
  return String(result.stderr || result.error?.message || `exit status ${result.status}`).trim();
}

function setupInstructions(handle: ServiceContainerHandle): string {
  if (handle.setup) return handle.setup;
  return (
    `Set ${handle.envVar}=<running-${handle.service}-container> on the test command. ` +
    `The value must be the exact name or ID of a running container visible to this host's Docker daemon.`
  );
}

/**
 * Resolve one deployment service without assuming that the deployment itself is Compose.
 *
 * An explicit handle is authoritative and therefore validated as a running Docker
 * container. With no handle, a unique running Compose service remains a local-dev
 * convenience; inability to discover one is an unmet harness prerequisite, not a
 * product failure.
 */
export function resolveServiceContainer(
  handle: ServiceContainerHandle,
  options: ResolveOptions = {},
): ServiceContainerResolution {
  const env = options.env ?? process.env;
  const runDocker = options.runDocker ?? docker;
  const configured = String(env[handle.envVar] || '').trim();

  if (configured) {
    const inspected = runDocker([
      'inspect', '--format', '{{.State.Running}}', configured,
    ]);
    if (inspected.error || inspected.status !== 0 || inspected.stdout.trim() !== 'true') {
      throw new Error(
        `${handle.envVar}=${JSON.stringify(configured)} does not name a running Docker container ` +
          `for the ${handle.service} service: ${diagnostic(inspected)}. ` +
          setupInstructions(handle),
      );
    }
    return { kind: 'ready', container: configured, source: 'configured' };
  }

  const discovered = runDocker([
    'ps',
    '--filter',
    `label=com.docker.compose.service=${handle.service}`,
    '--format',
    '{{.Names}}',
  ]);
  if (discovered.error || discovered.status !== 0) {
    return {
      kind: 'unavailable',
      reason:
        `${handle.service} service is unavailable to this E2E harness: ${handle.envVar} is unset ` +
        `and Compose discovery failed (${diagnostic(discovered)}). ${setupInstructions(handle)}`,
    };
  }

  const matches = discovered.stdout.split('\n').map((line) => line.trim()).filter(Boolean);
  if (matches.length !== 1) {
    return {
      kind: 'unavailable',
      reason:
        `${handle.service} service is unavailable to this E2E harness: ${handle.envVar} is unset ` +
        `and Compose discovery found ${matches.length} running containers (${matches.join(', ') || 'none'}). ` +
        setupInstructions(handle),
    };
  }
  return { kind: 'ready', container: matches[0], source: 'compose' };
}

/** Resolve the administrator login from the selected PostgreSQL deployment. */
export function resolvePostgresSuperuser(
  container: string,
  options: ResolveOptions = {},
): string {
  const env = options.env ?? process.env;
  const configured = String(env[POSTGRES_SUPERUSER_ENV_VAR] || '').trim();
  if (configured) return configured;

  const runDocker = options.runDocker ?? docker;
  const discovered = runDocker(['exec', container, 'printenv', 'POSTGRES_USER']);
  const setup =
    `Set ${POSTGRES_SUPERUSER_ENV_VAR}=<postgres-superuser> on the test command, ` +
    `or expose POSTGRES_USER in PostgreSQL container ${JSON.stringify(container)}.`;
  if (discovered.error || discovered.status !== 0) {
    throw new Error(
      `PostgreSQL superuser is unavailable: ${POSTGRES_SUPERUSER_ENV_VAR} is unset and ` +
        `\`docker exec ${container} printenv POSTGRES_USER\` failed (${diagnostic(discovered)}). ${setup}`,
    );
  }

  const user = discovered.stdout.trim();
  if (!user) {
    throw new Error(
      `PostgreSQL superuser is unavailable: ${POSTGRES_SUPERUSER_ENV_VAR} is unset and ` +
        `\`docker exec ${container} printenv POSTGRES_USER\` returned an empty value. ${setup}`,
    );
  }
  return user;
}

/** Resolve a service or mark the current Playwright test skipped with a visible reason. */
export function requireServiceContainer(handle: ServiceContainerHandle): string {
  const resolution = resolveServiceContainer(handle);
  if (resolution.kind === 'ready') return resolution.container;
  console.warn(`E2E prerequisite not met; skipping: ${resolution.reason}`);
  test.skip(true, resolution.reason);
  throw new Error(`Playwright did not stop the skipped test: ${resolution.reason}`);
}
