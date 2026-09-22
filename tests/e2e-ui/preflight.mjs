#!/usr/bin/env node
/**
 * Check the prerequisites for a browser round before it claims a sandbox.
 *
 * The checks distinguish missing Playwright, an unreachable console, absent
 * OIDC configuration, and a mismatched Postgres oracle from product failures.
 * They run in dependency order and report a bracketed code, observed value,
 * and repair command.
 *
 * `--plan` exercises local prerequisites without a deployment.
 */
import { constants as fsConstants } from 'node:fs';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

class PreflightError extends Error {
  constructor(code, detail, repair) {
    super(`[${code}] ${detail}${repair ? `. ${repair}` : ''}`);
    this.name = 'PreflightError';
    this.code = code;
  }
}

export function assertPlaywrightInstalled(e2eDir = here) {
  const nodeModules = path.join(e2eDir, 'node_modules');
  let realNodeModules;
  try {
    realNodeModules = fs.realpathSync(nodeModules);
  } catch (error) {
    throw new PreflightError(
      'E2E_NODE_MODULES_UNAVAILABLE',
      `node_modules=${nodeModules} error=${error.message}`,
      `Run npm --prefix ${e2eDir} install before the round`,
    );
  }
  const binary = path.join(realNodeModules, '.bin', 'playwright');
  const manifest = path.join(realNodeModules, '@playwright', 'test', 'package.json');
  try {
    fs.accessSync(binary, fsConstants.X_OK);
    fs.accessSync(manifest, fsConstants.R_OK);
  } catch (error) {
    throw new PreflightError(
      'E2E_PLAYWRIGHT_UNAVAILABLE',
      `node_modules=${realNodeModules} error=${error.message}`,
      `Run npm --prefix ${e2eDir} install before the round`,
    );
  }
  return { nodeModules: realNodeModules, binary };
}

export function assertRequiredEnv(env = process.env) {
  // Global setup signs in before any spec runs, so a value it needs is worth
  // naming here rather than as a crash inside a launched browser.
  const required = [
    'ASTRABOX_E2E_CONSOLE_URL',
    'ASTRABOX_E2E_OIDC_ISSUER',
    'ASTRABOX_E2E_OIDC_USERNAME',
    'ASTRABOX_E2E_OIDC_PASSWORD_FILE',
    'ASTRABOX_E2E_STORAGE_STATE',
  ];
  const missing = required.filter((name) => !String(env[name] || '').trim());
  if (missing.length) {
    throw new PreflightError(
      'E2E_ENV_INCOMPLETE',
      `missing=${missing.join(',')}`,
      'Export them, or source the deployment env file the testbed skill writes',
    );
  }
  const passwordFile = String(env.ASTRABOX_E2E_OIDC_PASSWORD_FILE).trim();
  let stat;
  try {
    stat = fs.statSync(passwordFile);
  } catch (error) {
    throw new PreflightError(
      'E2E_OIDC_SECRET_UNREADABLE',
      `file=${passwordFile} error=${error.message}`,
    );
  }
  if (!stat.isFile() || fs.readFileSync(passwordFile, 'utf8').trim() === '') {
    throw new PreflightError(
      'E2E_OIDC_SECRET_UNREADABLE',
      `file=${passwordFile} is not a regular file with a password in it`,
    );
  }
}

export async function assertConsoleReachable(env = process.env, fetchImpl = fetch) {
  const consoleUrl = String(env.ASTRABOX_E2E_CONSOLE_URL || '').replace(/\/$/, '');
  const healthUrl = `${consoleUrl}/healthz`;
  let response;
  try {
    response = await fetchImpl(healthUrl, {
      signal: AbortSignal.timeout(10_000),
      redirect: 'manual',
    });
  } catch (error) {
    throw new PreflightError(
      'E2E_CONSOLE_UNREACHABLE',
      `url=${healthUrl} error=${error.message}`,
      'Check the deployment is up and the tunnel or ingress reaches it',
    );
  }
  if (!response.ok) {
    throw new PreflightError(
      'E2E_CONSOLE_UNHEALTHY',
      `url=${healthUrl} status=${response.status}`,
      'The server answered but is not healthy; read its container logs',
    );
  }
}

export function assertOracleReachable(env = process.env, runner = spawnSync) {
  // The oracle reads the same database the server writes. A container that is
  // absent — or present under a different name than the server's — makes every
  // oracle assertion in the round meaningless rather than failing.
  const container = String(env.ASTRABOX_E2E_POSTGRES_CONTAINER || 'astrabox-postgres').trim();
  const database = String(env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox').trim();
  const user = String(env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox').trim();
  const result = runner(
    'docker',
    ['exec', container, 'psql', '-X', '-A', '-t', '-q', '-U', user, '-d', database, '-c', 'select 1'],
    { encoding: 'utf8', timeout: 15_000 },
  );
  if (result.error) {
    throw new PreflightError(
      'E2E_ORACLE_UNAVAILABLE',
      `container=${container} error=${result.error.message}`,
      'Start the deployment, or set ASTRABOX_E2E_POSTGRES_CONTAINER to the database the server uses',
    );
  }
  if ((result.status ?? 1) !== 0) {
    const detail = String(result.stderr || result.stdout || '').trim().split(/\r?\n/).at(-1) || 'psql failed';
    throw new PreflightError(
      'E2E_ORACLE_UNAVAILABLE',
      `container=${container} db=${database} user=${user} detail=${detail}`,
      'Set ASTRABOX_E2E_POSTGRES_CONTAINER/_DB/_USER to the database the server uses',
    );
  }
}

export async function runE2ePreflight({
  e2eDir = here,
  env = process.env,
  fetchImpl = fetch,
  runner = spawnSync,
} = {}) {
  assertPlaywrightInstalled(e2eDir);
  assertRequiredEnv(env);
  await assertConsoleReachable(env, fetchImpl);
  assertOracleReachable(env, runner);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  try {
    await runE2ePreflight();
    console.log('e2e-ui preflight OK');
  } catch (error) {
    console.error(String(error.message || error));
    process.exit(1);
  }
}
