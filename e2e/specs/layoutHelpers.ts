import type { Page } from "@playwright/test";

/**
 * Fixtures + API stubbing for the LAYOUT gate.
 *
 * The layout gate asserts geometry, not behaviour, so it runs against stubbed
 * `/api` responses instead of a live backend: the row count and string lengths
 * become controlled inputs, and the gate stays fast enough to run on every
 * change. Behavioural coverage stays in the live-turn specs, which keep their
 * own config and their real uvicorn + sandbox stack.
 *
 * Every response is wrapped in the platform envelope the client unwraps
 * (`{code:'OK', message, data}` — see frontend/src/api.ts `request`).
 */

/** Viewports the console must survive. See VIEWPORTS' doc comment for why these four. */
export const VIEWPORTS = [
  // 1440 is the width the live-turn config pins and the only one the console was
  // ever verified at. 1280 is the common laptop. 1024 and 768 are the widths at
  // which columns start disappearing with no scrollbar to reach them — they are
  // in this list precisely because that loss is what this gate exists to catch.
  { name: "1440", width: 1440, height: 900 },
  { name: "1280", width: 1280, height: 800 },
  { name: "1024", width: 1024, height: 768 },
  { name: "768", width: 768, height: 1024 },
] as const;

/** Console list routes, each with the testid of the table it must render. */
export const CONSOLE_ROUTES = [
  { id: "agents", path: "/manage/agents" },
  { id: "deployments", path: "/manage/deployments" },
  { id: "assistants", path: "/manage/assistants" },
  { id: "sessions", path: "/manage/sessions" },
  { id: "sandboxes", path: "/manage/sandboxes" },
  { id: "environments", path: "/manage/environments" },
  { id: "credentials", path: "/manage/credentials" },
  { id: "errors", path: "/manage/errors" },
] as const;

const iso = (daysAgo: number) =>
  new Date(Date.UTC(2026, 6, 27 - daysAgo, 12, 34, 56)).toISOString();

const ROWS = 8;
const range = <T,>(n: number, f: (i: number) => T): T[] =>
  Array.from({ length: n }, (_, i) => f(i + 1));

const user = { user_id: "u-1", display_name: "Operator", email: "op@example.com" };

const agents = range(ROWS, (i) => ({
  agent_id: `ag-${String(i).padStart(3, "0")}`,
  name: `release-notes-writer-${i}`,
  display_meta: { display_name: `Release Notes Writer ${i}` },
  description: "Drafts the release notes for each tagged build.",
  model: "claude-opus-5",
  environment_name: "production-us-west-2",
  enabled: i % 5 !== 0,
  visibility: "public",
  version: i,
  updated_at: iso(i % 20),
}));

const sessions = range(ROWS, (i) => ({
  session_id: `s-${String(i).padStart(3, "0")}`,
  user_id: "u-1",
  template_name: `release-notes-writer-${i}`,
  title: `Draft the 2026.07 release notes (#${i})`,
  // "Running in background" is the longest status label the pill can carry; it is
  // what sets the status column's real minimum width.
  state: ["READY", "BUSY", "BACKGROUND_RUNNING", "WAITING_INPUT"][i % 4],
  permission_mode: "default",
  model_name: "claude-opus-5",
  sandbox_id: `sbx-${String(i).padStart(6, "0")}-a3f9c2e1b7d4`,
  agent_id: `ag-${String(i % 4).padStart(3, "0")}`,
  source_type: "agent",
  created_at: iso(i % 20),
  updated_at: iso(i % 20),
}));

const adminSessions = sessions.map((s, i) => ({
  session_id: s.session_id,
  template_name: s.template_name,
  state: s.state,
  sandbox_id: s.sandbox_id,
  created_at: s.created_at,
  updated_at: s.updated_at,
  duration_seconds: 60 * (i + 3),
  has_local_runtime: i % 2 === 0,
  user_id: s.user_id,
  display_name: "Operator",
  agent_id: s.agent_id,
  title: s.title,
}));

const errors = {
  errors: [
    {
      source: "session",
      severity: "error",
      session_id: "s-004",
      sandbox_id: "sbx-000004-a3f9c2e1b7d4",
      user_id: "u-1",
      display_name: "Operator",
      message: "Sandbox startup timed out",
      updated_at: iso(0),
    },
    {
      source: "turn_checkpoint",
      severity: "warning",
      session_id: "s-002",
      sandbox_id: "sbx-000002-a3f9c2e1b7d4",
      turn_id: "turn-002",
      user_id: "u-1",
      display_name: "Operator",
      message: "Turn stopped before completion",
      failed_at: iso(1),
    },
  ],
  counts: { total: 2, session: 1, turn_checkpoint: 1 },
};

