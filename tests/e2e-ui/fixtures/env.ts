import fs from 'node:fs';
import path from 'node:path';

export const repoRoot = path.resolve(__dirname, '../../..');

// The console is served at the root. A deployment behind a path-prefixing
// reverse proxy sets ASTRABOX_E2E_APP_PREFIX.
export const appPrefix = process.env.ASTRABOX_E2E_APP_PREFIX || '';

export function appPath(route = '/'): string {
  const normalized = route.startsWith('/') ? route : `/${route}`;
  if (normalized === '/') {
    return `${appPrefix}/`;
  }
  return `${appPrefix}${normalized}`;
}

export function apiPath(route: string): string {
  const normalized = route.startsWith('/') ? route : `/${route}`;
  return `${appPrefix}/api/v1${normalized}`;
}

export function parseTimeoutEnv(name: string, fallbackMs: number): number {
  const raw = process.env[name];
  if (!raw) {
    return fallbackMs;
  }
  const value = Number.parseInt(raw, 10);
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error(`${name} must be a positive integer in milliseconds`);
  }
  return value;
}

/** Absolute e2e base URL (for Node `fetch` outside the Playwright request context). */
export function absoluteBaseUrl(): string {
  const base = process.env.ASTRABOX_E2E_BASE_URL || 'http://127.0.0.1:8000';
  return base.replace(/\/$/, '');
}

/** Absolute API URL for Node `fetch` (apiPath resolves relatively only inside Playwright contexts). */
export function absoluteApiUrl(route: string): string {
  return `${absoluteBaseUrl()}${apiPath(route)}`;
}

/** OIDC bearer for the few Node fetches outside Playwright's cookie context. */
export function oidcAccessHeaders(): Record<string, string> {
  const tokenPath = (process.env.ASTRABOX_E2E_AUTH_TOKEN_FILE || '').trim();
  if (!path.isAbsolute(tokenPath)) {
    throw new Error('ASTRABOX_E2E_AUTH_TOKEN_FILE must be an absolute path');
  }
  const stat = fs.lstatSync(tokenPath);
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new Error(`OIDC token is not a regular file: ${tokenPath}`);
  }
  const token = fs.readFileSync(tokenPath, 'utf8').trim();
  if (!token) throw new Error(`OIDC token file is empty: ${tokenPath}`);
  return { Authorization: `Bearer ${token}` };
}

/**
 * Refuse to audit a page whose API is not this product's.
 *
 * The audit drives a frontend, so it is easy to check that the FRONTEND is the
 * tree's — and easy to never check what is behind it. A dev server started
 * without ASTRABOX_DEV_PROXY_TARGET proxies to its default, which on a shared
 * box may be anything at all: this cost two commits' worth of results, measured
 * against a mock whose process had outlived its own deleted file by nine days.
 * Every walk still passed. What it reported about a missing record was the
 * mock's answer, and the control run that "reproduced" it was pointed at the
 * same mock — two consistent wrong answers look exactly like one right one.
 *
 * The probe is a contract the product states and a stand-in will not: an
 * unknown record is a 404, non-disclosing, asserted in
 * `tests/assistant_owner_policy_test.py`. A 200 here means the thing behind
 * this page answers for records it does not have.
 */
export async function refuseIfNotTheDeployment(
  fetchJson: (url: string, headers: Record<string, string>) => Promise<{ status: number }>,
): Promise<void> {
  const url = absoluteApiUrl('/assistants/__audit_probe_no_such_record__');
  const headers = (process.env.ASTRABOX_E2E_AUTH_TOKEN_FILE || '').trim()
    ? oidcAccessHeaders()
    : {};
  const { status } = await fetchJson(url, headers);
  if (status === 404) {
    return;
  }
  throw new Error(
    `${url} answered ${status}, and a record that cannot exist must answer 404.\n` +
      `Whatever is behind ${absoluteBaseUrl()} is not this deployment. Check the\n` +
      `dev server's ASTRABOX_DEV_PROXY_TARGET first — an unset one silently\n` +
      `proxies to the default port, which is how a nine-day-old mock once got\n` +
      `audited instead of the product.`,
  );
}
