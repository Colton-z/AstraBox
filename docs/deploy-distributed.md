# Platform replicas with shared services

`containers/compose.distributed.yaml` adds a platform replica to an existing
Kubernetes-backed deployment. Each host runs an AstraBox API process. The
replicas use the same PostgreSQL database, OpenSandbox client-pool Redis,
OpenSandbox Lifecycle API, LiteLLM and channel gateway. Kubernetes schedules
the sandboxes; AstraBox does not select a machine for each user request.

This overlay assembles application connections. It does not provision those
services, migrate an existing deployment, install a load balancer, or make a
single service highly available.

Use it for a fresh deployment with explicit shared signing keys, or to add
replicas to a deployment that already uses those same explicit keys. Converting
automatically derived callback keys requires a maintenance and credential
rotation plan; it is not a seamless configuration-only migration.

## Prepare the shared services

- Use the same AstraBox database, including SessionStore and encrypted Vault
  records. Both hosts need private network access to PostgreSQL and Redis;
  a port bound to the first host's loopback is not reachable from the second.
  Restrict access to the participating hosts, not the public Internet.
- Use one authenticated external OpenSandbox service connected to the same
  Kubernetes cluster and namespace. Both API hosts must reach the lifecycle
  endpoint and the sandbox endpoints it returns.
