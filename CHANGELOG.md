# Changelog

All notable changes to AstraBox are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.2] - 2026-10-01

This release fixes conversation recovery, background reply delivery, Pi dialogs,
long-history navigation, and prepared sandbox admission. Re-run the installer
to upgrade; existing settings, secrets, and conversation data are retained.

### Added

- Voice input in Web conversations through the existing model gateway. Transcripts
  are appended to an editable draft without sending automatically; failed
  recordings can be retried or discarded. Administrators can configure one or
  more transcription models. See the [voice input guide](docs/voice-input.md).
- Administrators can assign a separate execution account to a channel. Conversation
  history and retained context stay isolated when switching between accounts.
- Messaging destinations receive new main-Agent replies from native background
  work and extensions without waiting for another inbound message.
- An optional overlayfs snapshot differ for OpenSandbox Kubernetes nodes, with
  native fallback and [installation guidance](docs/providers/opensandbox-snapshot-differ.md).
- Delete unused Environments from the management console. Active Agent and
  Assistant definitions, and runtime resources awaiting cleanup, prevent deletion.

### Changed

- Web, built-in messaging platforms, and installed channel providers consume
  the same Session output, with reply identities and restart-safe replay.
- Reduce remote commands when installing Pi and DeepSeek Harness instructions
  and Hermes configuration files, retaining content verification and atomic replacement.

### Fixed

- Stop Pi's native dialog as well as its active turn, allowing the next message
  to be answered without leaving an extension waiting for a response.
- Keep Pi's current reply and tool results together when another message is
  queued during a tool call.
- Preserve an autonomous reply's complete text and tool results when a new
  Web input arrives, including DeepSeek Harness after backend restart and Pi
  while the previous reply is still running.
- Restore protected credentials and isolated execution sessions after snapshot
  resume, preserving the conversation's sandbox, account and workspace.
- Install Pi and DeepSeek Harness instructions in the conversation’s own
  workspace when cold-starting inside a shared sandbox.

- Clear confirmed-missing sandbox bindings on deleted Agents, avoiding repeated
  cleanup while preserving bindings that changed during the sandbox check.
- Show the prepared sandbox and preparation time on Agent records, and stop
  reporting expired shared slots as ready capacity.
- Withdraw prepared slots when their sandbox is confirmed missing, and fall
  back to a fresh sandbox if prepared compute disappears during handoff.
- Recheck shared sandbox memory before claiming a prepared runtime, accounting
  for concurrent admissions and rejecting slots that expire during the check.
- Preserve the current question and selected answers when older conversation
  events are replayed after reconnecting.
- Show when a Session record was last read and let operators refresh it while
  a turn is running, so newly recorded incidents can be inspected immediately.
- Preserve the reading position when loading older messages with different
  heights in a long conversation.
- Keep the latest messages visible when an idle history refresh appends replies
  and the reader is already at the bottom of the conversation.
- Keep earlier file changes visible in Diff after reloading a long conversation,
  even when those changes are outside the loaded message history.
- Continue scanning older background tasks when newer completed records fill the
  scan window, so their results can still reach the conversation.
- Track Claude background tasks before the parent turn finishes, preserving
  their results if the parent runtime is lost while the child keeps running.
- Administrator-terminated sessions publish their terminal state and reject
  new messages before automatic runtime recovery. Active sessions still
  recover after sandbox reclamation, and explicit manual recovery remains available.

## [0.1.1]

Upgrading is strongly recommended for every 0.1.0 installation: it fixes
sandbox isolation and access-control vulnerabilities, and engines that could
not complete a first turn on the one-command install. Re-run the installer to
upgrade; settings, secrets and data are kept.

Sandboxes that 0.1.0 started keep their old port bindings until they are
replaced. To close that exposure right away, remove them on the AstraBox host
after upgrading:

```bash
docker ps -aq --filter label=opensandbox.io/id | xargs -r docker rm -f
docker ps -aq --filter label=opensandbox.io/egress-sidecar-for | xargs -r docker rm -f
```

Each conversation continues on a new sandbox with its history. Files in a
removed sandbox's workspace are kept only with the optional persistent
workspace volume.

