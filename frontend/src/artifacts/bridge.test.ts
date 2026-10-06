// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { installBridge } from '../../artifact-runtime/bridge';
import type { PageWindow } from '../../artifact-runtime/bridge';
import { CHANNEL } from '../../artifact-runtime/types';
import type { Viewer } from '../../artifact-runtime/types';
function scene() {
  const parent = { postMessage: vi.fn() };
  const handlers: Record<string, (event: any) => void> = {};
  const page = {
    parent,
    document,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    addEventListener: (name: string, fn: any) => {
      handlers[name] = fn;
    },
  } as unknown as PageWindow;
  const bridge = installBridge(page);
  const init = (viewer: Viewer = { id: 'viewer', guest: false, level: 'owner' }) =>
    handlers.message({
      source: parent,
      data: {
        channel: CHANNEL,
        type: 'init',
        granted: ['artifact', 'db', 'user'],
        version: 3,
        viewer,
        storage: { saved: 'yes' },
      },
    });
  return { bridge, page, parent, handlers, init };
}
beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
});
describe('use and storage', () => {
  it('resolves asynchronously to frozen memoized namespaces after init', async () => {
    const { bridge, page, init } = scene();
    let resolved = false;
    const promise = bridge.use('artifact');
    void promise.then(() => {
      resolved = true;
    });
    expect(bridge.use('artifact')).toBe(promise);
    expect(resolved).toBe(false);
    init();
    const artifact = await promise;
    expect(Object.isFrozen(page.astrabox)).toBe(true);
    expect(Object.isFrozen(artifact)).toBe(true);
    expect(artifact?.version()).toBe(3);
    expect(await bridge.use('assets')).toBeNull();
    expect(await bridge.use('agent')).toBeNull();
  });
  it('resolves null after ten seconds without a shell', async () => {
    vi.useFakeTimers();
    const { bridge } = scene();
    const promise = bridge.use('db');
    await vi.advanceTimersByTimeAsync(10_000);
    expect(await promise).toBeNull();
    expect(bridge.use('db')).toBe(promise);
  });
  it('ignores initialization from another source', async () => {
    vi.useFakeTimers();
    const { bridge, handlers } = scene();
    handlers.message({
      source: {},
      data: { channel: CHANNEL, type: 'init', granted: ['db'] },
    });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(await bridge.use('db')).toBeNull();
  });
  it('does not give a guest a private self path through its presence id', async () => {
    const { bridge, init } = scene();
    init({ id: 'guest_per_load', guest: true, level: 'view' });
    const db = await bridge.use('db');
    await expect(db!.doc('data/users/{self}/profile').get()).rejects.toMatchObject({
      code: 'not_granted',
    });
  });
  it('seeds local storage synchronously, persists writes and keeps session storage in memory', () => {
    const { page, parent, init } = scene();
    init();
    expect(page.localStorage.getItem('saved')).toBe('yes');
    page.localStorage.setItem('key', 'value');
    expect(parent.postMessage).toHaveBeenLastCalledWith(
      {
        channel: CHANNEL,
        type: 'storage',
        payload: { saved: 'yes', key: 'value' },
      },
      '*',
    );
    parent.postMessage.mockClear();
    page.sessionStorage.setItem('temporary', 'data');
    expect(parent.postMessage).not.toHaveBeenCalled();
    expect(() =>
      page.localStorage.setItem('oversized', 'x'.repeat(5 * 1024 * 1024)),
    ).toThrow('5 MiB');
  });
  it('reports uncaught errors and promise rejections with their stack', () => {
    const { page, parent, handlers } = scene();
    const error = new Error('page failed');
    page.onerror?.('page failed', '', 0, 0, error);
    expect(parent.postMessage).toHaveBeenLastCalledWith(
      {
        channel: CHANNEL,
        type: 'error',
        message: 'page failed',
        stack: error.stack,
      },
      '*',
    );
    handlers.unhandledrejection({ reason: error });
    expect(parent.postMessage).toHaveBeenLastCalledWith(
      {
        channel: CHANNEL,
        type: 'error',
        message: 'page failed',
        stack: error.stack,
      },
      '*',
    );
  });
});