- Carry the existing workspace settings into the environment file, because the
  overlay replaces the base Compose environment. Persistent workspaces use
  `ASTRABOX_STORAGE_PROVIDER`, `ASTRABOX_SANDBOX_WORKSPACE_VOLUME` and
  `ASTRABOX_WORKSPACE_MOUNTER_IMAGE`. For AWS EFS, configure
  `ASTRABOX_STORAGE_PROVIDER=aws_efs` and the same `ASTRABOX_EFS_FILE_SYSTEM_ID`,
  and install the official CSI driver. The overlay sets
  `ASTRABOX_WORKSPACE_STORAGE_TOPOLOGY=shared`, so the backing claim must be a
  shared filesystem that every eligible sandbox node can mount. See
  [workspace storage](deploy.md#where-conversation-workspaces-live) and
  [AWS EFS workspace storage](providers/aws-efs.md). Persistent workspaces are
  optional.
- Give both API hosts a kubeconfig readable by container uid 999; the overlay
  requires it. The platform workspace-mount controller and EFS provider use
  Kubernetes even when the lifecycle service is external. Use the same cluster
  and namespace as OpenSandbox. Grant the kubeconfig identity the helper
  permissions listed in
  [workspace storage](deploy.md#where-conversation-workspaces-live); the EFS
  provider also reads the backing claim and its PersistentVolume.
- Share one HTTPS LiteLLM service and its existing inference/administration
  credentials. Share one authenticated HTTPS channel gateway and its token;
  separate embedded gateways are not a shared messaging runtime.
- Put the replicas behind an operator-managed HTTP/WebSocket/SSE ingress.
  Use the same public origin, identity configuration and Pod-reachable callback
  URL on every replica. Include the public, callback and private health-check
  hostnames in `ASTRABOX_ALLOWED_HOSTS`. Drain a replica before removing it from
  service; set the termination grace period above
  `ASTRABOX_SHUTDOWN_DRAIN_SECONDS`.

### OpenSandbox version boundary

With OpenSandbox Server 0.2.3, use a single external lifecycle service with its
own persistent SQLite store. This permits multiple AstraBox replicas and
sandbox nodes; it does **not** provide lifecycle-control-plane high availability.
Do not run independent SQLite-backed lifecycle replicas and assume that sharing
Kubernetes also shares snapshot records. The
[0.2.3 configuration reference](https://github.com/opensandbox-group/OpenSandbox/blob/c39b814/server/configuration.md#store)
supports only SQLite for server-managed metadata. A different upstream version
and its documented shared-store guarantees require separate acceptance.

The bundled lifecycle process listens on container loopback. Publishing a host
port alone does not expose it. Operate the external service using OpenSandbox's
documented server configuration, including API-key authentication. When moving
an existing lifecycle service, preserve its persistent metadata and stop the old
writer before handing the store to the new service; do not copy a live SQLite
database and run both writers.

## Configure each replica

Create a protected, absolute-path environment file on each host, for example
`/etc/astrabox/replica.env`, readable only by its operator. Use it for both
Compose interpolation and container environment injection. Do not commit it or
publish the rendered `docker compose config` output: both contain credentials.

The shared portion contains these existing application settings:

```dotenv
ASTRABOX_DB_URL=postgresql+asyncpg://astrabox:URL_ENCODED_PASSWORD@db.internal:5432/astrabox
ASTRABOX_AGENT_PREWARM_REDIS_URL=redis://redis.internal:6379/0
ASTRABOX_SANDBOX_OPENAPI_BASE_URL=https://sandbox-control.example.com
ASTRABOX_SANDBOX_API_KEY_SECRET_NAME=opensandbox-api-key
OPENSANDBOX_API_KEY=REPLACE_WITH_SHARED_LIFECYCLE_API_KEY
ASTRABOX_SANDBOX_SERVER_KUBE_API_SERVER=https://kubernetes.internal:6443
ASTRABOX_SANDBOX_SERVER_KUBE_NAMESPACE=opensandbox
ASTRABOX_MCP_PROXY_BASE_URL=https://callbacks.example.com
ASTRABOX_ALLOWED_HOSTS=astrabox.example.com,callbacks.example.com,api-a.internal,api-b.internal
ASTRABOX_LITELLM_BASE_URL=https://llm.example.com
ASTRABOX_CHANNEL_GATEWAY_BASE_URL=https://channels.example.com
ASTRABOX_CHANNEL_GATEWAY_TOKEN=REPLACE_WITH_EXISTING_SHARED_GATEWAY_TOKEN
ASTRABOX_VAULT_MASTER_KEY=REPLACE_WITH_EXISTING_SHARED_VAULT_KEY
ASTRABOX_AUTH_SESSION_SECRET=REPLACE_WITH_EXISTING_SHARED_SESSION_KEY
ASTRABOX_TRANSCRIPT_SIGNING_KEY=REPLACE_WITH_EXPLICIT_SHARED_TRANSCRIPT_KEY
```

The lifecycle API key reference uses `astrabox/secrets.py`'s secret provider,
not the product's encrypted Vault records. Its environment provider maps a name
to uppercase with hyphens replaced by underscores: `opensandbox-api-key`
therefore reads `OPENSANDBOX_API_KEY`. Supply the same value to each replica, as
shown above; this resolver currently reads only process environment. Sharing
the product database alone does not make that named secret available. The
container environment file carries this operator-chosen name without adding
an application-specific credential variable.

Every other setting the existing deployment uses also belongs in this file.
Preserve the existing LiteLLM settings, including `ASTRABOX_LITELLM_API_KEY`,
`ASTRABOX_LITELLM_SERVER_BASE_URL` and `ASTRABOX_LITELLM_ADMIN_URL` when the
deployment sets them. When `ASTRABOX_LITELLM_API_KEY` is empty, each replica's
entry point uses `LITELLM_MASTER_KEY` to confirm or create the sandbox model
key derived from `ASTRABOX_AUTH_SESSION_SECRET`, and the replica does not start
without it. Preserve the existing identity-provider settings, such as
`ASTRABOX_WEB_IDENTITY` and the registered OIDC settings. This overlay does not
configure an identity provider or install a second Casdoor instance.

The overlay selects the local secret-store provider backed by the shared
database. Its encryption key must be the existing deployment key, not a newly
generated key. The encoded value in `/data/vault.key` can populate
`ASTRABOX_VAULT_MASTER_KEY` without changing Vault encryption. Likewise,
`/data/auth-session.key` can populate `ASTRABOX_AUTH_SESSION_SECRET` without
changing the browser-session signing key.

Callback signing has a different boundary. When no explicit transcript key is
configured, a file-backed Vault key and its environment representation use
different callback-key derivations. Copying the encoded Vault value into the
environment therefore does not preserve existing callback credentials. The
explicit transcript setting accepts a text secret; it does not import an
arbitrary binary derived key. Do not base64-encode a derived key and assume the
result reproduces it.

An existing deployment without an explicit transcript key needs a coordinated
rotation before using this overlay: stop admitting work, drain active work,
retire prepared and resident sandbox runtimes carrying old callback credentials,
and configure one explicit transcript key across all replicas before preparing
replacement runtimes. Preserve the product database, SessionStore and workspace
storage. Accept the replacement runtimes against the new callback credentials
before reopening traffic. This overlay does not automate that rotation or
refresh tokens inside existing sandboxes. Do not copy `/data` wholesale: each
replica gets a separate local volume, not a database copy.

Add the host-specific Compose inputs to that same file:

```dotenv
ASTRABOX_REPLICA_ENV_FILE=/etc/astrabox/replica.env
ASTRABOX_SERVER_IMAGE=registry.example.com/astrabox/server@sha256:REPLACE_WITH_IMAGE_DIGEST
ASTRABOX_SERVER_BIND_IP=10.0.1.12
ASTRABOX_SERVER_HOST_PORT=8088
ASTRABOX_KUBECONFIG_HOST_PATH=/etc/astrabox/kubeconfig
ASTRABOX_STATE_VOLUME=astrabox-api-b-state
```

Use the same immutable server image and sandbox images on every host. Only the
bind address, local paths and local volume name vary between replicas. The
`ASTRABOX_REPLICA_ENV_FILE` input is a Compose file path, not an application
setting. Absolute paths avoid Compose's base-file-relative path resolution.
The overlay fixes the container listener at `0.0.0.0:8000`; change only
`ASTRABOX_SERVER_BIND_IP` and `ASTRABOX_SERVER_HOST_PORT` for its host publication.

## Start only the platform replica

From the repository root, on each host:

```bash
docker compose --env-file /etc/astrabox/replica.env \
  -p astrabox-api-b \
  -f containers/compose.yaml \
  -f containers/compose.kubernetes.yaml \
  -f containers/compose.distributed.yaml config --quiet

docker compose --env-file /etc/astrabox/replica.env \
  -p astrabox-api-b \
  -f containers/compose.yaml \
  -f containers/compose.kubernetes.yaml \
  -f containers/compose.distributed.yaml up -d --no-deps --no-build server
```

Use Docker Compose 2.24.4 or newer for the `!override` merge tag. The distributed
overlay must be last. Do not use `scripts/compose.sh` for this topology: that
wrapper prepares credentials for a local database. Do not enable the
`local-data-only` or `docker-runtime-only` profiles. The overlay removes local
service dependencies and mounts no Docker socket or local database secrets.
The entry point sees the external service URLs and does not start embedded
OpenSandbox, LiteLLM or channel-gateway processes.

Check `/healthz` and `/readyz` through each replica's private address, then
register healthy replicas with the ingress. Validate the deployment with
concurrent pool claims from both replicas, unique sandbox assignment,
same-session history and streaming through either replica, and cross-replica
stop/approval actions. Verify shared workspace persistence on different sandbox
nodes separately. These checks exercise shared-service connections that a
configuration render or a health check cannot verify.
