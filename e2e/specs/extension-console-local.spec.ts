import {
  expect,
  test,
  type APIRequestContext,
  type BrowserContext,
  type Page,
} from "@playwright/test";

import {
  directMcpCallCommand,
  resolvedMcpUrl,
} from "./helpers/direct-mcp";

const runLocalIdentity = process.env.ASTRABOX_E2E_AUTH_MODE?.trim() === "local";
const baseURL = process.env.ASTRABOX_E2E_BASE_URL?.trim();
const liveMcpURL = process.env.ASTRABOX_E2E_LITELLM_MCP_URL?.trim();
const liveSkillURL =
  process.env.ASTRABOX_E2E_SKILL_URL?.trim()
  || "https://github.com/mattpocock/skills/tree/main/skills/productivity/grill-me";
const liveSkillFileURL =
  process.env.ASTRABOX_E2E_SKILL_FILE_URL?.trim()
  || "https://raw.githubusercontent.com/mattpocock/skills/main/skills/productivity/grill-me/SKILL.md";

test.skip(!runLocalIdentity, "set ASTRABOX_E2E_AUTH_MODE=local for the no-auth deployment");

type ApiEnvelope<T> = { data: T };

async function api<T>(
  request: APIRequestContext,
  method: "get" | "post" | "delete",
  path: string,
  data?: unknown,
): Promise<T> {
  const response = await request[method](path, data === undefined ? undefined : { data });
  const body = await response.text();
  expect(response.ok(), `${method.toUpperCase()} ${path} returned ${response.status()}: ${body}`).toBeTruthy();
  return (JSON.parse(body) as ApiEnvelope<T>).data;
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

async function waitForSandbox(
  request: APIRequestContext,
  sessionId: string,
  timeoutMs = 180_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const session = await api<Record<string, unknown>>(
      request,
      "get",
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
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
  sessionId: string,
  command: string,
): Promise<{ status: number; body: string }> {
  const response = await request.post(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/terminal/stream`,
    {
      headers: { Accept: "text/event-stream" },
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

test("the no-auth installation can register, assign, and run real extensions", async ({
  browser,
  request,
}) => {
  expect(baseURL).toBeTruthy();
  expect(liveMcpURL, "the AWS MCP fixture URL must be configured").toBeTruthy();
  const suffix = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const skillName = `astrabox-local-${suffix}`;
  const mcpName = `astrabox_local_mcp_${suffix.replaceAll("-", "_")}`;
  let agentId = "";
  let sessionId = "";
  let context: BrowserContext | null = null;

  try {
    const current = await api<{ user_id: string; roles: string[] }>(
      request,
      "get",
      "/api/v1/user/current",
    );
    expect(current.user_id).toBeTruthy();
    expect(current.roles).toContain("admin");

    // The general write contract rejects access fields; a created Agent is
    // private by default and this journey needs nothing more.
    const created = await api<{ agent_id: string }>(request, "post", "/api/v1/agents", {
      name: `extension-local-agent-${suffix}`,
      model: "extension-e2e-model",
      environment_name: "claude-code",
    });
    agentId = created.agent_id;

    context = await browser.newContext();
    await context.addCookies([
      {
        name: "token",
        value: "unrelated-identity-provider-cookie",
        url: new URL("/", baseURL!).toString(),
      },
    ]);
    const page = await context.newPage();
    await page.goto(`/manage/agents/${encodeURIComponent(agentId)}`);
    await expect(page.getByRole("heading", { name: "MCP servers and Skills", exact: true })).toBeVisible();
    await expect(page.getByRole("combobox", { name: "MCP servers", exact: true })).toBeVisible();
    await expect(page.getByRole("combobox", { name: "Skills", exact: true })).toBeVisible();

    const modelCatalog = await api<{ models: string[] }>(
      request,
      "get",
      "/api/v1/agent-configuration/environments/claude-code/models",
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

    // The catalogue is the gateway's own application and opens in a tab of
    // its own; the Agent page and its half-typed cards stay behind.
    const [mcpConsole] = await Promise.all([
      context!.waitForEvent("page"),
      page.getByRole("button", { name: "Manage remote MCP servers", exact: true }).click(),
    ]);
    await expect(mcpConsole).toHaveURL(/\/ui\/mcp-servers\/?/);
    await expect(mcpConsole.getByRole("main").getByText("MCP Servers", { exact: true })).toBeVisible();
    const rootToken = (await context.cookies()).find(
      (cookie) => cookie.name === "token" && cookie.path === "/",
    );
    expect(rootToken?.value).toBe("unrelated-identity-provider-cookie");

    await mcpConsole.getByRole("button", { name: /Add New MCP Server/i }).click();
    await mcpConsole.getByRole("button", { name: /Custom Server/i }).click();
    await mcpConsole.getByLabel("MCP Server Name", { exact: true }).fill(mcpName);
    await mcpConsole.getByLabel("Transport Type", { exact: true }).click();
    await mcpConsole.getByText("Streamable HTTP (Recommended)", { exact: true }).click();
    await mcpConsole.getByLabel("MCP Server URL", { exact: true }).fill(liveMcpURL!);
    const mcpDialog = mcpConsole.getByRole("dialog", { name: /Add New MCP Server/i });
    await mcpDialog.getByRole("combobox").nth(1).click();
    await mcpConsole.locator('.ant-select-item-option[title="None"]:visible').click();
    await mcpDialog.getByRole("button", { name: "Add MCP Server", exact: true }).click();
    const mcpCard = mcpConsole
      .getByTestId("mcp-servers-grid")
      .getByRole("button", { name: new RegExp(mcpName) })
      .first();
    await expect(mcpCard).toBeVisible({ timeout: 20_000 });

    const liveMcp = await mcpConsole.evaluate(async ({ serverId }) => {
      const uiToken = window.sessionStorage.getItem("token") || "";
      const encodedPayload = uiToken.split(".")[1] || "";
      const normalized = encodedPayload.replaceAll("-", "+").replaceAll("_", "/");
      const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
      const claims = JSON.parse(window.atob(padded)) as { key?: string };
      const listResponse = await fetch(
        `/mcp-rest/tools/list?server_id=${encodeURIComponent(serverId)}`,
        { headers: { Authorization: `Bearer ${claims.key || ""}` } },
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
          arguments: { text: "browser-live-local" },
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
    expect(liveMcp.callBody).toContain("MCP_E2E_OK:browser-live-local");

    await page.goto(`/manage/agents/${encodeURIComponent(agentId)}`);
    const [skillsConsole] = await Promise.all([
      context!.waitForEvent("page"),
      page.getByRole("button", { name: "Manage Skills", exact: true }).click(),
    ]);
    await expect(skillsConsole).toHaveURL(/\/ui\/skills\/?/);
    await expect(skillsConsole.getByRole("heading", { name: "Skills", exact: true })).toBeVisible();
    const liveSkill = await request.get(liveSkillFileURL);
    expect(liveSkill.status(), `could not read live Skill source ${liveSkillFileURL}`).toBe(200);
    expect(await liveSkill.text()).toMatch(/name:\s*grill-me/i);
    await skillsConsole.getByText("Add Skill", { exact: false }).first().click();
    await skillsConsole.getByLabel("GitHub URL", { exact: true }).fill(liveSkillURL);
    await skillsConsole.getByLabel("Skill Name", { exact: true }).fill(skillName);
    const skillCreateResponsePromise = skillsConsole.waitForResponse(
      (response) => response.url().includes("/claude-code/plugins")
        && response.request().method() === "POST",
    );
    await skillsConsole.getByRole("button", { name: "Add Skill", exact: true }).click();
    const skillCreateResponse = await skillCreateResponsePromise;
    const skillCreateBody = await skillCreateResponse.text();
    expect(
      skillCreateResponse.ok(),
      `live Skill registration returned ${skillCreateResponse.status()}: ${skillCreateBody}`,
    ).toBeTruthy();
    const skillRow = skillsConsole.getByRole("row", { name: new RegExp(skillName) });
    await expect(skillRow).toBeVisible({ timeout: 20_000 });

    await page.goto(`/manage/agents/${encodeURIComponent(agentId)}`);
    await selectCatalogItem(page, "MCP servers", "Search MCP servers", mcpName);
    await selectCatalogItem(page, "Skills", "Search Skills", skillName);
    const extensionCard = page
      .getByRole("heading", { name: "MCP servers and Skills", exact: true })
      .locator("xpath=ancestor::section");
    await extensionCard.getByRole("button", { name: "Save MCP servers and Skills", exact: true }).click();
    await expect(extensionCard.getByText("Saved", { exact: true })).toBeVisible();

    const assignment = await api<{
      selected_mcp_server_ids: string[];
      selected_skill_ids: string[];
    }>(request, "get", `/api/v1/agents/${encodeURIComponent(agentId)}/extensions`);
    expect(assignment.selected_mcp_server_ids).toHaveLength(1);
    expect(assignment.selected_skill_ids).toHaveLength(1);

    const conversation = await api<{ session_id: string }>(
      request,
      "post",
      `/api/v1/agents/${encodeURIComponent(agentId)}/conversations`,
    );
    sessionId = conversation.session_id;
    await waitForSandbox(request, sessionId);

    const installedSkill = await terminalCommand(
      request,
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
    );
    const runtimeMcp = await terminalCommand(
      request,
      sessionId,
      directMcpCallCommand(
        resolvedMcpUrl(runtimeDetail, mcpName),
        `${mcpName}-echo`,
        "agent-runtime-local",
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
    ).toContain("MCP_E2E_OK:agent-runtime-local");

    await skillsConsole.goto("/ui/skills/");
    const cleanupSkillRow = skillsConsole.getByRole("row", { name: new RegExp(skillName) });
    await cleanupSkillRow.locator("button").last().click();
    await skillsConsole.getByRole("button", { name: "Delete", exact: true }).click();
    await expect(cleanupSkillRow).toBeHidden({ timeout: 20_000 });
    await page.goto(`/manage/agents/${encodeURIComponent(agentId)}`);
    const [cleanupConsole] = await Promise.all([
      context!.waitForEvent("page"),
      page.getByRole("button", { name: "Manage remote MCP servers", exact: true }).click(),
    ]);
    const cleanupMcpCard = cleanupConsole
      .getByTestId("mcp-servers-grid")
      .getByRole("button", { name: new RegExp(mcpName) })
      .first();
    await cleanupMcpCard.getByRole("button", { name: "Server actions" }).click();
    await cleanupConsole.getByText("Delete", { exact: true }).last().click();
    await page
      .getByRole("dialog", { name: /Delete MCP Server/i })
      .getByRole("button", { name: "Delete", exact: true })
      .click();
    await expect(cleanupMcpCard).toBeHidden({ timeout: 20_000 });
  } finally {
    await context?.close().catch(() => undefined);
    if (sessionId) {
      await request.delete(`/api/v1/sessions/${encodeURIComponent(sessionId)}`).catch(() => undefined);
    }
    if (agentId) {
      await request.delete(`/api/v1/agents/${encodeURIComponent(agentId)}`).catch(() => undefined);
    }
  }
});
