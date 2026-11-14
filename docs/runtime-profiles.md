# RuntimeProfiles

> Choose the Agent program, sandbox, network, and model connection used by a
> Session.

A RuntimeProfile defines the runtime used by a Session, including its Agent
program, sandbox image, model connection, and network access. Select an
RuntimeProfile available in your AstraBox deployment or create one for a specific
task.

## What a RuntimeProfile Is

A RuntimeProfile is the infrastructure layer beneath a Session:

- **Agent program** - the installed program that powers the Agent, together
  with the sandbox service that runs it.
- **Sandbox image** - the prebuilt image containing the Agent program, system
  packages, language runtimes, accounts, and background services.
- **Connections and controls** - the model service, outbound network access,
  sandbox lifecycle, and optional tracing settings.

When a Session starts, AstraBox creates or assigns a sandbox from the specified
RuntimeProfile template.

## Field Reference

The console presents the following settings.

| Field | Required | Description |
| --- | --- | --- |
| Name | Yes | Unique RuntimeProfile name. It cannot be renamed after creation. |
| Display name | No | Name shown when an Agent author selects the RuntimeProfile. |
| Description | No | What the RuntimeProfile is intended for. |
| Agent program | Yes | The installed program that powers Agents in this RuntimeProfile. |
| Enabled | No | Whether the RuntimeProfile can be selected for an Agent. |
| Sandbox service | No | The service that creates and manages sandboxes. AstraBox includes OpenSandbox support. A RuntimeProfile is refused on a service that cannot create sandboxes under its network access rules. |
| Sandbox image or template | No | The prebuilt sandbox image. When omitted, the selected Agent program uses its deployment default. |
| Sandbox CPU | No | The most CPU each sandbox may use, such as `8` or `500m`; at least `250m`. When omitted, the deployment's size applies. See [Sandbox size](#sandbox-size). |
| Sandbox memory | No | The most memory each sandbox may use, such as `16Gi`; at least `1Gi`. When omitted, the deployment's size applies. See [Sandbox size](#sandbox-size). |
| Network access | No | `Limited` allows required platform connections plus selected destinations; `Unrestricted` allows all outbound destinations. |
| Idle sandbox behavior | No | Terminates an idle sandbox or, when the sandbox service supports snapshots, pauses it for later recovery. |
| Sandbox sharing | No | Gives each conversation its own sandbox or shares one sandbox across the same Agent's conversations. Sharing runs each conversation in an isolated session, and is refused on a sandbox service that cannot. Without sharing, an Agent program that runs as a long-lived process in the sandbox (pi), and an Agent's terminal panel, need a sandbox service that keeps such processes; they are refused on one that does not. |
| Sandbox permissions | No | Grants the system permissions required by the selected Agent program and sandbox-sharing mode. |
| Container engine | No | `None` (default) or `Docker`: runs a rootless Docker Engine in each sandbox so the Agent can build images, run containers and use Docker Compose. Requires a sandbox per conversation, `Advanced` sandbox permissions and the bundled OpenSandbox server on Docker. See [Container engine](#container-engine). |
| Host networks | No | Docker networks of the sandbox host that each sandbox joins (`sandbox_networks`), from those the deployment allows. See [Host networks and storage](#host-networks-and-storage). |
| Host storage | No | Host directories and named volumes mounted into each sandbox (`sandbox_mounts`), read-only unless stated, from those the deployment allows. See [Host networks and storage](#host-networks-and-storage). |
| Model connection | No | Selects the model gateway and its connection details for Agents in this RuntimeProfile. |
| Tracing | No | Sends supported OpenTelemetry data from the Agent program to your collector. |

## Runtime Configuration

AstraBox is self-hosted, so a RuntimeProfile does not switch between a vendor's
managed cloud and a self-hosted worker. It selects infrastructure available in
your AstraBox deployment: an Agent program, a sandbox service and image, and a
model connection.

Pre-warming is configured on an Agent, not on its RuntimeProfile. When enabled,
AstraBox prepares the Agent's complete runtime from the selected RuntimeProfile
before a Session needs it. Both tenancy modes use OpenSandbox's official SDK
client pool. Agent tenancy prepares an isolated engine slot in a shared sandbox;
conversation tenancy prepares a complete sandbox before the pool makes it
available for a Session to claim.

For most installations, select an existing RuntimeProfile when creating an Agent.
Deployment operators can create additional RuntimeProfiles when Agents need a
  different Agent program, image, model connection, network rules, or sandbox
lifecycle.

## Sandbox size {#sandbox-size}

**Sandbox CPU** and **Sandbox memory** size every sandbox created from the
RuntimeProfile: a sandbox started for a conversation, one prepared ahead for an
Agent, and one that replaces a lost sandbox. Agents that build software and
Agents that only watch for events can then run side by side, for example one
RuntimeProfile with `8` CPU and `16Gi` for build work and another with `1` CPU
and `2Gi` for a watcher. A field left empty takes the deployment's size for
that kind of sandbox (`ASTRABOX_SANDBOX_*`, `ASTRABOX_SHARED_SANDBOX_*` or
`ASTRABOX_ASSISTANT_SANDBOX_*`, see
[Deploy](deploy.md#size-sandboxes-and-cap-what-they-use)).

Both are limits, the most a sandbox may use, written as Kubernetes quantities:
CPU in cores (`2`, `0.5`) or millicores (`500m`), memory with its unit (`4Gi`,
`1536Mi`). On Kubernetes, the scheduler also reserves the RuntimeProfile's
memory for each sandbox. Its CPU reservation stays the deployment's
(`ASTRABOX_SANDBOX_CPU_REQUEST` and its shared and Assistant counterparts),
lowered to the RuntimeProfile's CPU when that is smaller, because a sandbox
that finds the CPU busy only runs slower. On Docker, reservations do not apply.

With **Sandbox sharing**, the size is that of the sandbox the Agent's
conversations share. It takes another conversation while its free memory
leaves 640 MiB for it, so a larger sandbox holds more conversations.

Saving refuses a size that cannot be created:

- CPU below `250m` or memory below `1Gi`, which also catches a missing unit:
  `2` is two bytes of memory.
- A size that, with the egress sidecar each sandbox runs beside it, is more
  than the deployment's [sandbox quota](deploy.md#cap-what-a-sandbox-backend-runs)
  allows in total, since the quota would refuse every such sandbox.
- With the bundled sandbox server on Docker, more CPU than the host has, or
  more memory than the host has.

A size the deployment cannot see, such as more than any Kubernetes node offers
without a quota configured, is not checked when saving: such a sandbox fails
to start when it is created.

The sandbox quota counts each sandbox at the size it was created with. A
changed size applies to sandboxes created after the change. AstraBox retires
the Agents' prepared sandboxes of the old size and prepares new ones; sandboxes
in use keep their size until they are replaced.

## Preinstalled Packages

Install software in the sandbox image before a Session starts. For example, a
custom image can add system, Python, and Node.js dependencies to an installed
Agent image. Start from the image tag of the AstraBox release you run, because
the platform components inside the image must match the server:

```dockerfile
FROM ghcr.io/colton-z/astrabox-sandbox-claude-code:0.1.9

RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential libssl-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir pandas numpy scikit-learn
RUN npm install -g typescript eslint prettier
```

| Package manager | Image command | Typical use |
| --- | --- | --- |
| apt | `apt-get install` | Debian/Ubuntu system packages |
| pip | `pip3 install` | Python packages |
| npm | `npm install -g` | Node.js packages and CLIs |

> Pin package versions and the base-image tag or digest for production. Building
> dependencies into the image keeps on-demand and pre-warmed sandboxes
> consistent and avoids installing them during Session startup.

## Setup Script

An AstraBox RuntimeProfile does not have a general setup script. Put software,
system accounts, and services required by every sandbox in the image, where they
are available even when the sandbox was prepared before the Session existed.

Keep Agent-specific code in its Git repository and reusable Agent behavior in
its Plugins and Skills. See [Container Reference](container-reference.md) for
the sandbox filesystem layout and image requirements.

## Container engine {#container-engine}

With **Container engine** set to `Docker`, every sandbox of the RuntimeProfile
runs a Docker Engine for its Agent: `docker build`, `docker run` and
`docker compose` work in the Agent's commands and in the conversation's
terminal, on the default socket path. The engine runs as the sandbox's own
user, so a container's root is that user outside the container. The engine is
part of the Codex and Claude Code sandbox images and starts when the sandbox
does; a conversation gets its sandbox once the engine answers, and a sandbox
whose engine cannot start is refused with the reason.

Requirements, checked when the RuntimeProfile is saved:

- **Sandbox sharing** is a sandbox per conversation. Containers publish their
  ports in the sandbox's network, and the sandbox's CPU, memory and disk belong
  to one conversation.
- **Sandbox permissions** is `Advanced`.
- The Agent program's image carries the engine: Codex and Claude Code.
- The sandbox service is the OpenSandbox server bundled with AstraBox, on
  Docker. It is refused on the Kubernetes runtime and on a separately operated
  OpenSandbox service, which do not grant what the engine needs.

The engine runs rootless and reaches the network through a user-mode network
stack, so a container's connection leaves as an ordinary connection of the
sandbox, under this RuntimeProfile's network access rules: a destination the
RuntimeProfile does not allow is refused to a container as it is to the Agent,
by name and by address. With `Limited` network access, allow the registries the
Agent pulls from (for Docker Hub, `*.docker.io` and `*.docker.com`) and the
hosts its builds download from.

What to expect:

- A container reaches HTTPS destinations with their own certificates. The
  exception is a host a protected credential is bound to, such as the model
  gateway: the sandbox's egress proxy decrypts those connections to deliver the
  credential, and a container must trust the proxy's certificate authority,
  `/opt/opensandbox/mitmproxy-ca-cert.pem` in the sandbox, to use them.
- `docker run -p` publishes a port in the sandbox: the Agent reaches it at
  `127.0.0.1`. A port the sandbox already uses, such as 8080 or the egress
  proxy's 18080, cannot be published.
- Containers share the sandbox's CPU, memory and process limits. The engine
  has no cgroup driver, so `--memory`, `--cpus` and `--pids-limit` are accepted
  and not applied: a container sees the sandbox's own memory limit. A
  conversation's sandbox with its browser, the engine and a two-service Compose
  scene peaked at 266 of the default 512 processes and threads
  (`ASTRABOX_SANDBOX_PIDS_LIMIT`, which applies to every sandbox).
- Images, containers and build cache live on a volume created with the sandbox
  and deleted with it. A replacement sandbox starts with none.
- `docker run --privileged` fails, and a container cannot reach the sandbox's
  own services through the engine's host-loopback address.

Enable the engine only for teams you trust. On the Docker backend, a sandbox
created for the engine runs without masked `/proc` paths and without
`no_new_privileges`: whoever gains root in it can change kernel settings of
the Docker host. The image removes every setuid program but the two the engine
needs, so `sudo` and `su` do not work in such a sandbox.

The Docker host must allow unprivileged user namespaces. Ubuntu 23.10 and later
restrict them through AppArmor by default; set
`kernel.apparmor_restrict_unprivileged_userns=0` on the host (see
[Deploy](deploy.md#container-engine-host)). Otherwise the sandbox is refused
with a message naming that setting.

## Host networks and storage {#host-networks-and-storage}

A RuntimeProfile can give its sandboxes reach beyond the sandbox: services on
a Docker network of the sandbox host, such as a shared database, message
broker or object store, and host directories or Docker named volumes, such as
a shared git object store or package caches that should not be downloaded
again for every sandbox. The deployment operator lists what may be used
([Deploy](deploy.md#share-host-networks-and-storage)); a RuntimeProfile can
name only those, and is checked again each time a sandbox is created from it.

```json
{
  "sandbox_networks": ["foundry-shared"],
  "sandbox_mounts": [
    {"source": "/srv/foundry/git-objects", "target": "/mnt/git-objects"},
    {"source": "foundry-npm-cache", "target": "/mnt/npm-cache", "read_only": false}
  ]
}
```

**Host networks.** Each sandbox joins the named networks when it is created,
cold or prepared, and a replacement sandbox joins them again. The Agent reaches the
network's containers by their container names and their network aliases that
have no dots, at the addresses they had when the sandbox was created, or by
address. The whole network is reachable, every port, in both network access
modes: joining a network is the RuntimeProfile's grant to reach it, and a
`Limited` allow list does not narrow it. Everything else still leaves under
the RuntimeProfile's network access rules, names still resolve through the
sandbox's egress proxy, and containers on the network cannot open connections
into the sandbox. With one sandbox per Agent, every conversation of the Agent
shares the joined networks.

**Host storage.** A `source` that starts with `/` is a host directory; any
other source is a named volume. Each is mounted at `target` in every sandbox
of the RuntimeProfile, read-only unless `read_only` is `false`. The storage
is shared: every sandbox that mounts it sees the same files, and it outlives
every sandbox. Files are read and written as the sandbox's own user, so the
operator sets the directory's or volume's ownership. A target cannot be at,
under or above a directory the platform or the system uses in the sandbox:
`/workspace`, `/home`, `/tmp`, `/opt/astrabox`, `/opt/conversation-runtime`,
`/opt/gem`, `/opt/opensandbox`, `/var/lib/astrabox`, `/var/lib/execd` and the
system directories (`/bin`, `/boot`, `/dev`, `/etc`, `/lib*`, `/proc`,
`/root`, `/run`, `/sbin`, `/sys`, `/usr`, `/var/run`). Mount under `/mnt` or
`/srv`, and point tools at the mount, for example with
`GIT_ALTERNATE_OBJECT_DIRECTORIES` or a package manager's cache setting in the
sandbox image.

Requirements, checked when the RuntimeProfile is saved:

- Every network, host directory and volume is one the deployment allows
  (`SANDBOX_NETWORK_NOT_ALLOWED`, `SANDBOX_MOUNT_NOT_ALLOWED`); a host directory
  is named exactly, not by a parent.
- Each target is used once and stays clear of the directories above.
- The sandbox service is the OpenSandbox server bundled with AstraBox, on
  Docker. The Kubernetes runtime, where a Pod has one network and host paths
  and volume claims are a cluster decision, and a separately operated
  OpenSandbox service are refused.

When a sandbox is created, a network that is missing, has IPv6 enabled, or
lies inside the deployment's denied networks, and a host directory or volume
that does not exist, refuse the sandbox with the reason.

Use these only for teams you trust with what they reach:

- A joined network bypasses the RuntimeProfile's `Limited` allow list for that
  network's addresses. The Agent can connect to every service on the network
  with whatever access those services grant an unauthenticated or credentialed
  client.
- A read-write mount is write access to that host directory or volume: the
  Agent can change or delete everything in it, for every sandbox and process
  that uses it. A read-only mount still exposes every file in it to the Agent.

## Create a RuntimeProfile

1. Open **Runtime profiles** in the AstraBox console.
2. Select **New runtime profile**.
3. Enter a name and select the Agent program. Configure the sandbox image, model
   connection, network access, and any lifecycle options the RuntimeProfile needs.
4. Select **Create**.

The RuntimeProfile is now available when you create or edit an Agent, as long as it
is enabled and its Agent program supports Agents.

## Read RuntimeProfiles

Open **Runtime profiles** in the AstraBox console to see every RuntimeProfile, including
disabled entries. The list shows its name, Agent program, sandbox service, and
last update time. Select a RuntimeProfile to view its complete configuration.

Agent authors see only enabled RuntimeProfiles that can run Agents. Model and
tracing credentials remain hidden from that selection list.

Disabling a RuntimeProfile prevents new Agent conversations and prewarm refreshes.
The full list (`GET /api/v1/agents` without `page=1`) retains every visible Agent
and returns `can_start_conversation`. It is false when the Agent is disabled or
deleting, or its RuntimeProfile is disabled or missing. The conversation picker
marks these entries **Unavailable** and disables **Start conversation**.
Management and history views retain the definitions. Re-enabling the RuntimeProfile
restores the action for its enabled Agents. This field describes configuration
eligibility, not runtime health; listing neither prepares compute nor checks
credentials. Conversation creation still validates the current configuration.
AstraBox retires its Agents' unclaimed prepared capacity without reclaiming
sandboxes already assigned to Sessions. Managers can still read prepared-runtime
status and delete Agents; the status shows prewarming disabled and zero available
capacity even when the Agent's saved prewarm switch remains on.

## Update a RuntimeProfile

Open the RuntimeProfile, change the required section, and select **Save**.

> Updating a RuntimeProfile does not reconfigure a running sandbox in place. The
> saved configuration is used the next time AstraBox creates, assigns, or
> recreates a sandbox for an Agent that uses the RuntimeProfile. AstraBox also
> reconciles prepared capacity for Agents that depend on the RuntimeProfile.

## Choosing a RuntimeProfile

| Scenario | Recommended configuration |
| --- | --- |
| General development | Use an existing RuntimeProfile for the required Agent program. |
| Data analysis | Use an image with pinned Python and system dependencies, and allow only the required data services. |
| Frontend development | Use an image with the required Node.js toolchain, and allow the package registry if network access is limited. |
| CI/CD integration | Use an image with the required CLIs, connect the required credentials, and allow only the target services. |

## FAQ

**Q: How long do I have to wait after creating a RuntimeProfile before I can use it?**

A: A new RuntimeProfile is immediately available. AstraBox creates or assigns the
actual sandbox when a Session needs it; pre-warming can reduce that startup
time.

**Q: Can I pin package versions?**

A: Yes. Pin them in the sandbox image build and use an immutable image tag or
digest so newly created and pre-warmed sandboxes use the same software.

**Q: How many RuntimeProfiles can I create?**

A: AstraBox does not impose a product limit. Create the RuntimeProfiles your
deployment needs and use clear names to keep them organized.

## Next steps

- [Sessions](sessions.md) - use an Agent to start a task.
- [Defining an Agent](authoring-agents.md) - review Agent configuration.
