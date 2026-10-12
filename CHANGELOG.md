# Changelog

All notable changes to AstraBox are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.7] - 2026-10-11

**Upgrading:**

- an Agent's tags (`display_meta.tags`) are at most five, each at most 32
  characters, and are stored lower case with words joined by `-`. An Agent
  that carries more is refused when it is next saved until its tags are cut.
- a sandbox provider package must give its handle the declared data-plane
  faces: `files` (`SandboxFiles`), `commands` (`SandboxCommands`), `processes`
  (`SandboxProcesses`, or `UnsupportedSandboxProcesses` from a provider that
  does not declare `supports_process_sessions`), `get_endpoint` returning a
  `SandboxEndpoint`, and `get_signed_endpoint`. Core no longer reads the
  OpenSandbox SDK's objects off a handle. A provider whose boxes run the
  image's execd builds the first three from `astrabox.providers.execd`; see
  [Add a sandbox provider](docs/writing-a-sandbox-provider.md). The seam
  contract version is 6, one past the extension provider change.
- Agents stored without a model are given the deployment's current model when
  the server upgrades, and no longer follow later changes to the deployment's
  model. Resolving an Agent that still has no model returns
  `409 AGENT_MODEL_REQUIRED`.

### Added
- Skills accept structured sources and deployment-installed content providers
  through `astrabox.providers.skills`; Git uses the same acquisition and cache
  path. SSH Git Skills accept approved named deploy keys with verified host
  keys and invocation-scoped credential cleanup. See
  [Skill content providers](docs/providers/skill-content.md).

- The home page lists Agents under tabs: **Favorites**, **All**, then the
  Agent categories a platform administrator defines and orders on the console's
  **Agent categories** page (`/api/v1/admin/agent-categories`). Whoever may
  manage an Agent lists it under any number of categories (`categories`, ids
  from `GET /api/v1/agent-categories`) and gives it up to five tags
  (`display_meta.tags`), stored lower case with words joined by `-`. A search
  box matches names, tags, categories and descriptions within the current tab,
  and clicking a tag on a card filters by it. Each user favorites Agents with
  the star on their cards (`PUT`/`DELETE /api/v1/agents/{agent_id}/favorite`);
  the home page opens on the last tab the user chose, or on Favorites when the
  user has any. `GET /api/v1/agent-tags` lists the tags of the Agents the
  caller may see. Deleting a category takes it off its Agents.

- `astrabox.testing.sandbox_data_conformance` checks, on a live box, that a
  sandbox handle's `files`, `commands` and `processes` keep the contract core
  relies on: exact bytes whole and streamed, the mode and ownership a write or
  directory creation gives, `lstat` kinds, one-level listing, name search,
  moves that refuse an existing destination, removal that never follows a
  symlink, and literal paths; for commands a shell command line run as root,
  the exit status as data, stdout and stderr apart, the environment passed
  literally, an empty stdin, a signal as a negative status, and a deadline
  that raises `TimeoutError` and stops the command in the box; and, for a
  provider that declares `supports_process_sessions` (`PROCESSES_CONTRACT`),
  stdin in and the exit as the last frame, a reattach that replays from an
  offset, one holder unless another takes over, a replay that never starts
  past its cursor, a signal that reaches only a running process, `close` that
  stops a process and forgets it, and an exited process that runs again when
  attached. A provider binds it in its own live tests; the built-in
  OpenSandbox provider runs it on each engine image in the E2E suite.

### Changed
- A sandbox backend that cannot look a sandbox up by its create assignment
  (no `supports_correlated_create`) creates sandboxes; every create path used
  to refuse it with `501 SANDBOX_CORRELATED_CREATE_UNSUPPORTED` at the first
  conversation. AstraBox creates each sandbox once on it: a create that raises
  fails the start with that error, and a start a platform restart interrupted
  is refused with `409 SANDBOX_CREATE_INTERRUPTED` rather than creating a
  second sandbox; a sandbox the backend made for that start is left to the
  backend's own expiry. See
  [Add a sandbox provider](docs/writing-a-sandbox-provider.md).

- `SandboxProvider.read_memory_headroom` and `read_listening_ports` have
  default implementations that read the box's cgroup v2 memory files and
  `/proc/net/tcp` through its `commands`, so a provider that declares
  `supports_isolated_sessions` no longer has to write them.
- A sandbox handle carries the box's files as a declared contract,
  `SandboxHandle.files` (`SandboxFiles` in `astrabox.seams.sandbox_data`):
  absolute paths taken literally, POSIX permission bits such as `0o640`,
  account names, and three typed failures, `SandboxPathNotFound`,
  `SandboxPathRejected` and `SandboxUnreachable`, which is now also a
  `ConnectionError`. The built-in provider implements it over the execd every
  bundled image carries, with `astrabox.providers.execd.ExecdFiles`, which
  another provider whose boxes run execd can reuse. A third-party sandbox
  provider's handle must provide `files`; the handle methods `get_filesystem`
  and `search_file_paths` (`SandboxEndpointFilesystem`) are removed.
- A sandbox handle runs commands through a declared contract,
  `SandboxHandle.commands` (`SandboxCommands`): one shell command line run as
  root, with an optional environment and a deadline kept on the caller's side,
  answered with a `SandboxCommandResult` of exit status, stdout and stderr. A
  non-zero exit is data; the box refusing the command raises
  `SandboxCommandRefused`, a run it gives no exit status for raises
  `SandboxCommandOutcomeUnknown`, and an unreachable box `SandboxUnreachable`.
  Core no longer passes the OpenSandbox SDK's `RunCommandOpts` or
  `ExecutionHandlers` or reads its `Execution`. The built-in provider implements
  the contract with `astrabox.providers.execd.ExecdCommands`; a third-party
  sandbox provider's handle must provide `commands`. A command whose report
  carried no exit status is no longer taken for a success.
