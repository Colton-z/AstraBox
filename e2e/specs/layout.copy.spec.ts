import { expect, test } from "@playwright/test";

import { settle, stubApi } from "./layoutHelpers";

const ENGLISH: Array<[string, string]> = [
  [
    "/manage/agents",
    "Configure each Agent with its model, system prompt, MCP servers, Skills, Plugins, repositories, and runtime setup, then use it from any supported entry point.",
  ],
  [
    "/manage/deployments",
    "Start Agent conversations from a schedule, signed Webhook, external scheduler, or messaging platform.",
  ],
  [
    "/manage/assistants",
    "Create and manage personal Assistant workspaces. Each Assistant belongs to one user and keeps its files between conversations.",
  ],
  [
    "/manage/sessions",
    "Conversations started from an Agent. Open a Session to review its messages, current status, sandbox, and technical events.",
  ],
  [
    "/manage/environments",
    "Configure the Agent program, sandbox image, model connection, and network access used by Sessions.",
  ],
  [
    "/manage/sandboxes",
    "Current sandboxes reported by the configured backend. This page is read-only; Sessions create and delete sandboxes automatically.",
  ],
  [
    "/manage/errors",
    "Review recent Session and turn failures. Open a record to investigate the related Session.",
  ],
  ["/manage/system", "Health and active work for this AstraBox server."],
];

const CHINESE: Array<[string, string]> = [
  [
    "/manage/agents",
    "为 Agent 选择模型、系统提示词、MCP 服务、Skill、Plugin、代码仓库和运行设置，再从支持的入口使用它。",
  ],
  [
    "/manage/deployments",
    "通过定时计划、签名 Webhook、外部调度器或消息平台自动启动 Agent 会话。",
  ],
  [
    "/manage/assistants",
    "创建和管理个人 Assistant 工作区。每个 Assistant 只属于一位用户，并在不同会话之间保留文件。",
  ],
  [
    "/manage/sessions",
    "查看由 Agent 发起的会话。打开 Session 可查看消息、当前状态、沙箱和技术事件。",
  ],
  [
    "/manage/environments",
    "配置 Session 使用的 Agent 程序、沙箱镜像、模型连接和网络访问。",
  ],
  [
    "/manage/sandboxes",
    "查看当前沙箱后端报告的运行实例。此页面只读；Session 会自动创建和删除沙箱。",
  ],
  [
    "/manage/errors",
    "查看最近的 Session 和轮次失败记录。打开一条记录，可继续排查对应的 Session。",
  ],
  ["/manage/system", "查看此 AstraBox 服务的健康状态和当前任务。"],
];

