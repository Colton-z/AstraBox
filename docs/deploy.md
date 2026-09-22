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

The maintained local deployment has no authentication and listens on loopback
only. Configure [team login](team-login.md), TLS, and a trusted ingress before
making AstraBox reachable from another network.

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

Treat the server container as a privileged host service: it controls the
mounted Docker daemon.

### Install a release {#install-a-release}

```bash
curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh | bash
```

The installer:

1. checks Docker, the Compose plugin v2 or later, and that your user can use
   `/var/run/docker.sock`;
2. downloads the deployment bundle of the latest release, verifies its
   SHA-256, and unpacks the Compose files into `~/astrabox`;
3. generates the database and login secrets in
   `~/astrabox/.astrabox/database-secrets`, once, and keeps them afterwards;
4. asks which model service your Agents use, and for its API key and model ID,
   and writes their settings;
5. pulls the images published for that release, starts the stack, waits until
   the console page is served, and pulls the sandbox image of the seeded Claude
   Code Agent so that the first Session does not wait for it.

Re-run it to upgrade: it installs the latest release's Compose files, or those
of the release `ASTRABOX_VERSION` names, sets that release as the image version,
and keeps the volumes, the generated secrets, the model settings, and every
other line of the installation's settings file. To change the model service,
answer its question again, or run it with `ASTRABOX_INSTALL_MODEL_PROVIDER`
set; the settings it wrote for the previous service are removed.

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

`ANTHROPIC_MODEL` is the deployment's default model: the seeded Agents use it
until an Agent selects its own, and an Agent that selects a model enters the
name in the last column. With `ANTHROPIC_BASE_URL` set, the deployment serves
the default model through the gateway's `anthropic/` routes. For DeepSeek it
also recognises the endpoint and gives the same credential to the gateway's
OpenAI-wire DeepSeek routes, which the other Agent programs use.

The `openai-compatible/*` route sends `<model ID>` to
`OPENAI_COMPATIBLE_BASE_URL` over Chat Completions. Claude Code, the Agent
program a fresh installation seeds, speaks Anthropic Messages, so LiteLLM
translates between the two protocols; prefer a service's Anthropic-compatible
endpoint when it publishes one.

### Installer settings {#installer-settings}

Setting `ASTRABOX_INSTALL_MODEL_PROVIDER` runs the installer without questions,
which is what an unattended installation needs.

| Variable | Purpose |
|---|---|
| `ASTRABOX_VERSION` | Release to install. Default: the latest release. |
| `ASTRABOX_INSTALL_DIR` | Installation directory. Default: `~/astrabox`. |
| `ASTRABOX_INSTALL_BUNDLE` | Path of an `astrabox-deploy-<version>.tar.gz` downloaded elsewhere, for a host that cannot reach GitHub. |
| `ASTRABOX_IMAGE_PREFIX` | Repository prefix of a registry mirror holding the published images; the component name is appended. Default: `ghcr.io/colton-z/astrabox-`. |
| `ASTRABOX_INSTALL_MODEL_PROVIDER` | `anthropic`, `deepseek`, `anthropic-compatible`, `openai-compatible` or `none`. |
| `ASTRABOX_INSTALL_MODEL_API_KEY` | The model service's API key. |
| `ASTRABOX_INSTALL_MODEL_NAME` | The model ID the seeded Agents use. Default for DeepSeek: `deepseek-flash`. |
| `ASTRABOX_INSTALL_MODEL_BASE_URL` | Base URL of an Anthropic-compatible or OpenAI-compatible service. |

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
`docker compose down`.

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
occupy separate directories on that backing filesystem. The platform's
mergerfs router creates fixed views outside user sandboxes; OpenSandbox mounts those views
through its standard PVC or named-volume interface. Binding to the selected
new or existing workspace completes before delivery to the user. The storage
adapter selects the backing medium; it cannot opt out of platform routing.

An unset or empty value leaves workspace files on the sandbox's temporary
filesystem; replacing that sandbox can discard those files. This setting governs
workspace files, not the platform database's custody of native SessionStores.
In this no-volume mode no mount helper runs, whatever
`ASTRABOX_WORKSPACE_MOUNTER_IMAGE` names.

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
Docker's view volume uses a recursive bind so its numbered FUSE submounts are
present in the consumer. A nonrecursive bind would expose the empty underlying
host directories instead; the sandbox mount check must reject that state.

### What the medium has to support

For AWS EFS, use the [`aws_efs` storage provider](providers/aws-efs.md). It
verifies the configured EFS CSI claim; the platform router is unchanged.

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

## Run multiple platform replicas

The [platform-replica deployment recipe](deploy-distributed.md) connects
multiple API hosts to shared services and signing keys. A request can reach
either API replica; Kubernetes schedules the sandbox independently. Adding
API replicas does not make local workspace disks shared or turn a single
OpenSandbox lifecycle service into a highly available service.

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

Session messages and native session state are stored in the AstraBox database. Workspace files
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

## Check health and sandbox recovery

AstraBox exposes separate liveness and readiness endpoints:

```text
GET /healthz
GET /readyz
```

`/readyz` returns `503` after shutdown draining begins. Set the platform's
termination grace period higher than `ASTRABOX_SHUTDOWN_DRAIN_SECONDS` so
in-flight Agent work has time to finish.

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
