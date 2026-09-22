import { expect, test, type APIRequestContext } from "@playwright/test";
import { createHash } from "node:crypto";

import {
  directMcpCallCommand,
  resolvedMcpUrl,
} from "./helpers/direct-mcp";

type ApiEnvelope<T> = { data: T };

async function responseData<T>(
  response: Awaited<ReturnType<APIRequestContext["fetch"]>>,
  operation: string,
): Promise<T> {
  const body = await response.text();
  expect(
    response.ok(),
    `${operation} returned ${response.status()}: ${body.slice(0, 500)}`,
  ).toBeTruthy();
  return (JSON.parse(body) as ApiEnvelope<T>).data;
}

async function waitForSandbox(
  request: APIRequestContext,
  sessionId: string,
  timeoutMs = 180_000,
): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const session = await responseData<Record<string, unknown>>(
      await request.get(`/api/v1/sessions/${sessionId}`),
      "read MCP test Session",
    );
    const sandboxId = String(session.sandbox_id || "");
    if (sandboxId) return sandboxId;
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
  const response = await request.post(`/api/v1/sessions/${sessionId}/terminal/stream`, {
    data: { command },
    headers: { Accept: "text/event-stream" },
    timeout: 120_000,
  });
  return { status: response.status(), body: await response.text() };
}

async function completeRootTurn(
  request: APIRequestContext,
  sessionId: string,
  operation: string,
): Promise<void> {
  const response = await request.post(`/api/v1/sessions/${sessionId}/ai-stream`, {
    data: { content: "Do not use tools. Reply with OK only." },
    headers: { Accept: "text/event-stream" },
    timeout: 120_000,
  });
  const body = await response.text();
  expect(
    response.status(),
    `${operation} returned ${response.status()}: ${body.slice(0, 1_000)}`,
  ).toBe(200);
  const frames = body
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trim())
    .filter((line) => line && line !== "[DONE]")
    .map((line) => JSON.parse(line) as Record<string, unknown>);
  expect(
    frames.filter((frame) => frame.type === "error"),
    `${operation} emitted an error frame: ${body.slice(0, 1_000)}`,
  ).toEqual([]);
  expect(
    frames.some((frame) => frame.type === "finish"),
    `${operation} did not complete: ${body.slice(0, 1_000)}`,
  ).toBe(true);
}

function mcpVaultDestinationPrefix(serverUrl: string): string {
  const parsed = new URL(serverUrl);
  const isDefaultPort = (
    (parsed.protocol === "http:" && parsed.port === "80")
    || (parsed.protocol === "https:" && parsed.port === "443")
  );
  const port = parsed.port && !isDefaultPort ? `:${parsed.port}` : "";
  const scope = `${parsed.protocol.slice(0, -1).toLowerCase()}://${parsed.hostname.toLowerCase()}`
    + `${port}${parsed.pathname || "/"}`;
  const destination = createHash("sha256").update(scope).digest("hex").slice(0, 24);
  return `astrabox-mcp-${destination}-v-`;
}