const sandboxes = range(ROWS, (i) => ({
  sandbox_id: `sbx-${String(i).padStart(6, "0")}-a3f9c2e1b7d4`,
  backend: "open_sandbox",
  state: ["RUNNING", "STOPPED", "CREATING", "ERROR"][i % 4],
  created_at: iso(i % 12),
  image: "astrabox/sandbox-claude-code:latest",
  metadata: { session_id: `s-${String(i).padStart(3, "0")}` },
  session_id: `s-${String(i).padStart(3, "0")}`,
}));

const environments = range(ROWS, (i) => ({
  name: `environment-${i}`,
  display_name: `Environment ${i}`,
  enabled: i % 4 !== 0,
  engine_kind: i === ROWS - 1 ? "assistant" : "claude_code",
  sandbox_backend: "open_sandbox",
  endpoint_provider: "litellm",
  // Carried because the Agent form's eligibility test reads it and does NOT
  // tolerate its absence: `enabled` and `engine_available` are compared
  // `!== false`, but the session kind is `?.includes(...) === true`, so an
  // environment without this field is silently ineligible and the picker
  // renders with no options at all.
  supported_session_kinds: i === ROWS - 1 ? ["assistant"] : ["agent_chat"],
  engine_available: true,
}));

/** Model ids the selected Environment's gateway advertises to the Agent form. */
const MODELS = ["claude-opus-5", "claude-sonnet-5"];

// Bindings spread over four of the eight agents, two each. The `+ 1` puts them
// on ids `agents` above actually has: `i % 4` alone mints `ag-000`, and the two
// bindings landing on it would belong to nobody and never reach a page that
// resolves each binding's agent.
const deployments = range(ROWS, (i) => ({
  deployment_id: `dep-${String(i).padStart(4, "0")}-9f3c`,
  agent_id: `ag-${String((i % 4) + 1).padStart(3, "0")}`,
  enabled: i % 3 !== 0,
  scene: ["hmac", "scheduler", "channel:slack", "channel:lark"][i % 4],
  created_at: iso(i % 20),
  updated_at: iso(i % 10),
}));

const assistants = range(ROWS, (i) => ({
  assistant_id: `as-${String(i).padStart(3, "0")}`,
  owner_id: "u-1",
  display_name: `Ops Assistant ${i}`,
  icon: null,
  description: "Keeps a long-lived workspace for on-call triage.",
  engine_kind: "assistant",
  environment_name: "production-us-west-2",
  permission_mode_default: "acceptEdits",
  workspace_state: ["READY", "NOT_MATERIALIZED", "HIBERNATING"][i % 3],
  current_sandbox_id: `sbx-${String(i).padStart(6, "0")}-a3f9c2e1b7d4`,
  created_at: iso(i % 20),
}));

const vaults = [
  {
    vault_id: 'vlt-production-services',
    display_name: 'Production services',
    metadata: {},
    archived_at: null,
    credentials: [
      {
        credential_id: 'vcr-production-mcp',
        vault_id: 'vlt-production-services',
        display_name: 'Production MCP',
        auth: { type: 'static_bearer', mcp_server_url: 'https://mcp.example.test/mcp' },
        archived_at: null,
      },
    ],
  },
  {
    vault_id: 'vlt-staging-services',
    display_name: 'Staging services',
    metadata: {},
    archived_at: null,
    credentials: [],
  },
];

/**
 * One turn that dispatches a subagent, because the subagent drawer is only
 * reachable through one.
 *
 * Two things have to line up or the drawer never opens: the dispatching call's
 * tool name has to be one ToolParts treats as a dispatch (`Agent` / `Task`),
 * and the `childRunId` on the subagent frames has to equal that call's id — the
 * registry buckets by it, and `Open in Agents` passes the tool call id. That
 * equality is the Claude adapter's, not a convenience: it takes the child run's
 * id from `scope["tool_use_id"]` (engine/claude_code_background.py).
 *
 * `childRunId` and `engineKind` are the two fields the registry requires;
 * frames without them are not partial entries, they are dropped, and the panel
 * renders its empty state with nothing to say why. `canonical_child_run_data`
 * mints them on every frame the platform carries, so a fixture missing them is
 * a shape the backend cannot emit.
 */
