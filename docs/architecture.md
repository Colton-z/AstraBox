# Architecture

AstraBox keeps the service that manages Agents separate from the sandboxes
where Agent programs run. An Agent can therefore keep working when the browser
or developer's computer disconnects, while its sandbox remains isolated from
AstraBox accounts and stored product data.

## How AstraBox works

![What runs where in an AstraBox deployment](./img/architecture-system.svg#inline)

| Part | Responsibility |
| --- | --- |
| **AstraBox service** | Serves the web console and API, authenticates users, authorizes access, stores Agent and Session records, and streams work to clients. |
| **OpenSandbox** | Creates, connects, pauses, resumes, and deletes isolated sandboxes on Docker or Kubernetes. |
| **Sandbox** | Contains the Agent program and the operating-system software it can use. The Environment selects its image, network access, and lifecycle behavior. |
| **Agent program** | Interprets the task, calls the model and its available capabilities, and continues the work until it needs input or finishes. |

AstraBox coordinates authorization, runtime assignment, credentials, saved
state, and delivery of input and output. It uses the selected providers'
supported operations for sandbox allocation, isolation, and storage. An Agent
program runs its own tools, permissions, and conversation protocol through its
adapter. These integrations participate in the same platform workflow.

The Agent program reads and writes the Session workspace at `/workspace` in
the bundled images. An application started there can be opened through the URL
provided by OpenSandbox; that browser traffic does not pass through the
AstraBox API.

The model endpoint can be the bundled LiteLLM service, an existing LiteLLM
deployment, or an endpoint supplied by a model plugin. The Environment form
lists the Agent programs installed in the current deployment and validates the
selected sandbox image. The repository introduction carries the release roster
and image names.

## Main components

### Web console and HTTP API

The React console uses the same FastAPI service available to other clients.
Production images serve both on port `8000` inside the container. The service
provides:

- Agent, Environment, Session, Deployment, Assistant, and administration APIs;
- identity and access checks;
- saved Session history and live server-sent events (SSE);
- approval and question responses;
- sandbox lifecycle operations through the selected provider; and
- destination-bound MCP egress and messaging integrations.

`GET /healthz` is the liveness check. `GET /readyz` indicates whether the
replica should receive traffic. See [Deployment](deploy.md) for rolling-update
settings.

### Sandbox providers

The sandbox provider interface carries lifecycle, networking, permission, and
protected-credential requests without exposing a provider SDK. A selected
provider maps the capabilities it supports and refuses the rest before work
starts.

The built-in `open_sandbox` plugin calls an OpenSandbox lifecycle service. The server
image can run that service against a local Docker daemon or connect to a
separately managed OpenSandbox deployment backed by Kubernetes.

OpenSandbox creates sandboxes, resolves their addresses, renews leases, and
performs pause, resume, and cleanup operations supported by the deployment.
Isolation strength comes from the configured container runtime, such as Docker,
gVisor, or Kata. See [OpenSandbox backend](providers/opensandbox.md).

For an HTTP service inside a sandbox, AstraBox authorizes access and returns an
OpenSandbox URL. The browser then connects through OpenSandbox's Docker port
proxy or Kubernetes ingress. This path supports relative assets, SSE, and
WebSocket traffic without routing application bytes through AstraBox.

### Agent programs

Each installed Agent program has an adapter between its native API and
AstraBox's Session operations. Reasoning, tool selection, permission semantics,
and program-specific configuration remain native to that program.

The adapter declares its image, startup inputs, state format, and connection
requirements. Common platform startup code prepares the workspace, resolves
credentials, restores saved native state, and then activates the adapter. A
dedicated sandbox and an isolated placement in an Agent-shared sandbox use
this same orchestration.

An Environment must select an image compatible with its `engine_kind`. See
[Add an Agent program](writing-an-engine-adapter.md) for the required interfaces
and tests.

### Messaging platforms

The server image includes a private gateway for the supported official Satori
adapters. AstraBox stores provider configuration, write-only bot
credentials, inbound messages, delivery state, and source cursors. The gateway
maintains third-party connections and translates provider events.

Concrete providers need no separate Koishi or Satori deployment. The Satori
Protocol Server option connects to an endpoint an operator already runs. A
multi-replica installation can configure one authenticated external channel
gateway.

### Data and credentials

PostgreSQL is the deployment default for AstraBox records. MongoDB is available
through the `mongo` extra, and SQLite suits local development and tests. Team
deployments use shared PostgreSQL or MongoDB. The bundled LiteLLM service uses
a separate database and database role.

AstraBox Credential Vault records are organization-level resources. The `local`
secret-store plugin encrypts values with AES-GCM; `aws_kms` uses AWS KMS envelope
encryption. Administrators assign Vaults to Agents or Assistants. Conversation
requests use that administrator-managed assignment.

User identity is separate from runtime credentials. It determines who may use
a resource. Protected delivery is also separate: when enabled, the Agent
process receives a placeholder and the selected sandbox provider adds the real
credential only to a matching request. The built-in OpenSandbox provider uses
its outbound Credential Vault for that mapping.

The platform combines model, MCP, and configured environment credentials
before handing them to the selected sandbox provider. Cold startup, allocation
from prepared capacity, and reconnection use the same credential rules.
Agent-program adapters declare the inputs they need; the provider implements
the supported delivery mechanism.

## What happens when a Session runs

![The path of one Session request](./img/architecture-request-path.svg#inline)

1. The console, API, or an integration sends input to a Session.
2. AstraBox authenticates the caller, checks authorization, and saves the input
   before dispatching it.
3. AstraBox uses the Session's current sandbox. If a runtime must be prepared
   or rebuilt, it resolves the current Agent and Environment, then asks
   OpenSandbox to create, claim, resume, or reconnect the sandbox.
4. AstraBox confirms the workspace and credentials, restores native state when
   required, and connects to the selected Agent program before sending input.
5. The Agent program works in `/workspace` and returns its native output.
6. AstraBox stores the resulting Session events and streams them to the client
   over SSE.
7. A result, question, approval request, interruption, or error updates the
   Session state.

Closing the browser does not cancel the work. A client can reconnect with the
last `after_seq` cursor and continue receiving saved events. See
[SSE Event Stream](events-stream.md).

Starting a runtime can follow several paths:

| Path | Preparation before user input |
| --- | --- |
| Cold start | Allocate a sandbox or isolated placement, prepare its workspace and credentials, and start the Agent-program connection. |
| Prewarmed start | Allocate prepared capacity, associate it with the Session, select its workspace, and complete credentials and state restoration before activation. |
| Reconnect or resume | Reconnect to surviving compute, or restore the saved native state into replacement compute and use the program's native resume operation. |

Prepared capacity can exist before the Session that receives it. A persistent
workspace is therefore selected before delivery. A shared sandbox keeps its
existing root while each conversation receives its own isolated directory.
If compute is confirmed lost, the affected task fails; a subsequent message
can resume from saved native state without replaying the failed task
automatically.

## Where data lives

| Data | Stored in |
| --- | --- |
| Agent, Environment, and Deployment records | The database configured for AstraBox |
| Credential values | The configured Secret Store |
| Session state, messages, and Events | The database configured for AstraBox |
| Native Agent-program state (SessionStore) | The AstraBox database, in the program's native record or snapshot format |
| Session and Assistant files | `/workspace`, backed by an optional persistent workspace volume; otherwise retained with the sandbox or a supported snapshot |

Native state includes the records the Agent program needs to resume its
conversation, including native child-conversation state. AstraBox saves those
records without replacing them with the rendered message history. Before a
replacement runtime resumes, the records are restored through the program's
state interface or into its native files. This database storage works without
a persistent workspace volume.

Workspace files have a separate lifetime. With a persistent workspace volume,
replacement compute accesses the same workspace directory. Without one,
deleting the sandbox removes files not retained in a supported snapshot,
downloaded, committed to a repository, or saved elsewhere. Assistant pause
saves native state before releasing compute; preserving its workspace files
also requires persistent workspace storage. See [Files](files.md) and
[OpenSandbox](providers/opensandbox.md).

When persistent workspaces are enabled, the storage provider supplies their backing
filesystem, and the platform's common mergerfs router supplies a fixed entry
for each sandbox. Before delivery, the platform binds that entry to the new or
existing workspace. OpenSandbox mounts only the workspace child, not the
router's management root. Engines do not choose the storage medium or implement
directory routing. Local filesystems suit a single sandbox host; multiple hosts
require the same shared filesystem. No-volume deployments do not need a router.
See [workspace deployment](deploy.md#where-conversation-workspaces-live).

The `mounted_volume` storage plugin uses the deployment's configured volume.
The `aws_efs` plugin verifies an existing EFS CSI-backed Kubernetes volume with
shared storage access. Both use the same platform directory routing. The
deployment manages the backing filesystem and, for EFS, the filesystem, CSI
driver, and AWS credentials.

Directory selection happens before the sandbox is delivered to a Session.
Once assigned, that entry cannot be rebound to another workspace. The mergerfs
helper runs outside the user sandbox; it is not a user-facing switch for
changing filesystems during a task. On Kubernetes, the view volume's node
affinity keeps the sandbox on the node that runs its filesystem helper.

## Isolation and access

The Agent program does not receive AstraBox login credentials or direct access
to the AstraBox database. The Environment controls whether its sandbox has
limited or unrestricted network access. When credential protection is enabled,
protected model, MCP, and assigned outbound Vault credentials remain outside
the sandbox and are added only to matching outbound requests. This is not a
guarantee for every environment variable: native tracing authentication is
passed to the Agent program's OpenTelemetry configuration inside the sandbox,
not through outbound Vault injection. See the
[tracing configuration reference](cli/configuration.md#tracing).

The sandbox image already contains the Agent program, its AstraBox service,
system accounts, and common software before the sandbox starts. This is
required because a prewarmed sandbox can exist before the Session that claims
it. Add common software and platform capabilities while building the image;
prepare only Session-specific workspace content after assignment.

## Deployment options

| Option | Where Agent work runs |
| --- | --- |
| One Docker host | OpenSandbox creates a separate container for each sandbox on the AstraBox host. |
| Kubernetes | OpenSandbox creates sandbox workloads across the configured cluster. |
| Existing OpenSandbox service | AstraBox uses the Docker or Kubernetes runtime managed by that service. |

The AstraBox API and Session behavior are the same for all three options. See
[Deploy AstraBox](deploy.md) for setup, security, and backup requirements.

Two separate choices determine how work is distributed:

| Choice | What is shared |
| --- | --- |
| AstraBox backend replicas | API and orchestration replicas use the same product database, credential configuration, and reachable sandbox service. Saved Session and native state are available independently of a replica's local memory. |
| Sandbox worker nodes | The sandbox service places compute on its workers. Persistent workspaces spanning workers require shared backing storage; each mergerfs view runs beside its consuming sandbox. |

Increasing backend replicas does not make a node-local workspace filesystem
shared. Configure the two independently: database and service access for the
backend replicas, and suitable compute and workspace storage for sandbox
workers.

## Extensions {#plugin-interfaces}

Installed provider packages can connect another Agent program, sandbox
backend, model service, data store, Secret Store, identity provider, workspace
store, extension source, or messaging platform without changing the Session
API. AstraBox checks provider compatibility at startup and refuses an unknown
or incompatible configuration.

For extension points, see [Embed AstraBox](embedding.md),
[Add an Agent program](writing-an-engine-adapter.md), and
[Add a messaging platform](writing-a-channel-provider.md).

## Related documents

- [Overview](overview.md) — Agent, Environment, Session, and Event.
- [Deploy AstraBox](deploy.md) — installation and operations.
- [OpenSandbox](providers/opensandbox.md) — sandbox behavior and setup.
- [HTTP API](api.md) — requests, authentication, responses, and streaming.