### Security

- **Sandbox services were reachable from the network.** Each sandbox's command
  and file services were published on every host interface without
  authentication. They are now bound to the Docker bridge gateway only.
- **A sandbox could reach the management API.** With Unrestricted networking,
  code in a sandbox could call the platform API, which in no-login mode acts as
  an administrator. The server no longer publishes its API on the Docker
  bridge; the scoped sandbox edges reach it over a private network.
- **A sandbox could reach another sandbox.** Every sandbox now denies the
  Docker bridge subnet, apart from the scoped edges, in every networking mode.
- **Cloud metadata and raw sockets.** `169.254.0.0/16` is denied in every
  networking mode, and sandbox containers no longer have `NET_RAW` (Docker).
- **Agent fields could widen a sandbox's access.** Plugin, Skill and MCP hosts
  written by an Agent author are now checked against the Environment's policy.
  Private, loopback, link-local and Docker bridge hosts are refused unless an
  administrator has explicitly admitted them in that Environment.
  `deploy_key_secret_name` resolves only names listed in
  `ASTRABOX_DEPLOY_KEY_SECRET_NAMES`, an author's MCP definition cannot select
  the platform's gateway credential, and the Git HTTPS token is sent only to
  `ASTRABOX_GIT_HTTPS_TOKEN_HOST`. Refusals are `403` with registered codes.
- **Team login: members could become administrators.** A member could request
  `astrabox:admin` in their own token. API scopes now count only on tokens
  issued to the configured API client; a user's token carries the user's role.
- **Team login: bundled Casdoor defaults.** The built-in Casdoor administrator's
  default password is replaced with a generated secret at first start, and
  AstraBox accepts only accounts of the configured organization
  (`ASTRABOX_CASDOOR_ORGANIZATION`, default `astrabox`).
- **The name-only egress mode is refused.** `ASTRABOX_SANDBOX_EGRESS_MODE=dns`
  enforces no address rule, so the cloud metadata deny, the Docker bridge deny
  and address entries in Limited allow lists would not apply. The server now
  refuses to start with it instead of warning.
- **IPv6 stays off in every sandbox.** AstraBox now sets OpenSandbox's
  `egress.disable_ipv6` itself instead of relying on its default, so sandboxes
  have no IPv6 route even where the Docker daemon enables IPv6.
- **The server never offers sandboxes its own address.** With
  `ASTRABOX_MCP_PROXY_BASE_URL` unset, the server used its own container
  address as the sandbox callback base, and the protected embedded gateway used
  it as the private gateway address; either let a sandbox reach every port the
  server listens on. Neither is derived any more: an unset callback base fails
  the first sandbox, and the embedded gateway refuses to start without a
  gateway address. The maintained Compose stack sets both to the sandbox edge.
- **Kubernetes sandboxes lose `NET_RAW`.** Sandbox containers on Kubernetes
  could open raw sockets, which Docker sandboxes lost in 0.1.1. The bundled
  lifecycle server now creates every sandbox with `NET_RAW` dropped, through
  the OpenSandbox BatchSandbox template. With
  `ASTRABOX_SANDBOX_SERVER_KUBE_WORKLOAD_PROVIDER=agent-sandbox` the server
  logs a warning instead, because that provider reads a template AstraBox does
  not write.
- **Hermes' session token no longer reaches proxy logs.** AstraBox sent the
  token that opens an Assistant's Hermes backend in the connection URL, and the
  OpenSandbox ingress gateway and the sandbox's command service log every
  request URL. It now travels in a request header, and the Hermes image moves
  it to where Hermes reads it inside the sandbox. Rebuild or pull the
  `sandbox-hermes` image together with the server.
- **The Hermes backend credential is no longer computable from a box's
  identity.** The token that opens an Assistant's Hermes backend was a hash of
  the box's account and home directory, with no deployment secret, so anyone
  who could reach the backend port and knew those identifiers could present it.
  On Kubernetes the sandbox Pod has no ingress firewall, so that port is
  reachable from elsewhere in the cluster. The token is now derived from the
  deployment's own secret and cannot be reproduced from the identity alone.
  Rebuild or pull the `sandbox-hermes` image together with the server.