const TRANSCRIPT = [
  {
    session_id: "s-001",
    turn_id: "t-1",
    role: "user",
    content: "Draft the release notes and check the changelog.",
    created_at: iso(0),
  },
  {
    session_id: "s-001",
    turn_id: "t-1",
    role: "assistant",
    content: "",
    created_at: iso(0),
    blocks: [
      { type: "text", text: "I'll dispatch a subagent to check the changelog." },
      {
        type: "tool_use",
        id: "tu-task-1",
        name: "Task",
        input: { description: "Check the changelog", subagent_type: "Explore" },
      },
      {
        type: "tool_result",
        tool_use_id: "tu-task-1",
        is_error: false,
        tool_result_state: "output-available",
        content: "Found 2 missing entries.",
      },
      { type: "text", text: "Two entries were missing; added." },
    ],
  },
];

/**
 * Minimal schema for the create/record forms. The layout gate only measures
 * list-page geometry, so this needs to be well-formed, not complete — the
 * authoritative shape is
 * astrabox/core/service/orchestrator/{agent,environment}_schema.py.
 */
const AGENT_FORM_SCHEMA = {
  version: 1,
  groups: [{ id: "identity" }, { id: "model" }, { id: "runtime" }],
  fields: [
    { key: "name", type: "string", group: "identity", required: true },
    { key: "description", type: "text", group: "identity" },
    // A list field is here on purpose: it is the one shape a `<label for>`
    // cannot name, so a schema without one lets that case pass untested.
    { key: "tags", type: "string_list", group: "identity" },
    { key: "model", type: "string", group: "model", required: true },
    { key: "environment_name", type: "env_ref", group: "runtime", required: true },
    { key: "enabled", type: "boolean", group: "runtime" },
  ],
};

const ENVIRONMENT_FORM_SCHEMA = {
  version: 1,
  groups: [{ id: "basic" }, { id: "runtime" }],
  fields: [
    { key: "name", type: "string", group: "basic", required: true },
    { key: "display_name", type: "string", group: "basic" },
    { key: "description", type: "text", group: "basic" },
    {
      key: "engine_kind",
      type: "enum",
      group: "basic",
      required: true,
      enum: ["claude_code", "assistant"],
    },
    { key: "enabled", type: "boolean", group: "basic" },
    {
      key: "sandbox_tenancy",
      type: "enum",
      group: "runtime",
      enum: ["conversation", "agent"],
    },
    {
      key: "sandbox_backend",
      type: "enum",
      group: "runtime",
      enum: ["open_sandbox"],
    },
    {
      key: "networking",
      type: "object",
      group: "runtime",
      complex: true,
      item_schema: [
        {
          key: "type",
          type: "enum",
          enum: ["unrestricted", "limited"],
          required: true,
        },
        { key: "allowed_hosts", type: "string_list" },
        { key: "allow_mcp_servers", type: "boolean" },
      ],
    },
  ],
};

