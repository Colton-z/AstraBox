import { createHmac } from "node:crypto";

import {
  expect,
  test,
  type APIRequestContext,
  type Browser,
  type BrowserContext,
  type Page,
} from "@playwright/test";

import {
  directMcpCallCommand,
  resolvedMcpUrl,
} from "./helpers/direct-mcp";

const jwtSecret = process.env.ASTRABOX_E2E_JWT_SECRET?.trim();
const baseURL = process.env.ASTRABOX_E2E_BASE_URL?.trim();
const liveMcpURL = process.env.ASTRABOX_E2E_LITELLM_MCP_URL?.trim();
const liveSkillURL =
  process.env.ASTRABOX_E2E_SKILL_URL?.trim()
  || "https://github.com/mattpocock/skills/tree/main/skills/productivity/grill-me";
const liveSkillFileURL =
  process.env.ASTRABOX_E2E_SKILL_FILE_URL?.trim()
  || "https://raw.githubusercontent.com/mattpocock/skills/main/skills/productivity/grill-me/SKILL.md";

if (!jwtSecret) {
  throw new Error("ASTRABOX_E2E_JWT_SECRET is required for the extension-console suite");
}
if (!baseURL) {
  throw new Error("ASTRABOX_E2E_BASE_URL is required for the extension-console suite");
}
if (!liveMcpURL) {
  throw new Error(
    "ASTRABOX_E2E_LITELLM_MCP_URL must point to a live Streamable HTTP MCP server",
  );
}

type ApiEnvelope<T> = { data: T };

function encode(value: unknown): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function identityToken(userId: string, groups: string[] = []): string {
  const header = encode({ alg: "HS256", typ: "JWT" });
  const payload = encode({
    sub: userId,
    email: `${userId}@astrabox.invalid`,
    name: userId,
    groups,
    iat: Math.floor(Date.now() / 1000),
    exp: Math.floor(Date.now() / 1000) + 3600,
  });
  const signature = createHmac("sha256", jwtSecret!).update(`${header}.${payload}`).digest("base64url");
  return `${header}.${payload}.${signature}`;
}

async function api<T>(
  request: APIRequestContext,
  method: "get" | "post" | "put" | "delete",
  path: string,
  token: string,
  data?: unknown,
): Promise<T> {
  const response = await request[method](path, {
    headers: { Authorization: `Bearer ${token}` },
    data,
  });
  const body = await response.text();
  expect(response.ok(), `${method.toUpperCase()} ${path} returned ${response.status()}: ${body}`).toBeTruthy();
  return (JSON.parse(body) as ApiEnvelope<T>).data;
}

async function sessionStatus(
  browser: Browser,
  token: string,
  agentId: string,
  section = "skills",
): Promise<number> {
  const context = await browser.newContext({
    extraHTTPHeaders: { Authorization: `Bearer ${token}` },
  });
  try {
    const response = await context.request.post(
      `/api/v1/agents/${encodeURIComponent(agentId)}/extension-console/session`,
      { data: { section } },
    );
    return response.status();
  } finally {
    await context.close();
  }
}

async function authenticateAstraBoxRequests(
  context: BrowserContext,
  token: string,
): Promise<void> {
  await context.route("**/*", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (!pathname.startsWith("/api/v1/")) {
      await route.continue();
      return;
    }
    await route.continue({
      headers: {
        ...route.request().headers(),
        authorization: `Bearer ${token}`,
      },
    });
  });
}