test("direct MCP follows the Session snapshot and refreshes egress credentials", async ({
  request,
}) => {
  const upstreamUrl = String(process.env.ASTRABOX_E2E_MCP_UPSTREAM_URL || "").trim();
  const upstreamBearer = String(process.env.ASTRABOX_E2E_MCP_BEARER || "").trim();
  expect(upstreamUrl, "the AWS MCP fixture URL must be configured").not.toEqual("");
  expect(upstreamBearer, "the AWS MCP fixture bearer must be configured").not.toEqual("");

  const runId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const serverName = `e2e-runtime-${runId}`;
  let agentId = "";
  let mcpServerId = "";
  let vaultId = "";
  let credentialId = "";
  const sessionIds: string[] = [];
  let originalMcpIds: string[] | null = null;
  let originalVaultIds: string[] | null = null;

  const agents = await responseData<Array<{ agent_id: string }>>(
    await request.get("/api/v1/agents"),
    "list Agents",
  );
  expect(agents.length).toBeGreaterThan(0);
  agentId = String(agents[0].agent_id || "");

  try {
    const registered = await responseData<{ mcp_server_id: string }>(
      await request.post("/api/v1/admin/mcp-servers", {
        data: {
          name: serverName,
          url: upstreamUrl,
          transport: "streamable_http",
        },
      }),
      "register runtime MCP server",
    );
    mcpServerId = String(registered.mcp_server_id || "");

    const currentMcp = await responseData<{ mcp_server_ids: string[] }>(
      await request.get(`/api/v1/admin/agents/${agentId}/mcp-servers`),
      "read current MCP assignment",
    );
    originalMcpIds = currentMcp.mcp_server_ids;
    await responseData(
      await request.put(`/api/v1/admin/agents/${agentId}/mcp-servers`, {
        data: { mcp_server_ids: [...originalMcpIds, mcpServerId] },
      }),
      "assign runtime MCP server",
    );

    const vault = await responseData<{ vault_id: string }>(
      await request.post("/api/v1/admin/vaults", {
        data: { display_name: `MCP runtime e2e ${runId}` },
      }),
      "create MCP credential Vault",
    );
    vaultId = String(vault.vault_id || "");

    const credentialResponse = await request.post(
      `/api/v1/admin/vaults/${vaultId}/credentials`,
      {
        data: {
          display_name: "MCP fixture bearer",
          auth: {
            type: "static_bearer",
            mcp_server_url: upstreamUrl,
            token: upstreamBearer,
          },
        },
      },
    );
    const credential = await responseData<{ credential_id: string }>(
      credentialResponse,
      "create MCP fixture credential",
    );
    credentialId = String(credential.credential_id || "");
    expect(credentialId).not.toEqual("");
    expect(JSON.stringify(credential)).not.toContain(upstreamBearer);

    const currentVaults = await responseData<{ vault_ids: string[] }>(
      await request.get(`/api/v1/admin/agents/${agentId}/credential-vaults`),
      "read current Vault assignment",
    );
    originalVaultIds = currentVaults.vault_ids;
    await responseData(
      await request.put(`/api/v1/admin/agents/${agentId}/credential-vaults`, {
        data: { vault_ids: [...originalVaultIds, vaultId] },
      }),
      "assign MCP credential Vault",
    );

    const conversation = await responseData<{ session_id: string }>(
      await request.post(`/api/v1/agents/${agentId}/conversations`),
      "start Agent conversation",
    );
    const sessionId = String(conversation.session_id || "");
    sessionIds.push(sessionId);
    await waitForSandbox(request, sessionId);

    const environment = await terminalCommand(request, sessionId, "env");
    expect(environment.status).toBe(200);
    expect(environment.body).not.toContain(upstreamBearer);

    const detail = await responseData<Record<string, unknown>>(
      await request.get(`/api/v1/admin/sessions/${sessionId}/detail`),
      "read resolved direct MCP configuration",
    );
    const runtimeUrl = resolvedMcpUrl(detail, serverName);
    expect(runtimeUrl).toBe(upstreamUrl);
    const allowed = await terminalCommand(
      request,
      sessionId,
      directMcpCallCommand(runtimeUrl, "echo", "aws-runtime"),
    );
    expect(allowed.status, allowed.body.slice(0, 1_000)).toBe(200);
    expect(allowed.body).toContain("MCP_E2E_OK:aws-runtime");
    expect(allowed.body).not.toContain(upstreamBearer);

    await responseData(
      await request.patch(`/api/v1/admin/mcp-servers/${mcpServerId}`, {
        data: { enabled: false },
      }),
      "disable runtime MCP server",
    );
    const capturedAfterDisable = await terminalCommand(
      request,
      sessionId,
      directMcpCallCommand(runtimeUrl, "echo", "captured-session-still-works"),
    );
    expect(capturedAfterDisable.status, capturedAfterDisable.body).toBe(200);
    expect(capturedAfterDisable.body).toContain(
      "MCP_E2E_OK:captured-session-still-works",
    );
    const disabledConversation = await responseData<{ session_id: string }>(
      await request.post(`/api/v1/agents/${agentId}/conversations`),
      "start Session after disabling MCP server",
    );
    const disabledSessionId = String(disabledConversation.session_id || "");
    sessionIds.push(disabledSessionId);
    await waitForSandbox(request, disabledSessionId);
    const disabledDetail = await responseData<Record<string, unknown>>(
      await request.get(`/api/v1/admin/sessions/${disabledSessionId}/detail`),
      "read disabled Session MCP configuration",
    );
    expect(() => resolvedMcpUrl(disabledDetail, serverName)).toThrow();
    await responseData(
      await request.delete(`/api/v1/sessions/${disabledSessionId}`),
      "delete the disabled-MCP Session",
    );
    await responseData(
      await request.delete(`/api/v1/sessions/${sessionId}`),
      "delete the Session that retained its MCP snapshot",
    );

    await responseData(
      await request.patch(`/api/v1/admin/mcp-servers/${mcpServerId}`, {
        data: { enabled: true },
      }),
      "re-enable runtime MCP server",
    );
    const reenabledConversation = await responseData<{ session_id: string }>(
      await request.post(`/api/v1/agents/${agentId}/conversations`),
      "start Session after re-enabling MCP server",
    );
    const reenabledSessionId = String(reenabledConversation.session_id || "");
    sessionIds.push(reenabledSessionId);
    const reenabledSandboxId = await waitForSandbox(request, reenabledSessionId);
    const reenabledDetail = await responseData<Record<string, unknown>>(
      await request.get(`/api/v1/admin/sessions/${reenabledSessionId}/detail`),
      "read re-enabled Session MCP configuration",
    );
    const reenabled = await terminalCommand(
      request,
      reenabledSessionId,
      directMcpCallCommand(
        resolvedMcpUrl(reenabledDetail, serverName),
        "echo",
        "aws-runtime",
      ),
    );
    expect(reenabled.body).toContain("MCP_E2E_OK:aws-runtime");

    const destinationPrefix = mcpVaultDestinationPrefix(upstreamUrl);
    const initialSecurity = await responseData<{
      credential_names: string[];
      binding_names: string[];
    }>(
      await request.get(`/api/v1/admin/sandboxes/${reenabledSandboxId}/security`),
      "read active MCP credential posture",
    );
    const initialCredentialNames = initialSecurity.credential_names.filter((name) =>
      name.startsWith(destinationPrefix)
    );
    const initialBindingNames = initialSecurity.binding_names.filter((name) =>
      name.startsWith(destinationPrefix)
    );
    expect(initialCredentialNames).not.toEqual([]);
    expect(initialBindingNames).toHaveLength(1);

    const invalidRotatedBearer = `invalid-${runId}`;
    const rotated = await responseData<Record<string, unknown>>(
      await request.patch(
        `/api/v1/admin/vaults/${vaultId}/credentials/${credentialId}`,
        { data: { auth: { token: invalidRotatedBearer } } },
      ),
      "rotate the running Session's MCP credential",
    );
    expect(JSON.stringify(rotated)).not.toContain(invalidRotatedBearer);
    await completeRootTurn(
      request,
      reenabledSessionId,
      "turn after MCP credential rotation",
    );
    const rejectedAfterRotation = await terminalCommand(
      request,
      reenabledSessionId,
      directMcpCallCommand(
        resolvedMcpUrl(reenabledDetail, serverName),
        "echo",
        "rotated-credential-must-not-pass",
      ),
    );
    expect(rejectedAfterRotation.status).toBe(200);
    expect(rejectedAfterRotation.body).not.toContain(
      "MCP_E2E_OK:rotated-credential-must-not-pass",
    );
    expect(rejectedAfterRotation.body).toMatch(/MCP HTTP (?:401|403)/);
    expect(rejectedAfterRotation.body).not.toContain(invalidRotatedBearer);
    expect(rejectedAfterRotation.body).not.toContain(upstreamBearer);

    const archived = await responseData<Record<string, unknown>>(
      await request.post(
        `/api/v1/admin/vaults/${vaultId}/credentials/${credentialId}/archive`,
      ),
      "archive the running Session's MCP credential",
    );
    expect(JSON.stringify(archived)).not.toContain(invalidRotatedBearer);
    await completeRootTurn(
      request,
      reenabledSessionId,
      "turn after MCP credential archive",
    );
    const archivedSecurity = await responseData<{
      credential_names: string[];
      binding_names: string[];
    }>(
      await request.get(`/api/v1/admin/sandboxes/${reenabledSandboxId}/security`),
      "read archived MCP credential posture",
    );
    expect(
      archivedSecurity.credential_names.filter((name) =>
        name.startsWith(destinationPrefix)
      ),
    ).toEqual([]);
    const archivedBindingNames = archivedSecurity.binding_names.filter((name) =>
      name.startsWith(destinationPrefix)
    );
    expect(archivedBindingNames).toHaveLength(1);
    expect(archivedBindingNames).not.toEqual(initialBindingNames);
    expect(
      archivedBindingNames[0].split("-i-")[0],
      "archive must preserve the destination and Session Vault scope",
    ).toBe(initialBindingNames[0].split("-i-")[0]);
    const rejectedAfterArchive = await terminalCommand(
      request,
      reenabledSessionId,
      directMcpCallCommand(
        resolvedMcpUrl(reenabledDetail, serverName),
        "echo",
        "archived-credential-must-not-pass",
      ),
    );
    expect(rejectedAfterArchive.status).toBe(200);
    expect(rejectedAfterArchive.body).not.toContain(
      "MCP_E2E_OK:archived-credential-must-not-pass",
    );
    expect(rejectedAfterArchive.body).toMatch(/MCP HTTP (?:401|403)/);
    expect(rejectedAfterArchive.body).not.toContain(invalidRotatedBearer);
    expect(rejectedAfterArchive.body).not.toContain(upstreamBearer);
    await responseData(
      await request.delete(`/api/v1/sessions/${reenabledSessionId}`),
      "delete the re-enabled-MCP Session",
    );

    await responseData(
      await request.put(`/api/v1/admin/agents/${agentId}/mcp-servers`, {
        data: { mcp_server_ids: originalMcpIds },
      }),
      "unassign runtime MCP server",
    );
    const unassignedConversation = await responseData<{ session_id: string }>(
      await request.post(`/api/v1/agents/${agentId}/conversations`),
      "start Session after unassigning MCP server",
    );
    const unassignedSessionId = String(unassignedConversation.session_id || "");
    sessionIds.push(unassignedSessionId);
    await waitForSandbox(request, unassignedSessionId);
    const unassignedDetail = await responseData<Record<string, unknown>>(
      await request.get(`/api/v1/admin/sessions/${unassignedSessionId}/detail`),
      "read unassigned Session MCP configuration",
    );
    expect(() => resolvedMcpUrl(unassignedDetail, serverName)).toThrow();
    await responseData(
      await request.delete(`/api/v1/sessions/${unassignedSessionId}`),
      "delete the unassigned-MCP Session",
    );
    originalMcpIds = null;
  } finally {
    if (agentId && originalMcpIds !== null) {
      await request.put(`/api/v1/admin/agents/${agentId}/mcp-servers`, {
        data: { mcp_server_ids: originalMcpIds },
      }).catch(() => undefined);
    }
    if (agentId && originalVaultIds !== null) {
      await request.put(`/api/v1/admin/agents/${agentId}/credential-vaults`, {
        data: { vault_ids: originalVaultIds },
      }).catch(() => undefined);
    }
    for (const cleanupSessionId of sessionIds) {
      await request.delete(`/api/v1/sessions/${cleanupSessionId}`).catch(() => undefined);
    }
    if (mcpServerId) {
      await request.delete(`/api/v1/admin/mcp-servers/${mcpServerId}`).catch(() => undefined);
    }
    if (vaultId) {
      await request.delete(`/api/v1/admin/vaults/${vaultId}`).catch(() => undefined);
    }
  }
});
