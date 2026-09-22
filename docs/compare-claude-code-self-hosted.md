# AstraBox and Claude Code self-hosted environments

[Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview)
provides a prebuilt, configurable Agent runtime and the infrastructure needed
to run it. An Environment can use an Anthropic-managed sandbox or a self-hosted
environment on infrastructure you operate.

AstraBox takes the second idea further: you deploy the Agent service as well as
the sandboxes. It runs installed Agent programs as cloud Agents that remain
available through the AstraBox console, API, Deployments, and messaging
platforms.

## The main difference

Claude Code self-hosted environments move **Agent execution** into your
network. AstraBox moves the **complete Agent platform** into infrastructure you
operate.

| | Claude Code self-hosted environments | AstraBox |
| --- | --- | --- |
| What it is | A self-hosted execution option for Claude cloud Sessions | An open-source, self-hosted Agent platform |
| Agent runtime | Claude Code runs on runners in your network | An installed Agent program runs in an OpenSandbox sandbox |
| Console, API, and orchestration | Operated by Anthropic | Operated in your AstraBox deployment |
| Session records | Stored by Anthropic so supported Claude surfaces can continue the Session | Stored in the database you configure |
| Model connection | Uses the Anthropic API | Uses the model service configured by the operator |
| How other systems use the Agent | Through supported Anthropic products and integrations | Through the AstraBox API, Deployments, and messaging platforms |
| Infrastructure you maintain | Runner images, capacity, networking, and updates | AstraBox, OpenSandbox, data services, Agent images, capacity, networking, and updates |

With either option, repository checkouts, build artifacts, secrets, and files
created by the Agent can stay on your infrastructure. With Claude Code
self-hosted environments, the conversation—including prompts, responses, and
tool results—is sent to Anthropic, and Anthropic stores the Session transcript.
With AstraBox, Session records stay in your database; requests still leave your
network when the Agent calls the model service, a remote MCP server, or another
external service you configured.

## Which one fits

Choose Claude Code self-hosted environments when your team wants to use
Anthropic's cloud Session service and interfaces while running Claude Code next
to internal repositories, services, and toolchains. Anthropic operates the
Agent service; your team operates its execution capacity.

Choose AstraBox when the Agent service itself must run in infrastructure you
control: its console, API, authentication, Session records, sandboxes, and
integrations. This also lets one deployment run different supported Agent
programs and connect to the model service selected by its operator.

For installation and the first Agent, see [Deploy AstraBox](deploy.md) and
[Quickstart](quickstart.md).

## Local Agent programs

Local use is suited to interactive development on one computer. An AstraBox
Agent can be reached remotely, run long tasks, and connect to other systems;
the two approaches complement each other.

AstraBox does not replace Claude Code. It runs Claude Code and other installed
Agent programs in managed sandboxes and provides the surrounding service that
keeps their Agents available.

## Sources

Verified against Anthropic's official documentation on 2026-08-24:

- [Claude Managed Agents overview](https://platform.claude.com/docs/en/managed-agents/overview)
- [Self-hosted environments for Claude Code](https://code.claude.com/docs/en/self-hosted-environments)
- [Run Claude Code Sessions on your own compute](https://claude.com/blog/run-claude-code-sessions-on-your-own-compute)
