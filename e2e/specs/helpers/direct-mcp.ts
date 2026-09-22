function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

const DIRECT_MCP_SCRIPT = String.raw`
const [url, tool, text] = process.argv.slice(1);
let sessionId = "";
let initialized = false;
async function post(payload) {
  const headers = {
    accept: "application/json, text/event-stream",
    "content-type": "application/json",
  };
  if (sessionId) headers["mcp-session-id"] = sessionId;
  if (initialized) headers["mcp-protocol-version"] = "2025-03-26";
  const response = await fetch(url, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
  sessionId = response.headers.get("mcp-session-id") || sessionId;
  const body = await response.text();
  if (!response.ok) throw new Error("MCP HTTP " + response.status + ": " + body);
  return body
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trim())
    .filter(Boolean)
    .join("\n") || body;
}
(async () => {
  await post({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: "2025-03-26",
      capabilities: {},
      clientInfo: { name: "astrabox-e2e-direct", version: "1.0.0" },
    },
  });
  initialized = true;
  await post({ jsonrpc: "2.0", method: "notifications/initialized" });
  const result = await post({
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    params: { name: tool, arguments: { text } },
  });
  process.stdout.write(result + "\n");
})().catch((error) => {
  process.stderr.write(String(error && error.stack || error) + "\n");
  process.exitCode = 1;
});
`;

export function directMcpCallCommand(
  serverUrl: string,
  toolName: string,
  text: string,
): string {
  return [
    "node -e",
    shellQuote(DIRECT_MCP_SCRIPT),
    shellQuote(serverUrl),
    shellQuote(toolName),
    shellQuote(text),
  ].join(" ");
}

export function resolvedMcpUrl(
  detail: Record<string, unknown>,
  serverName: string,
): string {
  const raw = detail.template_mcp_config;
  const config = raw && typeof raw === "object"
    ? (raw as Record<string, unknown>)[serverName]
    : undefined;
  const url = config && typeof config === "object"
    ? String((config as Record<string, unknown>).url || "").trim()
    : "";
  if (!url) throw new Error(`Session detail has no MCP URL for ${serverName}`);
  return url;
}
