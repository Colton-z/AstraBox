# Usage guide

Choose a starting point for your task. The web console, HTTP API, CLI and MCP
interface use the same AstraBox resources and access controls.

## Choose an Agent or Assistant

| Your task | Start here | What you keep |
|---|---|---|
| Give a reusable Agent a task, work with a team, or embed it in a product | [Define an Agent](authoring-agents.md), then [start a Session](sessions.md) | An Agent definition and separate conversations with their own history and workspace |
| Continue personal work across conversations | [Use an Assistant](assistants.md) | An owner-only workspace, native program state, memory and conversations |
| Run without someone opening the console | [Configure a Deployment](deployments.md) | A schedule, signed webhook or messaging binding targeting an Agent or Assistant |

An Environment selects the installed Agent program, sandbox, model connection
and network settings. Administrators prepare these resources before users start
work. An unavailable Agent remains listed, with **Start conversation** disabled;
its manager can inspect its Agent and Environment configuration.

## Choose how to work

| What you want to do | Guide |
|---|---|
| Try a self-hosted installation and send the first task | [Quickstart](quickstart.md) |
| Work in a browser with streamed replies, approvals, tools and files | [Sessions](sessions.md), [files](files.md) and [voice input](voice-input.md) |
| Save or share an interactive page made by an Agent | [Artifacts](artifacts.md) |
| Add connected apps, MCP servers, Skills or plugins | [Agent tools and extensions](adding-tools.md) |
| Let an Assistant delegate to another Agent | [Agent-to-Agent communication](a2a.md) |
| Receive work in a messaging product | [Channels](channels.md) |
| Run periodic work or invoke it from another system | [Schedules](schedules.md) and [webhooks](webhooks.md) |
| Integrate a conversation into your application | [HTTP API](api.md) and [event stream](events-stream.md) |
| Manage definitions and submit work from scripts or a coding assistant | [CLI](cli/overview.md) and [Agent MCP](agent-mcp.md) |

The console handles navigation, draft input, streaming and interaction cards.
An API integration owns its interface: submit an input, keep its receipt, then
follow the output stream. Acceptance means the message is stored; the Agent
program decides when it consumes it and which response answers it. Answer a
pending approval or question through its interaction endpoint. See
[sending messages](sessions.md#send-a-message).

## Return to your work

Leaving a browser page does not stop the Agent. Reopen the Session to read saved
messages, tools, reasoning and recorded interactions, and follow new output.
Use **Stop** to interrupt the active response; queued inputs are not withdrawn.
Archive a conversation to take it offline while retaining its history.

If a sandbox is confirmed lost, the affected response fails. AstraBox retains
history and native state so the Agent program can resume on replacement compute;
it does not automatically replay work the program already consumed. Native
state recovery works without a persistent workspace volume. Keeping task files
when a sandbox is released or lost requires that optional storage. See
[Session lifecycle](sessions.md) and [workspace storage](deploy.md#where-conversation-workspaces-live).

## Run and extend AstraBox

| Your responsibility | Guide |
|---|---|
| Install or upgrade a deployment | [Deploy AstraBox](deploy.md) |
| Preserve selected records through an offline upgrade | [Offline data import](data-import.md) |
| Configure model routes, team login or protected credentials | [Models](models.md), [team login](team-login.md) and [credentials](credentials.md) |
| Plan sandbox capacity and optional file persistence | [Environments](environments.md) and [OpenSandbox](providers/opensandbox.md) |
| Embed AstraBox in a Python application | [Embedding](embedding.md) |
| Supply a program, sandbox, identity or database integration | [Extension interfaces](architecture.md#plugin-interfaces) |

The built-in database provider uses PostgreSQL. External relational providers
must supply their schema and coordination guarantees; MySQL type mappings alone
do not constitute a supported MySQL deployment.