- A sandbox handle's `get_endpoint(port)` returns a `SandboxEndpoint`
  (`astrabox.seams.sandbox_data`): an absolute `http(s)` URL and the headers
  every request to it carries. `get_signed_endpoint(port, expires_at=...)` is
  part of the handle contract and returns one whose URL carries a credential
  until `expires_at`; a backend that cannot sign raises the 501
  `SANDBOX_SIGNED_ENDPOINT_UNSUPPORTED`. `SandboxEndpointRef` is removed. A
  provider says which sandbox one of its endpoint strings names with
  `SandboxProvider.endpoint_names_sandbox`; a persisted endpoint whose backend
  cannot tell is resolved again from the sandbox id without the "discard
  unverifiable sandbox endpoint" warning.
- A sandbox handle runs long-lived processes through a declared contract,
  `SandboxHandle.processes` (`SandboxProcesses`): a process started from one
  shell command line is named by an id that any replica can attach to; one
  attachment holds it at a time and another may take it over; its output,
  stdout and stderr together, is numbered in bytes, and an attachment replays
  it from an offset the box still holds or fails with
  `SandboxProcessReplayGap`; `signal` and `close` act by id. The terminal of a
  conversation with its own box and the pi engine use it instead of execd's
  PTY API. A provider declares `SandboxProvider.supports_process_sessions`;
  the built-in provider implements the contract with
  `astrabox.providers.execd.ExecdProcesses`, and a provider without it returns
  `UnsupportedSandboxProcesses`, whose every operation raises the 501
  `SANDBOX_PROCESS_SESSIONS_UNSUPPORTED`.
- An Environment that pairs the pi engine with conversation tenancy, and an
  Agent that turns on the terminal panel in such an Environment, are refused
  with `400` on a sandbox backend that does not declare
  `supports_process_sessions`, instead of failing at the first conversation.
  An engine says it runs as a box process with
  `EngineRuntimeCapabilities.requires_process_sessions`.
- The file panel answers `503 SANDBOX_UNREACHABLE` when the box cannot be
  reached, where it answered 502; a refusal by the box stays a 502, with the
  box's reason in the server log. A path holding `$` is refused, where the
  panel read or wrote another path. Deleting a symlink removes the link
  itself, including a link to a directory, which failed, and a link to
  nothing, which reported success and stayed.

### Removed

- The runtime NAS mount of the shared Assistant root,
  `EnginePlatform.mount_assistant_workspace_storage` and its implementation,
  which nothing called, and the `assistant_workspace_root_preprovisioned`
  sandbox provider declaration that only it read.

### Fixed
- Channel sender allowlists and account links show the selected provider's
  identity format. Generic JSON accepts the original `sender` value; Satori
  uses `platform:user-id`.

- Administrators see the name a signed-in user's identity provider gives for
  the conversations that user starts, where they saw the user id. A failure to
  store the name fails the start instead of being ignored.
- An `astrabox.yaml` exported with `astrabox export` applies again, where the
  server refused it. `apply` sets an Agent's `display_name`, `icon` and `tags`
  inside `display_meta`, keeping the stored members the file does not name.
- An Agent edit that loses a race with a concurrent edit is refused with
  `409 AGENT_VERSION_CONFLICT` whatever it changes; one that changed only a
  name, description, tag or category was dropped and reported as saved.
- The Agents a new installation seeds could not be saved from the console
  (`model is required`). They are created with the deployment's model, and
  existing ones are given it when the server upgrades.
- A failed Agent, Environment or Assistant save shows its error beside the
  Save that was pressed. Agent forms also mark the field the error names
  until the next edit or save.
- A turn on a sandbox provider that declares `supports_turn_preparation` runs
  the provider's `prepare_turn` and reaches the engine, where every such turn
  was refused with `TURN_PREPARATION_FAILED`.
