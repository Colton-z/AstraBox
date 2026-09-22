// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  frontendEntryAssetFromHtml,
  holdFrontendRelease,
  normalizeFrontendEntryAsset,
  startFrontendReleaseGuard,
} from './frontendRelease';

describe('frontend release entry discovery', () => {
  it('resolves a Vite hashed main entry against the served document URL', () => {
    expect(
      frontendEntryAssetFromHtml(
        '<script type="module" crossorigin src="/console/assets/main-AbCdEf12.js"></script>',
        'https://example.test/console/sessions/s-1',
      ),
    ).toBe('https://example.test/console/assets/main-AbCdEf12.js');
  });

  it('ignores dev, unhashed, and non-main module entries', () => {
    expect(normalizeFrontendEntryAsset('/src/main.tsx', 'https://example.test/')).toBeNull();
    expect(normalizeFrontendEntryAsset('/assets/main.js', 'https://example.test/')).toBeNull();
    expect(normalizeFrontendEntryAsset('/assets/admin-AbCdEf12.js', 'https://example.test/')).toBeNull();
  });
});

describe('frontend release guard', () => {
  const servedHtml = (hash: string) =>
    `<html><head><script type="module" src="/assets/main-${hash}.js"></script></head></html>`;

  function mountLoadedEntry(hash: string) {
    const script = document.createElement('script');
    script.type = 'module';
    script.src = `/assets/main-${hash}.js`;
    document.head.appendChild(script);
    return () => script.remove();
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function checkOnce() {
    window.dispatchEvent(new Event('focus'));
    // The check fetches the document, reads its body, then decides.
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
  }

  it('reloads when the served entry differs, but not while something unsaved is held', async () => {
    const unmount = mountLoadedEntry('aaaaaaaa');
    vi.stubGlobal('fetch', vi.fn(async () => new Response(servedHtml('bbbbbbbb'), {
      status: 200,
      headers: { 'content-type': 'text/html' },
    })));
    const reload = vi.fn();
    const stop = startFrontendReleaseGuard({ reload });
    try {
      const release = holdFrontendRelease();
      await checkOnce();
      expect(reload).not.toHaveBeenCalled();

      release();
      release(); // releasing twice does not free a hold someone else placed
      await checkOnce();
      expect(reload).toHaveBeenCalledTimes(1);
    } finally {
      stop();
      unmount();
    }
  });

  it('does not reload while the served entry is the loaded one', async () => {
    const unmount = mountLoadedEntry('cccccccc');
    vi.stubGlobal('fetch', vi.fn(async () => new Response(servedHtml('cccccccc'), {
      status: 200,
      headers: { 'content-type': 'text/html' },
    })));
    const reload = vi.fn();
    const stop = startFrontendReleaseGuard({ reload });
    try {
      await checkOnce();
      expect(reload).not.toHaveBeenCalled();
    } finally {
      stop();
      unmount();
    }
  });
});
