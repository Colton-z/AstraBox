import {
  test,
  expect,
  type APIRequestContext,
  type APIResponse,
  type Response as PageResponse,
} from "@playwright/test";

import { LIVE_TESTIDS, tid } from "./helpers";

type ApiEnvelope<T> = { data: T };

// The shipping default holds the model gateway credential outside the sandbox.
// A second explicit opt-out run exercises the direct environment path.
const EXPECT_PROTECTED_DELIVERY =
  process.env.ASTRABOX_E2E_EXPECT_CREDENTIAL_VAULT !== "off";

// The default only resolves where the sandbox runtime shares the Docker
// daemon's image store. A cluster pulls through its registry, so a deployed
// run must name the pushed coordinate.
const HERMES_IMAGE =
  process.env.ASTRABOX_E2E_HERMES_IMAGE?.trim() ||
  "astrabox/sandbox-hermes:latest";

// Both Playwright response types reach here: `request.fetch` answers an
// APIResponse, while a response awaited off the page is a browser Response.
// They agree on the three members this reads, and the narrower signature was
// a type error at two call sites rather than a real incompatibility.
async function responseData<T>(
  response: APIResponse | PageResponse,
  operation: string,
): Promise<T> {
  const body = await response.text();
  expect(
    response.ok(),
    `${operation} returned ${response.status()}: ${body.slice(0, 500)}`,
  ).toBeTruthy();
  return (JSON.parse(body) as ApiEnvelope<T>).data;
}

async function terminalOutput(
  request: APIRequestContext,
  sessionId: string,
  command: string,
): Promise<string> {
  // SESSION_BUSY carries retryable=true — resend the same request — and that
  // contract is the only readiness signal the session offers; there is no
  // separate "terminal ready" flag to wait on. Any other failure still stops
  // the probe on the first response.
  const deadline = Date.now() + 90_000;
  for (;;) {
    const response = await request.post(`/api/v1/sessions/${sessionId}/terminal/stream`, {
      data: { command },
      headers: { Accept: "text/event-stream" },
      timeout: 60_000,
    });
    const body = await response.text();
    if (
      response.status() === 409 &&
      body.includes('"SESSION_BUSY"') &&
      Date.now() < deadline
    ) {
      await new Promise((resolve) => setTimeout(resolve, 3_000));
      continue;
    }
    expect(
      response.ok(),
      `terminal command returned ${response.status()}: ${body.slice(0, 500)}`,
    ).toBeTruthy();
    return body;
  }
}

function hermesGatewayCount(terminalStream: string): number {
  const match = terminalStream.match(/ASTRABOX_GATEWAY_COUNT=(\d+)/);
  expect(match, `terminal output did not contain a Gateway count: ${terminalStream.slice(0, 500)}`)
    .not.toBeNull();
  return Number(match?.[1] ?? "-1");
}

