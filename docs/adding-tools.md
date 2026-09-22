# Agent tools and extensions

> Equip an Agent with MCP servers, Skills, and Plugins while preserving its
> program's native tools.

MCP servers, Skills, and Plugins extend what an Agent can do. Configure them when creating or updating an Agent to give it external services, domain procedures, and packaged extensions.

## What Extensions Do

The selected Agent program supplies its own native tools, such as reading files, editing code, running commands, and searching. AstraBox does not duplicate those tools in a separate `tools` field.

Agent configuration adds three kinds of extension:

- **MCP servers** expose live functions and data.
- **Skills** provide reusable instructions, procedures, and supporting files.
- **Plugins** package Skills, commands, and MCP definitions together.

When AstraBox first prepares or rebuilds a Session runtime, it prepares the configured extensions in the sandbox. The Agent program decides when to use them based on the task.

Prewarming can prepare those extensions before a Session starts. To fetch
Plugins and Skills again using the saved configuration, use **Reprepare** on
the Agent. This replaces only unclaimed capacity; existing Sessions keep their
runtimes, and pinned commits remain pinned.

## Available Extensions

MCP servers use one of two connections: a remote HTTP service outside the
sandbox, or a local stdio command inside it. Connection type and configuration
source are separate choices. An Administrator-managed remote MCP server is
still remote; administrator management changes who maintains its definition.

| Extension | Purpose | Typical scenarios |
| --- | --- | --- |
| Remote MCP server | Call an MCP service running outside the Session sandbox | Databases, ticketing systems, document services, internal APIs |
| Local stdio MCP server | Start an MCP command inside the Session sandbox | Filesystem tools, local analyzers, command-based integrations |
| Skill | Supply a focused procedure and supporting files | Code review, release checks, report generation |
| Plugin | Install a reviewed bundle of Skills, commands, and MCP definitions | Team extension packs and domain workflows |

Notes:

- A server outside the sandbox is a **remote MCP server** whether its definition is saved on the Agent, managed by an administrator, or packaged in a Plugin. Those choices change only who maintains the configuration.
- The selected Agent program determines which extension formats it supports. AstraBox rejects an Agent configuration that its selected program does not consume.
- Saving an Agent does not reconfigure a task already running. AstraBox uses the latest extension configuration when it first prepares or rebuilds a Session runtime.
- Network rules and credentials are separate from an MCP definition. A remote server must be reachable from the sandbox, and protected credentials must match its exact destination.

## Native and Browser Capabilities

Browser and web capabilities belong to the selected Agent program or to an extension it supports. AstraBox does not define a second platform-level browser toolset with its own tool names.

Use the Agent program's native configuration when it includes browser support. Otherwise, add a browser MCP server or reviewed Plugin and allow its remote and package hosts in the Environment's network policy.

When integrating browser capabilities:

- use the names and permission rules defined by the Agent program or MCP server;
- changing the Agent does not alter a task already running; AstraBox uses the latest settings when it next prepares or rebuilds the Session runtime;
- verify one real browser operation and any live-preview path the extension provides;
- do not assume another Agent program implements the same browser behavior.

## Current Formats

Agent-level extensions use the native formats supported by the selected Agent program. Open **Console > Agents**, create or open an Agent, then use the **MCP, Skills and Plugins** section. Project repositories are configured separately under **Workspace configuration**.

Keep the MCP service name, URL, transport, and permitted request headers with
the service definition. Store the secret value in an AstraBox Credential Vault
and assign that Vault to the Agent. At runtime the sandbox connects to the
service itself and the selected provider attaches the assigned credential at
the matching protected outbound request, keeping the secret outside the
sandbox.

![Configure MCP servers, Skills, and Plugin repositories in the AstraBox console](./img/agent-create-console-en.png)

For an existing Agent, **MCP configuration** and **Skill configuration** each group a LiteLLM-managed multi-select with a custom configuration field. The two sources are combined; MCP server names must be unique, and identical Skill sources are deduplicated. Empty fields show examples without storing them. Press Tab in an empty field to accept its example, then edit and explicitly save it. Shift+Tab or Tab in a filled field keeps normal keyboard navigation. The custom configuration fields use these formats:

```json
{
  "mcp_servers": {
    "public-data": {
      "type": "http",
      "url": "https://mcp.example.com/mcp"
    }
  },
  "skills": [
    "https://github.com/example/agent-skills.git@REVIEWED_COMMIT_SHA#skills/code-review"
  ],
  "plugin_repos": [
    {
      "url": "https://github.com/example/agent-plugins.git",
      "sha": "REVIEWED_COMMIT_SHA",
      "plugin_paths": ["plugins/code-review"]
    }
  ]
}
```

The system prompt and every extension are optional. Configure only the capabilities the Agent needs, then click **Create** or **Save**.

## Extension Configuration Examples

### Minimal (remote MCP only)

```json
{
  "mcp_servers": {
    "public-data": {
      "type": "http",
      "url": "https://mcp.example.com/mcp"
    }
  }
}
```

### Full development stack

```json
{
  "mcp_servers": {
    "workspace-files": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
    },
    "public-data": {
      "type": "http",
      "url": "https://mcp.example.com/mcp"
    }
  },
  "skills": [
    "https://github.com/example/agent-skills.git@REVIEWED_COMMIT_SHA#skills/code-review"
  ],
  "plugin_repos": [
    {
      "url": "https://github.com/example/agent-plugins.git",
      "sha": "REVIEWED_COMMIT_SHA",
      "plugin_paths": ["plugins/development"]
    }
  ]
}
```

## Update extension configuration

Open the Agent in **Console > Agents**. Change the selected MCP servers or Skills in the extension card, or edit the direct MCP, Skill, and Plugin repository fields, then click **Save** for that section.

:::note
Saving does not reconfigure a task already running. AstraBox uses the latest saved Agent when it first prepares or rebuilds a Session runtime. If another user saves the same Agent first, the console reports a conflict instead of overwriting that change.
:::

## Inspect current extension configuration

Open the Agent in **Console > Agents**. Administrator-managed MCP servers and Skills appear in the **MCP servers and Skills** card. Direct MCP definitions, Skill sources, and Plugin repositories appear in the **MCP, Skills and Plugins** section.

## FAQ

**Q: What happens if I do not configure extensions?**

A: The Agent program retains its native tools. It simply has no additional MCP servers, Skills, or Plugins configured on the Agent.

**Q: Can extension configuration be overridden at the Session level?**

A: Not currently. Extension configuration belongs to the Agent. AstraBox resolves the current Agent configuration when it first prepares or rebuilds a Session runtime. Use another Agent when a different extension set is required.

**Q: Does the order of extension entries matter?**

A: The Agent program decides when to use an extension based on the task. Names must be unique where the program addresses an extension by name.

**Q: Will extension formats change over time?**

A: The format belongs to the Agent program, MCP protocol, Skill specification, or Plugin format. Pin reviewed Git revisions and follow the corresponding upstream release notes.

## Next steps

- [Permission modes](permission-modes.md) — Control how the Agent program asks
  for approval before tool actions.
- [Agent Skills](agent-skills.md) — Attach reusable domain procedures to an
  Agent.
- [Defining an Agent](authoring-agents.md) — Review the complete Agent
  configuration.
- [Sessions](sessions.md) — Start and continue Agent work.
