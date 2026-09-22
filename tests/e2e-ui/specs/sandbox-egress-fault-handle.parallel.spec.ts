import { expect, test } from '@playwright/test';

import type { AstraApi } from '../fixtures/astraApi';
import { setSandboxEgressFault } from '../fixtures/sandboxOps';

type RuleAction = 'allow' | 'deny';

class FakeEgressApi {
  readonly calls: Array<{ method: string; route: string; body?: unknown }> = [];
  readonly rules = new Map<string, RuleAction>();
  defaultAction: RuleAction = 'allow';
  available = true;
  faultRouteEnabled = true;
  applyMutations = true;

  async data<T>(method: string, route: string, body?: unknown): Promise<T> {
    this.calls.push({ method, route, body });
    if (method === 'GET' && route.endsWith('/security')) {
      return {
        sandbox_id: 'box-1',
        available: this.available,
        default_action: this.defaultAction,
        egress_rules: [...this.rules].map(([target, action]) => ({ action, target })),
        detail: this.available ? null : 'egress sidecar did not answer',
      } as T;
    }
    if (method === 'POST' && route.endsWith('/admin/e2e/sandboxes/box-1/egress')) {
      if (!this.faultRouteEnabled) {
        throw new Error('POST -> 404: Not Found');
      }
      const payload = body as { operation: 'patch' | 'delete'; action?: RuleAction; target: string };
      if (this.applyMutations) {
        if (payload.operation === 'delete') this.rules.delete(payload.target);
        else this.rules.set(payload.target, payload.action!);
      }
      return { operation: payload.operation } as T;
    }
    throw new Error(`unexpected fake request ${method} ${route}`);
  }
}

function asAstraApi(fake: FakeEgressApi): AstraApi {
  return fake as unknown as AstraApi;
}

test('egress fault handle confirms deny and restores the prior allow rule', async () => {
  const fake = new FakeEgressApi();
  fake.rules.set('platform.internal', 'allow');

  const fault = await setSandboxEgressFault(
    asAstraApi(fake),
    'box-1',
    'platform.internal',
  );

  expect(fake.rules.get('platform.internal')).toBe('deny');
  expect(fault.previousAction).toBe('allow');
  expect(fault.active).toBe(true);
  expect(fake.calls.map(({ method, route }) => `${method} ${route}`)).toEqual([
    'GET /admin/sandboxes/box-1/security',
    'POST /admin/e2e/sandboxes/box-1/egress',
    'GET /admin/sandboxes/box-1/security',
  ]);

  await fault.restore();

  expect(fake.rules.get('platform.internal')).toBe('allow');
  expect(fault.active).toBe(false);
  expect(fake.calls.slice(-2).map(({ method, route }) => `${method} ${route}`)).toEqual([
    'POST /admin/e2e/sandboxes/box-1/egress',
    'GET /admin/sandboxes/box-1/security',
  ]);
});

test('restore deletes a deny rule when the destination originally used default allow', async () => {
  const fake = new FakeEgressApi();
  const target = 'mirror.internal';

  const fault = await setSandboxEgressFault(asAstraApi(fake), 'box-1', target);
  expect(fake.rules.get(target)).toBe('deny');
  expect(fault.previousAction).toBeNull();

  await fault.restore();

  expect(fake.rules.has(target)).toBe(false);
  expect(fake.calls.at(-2)?.body).toEqual({ operation: 'delete', target });
});

test('missing armed route refuses with the gate, target, and backend contract', async () => {
  const fake = new FakeEgressApi();
  fake.rules.set('platform.internal', 'allow');
  fake.faultRouteEnabled = false;

  await expect(
    setSandboxEgressFault(asAstraApi(fake), 'box-1', 'platform.internal'),
  ).rejects.toThrow(/box-1.*platform\.internal.*ASTRABOX_E2E_FAULTS=1.*live egress mutation/s);
  expect(fake.rules.get('platform.internal')).toBe('allow');
});

test('a successful mutation response without a matching read-back is rejected', async () => {
  const fake = new FakeEgressApi();
  fake.rules.set('platform.internal', 'allow');
  fake.applyMutations = false;

  await expect(
    setSandboxEgressFault(asAstraApi(fake), 'box-1', 'platform.internal'),
  ).rejects.toThrow(/did not converge on read-back/);
  expect(
    fake.calls.filter(({ method, route }) => method === 'GET' && route.endsWith('/security')),
    'the handle must observe both the mutation and its automatic rollback',
  ).toHaveLength(3);
});
