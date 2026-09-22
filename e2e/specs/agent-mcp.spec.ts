import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { expect, test } from "@playwright/test";

type ToolPayload = Record<string, unknown>;

function payloadFromToolResult(result: Awaited<ReturnType<Client["callTool"]>>): ToolPayload {
  expect(result.isError, JSON.stringify(result)).not.toBe(true);
  if (result.structuredContent && typeof result.structuredContent === "object") {
    return result.structuredContent as ToolPayload;
  }
  const content = Array.isArray(result.content) ? result.content : [];
  const text = content.find(
    (part: unknown): part is { type: "text"; text: string } => (
      typeof part === "object"
      && part !== null
      && "type" in part
      && "text" in part
      && part.type === "text"
      && typeof part.text === "string"
    ),
  )?.text;
  expect(text, `tool returned no structured or text content: ${JSON.stringify(result)}`).toBeTruthy();
  return JSON.parse(String(text)) as ToolPayload;
}

async function answerUnexpectedInteraction(
  client: Client,
  agentId: string,
  sessionId: string,
  pending: ToolPayload,
): Promise<void> {
  const interactionId = String(pending.interaction_id || "");
  expect(interactionId, `pending interaction has no id: ${JSON.stringify(pending)}`).not.toEqual("");
  const questions = Array.isArray(pending.questions) ? pending.questions : [];
  const first = questions[0] as ToolPayload | undefined;
  const options = first && Array.isArray(first.options) ? first.options : [];
  const answer = first && options.length > 0
    ? {
        answers: [
          {
            question_id: String(first.id || ""),
            option_label: String(options[0]),
          },
        ],
      }
    : { decision: "deny", message: "The MCP publication e2e does not permit tool use." };
  payloadFromToolResult(
    await client.callTool({
      name: "answer_interaction",
      arguments: {
        agent_id: agentId,
        session_id: sessionId,
        interaction_id: interactionId,
        answer,
      },
    }),
  );
}

test("a real MCP client completes an Agent conversation", async ({ request }, testInfo) => {

  const configuredBaseURL = String(testInfo.project.use.baseURL || "").trim();
  expect(configuredBaseURL, "the Playwright project must configure baseURL").not.toEqual("");
  const token = String(process.env.ASTRABOX_E2E_MCP_TOKEN || "").trim();
  let agentId = String(process.env.ASTRABOX_E2E_AGENT_ID || "").trim();
  const endpoint = new URL("/api/v1/mcp", configuredBaseURL);
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;

  const transport = new StreamableHTTPClientTransport(endpoint, {
    requestInit: { headers },
  });
  const client = new Client({ name: "astrabox-agent-mcp-e2e", version: "1.0.0" });
  let sessionId = "";

  try {
    await client.connect(transport);
    const listed = await client.listTools();
    expect(listed.tools.map((tool) => tool.name).sort()).toEqual([
      "answer_interaction",
      "cancel_task",
      "create_conversation",
      "get_status",
      "list_agents",
      "send_message",
    ]);

    const available = payloadFromToolResult(
      await client.callTool({ name: "list_agents", arguments: {} }),
    );
    const agents = Array.isArray(available.agents)
      ? available.agents as ToolPayload[]
      : [];
    expect(agents.length, "the MCP caller must be able to use an Agent").toBeGreaterThan(0);
    if (agentId) {
      expect(
        agents.some((agent) => String(agent.agent_id || "") === agentId),
        `configured Agent ${agentId} is not visible: ${JSON.stringify(agents)}`,
      ).toBeTruthy();
    } else {
      agentId = String(agents[0].agent_id || "");
    }
    expect(agentId).not.toEqual("");

    const created = payloadFromToolResult(
      await client.callTool({
        name: "create_conversation",
        arguments: { agent_id: agentId },
      }),
    );
    sessionId = String(created.session_id || "");
    expect(sessionId, JSON.stringify(created)).not.toEqual("");

    const marker = `MCP_E2E_OK_${Date.now()}`;
    const submitted = payloadFromToolResult(
      await client.callTool({
        name: "send_message",
        arguments: {
          agent_id: agentId,
          session_id: sessionId,
          instruction: `Reply with exactly ${marker}. Do not call any tools.`,
        },
      }),
    );
    expect(submitted.state).toBe("SUBMITTED");

    const deadline = Date.now() + 240_000;
    let finalStatus: ToolPayload = {};
    let receivedExpectedAnswer = false;
    while (Date.now() < deadline) {
      finalStatus = payloadFromToolResult(
        await client.callTool({
          name: "get_status",
          arguments: { agent_id: agentId, session_id: sessionId },
        }),
      );
      if (finalStatus.state === "WAITING_INPUT") {
        const pending = finalStatus.pending_interaction;
        expect(pending && typeof pending === "object", JSON.stringify(finalStatus)).toBeTruthy();
        await answerUnexpectedInteraction(client, agentId, sessionId, pending as ToolPayload);
      }
      const messages = Array.isArray(finalStatus.recent_messages)
        ? finalStatus.recent_messages as ToolPayload[]
        : [];
      const answered = messages.some(
        (message) => message.role === "assistant" && String(message.content || "").includes(marker),
      );
      if (finalStatus.state === "READY" && answered) {
        receivedExpectedAnswer = true;
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 1_000));
    }

    expect(finalStatus.state, JSON.stringify(finalStatus)).toBe("READY");
    expect(receivedExpectedAnswer, JSON.stringify(finalStatus)).toBeTruthy();
  } finally {
    await client.close().catch(() => undefined);
    // Local/no-auth and browser-authenticated runs can clean up through the
    // platform API. A bearer-only OIDC run may intentionally expose only the
    // MCP facade, so cleanup is best-effort there.
    if (sessionId) {
      await request.delete(`/api/v1/sessions/${sessionId}`).catch(() => undefined);
    }
  }
});