test.describe("management copy", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("English page introductions explain the user's task", async ({ page }) => {
    await stubApi(page);

    for (const [path, copy] of ENGLISH) {
      await page.goto(path, { waitUntil: "domcontentloaded" });
      await settle(page);
      await expect(page.getByText(copy, { exact: true })).toBeVisible();
    }
  });

  test("Chinese page introductions explain the user's task", async ({ page }) => {
    await stubApi(page);

    await page.goto("/manage/agents", { waitUntil: "domcontentloaded" });
    await settle(page);
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("button", { name: "ZH", exact: true }).click();

    for (const [path, copy] of CHINESE) {
      await page.goto(path, { waitUntil: "domcontentloaded" });
      await settle(page);
      await expect(page.getByText(copy, { exact: true })).toBeVisible();
    }
  });

  test("error records render useful bilingual labels instead of i18n keys", async ({ page }) => {
    await stubApi(page);

    await page.goto("/manage/errors", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByRole("heading", { name: "Error records", exact: true })).toBeVisible();
    const errorTable = page.getByRole("table");
    await expect(errorTable).toBeVisible();
    await expect(page.getByRole("columnheader", { name: "Message", exact: true })).toBeVisible();
    await expect(errorTable.getByRole("row")).toHaveCount(3);
    await expect(errorTable.getByRole("cell").first()).toBeVisible();
    await expect(page.getByText("Sandbox startup timed out", { exact: true })).toBeVisible();
    await expect(page.getByText(/^manage:errors\./)).toHaveCount(0);

    await page.evaluate(() => window.localStorage.setItem("astrabox-lang", "zh"));
    await page.reload({ waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByRole("heading", { name: "错误记录", exact: true })).toBeVisible();
    await expect(page.getByRole("columnheader", { name: "错误内容", exact: true })).toBeVisible();
    await expect(page.getByText("轮次", { exact: true }).first()).toBeVisible();
    await expect(page.getByText(/^manage:errors\./)).toHaveCount(0);
  });

  test("Agent and Assistant choices explain what starts and what persists", async ({ page }) => {
    await stubApi(page);

    await page.goto("/agents", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText("Choose an Agent to start a conversation.", { exact: true })).toBeVisible();

    await page.goto("/assistants", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText(
      "Each Assistant is a private, long-lived workspace for one user. Its files remain available across conversations.",
      { exact: true },
    )).toBeVisible();
    const startButtons = page.getByRole("button", { name: "Start workspace", exact: true });
    const resumeButtons = page.getByRole("button", { name: "Resume workspace", exact: true });
    await expect(startButtons).toHaveCount(3);
    await expect(startButtons.first()).toBeVisible();
    await expect(resumeButtons).toHaveCount(3);
    await expect(resumeButtons.first()).toBeVisible();
  });

  test("Agent and Assistant choices use direct Chinese", async ({ page }) => {
    await page.addInitScript(() => window.localStorage.setItem("astrabox-lang", "zh"));
    await stubApi(page);

    await page.goto("/agents", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText("选择一个 Agent，开始新的会话。", { exact: true })).toBeVisible();

    await page.goto("/assistants", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText(
      "每个 Assistant 都是仅供一位用户使用的长期工作区，文件会在不同会话之间保留。",
      { exact: true },
    )).toBeVisible();
    const startButtons = page.getByRole("button", { name: "启动工作区", exact: true });
    const resumeButtons = page.getByRole("button", { name: "恢复工作区", exact: true });
    await expect(startButtons).toHaveCount(3);
    await expect(startButtons.first()).toBeVisible();
    await expect(resumeButtons).toHaveCount(3);
    await expect(resumeButtons.first()).toBeVisible();
  });

  test("Assistant workspace actions describe the actual operation", async ({ page }) => {
    await stubApi(page);

    await page.goto("/manage/assistants/as-001", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText("Not started", { exact: true }).first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Start workspace", exact: true })).toBeVisible();

    await page.goto("/manage/assistants/as-002", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText("Paused", { exact: true }).first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Resume workspace", exact: true })).toBeVisible();

    await page.goto("/manage/assistants/as-003", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByRole("button", { name: "Pause workspace", exact: true })).toBeVisible();
  });

  test("Agent form separates Environment settings from Credential Vaults", async ({ page }) => {
    await stubApi(page);

    await page.goto("/manage/agents/new", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText("Runtime and availability", { exact: true })).toBeVisible();
    await expect(page.getByText(
      "Select the saved runtime setup that provides the Agent program, sandbox settings, network access, and model connection. Credential Vaults are assigned separately.",
      { exact: true },
    )).toBeVisible();

    await page.evaluate(() => window.localStorage.setItem("astrabox-lang", "zh"));
    await page.reload({ waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText("运行设置", { exact: true })).toBeVisible();
    await expect(page.getByText(
      "选择已经保存的运行设置，其中包含 Agent 程序、沙箱设置、网络访问和模型连接。Credential Vault（凭证库）需要另行分配。",
      { exact: true },
    )).toBeVisible();
  });

  test("Assistant form separates Environment settings from Credential Vaults", async ({ page }) => {
    await stubApi(page);

    await page.goto("/manage/assistants/new", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText(
      "Select the Environment used by this Assistant. It supplies the Assistant engine, sandbox settings, and model connection. Credential Vaults are assigned separately.",
      { exact: true },
    )).toBeVisible();

    await page.evaluate(() => window.localStorage.setItem("astrabox-lang", "zh"));
    await page.reload({ waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText(
      "选择 Assistant 使用的 Environment（运行环境）。它提供 Assistant 引擎、沙箱设置和模型连接。Credential Vault（凭证库）需要另行分配。",
      { exact: true },
    )).toBeVisible();
  });

  test("sandbox sharing explains the security consequence directly", async ({ page }) => {
    await stubApi(page);

    await page.goto("/manage/environments/new", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText(
      "Shared mode does not safely isolate conversations belonging to users who do not trust one another.",
      { exact: false },
    )).toBeVisible();
    await expect(page.getByText(
      "Choose the service that creates and manages this Environment's sandboxes.",
      { exact: false },
    )).toBeVisible();
    await expect(page.getByText(
      "Outbound network rules for the sandbox, in JSON.",
      { exact: false },
    )).toBeVisible();
    await expect(page.getByText("security boundary", { exact: false })).toHaveCount(0);
    await expect(page.getByText("Where the boxes are created.", { exact: false })).toHaveCount(0);
    await expect(page.getByText(/\bbox(?:es)?\b/i)).toHaveCount(0);

    await page.evaluate(() => window.localStorage.setItem("astrabox-lang", "zh"));
    await page.reload({ waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText(
      "共用模式无法在互不信任的用户之间提供安全隔离。",
      { exact: false },
    )).toBeVisible();
    await expect(page.getByText(
      "选择负责创建和管理此 Environment 沙箱的后端。",
      { exact: false },
    )).toBeVisible();
    await expect(page.getByText(
      "沙箱的出站网络规则，使用 JSON 格式。",
      { exact: false },
    )).toBeVisible();
    await expect(page.getByText("箱子", { exact: false })).toHaveCount(0);
  });

  test("sandbox diagnostic errors tell the operator what to do next", async ({ page }) => {
    await stubApi(page);
    await page.route("**/api/v1/admin/sandboxes/*/diagnostics/*", async (route) => {
      await route.fulfill({
        status: 403,
        contentType: "application/json",
        body: JSON.stringify({ code: "FORBIDDEN", message: "Access denied", data: null }),
      });
    });

    const path = "/manage/sandboxes/sbx-000001-a3f9c2e1b7d4";
    await page.goto(path, { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText("Report output", { exact: true })).toBeVisible();
    await expect(page.getByText(
      "Check the request and sandbox backend using the error above, then retry.",
      { exact: true },
    )).toBeVisible();

    await page.evaluate(() => window.localStorage.setItem("astrabox-lang", "zh"));
    await page.reload({ waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText("报告内容", { exact: true })).toBeVisible();
    await expect(page.getByText(
      "请根据上方错误检查请求和沙箱后端，然后重试。",
      { exact: true },
    )).toBeVisible();
  });

  test("a malformed sandbox security response reports an error instead of blanking the page", async ({ page }) => {
    await stubApi(page);
    await page.route("**/api/v1/admin/sandboxes/*/security", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ code: "OK", message: "ok", data: null }),
      });
    });

    await page.goto("/manage/sandboxes/sbx-000001-a3f9c2e1b7d4", {
      waitUntil: "domcontentloaded",
    });
    await settle(page);
    // The record still renders around the failure: the sandbox's own sections
    // are there to read, and the malformed answer is reported inside the one
    // that asked for it rather than taking the page down with it.
    await expect(page.getByRole("heading", { name: "Overview", exact: true })).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Network and credentials", exact: true }),
    ).toBeVisible();
    await expect(page.getByText(/MALFORMED_RESPONSE/)).toBeVisible();
  });

  test("an unavailable sandbox security report states what could not be checked", async ({ page }) => {
    await stubApi(page);
    await page.route("**/api/v1/admin/sandboxes/*/security", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          code: "OK",
          message: "ok",
          data: {
            sandbox_id: "sbx-000001-a3f9c2e1b7d4",
            available: false,
            default_action: null,
            egress_rules: [],
            credential_names: [],
            binding_names: [],
            detail: "no egress proxy answered for this sandbox",
          },
        }),
      });
    });

    const path = "/manage/sandboxes/sbx-000001-a3f9c2e1b7d4";
    await page.goto(path, { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText(
      "AstraBox cannot verify this sandbox's security settings",
      { exact: true },
    )).toBeVisible();
    await expect(page.getByText("no egress proxy answered for this sandbox", { exact: true })).toBeVisible();

    await page.evaluate(() => window.localStorage.setItem("astrabox-lang", "zh"));
    await page.reload({ waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText("AstraBox 无法确认此沙箱的安全设置", { exact: true })).toBeVisible();
  });

  test("startup and paused-workspace messages explain the user-visible situation", async ({ page }) => {
    await stubApi(page);
    let scenario: "startup" | "paused" = "startup";
    await page.route("**/api/v1/sessions/s-001", async (route) => {
      const data = scenario === "startup"
        ? {
            session_id: "s-001",
            user_id: "u-1",
            template_name: "release-notes-writer-1",
            title: "Draft the release notes",
            state: "CREATING",
            source_type: "agent",
            agent_id: "ag-001",
            startup_progress: "waiting_for_startup_lease",
            created_at: "2026-07-27T12:34:56.000Z",
          }
        : {
            session_id: "s-001",
            user_id: "u-1",
            template_name: "ops-assistant",
            title: "Investigate the alert",
            state: "TERMINATED",
            source_type: "assistant",
            session_kind: "assistant",
            last_error: "sandbox TTL expired; workspace hibernated, will be rebuilt on next access",
            created_at: "2026-07-27T12:34:56.000Z",
          };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ code: "OK", message: "ok", data }),
      });
    });

    await page.goto("/sessions/s-001", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText("Waiting for another startup to finish", { exact: true })).toBeVisible();

    await page.evaluate(() => window.localStorage.setItem("astrabox-lang", "zh"));
    await page.reload({ waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText("正在等待另一个启动任务完成", { exact: true })).toBeVisible();

    scenario = "paused";
    await page.reload({ waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText(
      "沙箱超时后，工作区已暂停；下次打开时会自动恢复。",
      { exact: true },
    )).toBeVisible();

    await page.evaluate(() => window.localStorage.setItem("astrabox-lang", "en"));
    await page.reload({ waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText(
      "The sandbox timed out, so this workspace was paused. It will be restored the next time you open it.",
      { exact: true },
    )).toBeVisible();
  });

  // The console's system page names itself in both supported languages from
  // its canonical management address.
  test("the management console names itself in both languages", async ({ page }) => {
    await stubApi(page);

    await page.goto("/manage/system", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByRole("heading", { name: "System", exact: true })).toBeVisible();

    await page.evaluate(() => window.localStorage.setItem("astrabox-lang", "zh"));
    await page.goto("/manage/system", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByRole("heading", { name: "系统状态", exact: true })).toBeVisible();
  });

  test("console navigation uses task names and consistent product resource names", async ({ page }) => {
    await stubApi(page);

    await page.goto("/manage/deployments", { waitUntil: "domcontentloaded" });
    await settle(page);
    const navigationGroups = page.locator('[data-sidebar="group-label"]');
    await expect(navigationGroups).toHaveText([
      "Configure",
      "Operate",
      "Integrated services",
      "System",
    ]);
    await expect(page.getByRole("heading", { name: "Triggers", exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "Triggers", exact: true })).toBeVisible();
    const gatewayLink = page.getByRole("link", { name: /LiteLLM gateway/ });
    await expect(gatewayLink).toBeVisible();
    await expect(gatewayLink).toHaveAttribute("href", "/litellm");
    await expect(gatewayLink).toHaveAttribute("target", "_blank");
    const identityLink = page.getByRole("link", { name: /Casdoor identity/ });
    await expect(identityLink).toHaveAttribute("href", "https://identity.example.test");
    await expect(identityLink).toHaveAttribute("target", "_blank");
    const apiAccessLink = page.getByRole("link", { name: /AstraBox API API access/ });
    await expect(apiAccessLink).toHaveAttribute(
      "href",
      "https://identity.example.test/applications/astrabox/astrabox-api",
    );
    await expect(apiAccessLink).toHaveAttribute("target", "_blank");

    await page.evaluate(() => window.localStorage.setItem("astrabox-lang", "zh"));
    await page.reload({ waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(navigationGroups).toHaveText([
      "配置",
      "运行",
      "集成服务",
      "系统",
    ]);
    await expect(page.getByRole("heading", { name: "触发配置", exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: /LiteLLM 网关/ })).toBeVisible();
    await expect(page.getByRole("link", { name: /Casdoor 身份管理/ })).toBeVisible();
    await expect(page.getByRole("link", { name: /AstraBox API API 访问/ })).toBeVisible();

    await page.goto("/manage/agents", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByRole("heading", { name: "Agent", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "创建 Agent", exact: true })).toBeVisible();
    await expect(page.getByText("智能体", { exact: true })).toHaveCount(0);

    await page.goto("/agents", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByRole("tab", { name: "Agent", exact: true })).toBeVisible();

    await page.goto("/manage/assistants", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByRole("heading", { name: "Assistant", exact: true })).toBeVisible();
    await expect(page.getByText("工作区", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("工作空间", { exact: true })).toHaveCount(0);

    // The record page speaks the same vocabulary as its list. The heading is
    // the record's own name there (§1), and the standalone word is the list's
    // column header, so here the word is asserted inside the action that
    // carries it (启动工作区 on a NOT_MATERIALIZED workspace) — what matters
    // is the vocabulary, 工作区 and never 工作空间, not which control says it.
    await page.goto("/manage/assistants/as-004", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(page.getByText(/工作区/).first()).toBeVisible();
    await expect(page.getByText(/工作空间/)).toHaveCount(0);
  });

  test("console hides management links for services this deployment did not enable", async ({ page }) => {
    await stubApi(page, { integrations: [] });

    await page.goto("/manage/agents", { waitUntil: "domcontentloaded" });
    await settle(page);

    await expect(page.getByRole("link", { name: /LiteLLM gateway/ })).toHaveCount(0);
    await expect(page.getByRole("link", { name: /Casdoor identity/ })).toHaveCount(0);
    await expect(page.getByText("Integrated services", { exact: true })).toHaveCount(0);
  });

  test("subagent activity has a readable name in both languages", async ({ page }) => {
    await stubApi(page);
    await page.goto("/sessions/s-004", { waitUntil: "domcontentloaded" });
    await settle(page);

    await page.getByRole("tab", { name: "Agents", exact: true }).click();
    await page.getByTestId("subagent-agent-row").click();
    await expect(page.getByRole("dialog", { name: "Subagent activity" })).toBeVisible();
    await page.getByRole("button", { name: "Close subagent activity" }).click();

    await page.getByRole("button", { name: /Operator/ }).click();
    await page.getByRole("button", { name: "ZH", exact: true }).click();
    await page.goto("/sessions/s-004", { waitUntil: "domcontentloaded" });
    await settle(page);
    await page.getByRole("tab", { name: "Agents", exact: true }).click();
    await page.getByTestId("subagent-agent-row").click();
    await expect(page.getByRole("dialog", { name: "子 Agent 活动" })).toBeVisible();
    await expect(page.getByRole("button", { name: "关闭子 Agent 活动" })).toBeVisible();
  });
});
