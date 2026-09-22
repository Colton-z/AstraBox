# Defining an Agent

> Create a reusable Agent configuration for a self-hosted runtime.

An Agent runs an installed Agent program with a model, system prompt, extensions, and Environment. Multiple Sessions can share one Agent. Saving an Agent does not interrupt work already in progress; AstraBox resolves the latest saved Agent and Environment when it first prepares or rebuilds a Session runtime.

## Core elements

You can think of an Agent as a "job description":

| Element | Description |
| --- | --- |
| **model** | The Agent's intelligence level |
| **system prompt** | The Agent's behavioral guidelines |
| **MCP servers** | External services the Agent can call |
| **Skills and Plugins** | Reusable procedures and packaged extensions the Agent can use |

The Agent executes tasks inside a Session. AstraBox keeps the Agent available, starts or reuses its sandbox, streams progress, and stores the Session outside the sandbox.

## Field reference

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `agent_id` | string | — | System-generated Agent ID. |
| `name` | string | Yes | The Agent name. |
| `description` | string | No | A description for the Agent. |
| `use_cases` | array | No | Short examples of work this Agent handles. |
| `display_meta` | object | No | Display name, icon, and tags shown by clients. |
| `model` | string | Yes | The model identifier. See details below. |
| `system` | string | No | The system prompt. |
| `engine_options` | object | No | Advanced options defined by the Agent program selected through the Environment. |
| `skills` | array | No | Skill sources loaded into the Session sandbox. |
| `mcp_servers` | object | No | MCP server configurations, keyed by server name. |
| `default_repo` | object | No | The source repository cloned for each new Session. |
| `plugin_repos` | array | No | Plugin repositories and the reviewed revisions to install. |
| `environment_name` | string | Yes | The Environment that supplies the Agent program, sandbox, network policy, and model connection. |
| `exposure_mode` | string | No | Where the Agent is available: `chat_only`, `mcp_only`, or `both`. |
| `idle_hibernate_seconds` | integer | No | How long the Session runtime may remain idle before the Environment's pause or removal behavior applies. |
| `prewarm_enabled` | boolean | No | Whether AstraBox keeps a prepared runtime ready for this Agent. |
| `enabled` | boolean | No | Whether the Agent can start new Sessions. |
| `visibility` | string | — | Agent authorization mode: `public`, `private`, or `allowlist`. Managed through the Agent access endpoint. |
| `admins` | array | — | Accounts allowed to manage the Agent. Managed through the Agent access endpoint. |
| `allowed_user_ids` | array | — | Accounts allowed by `allowlist` authorization. Managed through the Agent access endpoint. |
| `version` | integer | — | The optimistic-concurrency revision. It starts at 1 and increments when a setting that affects the Agent runtime changes. |
| `state` | string | — | The Agent lifecycle state. |
| `created_at` | string | — | The creation timestamp in ISO 8601 format. |
| `updated_at` | string | — | The timestamp of the last update. |

### model

The `model` field specifies the model that the Agent uses. AstraBox passes this identifier to the model endpoint configured by the selected Environment:

| Value | Description |
| --- | --- |
| The value configured by `ANTHROPIC_MODEL` | The model served by the bundled Anthropic-compatible route. |
| Any model ID served by the selected endpoint | A model exposed by an external LiteLLM gateway, OpenAI-compatible endpoint, or local model service. |

A self-hosted deployment is not limited to a fixed vendor catalog. See [Connect a model service](models.md) for endpoint and credential configuration.

### Native runtime JSON {#native-runtime-json}

Select the Environment first: its engine declares which JSON blocks the Agent
can override. Each editor identifies the native target and its merge rules.
The platform validates block names, JSON object shape and platform-managed
keys, not a catalog of vendor options inside each block. The installed runtime
interprets those options; unknown fields follow that supplier's behavior.
The engine adapter passes configuration to that program's native files or API.
Assign credentials through the managed model connection and Credential Vault;
credential values are not runtime JSON overrides.

| Engine | `engine_options` block | Native target |
| --- | --- | --- |
| Claude Code | `sdk_options` | `ClaudeAgentOptions`; nested `settings` is serialized for CLI `--settings`. |
| Codex | `config` | `thread/start` and `thread/resume` configuration overrides. |
| Codex | `turn_start` | Each `turn/start` parameters object. |
| Codex | `model_catalog` | The complete native `models.json` object. |
| Pi | `settings` | The conversation's `~/.pi/agent/settings.json` root. |
| DeepSeek Harness | `session_create` | Native `session/create` request parameters, including `agentPreset`. |

Enter `{}` to supply an empty native object, or leave an editor blank to remove
that block. Runtime JSON and model-gateway
configuration serve different purposes; both remain available. The Model
field links to the configured LiteLLM management page when the deployment
provides one. Session identity, transport and assigned model credentials remain
platform-managed rather than arbitrary JSON overrides.

### MCP servers, Skills, and Plugins