- **Claude Code and Codex sandboxes admit only the platform on their engine
  ports.** A Claude Code sandbox's runner handed its live conversation to any
  connection that named the conversation's id, which the API shows and which
  the runner's own refusal revealed to a peer that named a wrong one. A Codex
  sandbox's app-server port relayed every connection to the server. On
  Kubernetes a sandbox Pod has no ingress firewall of OpenSandbox's making, so
  any pod in the cluster could reach both, and in an Agent-shared sandbox every
  other conversation could. Both now require a credential derived from the
  deployment's own secret and the sandbox's identity, and refuse a connection
  without it before naming or relaying anything. A new Claude Code sandbox's
  runner also refuses to be prepared until AstraBox has written that
  credential into the sandbox, so the first connection to reach it cannot
  claim it. Rebuild or pull the
  `sandbox-claude-code` and `sandbox-codex` images together with the server;
  sandboxes started before the upgrade keep the old behaviour until they are
  replaced.
- **The sandbox's :8080 services were open to any caller that reached the
  port.** The agent-infra base image serves a file and shell API, a terminal,
  JupyterLab, a VNC desktop, code-server and an MCP hub on :8080 and left them
  unauthenticated, so an unauthenticated request ran arbitrary commands in the
  box. On Kubernetes every Pod in the cluster can reach that port, and on Docker
  every container on the default bridge can, including through the address the
  egress sidecar publishes on the bridge gateway — cross-tenant code execution.
  AstraBox now sets the base image's own `SANDBOX_API_KEY` per box, derived from
  the deployment secret and the box's identity, so the port refuses a caller
  without it. Exposed-port links never carry that credential: port 8080 cannot
  be exposed, and previews use the agent's own web server port. The
  services AstraBox does not use — JupyterLab, code-server, the VNC desktop, the
  browser and the Node REPLs — no longer run at all. Rebuild or pull the
  `sandbox-*` images together with the server.

### Added

- **Team login from the installer.** `ASTRABOX_INSTALL_TEAM_LOGIN=casdoor` turns
  on the bundled Casdoor login; set `ASTRABOX_CONSOLE_ORIGIN` and
  `ASTRABOX_OIDC_ISSUER` for a deployment behind a reverse proxy. The release
  bundle now contains the login overlay.
- **Langfuse tracing through OpenTelemetry.** The bundled gateway sends traces
  with LiteLLM's OTel integration when `LANGFUSE_PUBLIC_KEY` and
  `LANGFUSE_SECRET_KEY` are set, and sends nothing otherwise.
- **Single-container deployment.** `ghcr.io/colton-z/astrabox:0.1.1` runs the
  platform with `docker run`; it keeps its data in a Docker volume and creates
  separate sandbox containers from the matching `sandbox-*` images. See the
  [all-in-one guide](docs/all-in-one.md).

### Fixed

- Assistant workspaces on persistent volumes prepare their private directory
  ownership before startup checks. Replacement sandboxes reuse the same numeric
  account so the existing workspace and Hermes profile remain writable.

- A conversation replacing a lost sandbox could fail its next message because
  background cleanup deleted the replacement during startup. Cleanup now
  preserves the replacement while the conversation's accepted turn is active.
- Prepared-runtime status keeps the existing pool identity while an Agent
  configuration change prepares a replacement.
- The first model request of the bundled gateway failed with
  `400 Invalid request format` for Pi, Hermes, DeepSeek Harness and Codex.
- DeepSeek Harness and Hermes could not start on the one-command install; the
  server now connects to sandboxes directly instead of through the lifecycle
  server's relay.
- Sandboxes could not be created on hosts with fewer than four CPUs.
- The server restarted once during a fresh install while the model gateway
  finished its first boot.
- `docker stop` now stops AstraBox before the services it depends on.
- Claude Code ran without its own system prompt when an Agent had no
  instructions (including the seeded Agent) and its sandbox started cold. It
  now always runs on Claude Code's preset, with Agent instructions appended.
- DeepSeek Harness failed every turn on models other than DeepSeek's (it asked
  for a 256,000-token output). Other models now use the harness's generic
  gateway route with its standard limits.
