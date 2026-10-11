// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import {
  artifactCSP,
  ARTIFACT_SANDBOX,
  buildArtifactDocument,
} from './document';
const settings = { base: 'https://console.example', release: 'release-1' };
describe('artifact document isolation', () => {
  it('installs the exact policy first, then the bridge, before author scripts', () => {
    const html = buildArtifactDocument({
      ...settings,
      kind: 'html',
      content:
        '<html><head><title>Page</title><script>localStorage.getItem("state")</script></head><body>Hello</body></html>',
    });
    const doc = new DOMParser().parseFromString(html, 'text/html');
    expect(doc.head.firstElementChild?.getAttribute('content')).toBe(
      artifactCSP(settings.base),
    );
    expect(doc.head.children[1].getAttribute('src')).toBe(
      'https://console.example/artifact-runtime/bridge.js?release=release-1',
    );
    expect(
      doc.querySelector('script[type="application/astrabox-script"]')
        ?.textContent,
    ).toContain('localStorage');
    expect(doc.title).toBe('Page');
    expect(doc.body.textContent).toBe('Hello');
  });
  it('blocks same-origin access, page downloads, external images and non-npm jsDelivr', () => {
    expect(ARTIFACT_SANDBOX.split(' ')).toEqual([
      'allow-scripts',
      'allow-forms',
      'allow-modals',
      'allow-popups',
      'allow-popups-to-escape-sandbox',
    ]);
    expect(ARTIFACT_SANDBOX).not.toContain('allow-same-origin');
    expect(ARTIFACT_SANDBOX).not.toContain('allow-downloads');
    const csp = artifactCSP(settings.base);
    expect(
      csp
        .split(';')
        .find((rule) => rule.trim().startsWith('img-src'))
        ?.trim(),
    ).toBe('img-src data: blob: https://console.example/artifact-runtime/');
    expect(csp).toContain('https://cdn.jsdelivr.net/npm/');
    expect(csp.replaceAll('https://cdn.jsdelivr.net/npm/', '')).not.toContain(
      'https://cdn.jsdelivr.net',
    );
    expect(csp).toContain("connect-src 'none'");
    expect(csp).toContain("frame-src 'none'");
  });
  it('creates a head for fragments and centers SVG', () => {
    const doc = new DOMParser().parseFromString(
      buildArtifactDocument({
        ...settings,
        kind: 'html',
        content: '<main>Fragment</main>',
      }),
      'text/html',
    );
    expect(doc.head.firstElementChild?.tagName).toBe('META');
    expect(doc.querySelector('main')?.textContent).toBe('Fragment');
    const svg = buildArtifactDocument({
      ...settings,
      kind: 'svg',
      content: '<svg><text>Figure</text></svg>',
    });
    expect(svg).toContain('justify-content: center');
    expect(svg).toContain('<svg>');
  });
  it.each(['react', 'mermaid'] as const)(
    'loads the %s renderer and quotes source safely',
    (kind) => {
      const html = buildArtifactDocument({
        ...settings,
        kind,
        content: '</script><script>alert(1)</script>',
      });
      expect(html).toContain(`/artifact-runtime/${kind}.js?release=release-1`);
      expect(html).toContain('\\u003c/script>');
      expect(html).not.toContain('<script>alert(1)</script>');
    },
  );
  it.each(['markdown', 'code'] as const)(
    'keeps %s outside the frame',
    (kind) => {
      expect(() =>
        buildArtifactDocument({ ...settings, kind, content: 'text' }),
      ).toThrow('render in the console');
    },
  );
});