The selected Agent program supplies its own native tools, such as reading files, editing code, and running commands. AstraBox does not redefine those tools. Agent configuration adds MCP servers, Skills, and Plugins in the native formats supported by that program. In **Console > Agents**, open an Agent to select administrator-managed remote MCP servers and Skills, or add a Plugin repository directly to the Agent.

For more configurations, see [MCP servers, Skills, and Plugins](adding-tools.md).

## Managing Agents

The web console covers the following common workflows. For programmatic management, see the [HTTP API](api.md).

### Create

Open **Console > Agents**, click **New agent**, then enter a name, Environment, and model. The system prompt and every extension are optional. Click **Create** when the Agent has the capabilities it needs.

Enable prewarming to prepare a runtime before the next Session needs it. Its
Agent configuration, configured Skills and Plugins, and engine startup are
completed before it is offered for use. The Agent's **Prewarm status** reports
whether prepared capacity is ready or preparation failed.

![Create an Agent in the AstraBox console](./img/agent-create-console-en.png)

### Read

Open **Console > Agents** to list the Agents available to your account. Select an Agent to view its model, Environment, and capabilities. If you are allowed to manage it, the page also shows editable configuration and authorization settings.

### Update

Open the Agent, edit the relevant section, then click **Save** on that section. The console includes the current `version` so a concurrent edit produces a conflict instead of silently replacing another user's changes. Saving does not reconfigure a task already running; the latest settings are used when AstraBox first prepares or rebuilds a Session runtime.

To fetch Skills and Plugins again using the saved configuration, select
**Reprepare** in **Prewarm status**. Save or revert pending edits first. This
replaces prepared capacity that has not been claimed by a Session; existing
Sessions keep their running sandboxes. A repository pinned to a commit keeps
that commit until its configured revision changes.

### Delete

Delete is currently available through `DELETE /api/v1/agents/{agent_id}`. Deleting an Agent removes it from further use and starts releasing runtime resources it owns. Existing Session records and transcripts remain, but a Session cannot start or rebuild its runtime after the Agent it references has been deleted.

## Versioning

The Agent API uses optimistic concurrency control (OCC):

- The `version` starts from `1` upon creation.
- After a successful update to a runtime field, the `version` is automatically incremented by 1.
- An update may include the current `version`. If it does not match the server-side version, AstraBox returns **409** `AGENT_VERSION_CONFLICT`.
- An update without `version` is allowed, but a concurrent write detected while it is being applied still returns **409**.

This prevents concurrent modifications from overwriting each other.

### Handling 409

If your request fails because the version is outdated, you receive an error like this:

```json
{
  "code": "AGENT_VERSION_CONFLICT",
  "message": "agent version mismatch: supplied 1, stored 2",
  "data": null
}
```

To resolve the conflict:

1. `GET` the latest Agent to retrieve the current `version`
2. Re-apply your desired changes to the latest object data.
3. Use the new `version` to perform another `PUT`.

## Best practices

1. **Naming conventions** — Use the `team-purpose` format, such as `backend-code-review`, `frontend-test-gen`.
2. **Prompt refinement** — Specify the role, output format, and constraints in the `system` field.
3. **Apply the principle of least privilege** — Add only the MCP servers, Skills, Plugins, repositories, credentials, and network destinations required for the Agent's work.
4. **Use display metadata effectively** — Add a clear description and tags so users can find the right Agent.
5. **Use version checks for automated updates** — Include the version when a service edits an Agent so concurrent changes produce a conflict instead of replacing one another.

See [Multiagent orchestration](multi-agents.md) when the installed Agent
program should divide work among child runs.

## FAQ

**Q: How does an Agent's network policy interact with extensions and credentials?**

A: Use `networking.type: unrestricted` for open internet access. `limited` admits
the listed hosts plus destinations AstraBox can derive for the model, platform
callbacks, and Plugin repositories; remote Agent MCP servers are admitted only
when `allow_mcp_servers` is true. Credential Vault assignments leave the
Environment record unchanged; their binding hosts are added to the effective
sandbox policy.

**Q: Does updating an Agent affect currently running Sessions?**

A: Saving does not interrupt work already executing. A Session keeps using its current runtime until AstraBox has to prepare or rebuild it; that preparation resolves the latest saved Agent and Environment.

**Q: Can the MCP server, Skill, and Plugin lists be empty?**

A: Yes. The Agent program retains its native capabilities. Add extensions only when the Agent needs external services, reusable procedures, or packaged commands.

**Q: Is there a length limit for the `name` field?**

A: AstraBox does not impose a fixed length limit. Use a concise name that people can recognize in the console and integrations.

**Q: How do I restore an earlier Agent configuration?**

A: AstraBox does not keep selectable configuration history. Save the Agent configuration before updating it. To restore it, `PUT` the saved configuration with the latest `version` number.

## Next steps

- [Environment](environments.md) — Configure where the Agent runs.
- [Start a Session](sessions.md) — Create a Session with an Agent.
- [MCP servers, Skills, and Plugins](adding-tools.md) — Configure extensions and credentials.
- [Multiagent orchestration](multi-agents.md) — Inspect and control delegated child runs.
