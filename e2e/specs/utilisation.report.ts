import { test } from "@playwright/test";
import { settle, stubApi } from "./layoutHelpers";

/**
 * SPACE UTILISATION REPORT — how much of the window each page actually uses.
 *
 * Not a gate. It prints a table and asserts nothing, because "how wide should
 * this page be" has no single right answer: a transcript is capped on purpose
 * at a readable line length, while a card grid capped the same way just wastes
 * the window. The number is here to make that a decision someone makes, rather
 * than one that happens.
 *
 * Two figures per page, both against the content well (the area the shell hands
 * to the page — window minus the rail and the top bar):
 *
 *   across  the page column's width over the well's width. Low means a cap that
 *           does not suit the content: the agent picker read 69% at 1920 and
 *           50% at 2560 while showing the same three cards it showed at 1440.
 *   down    the bottom of the lowest visible content over the well's height.
 *           Low means the page stops early and leaves bare background — the
 *           shape an empty state produces when its grid does not stretch to
 *           fill the container.
 *
 * Reading it: `across` below ~85% on a wide viewport is worth asking about;
 * `down` below ~60% usually means something is not filling its container. Both
 * can be legitimately low — a short form has nothing to put down there — so
 * treat them as questions, not failures.
 *
 *   npm --prefix e2e run utilisation
 */

const VIEWPORTS = [
  { w: 1280, h: 800 },
  { w: 1440, h: 900 },
  { w: 1920, h: 1080 },
  { w: 2560, h: 1440 },
];

const PAGES = [
  { path: "/agents", name: "agent picker" },
  { path: "/assistants", name: "assistant picker" },
  { path: "/sessions/s-001", name: "session" },
  { path: "/manage/agents", name: "console · agents" },
  { path: "/manage/deployments", name: "console · deployments" },
  { path: "/manage/assistants", name: "console · assistants" },
  { path: "/manage/sessions", name: "console · sessions" },
  { path: "/manage/sandboxes", name: "console · sandboxes" },
  { path: "/manage/environments", name: "console · environments" },
  { path: "/manage/agents/new", name: "console · agent create page" },
];

type Row = { page: string; w: number; across: number | null; down: number | null; note: string };

test("space utilisation", async ({ browser }) => {
  const rows: Row[] = [];

  for (const vp of VIEWPORTS) {
    const ctx = await browser.newContext({ viewport: { width: vp.w, height: vp.h } });
    for (const page of PAGES) {
      const p = await ctx.newPage();
      await stubApi(p);
      try {
        await p.goto(page.path, { waitUntil: "domcontentloaded", timeout: 30_000 });
        await settle(p);
        await p.waitForTimeout(500);

        const m = await p.evaluate(() => {
          const well = document.querySelector('[data-slot="app-content"]');
          if (!well) return null;
          const wellBox = well.getBoundingClientRect();

          // The page column is whatever PageShell laid out; pages that predate
          // it (the session view) are measured by their own widest child.
          const column = document.querySelector('[data-slot="page-measure"]');
          const across = column
            ? column.getBoundingClientRect().width / wellBox.width
            : (() => {
                let widest = 0;
                for (const el of well.querySelectorAll<HTMLElement>(":scope > *")) {
                  widest = Math.max(widest, el.getBoundingClientRect().width);
                }
                return widest / wellBox.width;
              })();

          // Lowest visible content, ignoring the shell's own chrome.
          let lowest = wellBox.top;
          const walk = (el: Element) => {
            for (const c of el.children) {
              const r = c.getBoundingClientRect();
              const s = getComputedStyle(c);
              if (r.height > 0 && r.width > 0 && s.visibility !== "hidden" && s.display !== "none") {
                if (r.bottom <= wellBox.bottom + 1) lowest = Math.max(lowest, r.bottom);
                walk(c);
              }
            }
          };
          walk(well);

          return {
            across,
            down: (lowest - wellBox.top) / wellBox.height,
            wellW: Math.round(wellBox.width),
            colW: column ? Math.round(column.getBoundingClientRect().width) : null,
          };
        });

        rows.push(
          m
            ? {
                page: page.name,
                w: vp.w,
                across: m.across,
                down: m.down,
                note: m.colW ? `${m.colW}/${m.wellW}px` : `${m.wellW}px well`,
              }
            : { page: page.name, w: vp.w, across: null, down: null, note: "no shell" },
        );
      } catch (e) {
        rows.push({ page: page.name, w: vp.w, across: null, down: null, note: `failed: ${String(e).slice(0, 40)}` });
      }
      await p.close();
    }
    await ctx.close();
  }

  // ── print ────────────────────────────────────────────────────────────────
  const pct = (v: number | null) => (v === null ? "  —  " : `${String(Math.round(v * 100)).padStart(3)}%`);
  const flag = (v: number | null, floor: number) => (v !== null && v < floor ? "◂" : " ");

  const widths = VIEWPORTS.map((v) => v.w);
  const header = `${"page".padEnd(24)}${widths.map((w) => `${String(w).padStart(6)}      `).join("")}`;
  process.stdout.write(`\n${"═".repeat(header.length)}\n`);
  process.stdout.write("SPACE UTILISATION — across (column/well) · down (content/well)\n");
  process.stdout.write(`${"─".repeat(header.length)}\n${header}\n`);

  for (const page of PAGES) {
    const mine = rows.filter((r) => r.page === page.name);
    const across = widths
      .map((w) => {
        const r = mine.find((x) => x.w === w);
        return `${pct(r?.across ?? null)}${flag(r?.across ?? null, 0.85)}     `;
      })
      .join("");
    const down = widths
      .map((w) => {
        const r = mine.find((x) => x.w === w);
        return `${pct(r?.down ?? null)}${flag(r?.down ?? null, 0.6)}     `;
      })
      .join("");
    process.stdout.write(`${page.name.padEnd(24)}${across}\n`);
    process.stdout.write(`${"".padEnd(24)}${down}   ↓\n`);
  }
  process.stdout.write(`${"═".repeat(header.length)}\n`);
  process.stdout.write("◂ marks across < 85% or down < 60% — a question, not a verdict.\n\n");
});
