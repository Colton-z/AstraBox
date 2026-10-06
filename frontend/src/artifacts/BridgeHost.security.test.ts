// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { CHANNEL, CONTRACT } from '../../artifact-runtime/types';
import { BridgeHost } from './BridgeHost';
import { artifactRequest, runtimePath } from './api';
import type { ArtifactRuntime } from './types';
vi.mock('./api', async (original) => ({
  ...await original<typeof import('./api')>(),
  subscribeArtifactLive: () => () => {},
}));
afterEach(() => vi.unstubAllGlobals());
const invalid = ['', '.', '..', '%2e%2e', '.%2E', '%252e%252e', 'a%2fb', 'a\\b', 'a\0b'];
const calls: [string, string, unknown[]][] = [
  ...invalid.flatMap((part): [string, string, unknown[]][] => [
    ['db', 'get', [`${part}/x`]], ['db', 'set', [`${part}/x`, {}]],
    ['db', 'update', [`${part}/x`, {}]], ['db', 'delete', [`${part}/x`]],
    ['db', 'query', [{ collection: part }]], ['db', 'add', [part, {}]],
    ['room', 'emit', [part, 'topic', {}]], ['room', 'presence', [part, {}]],
    ['agent', 'status', [part]], ['agent', 'watch', [part, 'watch']],
  ]),
  ['db', 'delete', ['../../../../artifacts/art_x']],
  ['db', 'get', ['collection']], ['db', 'query', [{ collection: 'items/a' }]],
  ['db', 'add', ['items/a', {}]], ['room', 'presence', ['a/b', {}]],
  ['agent', 'status', ['a/b']],
];
it.each(calls)('rejects forged %s.%s arguments before fetching: %j', async (capability, method, args) => {
  const fetch = vi.fn();
  vi.stubGlobal('fetch', fetch);
  const frame = document.createElement('iframe');
  document.body.append(frame);
  const send = vi.spyOn(frame.contentWindow!, 'postMessage');
  const host = new BridgeHost(frame, {
    artifact: { artifact_id: 'art_safe', origin: {} }, version: 1,
    manifest: { db: {}, room: { topics: { topic: 'view' } }, agent: {} },
    viewer: { id: 'owner', guest: false, level: 'owner' },
  } as unknown as ArtifactRuntime, { id: 'art_safe' }, async () => true, vi.fn(), vi.fn());
  const message = (data: object) => window.dispatchEvent(new MessageEvent('message', {
    source: frame.contentWindow, data: { channel: CHANNEL, ...data },
  }));
  try {
    message({ type: 'hello', contract: CONTRACT });
    message({ type: 'call', id: 'attack', capability, method, args });
    await Promise.resolve();
    await Promise.resolve();
    expect(send).toHaveBeenCalledWith(expect.objectContaining({
      type: 'result', ok: false, error: expect.objectContaining({ code: 'invalid_argument' }),
    }), '*');
    expect(fetch).not.toHaveBeenCalled();
  } finally { host.stop(); frame.remove(); }
});
it('fetches valid segments inside the exact artifact or share runtime prefix', async () => {
  const fetch = vi.fn().mockImplementation(async () =>
    new Response(JSON.stringify({ code: 'OK', data: {} }), {
      headers: { 'Content-Type': 'application/json' },
    }));
  vi.stubGlobal('fetch', fetch);
  for (const target of [{ id: 'art_safe' }, { token: 'share_safe', guestId: 'guest' }]) {
    const prefix = new URL(runtimePath(target), window.location.href).pathname;
    await artifactRequest(runtimePath(target, 'data', 'items/a b'));
    const pathname = new URL(fetch.mock.lastCall![0], window.location.href).pathname;
    expect(pathname.startsWith(`${prefix}/`)).toBe(true);
  }
});
