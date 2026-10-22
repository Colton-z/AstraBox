/** Optional release-upgrade inputs for the existing native recovery journeys. */
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';
import { AstraApi } from './astraApi';
import { requireSandboxHandle } from './sandboxOps';

export async function upgradeRuntimeImage(api: AstraApi, name: string): Promise<string | null> {
  const path = process.env.ASTRABOX_E2E_RUNTIME_UPGRADE_FILE;
  if (!path) return null;
  const plan = JSON.parse(readFileSync(path, 'utf8'))[name];
  expect(plan, `an upgrade must declare RuntimeProfile ${name}`).toBeTruthy();
  const profiles = await api.data<Array<Record<string, unknown>>>('GET', '/admin/runtime-profiles');
  expect(profiles.find((profile) => profile.name === name)?.runtime_template_name).toBe(plan.before);
  expect(plan.config.runtime_template_name).not.toBe(plan.before);
  await api.data('PUT', `/admin/runtime-profiles/${name}`, plan.config);
  return String(plan.config.runtime_template_name);
}

export async function recordRuntimeImage(api: AstraApi, box: string, expected?: string): Promise<void> {
  if (!process.env.ASTRABOX_E2E_RUNTIME_UPGRADE_FILE) return;
  const handle = await requireSandboxHandle(api, box);
  if (handle.runtime !== 'kubernetes') throw new Error('native upgrade evidence requires Kubernetes');
  const pod = JSON.parse(execFileSync('kubectl', [
    'get', 'pod', handle.pod, '-n', handle.namespace, '-o', 'json',
  ], { encoding: 'utf8', timeout: 15_000 }));
  const images = pod.spec.containers.map((container: { image: string }) => container.image);
  if (expected) expect(images).toContain(expected);
  await test.info().attach('native-runtime-image', {
    body: JSON.stringify({ box, images, statuses: pod.status.containerStatuses }), contentType: 'application/json',
  });
}
