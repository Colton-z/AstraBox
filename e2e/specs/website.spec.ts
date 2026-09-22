import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const SITE_PREFIX = "";

function sitemapRoutes(xml: string): string[] {
  return [...xml.matchAll(/<loc>([^<]+)<\/loc>/g)]
    .map((match) => new URL(match[1]).pathname)
    .map((path) => path.replace(SITE_PREFIX, "") || "/")
    .sort();
}

function localUrl(route: string): string {
  return route === "/" ? "." : `.${route}`;
}

function visibleStaticCopy(html: string): string {
  const namedEntities: Record<string, string> = {
    amp: "&",
    apos: "'",
    gt: ">",
    lt: "<",
    nbsp: " ",
    quot: '"',
  };
  return html
    .replace(/<!--[^]*?-->/g, " ")
    .replace(/<(code|pre|script|style)\b[^>]*>[^]*?<\/\1>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&#x([0-9a-f]+);/gi, (_, value: string) =>
      String.fromCodePoint(Number.parseInt(value, 16)),
    )
    .replace(/&#([0-9]+);/g, (_, value: string) =>
      String.fromCodePoint(Number.parseInt(value, 10)),
    )
    .replace(/&(amp|apos|gt|lt|nbsp|quot);/g, (_, name: string) => namedEntities[name]);
}