/** Resolve one `/api/v1/...` path to its fixture, or undefined when unmapped. */
function resolve(pathname: string): unknown {
  const t = pathname.replace(/\/+$/, "").split("/").filter(Boolean).slice(2);
  const at = (...parts: string[]) =>
    t.length === parts.length && parts.every((p, i) => p === "*" || t[i] === p);

  if (at("user", "current")) return user;
  if (at("agents")) return agents;
  if (at("assistants")) return assistants;
  if (at("admin", "vaults")) return {
    vaults,
    credential_delivery: {
      deployment_mode: 'local_development',
      model_credentials: 'egress_placeholder',
      mcp_credentials: 'egress_injection',
      environment_credentials: 'egress_placeholder',
    },
  };
  if (at("sessions")) return { sessions, has_more: false, next_cursor: null };
  // The list pages re-fetch each row's detail by id; without these the response
  // is null and the page throws on the first property read.
  if (at("agents", "*")) return agents.find((a) => a.agent_id === t[1]) ?? agents[0];
  if (at("agents", "*", "conversations")) return { session_id: "s-001" };
  if (at("assistants", "*")) {
    return assistants.find((a) => a.assistant_id === t[1]) ?? assistants[0];
  }
  if (at("assistants", "*", "conversations")) return { session_id: "s-001" };
  if (at("sessions", "*")) return sessions.find((s) => s.session_id === t[1]) ?? sessions[0];
  // The raw record read. The whole `MessagePage` (frontend/src/api.ts), not the
  // part the transcript reads.
  if (at("sessions", "*", "messages")) {
    return {
      messages: TRANSCRIPT,
      has_more: false,
      active_turn_overlay: null,
      session_frame_seq: null,
      pending_interaction: null,
    };
  }
  // What the console draws its transcript from (`useFirstPageMessages`): the
  // same records, paged by record, with the fields that page refuses to be
  // without. It rejects a `paging_mode` other than `blocks`, and
  // `requireSessionFrameSeq` rejects a page that omits `session_frame_seq` —
  // where the session's frames end, and what a client with no turn in flight
  // opens the output channel with; `null` is the value the server sends for a
  // session with no frames journalled. An incomplete fixture here is not a
  // session view missing a cursor, it is the session view replaced by "Failed
  // to load messages" for every spec that opens one.
  if (at("sessions", "*", "history-blocks")) {
    return {
      messages: TRANSCRIPT,
      has_more: false,
      next_cursor: null,
      paging_mode: "blocks",
      block_count: TRANSCRIPT.length,
      active_turn_overlay: null,
      session_frame_seq: null,
      pending_interaction: null,
    };
  }
  if (at("sessions", "*", "child-runs")) {
    return {
      session_id: t[1],
      child_runs: [{
        child_run_id: "tu-task-1",
        engine_kind: "claude_code",
        depth: 1,
        engine_event: "task_notification",
        engine_status: "completed",
        engine_reason: null,
        closed: true,
        operations: [],
        task_type: "Explore",
        description: "Check the changelog",
        usage: { total_tokens: 18402, tool_uses: 6, duration_ms: 21300 },
      }],
    };
  }
  if (at("sessions", "*", "child-runs", "*", "messages")) {
    return {
      session_id: t[1],
      child_run_id: t[3],
      messages: [{
        role: "assistant",
        message_id: "sam-1",
        content: [{ type: "text", text: "Scanning entries added since the last tag." }],
      }],
    };
  }
  if (at("sessions", "*", "files", "list")) {
    return {
      root_path: "/workspace",
      current_path: "/workspace",
      parent_path: null,
      entries: range(4, (i) => ({
        path: `/workspace/file-${i}.md`,
        name: `file-${i}.md`,
        kind: "file",
        size: 1024 * i,
        modified_at: iso(i),
      })),
    };
  }
  // The Agent form reads its schema, its Environment choices and that
  // Environment's models from the secret-free agent-configuration surface — not
  // from `admin/*`, which is the Environment form's. `listAgentEnvironments`
  // rejects a non-array (`expectList`), so an unmapped path here is not a page
  // with an empty picker: it is the create page's whole load failing.
  if (at("agent-configuration", "schema")) return AGENT_FORM_SCHEMA;
  if (at("agent-configuration", "environments")) return environments;
  if (at("agent-configuration", "environments", "*", "models")) return { models: MODELS };
  if (at("admin", "integrations")) return {
    services: [
      {
        id: "casdoor",
        name: "Casdoor",
        category: "identity",
        admin_url: "https://identity.example.test",
      },
      {
        id: "casdoor-api",
        name: "AstraBox API",
        category: "api_access",
        admin_url: "https://identity.example.test/applications/astrabox/astrabox-api",
      },
      {
        id: "litellm",
        name: "LiteLLM",
        category: "model_gateway",
        admin_url: "/litellm",
      },
    ],
  };
  if (at("admin", "environment-schema")) return ENVIRONMENT_FORM_SCHEMA;
  if (at("admin", "environments")) return environments;
  // The paginated envelope the guarded client requires: a bare array reads as
  // malformed now, which turned every stubbed console-sessions surface into the
  // error card and left layout.tone with zero pills to measure.
  if (at("admin", "sessions", "all")) {
    return {
      items: adminSessions,
      pagination: {
        page: 1,
        page_size: 50,
        total_items: adminSessions.length,
        total_pages: 1,
        has_next_page: false,
      },
    };
  }
  if (at("admin", "errors")) return errors;
  if (at("admin", "sandboxes")) {
    return {
      backend: "open_sandbox",
      items: sandboxes,
      pagination: {
        page: 1,
        page_size: 50,
        total_items: sandboxes.length,
        total_pages: 1,
        has_next_page: false,
      },
    };
  }
  if (at("admin", "sandboxes", "*", "security")) {
    return {
      sandbox_id: t[2],
      available: true,
      default_action: "deny",
      egress_rules: [{ action: "allow", target: "gateway.astrabox.test:80" }],
      credential_names: [],
      binding_names: [],
      detail: null,
    };
  }
  if (at("admin", "sandboxes", "*", "diagnostics", "*")) {
    return {
      sandbox_id: t[2],
      backend: "open_sandbox",
      scope: t[4],
      content_type: "text/plain",
      text: "sandbox report",
      truncated: false,
      known_scopes: ["summary", "inspect", "events", "logs"],
    };
  }
  if (at("agents", "*", "prepared-runtime")) {
    return {
      enabled: true,
      ready: true,
      prepared_count: 1,
      state: "prepared",
      placement: "agent",
      runtime_generation: "runtime-generation-1",
      sandbox_id: "sbx-prepared",
      last_error: null,
    };
  }
  // Per-agent, like the route it stands in for. The Triggers page has no route
  // that lists every binding, so it fans out over the agents and concatenates;
  // answering each of those calls with the whole set would hand that page one
  // copy of every binding per agent, all sharing a key.
  if (at("admin", "agents", "*", "deployments")) {
    return deployments.filter((d) => d.agent_id === t[2]);
  }
  if (at("admin", "agents", "*", "credential-vaults")) return {
    target_type: "agent",
    target_id: t[2],
    vault_ids: [],
    vaults: [],
  };
  if (at("admin", "assistants", "*", "credential-vaults")) return {
    target_type: "assistant",
    target_id: t[2],
    vault_ids: [],
    vaults: [],
  };
  return undefined;
}

