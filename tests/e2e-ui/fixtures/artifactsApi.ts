import { createHash, randomUUID } from 'node:crypto';
import { expect, type APIRequestContext, type Page } from '@playwright/test';

import { apiPath } from './env';
import { postgresRows, sqlLiteral } from './dbOracle';
import { nativeCalls, nativeRows } from './nativeChildLifecycle';

// These shapes follow docs/maintainers/artifacts.md §§3, 5.3 and 6. Keep
// artifact HTTP paths and persistence reads here so backend integration has
// one fixture boundary; do not import the console's adapter as an oracle.
export type Manifest = Record<string, Record<string, unknown> | boolean>;
export interface ArtifactRecord {
  artifact_id: string;
  title: string;
  kind: string;
  current_version: number;
  manifest: Manifest;
  origin: { session_kind: string; session_id?: string; agent_id?: string; assistant_id?: string };
}
export interface ArtifactVersion {
  version: number;
  title: string;
  kind: string;
  content: string;
  manifest: Manifest;
}
export type ArtifactVersionSummary = Omit<ArtifactVersion, 'content'>;
export interface ArtifactShare {
  enabled: boolean;
  token: string;
  access: 'view' | 'interact';
  audience: 'anyone' | 'signed_in';
  version: number | null;
}
export type ArtifactTarget = { id: string; token?: never } | { token: string; id?: never };
export interface ArtifactRuntime {
  artifact: Omit<ArtifactRecord, 'origin'> & {
    origin: Partial<ArtifactRecord['origin']>;
    share?: Pick<ArtifactShare, 'version' | 'access' | 'audience'>;
  };
  version: number;
  content: string;
  manifest: Manifest;
  viewer: { level: 'none' | 'view' | 'interact' | 'owner'; id: string | null; guest: boolean };
  pinned: boolean;
  after_seq: number;
}
export interface AgentRun { run_id: string; session_id: string; url: string }

function ownerPath(id: string): string { return `/artifacts/${encodeURIComponent(id)}`; }
function runtimePath(target: ArtifactTarget): string {
  return target.token
    ? `/artifact-share/${encodeURIComponent(target.token)}/runtime`
    : `${ownerPath(target.id!)}/runtime`;
}
function dataPath(target: ArtifactTarget, path: string): string {
  return `${runtimePath(target)}/data/${path.split('/').map(encodeURIComponent).join('/')}`;
}

export class ArtifactsApi {
  constructor(private readonly request: APIRequestContext, private readonly guestId: string = randomUUID()) {}

  private viewerPath(path: string) {
    if (!path.startsWith('/artifact-share/')) return path;
    return `${path}${path.includes('?') ? '&' : '?'}guest_id=${this.guestId}`;
  }

  private async data<T>(method: string, path: string, body?: unknown): Promise<T> {
    const response = await this.request.fetch(apiPath(this.viewerPath(path)), { method, data: body, timeout: 30_000 });
    expect(response.ok(), `${method} ${path}: ${await response.text()}`).toBe(true);
    const envelope = await response.json();
    expect(envelope.code).toBe('OK');
    return envelope.data as T;
  }

