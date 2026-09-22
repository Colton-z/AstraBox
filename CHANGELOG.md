# Changelog

All notable changes to AstraBox are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0]

First public release: a self-hosted runtime that turns the Agent programs you
already use into managed cloud Agents, on your own infrastructure and with your
own models.

### Added

- **Five Agent programs behind one platform.** Claude Code, Codex, Hermes,
  DeepSeek Harness and Pi each run through their own engine adapter. The vendor
  program owns its agent loop and its native session; AstraBox owns placement,
  sandbox lifecycle, transport, streaming, credentials and orchestration state.
  Adapters are entry points, so a deployment can install its own.
- **Sandboxes through the OpenSandbox lifecycle API.** One Docker host or a
  Kubernetes cluster runs the sandboxes; a deployment can also point at an
  OpenSandbox service it already operates. Conversations get their own sandbox,
  or share an Agent-owned one through isolated Linux accounts.
- **Prepared capacity.** An Agent keeps warm sandboxes ready, so a conversation
  starts in seconds instead of waiting for a cold container, and resumes the
  same way after its box is gone. Idle boxes pause or terminate by the
  Environment's own rule.
- **Recovery that keeps the conversation.** Every engine's native session store
  is kept in the platform database and restored for the supplier's own resume,
  so a lost sandbox, a restarted server or a replaced box does not cost the
  conversation its history. Persistent workspace volumes stay optional.
- **Web console and HTTP API over the same resources.** Agents, Environments,
  Sessions, Events, Assistants, Deployments, Vaults and channels are one set of
  records; the console and the API operate them the same way. Events stream over
  SSE with tool approval, interruption, reconnect and durable history. The
  console ships in English and Simplified Chinese.
- **Any model through the bundled gateway.** Model requests go through LiteLLM,
  so Anthropic, OpenAI-compatible and local providers all work; an Environment
  names the route, never a vendor key.
- **Credentials the sandbox never holds.** Vault credentials are injected
  outside the sandbox at its egress boundary — model keys, MCP server
  credentials and HTTP Basic repository access. The Agent sees a placeholder;
  a backend that cannot inject reports it instead of degrading.
- **Tools: MCP servers, Plugins and Skills.** Anonymous direct MCP servers,
  managed MCP servers with administrator-held secrets through the gateway, and
  each program's native Plugin and Skill formats.
- **Deployments.** Schedules, webhooks and manual runs start Agents without a
  person present, and messaging channels connect them to chat platforms through
  official Satori adapters.
- **Assistants.** A long-lived Agent with one durable shared workspace across
  its conversations.
- **Identity.** Single-user with no auth out of the box; `trusted_header`,
  verified `jwt`, and OIDC (a bundled Casdoor deployment) add team login,
  ownership and an administrator surface by configuration.
- **One-command install.** `curl -fsSL .../install.sh | bash` pulls published
  images and starts the Compose deployment: API and console, PostgreSQL, Redis,
  the model gateway, the messaging gateway and the local sandbox service.
  Building from source stays documented.
- **Observability.** Prometheus metrics, and OpenTelemetry tracing to a
  collector of your choice (Langfuse included) when you switch it on.
- **Extension seams.** Sandbox backend, storage, model access, secret store,
  repository, identity resolver and engine adapter are all entry points with
  versioned contracts and conformance suites, so a deployment can replace one
  without forking the platform.
