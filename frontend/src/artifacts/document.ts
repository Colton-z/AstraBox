import type { ArtifactKind } from './types';
export const ARTIFACT_SANDBOX =
  'allow-scripts allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox';
export function artifactCSP(origin: string) {
  return `default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' ${origin}/artifact-runtime/ https://cdnjs.cloudflare.com https://cdn.jsdelivr.net/npm/ https://unpkg.com https://cdn.tailwindcss.com https://code.jquery.com; style-src 'unsafe-inline' ${origin}/artifact-runtime/ https://fonts.googleapis.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net/npm/; font-src ${origin}/artifact-runtime/ https://fonts.gstatic.com data:; img-src data: blob: ${origin}/artifact-runtime/; media-src data: blob:; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'`;
}
const scriptJSON = (value: string) =>
  JSON.stringify(value)
    .replaceAll('<', '\\u003c')
    .replaceAll('\u2028', '\\u2028')
    .replaceAll('\u2029', '\\u2029');
export function buildArtifactDocument({
  kind,
  content,
  origin,
  release,
  theme,
}: {
  kind: ArtifactKind;
  content: string;
  origin: string;
  release: string;
  theme?: 'light' | 'dark';
}) {
  if (kind === 'markdown' || kind === 'code')
    throw new Error('Markdown and code render in the console.');
  const doc = new DOMParser().parseFromString(
    kind === 'html'
      ? content
      : '<!doctype html><html><head></head><body><div id="root"></div></body></html>',
    'text/html',
  );
  if (kind === 'html') {
    for (const script of doc.querySelectorAll('script')) {
      if (
        script.type &&
        !['text/javascript', 'application/javascript', 'module'].includes(
          script.type,
        )
      )
        continue;
      if (script.type) script.setAttribute('data-artifact-type', script.type);
      script.type = 'application/astrabox-script';
    }
  }
  const csp = doc.createElement('meta');
  csp.httpEquiv = 'Content-Security-Policy';
  csp.content = artifactCSP(origin);
  const bridge = doc.createElement('script');
  bridge.src = `${origin}/artifact-runtime/bridge.js?release=${encodeURIComponent(release)}`;
  doc.head.prepend(csp, bridge);
  if (kind === 'svg') {
    const root = doc.getElementById('root')!;
    root.style.cssText =
      'display:flex;min-height:100vh;align-items:center;justify-content:center';
    root.innerHTML = content;
  }
  if (kind === 'react' || kind === 'mermaid') {
    if (theme) {
      doc.documentElement.style.colorScheme = theme;
      doc.documentElement.classList.toggle('dark', theme === 'dark');
    }
    const tailwind = doc.createElement('style');
    tailwind.type = 'text/tailwindcss';
    tailwind.textContent =
      '@custom-variant dark (&:where(.dark, .dark *)); @theme inline { --color-background: var(--background); --color-foreground: var(--foreground); --color-card: var(--background); --color-card-foreground: var(--foreground); --color-muted: var(--muted); --color-muted-foreground: var(--muted-foreground); --color-border: var(--border); --color-input: var(--border); --color-primary: var(--primary); --color-primary-foreground: var(--primary-foreground); --color-secondary: var(--muted); --color-secondary-foreground: var(--foreground); --color-accent: var(--muted); --color-accent-foreground: var(--foreground); --color-ring: Highlight; }';
    if (kind === 'react') doc.head.append(tailwind);
    const config = doc.createElement('script');
    config.textContent = `window.__artifactSource=${scriptJSON(content)};window.__artifactBase=${scriptJSON(`${origin}/artifact-runtime`)};window.__artifactRelease=${scriptJSON(release)};`;
    const renderer = doc.createElement('script');
    renderer.src = `${origin}/artifact-runtime/${kind}.js?release=${encodeURIComponent(release)}`;
    if (kind === 'react') {
      const theme = doc.createElement('link');
      theme.rel = 'stylesheet';
      theme.href = `${origin}/artifact-runtime/theme.css?release=${encodeURIComponent(release)}`;
      doc.head.append(theme);
    }
    doc.body.append(config, renderer);
  }
  return `<!doctype html>${doc.documentElement.outerHTML}`;
}