  list(query: { q?: string; session_id?: string; assistant_id?: string } = {}) {
    return this.data<{ items: ArtifactRecord[]; next_cursor: string | null; has_more: boolean }>(
      'GET', `/artifacts?${new URLSearchParams(query)}`,
    );
  }
  get(id: string) { return this.data<ArtifactRecord>('GET', ownerPath(id)); }
  versions(id: string) { return this.data<ArtifactVersionSummary[]>('GET', `${ownerPath(id)}/versions`); }
  version(id: string, version: number) {
    return this.data<ArtifactVersion>('GET', `${ownerPath(id)}/versions/${version}`);
  }
  remove(id: string) { return this.data<{ deleted: boolean }>('DELETE', ownerPath(id)); }
  share(id: string) { return this.data<ArtifactShare>('GET', `${ownerPath(id)}/share`); }
  runtime(target: ArtifactTarget) { return this.data<ArtifactRuntime>('GET', runtimePath(target)); }
  profiles(target: ArtifactTarget, ids: string[]) {
    return this.data<{ profiles: Array<{ id: string; display_name: string }> }>(
      'GET', `${runtimePath(target)}/viewers?${new URLSearchParams({ ids: ids.join(',') })}`,
    );
  }
  getData(target: ArtifactTarget, path: string) {
    return this.data<{ exists: boolean; data: Record<string, unknown> | null }>('GET', dataPath(target, path));
  }
  queryData(target: ArtifactTarget, collection: string) {
    return this.data<{ items: Array<{ path: string; data: Record<string, unknown> }> }>(
      'POST', `${runtimePath(target)}/data-query`, { collection, where: [], order_by: [], limit: 100 },
    );
  }
  async refusal(target: ArtifactTarget, operation: 'runtime' | 'read' | 'write', path = '', data?: unknown) {
    const response = await this.request.fetch(apiPath(this.viewerPath(operation === 'runtime'
      ? runtimePath(target) : dataPath(target, path))), {
      method: operation === 'write' ? 'PUT' : 'GET',
      ...(operation === 'write' ? { data: { data } } : {}),
    });
    const envelope = await response.json();
    return { status: response.status(), code: envelope.error?.code ?? envelope.code, data: envelope.data };
  }
  async refusePublicShare(id: string) {
    const response = await this.request.post(apiPath(`${ownerPath(id)}/share`), {
      data: { access: 'view', audience: 'anyone', version: null },
    });
    expect(response.ok()).toBe(false);
    const envelope = await response.json();
    return envelope.error?.code ?? envelope.code;
  }
  async nextAgentRun(page: Page, id: string): Promise<AgentRun> {
    const response = await page.waitForResponse((candidate) =>
      new URL(candidate.url()).pathname === apiPath(`${runtimePath({ id })}/agent-runs`)
      && candidate.request().method() === 'POST');
    expect(response.ok(), await response.text()).toBe(true);
    const envelope = await response.json();
    expect(envelope.code).toBe('OK');
    return envelope.data as AgentRun;
  }
  observeCalls(page: Page, id: string, operation: 'sample' | 'agent-runs') {
    let count = 0;
    page.on('request', (request) => {
      if (new URL(request.url()).pathname === apiPath(`${runtimePath({ id })}/${operation}`)
        && request.method() === 'POST') count += 1;
    });
    return () => count;
  }
}

export function storedVersions(id: string): Record<string, unknown>[] {
  return postgresRows(
    'SELECT version, sha256, content FROM artifact_versions '
    + 'JOIN artifact_version_contents USING (artifact_id, version) '
    + `WHERE artifact_id = ${sqlLiteral(id)} ORDER BY version`,
  );
}
export function storedArtifact(id: string): Record<string, unknown>[] {
  return postgresRows(
    "SELECT owner_id, json_build_object('enabled', enabled, 'token', token) AS share "
    + 'FROM artifacts LEFT JOIN artifact_shares USING (artifact_id) '
    + `WHERE artifact_id = ${sqlLiteral(id)}`,
  );
}
export function storedData(id: string, path: string): Record<string, unknown>[] {
  return postgresRows(
    'SELECT path, data FROM artifact_documents '
    + `WHERE artifact_id = ${sqlLiteral(id)} AND path = ${sqlLiteral(path)}`,
  );
}
export function nativeArtifactCalls(sessionId: string) {
  return nativeRows(sessionId).filter((row) => row.subpath == null)
    .sort((left, right) => Number(left.seq) - Number(right.seq))
    .flatMap((row) => nativeCalls(JSON.parse(String(row.entry_json)), row.subpath))
    .filter((call) => call.name.includes('artifacts'));
}
export async function expectStoredVersion(api: ArtifactsApi, id: string, version: number, content: string) {
  expect((await api.version(id, version)).content, 'HTTP version must retain the exact tool input').toBe(content);
  await expect.poll(() => storedVersions(id).find((row) => row.version === version), {
    timeout: 15_000,
  }).toMatchObject({ content, sha256: createHash('sha256').update(content).digest('hex') });
}