async function waitForSandbox(
  request: APIRequestContext,
  token: string,
  sessionId: string,
  timeoutMs = 180_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const session = await api<Record<string, unknown>>(
      request,
      "get",
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
      token,
    );
    if (String(session.sandbox_id || "")) return;
    const state = String(session.state || "");
    if (["TERMINATED", "FAILED", "DELETED"].includes(state)) {
      throw new Error(
        `Session ${sessionId} reached ${state}: ${String(session.last_error || "no error")}`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(`Session ${sessionId} did not provision a sandbox`);
}

async function terminalCommand(
  request: APIRequestContext,
  token: string,
  sessionId: string,
  command: string,
): Promise<{ status: number; body: string }> {
  const response = await request.post(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/terminal/stream`,
    {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "text/event-stream",
      },
      data: { command },
      timeout: 120_000,
    },
  );
  return { status: response.status(), body: await response.text() };
}

type TerminalEvent = {
  type?: string;
  text?: string;
  exit_code?: number;
};

function terminalEvents(body: string): TerminalEvent[] {
  return body
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data: "))
    .flatMap((line) => {
      try {
        return [JSON.parse(line.slice("data: ".length)) as TerminalEvent];
      } catch {
        return [];
      }
    });
}

async function selectCatalogItem(
  page: Page,
  fieldLabel: string,
  searchPlaceholder: string,
  itemName: string,
): Promise<void> {
  // The combobox role sits on the chips input itself, and its placeholder
  // switches between the select and search wordings with the chip count — so
  // type into the role element and never address the placeholder. Key-by-key
  // typing is what the control receives from a person; the select-all first
  // replaces anything already displayed in the field.
  const select = page.getByRole("combobox", { name: fieldLabel, exact: true });
  await select.click();
  await page.keyboard.press("ControlOrMeta+a");
  await select.pressSequentially(itemName);
  await page.getByRole("option", { name: itemName, exact: true }).click();
  await page.keyboard.press("Escape");
  // The combobox role sits on the input; a chosen item shows as a chip beside
  // it, so the evidence lives on the chips container, not in input text.
  await expect(
    select.locator('xpath=ancestor::div[@data-slot="combobox-chips"]'),
  ).toContainText(itemName);
}

test("extension management follows the selected Agent's managers and stays route-scoped", async ({
  browser,
  request,
}) => {
  const suffix = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const owner = identityToken(`extension-owner-${suffix}`);
  const coadmin = identityToken(`extension-coadmin-${suffix}`);
  const viewer = identityToken(`extension-viewer-${suffix}`);
  const stranger = identityToken(`extension-stranger-${suffix}`);
  const platformAdmin = identityToken(`extension-platform-admin-${suffix}`, ["astrabox-admin"]);
  let agentId = "";
  let unrelatedAgentId = "";
  let sessionId = "";

  try {
    // The general write contract rejects access fields; policy is written
    // through the dedicated manager-authorized access endpoint.
    const agent = await api<{ agent_id: string; version: number }>(
      request,
      "post",
      "/api/v1/agents",
      owner,
      {
        name: `extension-owner-agent-${suffix}`,
        model: "extension-e2e-model",
        environment_name: "claude-code",
      },
    );
    agentId = agent.agent_id;
    await api(
      request,
      "put",
      `/api/v1/agents/${encodeURIComponent(agentId)}/access`,
      owner,
      {
        visibility: "allowlist",
        admins: [`extension-coadmin-${suffix}`],
        allowed_user_ids: [`extension-viewer-${suffix}`],
      },
    );

    // A created Agent is private by default.
    const unrelated = await api<{ agent_id: string }>(
      request,
      "post",
      "/api/v1/agents",
      stranger,
      {
        name: `extension-unrelated-agent-${suffix}`,
        model: "extension-e2e-model",
        environment_name: "claude-code",
      },
    );
    unrelatedAgentId = unrelated.agent_id;

    expect(await sessionStatus(browser, owner, agentId)).toBe(200);
    expect(await sessionStatus(browser, coadmin, agentId)).toBe(200);
    expect(await sessionStatus(browser, platformAdmin, agentId)).toBe(200);

    // Being allowed to use an Agent is not permission to edit its extensions.
    expect(await sessionStatus(browser, viewer, agentId)).toBe(403);
    // Managing some other Agent must not grant a global extension-admin role.
    expect(await sessionStatus(browser, stranger, agentId)).toBe(403);

    const viewerPageContext = await browser.newContext();
    await authenticateAstraBoxRequests(viewerPageContext, viewer);
    const viewerPage = await viewerPageContext.newPage();
    await viewerPage.goto(`/manage/agents/${encodeURIComponent(agentId)}`);
    await expect(viewerPage.getByRole("heading", { name: `extension-owner-agent-${suffix}` })).toBeVisible();
    await expect(viewerPage.getByRole("button", { name: "Manage remote MCP servers", exact: true })).toHaveCount(0);
    await expect(viewerPage.getByRole("button", { name: "Manage Skills", exact: true })).toHaveCount(0);
    await expect(viewerPage.getByRole("heading", { name: "MCP servers and Skills", exact: true })).toHaveCount(0);
    await viewerPageContext.close();

    const adminPageContext = await browser.newContext();
    await authenticateAstraBoxRequests(adminPageContext, platformAdmin);
    const adminPage = await adminPageContext.newPage();
    await adminPage.goto(`/manage/agents/${encodeURIComponent(agentId)}`);
    await expect(adminPage.getByRole("heading", { name: "MCP servers and Skills", exact: true })).toBeVisible();
    await expect(adminPage.getByRole("combobox", { name: "MCP servers", exact: true })).toBeVisible();
    await expect(adminPage.getByRole("combobox", { name: "Skills", exact: true })).toBeVisible();
    await expect(adminPage.getByRole("button", { name: "Manage remote MCP servers", exact: true })).toBeVisible();
    await expect(adminPage.getByRole("button", { name: "Manage Skills", exact: true })).toBeVisible();
    const extensionEntry = await adminPage.getByTestId("agent-extension-entry").boundingBox();
    expect(extensionEntry?.height).toBeGreaterThan(100);
    expect(extensionEntry?.width).toBeGreaterThan(500);
    await adminPageContext.close();

    const context = await browser.newContext();
    await authenticateAstraBoxRequests(context, owner);
    // Casdoor and other identity services commonly set a root-path cookie
    // named `token`. Cookies ignore ports, so that cookie is also sent to a
    // loopback AstraBox deployment. Extension access must use AstraBox's
    // namespaced cookie and leave the identity provider's cookie untouched.
    await context.addCookies([
      {
        name: "token",
        value: "unrelated-identity-provider-cookie",
        url: new URL("/", baseURL).toString(),
      },
    ]);
    const page = await context.newPage();
    await page.goto(`/manage/agents/${encodeURIComponent(agentId)}`);
    await expect(page.getByRole("heading", { name: "MCP servers and Skills", exact: true })).toBeVisible();

    // Model options come from the selected Environment's real LiteLLM catalog,
    // and the value selected in AstraBox is persisted on this Agent.
    const modelCatalog = await api<{ models: string[] }>(
      request,
      "get",
      "/api/v1/agent-configuration/environments/claude-code/models",
      owner,
    );
    expect(modelCatalog.models.length).toBeGreaterThan(0);
    const selectedModel = modelCatalog.models[0];
    const modelCard = page
      .getByRole("heading", { name: "Model and prompt", exact: true })
      .locator("xpath=ancestor::section");
    const modelSelect = modelCard.getByRole("combobox", { name: "Model", exact: true });
    await modelSelect.click();
    await page.getByPlaceholder("Search models or enter a model id", { exact: true }).click();
    await page.keyboard.press("ControlOrMeta+a");
    await page.getByPlaceholder("Search models or enter a model id", { exact: true }).pressSequentially(selectedModel);
    await page.getByRole("option", { name: selectedModel, exact: true }).click();
    await modelCard.getByRole("button", { name: "Save", exact: true }).click();
    await expect(modelCard.getByText("Saved", { exact: true })).toBeVisible();
    // The model combobox IS an input now; the chosen id is its value.
    await expect(modelSelect).toHaveValue(selectedModel);
    const agentsAfterModelSave = await api<Array<{ agent_id: string; model?: string }>>(
      request,
      "get",
      "/api/v1/agents",
      owner,
    );
    expect(agentsAfterModelSave.find((item) => item.agent_id === agentId)?.model).toBe(selectedModel);

    // The catalogue is the gateway's own application and opens in a tab of
    // its own; the Agent page and its half-typed cards stay behind.
    const [gateway] = await Promise.all([
      context.waitForEvent("page"),
      page.getByRole("button", { name: "Manage Skills", exact: true }).click(),
    ]);
    await expect(gateway).toHaveURL(/\/ui\/skills\/?/);
    await expect(gateway.getByRole("heading", { name: "Skills", exact: true })).toBeVisible();
    await expect(gateway.getByText("Add Skill", { exact: false })).toBeVisible();
    const identityCookie = (await context.cookies()).find(
      (cookie) => cookie.name === "token" && cookie.path === "/",
    );
    expect(identityCookie?.value).toBe("unrelated-identity-provider-cookie");
    await gateway.getByText("Add Skill", { exact: false }).first().click();
    const liveSkill = await request.get(liveSkillFileURL);
    expect(liveSkill.status(), `could not read live Skill source ${liveSkillFileURL}`).toBe(200);
    expect(await liveSkill.text()).toMatch(/name:\s*grill-me/i);
    const skillName = `astrabox-e2e-${suffix}`;
    await gateway.getByLabel("GitHub URL", { exact: true }).fill(liveSkillURL);
    await gateway.getByLabel("Skill Name", { exact: true }).fill(skillName);
    const skillCreateResponsePromise = gateway.waitForResponse(
      (response) => response.url().includes("/claude-code/plugins")
        && response.request().method() === "POST",
    );
    await gateway.getByRole("button", { name: "Add Skill", exact: true }).click();
    const skillCreateResponse = await skillCreateResponsePromise;
    const skillCreateBody = await skillCreateResponse.text();
    expect(
      skillCreateResponse.ok(),
      `live Skill registration returned ${skillCreateResponse.status()}: ${skillCreateBody}`,
    ).toBeTruthy();
    await expect(gateway.getByText(skillName, { exact: true })).toBeVisible();
    const registeredSkill = await gateway.evaluate(async () => {
      const uiToken = window.sessionStorage.getItem("token") || "";
      const encodedPayload = uiToken.split(".")[1] || "";
      const normalized = encodedPayload.replaceAll("-", "+").replaceAll("_", "/");
      const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
      const claims = JSON.parse(window.atob(padded)) as { key?: string };
      const response = await fetch("/claude-code/plugins", {
        headers: { Authorization: `Bearer ${claims.key || ""}` },
      });
      return { status: response.status, body: await response.text() };
    });
    expect(registeredSkill.status).toBe(200);
    const registeredSkills = JSON.parse(registeredSkill.body) as {
      plugins: Array<{
        name: string;
        source?: { source?: string; repo?: string };
      }>;
    };
    expect(registeredSkills.plugins).toEqual(expect.arrayContaining([
      expect.objectContaining({
        name: skillName,
        source: expect.objectContaining({
          source: "git-subdir",
          url: "https://github.com/mattpocock/skills",
          path: "skills/productivity/grill-me",
        }),
      }),
    ]));
    const skillRow = gateway.getByRole("row", { name: new RegExp(skillName) });

    await gateway.goto("/ui/mcp-servers/");
    await expect(gateway.getByRole("main").getByText("MCP Servers", { exact: true })).toBeVisible();
    const mcpLogo = await context.request.get("/ui/assets/logos/mcp_logo.png");
    expect(mcpLogo.status()).toBe(200);
    await gateway.getByRole("button", { name: /Add New MCP Server/i }).click();
    await expect(gateway.getByText(/Failed to load servers/i)).toBeHidden();
    await gateway.getByRole("button", { name: /Custom Server/i }).click();
    const mcpName = `astrabox_e2e_mcp_${suffix.replaceAll("-", "_")}`;
    await gateway.getByLabel("MCP Server Name", { exact: true }).fill(mcpName);
    await gateway.getByLabel("Transport Type", { exact: true }).click();
    await gateway.getByText("Streamable HTTP (Recommended)", { exact: true }).click();
    await gateway.getByLabel("MCP Server URL", { exact: true }).fill(liveMcpURL);
    const mcpDialog = gateway.getByRole("dialog", { name: /Add New MCP Server/i });
    await mcpDialog.getByRole("combobox").nth(1).click();
    await gateway.locator('.ant-select-item-option[title="None"]:visible').click();
    await mcpDialog.getByRole("button", { name: "Add MCP Server", exact: true }).click();
    const mcpCard = gateway
      .getByTestId("mcp-servers-grid")
      .getByRole("button", { name: new RegExp(mcpName) })
      .first();
    await expect(mcpCard).toBeVisible({ timeout: 20_000 });
    const liveMcp = await gateway.evaluate(async ({ serverId }) => {
      const uiToken = window.sessionStorage.getItem("token") || "";
      const encodedPayload = uiToken.split(".")[1] || "";
      const normalized = encodedPayload.replaceAll("-", "+").replaceAll("_", "/");
      const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
      const claims = JSON.parse(window.atob(padded)) as { key?: string };
      const listResponse = await fetch(
        `/mcp-rest/tools/list?server_id=${encodeURIComponent(serverId)}`,
        {
          headers: { Authorization: `Bearer ${claims.key || ""}` },
        },
      );
      const listBody = await listResponse.text();
      const callResponse = await fetch("/mcp-rest/tools/call", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${claims.key || ""}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          server_id: serverId,
          name: "echo",
          arguments: { text: "browser-live" },
        }),
      });
      return {
        listStatus: listResponse.status,
        listBody,
        callStatus: callResponse.status,
        callBody: await callResponse.text(),
      };
    }, { serverId: mcpName });
    expect(liveMcp.listStatus, liveMcp.listBody).toBe(200);
    expect(liveMcp.listBody).toContain("echo");
    expect(liveMcp.callStatus, liveMcp.callBody).toBe(200);
    expect(liveMcp.callBody).toContain("MCP_E2E_OK:browser-live");
    await expect(gateway.getByText("Virtual Keys", { exact: true })).toBeHidden();
    await expect(gateway.getByText("Models + Endpoints", { exact: true })).toBeHidden();
    await expect(gateway.getByText("Teams", { exact: true })).toBeHidden();
    await expect(gateway.getByRole("tab", { name: /Submitted MCPs/i })).toBeHidden();

    const forbidden = await gateway.evaluate(async () => {
      const [keys, inference] = await Promise.all([
        fetch("/key/generate", { method: "POST", body: "{}" }),
        fetch("/v1/chat/completions", { method: "POST", body: "{}" }),
      ]);
      return { keys: keys.status, inference: inference.status };
    });
    expect(forbidden).toEqual({ keys: 403, inference: 403 });

    await page.goto(`/manage/agents/${encodeURIComponent(agentId)}`);
    await selectCatalogItem(page, "MCP servers", "Search MCP servers", mcpName);
    await selectCatalogItem(page, "Skills", "Search Skills", skillName);
    const extensionCard = page
      .getByRole("heading", { name: "MCP servers and Skills", exact: true })
      .locator("xpath=ancestor::section");
    await extensionCard.getByRole("button", { name: "Save MCP servers and Skills", exact: true }).click();
    await expect(extensionCard.getByText("Saved", { exact: true })).toBeVisible();
    await expect(
      extensionCard.getByRole("combobox", { name: "MCP servers", exact: true })
        .locator('xpath=ancestor::div[@data-slot="combobox-chips"]'),
    ).toContainText(mcpName);
    await expect(
      extensionCard.getByRole("combobox", { name: "Skills", exact: true })
        .locator('xpath=ancestor::div[@data-slot="combobox-chips"]'),
    ).toContainText(skillName);

    const assignment = await api<{
      selected_mcp_server_ids: string[];
      selected_skill_ids: string[];
    }>(
      request,
      "get",
      `/api/v1/agents/${encodeURIComponent(agentId)}/extensions`,
      owner,
    );
    expect(assignment.selected_mcp_server_ids).toHaveLength(1);
    expect(assignment.selected_skill_ids).toHaveLength(1);

    const conversation = await api<{ session_id: string }>(
      request,
      "post",
      `/api/v1/agents/${encodeURIComponent(agentId)}/conversations`,
      owner,
    );
    sessionId = conversation.session_id;
    await waitForSandbox(request, owner, sessionId);

    const installedSkill = await terminalCommand(
      request,
      owner,
      sessionId,
      // Engine state stays under the workload account even though the Session
      // workspace has the stable path /workspace.
      "test -f \"$HOME/.claude/skills/grill-me/SKILL.md\" && grep -m1 '^name: grill-me' \"$HOME/.claude/skills/grill-me/SKILL.md\"",
    );
    expect(installedSkill.status, installedSkill.body).toBe(200);
    const installedSkillEvents = terminalEvents(installedSkill.body);
    expect(installedSkillEvents).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: "exit", exit_code: 0 }),
    ]));
    expect(
      installedSkillEvents
        .filter((event) => event.type === "stdout")
        .map((event) => event.text || "")
        .join(""),
    ).toContain("name: grill-me");

    const runtimeDetail = await api<Record<string, unknown>>(
      request,
      "get",
      `/api/v1/admin/sessions/${encodeURIComponent(sessionId)}/detail`,
      platformAdmin,
    );
    const runtimeMcp = await terminalCommand(
      request,
      owner,
      sessionId,
      directMcpCallCommand(
        resolvedMcpUrl(runtimeDetail, mcpName),
        `${mcpName}-echo`,
        "agent-runtime",
      ),
    );
    expect(runtimeMcp.status, runtimeMcp.body).toBe(200);
    const runtimeMcpEvents = terminalEvents(runtimeMcp.body);
    expect(runtimeMcpEvents).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: "exit", exit_code: 0 }),
    ]));
    expect(
      runtimeMcpEvents
        .filter((event) => event.type === "stdout")
        .map((event) => event.text || "")
        .join(""),
    ).toContain("MCP_E2E_OK:agent-runtime");

    await gateway.goto("/ui/skills/");
    const cleanupSkillRow = gateway.getByRole("row", { name: new RegExp(skillName) });
    await cleanupSkillRow.locator("button").last().click();
    await gateway.getByRole("button", { name: "Delete", exact: true }).click();
    await expect(cleanupSkillRow).toBeHidden({ timeout: 20_000 });
    await gateway.goto("/ui/mcp-servers/");
    const cleanupMcpCard = gateway
      .getByTestId("mcp-servers-grid")
      .getByRole("button", { name: new RegExp(mcpName) })
      .first();
    await cleanupMcpCard.getByRole("button", { name: "Server actions" }).click();
    await gateway.getByText("Delete", { exact: true }).last().click();
    await page
      .getByRole("dialog", { name: /Delete MCP Server/i })
      .getByRole("button", { name: "Delete", exact: true })
      .click();
    await expect(cleanupMcpCard).toBeHidden({ timeout: 20_000 });
    await context.close();

    const viewerContext = await browser.newContext({
      extraHTTPHeaders: { Authorization: `Bearer ${viewer}` },
    });
    const copiedLink = await viewerContext.request.get("/ui/skills/");
    expect(copiedLink.status()).toBe(403);
    await viewerContext.close();
  } finally {
    if (sessionId) {
      await request.delete(`/api/v1/sessions/${encodeURIComponent(sessionId)}`, {
        headers: { Authorization: `Bearer ${owner}` },
      }).catch(() => undefined);
    }
    if (unrelatedAgentId) {
      await request.delete(`/api/v1/agents/${encodeURIComponent(unrelatedAgentId)}`, {
        headers: { Authorization: `Bearer ${stranger}` },
      }).catch(() => undefined);
    }
    if (agentId) {
      await request.delete(`/api/v1/agents/${encodeURIComponent(agentId)}`, {
        headers: { Authorization: `Bearer ${owner}` },
      }).catch(() => undefined);
    }
  }
});
