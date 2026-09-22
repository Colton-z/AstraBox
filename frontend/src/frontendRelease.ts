const HASHED_MAIN_ENTRY_RE =
  /<script\b[^>]*\bsrc=(["'])([^"']*\/assets\/main-[A-Za-z0-9_-]{8,}\.js(?:\?[^"']*)?)\1[^>]*>/i;
const RELEASE_CHECK_INTERVAL_MS = 60_000;

let releaseHolds = 0;

/**
 * Keep the release guard from replacing the document while the reader has
 * something in it that a reload would lose: a half-typed prompt, an unsaved
 * draft on an edit page. Returns the release; the first check after every
 * hold is gone reloads.
 */
export function holdFrontendRelease(): () => void {
  releaseHolds += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    releaseHolds -= 1;
  };
}

export function normalizeFrontendEntryAsset(
  value: string | null | undefined,
  baseUrl: string,
): string | null {
  const candidate = String(value ?? '').trim();
  if (!candidate) return null;
  const url = new URL(candidate, baseUrl);
  if (!/\/assets\/main-[A-Za-z0-9_-]{8,}\.js$/.test(url.pathname)) {
    return null;
  }
  return url.href;
}

export function frontendEntryAssetFromHtml(
  html: string,
  baseUrl: string,
): string | null {
  const match = String(html ?? '').match(HASHED_MAIN_ENTRY_RE);
  return normalizeFrontendEntryAsset(match?.[2], baseUrl);
}

export function frontendEntryAssetFromDocument(
  documentValue: Document,
  baseUrl: string,
): string | null {
  const script = documentValue.querySelector<HTMLScriptElement>(
    'script[type="module"][src*="/assets/main-"]',
  );
  return normalizeFrontendEntryAsset(script?.src, baseUrl);
}

/**
 * Keep a resident console tab on the release currently served by the backend.
 *
 * Vite entry assets are immutable and hash-named. A focus/visibility/interval
 * probe therefore compares only the document's entry URL; when it changes the
 * whole document is replaced so no old/new module graph can be mixed in one tab.
 */
export function startFrontendReleaseGuard(
  options: { reload?: () => void } = {},
): () => void {
  const reload = options.reload ?? (() => window.location.reload());
  const loadedEntry = frontendEntryAssetFromDocument(document, window.location.href);
  if (!loadedEntry) {
    // The Vite dev server uses /src/main.tsx rather than a hashed production
    // entry, so the guard is deliberately inert during local frontend work.
    return () => undefined;
  }
  let stopped = false;
  let checking = false;
  let reloading = false;

  const checkForRelease = async () => {
    if (stopped || checking || reloading) return;
    checking = true;
    try {
      const response = await window.fetch(window.location.href, {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {
          Accept: 'text/html',
          'Cache-Control': 'no-cache',
        },
      });
      if (!response.ok) {
        throw new Error(`frontend document check failed with HTTP ${response.status}`);
      }
      const serverEntry = frontendEntryAssetFromHtml(
        await response.text(),
        response.url || window.location.href,
      );
      if (!serverEntry) {
        throw new Error('frontend document check returned no hashed main entry');
      }
      if (serverEntry !== loadedEntry && releaseHolds === 0) {
        reloading = true;
        reload();
      }
    } catch (error) {
      console.error('[frontend-release] document check failed', error);
    } finally {
      checking = false;
    }
  };

  const handleFocus = () => {
    void checkForRelease();
  };
  const handleVisibilityChange = () => {
    if (document.visibilityState === 'visible') {
      void checkForRelease();
    }
  };
  window.addEventListener('focus', handleFocus);
  document.addEventListener('visibilitychange', handleVisibilityChange);
  const interval = window.setInterval(() => {
    void checkForRelease();
  }, RELEASE_CHECK_INTERVAL_MS);

  return () => {
    stopped = true;
    window.clearInterval(interval);
    window.removeEventListener('focus', handleFocus);
    document.removeEventListener('visibilitychange', handleVisibilityChange);
  };
}