- Such a provider's `prepare_turn`, and the refresh of a conversation's MCP
  credentials, finish before each message is handed to the engine, including a
  message sent while a turn is running. A message whose sandbox cannot be
  prepared or whose credential cannot be refreshed is refused when it is sent,
  with `TURN_PREPARATION_FAILED`, `409 TURN_PREPARATION_USER_ACTION_REQUIRED`
  (the provider's user action in `error.evidence.user_action`) or the
  credential's error such as `VAULT_CREDENTIAL_EXPIRED`, and never reaches the
  engine.
- A user action a provider's `prepare_turn` asks for while a turn begins ends
  the turn with `TURN_PREPARATION_USER_ACTION_REQUIRED` and the provider's
  message, where it was reported as `ENGINE_INPUT_DELIVERY_FAILED`.
- The console's session record and **Errors** page name a session's running
  turn (`current_turn_id` of `GET /api/v1/admin/sessions/{session_id}/trace`,
  `turn_id` of `GET /api/v1/admin/errors`), where both showed none.
- Conversation and share-link file downloads stream a file of any size from
  the sandbox, where a file above 64 MiB was refused with
  `502 AGENT_RUNTIME_ERROR`. A failure after the download has started closes
  the connection short of its `Content-Length`. An upload now waits for the
  sandbox's answer for a time that grows with the file, where a large upload
  could fail with `503 SANDBOX_UNREACHABLE` while the sandbox was still
  writing it into place.

## [0.1.6] - 2026-10-10

**Upgrading:**

- a conversation's own box is limited to 1 CPU and 1Gi (requests 250m and 1Gi),
  and an Agent's shared box to 4 CPU and 4Gi (requests 1 CPU and 4Gi); every
  box was limited to 4 CPU and 4Gi with 200m and 768Mi requests.
  `ASTRABOX_SANDBOX_*` and `ASTRABOX_SHARED_SANDBOX_*` set them. Running boxes
  keep their size until they are replaced.
- the bundled sandbox server on a Docker host refuses a box that would take the
  memory limits of the boxes it runs, and their egress sidecars', past the
  host's memory less 4 GiB. `ASTRABOX_SANDBOX_QUOTA_MEMORY` sets another
  ceiling, and `unlimited` removes it.
- each box's egress sidecar is limited to 1 CPU and 128Mi on a Docker host. On
  Kubernetes, give the sandbox namespace a LimitRange with that container
  default; OpenSandbox 0.2.3 creates the sidecar without resources. See
  [Size sandboxes and cap what they use](docs/deploy.md#size-sandboxes-and-cap-what-they-use).
- an extension provider package must declare
  `features = ExtensionProviderFeatures(display_name=..., management_console=...)`
  on its `ExtensionProvider`, and implement `open_management_console` if it
  declares a console; registration refuses one that does not. The seam contract
  version (`SEAMS_API_VERSION`) is now 5.
- a deployment on the MongoDB repository backend must set
  `ASTRABOX_SCHEDULER_PROVIDER=none` (in `containers/.env` for a Compose
  installation), or install a scheduler provider that supports MongoDB, before
  upgrading. The built-in `dbos` scheduler keeps its
  schedules and Run history in the repository's SQL database, and startup now
  refuses it on a backend without one instead of starting with Schedule creation
  refused. With `none`, startup also refuses while an enabled scheduled Deployment
  is stored, because it would never run: disable or delete such Deployments
  before switching to `none`.

### Added

- A messaging provider can report a message and the permission to answer it as
  two signals with the same `dedup_key`, in either order:
  `ChannelInbound.signal="observation"` records the message as context only,
  and `"authorization"` runs it. AstraBox keeps one record per message, never
  runs an observation, runs an authorized message at most once with the text
  first recorded for it, refuses a signal that names another sender or
  conversation, never runs a message recalled before its authorization, and
  tells the participant when an authorization's message has not arrived after
  a day. See "Separate context and trigger feeds" in the provider guide.
- A messaging provider can group messages into conversations by reply chain.
  With `ChannelInbound.conversation_from_reference=True`, a reply continues the
  conversation of the message its `reference` names when AstraBox recorded
  that message, as an inbound message or as a reply it delivered; otherwise
  the reply's own `conversation_key` applies. Retained context follows the
  same conversations. See "Group messages into conversations" in the provider
  guide.
- A messaging trigger on an Agent can run each sender's messages as that
  sender's own AstraBox account (`execution_mode: "linked"`). Administrators
  link a sender (`platform:user-id`) to a user ID under **Linked accounts** on
  the trigger's page, or through
  `/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/account-links`.
  The account is resolved once per message and kept with it. A sender
  without a link, or whose account may not use the Agent, is refused with a
  notice and never answered as anyone else. `ASTRABOX_CHANNEL_ACCOUNT_RESOLVER`
  selects another resolver, such as one backed by a directory and registered
  at the `astrabox.providers.channel_accounts` entry point. A provider lists
  the modes its triggers may use in `ChannelDescriptor.execution_modes`; one
  whose work belongs to a person lists only `linked`.
- A deployment can run without an extension catalog:
  `ASTRABOX_EXTENSION_PROVIDER=none` (in `containers/.env` for a Compose
  installation, which now forwards the variable). Agent, Assistant and
  Assistant-defaults pages then show only the record's own MCP servers, Skills
  and packages and send no catalog request, and the catalog routes answer
  `409 EXTENSION_CATALOG_DISABLED`. A catalog selection a record already holds
  is not dropped: startup names each enabled Agent, Assistant and the Assistant
  defaults that hold one, new work for such a record fails with
  `EXTENSION_CATALOG_DISABLED` while its existing conversations stay readable,
  and its page lists the selection with **Remove selections**. Startup refuses
  an `ASTRABOX_EXTENSION_PROVIDER` that no installed provider registers. See
  [The extension catalog](docs/adding-tools.md#extension-catalog).
- `GET /api/v1/extension-provider` returns the selected extension provider's
  name, display name and whether it has a management console, or `null` when
  the deployment runs no catalog.
- AstraBox can be published below a path of a host it shares with other
  applications, such as `https://example.com/astrabox/`. Set
  `ASTRABOX_PUBLIC_PATH_PREFIX` to the path. A reverse proxy removes the path
  before forwarding, or a Python application mounts AstraBox at it. The
  prebuilt console, its API calls and event streams, the sign-in callback,
  cookie paths, share and file links, the MCP server URL and channel callback
  URLs all follow the path. A proxy that forwards the path instead of removing
  it is refused with an error that names the setting. LiteLLM's management
  pages cannot be served below a path. See
  [Publish AstraBox under a path](docs/deploy.md#publish-under-a-path).
- A deployment can enable several installed sandbox backends with
  `ASTRABOX_SANDBOX_BACKENDS` (comma-separated). The list must include
  `ASTRABOX_SANDBOX_BACKEND`, which stays the default; left empty, it enables
  only the default. Environments offer only enabled backends, and a record that
  names an installed backend the deployment does not enable fails with
  `409 SANDBOX_BACKEND_DISABLED` instead of being served by another backend;
  startup logs each enabled Environment that selects such a backend.
  A sandbox provider lists the variables only it reads in `owned_settings`, and
  startup warns when one is set while that backend is not enabled.
- A deployment sets the size of a conversation's own box with
  `ASTRABOX_SANDBOX_CPU_LIMIT`, `ASTRABOX_SANDBOX_MEMORY_LIMIT`,
  `ASTRABOX_SANDBOX_CPU_REQUEST` and `ASTRABOX_SANDBOX_MEMORY_REQUEST`, and of an
  Agent's shared box with `ASTRABOX_SHARED_SANDBOX_CPU_LIMIT`,
  `ASTRABOX_SHARED_SANDBOX_MEMORY_LIMIT`, `ASTRABOX_SHARED_SANDBOX_CPU_REQUEST`
  and `ASTRABOX_SHARED_SANDBOX_MEMORY_REQUEST` (Kubernetes quantities; in
  `containers/.env` for a Compose installation, which now forwards them).
  Prepared and replacement boxes take their kind's size. Startup refuses a value
  that is not a quantity and a request above its limit. A sandbox provider that
  cannot create a box of that shape raises `SandboxResourcesUnsupported`
  (`409 SANDBOX_RESOURCES_UNSUPPORTED`, naming the backend and the envelope)
  from `fit_resources` before anything is allocated.
- A deployment caps what each sandbox backend runs with
  `ASTRABOX_SANDBOX_QUOTA_BOXES`, `ASTRABOX_SANDBOX_QUOTA_MEMORY`,
  `ASTRABOX_SANDBOX_QUOTA_CPU` and `ASTRABOX_SANDBOX_QUOTA_CONCURRENT_CREATES`
  (in `containers/.env` for a Compose installation, which forwards them). Every
  box the backend runs for the deployment counts, across all replicas, at its
  limits plus its egress sidecar's: conversations' boxes, shared boxes,
  prepared boxes, boxes being created and boxes whose removal is unconfirmed. A
  box past a ceiling is not created; the conversation that needed it ends at
  once with an error naming the ceiling, the use and the limit.
  `GET /api/v1/admin/sandbox-quota` and the console's **Sandboxes** page show
  each ceiling, its use, and how many boxes are in use, prepared, starting and
  being cleaned up. A sandbox provider reports what runs beside each
  box through `per_box_overhead`, and whether a paused box releases its
  resources through `paused_box_releases_resources`. See
  [Cap what a sandbox backend runs](docs/deploy.md#cap-what-a-sandbox-backend-runs).
- Scheduled Deployments run through a scheduler provider selected with
  `ASTRABOX_SCHEDULER_PROVIDER`: `dbos` (the default, DBOS Transact embedded in
  the server process, as before) or `none`, which turns scheduled Deployments
  off — creating or enabling one then answers `409 SCHEDULE_UNAVAILABLE`. A
  package can add another engine at the `astrabox.providers.scheduler` entry
  point and check it with `astrabox.testing.scheduler_conformance`; see
  [Add a scheduler](docs/writing-a-scheduler-provider.md).

### Changed

- A channel message the Deployment's attention policy does not address is
  recorded as `OBSERVED` retained context and acknowledged with status
  `observed` (it was `ignored`), so a later signal for the same message that
  addresses the agent can run it. Refused senders and provider-classified
  events stay `IGNORED`.
- `execution_mode` (`owner`, `account` or `linked`) states whose account a
  messaging trigger's messages run as. `execution_user_id` applies only with
  `execution_mode: "account"`: send both to delegate, and
  `execution_mode: "owner"` to return to the Agent's creator; an
  `execution_user_id` without that mode is refused. Existing triggers keep
  running as before: one with an execution account runs as `account`, any
  other as `owner`. A replacement deployment service receives
  `execution_mode` on `create` when a request names it.
- A reply, quote or button press that would continue another account's
  conversation is refused with a notice to the sender. Before, a quote of the
  agent's reply there was refused silently, and a reply to a message that ran
  there, or a press of its button, was not refused at all.
- Retained context no longer depends on the account a message runs as: what
  was said in a room is context for whoever the agent answers there.
- `ChannelEvent.command_id` is removed and `CHANNEL_EVENT_VERSION` is 2. A
  streaming provider identifies the reply by `response_id`, with `turn_id`
  naming the Session turn that produced it.
- WeChat Official Account replies are refused with an explicit error before
  anything is sent. These replies already failed inside the adapter; inbound
  messages still reach the agent.
- The console names the extension catalog after its provider ("MCP servers
  from LiteLLM") instead of calling every catalog LiteLLM-managed, and an
  Agent's **Manage … in** buttons appear only when the provider declares a
  management console; the built-in registry declares none. The console
  session route (`POST /api/v1/agents/{agent_id}/extension-console/session`)
  keeps its path and body and opens the selected provider's console, answering
  `409 EXTENSION_CONSOLE_UNSUPPORTED` for a provider without one.
- An Agent read returns its catalog Skill selection (`extension_catalog`), as
  an Assistant read already did, and the Assistant defaults return the layer's
  catalog selection.
- An Assistant read's `inherited` summary lists the Skills the Assistant
  defaults select from the extension catalog as `catalog_skills`, apart from
  the Skills they write themselves (`skills`, which no longer includes them).
  The Assistant's profile and first-conversation welcome list catalog Skills,
  and the profile its catalog tool servers, only while the deployment runs a
  catalog; without one the profile says those selections are unavailable.
- A sandbox provider answers a box's memory and listening ports through
  declared `SandboxProvider` methods: `read_memory_headroom` returns a
  `SandboxMemoryHeadroom`, or `None` for a box without a memory limit, and
  `read_listening_ports` a `frozenset`. Both raise `SandboxUnreachable` (503)
  for a box that cannot be reached and `SandboxProbeFailed` (502) for an
  answer that cannot be read, instead of answering as if the box had no
  headroom or no listeners. A shared placement now fails, naming the probe,
  when either probe cannot be read, where it used to stop packing boxes or
  skip its port-collision check without a trace.
- `register_sandbox` checks every capability flag against one rule table,
  `SANDBOX_CAPABILITY_RULES`. A provider that declares a flag must implement
  every method the flag obliges (`supports_pause` brings `pause` and `resume`,
  `supports_client_pool` the pool methods and the new `client_pool_readiness`)
  and declare the flags it requires, or it is refused when it is installed.
  Saving an Environment or an Agent refuses what the selected backend has not
  declared: shared tenancy without isolated sessions, an Environment on a
  backend that cannot create boxes under a network policy, and prepared
  capacity where the backend's client pool cannot run here
  (`409 AGENT_PREWARM_UNSUPPORTED`). A new Agent's default prepared capacity is
  its backend's answer about that pool, where it was the presence of
  `ASTRABOX_AGENT_PREWARM_REDIS_URL`.
- Whether a box whose control plane still answers is really gone is the
  declared `assess_data_plane`, answering `ALIVE`, `DEAD` or `UNKNOWN`. Only
  `DEAD` replaces the box; a backend that cannot look answers `UNKNOWN`.
- The platform's box resource recipe is fitted by the backend that creates the
  box, through the declared `fit_resources`, and the create spec carries the
  fitted values. The OpenSandbox backend lowers the CPU limit to the host's
  CPU count only when its bundled server runs boxes on this host's Docker; an
  existing OpenSandbox service on Docker now receives the recipe as is, where
  it was clamped to the CPU count of the host running AstraBox.
- Core no longer asks a provider to discard a box's snapshots before
  destroying it. A provider whose pause leaves snapshots reclaims them in its
  own `kill`, as the OpenSandbox provider now does, so every destroy through
  the seam reclaims them, not only the one path that used to ask.
- The census of a shared box's active isolated sessions, consulted before the
  box is destroyed, is the declared `count_active_isolated_sessions`. It
  raises when the box cannot answer, where `count_live_isolated_sessions`
  returned -1; an unanswered census still leaves the decision to the
  ownership records.

### Fixed

- **Sign out** in the console's settings menu signs the user out; it did
  nothing.
- Interrupting a command in a conversation box's terminal stops it; the
  command ran on. The interrupt is now the terminal session's own `SIGINT`.
- A command in a conversation box's terminal shows its output while it runs
  and reports its exit as soon as it ends; short output appeared only when the
  command finished, and the exit about 10 s later.
- A PostgreSQL or SQLite outage answers `503 PERSISTENCE_UNAVAILABLE`
  (retryable) like a MongoDB one, instead of `500 UNEXPECTED_SERVER_ERROR`; the
  error's `owner` is `store` for every repository backend (it read `mongo`),
  and the console recognises the outage by its code.
- `ASTRABOX_DB_BACKEND` naming an installed repository plugin is used even when
  `ASTRABOX_DB_URL` has a `mongodb://` or `postgresql://` scheme; the URL used to
  select the builtin backend of that scheme and the plugin never loaded.
- Emptying a saved JSON block in the Agent, Assistant or Environment editor
  (custom MCP servers, packages, engine options) and saving clears it; the
  server kept the old value while the page showed none.
- A messaging Deployment whose bot is in several chats no longer drops a message
  whose platform number repeats one from another chat, and a quote reply no
  longer resolves into the wrong chat. Message identities now include the chat;
  replies to messages recorded before upgrading no longer find their
  conversation.
- A channel redelivery that takes over an expired or dead message runs the
  input and execution account the message was admitted with; it replaced them
  with the redelivered body and the Deployment's current execution account.
- Crash recovery no longer runs a channel message the attention policy did not
  address.
- Crash recovery of a channel message that reached its conversation only
  through the reply it quotes waits for that conversation's turn lock; it ran
  the turn without the lock.
- Retained group context quotes what each participant wrote; with a
  Deployment prompt prefix, every quoted message repeated the prefix.
- Slack: the agent's reply to a message in a thread is posted in that thread.
- Telegram forum topics: topics of different groups that share a topic number
  are separate conversations, and a reply reaches its topic's group instead of
  a chat named by the topic number. A forum topic's conversation starts a new
  Session after upgrading.
- Zulip: a stream topic is identified by its stream as well as its name, so
  same-named topics in two streams are separate conversations, and replies
  reach their stream or direct recipient; they were sent with neither. A
  stream topic's conversation starts a new Session after upgrading.
- Every concrete messaging platform now receives the addressing of the message
  a reply answers, which the official DingTalk and WeCom adapters read to send
  a direct reply one-to-one and to name the recipient.
- The background sweeps that reclaim sandboxes no record owns and retire
  sandboxes created with stale network wiring read only the `open_sandbox`
  backend. They now run for every enabled backend that can list its sandboxes,
  each on its own, so a deployment whose sandboxes come from another provider
  has them swept too, and one backend's failing control plane no longer stops
  another's sweep; workspace-view reconciliation reads each enabled backend's
  route the same way. A sandbox provider joins the sweeps by declaring
  `supports_inventory_listing = True` (registration checks that it implements
  `list_sandboxes` and `describe_sandbox`), and startup warns about each enabled
  backend that does not.
- A Claude background Agent that recovery closed after a server restart stays
  closed when the engine's retained progress reports reach the journal later;
  it reopened, with a stop operation and its earlier usage, until the replayed
  completion arrived.
- Reading a Claude conversation's child runs or a child's transcript no longer
  attaches the engine. When a server that held no runtime for the conversation
  (after a restart, or on another replica) served such a read, a finished
  background Agent's completion record was held until the parent's own
  continuation ended. Such a read now serves what the journal holds; a
  viewer's or channel's subscription attaches the engine as before.

### Security

- A Skill source whose HTTP(S) URL embeds a username or token
  (`https://user:token@host/repo`) is refused when the Agent or Assistant is
  saved and when its runtime is prepared. The URL was stored with the Agent,
  written to the Skill cache manifest and repeated in download errors. Keep the
  URL clean and assign an HTTP Basic (Git HTTPS) Vault credential for the
  repository, as private Plugin sources already require.
- An admission controller or secret store published as a class at its
  `astrabox.providers.admission` / `astrabox.providers.secrets` entry point is
  installed at startup. It was skipped, so a deployment's quota controller
  silently left every request admitted.

## [0.1.5] - 2026-10-09

This release gives every user an Assistant without setting one up, connects
Assistants and Agents to work apps through a connector hub, runs the scheduled
jobs an Assistant creates and delivers their results to its conversations and
messaging channels, and lets every engine load its own native extensions.
Re-run the installer to upgrade; existing secrets and conversation data are
retained.

**Upgrading:** an Agent's `plugin_repos` and `plugin_paths` are now `packages`
(each entry with `"kind": "claude-plugin"`) and `paths`, and an Assistant's
`mcp_config_override`, `skill_manifest_override` and `plugin_repos_override`
are now `mcp_servers`, `skills` and `packages`. Values stored under the old
names are not carried over: after upgrading, add an Agent's Claude Code plugins
again under **Packages**, and an Assistant's MCP servers, Skills and packages
on its record page. Connectors run only with the connector hub, which the
installer does not start: from a clone, add `containers/compose.connectors.yaml`
(and `containers/compose.connectors-google.yaml` for Google apps) as
[Run the connector hub](docs/deploy.md#run-the-connector-hub) describes.

### Added

- Every user has an Assistant without setting one up. The first visit creates
  a primary Assistant from the deployment's Assistant defaults
  (`POST /api/v1/assistants/primary`), and the rail lists the user's
  Assistants above Sessions. Opening one lands in its main conversation
  (`POST /api/v1/assistants/{id}/main-conversation`), where its scheduled jobs
  report back and whose unread mark the row carries; an empty one lists what
  the Assistant can use, and its header opens a profile for name, timezone,
  instructions, connected apps and the jobs it scheduled for itself
  (`GET /api/v1/assistants/{id}/scheduled-jobs`, read from the program's own
  store in the running workspace). An Assistant's earlier conversations open
  under its row, and Sessions lists Agent conversations only. The Home page's
  Assistant tab is removed.
- Assistant defaults: administrators set, in the console or at
  `/api/v1/admin/assistant-defaults` and its `/extensions`, the Environment new
  Assistants use, the model, organization instructions, and the Skills, MCP
  servers (own or from the extension catalog), packages, Agent-program
  configuration and Credential Vaults every Assistant inherits. An Environment
  the defaults name cannot be deleted. A deployment with one Environment that
  runs Assistants needs no defaults at all.
- Assistant triggers: messaging channels, schedules, and webhooks can target an
  Assistant, managed by its owner through `/api/v1/assistants/{id}/deployments`
  and the trigger form. Channel triggers gain an `allowed_participants` list,
  required on an Assistant.
- Connectors: people connect Gmail, Google Calendar and Drive, Notion, Linear,
  Jira and Confluence, GitHub, and Slack, and turn them on per Assistant or,
  for an Agent's managers, per Agent. A ContextForge connector hub
  (`containers/contextforge`, the v1.0.11 release with two patches that add
  Google offline access and per-user disconnect) holds and refreshes each
  person's tokens and forwards calls to the vendor. Connectors are platform
  MCP servers: the platform relays each call to the hub as the person the
  conversation acts for (an Assistant's owner, an Agent conversation's user),
  so no hub credential enters the sandbox. When an app is not connected, the
  agent is told so and can ask the person to connect it with a card in the
  conversation. An app a person connects is turned on in each of their
  Assistants, and a new Assistant starts with its owner's connected apps;
  turning one off on an Assistant keeps it off there. Google's apps run on the deployment's own
  google_workspace_mcp instances, because Google's MCP servers are a
  developer preview limited to your own organization. Administrators set each
  app up under **Connectors**; see `ASTRABOX_CONTEXTFORGE_*` and the
  `ASTRABOX_*_MCP_URL` variables.
- Unread replies: a conversation records when it last gained a reply
  (`last_output_at`) and when its owner read it (`read_at`,
  `POST /api/v1/sessions/{id}/read`). The console shows unread conversations
  in its list and, once the owner turns notifications on, shows a browser
  notification for each new reply, including one an Assistant sends on its
  own from a scheduled job or heartbeat, which also moves the conversation to
  the top of the list.
- Codex Agents take Codex plugins (`packages` of kind `codex-plugin`),
  including Claude Code, Cursor and Agent Plugins v1.0 plugin directories,
  installed by Codex from a local marketplace before each conversation starts.
- Assistants select MCP servers and Skills from the administrator-managed
  catalog, as Agents do, on their record page or through
  `/api/v1/assistants/{assistant_id}/extensions`.
- MCP servers an Agent's author declares now work on Codex, Pi and DeepSeek
  Harness too (stdio and streamable HTTP), each rendered into the program's
  own MCP configuration.
- DeepSeek Harness Agents take plugin Bundles as their web profile's
  `package.json` dependencies (`engine_options.profile_package`), installed
  with `dsh plugin` before each conversation starts.
- Agents on Codex, Pi and DeepSeek Harness take Skills, placed in
  `~/.agents/skills`, the Skills root all three scan. Where each engine
  discovers Skills is now its adapter's declaration rather than the Claude
  Code configuration directory.
- Every Assistant has AstraBox's `astrabox-proactive` Skill, shipped in the
  Hermes image, which teaches it to offer scheduled briefings and quiet
  monitors when it notices recurring work, and to set them up with the
  user's connected apps once they agree.
- An Assistant has a timezone, its owner's, which the console fills in from
  the browser. Hermes names it in every system prompt and reads the schedules
  of the jobs it creates in it, so a job set for 8 in the morning runs at the
  owner's 8.
- Assistants take Skills, Hermes plugins (`hermes-plugin`), Agent Plugins
  packages (`agent-plugin`) and Hermes `config.yaml` keys
  (`engine_options.config`, applied through Hermes' managed scope), edited on
  the Assistant's record page. Each engine declares what it accepts, and a save
  that names something the Environment's engine does not load is refused.
- Hermes scheduled jobs created by an Assistant now run, through Hermes'
  gateway scheduler in the image, and each run is reported back into the
  conversation that created the job, including its messaging chat.
- Output Hermes produces without a new message (`/loop` and `/heartbeat`
  ticks, finished background work) is published into the conversation and
  delivered to attached channels.
- Messaging channels show the question or approval an Agent is waiting on,
  after the reply written so far, with numbered options and buttons on
  Telegram and Discord. Participants answer by pressing a button, replying
  to the question, or sending the option number; the Agent continues the same
  task. Forms with several questions are asked one at a time.
- Channel provider seam: `ChannelInteraction` on `deliver_outbound` and
  `ChannelEvent` for providers that declare `supports_interaction_actions`, and
  `ChannelInbound.action` for a pressed button. `generic_json` replies carry a
  `message_id` and a structured `question`, and accept `reply_to` and `action`.
- Sandbox provider seam: `SandboxProvider.workspace_route()` returns the
  backend's `WorkspaceRoute` (`astrabox.seams.storage`), so a third-party
  sandbox backend can offer persistent workspaces through its own mounts. A
  route that cannot re-point a created sandbox declares
  `supports_late_binding = False`; Sessions that already have files then start
  on a new sandbox instead of a prepared one.

### Changed

- `plugin_repos` is now `packages`: each entry names its `kind`
  (`claude-plugin` for Claude Code) beside its Git source, and `plugin_paths`
  is `paths`. The Assistant fields `mcp_config_override`,
  `skill_manifest_override` and `plugin_repos_override` are now
  `mcp_servers`, `skills` and `packages`, as on an Agent. The error codes
  `PLUGIN_REPO_INVALID` and `PLUGIN_REPO_UNSUPPORTED_PROTOCOL` are now
  `PACKAGE_INVALID` and `PACKAGE_UNSUPPORTED_PROTOCOL`.
- An Assistant conversation is compressed while it is quiet, once its context
  is 40% full, instead of inside a reply the user waits for.
- An Assistant's Hermes profile state (memory, the skills it created or
  installed, scheduled jobs and its other per-profile stores) is saved in the
  platform database beside its conversation store and restored into a new
  box, so pausing or losing the sandbox no longer needs a persistent
  workspace volume to keep them.
- READY Assistant workspaces keep their sandbox lease renewed, and channel
  delivery keeps an engine reader attached, so an idle Assistant keeps running
  scheduled work and delivering it.
- A channel message that does not answer a waiting question now stops the
  waiting task before it runs. Previously a form was declined and the Agent
  continued on its own before reading the new message.
- Startup refuses `ASTRABOX_SANDBOX_WORKSPACE_VOLUME` when the default sandbox
  backend has no workspace route, instead of failing at the first conversation.

### Removed

- `SandboxProvider.created_sandboxes_self_expire`: no code read it.

### Fixed

- Editing an Agent while its prepared capacity was being rebuilt (for example,
  changing its Environment and then its prompt) no longer leaves a prepared
  sandbox running with nothing to retire it. Each client-pool namespace is now
  recorded on the Agent before it starts, so the next reconciliation retires it.
- A Codex, Pi, DeepSeek Harness or Hermes turn that fails mid-way, for
  example when its sandbox is stopped, no longer stays in the recovery scan,
  which re-queued it every ten seconds for as long as the server ran.
- `DELETE /api/v1/assistants/{id}/workspace` destroys the Assistant's sandbox
  before answering `destroyed: true`. It only changed the workspace's state,
  leaving the box running and free to be rebuilt without a request.
- An Assistant named like an Agent no longer puts its owner's conversations
  in that Agent's managers' session list.
- Deleting an Assistant deletes its conversations. They stayed behind and
  read as ready although none of them could continue without it.
- A page reloaded while a reply was streaming text shows that text. The
  session stream counted the reply's stored text as delivered once the page
  had received its last chunk live, so everything before that chunk was lost
  until the next reload.
- A message sent while a reply is running appears after that reply. The
  console placed it above the first reply of the turn until the page was
  reloaded.
- A prepared Pi conversation starts when its Agent declares a Pi package:
  the conversation no longer replays what the parked process wrote before
  it was claimed, where execd mixes the process's stderr, such as npm
  installing the package, into the JSON it reads.
- A Codex plugin's hooks run: Codex skips a hook until its hash is recorded
  as trusted, and the platform now records it for the plugins an Agent
  declares, as Codex's own review would.
- A Hermes plugin that declares Python dependencies starts under Limited
  networking: PyPI (`pypi.org`, `files.pythonhosted.org`), where Hermes'
  installer fetches them, is admitted for an Assistant with a Hermes plugin.
- A catalog-assigned MCP server works in an Environment that leaves
  `allow_mcp_servers` off: that setting admits MCP URLs declared on the Agent
  itself, and the deployment's extension gateway is now admitted as a platform
  destination.
- An Agent on Codex, Pi or DeepSeek Harness with a connector turned on no
  longer fails to start: the runtime planning counted the connector, a
  platform MCP server those engines reach through the platform, as MCP
  configuration they do not accept.
- DeepSeek Harness boxes no longer send conversations to DeepSeek: the
  harness's session-log upload and its OpenTelemetry feedback export, both on
  by default upstream, are turned off with the vendor's own switches.
- A Hermes scheduled run that answers `[SILENT]` is kept in the job's output
  and no longer delivered to the conversation or its channels.
- Listing connectors no longer fails when the connector hub's per-client rate
  limits trip; the bundled hub runs with them off, because every request
  reaches it from the AstraBox server and all of a deployment's people would
  share one client's allowance.
- A platform tool card, such as a connection request, stays in the
  conversation after its turn ends and appears in Assistant conversations.
- A Hermes reply started by the engine itself no longer ends the next user
  turn early with its own text.
- Recovering a Hermes turn after the server reconnects no longer discards the
  turn's own remaining output.
- A channel conversation no longer stops responding when the Agent waits on an
  approval or a decision: every later message was refused until it was dropped.
- Pi and Codex forms waiting in a channel conversation can be superseded by a
  new message.
- With a persistent workspace volume on Kubernetes, deleting a sandbox no
  longer logs `workspace view release failed` on every reconciliation while
  its Pod terminates; the view is released once the Pod is gone, and an error
  is logged only for a Pod that still mounts the view and is not terminating.
  `WorkspaceRoute.release_mounts` returns False for that wait.


## [0.1.4] - 2026-10-07

This release lets each Agent keep as many prepared runtimes as the conversations
it expects to start at once, including Agents whose conversations share a
sandbox, and starts prepared conversations faster. Re-run the installer to
upgrade; existing secrets and conversation data are retained.

**Upgrading:** the per-Agent prewarm switch is replaced by a prepared capacity,
and Agents created before this release have a capacity of 0, so their
prewarming is off after the upgrade. To keep a fast first conversation, set
**Prepared capacity** to 1 or more in each Agent's **Runtime and availability**
settings, or send `"prepared_capacity": 1` when updating the Agent.

### Added

- Per-Agent prepared capacity (`prepared_capacity`, 0 to 1000): how many new
  conversations can start at the same moment on a prepared runtime. New Agents
  get 1 where the deployment can prewarm. The Agent page shows how many units
  are ready against the target and when a conversation last found none, and
  `astrabox_prepared_capacity_misses_total` counts those misses.
- `ASTRABOX_PREPARED_CAPACITY_LIMIT` refuses an Agent write that would take the
  deployment's total prepared capacity above it.
- Agents whose conversations share a sandbox honour their capacity: each
  prepared sandbox carries as many conversation seats as its memory admits, and
  a burst of concurrent conversations each starts on a prepared seat.

### Changed

- **Breaking:** `prewarm_enabled` is removed from the Agent API, `astrabox.yaml`
  documents and the console; use `prepared_capacity`, where 0 turns prewarming off.
- Changing an Agent's managed MCP selection replaces its waiting prepared
  runtimes, because engines load their MCP servers when they start.
- Prepared conversations start faster: on a Docker host, a claim no longer asks
  Docker for image tags, and gateway-key cleanup and recovery lookups moved off
  the claim path.
- Update the console's AI SDK and Vite families, the end-to-end suite's MCP SDK
  and Hono, and ruff, mypy and anyio.

### Fixed

- Prewarm replenishment stalled for up to 13 minutes after a server restart;
  a restarted server resumes it at once, and another replica takes over within
  about two and a half minutes when one stops for good.
- Collect the platform MCP bindings of prepared runtimes that expired unclaimed.
- A fresh developer install passes `make typecheck` again with version 37 of
  the Kubernetes Python client, which types model fields as optional.

## [0.1.3] - 2026-10-05

This release adds platform-hosted artifacts, updates all five bundled engines,
and fixes DeepSeek Harness images, plans and questions, Pi workflow children,
Hermes history, and session recovery after a backend restart. Re-run the installer to upgrade; existing settings,
secrets, and conversation data are retained.

### Added

- Platform-hosted Agent and Assistant artifacts through built-in, origin-scoped
  `artifacts` MCP tools, with immutable versions, a console viewer and gallery,
  revocable shares, live data and presence, downloads, and consent-controlled
  model requests and Agent runs. Pinned shares fix content under current runtime
  policy; messaging-channel Sessions exclude the tools, and each Agent has an
  Artifacts switch (`artifacts_enabled`, on by default) that removes them from
  its conversations.

### Changed

- Update the bundled engines to Claude Code 2.1.287 (Python SDK 0.2.163),
  Codex 0.160.0, DeepSeek Harness 0.2.0-rc.2, Pi 1.0.0, and Hermes 0.21.5.
- Update Pi's sub-agent extension to 0.74.0, including its upstream Pi 1.0
  child-start compatibility fix, and use its current workflow input.
- Support DeepSeek Harness's native Messages and Files API credential delivery
  alongside its Chat Completions gateway route. DeepSeek catalog models now
  require a Messages-compatible model endpoint.
- Read DeepSeek Harness's current tool-result messages in the main conversation
  and child transcripts, including retained output after backend restarts.

### Fixed

- Preserve native tool identities and complete plan text in DeepSeek Harness
  plan reviews, including approval and the resumed conversation.
- Preserve complete engine usage data in conversation history.
- Route DeepSeek catalog models through LiteLLM's native provider so Messages
  tool histories retain their order during multi-step tasks.
- Carry gateway-declared image capabilities into DeepSeek Harness's model
  configuration so vision-capable gateway aliases can read images.
- Restore DeepSeek Harness image attachments from the platform database after
  sandbox replacement, so earlier images remain usable without re-uploading.
- Preserve conversation ownership when reattaching a runtime after a backend
  restart, including control operations and continued messages.
- Apply nested session cursor filters in SQL, avoiding reads of earlier pages
  while retaining conversation ownership, visibility, and ordering.
- Clean up newly allocated sandbox pool members when creation is cancelled,
  preserving members already handed to a conversation.
- Preserve Hermes's native binary session identities when saving and restoring
  its conversation database after sandbox replacement.
- Read complete saved Hermes child conversations, including tool arguments,
  results, the final reply, model, and usage, without requiring the original runtime.
- Keep Pi workflow children visible with their current names and controls,
  including updates that do not change a child's running state.
- Show Pi tool failures as failed in the live conversation and retained history,
  preserving the complete error output and allowing the conversation to continue.
- Prevent a delayed sandbox lease check from overwriting a newer renewal for
  conversations, shared Agents, and Assistant workspaces.
- Answer a dismissed DeepSeek Harness question with the engine's own
  cancellation result, so the conversation continues normally afterwards.
- Open Hermes conversations with long saved histories quickly by reading their
  native snapshot through its indexed batch instead of scanning earlier snapshots.
- Keep what you type into a new conversation while it starts: a history read
  that arrives after the Session is ready no longer remounts the composer.

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
