# OpenSandbox

OpenSandbox is AstraBox's built-in sandbox service. An
[Environment](../environments.md) tells AstraBox which sandbox image, network
access, and lifecycle options an Agent uses. When a Session starts, AstraBox
creates or claims an OpenSandbox sandbox, runs the Agent program in
`/workspace`, and keeps the conversation and other product records outside the
sandbox.

OpenSandbox provides the sandbox lifecycle, command execution, file access, and
service endpoints. Because the Agent runs in deployment-owned infrastructure,
it can continue working after the developer's computer disconnects. Users can
return through the console, API, or a configured integration.

## Choose a deployment

| Deployment | Best for | Where sandboxes run |
|---|---|---|
| Bundled Docker | Evaluation, development, and one trusted host | Separate containers on the AstraBox host |
| Kubernetes | Multiple nodes, prewarmed capacity, snapshots, and cluster controls | Pods managed by the OpenSandbox controller |
| Existing OpenSandbox service | Organizations that operate sandbox infrastructure separately | The Docker or Kubernetes runtime configured for that service |

The maintained Compose deployment uses bundled Docker. See
[Deploy AstraBox](../deploy.md) for the single-host setup and the data that must
be backed up.

OpenSandbox also maintains its own
[server](https://open-sandbox.ai/components/server) and
[Kubernetes](https://open-sandbox.ai/kubernetes/deployment) deployment guides.
AstraBox uses the OpenSandbox Lifecycle API and does not replace the controller,
container runtime, or cluster network described there.

## Run sandboxes on Kubernetes

Install the OpenSandbox controller and CRDs, then create the namespace where
sandbox Pods will run. AstraBox checks that the namespace, selected workload
CRD, Kubernetes API access, and required permissions are available; it does not
create cluster-scoped resources or namespaces.

The maintained Compose overlay runs AstraBox outside the cluster and creates
sandbox Pods inside it. Copy a kubeconfig to a location readable by the
container, then provide addresses that are valid from both networks:

```bash
sudo install -o 999 -g 999 -m 600 "$HOME/.kube/config" \
  "$HOME/astrabox-kubeconfig"

export ASTRABOX_KUBECONFIG_HOST_PATH="$HOME/astrabox-kubeconfig"
export ASTRABOX_SANDBOX_SERVER_KUBE_API_SERVER=https://10.0.1.7:6443
export ASTRABOX_SERVER_BIND_IP=10.0.1.7
export ASTRABOX_MCP_PROXY_BASE_URL=http://10.0.1.7:8088
export ASTRABOX_ALLOWED_HOSTS=10.0.1.7
export ASTRABOX_LITELLM_BASE_URL=https://llm.example.com

scripts/compose.sh -f containers/compose.kubernetes.yaml up -d
```

The addresses serve different paths:

- AstraBox must reach the Kubernetes API address, and its TLS certificate must
  cover that address.
- Sandbox Pods must reach the AstraBox callback address and the model gateway.
- Browsers must use a hostname included in `ASTRABOX_ALLOWED_HOSTS`.

The tested workload type is `batchsandbox`, backed by
`batchsandboxes.sandbox.opensandbox.io`. An installation that uses the Agent
Sandbox CRD can set
`ASTRABOX_SANDBOX_SERVER_KUBE_WORKLOAD_PROVIDER=agent-sandbox` instead.

Publish every selected sandbox image to a registry available to the cluster.
Use immutable image tags or digests so new and prewarmed Pods start with the
same software. Clusters that add nodes on demand may also need a longer
`ASTRABOX_SANDBOX_SERVER_KUBE_CREATE_TIMEOUT_SECONDS` for node startup and image
pulls.

## Connect an existing OpenSandbox service

Set the Lifecycle API address and its named API key in the AstraBox server
process environment:

```bash
ASTRABOX_SANDBOX_OPENAPI_BASE_URL=https://sandbox-control.example.com
ASTRABOX_SANDBOX_API_KEY_SECRET_NAME=opensandbox-api-key
OPENSANDBOX_API_KEY=REPLACE_WITH_LIFECYCLE_API_KEY
```

The secret provider reads process environment, not the product's encrypted
Vault. It uppercases the reference name and replaces hyphens with underscores.
For a container deployment, explicitly inject these settings into the server
container; exporting them on the host alone does not add them to the bundled
Compose files. The [platform-replica recipe](../deploy-distributed.md) includes
that environment-file configuration for an external Kubernetes-backed service.

AstraBox must be able to reach both the Lifecycle API and the sandbox endpoints
returned by that API. Require OpenSandbox API-key authentication whenever the
service is exposed beyond a trusted loopback network. The bundled lifecycle
service listens only inside the AstraBox container and does not need a second
public endpoint.

## Prepare sandbox images

Each sandbox image contains the Agent program, system packages, accounts, and
background services needed by every Session that uses it. Bundled images use
`/workspace` as the Session workspace.

Add common software to a custom image based on the corresponding bundled image,
then select that image under **Sandbox image or template** in
**Management console → Environments**. Do not depend on a per-Session setup
script: a prewarmed sandbox can exist before the Session that claims it.

AstraBox uses OpenSandbox-native command, filesystem, and endpoint APIs. The
bundled images require AIO because their boot and workload-account lifecycle
ends in `/opt/gem/run.sh`; a custom base image needs an equivalent lifecycle
and a matching entrypoint.

The [Container Reference](../container-reference.md) describes the image and
workspace layout. Every AstraBox-created sandbox carries the platform's measured
box envelope: limits of `4` CPU and `4Gi` memory, and requests of `200m` CPU and
`768Mi` memory. This is one platform recipe for both cold and prewarmed creation,
not a per-Session Environment setting. Disk, GPU access, and cluster admission
remain OpenSandbox deployment concerns.

## Use prewarmed capacity

Enable prewarming on an Agent to keep its complete runtime ready before a user
starts a conversation. Both tenancy modes use OpenSandbox's official SDK client
pool for creation, coordination, replenishment, retries, and atomic acquisition.
With Agent tenancy, a claimed box becomes the Agent's shared placement and
keeps its root, with isolated directories for each conversation. With
conversation tenancy, the claiming Session owns the whole box; if persistent
storage is configured, the platform binds its workspace view to the new or
existing Session directory before activating the prepared runtime. Neither
mode changes a running Session's workspace. Multi-process or multi-machine deployments use
`ASTRABOX_AGENT_PREWARM_REDIS_URL` for the SDK client pool's shared state.

Both modes prepare the engine before reporting available capacity, and claiming
activates that prepared runtime. For conversation tenancy, preparation runs in
the SDK's pre-publication callback; its transient receipt stays in the box's
private home, outside Workspace. Shared tenancy prepares an isolated engine
slot in its resident box. Neither receipt is the durable SessionStore.

Changing a conversation-tenancy Agent's startup configuration replaces its
unclaimed pool inventory. Already claimed boxes remain with their Sessions.
For shared tenancy, injectable configuration changes replace the waiting engine
slot without replacing the resident box.

The platform chooses the Agent version, persistent workspace mount,
credentials, and startup requirements. OpenSandbox supplies sandbox lifecycle and
pool machinery; it does not participate in AstraBox's user/session workflow.

An Environment can give each conversation a separate sandbox or let one Agent's
conversations share a sandbox. Shared-sandbox mode still gives each conversation
its own Linux user and workspace, but the conversations share a container and
network namespace. It therefore requires the advanced sandbox permission level
and an OpenSandbox deployment configured for isolated sessions.

Every bundled Agent program runs each conversation under its own account in
shared-sandbox mode. AstraBox refuses a claimed sandbox that cannot attest
isolated-session support; it does not fall back to unisolated execution.

## Pause and resume sandboxes

An Environment can pause an idle sandbox instead of terminating it when the
OpenSandbox deployment supports snapshots. Pause commits the sandbox root
filesystem to an OCI image and releases its compute. Resume keeps the sandbox
ID and restores the files, but starts new processes; ordinary process memory is
not restored.

The supported Kubernetes path requires:

- the OpenSandbox snapshot controller and image committer;
- access to the containerd socket used by sandbox Pods;
- an OCI registry reachable for snapshot push and pull; and
- registry credentials in the sandbox namespace when the registry is private.

Verify the complete write, pause, resume, and read path before enabling pause
for production Environments:

```bash
astrabox verify-opensandbox-snapshots
```

Apply a retention policy to the snapshot registry. Deleting OpenSandbox
snapshot metadata does not by itself garbage-collect the OCI image data. See
OpenSandbox's
[Pause and Resume](https://open-sandbox.ai/guides/pause-resume) guide for the
controller and registry requirements.

Without a workspace volume, files remain available only while the same sandbox
or a verified filesystem snapshot is retained. With a volume, workspace files
remain on the backing filesystem independently of the sandbox. Native session
state is stored in AstraBox's database in either mode. Assistant hibernation
saves that state and releases its sandbox; it is not OpenSandbox pause. See
[Assistants](../assistants.md) and [workspace storage](../deploy.md#where-conversation-workspaces-live).

## Configure networking and credential protection {#credential-protection}

Use **Management console → Environments → Network access** to allow every
outbound destination or only the hosts an Agent needs. AstraBox combines this
choice with the model endpoint, platform callback, declared Plugin sources,
allowed remote MCP servers, and destinations authorized by assigned
Credentials. See [IP Addresses](../networking.md) for firewall and stable-egress
configuration.

The maintained deployment keeps real model, remote MCP, and external API
credentials outside the sandbox by default:

```bash
ASTRABOX_SANDBOX_CREDENTIAL_VAULT=true
ASTRABOX_SANDBOX_EGRESS_MODE=dns+nft
```

The OpenSandbox outbound proxy gives the Agent program opaque placeholders and
adds real values only to matching outbound requests. Every deployment used for
prepared capacity uses the same standard OpenSandbox create request as cold
capacity: AstraBox supplies the effective network policy and enables the proxy,
then OpenSandbox provisions it and returns per-sandbox endpoint authentication
to the SDK. AstraBox does not configure a deployment-wide egress token. See
[Protect credentials used by Agents](../egress-credential-injection.md) for
the supported request matches and verification flow.

For HTTPS services that accept HTTP Basic, including private Git repositories,
create an `http_basic` Credential in an AstraBox Vault and assign the Vault to
the Agent. Supply the clean repository URL, username, and write-only password
or token. The provider translates that intent into OpenSandbox's native
`auth.type="basic"` binding; no Git credential helper, token-bearing clone URL,
or secret environment variable is installed in the sandbox. The binding is
available before Skill and Plugin downloads on cold and prepared startup paths.

The binding matches HTTPS on port 443, the exact configured path and its
descendants, and `GET`, `HEAD`, or `POST`. Credentials are not injected into
other repository paths. This is an authentication method, not a Git-specific
engine capability. See the [Vault configuration example](../credentials.md#2-add-a-credential)
and OpenSandbox's [native Git credential guide](https://github.com/opensandbox-group/OpenSandbox/blob/2f03f68c25644a68fee31d0759915de0b104b4ca/docs/guides/credential-vault.md#git-and-curl-with-vault-injected-credentials).

## Select a sandbox runtime

`ASTRABOX_SANDBOX_SECURE_RUNTIME` selects one container runtime for the
deployment. Leave it empty for runc, or use a runtime already installed on the
host or cluster:

| Runtime | OpenSandbox support in AstraBox |
|---|---|
| runc | General use, limited networking, and protected credential delivery |
| gVisor | Additional syscall isolation; OpenSandbox networking and Credential Vault are unavailable |
| Kata | VM-backed isolation with networking and Credential Vault support |
| Firecracker | Kubernetes only, with a matching Kata/Firecracker `RuntimeClass` |

OpenSandbox's outbound proxy requires the `iptables` NAT table, which gVisor
does not provide. A sandbox that combines gVisor with OpenSandbox network rules
is rejected instead of starting without those rules. Use separate deployments
when Agents need different runtime policies. OpenSandbox's
[Secure Container Runtime](https://open-sandbox.ai/guides/secure-container)
guide contains the current installation requirements.

## Publish sandbox services safely

AstraBox can connect directly to an endpoint returned by OpenSandbox or ask the
Lifecycle API to relay HTTP, SSE, and WebSocket traffic. The maintained Docker
deployment uses the relay because AstraBox runs inside a container while the
published ports belong to the host.

Kubernetes deployments can use direct Pod routing or the OpenSandbox ingress
component. Before exposing a sandbox service to an untrusted network, enable
OpenSandbox Secure Access and configure matching signing keys for AstraBox and
the ingress component. AstraBox checks Session access before issuing a
short-lived signed URL.

## Inspect a deployment

Open **Management console → Sandboxes** to see the state, image, expiration,
network rules, Credentials, and diagnostics reported for each sandbox.

| Symptom | Check |
|---|---|
| Pod remains pending | Controller and CRD status, sandbox namespace, node capacity, image reference, and registry access |
| Kubernetes API reports TLS errors | Route to the configured API address and the names covered by its certificate |
| Session waits after the sandbox is ready | Route from AstraBox to the Agent service, including direct versus relayed endpoints |
| Agent prepared capacity remains unavailable | `prepared-runtime` state, image, entrypoint, permissions, and Redis connectivity |
| A resumed sandbox is missing files | Snapshot controller, containerd socket, registry credentials, and the snapshot verifier |

## Related documents

- [Deploy AstraBox](../deploy.md)
- [Environments](../environments.md)
- [Container Reference](../container-reference.md)
- [Protect credentials used by Agents](../egress-credential-injection.md)
