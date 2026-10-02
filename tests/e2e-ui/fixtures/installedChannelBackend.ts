import { execFileSync } from 'node:child_process';
import path from 'node:path';

import { expect } from '@playwright/test';

import { absoluteBaseUrl, repoRoot } from './env';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from './serviceContainer';

const helper = path.join(repoRoot, 'tests/e2e-ui/fixtures/installed_channel_backend.py');

/** Replace only the server process while exercising an installed channel wheel. */
export class InstalledChannelBackend {
  readonly evidence: Record<string, unknown>;

  constructor(readonly container: string, private readonly receipt: string) {
    this.evidence = JSON.parse(execFileSync('python3', [helper, 'start',
      '--source', requireServiceContainer(SERVER_CONTAINER_HANDLE),
      '--name', container, '--origin', absoluteBaseUrl(), '--receipt', receipt,
    ], { encoding: 'utf8', timeout: 90_000 }));
  }

  async ready(): Promise<void> {
    expect(this.evidence.metadata).toEqual({ version: '1.0.0', entry_points: [{
      group: 'astrabox.providers.channel', name: 'installed_probe',
      value: 'astrabox_test_channel:InstalledChannel',
    }] });
    await expect.poll(async () => {
      try {
        return (await fetch(`${absoluteBaseUrl()}/readyz`, { signal: AbortSignal.timeout(2_000) })).status;
      } catch (error) {
        if (!(error instanceof TypeError) && !(error instanceof DOMException)) throw error;
        return 0;
      }
    }, { timeout: 30_000, intervals: [500, 1_000] }).toBe(200);
  }

  async restore(): Promise<void> {
    this.evidence.restored = JSON.parse(execFileSync('python3', [helper, 'restore', '--receipt', this.receipt],
      { encoding: 'utf8', timeout: 30_000 }));
    await this.ready();
  }
}