function staticHrefs(html: string): string[] {
  return [
    ...html.matchAll(/<a\b[^>]*\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')/gi),
  ].map((match) => (match[1] ?? match[2]).replaceAll("&amp;", "&"));
}

test.describe("public website", () => {
  test("readers can reach task guides in both languages and use their technical examples and next steps", async ({
    page,
  }) => {
    const categories = {
      start: ["Quick start", "快速开始"],
      build: ["Build Agent", "构建 Agent"],
      environment: ["Configure Agent environment", "配置 Agent 环境"],
      tasks: ["Delegate tasks", "委派任务"],
      integration: ["Integrate Agent", "集成 Agent"],
    };
    const guides = [
      { id: "quickstart", category: categories.start, content: "pre code", image: true },
      { id: "agent-skills", category: categories.build, content: "pre code", image: true },
      { id: "permission-modes", category: categories.build, content: "pre code" },
      { id: "environments", category: categories.environment, content: "table tbody tr" },
      { id: "container-reference", category: categories.environment, content: "pre code" },
      { id: "networking", category: categories.environment, content: "table tbody tr" },
      { id: "sessions", category: categories.tasks, content: "pre code" },
      { id: "events-stream", category: categories.tasks, content: "pre code" },
      { id: "working-with-repos", category: categories.tasks, content: "ol li", image: true },
      { id: "schedules", category: categories.integration, content: "pre code" },
    ];
    for (const locale of ["", "/zh-Hans"]) {
      await page.goto(localUrl(`${locale}/docs/overview`), { waitUntil: "domcontentloaded" });
      for (const guide of guides) {
        await test.step(`${locale || "en"}: ${guide.id}`, async () => {
          const route = `${SITE_PREFIX}${locale}/docs/${guide.id}`;
          const sidebar = page.locator(".theme-doc-sidebar-menu:visible");
          // Collapsed categories do not mount their child links until opened.
          const category = sidebar.getByRole("button", {
            name: guide.category[locale ? 1 : 0], exact: true,
          });
          await expect(category).toHaveCount(1);
          if (await category.getAttribute("aria-expanded") === "false") await category.click();
          await expect(category).toHaveAttribute("aria-expanded", "true");
          const entry = sidebar.locator(`a[href="${route}"]`);
          await expect(entry).toHaveCount(1);
          await expect(entry).toBeVisible();
          await entry.click();
          await expect(page).toHaveURL((url) => url.pathname === route);
          const article = page.locator("article");
          await expect(article.getByRole("heading", { level: 1 })).toBeVisible();
          const instructions = article.locator(guide.content);
          expect(await instructions.count(), `${route}: missing rendered technical content`).toBeGreaterThan(0);
          for (const instruction of await instructions.all()) {
            await expect(instruction).toBeVisible();
            expect((await instruction.innerText()).trim()).not.toBe("");
          }
          if (guide.image) {
            const screenshots = article.locator("img");
            expect(await screenshots.count(), `${route}: missing instructional screenshot`).toBeGreaterThan(0);
            for (const screenshot of await screenshots.all()) {
              await screenshot.scrollIntoViewIfNeeded();
              await expect(screenshot).toBeVisible();
              await expect.poll(() => screenshot.evaluate((element: HTMLImageElement) => (
                element.complete && element.naturalWidth > 0 && element.naturalHeight > 0
              ))).toBe(true);
            }
          }
          const next = page.locator(".pagination-nav__link--next");
          await expect(next).toHaveCount(1);
          await expect(next).toBeVisible();
          expect((await next.innerText()).trim()).not.toBe("");
          const nextHref = await next.getAttribute("href");
          expect(nextHref, `${route}: next task must stay in the reader's language`).toMatch(
            new RegExp(`^${SITE_PREFIX}${locale}/docs/`),
          );
          const nextPath = new URL(nextHref!, page.url()).pathname;
          expect(nextPath).not.toBe(route);
          await next.click();
          await expect(page).toHaveURL((url) => url.pathname === nextPath);
          await expect(page.locator("article").getByRole("heading", { level: 1 })).toBeVisible();
        });
      }
    }
  });

  test("readers can locate native JSON targets and follow localized configuration guides", async ({
    page,
  }) => {
    const localizedRows: { program: string; block: string; target: string[] }[][] = [];
    for (const locale of ["", "/zh-Hans"]) {
      const route = `${locale}/docs/authoring-agents`;
      await page.goto(localUrl(route), { waitUntil: "domcontentloaded" });
      const article = page.locator("article");
      await expect(article.getByRole("heading", { level: 1 })).toBeVisible();

      const table = article.getByRole("table").filter({
        has: page.getByRole("columnheader").filter({ hasText: "engine_options" }),
      });
      await expect(table).toHaveCount(1);
      const heading = table.locator("xpath=preceding::h3[1]");
      const headingId = await heading.getAttribute("id");
      expect(headingId, `${route}: native JSON section needs a working anchor`).toBeTruthy();
      const toc = page.locator(`.table-of-contents a[href="#${headingId}"]:visible`);
      await expect(toc).toHaveCount(1);
      await toc.click();
      await expect(page).toHaveURL((url) => decodeURIComponent(url.hash) === `#${headingId}`);
      await expect(heading).toBeInViewport();
      await table.scrollIntoViewIfNeeded();
      await expect(table).toBeVisible();

      const rows = table.locator("tbody tr");
      expect(await rows.count(), `${route}: native JSON reference must not be empty`).toBeGreaterThan(0);
      const entries: { program: string; block: string; target: string[] }[] = [];
      for (const row of await rows.all()) {
        const cells = row.getByRole("cell");
        await expect(cells).toHaveCount(3);
        for (const cell of await cells.all()) await expect(cell).toBeVisible();
        const program = (await cells.nth(0).innerText()).trim();
        const block = (await cells.nth(1).innerText()).trim();
        expect(program).not.toBe("");
        expect(block).toMatch(/^[a-z][a-z0-9_]*$/);
        const target = await cells.nth(2).locator("code").allTextContents();
        expect(target.length, `${route}: ${block} needs a concrete native target`).toBeGreaterThan(0);
        expect(target.every((value) => value.trim().length > 0)).toBe(true);
        entries.push({ program: program.toLowerCase(), block, target });
      }
      expect(new Set(entries.map(({ program, block }) => `${program}:${block}`)).size).toBe(entries.length);
      localizedRows.push(entries);

      for (const destination of ["models", "adding-tools"]) {
        const link = article.locator(`p a[href="${SITE_PREFIX}${locale}/docs/${destination}"]`);
        await expect(link).toHaveCount(1);
        await link.click();
        await expect(page).toHaveURL(new RegExp(`${SITE_PREFIX}${locale}/docs/${destination}$`));
        await expect(page.locator("article").getByRole("heading", { level: 1 })).toBeVisible();
        await page.goBack({ waitUntil: "domcontentloaded" });
        await expect(table).toHaveCount(1);
      }
    }
    expect(localizedRows[1]).toEqual(localizedRows[0]);
  });

  test("English and Chinese homepages pass automated WCAG A and AA checks in both themes", async ({
    page,
  }) => {
    for (const route of [".", "./zh-Hans"]) {
      for (const theme of ["light", "dark"] as const) {
        await page.goto(`${route}?docusaurus-theme=${theme}`, { waitUntil: "networkidle" });
        await expect(page.locator("html")).toHaveAttribute("data-theme", theme);

        const results = await new AxeBuilder({ page })
          .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
          .analyze();
        const violations = results.violations.map((violation) => ({
          help: violation.help,
          id: violation.id,
          impact: violation.impact,
          targets: violation.nodes.map((node) => node.target.join(" ")),
        }));
        expect(
          violations,
          `${route} (${theme}) has automatically detectable accessibility violations`,
        ).toEqual([]);
      }
    }
  });

  test("the homepage explains cloud Agents without internal program terminology", async ({ page }) => {
    await page.goto(".", { waitUntil: "networkidle" });
    await expect(page.getByRole("heading", { level: 1 })).toContainText(
      "The open-source alternative to Claude Managed Agents",
    );
    await expect(
      page.getByText("A cloud Agent powered by an installed Agent program.", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("AstraBox sandbox service", { exact: true }).first()).toBeVisible();
    await expect(
      page.getByText("connects to the Agent program", { exact: true }).first(),
    ).toBeVisible();
    const englishCopy = await page.locator("body").innerText();
    expect(englishCopy).not.toMatch(/Agent engines?|Agent harness|agent adapter|session connector/i);
    expect(englishCopy).not.toContain("Satori");

    await page.goto("./zh-Hans/", { waitUntil: "networkidle" });
    await expect(page.getByRole("heading", { level: 1 })).toContainText("开源替代");
    await expect(
      page.getByText("由已安装 Agent 程序驱动的云端 Agent。", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("沙箱内的 AstraBox 服务", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("连接 Agent 程序", { exact: true }).first()).toBeVisible();
    const chineseCopy = await page.locator("body").innerText();
    expect(chineseCopy).not.toMatch(/Agent 引擎|Agent Harness|Agent 适配器|会话连接组件/);
    expect(chineseCopy).not.toContain("Satori");

    await page.goto("./docs/architecture", { waitUntil: "networkidle" });
    await expect(page.getByText("Agent program", { exact: true }).first()).toBeVisible();
    const englishArchitecture = await page.locator("article").innerText();
    expect(englishArchitecture).not.toMatch(/Agent engine|Agent harness|session connector/i);

    await page.goto("./zh-Hans/docs/architecture", { waitUntil: "networkidle" });
    await expect(page.getByText("Agent 程序", { exact: true }).first()).toBeVisible();
    const architectureCopy = await page.locator("article").innerText();
    expect(architectureCopy).not.toMatch(/Agent 引擎|Agent Harness|会话连接组件/);
  });

  test("the Chinese homepage stays within a phone viewport and keeps the quickstart localized", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("./zh-Hans/", { waitUntil: "networkidle" });

    const overflow = await page.evaluate(() => ({
      viewport: document.documentElement.clientWidth,
      content: document.documentElement.scrollWidth,
    }));
    expect(overflow.content).toBeLessThanOrEqual(overflow.viewport + 1);

    await page
      .getByRole("banner")
      .getByRole("link", { name: "快速开始", exact: true })
      .click();
    await expect(page).toHaveURL(/\/zh-Hans\/docs\/quickstart$/);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();

    await page.goto("./zh-Hans/", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: "从部署到运行 Agent" })).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "AstraBox 让 Agent 持续在线，OpenSandbox 负责沙箱" }),
    ).toBeVisible();
  });

  test("homepage tables fill the cards that frame them", async ({ page }) => {
    // A table left at the theme's `display: block` takes the card's full width
    // while its columns size to content, so the card shows a dead strip on the
    // right. The columns, not the box, have to reach the edge.
    for (const route of [".", "./zh-Hans"]) {
      await page.goto(route, { waitUntil: "networkidle" });
      const gaps = await page.locator("table").evaluateAll((tables) =>
        tables.map((table) => {
          const last = table.querySelector("tr > *:last-child");
          if (!last) return 0;
          return Math.round(
            table.getBoundingClientRect().right - last.getBoundingClientRect().right,
          );
        }),
      );
      expect(gaps.length, `${route} renders no table`).toBeGreaterThan(0);
      expect(gaps, `${route} leaves empty width inside a table`).toEqual(gaps.map(() => 0));
    }
  });

  test("the homepage keeps its workflow and mobile architecture flow readable", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(".", { waitUntil: "networkidle" });

    const headings = await page.getByRole("heading", { level: 2 }).allTextContents();
    expect(headings.indexOf("From deployment to a running Agent")).toBeLessThan(
      headings.indexOf("AstraBox keeps the Agent available; OpenSandbox runs its sandbox"),
    );
    expect(
      headings.indexOf("AstraBox keeps the Agent available; OpenSandbox runs its sandbox"),
    ).toBeLessThan(
      headings.indexOf("Choose what you want to do next"),
    );

    const mobileFlow = page.getByTestId("mobile-architecture-flow");
    await expect(mobileFlow).toBeVisible();
    await expect(mobileFlow.getByRole("listitem")).toHaveCount(5);
    const smallestText = await mobileFlow.locator("li").evaluateAll((items) =>
      Math.min(...items.map((item) => Number.parseFloat(getComputedStyle(item).fontSize))),
    );
    expect(smallestText).toBeGreaterThanOrEqual(12);
  });

  test("every homepage next step links to one task page", async ({ page }) => {
    const cases = [
      {
        route: ".",
        heading: "Choose what you want to do next",
      },
      {
        route: "./zh-Hans/",
        heading: "选择接下来要做的事",
      },
    ];
    const expected = [
      "/docs/quickstart",
      "/docs/deploy",
      "/docs/api",
    ];

    for (const { route, heading } of cases) {
      await page.goto(route, { waitUntil: "domcontentloaded" });
      const section = page
        .getByRole("heading", { name: heading })
        .locator("xpath=ancestor::section");
      const hrefs = await section.locator("a[href]").evaluateAll((anchors) =>
        anchors.map((anchor) => (anchor as HTMLAnchorElement).href),
      );
      const normalized = hrefs.map((href) =>
        new URL(href).pathname
          .replace(/^\/zh-Hans(?=\/)/, ""),
      );
      expect(normalized).toEqual(expected);
      expect(hrefs.every((href) => !href.includes("#"))).toBe(true);
    }
  });

  test("English and Chinese publish the same pages without internal or development-history wording", async ({
    request,
  }) => {
    const [englishMap, chineseMap] = await Promise.all([
      request.get("sitemap.xml"),
      request.get("zh-Hans/sitemap.xml"),
    ]);
    expect(englishMap.ok()).toBe(true);
    expect(chineseMap.ok()).toBe(true);

    const englishRoutes = sitemapRoutes(await englishMap.text());
    const chineseRoutes = sitemapRoutes(await chineseMap.text()).map((route) =>
      route.replace(/^\/zh-Hans(?=\/|$)/, "") || "/",
    );
    expect(chineseRoutes).toEqual(englishRoutes);

    const problems: string[] = [];
    const wording = {
      en: /\bshared\s+MCP\b|\bAgent (?:engine|harness)\b|\b(?:seams?|wiring|spine|projection|technical debt)\b|\bfail(?:s|ed|ing)? loud(?:ly)?\b|\b(?:old behavior|new flow|instead of the old)\b|\bwe (?:found|discovered|fixed|changed|removed|added|learned)\b/i,
      zh: /共享\s*MCP|Agent 引擎|Agent Harness|接缝|接线|池里的箱子|取箱子|借箱子|回刷|大声失败|失败即响|唯一真相|钉住|新流程|替代旧流程|技术债|回归问题|我们(?:发现|修复|更改|删除|添加|得知)/i,
    };

    const jobs = englishRoutes.flatMap((route) =>
      (["en", "zh"] as const).map((locale) => ({ locale, route })),
    );
    for (let offset = 0; offset < jobs.length; offset += 8) {
      await Promise.all(jobs.slice(offset, offset + 8).map(async ({ locale, route }) => {
        const localizedRoute = locale === "zh" ? `/zh-Hans${route}` : route;
        const response = await request.get(localUrl(localizedRoute));
        if (!response.ok()) {
          problems.push(`${localizedRoute}: HTTP ${response.status()}`);
          return;
        }
        const copy = visibleStaticCopy(await response.text());
        const match = copy.match(wording[locale]);
        if (match) {
          problems.push(`${localizedRoute}: discouraged wording ${JSON.stringify(match[0])}`);
        }
      }));
    }

    expect(problems, problems.join("\n")).toEqual([]);
  });

  test("every internal website link resolves", async ({ request }) => {
    const englishMap = await request.get("sitemap.xml");
    expect(englishMap.ok()).toBe(true);

    const routes = sitemapRoutes(await englishMap.text());
    const targets = new Set<string>();
    const sourceProblems: string[] = [];
    const localizedRoutes = routes.flatMap((route) => [route, `/zh-Hans${route}`]);
    for (let offset = 0; offset < localizedRoutes.length; offset += 8) {
      await Promise.all(localizedRoutes.slice(offset, offset + 8).map(async (route) => {
        const response = await request.get(localUrl(route));
        if (!response.ok()) {
          sourceProblems.push(`${route}: HTTP ${response.status()}`);
          return;
        }
        const sourceUrl = new URL(response.url());
        for (const href of staticHrefs(await response.text())) {
          const target = new URL(href, sourceUrl);
          if (
            target.origin === sourceUrl.origin &&
            target.pathname.startsWith(`${SITE_PREFIX}/`)
          ) {
            target.hash = "";
            targets.add(target.href);
          }
        }
      }));
    }
    expect(sourceProblems, sourceProblems.join("\n")).toEqual([]);

    const broken: string[] = [];
    const sortedTargets = [...targets].sort();
    for (let offset = 0; offset < sortedTargets.length; offset += 12) {
      await Promise.all(sortedTargets.slice(offset, offset + 12).map(async (target) => {
        const response = await request.get(target);
        if (!response.ok()) broken.push(`${target}: HTTP ${response.status()}`);
      }));
    }
    expect(broken, broken.join("\n")).toEqual([]);
  });
});