- Conversation titles and process summaries were never generated on the
  one-command install. Every turn logged `session title generation failed`
  and the conversation kept the Agent's name, because the server sent these
  requests to the sandbox-only gateway name `gateway.astrabox.test`. The
  server now uses the gateway's server-side address and its own gateway
  credential, as model discovery does. The `ASTRABOX_TITLE_MODEL_*` settings
  still override both.
- A lost sandbox now produces the registered, non-retryable `SANDBOX_GONE`
  result; input that was never delivered produces the registered,
  non-retryable `INPUT_NOT_DELIVERED` result instead of looking like a failed
  agent turn.
- Codex child command cards recover an empty command result from the same
  call's recorded native output after the child finishes.
- **Conversations stopped answering after a restart.** On the one-host Docker
  stack, the two sandbox edges can come back at different Docker bridge
  addresses after `docker compose stop` and `start`, or after an edge restarts.
  A conversation whose sandbox was created before then could not reach the
  model: each message retried for about six minutes and failed. Sandboxes now
  record the addresses they were created with; the server removes those
  created with other addresses, and the next message takes a new sandbox, as
  after any other sandbox loss. A server that sees an edge move while it runs
  exits so that Docker restarts it. Sandboxes started by 0.1.0 carry no such
  record; after upgrading, remove them with the commands at the start of these
  notes if a conversation stops answering after an edge restart.
- **`docker stop` reported a failure.** A stop that completes now exits with
  status 0 instead of 241. A process killed by a signal reports 128 plus the
  signal number.
- **Hermes Assistants could not start behind the OpenSandbox ingress
  gateway.** The gateway that Secure Access requires on Kubernetes replaces the
  `Host` header, and Hermes refuses a `Host` other than its loopback address,
  so the workspace never became ready. The Hermes image now presents the
  address Hermes expects, and the server connects to the sandbox address it
  was given, which wildcard gateway routes also need. Rebuild or pull the
  `sandbox-hermes` image together with the server.
- **The Kubernetes Compose stack ignored the ingress settings.**
  `ASTRABOX_SANDBOX_SECURE_ACCESS`, `ASTRABOX_SANDBOX_ENDPOINT_SCHEME` and the
  `ASTRABOX_SANDBOX_SERVER_INGRESS_*` settings never reached the server, so
  sandboxes were served without Secure Access even when it was set. They are
  now passed through, and on Docker the server refuses to start with Secure
  Access set instead of ignoring it.

### Changed

- A user's own OAuth token no longer needs `astrabox:*` scopes; it can call
  every route the user can reach in the console. Tokens issued to OAuth clients
  other than the console and the API client are refused.
- Existing Agents that reference a deploy key name not listed in
  `ASTRABOX_DEPLOY_KEY_SECRET_NAMES` are refused when a Session starts.
- `ASTRABOX_SERVER_SANDBOX_PORT`, `ASTRABOX_MODEL_GATEWAY_HOST_PORT` and
  `ASTRABOX_GATEWAY_DNS_HOST_PORT` are no longer used; remove them from
  `containers/.env`.
- Docker deployments now require Docker Engine 26.0 or later and the Compose
  plugin 2.17.0 or later. The installer checks both before starting.
- **New Agents keep a sandbox ready.** An Agent created without a prewarm
  choice, from the API, the console, the CLI or the seed of a fresh
  installation, now has prewarming on wherever the deployment can prewarm
  (`ASTRABOX_AGENT_PREWARM_REDIS_URL` is set, as in every bundled Compose
  deployment), so its first conversation claims a prepared sandbox instead of
  starting one. Each such Agent holds one idle sandbox. Existing Agents keep
  their setting. The cost per deployment and how to turn it off are in
  [Plan capacity for prepared sandboxes](docs/deploy.md#plan-capacity-for-prepared-sandboxes).

### Documentation

- A reverse-proxy guide for team login: public URLs, and keeping Casdoor's
  administration pages private.
- A sandbox network boundary section in the deployment guide.
- The AstraBox CLI and the plugin interfaces are introduced on the site and in
  the README.

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