function terminalProbeProcessCount(terminalStream: string): number {
  const match = terminalStream.match(/ASTRABOX_TERMINAL_PROCESS_COUNT=(\d+)/);
  expect(match, `terminal output did not contain a terminal-process count: ${terminalStream.slice(0, 500)}`)
    .not.toBeNull();
  return Number(match?.[1] ?? "-1");
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
      "read public Session",
    );
    expect(session).not.toHaveProperty("vault_ids");
    const sandboxId = String(session.sandbox_id || "");
    if (sandboxId) return sandboxId;

    const state = String(session.state || "");
    if (["TERMINATED", "FAILED", "DELETED"].includes(state)) {
      throw new Error(
        `Session ${sessionId} reached ${state} before provisioning a sandbox: ${String(session.last_error || "no last_error")}`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
  throw new Error(`Session ${sessionId} did not provision a sandbox within ${timeoutMs}ms`);
}

/**
 * The administrator configures a credential once. A user then starts an Agent
 * conversation with no credential UI and no credential-bearing request body.
 * The backend inherits the managed binding, gives the sandbox only a fresh
 * placeholder, and writes the real value to OpenSandbox's egress sidecar.
 */
test("an admin-managed credential is inherited by Agent conversations without user selection", async ({
  page,
  request,
}) => {

  const runId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const vaultName = `E2E production service ${runId}`;
  const secretName = `E2E_BOUND_TOKEN_${runId.replaceAll("-", "_").toUpperCase()}`;
  const secretValue = `e2e-real-secret-${runId}-must-not-enter-sandbox`;
  const allowedHost = `api-${runId}.example.test`;
  let vaultId = "";
  let credentialId = "";
  let sessionId = "";
  let originalBinding: string[] = [];

  const agents = await responseData<Array<{ agent_id: string; name: string }>>(
    await request.get("/api/v1/agents"),
    "list Agents",
  );
  expect(agents.length, "the real e2e deployment must expose an Agent").toBeGreaterThan(0);
  const agent = agents[0];

  try {
    // The old user-facing surface must stay gone, and conversation callers may
    // not smuggle a Vault id into the strict empty request model.
    expect((await request.get("/api/v1/vaults")).status()).toBe(404);
    const injected = await request.post(`/api/v1/agents/${agent.agent_id}/conversations`, {
      data: { vault_ids: ["vlt-user-controlled"] },
    });
    expect(injected.status()).toBe(422);

    await page.addInitScript(() => localStorage.setItem("astrabox-lang", "en"));
    await page.goto("/manage/credentials");
    await expect(tid(page, LIVE_TESTIDS.credentialVaultPage)).toBeVisible();

    // Create the administrator-owned Vault through the shipping management UI.
    await tid(page, LIVE_TESTIDS.credentialVaultCreate).click();
    await tid(page, LIVE_TESTIDS.credentialVaultName).fill(vaultName);
    await tid(page, LIVE_TESTIDS.credentialVaultSave).click();
    await expect(page.getByText(vaultName).first()).toBeVisible();

    const catalogAfterCreate = await responseData<{
      vaults: Array<{ vault_id: string; display_name: string }>;
    }>(await request.get("/api/v1/admin/vaults"), "read managed Vault catalog");
    vaultId = String(
      catalogAfterCreate.vaults.find((vault) => vault.display_name === vaultName)?.vault_id || "",
    );
    expect(vaultId, "the UI-created Vault must exist in the admin catalog").not.toEqual("");

    // Add an environment credential. The value is write-only; only its safe
    // metadata comes back from the catalog.
    await tid(page, LIVE_TESTIDS.credentialAdd).click();
    await tid(page, LIVE_TESTIDS.credentialType).click();
    await page.getByRole("option", { name: "Environment variable for another service" }).click();
    await tid(page, LIVE_TESTIDS.credentialTarget).fill(secretName);
    await tid(page, LIVE_TESTIDS.credentialSecret).fill(secretValue);
    await tid(page, LIVE_TESTIDS.credentialAllowedHosts).fill(allowedHost);
    await tid(page, LIVE_TESTIDS.credentialSave).click();
    await expect(page.getByText(secretName).first()).toBeVisible();
    await expect(page.locator("body")).not.toContainText(secretValue);

    const catalogAfterCredential = await responseData<{
      vaults: Array<{
        vault_id: string;
        credentials: Array<{ credential_id: string; auth: { secret_name?: string } }>;
      }>;
    }>(await request.get("/api/v1/admin/vaults"), "read credential metadata");
    credentialId = String(
      catalogAfterCredential.vaults
        .find((vault) => vault.vault_id === vaultId)
        ?.credentials.find((credential) => credential.auth.secret_name === secretName)
        ?.credential_id || "",
    );
    expect(credentialId, "the UI-created credential must have an id").not.toEqual("");
    expect(JSON.stringify(catalogAfterCredential)).not.toContain(secretValue);

    const current = await responseData<{ vault_ids: string[] }>(
      await request.get(`/api/v1/admin/agents/${agent.agent_id}/credential-vaults`),
      "read Agent credential assignment",
    );
    originalBinding = current.vault_ids;

    // Assign the Vault to the Agent. This is an administrator decision made on
    // the managed Agent, not a choice made by each conversation user.
    await tid(page, LIVE_TESTIDS.credentialBindingOpen).click();
    await tid(page, LIVE_TESTIDS.credentialBindingTarget).click();
    await page.getByRole("option", { name: agent.name, exact: true }).click();
    await tid(page, LIVE_TESTIDS.credentialBindingSave).click();
    await expect(page.getByTestId("credential-assignment-row")).toContainText(agent.name);

    const managedBinding = await responseData<{ vault_ids: string[] }>(
      await request.get(`/api/v1/admin/agents/${agent.agent_id}/credential-vaults`),
      "verify Agent credential assignment",
    );
    expect(managedBinding.vault_ids).toEqual([...originalBinding, vaultId]);

    // Destructive actions fail at the management boundary while the Vault is
    // assigned. This catches dangling runtime policy before production users
    // discover it through a failed conversation.
    // Deleting is two steps, both in the page: the record header's Delete arms
    // the confirm, and only then is the confirming button rendered. There is no
    // browser dialog to accept.
    //
    // The credentials list below carries a Delete with the same label, so this
    // takes the first in DOM order — the record header renders above the cards.
    // The assertion on the next line is what makes that safe: only the
    // vault-level confirm carries this testid, so arming the wrong control
    // fails here instead of deleting the wrong record.
    await page.getByRole("button", { name: "Delete", exact: true }).first().click();
    const confirmDelete = tid(page, LIVE_TESTIDS.credentialVaultDelete);
    await expect(confirmDelete).toBeVisible();
    await confirmDelete.click();
    // The refusal has to reach the operator, not just the network tab. Asserting
    // on the alert role rather than a container class ties this to the failure
    // being announced — which is the part a screen reader gets too.
    //
    // The verb is part of the assertion: the refusal names the action that was
    // attempted, so a Delete that reported "archiving" would be a regression
    // even though the request was correctly refused.
    const refusal = page.getByRole("alert").filter({ hasText: "Unbind it before" });
    await expect(refusal).toBeVisible();
    await expect(refusal).toContainText("before deleting the Vault");
    await expect(refusal).toContainText(agent.name);
    const catalogAfterBlockedDelete = await responseData<{
      vaults: Array<{ vault_id: string }>;
    }>(await request.get("/api/v1/admin/vaults"), "verify bound Vault was not deleted");
    expect(catalogAfterBlockedDelete.vaults.some((vault) => vault.vault_id === vaultId)).toBe(true);

    // Ordinary user surface: no Vault name, no picker, and a bodyless start.
    await page.goto("/agents");
    await expect(tid(page, LIVE_TESTIDS.sessionsPage)).toBeVisible();
    await expect(page.locator("body")).not.toContainText(vaultName);
    const agentCard = page.locator(
      `[data-testid="${LIVE_TESTIDS.agentOption}"][data-agent-name="${agent.name}"]`,
    );
    await expect(agentCard).toBeVisible();

    const [conversationRequest, conversationResponse] = await Promise.all([
      page.waitForRequest((candidate) =>
        candidate.method() === "POST"
        && candidate.url().includes(`/api/v1/agents/${agent.agent_id}/conversations`),
      ),
      page.waitForResponse((candidate) =>
        candidate.request().method() === "POST"
        && candidate.url().includes(`/api/v1/agents/${agent.agent_id}/conversations`),
      ),
      agentCard.getByRole("button").click(),
    ]);
    expect(conversationRequest.postData()).toBeNull();
    const created = await responseData<{ session_id: string }>(
      conversationResponse,
      "start Agent conversation",
    );
    sessionId = String(created.session_id || "");
    expect(sessionId).not.toEqual("");
    await expect(page).toHaveURL(new RegExp(`/sessions/${sessionId}(?:[/?#]|$)`));

    const sandboxId = await waitForSandbox(request, sessionId);

    const security = await responseData<{
      available: boolean;
      default_action: string | null;
      egress_rules: Array<{ action: string; target: string }>;
      credential_names: string[];
      binding_names: string[];
    }>(
      await request.get(`/api/v1/admin/sandboxes/${sandboxId}/security`),
      "read live sandbox security posture",
    );
    expect(security.available).toBe(true);
    expect(security.default_action).toBe("deny");
    expect(security.egress_rules).toContainEqual({ action: "allow", target: allowedHost });
    expect(security.credential_names).toContain(`astrabox-vault-${credentialId}`);
    expect(security.binding_names).toContain(`astrabox-vault-${credentialId}`);

    // The placeholder is the non-secret handle every process in the sandbox
    // uses to ask the egress proxy for the managed credential. A terminal may
    // see that handle; the real value must never enter its output.
    const envOutput = await terminalOutput(request, sessionId, `printenv ${secretName}`);
    expect(envOutput).toContain(`ASTRABOX-VAULT-CRED::${credentialId}::`);
    expect(envOutput).not.toContain(secretValue);

    // Exercise the actual Agent tool process. The prompt never reveals the
    // credential id or expected prefix, so that exact placeholder can enter the
    // transcript only if Bash inherited the managed runtime environment.
    const composer = tid(page, LIVE_TESTIDS.composerPrompt);
    await expect(composer).toBeEnabled({ timeout: 60_000 });
    await composer.fill(
      `Use the Bash tool to run exactly: printenv ${secretName}. `
      + "Then reply with the exact stdout and, on the final line, the number 2.",
    );
    await tid(page, LIVE_TESTIDS.composerSubmit).click();
    await expect(tid(page, LIVE_TESTIDS.assistantText).last()).toContainText("2", {
      timeout: 240_000,
    });
    const transcript = JSON.stringify(await responseData<unknown>(
      await request.get(`/api/v1/sessions/${sessionId}/messages?limit=100`),
      "read the real Agent tool transcript",
    ));
    expect(transcript).toContain(`ASTRABOX-VAULT-CRED::${credentialId}::`);
    expect(transcript).not.toContain(secretValue);
  } finally {
    if (agent?.agent_id && vaultId) {
      await request.put(`/api/v1/admin/agents/${agent.agent_id}/credential-vaults`, {
        data: { vault_ids: originalBinding },
      }).catch(() => undefined);
    }
    if (sessionId) {
      await request.delete(`/api/v1/sessions/${sessionId}`).catch(() => undefined);
    }
    if (vaultId) {
      await request.delete(`/api/v1/admin/vaults/${vaultId}`).catch(() => undefined);
    }
  }
});

/**
 * The resident Assistant engine uses a different image and launch path. The
 * platform model credential is deployment infrastructure: users select nothing,
 * and protected mode keeps the real gateway key outside the Hermes workspace.
 */
test("Hermes automatically uses the protected gateway credential for a real model turn", async ({
  page,
  request,
}) => {

  const runId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const environmentName = "e2e-hermes-vault";
  const assistantName = `e2e Hermes Vault ${runId}`;
  let assistantId = "";
  let sessionId = "";
  let cleanupProbeSessionId = "";
  let sandboxId = "";

  try {
    await responseData(
      await request.put(`/api/v1/admin/environments/${environmentName}`, {
        data: {
          display_name: "Hermes E2E",
          description: "Environment for the real Hermes browser gate.",
          engine_kind: "assistant",
          enabled: true,
          sandbox_backend: "open_sandbox",
          runtime_template_name: HERMES_IMAGE,
          sandbox_tenancy: "agent",
          endpoint_provider: "litellm",
        },
      }),
      "configure Hermes environment",
    );

    const [{ models }, agents] = await Promise.all([
      responseData<{ models: string[] }>(
        await request.get(`/api/v1/admin/environments/${environmentName}/models`),
        "enumerate the Hermes environment model routes",
      ),
      responseData<Array<{ model?: string | null }>>(
        await request.get("/api/v1/agents"),
        "read configured deployment models for Hermes",
      ),
    ]);
    expect(models.length, "the real model gateway must publish its routing catalogue").toBeGreaterThan(0);
    const retiredDeepSeekModels = new Set(["deepseek-chat", "deepseek-reasoner"]);
    const routeMatches = (modelName: string, routeName: string) => {
      if (routeName.endsWith("*")) {
        return modelName.startsWith(routeName.slice(0, -1));
      }
      return modelName === routeName;
    };
    const configuredModels = agents
      .map((agent) => String(agent.model || "").trim())
      .filter(Boolean);
    expect(
      configuredModels.filter((name) => retiredDeepSeekModels.has(name)),
      "test data must not retain DeepSeek model ids retired on 2026-07-24",
    ).toEqual([]);
    const model = configuredModels.find((name) =>
      models.some((route) => routeMatches(name, route)),
    ) || "";
    expect(
      model,
      "the deployment must configure a current model that its gateway actually routes",
    ).not.toEqual("");

    const assistant = await responseData<{ assistant_id: string }>(
      await request.post("/api/v1/assistants", {
        data: {
          display_name: assistantName,
          description: "Real protected-delivery browser probe.",
          engine_kind: "assistant",
          environment_name: environmentName,
          model_config_override: { model_name: model },
        },
      }),
      "create Hermes Assistant",
    );
    assistantId = String(assistant.assistant_id || "");
    expect(assistantId).not.toEqual("");

    await responseData(
      await request.post(`/api/v1/assistants/${assistantId}/workspace/wake`, {
        timeout: 240_000,
      }),
      "wake the real Hermes workspace",
    );
    await expect.poll(async () => {
      const detail = await responseData<{
        workspace_state?: string;
        current_sandbox_id?: string | null;
      }>(await request.get(`/api/v1/assistants/${assistantId}`), "read Hermes workspace");
      sandboxId = String(detail.current_sandbox_id || "");
      return detail.workspace_state;
    }, {
      message: "the real Hermes workspace must reach READY",
      timeout: 240_000,
      intervals: [1_000, 2_000, 3_000],
    }).toBe("READY");

    await page.goto("/assistants");
    await expect(tid(page, LIVE_TESTIDS.sessionsPage)).toBeVisible();
    const assistantCard = page.locator(
      `[data-testid="${LIVE_TESTIDS.assistantOption}"][data-assistant-id="${assistantId}"]`,
    );
    await expect(assistantCard).toHaveAttribute("data-assistant-state", "READY");
    // The browser output channel is a session-level resource: the client opens
    // one standing `follow=session` stream when the conversation mounts, and
    // every turn's frames return on it — there is no send-time GET to await.
    // Collect the stream responses from before navigation and judge them once
    // delivery is proven.
    const sessionStreamResponses: PageResponse[] = [];
    page.on("response", (candidate) => {
      if (candidate.request().method() !== "GET") return;
      const url = new URL(candidate.url());
      if (url.pathname.endsWith("/ai-stream") && url.searchParams.get("follow") === "session") {
        sessionStreamResponses.push(candidate);
      }
    });
    const [conversationRequest, conversationResponse] = await Promise.all([
      page.waitForRequest((candidate) =>
        candidate.method() === "POST"
        && candidate.url().includes(`/api/v1/assistants/${assistantId}/conversations`),
      ),
      page.waitForResponse((candidate) =>
        candidate.request().method() === "POST"
        && candidate.url().includes(`/api/v1/assistants/${assistantId}/conversations`),
      ),
      assistantCard.getByRole("button").click(),
    ]);
    expect(conversationRequest.postData()).toBeNull();
    const created = await responseData<{ session_id: string }>(
      conversationResponse,
      "start Hermes conversation",
    );
    sessionId = String(created.session_id || "");
    expect(sessionId).not.toEqual("");

    const session = await responseData<Record<string, unknown>>(
      await request.get(`/api/v1/sessions/${sessionId}`),
      "read Hermes Session",
    );
    expect(session).not.toHaveProperty("vault_ids");
    expect(session.engine_kind).toBe("assistant");
    expect(String(session.sandbox_id || "")).toBe(sandboxId);

    const security = await responseData<{
      available: boolean;
      default_action: string | null;
      egress_rules: Array<{ action: string; target: string }>;
      credential_names: string[];
      binding_names: string[];
    }>(
      await request.get(`/api/v1/admin/sandboxes/${sandboxId}/security`),
      "read real Hermes sandbox security posture",
    );
    if (EXPECT_PROTECTED_DELIVERY) {
      expect(security.available).toBe(true);
      expect(security.default_action).toBe("deny");
      expect(security.egress_rules.some((rule) => rule.action === "allow")).toBe(true);
      expect(security.credential_names).toContain("astrabox-model-gateway");
      expect(security.binding_names).toContain("astrabox-model-gateway");

      // The gateway key is a capability for model calls, not a host-wide key.
      // A host outside the policy must be unreachable from the box. The
      // sidecar resolves only policy hosts and nft drops direct routes, so
      // the refusal arrives as a resolution/connect failure (000), not as an
      // HTTP status; 4xx is also accepted so a sidecar that learns to answer
      // for unmatched hosts does not break this probe. 000 is meaningful
      // here only because the bound gateway path succeeds in this same
      // session — selective blocking, not a broken network.
      const unrelatedGatewayPath = await terminalOutput(
        request,
        sessionId,
        "curl -sS -o /dev/null -w 'ASTRABOX_UNMATCHED_GATEWAY_STATUS=%{http_code}' "
          + "-H 'Authorization: Bearer e2e-not-a-real-key' "
          + "http://gateway.astrabox.test/v1/models",
      );
      expect(unrelatedGatewayPath).toMatch(
        /ASTRABOX_UNMATCHED_GATEWAY_STATUS=(?:000|400|401|403)/,
      );
    } else {
      expect(security.available).toBe(false);
    }

    // AstraBox controls Hermes through its TUI JSON-RPC process. Advertising
    // the removed 9119 dashboard as an Assistant console would hand clients a
    // URL that can never work and would reintroduce an unnecessary open port.
    const obsoleteConsole = await request.get(
      `/api/v1/assistants/${assistantId}/workspace/console-url`,
    );
    expect(obsoleteConsole.status()).toBe(404);

    const composer = tid(page, LIVE_TESTIDS.composerPrompt);
    await expect(composer).toBeEnabled({ timeout: 60_000 });
    await composer.fill("Answer with the number only: what is 1 plus 1?");
    await tid(page, LIVE_TESTIDS.composerSubmit).click();
    await expect(tid(page, LIVE_TESTIDS.assistantText).last()).toContainText("2", {
      timeout: 240_000,
    });
    const sessionStreams = sessionStreamResponses.filter((candidate) =>
      new URL(candidate.url()).pathname.endsWith(`/api/v1/sessions/${sessionId}/ai-stream`),
    );
    expect(
      sessionStreams.length,
      "the delivered turn must have a browser output stream for this conversation",
    ).toBeGreaterThan(0);
    for (const stream of sessionStreams) {
      expect(stream.status(), "the browser output stream must be open").toBe(200);
      expect(
        stream.headers()["x-vercel-ai-ui-message-stream"],
        "the browser output must use the AI SDK UI message stream protocol",
      ).toBe("v1");
    }

    const gatewayCountCommand = [
      "printf 'ASTRABOX_GATEWAY_COUNT='",
      "ps -eo comm=,args= | awk '$1 ~ /^python([0-9.]*)?$/ && /[t]ui_gateway[.]entry/{count++} END{print count+0}'",
    ].join("; ");
    const terminalProcessMarker = `astrabox-terminal-${runId}`;
    await terminalOutput(
      request,
      sessionId,
      `bash -c 'exec -a ${terminalProcessMarker} sleep 600' &`,
    );
    const terminalProbeCountCommand = [
      "printf 'ASTRABOX_TERMINAL_PROCESS_COUNT='",
      `pgrep -fc '[a]strabox-terminal-${runId}' || true`,
    ].join("; ");
    expect(terminalProbeProcessCount(
      await terminalOutput(request, sessionId, terminalProbeCountCommand),
    )).toBe(1);
    const gatewaysBeforeDelete = hermesGatewayCount(
      await terminalOutput(request, sessionId, gatewayCountCommand),
    );
    expect(
      gatewaysBeforeDelete,
      "the completed conversation must still own a resident Hermes Gateway before deletion",
    ).toBeGreaterThan(0);

    // Exercise shared-workspace conversation cleanup inside the asserted flow;
    // focused tests separately pin Hermes session.close and OpenSandbox PTY deletion.
    const deleted = await responseData<{ status: string; killed: boolean }>(
      await request.delete(`/api/v1/sessions/${sessionId}`),
      "delete the Hermes conversation and terminate its resident Gateway process",
    );
    expect(deleted.status).toBe("conversation-deleted");
    expect(deleted.killed).toBe(false);
    sessionId = "";

    // The Assistant sandbox is intentionally retained, so inspect that same
    // production-style workspace through a fresh conversation. Exactly the
    // deleted conversation's Gateway must be gone while unrelated resident
    // processes remain.
    const cleanupProbe = await responseData<{ session_id: string }>(
      await request.post(`/api/v1/assistants/${assistantId}/conversations`),
      "start a cleanup probe conversation in the retained Assistant workspace",
    );
    cleanupProbeSessionId = String(cleanupProbe.session_id || "");
    expect(cleanupProbeSessionId).not.toEqual("");
    const cleanupProbeSession = await responseData<Record<string, unknown>>(
      await request.get(`/api/v1/sessions/${cleanupProbeSessionId}`),
      "read cleanup probe Session",
    );
    expect(String(cleanupProbeSession.sandbox_id || "")).toBe(sandboxId);
    const gatewaysAfterDelete = hermesGatewayCount(
      await terminalOutput(request, cleanupProbeSessionId, gatewayCountCommand),
    );
    expect(gatewaysAfterDelete).toBe(gatewaysBeforeDelete - 1);
    expect(terminalProbeProcessCount(
      await terminalOutput(request, cleanupProbeSessionId, terminalProbeCountCommand),
    )).toBe(0);

    await responseData(
      await request.delete(`/api/v1/sessions/${cleanupProbeSessionId}`),
      "delete the cleanup probe conversation",
    );
    cleanupProbeSessionId = "";
  } finally {
    if (sessionId) {
      await request.delete(`/api/v1/sessions/${sessionId}`).catch(() => undefined);
    }
    if (cleanupProbeSessionId) {
      await request.delete(`/api/v1/sessions/${cleanupProbeSessionId}`).catch(() => undefined);
    }
    if (assistantId) {
      await request.delete(`/api/v1/assistants/${assistantId}/workspace`, {
        timeout: 180_000,
      }).catch(() => undefined);
      await request.delete(`/api/v1/assistants/${assistantId}`, {
        timeout: 180_000,
      }).catch(() => undefined);
    }
    await request.put(`/api/v1/admin/environments/${environmentName}`, {
      data: {
        display_name: "Hermes E2E",
        description: "Environment for the real Hermes browser gate.",
        engine_kind: "assistant",
        enabled: false,
        sandbox_backend: "open_sandbox",
        runtime_template_name: HERMES_IMAGE,
        sandbox_tenancy: "agent",
        endpoint_provider: "litellm",
      },
    }).catch(() => undefined);
  }
});