/**
 * A value long enough that nothing can lay it out without deciding what gives
 * way. Latin with no break opportunity is the harder case than long prose.
 */
const LONG =
  "ProductionKubernetesClusterAgentHarnessConfigurationRevisionAlpha0000000001";

/** Swap every display string for one that cannot fit, leaving shape untouched. */
function stress(value: unknown): unknown {
  if (typeof value === "string") {
    // Ids and enum-ish values keep their shape; only human-facing text grows.
    return /^(s-|ag-|as-|dep-|sbx-|u-)/.test(value) || value === value.toUpperCase()
      ? value
      : `${value} ${LONG}`;
  }
  if (Array.isArray(value)) return value.map(stress);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([k, v]) => [
        k,
        ["name", "title", "display_name", "description", "model", "environment_name"].includes(k)
          ? stress(v)
          : v,
      ]),
    );
  }
  return value;
}

/**
 * Answer every `/api` call from fixtures. An unmapped path resolves to `data:null`
 * rather than failing the request, so a surface that grows a new call renders its
 * own empty state instead of turning a layout failure into a network failure.
 */
export async function stubApi(
  page: Page,
  opts?: {
    stress?: boolean;
    integrations?: Array<{
      id: string;
      name: string;
      category: "identity" | "api_access" | "model_gateway";
      admin_url: string;
    }>;
  },
): Promise<void> {
  await page.route("**/api/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    // Auth probing is intentionally not an API-envelope route. Returning the
    // normal {data: ...} fixture shape here makes RequireAuth read
    // `authenticated` as missing and turns every layout test into a login-page
    // test without any of its assertions noticing why.
    if (pathname === "/api/v1/auth/session") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ authenticated: true, user }),
      });
      return;
    }
    const raw = pathname === "/api/v1/admin/integrations" && opts?.integrations
      ? { services: opts.integrations }
      : resolve(pathname);
    const data = opts?.stress ? stress(raw) : raw;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        code: "OK",
        message: data === undefined ? "ok (unmapped)" : "ok",
        data: data ?? null,
      }),
    });
  });
}

/**
 * Hold until a width measurement means something.
 *
 * Two things move after the table first becomes visible, and both change
 * geometry: an in-flight transition, and a webfont that has not swapped in yet.
 * The second one is the flaky one — text laid out in the fallback face is a
 * different width, so a measurement taken before `document.fonts.ready` can be
 * off by enough to flip an overflow assertion. Vite's first cold route is where
 * this shows up.
 */
export async function settle(page: Page): Promise<void> {
  await page.addStyleTag({
    content: `*,*::before,*::after{
      animation-duration:0s!important;animation-delay:0s!important;
      transition-duration:0s!important;transition-delay:0s!important}`,
  });
  await page.evaluate(() => document.fonts.ready.then(() => undefined));
  // One rAF so the swapped face is laid out before anything is measured.
  await page.evaluate(
    () => new Promise<void>((r) => requestAnimationFrame(() => r())),
  );
  await page.waitForTimeout(250);
}
