# Deploy AstraBox

AstraBox is self-hosted. You can run the whole service on one Docker host,
connect it to OpenSandbox on Kubernetes, or use an OpenSandbox service that
your organization already operates. The maintained single-host deployment
starts the web console, API, data stores, model and messaging gateways, and the
local sandbox service. Agent tasks run in isolated sandboxes.

![AstraBox deployment options](./img/deploy-topology.svg#inline)

## Choose where Agent sandboxes run

| Deployment | Best for | Where sandboxes run |
|---|---|---|
| One Docker host | Evaluation, development, and a small trusted team | Separate containers on the host Docker daemon |
| Kubernetes with OpenSandbox | Multiple nodes, prewarmed sandboxes, snapshots, and cluster controls | Sandbox Pods created by OpenSandbox |
| An existing OpenSandbox service | Organizations that operate sandbox infrastructure separately | The container runtime configured for that service |

The maintained local deployment listens on loopback only, and has no
authentication until you turn on [team login](team-login.md). Configure team
login, TLS, and a trusted ingress before making AstraBox reachable from another
network.

For Kubernetes and external-service settings, see the
[OpenSandbox deployment guide](providers/opensandbox.md).

## Run on one Docker host {#run-on-one-docker-host}

The stack starts the following components:

| Component | Purpose |
|---|---|
| AstraBox API and console | Create and use Agents, Assistants, Sessions, and triggers |
| PostgreSQL | Store AstraBox and LiteLLM data |
| LiteLLM | Route model requests and discover available models |
| Messaging gateway | Connect Agents to messaging platforms |
| OpenSandbox | Create and manage local sandbox containers |
| Casdoor, with team login on | Sign people in to the console |

Treat the server container as a privileged host service: it controls the
mounted Docker daemon.

### Docker requirements {#docker-requirements}

| Requirement | Why |
|---|---|
| Docker Engine 26.0 or later (Engine API 1.45) | Persistent workspaces reach each sandbox through a mount of a sub-directory of a Docker volume (`VolumeOptions.Subpath`), which Engine API 1.45 added. Everything else the stack uses is older. |
| Docker Compose plugin 2.17.0 or later | `containers/compose.yaml` restarts the server when a sandbox edge is recreated (`depends_on` with `restart: true`), which Compose 2.17.0 introduced. |

The installer and the server both check the daemon and refuse an older one,
naming the version they found; the server checks at every start, so upgrading
the host never leaves it running on an engine it does not support.

### The sandbox network boundary {#sandbox-network-boundary}

Each sandbox is a container on Docker's default `bridge` network, paired with
an OpenSandbox egress sidecar that enforces its Environment's networking. The
stack keeps the platform off that network:

- The server publishes only the console, on `127.0.0.1`. PostgreSQL and Redis
  publish only loopback development ports.
- Two edge containers, `sandbox-edge` and `sandbox-dns-edge`, have their own
  addresses on the bridge. They forward only the model route, the
  capability-scoped callback routes and DNS to the server, over the private
  `sandbox-edges` network, which has no route off the host and which sandboxes
  never join.
- The lifecycle server publishes each sandbox's execd, file/terminal and
  egress-API ports on the Docker bridge gateway (`ASTRABOX_PUBLISH_HOST_IP`,
  which the installer sets from `docker network inspect bridge`), not on the
  host's other interfaces. Those in-box services have no authentication: do not
  set `ASTRABOX_PUBLISH_HOST_IP` to `0.0.0.0` unless something in front of those
  ports authenticates every request.
- Every sandbox's egress policy, under both `Limited` and `Unrestricted`
  networking, denies Docker's default bridge except `sandbox-edge`: other
  sandboxes and the bridge gateway where sandbox ports are published. The deny
  rules come before every allow rule, so an Environment allowlist entry that
  names a bridge address does not reopen it. The deny list is
  `ASTRABOX_SANDBOX_EGRESS_DENY_CIDRS`, which the server derives at startup.
  The egress sidecar enforces address rules only in its `dns+nft` mode, so the
  server refuses to start with `ASTRABOX_SANDBOX_EGRESS_MODE=dns`.
- `169.254.0.0/16`, which holds the cloud metadata service, is denied in every
  networking mode.
- Sandboxes have no IPv6. The egress sidecar turns IPv6 off in the network
  namespace it shares with its sandbox (OpenSandbox's `egress.disable_ipv6`,
  which AstraBox sets), including when the Docker daemon enables IPv6 on the
  default bridge. The deny rules above are therefore IPv4 networks.

`Unrestricted` still allows every other destination, including the host's
other addresses and your LAN. Other containers on the default bridge that are
not AstraBox sandboxes, and processes on the host, can reach the published
sandbox ports; the host and its other containers are inside the trust boundary.

Docker gives the two edges their bridge addresses when they start, and an edge
that restarts can come back at a different one. Every sandbox keeps the
addresses it was created with (its DNS upstream, its deny rules and its
callback address), so the platform records them on the sandbox. When the
server starts with different addresses, it removes the sandboxes created with
the old ones, and a server that sees an edge move while it runs exits so that
Docker restarts it. A conversation whose sandbox was removed takes a new
sandbox at its next message, as after any other sandbox loss; files in the
removed sandbox's workspace are kept only with the optional persistent
workspace volume. Sandboxes created by AstraBox 0.1.0 carry no such
record and are not removed this way. If their conversations stop receiving
answers after an edge restart, remove every sandbox on the host; each
conversation then continues on a new one:

```bash
docker ps -aq --filter label=opensandbox.io/id | xargs -r docker rm -f
docker ps -aq --filter label=opensandbox.io/egress-sidecar-for | xargs -r docker rm -f
```

### Install a release {#install-a-release}

```bash
curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh | bash
```

The installer:

1. checks Docker Engine 26.0 or later, the Compose plugin 2.17.0 or later
   (see [Docker requirements](#docker-requirements)), and that your user can
   use `/var/run/docker.sock`;
2. downloads the deployment bundle of the latest release, verifies its
   SHA-256, and unpacks the Compose files into `~/astrabox`;
3. generates the database and login secrets in
   `~/astrabox/.astrabox/database-secrets`, once, and keeps them afterwards;
4. asks which model service your Agents use, and for its API key and model ID,
   and writes their settings;
5. asks whether to turn on team login, which runs the bundled Casdoor login
   service with the stack;
6. pulls the images published for that release, starts the stack, waits until
   the console page is served, and pulls the sandbox image of the seeded Claude
   Code Agent so that the first Session does not wait for it.

Re-run it to upgrade: it installs the latest release's Compose files, or those
of the release `ASTRABOX_VERSION` names, sets that release as the image version,
and keeps the volumes, the generated secrets, the model settings, the
team-login choice, and every other line of the installation's settings file.
To change the model service, answer its question again, or run it with
`ASTRABOX_INSTALL_MODEL_PROVIDER` set; the settings it wrote for the previous
service are removed.

### Upgrade matching components

Update the server and sandbox runtime images together. Process transport changes
also require matching execd and sandbox images; an existing sandbox keeps its
installed binaries. In 0.1.10, the built-in PostgreSQL provider advances the
schema to Alembic revision `0003`, adding nullable `sessions.runtime_versions`.
External persistence providers must supply the corresponding physical schema.
Administrative version views report saved runtime observations without waking
compute; see [runtime version observations](writing-an-engine-adapter.md).

### Upgrade from AstraBox 0.1.8 {#upgrade-from-0-1-8}

AstraBox 0.1.9 uses a new PostgreSQL schema and does not automatically convert
0.1.8 records. To preserve selected records, an operator can convert and load
them into the current schema before normal startup. Follow the
[offline import guide](data-import.md) for object ordering, stable IDs,
authorization, key custody and history/native-state limits. Conversion and
execution belong to the operator; there is no built-in installation-wide
import command or general legacy-format compatibility.

The procedure below **starts fresh without the old AstraBox records**. Agents,
Environments, Assistants, Sessions/history, Deployments and Vaults are not
carried over. The release seeds its default Environment and Agent; recreate
the other resources if choosing this path.

A new database also gives the installation a new identity. AstraBox removes,
reuses, and counts only the sandboxes its own installation created, so the
sandboxes of the 0.1.8 installation keep running until you remove them. Before
you run the installer for the new release:

1. Stop the server. From `~/astrabox/containers`:

   ```bash
   docker compose stop server
   ```

2. Remove the sandboxes on the Docker host. These commands remove every
   OpenSandbox sandbox on the host, including those of any other AstraBox
   installation there:

   ```bash
   docker ps -aq --filter label=opensandbox.io/id | xargs -r docker rm -f
   docker ps -aq --filter label=opensandbox.io/egress-sidecar-for | xargs -r docker rm -f
   ```

   When sandboxes run on Kubernetes or an existing OpenSandbox service, delete
   the 0.1.8 installation's sandboxes there instead.

3. Empty the AstraBox database. In the maintained Compose stack it is the
   `astrabox` database of the `postgres` service; the LiteLLM and Casdoor
   databases are separate and keep their data:

   ```bash
   docker compose exec postgres psql -U postgres \
     -c 'DROP DATABASE astrabox WITH (FORCE)' \
     -c 'CREATE DATABASE astrabox OWNER astrabox' \
     -c 'REVOKE ALL ON DATABASE astrabox FROM PUBLIC' \
     -c 'GRANT CONNECT, TEMPORARY ON DATABASE astrabox TO astrabox'
   ```

   With an external PostgreSQL service, point `ASTRABOX_DB_URL` at a new, empty
   database instead.

4. Run the installer for the new release.

### Model services the installer configures {#model-services-the-installer-configures}

The bundled LiteLLM gateway serves each of these. Add further routes afterwards
as described in [Connect a model](models.md).

| Choice | Settings it writes | Model name the gateway routes |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | the model ID, through the `claude-*` route |
| DeepSeek | `ANTHROPIC_BASE_URL` (DeepSeek's Anthropic endpoint), `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (default `deepseek-flash`) | `anthropic/<model ID>` |
| Another Anthropic-compatible service | `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | `anthropic/<model ID>` |
| An OpenAI-compatible service | `OPENAI_COMPATIBLE_BASE_URL`, `OPENAI_COMPATIBLE_API_KEY`, `ANTHROPIC_MODEL` | `openai-compatible/<model ID>` |
| None for now | nothing | add routes in the console under **Integrated services** |

`ANTHROPIC_MODEL` is the deployment's default model: a new installation creates
its seeded Agents with it, and an Agent that selects a model enters the name in
the last column. Every Agent stores the model it runs, so changing
`ANTHROPIC_MODEL` later leaves existing Agents on theirs; select the new model
on each Agent's page. With `ANTHROPIC_BASE_URL` set, the deployment serves
the default model through the gateway's `anthropic/` routes. For DeepSeek it
also recognises the endpoint and gives the same credential to the gateway's
native DeepSeek routes, which serve the other Agent programs' Messages and
OpenAI protocols.

The `openai-compatible/*` route sends `<model ID>` to
`OPENAI_COMPATIBLE_BASE_URL` over Chat Completions. Claude Code, the Agent
program a fresh installation seeds, speaks Anthropic Messages, so LiteLLM
translates between the two protocols; prefer a service's Anthropic-compatible
endpoint when it publishes one.

### Installer settings {#installer-settings}

Setting `ASTRABOX_INSTALL_MODEL_PROVIDER` and `ASTRABOX_INSTALL_TEAM_LOGIN` runs
the installer without questions, which is what an unattended installation
needs. Without a terminal, the installer keeps the current team-login choice,
which is off on a new installation.

| Variable | Purpose |
|---|---|
| `ASTRABOX_VERSION` | Release to install. Default: the latest release. |
| `ASTRABOX_INSTALL_DIR` | Installation directory. Default: `~/astrabox`. |
| `ASTRABOX_INSTALL_BUNDLE` | Path of an `astrabox-deploy-<version>.tar.gz` downloaded elsewhere, for a host that cannot reach GitHub. |
| `ASTRABOX_IMAGE_PREFIX` | Repository prefix of a registry mirror holding the published images; the component name is appended. Default: `ghcr.io/colton-z/astrabox-`. |
| `ASTRABOX_INSTALL_MODEL_PROVIDER` | `anthropic`, `deepseek`, `anthropic-compatible`, `openai-compatible` or `none`. |
| `ASTRABOX_INSTALL_MODEL_API_KEY` | The model service's API key. |
| `ASTRABOX_INSTALL_MODEL_NAME` | The model ID written as `ANTHROPIC_MODEL`, which a new installation creates its seeded Agents with. Default for DeepSeek: `deepseek-flash`. |
| `ASTRABOX_INSTALL_MODEL_BASE_URL` | Base URL of an Anthropic-compatible or OpenAI-compatible service. |
| `ASTRABOX_INSTALL_TEAM_LOGIN` | `casdoor` runs the bundled Casdoor login service with the stack; `none` runs the stack without login. |
| `ASTRABOX_CONSOLE_ORIGIN` | With team login: the console's public URL, such as `https://astrabox.example.com`. |
| `ASTRABOX_OIDC_ISSUER` | With team login: Casdoor's public URL, such as `https://login.example.com`. Set both URLs for browsers on other computers, or neither to sign in on this host only. |
| `ASTRABOX_PUBLIC_PATH_PREFIX` | The path a gateway publishes the console under, such as `/astrabox`, when the console shares its host with other applications. See [Publish AstraBox under a path](#publish-under-a-path). With team login, the callback URL the installer writes is below this path. |

```bash
curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh \
  | ASTRABOX_INSTALL_MODEL_PROVIDER=deepseek \
    ASTRABOX_INSTALL_MODEL_API_KEY="your-deepseek-api-key" bash
```

Compose settings live in `~/astrabox/containers/.env`, which the installer
keeps across upgrades. Create or edit it to change a port, a volume name or any
other Compose value, then re-run the installer:

```bash
printf '%s\n' "ASTRABOX_SERVER_HOST_PORT='9000'" >> ~/astrabox/containers/.env
```

Manage the installed stack from that directory with the ordinary Compose
commands, `docker compose ps`, `docker compose logs -f server` and
`docker compose down`. With team login on, they include Casdoor.

### Turn on team login {#turn-on-team-login}

```bash
curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh \
  | ASTRABOX_INSTALL_TEAM_LOGIN=casdoor \
    ASTRABOX_CONSOLE_ORIGIN=https://astrabox.example.com \
    ASTRABOX_OIDC_ISSUER=https://login.example.com bash
```

The installer adds the SSO overlay, `containers/compose.sso.yaml`, to the
`COMPOSE_FILE` setting in `~/astrabox/containers/.env`, so the stack and every
`docker compose` command in `~/astrabox/containers` run Casdoor with AstraBox.
Casdoor listens on `127.0.0.1:8087`. With the two public URLs, the installer
also writes the callback URL and the Host allowlist the server needs; put both
loopback ports behind an HTTPS proxy as described in
[Put the login flow behind a proxy](team-login.md#put-the-login-flow-behind-a-proxy).
Leave both URLs out to sign in on this host only.

When it finishes, the installer prints where the generated passwords are kept:
that of the `admin` account people sign in to AstraBox with, and that of
Casdoor's own administrator, `built-in/admin`. AstraBox accepts only accounts of
the bundled `astrabox` organization; see
[Turn on the bundled login service](team-login.md#bundled-login-service).

Running the installer again keeps team login on. To turn it off, run it with
`ASTRABOX_INSTALL_TEAM_LOGIN=none`. An installation with public URLs refuses
until they are removed from `containers/.env`, so that it never serves the
console without login to other computers.

### Published images {#published-images}

Every release publishes these images, tagged with its version. The installer
writes that version to the settings file as `ASTRABOX_IMAGE_TAG`; a deployment
that names no image of its own runs `<ASTRABOX_IMAGE_PREFIX><component>:<ASTRABOX_IMAGE_TAG>`
for its server, every Agent program's sandbox and the workspace helper.

| Image | Architectures |
|---|---|
| `ghcr.io/colton-z/astrabox-server` | amd64, arm64 |
| `ghcr.io/colton-z/astrabox-sandbox-claude-code` | amd64, arm64 |
| `ghcr.io/colton-z/astrabox-sandbox-codex` | amd64, arm64 |
| `ghcr.io/colton-z/astrabox-sandbox-deepseek-harness` | amd64, arm64 |
| `ghcr.io/colton-z/astrabox-sandbox-hermes` | amd64, arm64 |
| `ghcr.io/colton-z/astrabox-sandbox-pi` | amd64, arm64 |
| `ghcr.io/colton-z/astrabox-workspace-mounter` | amd64 |

The workspace helper is amd64 only: it installs the mergerfs release archive
built for that architecture.

### Run from a clone {#run-from-a-clone}

A checkout builds and runs its own images, `astrabox/<component>:latest`:

```bash
make build-agent-image

export ANTHROPIC_API_KEY="your-anthropic-api-key"
export ANTHROPIC_MODEL="your-model-name"
scripts/compose.sh up --build -d
```

Open <http://127.0.0.1:8088>.

On Linux, `scripts/compose.sh` detects the Docker socket's group. If detection
does not work for your Docker installation, set its numeric group ID before
starting the stack:

```bash
export DOCKER_GID="$(stat -c %g /var/run/docker.sock)"
scripts/compose.sh up --build -d
```

On macOS, use `stat -f %g` for the socket group, or rely on Docker Desktop's
socket access.

## Where conversation workspaces live {#where-conversation-workspaces-live}

Workspace file persistence is optional. The bundled backing-medium adapter is
`ASTRABOX_STORAGE_PROVIDER=mounted_volume`. Persistent deployments set
`ASTRABOX_SANDBOX_WORKSPACE_VOLUME` to the backing volume's name: an existing
PersistentVolumeClaim on Kubernetes, or a named volume on Docker. Workspaces
occupy separate directories on that backing filesystem. With the bundled
OpenSandbox backend, mergerfs views created outside user sandboxes carry them;
OpenSandbox mounts those views through its standard PVC or named-volume
interface. Binding to the selected new or existing workspace completes before
delivery to the user. The storage adapter selects the backing medium; the
sandbox backend decides how it is mounted, and startup refuses this volume when
an enabled backend cannot mount a persistent workspace.

An unset or empty value leaves workspace files on the sandbox's temporary
filesystem; replacing that sandbox can discard those files. This setting governs
workspace files, not the platform database's custody of native SessionStores.
In this no-volume mode no mount helper runs, whatever
`ASTRABOX_WORKSPACE_MOUNTER_IMAGE` names.

With workspace storage enabled, Assistant profiles are also mounted by default.
The filesystem holding a profile must support the engine's live SQLite and
atomic file operations. Set
`ASTRABOX_ASSISTANT_PROFILE_ON_WORKSPACE_VOLUME=false` to mount only the user
workspace and keep the profile on the sandbox filesystem. The common mount
planner applies this choice before allocation; storage providers receive the
resulting plan without filtering paths. Native and profile snapshots still use
the platform database, and an unconfirmed save still retains the allocation.

Hermes snapshots preserve its complete SessionDB and the supported profile
state: memories, user skills, scheduled jobs and execution history, project and
memory stores, kanban boards, verification evidence and A2A records. Runtime
configuration and credentials are supplied by the platform. Arbitrary extra
files in the profile, caches and vendor messaging-pairing data are outside this
profile snapshot contract; keep user task files in the workspace.

Before changing this option for an existing Assistant, confirm both native and
profile snapshots through its normal save/hibernate path. An Assistant with an
unconfirmed save must finish recovery on its retained allocation first. Keep the
previous backing directory and recovery backups; changing this option does not
delete or import them.

With an existing OpenSandbox service (`ASTRABOX_SANDBOX_OPENAPI_BASE_URL`),
persistent workspaces require that service's Kubernetes runtime; see
[Connect an existing OpenSandbox service](providers/opensandbox.md#connect-an-existing-opensandbox-service).

### Configure the workspace helper

`ASTRABOX_WORKSPACE_MOUNTER_IMAGE` names the helper image; unset, it is the
release's own `ghcr.io/colton-z/astrabox-workspace-mounter:<version>`. Set it to
an image of your own, built with `make build-workspace-mounter-image` and
published under an immutable tag, when you change the helper. This is a
separate host-side helper, not an additional agent-runtime component.

Set `ASTRABOX_WORKSPACE_STORAGE_TOPOLOGY=local` only for one eligible sandbox
host. Local ext4 is suitable for that topology. Multiple sandbox hosts require
`shared` and the same remotely accessible filesystem on every eligible host;
identical directory names on separate local disks are not shared storage.
API server replicas alone do not change this requirement.

`ASTRABOX_WORKSPACE_MOUNT_ROOT` names a host-side directory for assignment
views, defaulting to `/var/lib/astrabox/workspace-mounts`. Keep it separate
from the backing workspace data. Each helper receives only its own view
directory and uses bidirectional mount propagation. On Docker, configure the
containing host mount as shared before enabling persistent workspaces; Docker refuses
`rshared` propagation from an unshared source. The helper requires Linux 6.9+
on amd64, root, `/dev/fuse`, and privileged mount access. User sandboxes do not
receive those privileges. The helper requires native mergerfs I/O passthrough
and its compatible cache mode; it does not silently disable passthrough.

On Kubernetes, the platform identity needs permission to manage the helper
Pods and their exec endpoint, assignment PVCs and PVs, and to list Nodes.
The namespace must permit these privileged infrastructure Pods. The backing
PVC remains operator-owned. Kubernetes schedules each helper; its view PV
pins the sandbox to that same node. Losing a helper invalidates its existing
FUSE mount and is not repaired by silently restarting it under a live sandbox.
Docker's view volume is a recursive bind of the helper's view directory, so
its numbered FUSE submounts are present in the consumer; a nonrecursive bind
would expose the empty underlying host directories instead, which the sandbox
mount check rejects. The bind is a slave copy: Docker unmounts the volume when
its last container exits, and a shared copy would carry that unmount back to
the helper's views on the host. Docker only mounts such a volume for a
container that uses it by name, so the sandbox receives its workspace through
Docker's volume sub-path mount, one reason the stack needs Docker Engine 26.0
([Docker requirements](#docker-requirements)).

Each helper records, from the moment it is created, the assignment it serves,
the sandbox backend and the AstraBox installation. The expiration watcher
releases a view once its sandbox is confirmed gone, and also releases a view no
sandbox ever took: when a create fails or provisioning stops part way, the view
is removed ten minutes after its helper was created if no sandbox of the
installation carries its assignment. A view a container or Pod still mounts is
never removed. Each enabled backend's route is reconciled on its own. A view
whose helper names a sandbox backend this deployment does not enable is kept and
logged, because no enabled backend can confirm that its sandbox is gone.

### What the medium has to support

For AWS EFS, use the [`aws_efs` storage provider](providers/aws-efs.md). It
verifies the configured EFS CSI claim; the OpenSandbox backend's workspace
route is unchanged.

A workspace is a working directory, not a document store, and the code that
prepares one uses these operations. A medium that lacks any of them is not a
candidate, however convenient its capacity:

| Operation | Used by |
|---|---|
| `rename` onto an existing path | publishing each file of a plugin-repo cache, so a sibling conversation never reads half of one |
| `chmod` (mode preservation) | plugin scripts committed `100755`; without it the conversation runtime has executables it can neither run nor `chmod`, because the cache is root-owned |
| `flock` | serialising concurrent clones into one Agent's shared cache |
| symbolic links | the conversation's visible `/workspace` is a symlink onto its physical directory, and the mount check resolves through it |
| `ReadWriteMany` | several boxes mount the claim at once, and one Agent's box carries several conversations |

| Medium | Verdict |
|---|---|
| POSIX network filesystems, such as NFS, EFS and CephFS | Must support the operations above, have a filesystem type recognized by the helper, and provide a claim that binds before a consumer starts. Do not serve NFS from the same node that mounts it. |
| Cluster default StorageClass | Depends what it provisions. Confirm it is POSIX and not node-local before relying on it. |
| Object storage through a FUSE driver | Use only after proving every operation above. Mountpoint for Amazon S3 does not qualify. |

In `shared` topology, the helper recognizes `nfs`, `nfs4`, `cifs`, `smb3`,
`ceph`, `fuse.ceph`, `glusterfs`, `fuse.glusterfs`, `lustre`, `gpfs`, `beegfs`,
and `fuse.juicefs`. Other filesystem types are rejected, even when a driver
advertises POSIX support. Recognition does not replace checking the required
operations on the chosen deployment.

Mountpoint for Amazon S3 documents that file rename is unavailable on
general-purpose buckets, directory rename is unavailable on every bucket type,
and `chmod`, `lockf`, hard links, and symbolic links are unsupported. These
limitations conflict with the workspace operations above. See its
[file-system behavior](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md).

Object storage still has a place in this system — snapshots, exports, backups
are whole objects written once. A live working directory is not.

OpenSandbox also defines an `ossfs` volume type, but AstraBox does not select
it. When workspace persistence is configured, every sandbox creation path,
including prepared capacity, uses the platform's fixed-view volume plan over
the selected storage provider's backing medium.
AstraBox accepts only storage providers configured for the deployment.

### What the deploy checks

When enabling workspace persistence on Kubernetes, provide a bound
`ReadWriteMany` PersistentVolumeClaim with the name in
`ASTRABOX_SANDBOX_WORKSPACE_VOLUME`. The storage helper mounts this backing
claim; OpenSandbox receives the assignment's ready view claim. AstraBox refuses runtime preparation
when the configured storage provider cannot confirm the requested mount.

## Connect messaging platforms

The server image already contains the pinned Satori adapters and their private
connector runtime. Create a messaging-platform Deployment from the Agent page and complete
the provider form. The detail page shows the callback URL or connection state
and links to the provider's official developer console.

Single-server installations use the packaged connector on container loopback.
A multi-replica installation can operate one shared connector behind
authenticated HTTPS. Set `ASTRABOX_CHANNEL_GATEWAY_BASE_URL` to that endpoint
and configure the same `ASTRABOX_CHANNEL_GATEWAY_TOKEN` on AstraBox and the
gateway. See the [configuration reference in the repository](https://github.com/Colton-z/AstraBox/blob/main/docs/configuration.md)
for the requirements.

## Run the connector hub {#run-the-connector-hub}

Assistant and Agent connectors (Gmail, Notion and the other work apps in
[Connect work apps](assistants.md#connectors)) run through a connector hub,
ContextForge, which the base installation does not start. From a clone, add
its Compose overlay with the hub's secrets, and the Google overlay with the
deployment's Google OAuth web client for Gmail, Google Calendar and Drive:

```bash
export CONTEXTFORGE_JWT_SECRET=$(openssl rand -hex 32)
export CONTEXTFORGE_AUTH_ENCRYPTION_SECRET=$(openssl rand -hex 32)
export CONTEXTFORGE_ADMIN_EMAIL=connectors@your-domain.example
export CONTEXTFORGE_ADMIN_PASSWORD='<a strong password>'
export ASTRABOX_CONTEXTFORGE_PUBLIC_URL=https://<your-astrabox-host>/connector-hub
export GOOGLE_OAUTH_CLIENT_ID=<client ID> GOOGLE_OAUTH_CLIENT_SECRET=<client secret>
scripts/compose.sh -f containers/compose.connectors.yaml \
  -f containers/compose.connectors-google.yaml up -d --build
```

The first overlay starts ContextForge, built from `containers/contextforge`
(the pinned release with the patches named there), and points the server at
it. The second starts one google_workspace_mcp instance each for Gmail, Google
Calendar and Google Drive; without it those three connectors are unavailable
and the others work.

- **The browser** reaches the hub only at `/connector-hub/oauth/callback` on
  the console's origin, where each vendor returns it after a consent; register
  that URL as the redirect URI of each vendor OAuth app. Vendors accept it only
  over HTTPS, or at `localhost`.
- **Sandboxes** never reach the hub. An agent calls a connector through the
  AstraBox server's platform MCP route, which relays the request to the hub as
  the person the conversation acts for, so neither the hub's address nor
  anyone's token is in a sandbox and the hub needs no sandbox network.
- **Rate limits** are off in the hub. ContextForge counts requests per
  client, and its only client is the AstraBox server, so its limits would
  apply to the whole deployment at once: by default 30 OAuth requests and 100
  tool calls a minute for everyone, then a 15-minute lockout after repeated
  overruns. A hub run without this overlay needs `RATE_LIMITING_ENABLED=false`
  as well.
- **The administrator email** must use a domain the hub accepts as an email
  address, not a reserved one such as `.local`; each AstraBox user's hub
  account is named on that domain.

The hub keeps people's vendor tokens in its own encrypted database (the
`contextforge-data` volume); back it up with the platform's data.

## The AstraBox database {#the-astrabox-database}

PostgreSQL is the supported default database, selected by
`ASTRABOX_PERSISTENCE_PROVIDER=postgres`. This built-in provider requires a
`postgresql://` `ASTRABOX_DB_URL`. The maintained Compose stack and all-in-one
container run PostgreSQL and build the URL from generated credentials;
other deployments using this provider set the URL to their own PostgreSQL
service. Every platform replica uses the same provider and database.

External providers register through `astrabox.providers.persistence` and
validate their own native URL. They must supply compatible SQLAlchemy engines,
native conflict operations, schema initialization, error classification and
physical coordination, including transaction-bound notifications, runtime locks
and listening with resynchronization. Registration or the reusable MySQL
conflict operations alone do not establish a complete backend. See
[Relational persistence providers](providers/relational-persistence.md).

With the built-in `postgres` provider, the server brings the database schema
to its release's version at startup, before it serves requests. Replicas that
start together take turns on a
PostgreSQL advisory lock, so each schema change runs once. A schema change that
fails stops the server and leaves the schema as it was. A database that a
newer release has already upgraded does not start an older release.

With this provider, each server process keeps up to 30 pooled connections for
repository queries and up to 30 more for runtime attachment and archive locks. The separate pools
let a lock holder run repository queries without waiting behind other lock
holders. It also uses one connection for PostgreSQL notifications and the
connections of the built-in scheduler. Size the database's connection limit
for every replica. Because the
server listens for notifications and caches prepared statements on its
connections, connect it to PostgreSQL directly or through a connection pooler
in session mode.

## Run multiple platform replicas

The [platform-replica deployment recipe](deploy-distributed.md) connects
multiple API hosts to shared services and signing keys. A request can reach
either API replica; Kubernetes schedules the sandbox independently. One replica
at a time holds each Session's connection to its Agent program. A replica that
accepts a message or an answer for the Session records it; with the default
`postgres` provider, a PostgreSQL notification tells the replica that holds the
connection. When that replica stops, another replica takes the Session over: within seconds after a clean
shutdown, or once the stopped replica's 30-second lease expires. Adding API replicas does not make local workspace disks shared
or turn a single OpenSandbox lifecycle service into a highly available service.

## Build sandbox images

An Environment connects an Agent to an Agent program and a compatible sandbox
image. The image supplies the operating system, CPU architecture, Agent
program, commands, and language runtimes available to the Agent.

Build a custom image when every Session needs additional software. Start from
the corresponding bundled sandbox image, install and pin the dependencies, and
then select the custom image in the Environment. Publish immutable image tags
for production deployments.

Agent programs, accounts, and control processes required by a sandbox must
already be present when the sandbox starts. Do not depend on a per-Session setup
script: a prewarmed sandbox can exist before the Session that uses it.

Bundled sandbox images expose `/workspace` as the Session workspace. The
Session's Files view reads and writes the same directory. A custom image must
make its configured working directory writable by the Agent program. See the
[container reference](container-reference.md) for the image requirements.

## Save and restore data

The maintained Compose deployment stores service data separately from Agent
sandboxes:

| Location | Contents |
|---|---|
| `astrabox-postgres` volume | AstraBox and LiteLLM databases |
| `astrabox-state` volume | `/data` state, generated keys, and local OpenSandbox metadata |
| `.astrabox/database-secrets` | Generated database credentials and optional bundled-SSO credentials, under the installation directory (`~/astrabox`) or the checkout |
| Optional workspace volume | Agent and Assistant workspace files, independently of any sandbox |

Session messages, the responses and output the Agent programs reported, and
native session state are stored in the AstraBox database. Workspace files
live on the workspace volume when configured, so terminating or replacing a sandbox
does not remove them; a replacement box mounts the same stored workspace.
Without a workspace volume, files depend on the sandbox or a retained
filesystem snapshot. OpenSandbox's ordinary pause snapshots preserve the root
filesystem, not process memory; they are not the durability mechanism for
volume-backed workspace files or database-backed native session state.

Back up the following as one recovery set:

- `astrabox-postgres`;
- `astrabox-state`;
- `.astrabox/database-secrets`;
- the local vault key or KMS key required to decrypt saved credentials;
- the workspace volume or its backing filesystem, when configured;
- external LiteLLM storage, when configured.

The default local Credential Vault key comes from
`ASTRABOX_VAULT_MASTER_KEY` or `/data/vault.key`. Encrypted credentials cannot
be restored without the same key.

## Make AstraBox available to a team

AstraBox supports OIDC, trusted identity headers from an authenticated gateway,
and JWT verification. An installation configures one of them through its
settings file; [Set up team login](team-login.md) lists the settings. The
bundled SSO overlay, which starts Casdoor for evaluation, runs from a clone:

```bash
scripts/compose.sh -f containers/compose.sso.yaml up -d
```

The bundled Casdoor applications use `/static/astrabox-mark.svg` for their
login logo. The overlay mounts the repository's existing brand asset into
Casdoor's static directory, so loading the logo requires no external CDN.
Casdoor seeds application data only at initialization; for an existing Casdoor
database, set the application's Logo field to that path in its application
settings. The AWS testbed reconciles the console application's Logo through
Casdoor's API as part of deployment.

At the team ingress, terminate TLS, add the public hostname to
`ASTRABOX_ALLOWED_HOSTS`, prevent direct access to the AstraBox service port,
and use the same login-cookie signing secret on every replica. See
[Set up team login](team-login.md) for the supported identity configurations.

## Publish AstraBox under a path {#publish-under-a-path}

A gateway that serves several applications on one host can publish AstraBox
below a path, such as `https://example.com/astrabox/`. Set that path on the
AstraBox server, for an installation in `~/astrabox/containers/.env`, then run
the installer again:

```bash
printf '%s\n' "ASTRABOX_PUBLIC_PATH_PREFIX='/astrabox'" >> ~/astrabox/containers/.env
```

The value starts with a slash and has no trailing slash, and its segments use
letters, digits, `.`, `_`, `~` and `-`. Leave it empty to publish AstraBox at
the root of its host. The server refuses to start with any other value and
names it.

AstraBox routes each request by the part of its path below the prefix, so the
gateway removes the prefix before the request reaches AstraBox. Do it in one
of two ways:

| Gateway | How it removes the prefix |
|---|---|
| A reverse proxy | It strips the prefix before forwarding, as in the examples below. |
| A Python application that runs AstraBox in its own process | It mounts AstraBox at the prefix; see [Mount AstraBox under a path](embedding.md#mount-astrabox-under-a-path). |

With nginx, the URI part of `proxy_pass` replaces the matched prefix:

```nginx
location /astrabox/ {
    proxy_pass http://127.0.0.1:8088/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    # Session events and terminal output arrive as long-lived streams.
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

With Caddy, `handle_path` strips the prefix:

```caddyfile
example.com {
    redir /astrabox /astrabox/ 308
    handle_path /astrabox/* {
        reverse_proxy 127.0.0.1:8088 {
            flush_interval -1
        }
    }
}
```

AstraBox refuses a request that arrives any other way, and the error names the
setting:

| Error | Cause |
|---|---|
| `PUBLIC_PATH_PREFIX_NOT_REMOVED` (404) | The request still carries the prefix: the proxy forwards it instead of removing it. The console then reports that it cannot load. With nginx, `proxy_pass` needs the trailing `/`. |
| `PUBLIC_PATH_PREFIX_MISMATCH` (409) | A Python application mounted AstraBox at a path other than `ASTRABOX_PUBLIC_PATH_PREFIX`. |

Health and readiness probes, the installer, and sandboxes calling back to the
server reach AstraBox directly, at `/healthz`, `/readyz` and the sandbox callback
address, not through the gateway.

Below the prefix, AstraBox serves the console's pages and files, its API, and
its event streams. It also composes the addresses it gives out below the prefix:
the sign-in callback, share links, file downloads, the MCP server URL the console
shows, channel callback URLs, and the address a person returns to after
connecting a work app. With team login, register
`https://example.com/astrabox/api/v1/auth/callback` as the console's callback URL
with the identity provider; see [Set up team login](team-login.md). The login
cookie is scoped to the prefix.

Set the prefix before you create messaging-platform Deployments and connectors.
A channel Deployment keeps the callback URL it was created with, and a connector
keeps the address it was set up with. After changing the prefix, create the
channel Deployments again. Update `ASTRABOX_CONTEXTFORGE_PUBLIC_URL` and each
vendor OAuth app's redirect URI, then set the connectors up again. With team
login, also update the identity provider's redirect URL and
`ASTRABOX_OIDC_REDIRECT_URL`, and set a new `ASTRABOX_AUTH_SESSION_SECRET`. A
login cookie issued for the old path would otherwise keep a browser signed in
after it signs out.

LiteLLM's management pages load their files from the root of the host, so
AstraBox cannot serve them below a prefix. Under **Integrated services**, the
bundled gateway's console is not listed. On an Agent's page, **Manage remote MCP
servers** and **Manage Skills** report that these pages cannot be opened below
the prefix (`PUBLIC_PATH_PREFIX_UNSUPPORTED`). An Agent's MCP servers and Skills
remain editable on its page. Open the gateway's own console at its address.

A path separates addresses, not trust. Every application on the host shares one
browser origin, so a script of another application can call AstraBox with the
cookies of a person signed in to it. Give AstraBox its own hostname when the
other applications on that host are not trusted.

## Size sandboxes and cap what they use {#size-sandboxes-and-cap-what-they-use}

A conversation's own sandbox and an Agent's shared sandbox each have a size:

| Sandbox | Limits | Requests | Set with |
|---|---|---|---|
| A conversation's own sandbox | 1 CPU, 1 GiB | 250m CPU, 1 GiB | `ASTRABOX_SANDBOX_CPU_LIMIT`, `ASTRABOX_SANDBOX_MEMORY_LIMIT`, `ASTRABOX_SANDBOX_CPU_REQUEST`, `ASTRABOX_SANDBOX_MEMORY_REQUEST` |
| An Agent's shared sandbox | 4 CPU, 4 GiB | 1 CPU, 4 GiB | The same four variables with the prefix `ASTRABOX_SHARED_SANDBOX_` |

A limit is the most a sandbox may use. A request is what the Kubernetes
scheduler reserves for it on a node; Docker ignores requests. Memory requests
equal the limits, so a node never promises memory it does not have. CPU
requests are lower, because a sandbox that finds the CPU busy only runs slower.
A sandbox takes the size in effect when it is created and keeps it until it is
replaced.

A shared sandbox takes another conversation while its free memory, measured
from what the sandbox uses rather than from its limit, leaves 640 MiB for it.
One Claude Code conversation grows by about 570 MiB from its start, so a 4 GiB
shared sandbox holds about five. A larger shared sandbox holds more
conversations for its size, because the runtime underneath is paid for once:
an 8 GiB sandbox holds about twelve.

Each sandbox runs beside an egress sidecar limited to 1 CPU and 128 MiB. On a
Docker host the bundled sandbox server sets that limit. On Kubernetes,
OpenSandbox 0.2.3 creates the sidecar without resources, so give the sandbox
namespace a LimitRange whose container default is the same:

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: astrabox-sandbox-sidecars
spec:
  limits:
    - type: Container
      default: {cpu: "1", memory: 128Mi}
      defaultRequest: {cpu: 50m, memory: 128Mi}
```

The default applies to every container in the namespace that states no
resources of its own, OpenSandbox's execd init container included.

### Cap what a sandbox backend runs {#cap-what-a-sandbox-backend-runs}

A sandbox quota caps what one sandbox backend runs for the deployment, across
all platform replicas:

| Variable | Caps |
|---|---|
| `ASTRABOX_SANDBOX_QUOTA_BOXES` | The number of sandboxes |
| `ASTRABOX_SANDBOX_QUOTA_MEMORY` | The sum of their memory limits and their sidecars', for example `24Gi` |
| `ASTRABOX_SANDBOX_QUOTA_CPU` | The sum of their CPU limits and their sidecars' |
| `ASTRABOX_SANDBOX_QUOTA_CONCURRENT_CREATES` | The number of sandboxes being created at once |

Every sandbox the backend runs for the deployment counts: those conversations
use, shared sandboxes, prepared sandboxes, sandboxes being created and
sandboxes whose removal the backend has not confirmed. A paused sandbox counts
on a Docker host, where it keeps its memory, and not on Kubernetes, where
pausing it releases its Pod; resuming it takes its room back. A sandbox's
charge is fixed when it is created, so a size changed later does not change
what a running sandbox counts.

A sandbox that would take the backend past a ceiling is not created: the
conversation that needed it ends at once, and its error names the ceiling, what
is in use and the limit, for example `sandbox backend 'opensandbox' has no room
for another box: boxes 12 of 12 in use, and this box needs 1`. Nothing waits for
room.
A prepared sandbox refused this way is tried again later, at a growing
interval. Conversations that already have a sandbox are not affected.

Leave a variable unset for no ceiling, except the memory ceiling of the bundled
sandbox server on a Docker host: there it is the host's memory less 4 GiB for
the platform, its stores and the host. On a 16 GiB host that is 12 GiB, about
ten conversations with their own sandboxes. On a host of 8 GiB or less the
ceiling is 4 GiB or less, too little for one shared sandbox of the default size
(4 GiB and its sidecar), so an Agent or Assistant whose Environment shares a
sandbox cannot start: lower `ASTRABOX_SHARED_SANDBOX_MEMORY_LIMIT` and its request, or raise
`ASTRABOX_SANDBOX_QUOTA_MEMORY`. Set a variable to `unlimited` to remove the
ceiling. Startup logs each ceiling and where it came from, and
refuses a ceiling on a backend that cannot list its sandboxes, since what it
runs could not be counted. The **Sandboxes** page of the console shows each
ceiling and what is in use.

## Plan capacity for prepared sandboxes {#plan-capacity-for-prepared-sandboxes}

Every Agent keeps as many idle prepared runtimes as its
[prepared capacity](authoring-agents.md#prewarming), so that that many new
conversations can start in under a second at once instead of starting a
sandbox. New Agents get a capacity of 1 wherever
`ASTRABOX_AGENT_PREWARM_REDIS_URL` is set, which every bundled Compose
deployment and the [all-in-one container](all-in-one.md) do; there, the two
Agents a fresh installation creates get it as well. The runtimes are held for
as long as the capacity is above 0 and the Agent and its Environment are
enabled; after a conversation takes an idle sandbox, AstraBox prepares a
replacement, up to eight at a time per Agent. Size the deployment for the sum
of the Agents' `prepared_capacity`: for example, an Agent serving 1,000 users
that expects 100 conversations to start together needs a capacity of 100, and
with a sandbox per conversation that is 100 idle sandboxes. Prepared sandboxes
count against the [sandbox quota](#cap-what-a-sandbox-backend-runs). When an Agent's
conversations share a sandbox, each prepared sandbox carries as many seats as
its memory limit admits with 640 MiB set aside for each, and the Agent keeps
the capacity divided by that number of idle sandboxes, rounded up. A seat that no
conversation takes within the sandbox lease (`ASTRABOX_SANDBOX_LEASE_SECONDS`)
is discarded, like an idle sandbox. Set `ASTRABOX_PREPARED_CAPACITY_LIMIT`
to the sum the deployment can hold, and AstraBox refuses an Agent write that
would exceed it. Without it, one Agent's prepared sandboxes can take the whole
sandbox quota and leave none for conversations. Set the capacity of Agents that
do not need a fast first conversation to 0.

What an idle prepared sandbox uses depends on the engine that the Agent's
Environment runs:

| Engine | Idle memory of one prepared sandbox |
|---|---|
| Claude Code | About 0.5 GiB: 490–536 MiB for the sandbox container on a Docker host |
| pi | About 0.45 GiB: 450 MiB for the sandbox container on a Docker host |
| DeepSeek Harness | 921–940 MiB of anonymous memory per sandbox Pod on Kubernetes |
| Other engines, including Codex | 390–650 MiB of anonymous memory per sandbox Pod on Kubernetes |

On a Docker host each sandbox's egress sidecar adds about 60 MiB, and an idle
sandbox uses 2–5% of one CPU.

| Where sandboxes run | How that capacity is accounted for |
|---|---|
| One Docker host | Docker reserves nothing for an idle container, so the host's memory holds the figures above plus the sidecar. The sandbox quota still counts each prepared sandbox at its full size. |
| Kubernetes with OpenSandbox | The scheduler reserves each sandbox's requests on a node: 250m CPU and 1 GiB for a conversation's own sandbox, 1 CPU and 4 GiB for a shared one, plus the sidecar's request from the namespace's LimitRange. |
| An existing OpenSandbox service | The same requests. Whether they are reserved depends on that service's runtime, as in the two rows above. |

The figures above are what idle capacity takes; while it works, a sandbox may
grow to its [limits](#size-sandboxes-and-cap-what-they-use). To turn
prewarming off for an Agent, set **Prepared capacity** to 0 in
its **Runtime and availability** settings, or set `"prepared_capacity": 0`
through the API or in an `astrabox.yaml` document.

With several platform replicas, one replica at a time replenishes each Agent's
prepared sandboxes and any replica can hand one to a conversation. A replica
restarted on the same host resumes replenishing at once; when a replica stops
for good, another takes over replenishment within about two and a half minutes,
while conversations keep taking the sandboxes already prepared.

## Check health and sandbox recovery

AstraBox exposes separate liveness and readiness endpoints:

```text
GET /healthz
GET /readyz
```

`/readyz` returns `503` after shutdown draining begins. Set the platform's
termination grace period higher than `ASTRABOX_SHUTDOWN_DRAIN_SECONDS` so
requests in flight on that replica have time to finish. Agent programs keep
running in their sandboxes while a replica stops, and another replica takes
over their Sessions.

CPU, memory, disk, and sandbox timeouts come from the selected OpenSandbox
runtime and its configuration. Before relying on paused sandboxes, verify every
Environment that uses them against the deployed backend:

```bash
astrabox verify-opensandbox-snapshots
```

## Connect models, messaging platforms, and credentials

- [Connect a model service](models.md).
- [Connect an Agent to a messaging platform](channels.md).
- [Protect credentials used by Agents](egress-credential-injection.md).

## Production checklist

Before inviting users:

- enable authentication and verify authorization for user and machine access;
- terminate TLS at a trusted ingress and set `ASTRABOX_ALLOWED_HOSTS`;
- use shared persistence and the same signing and encryption keys on every
  replica;
- back up database data, state, credentials, and encryption keys together;
- verify sandbox access to the model gateway, AstraBox callbacks, remote MCP
  servers, and other required destinations;
- verify snapshot recovery for every Environment that pauses sandboxes;
- set the required outbound network rules and sandbox permissions;
- run a real Agent task while testing graceful shutdown;
- collect logs, metrics, and traces with explicit access and retention rules.

## Related guides

- [Environments](environments.md)
- [OpenSandbox](providers/opensandbox.md)
- [Team login](team-login.md)
- [Container reference](container-reference.md)
