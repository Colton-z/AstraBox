import { expect, test, type APIRequestContext } from "@playwright/test";

type ApiEnvelope<T> = { data: T };

/** One row of the Agent's assignment list: a catalog id and the provider it came from. */
type McpAssignment = { provider: string; item_id: string };

/** The ids assigned from AstraBox's own registry, which this test registers into. */
function builtinItemIds(assignments: McpAssignment[] | undefined): string[] {
  return (assignments ?? [])
    .filter((entry) => entry.provider === "builtin")
    .map((entry) => entry.item_id);
}

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

test("an administrator registers an MCP server and explicitly assigns it to an Agent", async ({
  request,
}) => {
  const runId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const name = `e2e-mcp-${runId}`;
  let mcpServerId = "";
  let agentId = "";
  let originalIds: string[] | null = null;

  const agents = await responseData<Array<{ agent_id: string }>>(
    await request.get("/api/v1/agents"),
    "list Agents",
  );
  expect(agents.length, "the real test installation must expose an Agent").toBeGreaterThan(0);
  agentId = String(agents[0].agent_id || "");

  try {
    const created = await responseData<{
      mcp_server_id: string;
      name: string;
      url: string;
      transport: string;
      enabled: boolean;
    }>(
      await request.post("/api/v1/admin/mcp-servers", {
        data: {
          name,
          url: `https://${name}.example.test/mcp`,
          transport: "streamable_http",
        },
      }),
      "register MCP server",
    );
    mcpServerId = String(created.mcp_server_id || "");
    expect(mcpServerId).toMatch(/^mcp_/);
    expect(created).toMatchObject({ name, transport: "streamable_http", enabled: true });

    const fetched = await responseData<{
      mcp_server_id: string;
      name: string;
      url: string;
    }>(
      await request.get(`/api/v1/admin/mcp-servers/${mcpServerId}`),
      "read registered MCP server",
    );
    expect(fetched).toMatchObject({ mcp_server_id: mcpServerId, name });

    const updatedUrl = `https://${name}.example.test/v2/mcp`;
    const updated = await responseData<{ url: string; description: string }>(
      await request.patch(`/api/v1/admin/mcp-servers/${mcpServerId}`, {
        data: { url: updatedUrl, description: "AWS Playwright fixture" },
      }),
      "edit registered MCP server",
    );
    expect(updated).toMatchObject({
      url: updatedUrl,
      description: "AWS Playwright fixture",
    });

    const catalog = await responseData<{ mcp_servers: Array<{ mcp_server_id: string }> }>(
      await request.get("/api/v1/admin/mcp-servers"),
      "list registered MCP servers",
    );
    expect(catalog.mcp_servers.some((server) => server.mcp_server_id === mcpServerId)).toBe(true);

    const before = await responseData<{ mcp_server_ids: string[] }>(
      await request.get(`/api/v1/admin/agents/${agentId}/mcp-servers`),
      "read Agent MCP assignment",
    );
    originalIds = before.mcp_server_ids;
    expect(originalIds).not.toContain(mcpServerId);

    const beforeAgent = await responseData<{ mcp_assignments?: McpAssignment[] }>(
      await request.get(`/api/v1/agents/${agentId}`),
      "read unassigned Agent",
    );
    expect(builtinItemIds(beforeAgent.mcp_assignments)).not.toContain(mcpServerId);

    const unknownAssignment = await request.put(
      `/api/v1/admin/agents/${agentId}/mcp-servers`,
      { data: { mcp_server_ids: [...originalIds, "mcp_unknown"] } },
    );
    expect(unknownAssignment.status()).toBe(400);

    const assigned = await responseData<{ mcp_server_ids: string[] }>(
      await request.put(`/api/v1/admin/agents/${agentId}/mcp-servers`, {
        data: { mcp_server_ids: [...originalIds, mcpServerId] },
      }),
      "assign MCP server to Agent",
    );
    expect(assigned.mcp_server_ids).toEqual([...originalIds, mcpServerId]);

    const assignedAgent = await responseData<{ mcp_assignments?: McpAssignment[] }>(
      await request.get(`/api/v1/agents/${agentId}`),
      "read assigned Agent",
    );
    expect(builtinItemIds(assignedAgent.mcp_assignments)).toContain(mcpServerId);

    const disabled = await responseData<{ enabled: boolean }>(
      await request.patch(`/api/v1/admin/mcp-servers/${mcpServerId}`, {
        data: { enabled: false },
      }),
      "disable MCP server",
    );
    expect(disabled.enabled).toBe(false);

    const disabledAssignment = await responseData<{
      mcp_servers: Array<{ mcp_server_id: string; enabled: boolean }>;
    }>(
      await request.get(`/api/v1/admin/agents/${agentId}/mcp-servers`),
      "read disabled MCP assignment",
    );
    expect(
      disabledAssignment.mcp_servers.find(
        (server) => server.mcp_server_id === mcpServerId,
      ),
    ).toMatchObject({ enabled: false });

    const enabled = await responseData<{ enabled: boolean }>(
      await request.patch(`/api/v1/admin/mcp-servers/${mcpServerId}`, {
        data: { enabled: true },
      }),
      "re-enable MCP server",
    );
    expect(enabled.enabled).toBe(true);

    const deleted = await request.delete(`/api/v1/admin/mcp-servers/${mcpServerId}`);
    expect(deleted.status()).toBe(204);

    const missingServer = await request.get(`/api/v1/admin/mcp-servers/${mcpServerId}`);
    expect(missingServer.status()).toBe(404);

    const danglingAssignment = await responseData<{
      mcp_server_ids: string[];
      missing_mcp_server_ids: string[];
    }>(
      await request.get(`/api/v1/admin/agents/${agentId}/mcp-servers`),
      "read assignment after MCP deletion",
    );
    expect(danglingAssignment.mcp_server_ids).toContain(mcpServerId);
    expect(danglingAssignment.missing_mcp_server_ids).toContain(mcpServerId);

    await responseData<{ mcp_server_ids: string[] }>(
      await request.put(`/api/v1/admin/agents/${agentId}/mcp-servers`, {
        data: { mcp_server_ids: originalIds },
      }),
      "restore Agent MCP assignment",
    );
    const restoredAgent = await responseData<{ mcp_assignments?: McpAssignment[] }>(
      await request.get(`/api/v1/agents/${agentId}`),
      "verify restored Agent",
    );
    expect(builtinItemIds(restoredAgent.mcp_assignments)).not.toContain(mcpServerId);
    originalIds = null;
    mcpServerId = "";
  } finally {
    if (agentId && originalIds !== null) {
      await request.put(`/api/v1/admin/agents/${agentId}/mcp-servers`, {
        data: { mcp_server_ids: originalIds },
      }).catch(() => undefined);
    }
    if (mcpServerId) {
      await request.delete(`/api/v1/admin/mcp-servers/${mcpServerId}`).catch(() => undefined);
    }
  }
});
