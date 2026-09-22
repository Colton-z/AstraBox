/**
 * Renders one docs figure in both colour modes, without building the site.
 *
 *   node scripts/figure-preview.mjs ../docs/img/name.svg /tmp/out
 *
 * Writes `<out>-light.png` and `<out>-dark.png`, and fails on the three defects
 * a hand-placed SVG produces that no build check would catch: a label
 * straddling a node's edge, two labels on top of each other, and a label whose
 * colour misses WCAG AA against the page.
 *
 * The wrapper carries the same tokens `website/src/css/custom.css` declares,
 * because that is what the figure inherits once it is inlined into a page. Keep
 * the two lists in step: a token that only exists here would look correct in a
 * preview and wrong on the site.
 */

import fs from "node:fs";
import path from "node:path";

import { chromium } from "@playwright/test";

const LIGHT = `
  --ifm-background-color: #fafafa;
  --ifm-background-surface-color: #ffffff;
  --fg-1: #0a0a0a; --fg-2: #2e2e2e; --fg-3: #666666; --fg-4: #999999;
  --line: #e5e5e5; --line-strong: #d4d4d4;
  --astra: #5c5bff; --astra-tint: #ecebff; --plasma: #e8754a;
`;

const DARK = `
  --ifm-background-color: #0e0e0e;
  --ifm-background-surface-color: #1a1a1a;
  --fg-1: #ededed; --fg-2: #a8a8a8; --fg-3: #909090; --fg-4: #555555;
  --line: #232323; --line-strong: #383838;
  --astra: #5c5bff; --astra-tint: #1a1b3a; --plasma: #e8754a;
`;

function page(svg, tokens) {
  return `<!doctype html><html><head><meta charset="utf-8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root { ${tokens} }
  html, body { margin: 0; background: var(--ifm-background-color); }
  body { font-family: Geist, ui-sans-serif, system-ui, sans-serif; }
  figure { margin: 0; padding: 24px; width: 1000px; }
  figure svg { display: block; width: 100%; height: auto; }
</style></head><body><figure id="fig">${svg}</figure></body></html>`;
}

const [source, outPrefix] = process.argv.slice(2);
if (!source || !outPrefix) {
  console.error("usage: figure-preview.mjs <figure.svg> <out-prefix>");
  process.exit(2);
}

const raw = fs.readFileSync(source, "utf8");
const open = raw.indexOf("<svg");
const close = raw.lastIndexOf("</svg>");
if (open < 0 || close < 0) {
  console.error(`${source}: no <svg> element`);
  process.exit(2);
}
const svg = raw.slice(open, close + "</svg>".length);

fs.mkdirSync(path.dirname(path.resolve(outPrefix)), { recursive: true });

const browser = await chromium.launch();
const problems = [];

// A figure has two readers. Inlined into a page it is parsed as HTML, which
// forgives things XML does not; opened on its own — the GitHub rendering the
// Markdown source promises — it is parsed as strict XML and one valueless
// attribute makes the whole file render nothing. Check the strict reader too.
{
  const tab = await browser.newPage();
  const xmlErrors = await tab.evaluate((source) => {
    const parsed = new DOMParser().parseFromString(source, "image/svg+xml");
    return [...parsed.getElementsByTagName("parsererror")].map((node) =>
      node.textContent.replace(/\s+/g, " ").trim().slice(0, 200),
    );
  }, raw);
  problems.push(...xmlErrors.map((error) => `standalone: not well-formed XML — ${error}`));
  await tab.close();
}

for (const [mode, tokens] of [
  ["light", LIGHT],
  ["dark", DARK],
]) {
  const tab = await browser.newPage({ viewport: { width: 1080, height: 900 } });
  await tab.setContent(page(svg, tokens), { waitUntil: "load" });
  await tab.evaluate(() => document.fonts.ready);

  if (mode === "light") {
    // Geometry is checked once: it does not depend on the colour mode.
    const found = await tab.evaluate(() => {
      const svgEl = document.querySelector("#fig svg");
      const rect = (node) => node.getBoundingClientRect();
      // Masks are the opaque backing behind a label, and a zone is the dashed
      // container drawn before everything else — a label may sit over either.
      // What must never happen is a label straddling the edge of a node box,
      // where the node's own fill clips it into a fragment.
      const boxes = [...svgEl.querySelectorAll("rect")]
        .filter(
          (node) =>
            !node.classList.contains("mask") && !node.classList.contains("zone"),
        )
        .map(rect);
      const out = [];

      for (const text of svgEl.querySelectorAll("text")) {
        const t = rect(text);
        if (t.width === 0) continue;
        // A label must sit inside some box, or clear every box entirely.
        // Straddling an edge is the failure this catches.
        for (const b of boxes) {
          const overlaps =
            t.right > b.left && t.left < b.right && t.bottom > b.top && t.top < b.bottom;
          const inside =
            t.left >= b.left - 0.5 && t.right <= b.right + 0.5 &&
            t.top >= b.top - 0.5 && t.bottom <= b.bottom + 0.5;
          if (overlaps && !inside) {
            out.push(`label ${JSON.stringify(text.textContent.trim())} crosses a box edge`);
            break;
          }
        }
      }

      const texts = [...svgEl.querySelectorAll("text")].map((node) => ({
        label: node.textContent.trim(),
        box: rect(node),
      }));
      for (let i = 0; i < texts.length; i += 1) {
        for (let j = i + 1; j < texts.length; j += 1) {
          const a = texts[i].box;
          const b = texts[j].box;
          if (a.right > b.left && a.left < b.right && a.bottom > b.top && a.top < b.bottom) {
            out.push(
              `labels ${JSON.stringify(texts[i].label)} and ${JSON.stringify(texts[j].label)} overlap`,
            );
          }
        }
      }
      return [...new Set(out)];
    });
    problems.push(...found);
  }

  // Contrast is checked in both modes: --fg-4 reads as a plausible label colour
  // in a light preview and misses AA in both, and an accent used as a text
  // colour only fails once the page flips.
  const lowContrast = await tab.evaluate(() => {
    const channel = (value) => {
      const c = value / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    };
    const luminance = (rgb) =>
      0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2]);
    const parse = (value) => {
      const parts = value.match(/[\d.]+/g);
      return parts ? parts.slice(0, 3).map(Number) : null;
    };

    const paper = parse(getComputedStyle(document.body).backgroundColor);
    const found = [];
    for (const text of document.querySelectorAll("#fig svg text")) {
      if (!text.textContent.trim()) continue;
      const ink = parse(getComputedStyle(text).fill);
      if (!ink || !paper) continue;
      const [a, b] = [luminance(ink), luminance(paper)].sort((x, y) => y - x);
      const ratio = (a + 0.05) / (b + 0.05);
      if (ratio < 4.5) {
        found.push(
          `label ${JSON.stringify(text.textContent.trim())} is ${ratio.toFixed(2)}:1 against the page — below AA`,
        );
      }
    }
    return [...new Set(found)];
  });
  problems.push(...lowContrast.map((problem) => `${mode}: ${problem}`));

  await tab.locator("#fig").screenshot({ path: `${outPrefix}-${mode}.png` });
  await tab.close();
}

await browser.close();

console.log(`wrote ${outPrefix}-light.png and ${outPrefix}-dark.png`);
if (problems.length > 0) {
  console.error(`\n${problems.length} problem(s) in ${source}:`);
  for (const problem of problems) console.error(`  - ${problem}`);
  process.exit(1);
}
console.log("clean: no label crosses a box edge, no two labels overlap, every label passes AA in both modes");
