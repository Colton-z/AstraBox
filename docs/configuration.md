<!-- GENERATED FILE — do not hand-edit.
     Generated from: astrabox/config/env_registry.py
     Regenerate with: python scripts/gen_config_docs.py -->

# Complete environment-variable list

This generated page lists every environment variable read by AstraBox. Use it
to look up an exact name, default, or accepted scope. For deployment workflows,
start with [Configure AstraBox](environments.md), which groups common settings by
task and explains when to use them.

The complete list is grouped by intended use:

* **Deployment settings** configure a running installation.
* **Advanced tuning** changes timeouts, retries, and other defensive defaults.
* **Sandbox runtime variables** are prepared by the service for sandbox code.
* **Local development** options support debugging on a developer machine.
* **Automated tests** options are reserved for the end-to-end test suite.

## Deployment settings

Settings for credentials, endpoints, feature options, resource limits, and security. `.env.example` hand-picks common quickstart settings rather than rendering this complete group.

| Variable | Default | Description |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | *(none)* | API key for the Anthropic-compatible model endpoint. Requests use the x-api-key header when neither bearer-token alias is configured. |
| `ANTHROPIC_AUTH_TOKEN` | *(none)* | Bearer token for the Anthropic-compatible model endpoint. It shares a setting with ANTHROPIC_API_KEY and ASTRABOX_LLM_AUTH_TOKEN; the first configured value in that order takes precedence. |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | Base URL of the Anthropic-compatible LLM endpoint the agent loop calls; the one required setting for a bring-your-own-key install. Aliased with ASTRABOX_LLM_BASE_URL (same field, either name works). |
| `ANTHROPIC_MODEL` | `claude-opus-4-8` | Default model id passed to the agent loop. Aliased with ASTRABOX_LLM_MODEL (same field, either name works). |
| `ASTRABOX_ADMIN_API_TOKEN` | *(none)* | Optional bearer token locking the /api/v1/admin-api automation surface; unset keeps it open (single-tenant default). |
| `ASTRABOX_ADMIN_GROUP` | `astrabox-admin` | Group name mapped to the 'admin' role; shared by both SSO resolvers (trusted-header and JWT). |
| `ASTRABOX_AGENT_ENABLED` | `true` | Enables the Agent APIs and Session creation. |
| `ASTRABOX_AGENT_IMAGE` | `derived` | Agent sandbox container image, used by every Claude Code Environment that pins none of its own. Unset, it is <ASTRABOX_IMAGE_PREFIX>sandbox-claude-code:<ASTRABOX_IMAGE_TAG>. The image must include @anthropic-ai/claude-code. Also read by `astrabox up`, which reports whether the image is present on the host rather than letting the first session fail to find it. |
| `ASTRABOX_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1],testserver` | Comma-separated Host allowlist for the DNS-rebinding guard; REPLACES the default list wholesale. Exposing the console behind a real hostname means adding it here (and putting real authentication in front). |
| `ASTRABOX_AUTH_SESSION_SECRET` | *(none)* | HS256 signing secret for the console session cookie. Empty = a key is generated once into <state_dir>/auth-session.key (mode 0600). Multi-replica deployments must set the same value in every replica. |
| `ASTRABOX_AUTH_SESSION_TTL_SECONDS` | `604800` | Console session cookie lifetime in seconds (default 7 days). |
| `ASTRABOX_AWS_KMS_KEY_ARN` | *(none)* | Immutable symmetric AWS KMS key ARN used when ASTRABOX_SECRET_STORE=aws_kms. The workload identity needs kms:GenerateDataKey and kms:Decrypt; the value is rejected for every non-key ARN shape. |
| `ASTRABOX_CASDOOR_ADMIN_URL` | *(none)* | Browser-facing Casdoor management URL shown to platform administrators. The bundled SSO Compose overlay sets it to the public issuer. Leave it empty for another OIDC provider or when Casdoor management is not reachable from the browser. |
| `ASTRABOX_CASDOOR_API_ACCESS_URL` | *(none)* | Browser-facing Casdoor application URL shown as the API-access management entry. It is exposed only when OIDC API credentials are configured; the bundled SSO overlay fills it automatically. |
| `ASTRABOX_CHANNEL_CLAIM_STALE_SECONDS` | `900` | Lease lifetime in seconds for processing an inbound messaging-platform event. Active processing renews the lease; an event becomes eligible for retry after this interval without renewal. The minimum is 300 seconds. |
| `ASTRABOX_CHANNEL_GATEWAY_BASE_URL` | *(none)* | Private channel-adapter gateway URL. Empty makes the server image run its bundled gateway on loopback. Multi-replica deployments set one shared HTTPS URL; plaintext HTTP is accepted only on loopback. |
| `ASTRABOX_CHANNEL_GATEWAY_TOKEN` | `generated` | Service-to-service credential shared by AstraBox and the private channel gateway. The bundled deployment generates an ephemeral value. An external gateway uses the same operator-supplied value on both services; the value must contain at least 32 characters. |
| `ASTRABOX_CHANNEL_RECONCILE_INTERVAL_SECONDS` | `60` | Interval in seconds for retrying expired inbound messaging events and unfinished outbound deliveries. Multiple replicas can run this recovery loop. The minimum is 10 seconds. |
| `ASTRABOX_CLIENT_ID` | *(none)* | OAuth client id the client subcommands exchange for a short-lived bearer token when ASTRABOX_TOKEN is unset. Requires ASTRABOX_CLIENT_SECRET and ASTRABOX_TOKEN_URL; a partial set is refused rather than downgraded to an unauthenticated request. |
| `ASTRABOX_CLIENT_SECRET` | *(none)* | OAuth client secret paired with ASTRABOX_CLIENT_ID for the client-credentials exchange. |
| `ASTRABOX_DB_BACKEND` | `postgresql` | Persistence backend name. PostgreSQL is the deployment default; SQLite is included, and the [mongo] extra registers MongoDB. |
| `ASTRABOX_DB_NAME` | `astrabox` | Mongo database name fallback when the connection URI carries none (mongo backend). |
| `ASTRABOX_DB_URL` | *(none)* | Database connection URL. The maintained local launchers populate it from generated service-scoped credentials; direct and external deployments must set it explicitly. A PostgreSQL, MongoDB, or SQLite URL also selects the matching backend. |
| `ASTRABOX_EFS_FILE_SYSTEM_ID` | *(none)* | Existing AWS EFS filesystem ID (fs-...), required when ASTRABOX_STORAGE_PROVIDER=aws_efs. The provider requires Kubernetes, shared workspace topology, and a Bound ReadWriteMany PVC configured by ASTRABOX_SANDBOX_WORKSPACE_VOLUME. Its PV must use efs.csi.aws.com, exactly this filesystem ID as volumeHandle, and encryption in transit. EFS resources, CSI installation and credentials remain deployment-owned. |
| `ASTRABOX_ENABLED` | `true` | Master feature flag (is_astrabox_enabled()); false disables the feature wholesale. |
| `ASTRABOX_ENDPOINT` | `derived` | Deployment base URL the client subcommands call. Derived from ASTRABOX_SERVER_HOST_PORT as http://127.0.0.1:<port> when unset. A deployment started with `astrabox serve` directly binds ASTRABOX_PORT on the host instead, and needs this set. `--endpoint` takes precedence. |
| `ASTRABOX_ENV` | `community` | Deployment environment label (diagnostic) and the app-{env}.yml YAML-overlay selector. Aliased with SERVER_ENV. |
| `ASTRABOX_EXTENSION_PROVIDER` | *(none)* | Selects the ExtensionProvider that supplies catalogs, runtime bindings, and provider-owned MCP request authorization. Empty selects the provider that declares itself as the deployment default. |
| `ASTRABOX_GIT_HTTPS_TOKEN_SECRET_NAME` | *(none)* | Name of the secret holding a git host HTTPS access token, used to clone plugin/default repos on backends that cannot reach git over SSH. |
| `ASTRABOX_HOST` | `127.0.0.1` | Bind address for the `astrabox serve` / uvicorn entrypoint. Loopback by default (no API auth); the server container sets 0.0.0.0 explicitly. |
| `ASTRABOX_IMAGE_PREFIX` | `ghcr.io/colton-z/astrabox-` | Repository prefix of every default AstraBox image; the component name (server, sandbox-claude-code, sandbox-hermes, workspace-mounter, ...) is appended. Set it to a registry mirror that holds the published images. scripts/compose.sh sets astrabox/, the prefix `make build-*` gives images built from a checkout. The Compose file applies it to the server image too. |
| `ASTRABOX_IMAGE_TAG` | `derived` | Tag of every default AstraBox image. Unset, it is the installed AstraBox version, so a release runs the sandbox images published with it. scripts/install.sh writes the installed release; scripts/compose.sh sets latest for images built from a checkout. The Compose file applies it to the server image too. |
| `ASTRABOX_JWT_ALGORITHMS` | `RS256,ES256` | Comma-separated signature algorithms accepted; overrides the key-material path's default (RS256,ES256 for JWKS/OIDC, HS256 for the shared-secret path). |
| `ASTRABOX_JWT_AUDIENCE` | *(none)* | Expected `aud` claim; when set, audience verification is enforced. |
| `ASTRABOX_JWT_EMAIL_CLAIM` | `email` | JWT claim mapped to the user's email. |
| `ASTRABOX_JWT_GROUPS_CLAIM` | `groups` | JWT claim mapped to group membership. |
| `ASTRABOX_JWT_ISSUER` | *(none)* | OIDC issuer used for JWKS discovery when no JWKS URL is set; also checked as the expected `iss` claim whenever set, even alongside a JWKS URL. |
| `ASTRABOX_JWT_JWKS_URL` | *(none)* | JWKS URL used to verify the token signature (RS256/ES256); takes precedence over ASTRABOX_JWT_ISSUER and ASTRABOX_JWT_SECRET. |
| `ASTRABOX_JWT_NAME_CLAIM` | `name` | JWT claim mapped to the user's display name. |
| `ASTRABOX_JWT_ORG_CLAIM` | *(none)* | JWT claim mapped to the org id; unset falls back to the deployment default org. |
| `ASTRABOX_JWT_SECRET` | *(none)* | HS256 shared secret used to verify the token signature when neither a JWKS URL nor an issuer is set. |
| `ASTRABOX_JWT_STRICT` | `true` | Strict (default): a missing bearer token is a 401. Set an explicit falsey value (0/false/no/off) to fall through anonymous instead. |
| `ASTRABOX_JWT_USER_CLAIM` | `sub` | JWT claim mapped to the user id. |
| `ASTRABOX_LITELLM_ADMIN_URL` | *(none)* | Browser-facing management URL for an external LiteLLM deployment. When empty, external gateways have no console link; the bundled gateway always uses AstraBox's protected /litellm route and rejects this setting. |
| `ASTRABOX_LITELLM_API_KEY` | *(none)* | API key the sandbox sends to the LiteLLM proxy. |
| `ASTRABOX_LITELLM_BASE_URL` | *(none)* | Base URL of an EXTERNAL LiteLLM proxy (your own gateway). Unset (the default) means the embedded proxy: sandboxes get the platform callback host on HTTP port 80 so Credential Vault can match it, the server talks to it over loopback, and the image starts it automatically. |
| `ASTRABOX_LITELLM_SERVER_BASE_URL` | *(none)* | Optional server-side URL for the same LiteLLM proxy. Set it only when the sandbox-facing URL uses private DNS or a different network path; the platform uses this address to query /v1/models. Unset defaults to the external shared URL or embedded loopback. |
| `ASTRABOX_LLM_AUTH_TOKEN` | *(none)* | AstraBox-prefixed alias of ANTHROPIC_AUTH_TOKEN. Either name configures the same bearer-token setting. |
| `ASTRABOX_LLM_BASE_URL` | `https://api.anthropic.com` | AstraBox-namespaced alias of ANTHROPIC_BASE_URL — same field, either name works. |
| `ASTRABOX_LLM_MODEL` | `claude-opus-4-8` | AstraBox-namespaced alias of ANTHROPIC_MODEL — same field, either name works. |
| `ASTRABOX_LOCAL_MODE` | `false` | Enables local-development features: explicitly permitted plaintext model and sandbox keys, latest-version CLI installation, and per-Session debug paths. Standard bring-your-own-key deployments leave this false and use the regular credential settings. |
| `ASTRABOX_LOGGING_PATH` | *(none)* | Extra log directory the admin log viewer scans, in addition to ./logs. |
| `ASTRABOX_LOG_FORMAT` | *(none)* | AstraBox application logger output format. Unset/`text` = the human-readable line format; `json` emits one structured JSON object per record (ts/level/logger/msg/module/line/exc) for a log aggregator. Root, Uvicorn, and third-party loggers retain their own format. |
| `ASTRABOX_LOG_LEVEL` | `info` | uvicorn log level for the `astrabox serve` / programmatic entrypoints. Also read by astrabox.deploy.sandbox_server, which upper-cases it into the OpenSandbox lifecycle server's log level so one setting covers both processes in the container. |
| `ASTRABOX_LOOP_STALL_DUMP_S` | `5.0` | An event-loop stall longer than this many seconds dumps every thread's stack to stderr. The independent loop-delay warning starts at one second. |
| `ASTRABOX_MCP_PROXY_BASE_URL` | *(none)* | Base URL sandboxes use for platform-MCP and Session callback requests. A container deployment derives the server's bridge address when empty; a host deployment sets it explicitly, for example http://host.docker.internal:8000. |
| `ASTRABOX_METRICS_ENABLED` | `true` | Serve the /metrics Prometheus endpoint with basic in-process counters. Set false to return 404. |
| `ASTRABOX_METRICS_TOKEN` | *(none)* | Optional bearer token for /metrics. Unset (default): the endpoint is unauthenticated, the usual scrape posture for counter names + integers. Set it when /metrics is reachable beyond the scrape network: requests must then send Authorization: Bearer <token> (Prometheus authorization.credentials) or get 401. |
| `ASTRABOX_MODEL_API_KEY` | *(none)* | Operator override for the model credential used by Agent Sessions. It takes precedence over ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, and ASTRABOX_LLM_AUTH_TOKEN. |
| `ASTRABOX_MODEL_API_KEY_SECRET_NAME` | *(none)* | Name of the service-side secret entry that supplies the model API key. This keeps the credential out of plaintext deployment variables. |
| `ASTRABOX_MODEL_BASE_URL` | *(none)* | Model endpoint requested from the selected model-endpoint plugin when no plugin-specific endpoint replaces it. Built-in LiteLLM uses ASTRABOX_LITELLM_BASE_URL for Agent inference. |
| `ASTRABOX_MODEL_ENDPOINT_PROVIDER` | *(none)* | Selects the ModelEndpointProvider that decides the sandbox's wire endpoint. Empty selects the provider that declares itself as the deployment default. |
| `ASTRABOX_MODEL_GATEWAY_REQUIRE_HTTPS` | `false` | Require the sandbox-facing model gateway to use an HTTPS fully qualified domain name on port 443. The embedded gateway is intentionally HTTP-only for single-host development, so enabling this setting also requires an external ASTRABOX_LITELLM_BASE_URL. This transport rule is independent of whether Credential Vault is enabled. |
| `ASTRABOX_MODEL_NAME` | *(none)* | Operator override for the model name when the agent doesn't specify one. |
| `ASTRABOX_MONGODB_URI` | *(none)* | Mongo connection URI (mongo backend); checked after ASTRABOX_DB_URL and before the plain MONGODB_URI in the mongo backend's URI resolution chain. |
| `ASTRABOX_NAS_BASE_PATH` | `/astrabox` | Base path under the NAS mount used for Session workspaces. It prefixes the persistent workspace path on every storage backend. |
| `ASTRABOX_NAS_ENABLED` | `false` | Enables the NAS-backed shared workspace mount, and moves the agent's cwd to the mount target (/root/workspace). Only honored by a sandbox backend that mounts at runtime; the bundled open_sandbox backend mounts at create time, and setting this with it selected is refused at startup. |
| `ASTRABOX_NAS_ENDPOINT` | *(none)* | NAS endpoint address; required when ASTRABOX_NAS_ENABLED is set. Same backend condition as ASTRABOX_NAS_ENABLED. |
| `ASTRABOX_OIDC_ADMIN_GROUP` | `astrabox-admin` | Group whose members get the admin role (same convention as the trusted_header/jwt resolvers). |
| `ASTRABOX_OIDC_API_CLIENT_ID` | *(none)* | OAuth client id for machine-to-machine AstraBox API access. When set with its secret, bearer tokens are checked through the provider's RFC 7662 introspection endpoint and authorized by AstraBox API scopes. |
| `ASTRABOX_OIDC_API_CLIENT_SECRET` | *(none)* | Long-lived OAuth machine-client secret used to issue and introspect short-lived AstraBox API tokens. This setting and ASTRABOX_OIDC_API_CLIENT_SECRET_FILE are mutually exclusive. |
| `ASTRABOX_OIDC_API_CLIENT_SECRET_FILE` | *(none)* | File containing the long-lived OAuth machine-client secret. The bundled Casdoor deployment generates this file and mounts it only into Casdoor and AstraBox. This setting and ASTRABOX_OIDC_API_CLIENT_SECRET are mutually exclusive. |
| `ASTRABOX_OIDC_CLIENT_ID` | *(none)* | OAuth client id registered at the IdP for the AstraBox console. |
| `ASTRABOX_OIDC_CLIENT_SECRET` | *(none)* | OAuth client secret for the console token exchange. This setting and ASTRABOX_OIDC_CLIENT_SECRET_FILE are mutually exclusive. |
| `ASTRABOX_OIDC_CLIENT_SECRET_FILE` | *(none)* | File containing the OAuth client secret for the token exchange. The bundled Casdoor deployment uses this form so the value is absent from the server container environment. This setting and ASTRABOX_OIDC_CLIENT_SECRET are mutually exclusive. |
| `ASTRABOX_OIDC_GROUPS_CLAIM` | `groups` | ID-token claim read as the user's groups for role mapping. |
| `ASTRABOX_OIDC_INTERNAL_ISSUER` | *(none)* | Server-side base for discovery/token/JWKS calls when the IdP is reached differently from inside the deployment than from the browser (compose split-horizon: http://casdoor:8000). Empty = same as the issuer. |
| `ASTRABOX_OIDC_ISSUER` | *(none)* | OIDC issuer URL for the built-in login (ASTRABOX_WEB_IDENTITY=oidc): the browser-facing base the authorize redirect and id_token `iss` are validated against. Required (with the client id) when the oidc mode is selected. |
| `ASTRABOX_OIDC_REDIRECT_URL` | *(none)* | Explicit OAuth redirect URL override for deployments behind a proxy whose external base the server cannot derive from the request. Empty = derived as <request base>/api/v1/auth/callback. |
| `ASTRABOX_OIDC_SCOPES` | `openid profile email` | Scopes requested at authorization. |
| `ASTRABOX_OIDC_STRICT` | `true` | Absent-session behaviour under the oidc resolver: strict (default) rejects with 401 AUTH_REQUIRED so the console redirects to login; an explicit falsey value lets anonymous requests fall through to the local identity. A present-but-invalid session is always rejected regardless. |
| `ASTRABOX_PERSISTENCE_SLOW_OP_S` | `1.0` | A persistence operation slower than this many seconds logs a warning naming the operation. This diagnostic threshold does not cancel the operation. |
| `ASTRABOX_PORT` | `8000` | Listen port for `astrabox serve` / uvicorn. Also read (container-only) to derive the sandbox callback URL when ASTRABOX_MCP_PROXY_BASE_URL is unset. |
| `ASTRABOX_PUBLISH_HOST_IP` | `127.0.0.1` | IP address used for Docker-published sandbox ports and for the bundled lifecycle server's connection back to those ports. Keep 127.0.0.1 when AstraBox runs on the host. When AstraBox runs in a container, use a reachable Docker host address, normally the bridge gateway; the maintained Compose stack sets 172.17.0.1 by default. A 0.0.0.0 address requires a firewall or authenticated proxy around sandbox HTTP and execd ports. The value must be an IP address. This setting applies to Docker only. |
| `ASTRABOX_RECONCILE_HEARTBEAT_STALE_S` | `20` | A worker heartbeat older than this marks the turn stale and eligible for recovery by another worker. |
| `ASTRABOX_RECONCILE_SCAN_INTERVAL_S` | `10` | Interval in seconds for finding unfinished turns whose worker has stopped reporting. Lower values reduce recovery latency during a rolling deployment. |
| `ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED` | `true` | Whether this process may CREATE missing unique indexes itself, vs. only verifying they already exist — set false for a managed-Mongo operator whose DBAs pre-create indexes out-of-band. |
| `ASTRABOX_SANDBOX_API_KEY_SECRET_NAME` | *(none)* | Name of the secret holding the OpenSandbox platform API key (resolved via SecretProvider). |
| `ASTRABOX_SANDBOX_BACKEND` | `open_sandbox` | Name of the registered sandbox backend. Startup rejects unknown names. |
| `ASTRABOX_SANDBOX_CREDENTIAL_VAULT` | `true` | Keeps model, MCP, and assigned outbound credentials outside the sandbox and gives the Agent process placeholders. Enabled by default; the selected sandbox backend must implement protected credential delivery independently of Environment networking. A false value passes the model credential to the Agent process and refuses managed outbound credentials. |
| `ASTRABOX_SANDBOX_EGRESS_IMAGE` | `opensandbox/egress:v1.1.7` | OpenSandbox outbound proxy image used for provider network enforcement and protected credential injection. The default is the tested image. A missing image makes either requested OpenSandbox capability fail before allocation. |
| `ASTRABOX_SANDBOX_EGRESS_MODE` | `dns+nft` | How the outbound proxy enforces network rules: 'dns' filters on resolved names, 'dns+nft' adds packet-level rules. Only read when ASTRABOX_SANDBOX_EGRESS_IMAGE is set. OpenSandbox egress v1.1.7 requires 'dns+nft' for its credential component; this provider constraint does not couple AstraBox's Vault and Environment networking contracts. |
| `ASTRABOX_SANDBOX_ENDPOINT_SCHEME` | *(none)* | Scheme for OpenSandbox data-plane URLs: http or https. Leave unset when the lifecycle API and the ingress/execd endpoint use the same scheme. Set it when an internal HTTP lifecycle API returns routes served by an HTTPS ingress gateway. |
| `ASTRABOX_SANDBOX_PIDS_LIMIT` | `512` | PID limit for each Docker sandbox container. Kubernetes applies PID limits through the kubelet podPidsLimit setting instead. |
| `ASTRABOX_SANDBOX_SECURE_ACCESS` | `false` | Protect browser-facing sandbox ports with OpenSandbox Secure Access. This requires the Kubernetes runtime, ingress.mode=gateway, and matching signing keys on the lifecycle server and the OpenSandbox ingress component. AstraBox opts cold-created and prewarmed sandboxes into protection and returns short-lived signed URLs. Docker does not support this setting and the bundled launcher refuses it there. |
| `ASTRABOX_SANDBOX_SECURE_RUNTIME` | *(none)* | Deployment-wide sandbox runtime: gvisor, kata, firecracker, or empty for runc. The host or cluster must provide the selected runtime. Firecracker requires Kubernetes. OpenSandbox's outbound proxy, used by its networking and protected-delivery implementations, is incompatible with gVisor; use runc or Kata for that combination. Distinct runtime policies require separate AstraBox deployments. |
| `ASTRABOX_SANDBOX_SERVER_INGRESS_GATEWAY_ADDRESS` | *(none)* | Public host, host:port, or wildcard domain of the OpenSandbox ingress gateway. This value contains no URL scheme; configure the browser scheme with ASTRABOX_SANDBOX_ENDPOINT_SCHEME. Required in gateway mode. |
| `ASTRABOX_SANDBOX_SERVER_INGRESS_MODE` | `direct` | OpenSandbox endpoint mode for the bundled Kubernetes lifecycle server. Use direct only when AstraBox and its clients can route to sandbox Pod addresses. Use gateway for a multi-node deployment after installing the official OpenSandbox ingress component. |
| `ASTRABOX_SANDBOX_SERVER_INGRESS_ROUTE_MODE` | `uri` | OpenSandbox gateway routing mode for browser links: uri or wildcard. AstraBox refuses header mode because a normal browser navigation cannot supply its required OpenSandbox-Ingress-To header. |
| `ASTRABOX_SANDBOX_SERVER_INGRESS_SIGNING_KEY` | *(none)* | Base64 signing secret for short-lived OpenSandbox browser URLs. Required when ASTRABOX_SANDBOX_SECURE_ACCESS=true and written only to the generated 0600 server.toml. Configure the same key on the separately deployed OpenSandbox ingress component. |
| `ASTRABOX_SANDBOX_SERVER_KUBECONFIG` | *(none)* | Path inside the AstraBox container to the kubeconfig used by the bundled OpenSandbox server. An empty value selects in-cluster ServiceAccount credentials. The file must be readable by the astrabox user; startup validates the credentials and Kubernetes API connection. |
| `ASTRABOX_SANDBOX_SERVER_KUBE_API_SERVER` | *(none)* | Kubernetes API server address to substitute into every cluster entry of the kubeconfig, for example https://10.0.1.7:6443. An empty value uses the address already present in the kubeconfig. Choose an address reachable from the container and covered by the API server certificate. AstraBox writes a 0600 derived copy beside server.toml and leaves the source file unchanged. |
| `ASTRABOX_SANDBOX_SERVER_KUBE_CREATE_TIMEOUT_SECONDS` | `60` | Seconds the Kubernetes runtime waits for a new sandbox Pod to report an IP. Use a value of at least 1 and increase it for clusters that must add a node or pull a large Agent image before scheduling the Pod. |
| `ASTRABOX_SANDBOX_SERVER_KUBE_NAMESPACE` | `opensandbox` | Existing Kubernetes namespace where OpenSandbox creates sandbox Pods. This workload namespace is separate from the controller namespace. Startup checks that it exists and reports the command for creating it when absent. |
| `ASTRABOX_SANDBOX_SERVER_KUBE_WORKLOAD_PROVIDER` | `batchsandbox` | Kubernetes resource used for each sandbox: batchsandbox selects batchsandboxes.sandbox.opensandbox.io; agent-sandbox selects sandboxes.agents.x-k8s.io. Startup validates the selected value and CRD. |
| `ASTRABOX_SANDBOX_SERVER_METADATA_DIR` | `derived` | Writable directory for OpenSandbox metadata, expiration updates, generated server.toml, and any derived kubeconfig. The default is <state_dir>/opensandbox/metadata. Docker deployments persist this directory so renewed sandbox leases survive a service restart; Kubernetes stores renewal state in the workload resource. |
| `ASTRABOX_SANDBOX_SERVER_NETWORK_MODE` | `bridge` | Docker network mode for sandboxes the bundled lifecycle server creates when ASTRABOX_SANDBOX_SERVER_RUNTIME=docker. OpenSandbox's protected credential component requires bridge; that is a provider topology constraint, not an Environment networking rule. The fixed sandbox service ports make host, container:<id>, and none invalid for this deployment layout. |
| `ASTRABOX_SANDBOX_SERVER_PORT_RANGE` | `20000-32000` | Host port range ('min-max', both >= 1024, spanning >= 100) the lifecycle server publishes sandbox ports into. Each sandbox consumes 2-3 ports; narrow it to match a firewall policy. It must not overlap the kernel's ephemeral range (/proc/sys/net/ipv4/ip_local_port_range, 32768-60999 by default): a published port and an outgoing connection's source port come from the same numbers, so an overlapping range fails concurrent creates with 'address already in use'. Startup refuses an overlap it can read. When multiple lifecycle servers share one Docker daemon, give every server a non-overlapping range: OpenSandbox probes and releases candidate ports before Docker binds them, so overlapping ranges can race during concurrent creates. The maintained Compose stack forwards this setting. ASTRABOX_SANDBOX_SERVER_RUNTIME=docker only — Kubernetes publishes no host ports at all. |
| `ASTRABOX_SANDBOX_SERVER_RUNTIME` | `docker` | Runtime used by the bundled OpenSandbox lifecycle server: docker creates containers on the configured Docker daemon; kubernetes creates Pods through the selected OpenSandbox workload provider. ASTRABOX_SANDBOX_SERVER_KUBE_* settings apply to Kubernetes, while network mode, port range, PID limit, and publish-host settings apply to Docker. |
| `ASTRABOX_SANDBOX_WORKSPACE_VOLUME` | *(none)* | Optional platform volume that durable workspace files are mounted from — a PersistentVolumeClaim on Kubernetes, a named volume on Docker. Each Agent conversation and each Assistant is a subPath under it, so one volume serves the deployment and a pooled box (created before the conversation that borrows it) can still carry it. Empty leaves workspace files on the sandbox's temporary filesystem. Workspace file persistence is separate from database-backed SessionStore recovery. |
| `ASTRABOX_SCOPE` | *(none)* | Space-separated scopes requested in the client-credentials exchange (astrabox:read, astrabox:write, astrabox:admin). Unset sends no scope parameter, letting the identity provider issue what the client is registered for. |
| `ASTRABOX_SECRET_STORE` | `local` | Credential secret-store provider. local encrypts values in the product database under a persistent deployment key; aws_kms uses AWS KMS envelope encryption for stateless replicas. Unknown providers fail during startup. |
| `ASTRABOX_SERVER_HOST_PORT` | `8088` | Host port the maintained local Compose deployment publishes AstraBox on (containers/compose.yaml maps 127.0.0.1:<this>:8000). The client subcommands derive their default endpoint from it, so moving the published port moves it for both. Distinct from ASTRABOX_PORT, which is the port the app binds inside the container. |
| `ASTRABOX_SHUTDOWN_DRAIN_SECONDS` | `0` | Seconds to keep services available for in-flight work after /readyz turns not ready during shutdown. Kubernetes deployments pair this with preStop and a terminationGracePeriodSeconds longer than the drain window. |
| `ASTRABOX_STATE_DIR` | `./.astrabox` | Root directory for session workspaces, generated deployment keys, OpenSandbox metadata, and other local artifacts. Database records live in PostgreSQL by default. |
| `ASTRABOX_STORAGE_PROVIDER` | `mounted_volume` | Name of the registered storage provider that confirms a box's durable workspace arrived. Independent of ASTRABOX_SANDBOX_BACKEND — where a workspace lives is a durability decision, not a sandbox runtime one. Startup rejects unknown names. 'mounted_volume' supplies the deployment's backing filesystem; the platform's common mergerfs router assigns workspaces independently of the selected medium. 'aws_efs' verifies an operator-owned EFS CSI-backed PVC on Kubernetes. Additional providers can be installed through the astrabox.providers.storage entry-point group. |
| `ASTRABOX_TITLE_MODEL_API_KEY` | *(none)* | API key for the title-generation model. |
| `ASTRABOX_TITLE_MODEL_API_KEY_SECRET_NAME` | *(none)* | Name of the secret holding the title-generation model's API key. |
| `ASTRABOX_TITLE_MODEL_BASE_URL` | *(none)* | Base URL of a separate model used for titles and process summaries; empty falls back to the main model config. |
| `ASTRABOX_TITLE_MODEL_ENABLED` | `true` | Enable automatic conversation titles and process summaries. False sends no label-model requests; existing labels and normal Agent turns remain available. YAML: astrabox.title_model.enabled. Restart to apply. |
| `ASTRABOX_TITLE_MODEL_NAME` | *(none)* | Model name for session-title generation. |
| `ASTRABOX_TITLE_MODEL_REQUEST_TIMEOUT_SECONDS` | `60` | Positive, finite HTTPX network inactivity timeout in seconds for title requests. YAML: astrabox.title_model.request_timeout_seconds. Does not limit the whole Agent turn or add retries. Restart to apply. |
| `ASTRABOX_TOKEN` | *(none)* | Bearer token the client subcommands send. Unset sends no Authorization header, which is what the default local identity mode expects. `--token` takes precedence, and a token set either way skips the client-credentials exchange below. |
| `ASTRABOX_TOKEN_URL` | *(none)* | Identity provider token endpoint the client-credentials exchange posts to. |
| `ASTRABOX_TRANSCRIPT_CAPABILITY_REQUIRED` | `true` | Requires a per-Session capability token on sandbox-to-service transcript requests. A false value permits requests without this token and is intended only for network-isolated, single-tenant deployments. |
| `ASTRABOX_TRANSCRIPT_SIGNING_KEY` | *(none)* | Shared secret keying the transcript capability HMAC (the tenant fence's trust root). Unset derives one (domain-separated) from ASTRABOX_VAULT_MASTER_KEY, else from the deployment master key generated once into <state_dir>/vault.key when ASTRABOX_SECRET_STORE=local — the zero-config single-node path. KMS-backed and other stateless deployments must set this variable to the same value on every replica. |
| `ASTRABOX_TRUSTED_HEADER_EMAIL` | `x-forwarded-email` | Header name carrying the user's email. |
| `ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET` | *(none)* | Shared secret the proxy must present (in ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET_HEADER) to prove a request transited it; empty disables the check (forwarded headers are then trusted unconditionally). |
| `ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET_HEADER` | `x-astrabox-gateway-secret` | Header name the gateway secret is compared against. |
| `ASTRABOX_TRUSTED_HEADER_GROUPS` | `x-forwarded-groups` | Header name carrying comma/whitespace-delimited group membership. |
| `ASTRABOX_TRUSTED_HEADER_NAME` | `x-forwarded-preferred-username` | Header name carrying the user's display name. |
| `ASTRABOX_TRUSTED_HEADER_ORG` | *(none)* | Header name carrying the org id; unset falls back to the deployment default org. |
| `ASTRABOX_TRUSTED_HEADER_STRICT` | `true` | Strict (default): a missing user header is a 401. Set an explicit falsey value (0/false/no/off) to fall through anonymous instead. |
| `ASTRABOX_TRUSTED_HEADER_USER` | `x-forwarded-user` | Header name carrying the authenticated user id, forwarded by the SSO proxy (oauth2-proxy / Authelia / Authentik / Cloudflare Access). |
| `ASTRABOX_VAULT_MASTER_KEY` | `generated` | Urlsafe-base64 32-byte AES-GCM master key for the local secret vault; unset auto-generates and atomically persists one at <state_dir>/vault.key (0600). Only consumed when ASTRABOX_SECRET_STORE=local; must be the SAME value on every replica when that provider is used in a multi-replica deployment. |
| `ASTRABOX_WEBHOOK_HMAC_WINDOW_SECONDS` | `300` | Freshness window (seconds) for the hmac webhook scene: a request whose X-WEBHOOK-TIMESTAMP is outside +/- this is rejected 401, so a captured (timestamp, signature, body) triple expires. The signature binds the body, so it also cannot be replayed with a different body. |
| `ASTRABOX_WEB_IDENTITY` | `local` | Identity profile for the web console and HTTP API: local, oidc, jwt, or trusted_header. Startup validates the selected name. |
| `ASTRABOX_WORKSPACE_MOUNTER_IMAGE` | `derived` | Image built from containers/workspace-mounter/Dockerfile, used by the platform workspace router whenever ASTRABOX_SANDBOX_WORKSPACE_VOLUME is set; runs outside user sandboxes. Unset, it is the release image <ASTRABOX_IMAGE_PREFIX>workspace-mounter:<ASTRABOX_IMAGE_TAG>, which is published for amd64 only. |
| `ASTRABOX_WORKSPACE_MOUNT_ROOT` | `/var/lib/astrabox/workspace-mounts` | Host-side root for mergerfs mount entries and their control state. Separate from the backing workspace volume; never a user workspace. |
| `ASTRABOX_WORKSPACE_STORAGE_TOPOLOGY` | `local` | Workspace storage topology: local for one sandbox host, shared for a filesystem shared by all eligible sandbox hosts. The mergerfs router rejects local storage on multi-node Kubernetes clusters. |
| `DEEPSEEK_API_KEY` | *(none)* | Provider API key for the bundled LiteLLM DeepSeek routes. When the single-endpoint setup points ANTHROPIC_BASE_URL at api.deepseek.com, the bundled gateway uses this credential for the DeepSeek route consumed by both supported client protocols. |
| `GEMINI_API_KEY` | *(none)* | Provider API key for the bundled LiteLLM Gemini routes. It is read directly by the bundled LiteLLM configuration, not by AstraBox code. |
| `LANGFUSE_HOST` | *(none)* | Optional Langfuse API origin consumed by LiteLLM's configured callback. Unset uses the Langfuse SDK's cloud endpoint. |
| `LANGFUSE_PUBLIC_KEY` | *(none)* | Langfuse project public key consumed by LiteLLM's configured callback. The integration stays off unless both Langfuse keys are set. |
| `LANGFUSE_SECRET_KEY` | *(none)* | Langfuse project secret key consumed by LiteLLM's configured callback. The integration stays off unless both Langfuse keys are set. |
| `LITELLM_MASTER_KEY` | *(none)* | Private service credential for the embedded LiteLLM gateway. The container entry point reads it and defaults the sandbox-facing provider credential to it. Empty = a value is generated once into <state_dir>/litellm.key (mode 0600). Multi-replica deployments must set the same value in every replica. LiteLLM's own name, not ASTRABOX_* — it is the proxy's variable, forwarded verbatim. The provider adapter uses the same credential for server-side model and extension operations. |
| `MONGODB_URI` | *(none)* | Plain (non-namespaced) Mongo connection URI; last in the mongo backend's URI resolution chain, after ASTRABOX_DB_URL and ASTRABOX_MONGODB_URI. |
| `OPENAI_API_KEY` | *(none)* | Provider API key for the bundled LiteLLM OpenAI routes. It is read directly by the bundled LiteLLM configuration, not by AstraBox code. |
| `OPENAI_COMPATIBLE_API_KEY` | *(none)* | API key for the OpenAI-compatible service at OPENAI_COMPATIBLE_BASE_URL. It is read directly by the bundled LiteLLM configuration, not by AstraBox code. |
| `OPENAI_COMPATIBLE_BASE_URL` | *(none)* | Base URL of the OpenAI-compatible service behind the bundled LiteLLM route `openai-compatible/*`; an Agent selects `openai-compatible/<model id>`. Set it together with OPENAI_COMPATIBLE_API_KEY. It is read directly by the bundled LiteLLM configuration, not by AstraBox code. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(none)* | OTLP/HTTP endpoint for AstraBox request and orchestration spans. Setting the endpoint enables OpenTelemetry export; an empty value disables it. For Langfuse, use <LANGFUSE_HOST>/api/public/otel and let the exporter append /v1/traces. |
| `OTEL_EXPORTER_OTLP_HEADERS` | *(none)* | Extra headers the OTLP exporter sends (W3C key=value,key2=value2), typically the Authorization=Basic <...> credential for a BYO Langfuse endpoint. Startup diagnostics report whether headers are configured while keeping their values redacted. This setting takes effect when OTEL_EXPORTER_OTLP_ENDPOINT is set. |
| `OTEL_SERVICE_NAME` | `astrabox` | service.name stamped on the OpenTelemetry Resource of every exported span, so this service is distinguishable in the tracing backend; defaults to 'astrabox' when unset. Only read when OTEL_EXPORTER_OTLP_ENDPOINT turns tracing on. |
| `SERVER_ENV` | `community` | Alias of ASTRABOX_ENV used by YAML overlay selection and administrator diagnostics. |

## Advanced tuning

Timeouts, retries, and intervals for deployments that need to tune specific operational behavior.

| Variable | Default | Description |
| --- | --- | --- |
| `ASTRABOX_AGENT_PREWARM_REDIS_URL` | *(none)* | Redis URL passed to OpenSandbox's official client-side pool for distributed idle-capacity coordination. Redis is not AstraBox's product database and stores no workspace, credential, Agent, or Session data. Required when Agent prewarming is enabled; bundled Compose sets it. |
| `ASTRABOX_AGENT_SANDBOX_RENEW_TTL_SECONDS` | `604200` | Lease TTL an Assistant's workspace sandbox is created with and renewed to on activity; a conversation's sandbox uses ASTRABOX_SANDBOX_LEASE_SECONDS. |
| `ASTRABOX_AUTH_EXEMPT_PREFIXES` | `/api/v1/share/,/api/v1/deployments/,/api/v1/sandbox-callback/,/api/v1/sbxcap/,/api/v1/platform-mcp/` | Comma-separated path prefixes that REPLACE the default auth-exempt list wholesale (machine/capability surfaces that carry their own auth). |
| `ASTRABOX_CHANNEL_GATEWAY_HOST` | `127.0.0.1` | Bind host for the messaging-platform gateway process. The bundled process uses loopback; a separately deployed gateway may bind behind its own authenticated HTTPS boundary. |
| `ASTRABOX_CHANNEL_GATEWAY_MANIFEST` | `/opt/astrabox/channel-gateway/channel_gateway_manifest.json` | Path to the shared concrete-provider manifest consumed by the channel gateway. The server image supplies it; override only when running the gateway as a separately packaged service. |
| `ASTRABOX_CHANNEL_GATEWAY_PORT` | `8765` | Loopback TCP port used by the bundled channel adapter gateway. |
| `ASTRABOX_CONFIG_FILE` | `astrabox.toml` | Path to the optional TOML config file. If explicitly set, the file must exist or startup returns an error. |
| `ASTRABOX_DB_HOST` | `postgres` | PostgreSQL service hostname used only with ASTRABOX_DB_PASSWORD_FILE. |
| `ASTRABOX_DB_PASSWORD_FILE` | *(none)* | Compose secret file containing only the AstraBox PostgreSQL role password. The bundled launcher builds ASTRABOX_DB_URL at process start; an explicit URL takes precedence. |
| `ASTRABOX_DB_PORT` | `5432` | PostgreSQL service port used only with ASTRABOX_DB_PASSWORD_FILE. |
| `ASTRABOX_DEFAULT_ORG` | `default` | The implicit organization id every identity belongs to, absent a resolver override. |
| `ASTRABOX_ENV_FILE` | `.env` | Path to the .env file loaded into the process environment at startup and read by the pydantic-settings dotenv source; lets an operator point at an alternate env file. |
| `ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS` | `300` | Poll interval for the background watcher that renews/expires sandboxes nearing lease end. |
| `ASTRABOX_EXPIRATION_WATCHER_THRESHOLD_SECONDS` | `3600` | How far ahead of lease expiry the watcher treats a sandbox as due for renewal. |
| `ASTRABOX_FRONTEND_DIST` | `<repo>/frontend/dist` | Path to the built console SPA served at '/'; set in the server image. Absent means API-only (a from-source checkout that hasn't run `npm run build`). |
| `ASTRABOX_HERMES_LISTEN_PORT` | `9118` | In-box listening port of the Hermes forwarder. Must match the endpoint port declared by the Hermes adapter; changing this image variable alone does not change the platform's endpoint declaration. |
| `ASTRABOX_HERMES_LOOPBACK_PORT` | `9119` | In-box loopback port used by the Hermes backend and its forwarder. Read inside the sandbox image, not from the platform process environment. |
| `ASTRABOX_HERMES_PROFILE_WAIT_SECONDS` | `1800` | Maximum seconds the image-owned Hermes launcher waits for a usable Assistant profile, account and workspace before exiting with status 75. |
| `ASTRABOX_HERMES_UPSTREAM_WAIT_SECONDS` | `1800` | Maximum seconds the in-box Hermes forwarder waits for an HTTP response from the backend before publishing its listener or failing startup. |
| `ASTRABOX_MONGO_RETRY_ATTEMPTS` | `3` | Max attempts for a retried Mongo operation (clamped to 1-6). |
| `ASTRABOX_MONGO_RETRY_BASE_DELAY_SECONDS` | `0.2` | Base backoff delay between retried Mongo operations. |
| `ASTRABOX_PI_API_KEY` | *(none)* | Model gateway credential for a pi sandbox. The image writes "$ASTRABOX_PI_API_KEY" into pi's models.json rather than the value, so pi resolves it at request time and the credential never lands on disk. Under the Credential Vault this carries a placeholder the outbound proxy swaps for the real key. |
| `ASTRABOX_PI_BASE_URL` | *(none)* | OpenAI-compatible base URL of the model gateway a pi sandbox reaches. The image renders it into models.json at boot because pi accepts only a literal baseUrl there. Deployment-level input, so a prewarmed box is already complete before any session claims it. |
| `ASTRABOX_PI_MODEL` | *(none)* | Model id the pi sandbox publishes under its astrabox provider. The adapter starts pi with the matching provider/id reference. |
| `ASTRABOX_REMOTE_CWD` | `/workspace` | Overrides the in-sandbox working directory the agent runs in. The default matches the bundled image workspace; a custom value must remain writable by the image's workload account. |
| `ASTRABOX_REPROVISION_COOLDOWN_SECONDS` | `600` | Minimum interval between reprovision attempts for the same session. |
| `ASTRABOX_RUNNER_INTERACTION_WAIT_SECONDS` | `120` | How long the in-box runner keeps holding an answer slot for a host that is not connected, before it stops waiting. Sent to the runner on every activation. |
| `ASTRABOX_SANDBOX_CONTROL_DEADLINE_S` | `30` | Deadline in seconds for a sandbox probe, connection, or delete operation. Command and Agent streams use their own lifetimes. |
| `ASTRABOX_SANDBOX_DNS_EDGE_SERVICE` | *(none)* | Compose service name of the DNS-only bridge forwarder used as the OpenSandbox egress DNS upstream. The bundled launcher discovers its address from Docker labels and routes sandbox DNS directly to it. |
| `ASTRABOX_SANDBOX_EDGE_CALLBACK_PORT` | `8000` | Port the sandbox-edge container exposes for capability-scoped platform callbacks; the maintained proxy listens on 8000. |
| `ASTRABOX_SANDBOX_EDGE_SERVICE` | *(none)* | Compose service name of the single-purpose sandbox callback/model proxy. The bundled launcher discovers its bridge address from Docker labels. |
| `ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM` | *(none)* | Host-controlled DNS resolver passed only to the OpenSandbox outbound proxy. Maintained Compose points it at a DNS-only bridge container; the standalone server image uses its private CoreDNS listener. Sandbox Environment variables cannot set or override it because DNS controls where a credential-bound hostname is sent. |
| `ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM_DEFAULT` | *(none)* | Fallback DNS address used only when the bundled protected gateway actually starts and no dedicated DNS edge has supplied the active upstream. An external model gateway continues to use ordinary DNS. |
| `ASTRABOX_SANDBOX_ENDPOINT_URL_TTL_SECONDS` | `900` | Lifetime of a browser URL minted through OpenSandbox Secure Access. The accepted range is 60 to 86400 seconds; the default is 15 minutes. |
| `ASTRABOX_SANDBOX_ENDPOINT_VIA_SERVER_PROXY` | `false` | How the open_sandbox backend reaches a sandbox. False (default) takes the direct route: the lifecycle server hands back a published host port and AstraBox dials it — right when AstraBox and the Docker daemon share a network namespace. True asks for endpoints that point at the lifecycle server itself, which relays each request (HTTP, SSE and WebSocket) to the sandbox's container IP on the Docker network — required when AstraBox runs in a container, because the published ports are on the host and the host's loopback is not the container's. The one-container deployment sets it; the trade is that every data-plane byte crosses the server process. |
| `ASTRABOX_SANDBOX_GATEWAY_IP` | *(none)* | Private sandbox-edge address that the bundled CoreDNS resolver maps gateway.astrabox.test to. The maintained Compose launcher discovers it; source-development scripts set it for their temporary edge. |
| `ASTRABOX_SANDBOX_IDLE_ACTION` | `terminate` | Seeds an Environment's own idle_action when one is created without stating it. Every Environment then carries its own value, and that is what a lapsed lease acts on; this setting is not consulted again. 'terminate' destroys the sandbox and its workspace, 'pause' commits the filesystem and frees the compute so the next turn resumes the same sandbox with its files. 'pause' is refused at startup unless the sandbox backend declares it can snapshot. |
| `ASTRABOX_SANDBOX_LEASE_RENEW_THRESHOLD_SECONDS` | `3600` | Renew a conversation's sandbox lease only once less than this remains. |
| `ASTRABOX_SANDBOX_LEASE_SECONDS` | `14400` | The conversation sandbox lease; renewed on turn/mirror activity. An abandoned session dies roughly one lease after its last activity. |
| `ASTRABOX_SANDBOX_OPENAPI_BASE_URL` | *(none)* | Base URL of the OpenSandbox lifecycle API, consumed by the open_sandbox backend (must carry an explicit http:// or https:// scheme — see docs/providers/opensandbox.md). Leave it unset to have the AstraBox image start a lifecycle server of its own alongside the app. |
| `ASTRABOX_SANDBOX_PARKED_RETENTION_SECONDS` | `604800` | Retention period for a paused sandbox whose Environment uses idle_action=pause. Expiry deletes the sandbox record; configure the OCI registry with a matching policy for snapshot images. |
| `ASTRABOX_SANDBOX_READY_TIMEOUT_SECONDS` | `120` | How long the open_sandbox backend waits for a newly created sandbox to report ready. |
| `ASTRABOX_SANDBOX_REQUEST_TIMEOUT_SECONDS` | `15` | HTTP request timeout for the OpenSandbox lifecycle SDK client, consumed by the open_sandbox backend. |
| `ASTRABOX_SANDBOX_SERVER_EXECD_IMAGE` | `opensandbox/execd:v1.1.0` | Image the lifecycle server copies the execd init binary from into every new sandbox. Prewarmed Agent images include the same release. A custom value must be a compatible private-registry mirror. |
| `ASTRABOX_SANDBOX_SERVER_INGRESS_SIGNING_KEY_ID` | `a` | One lowercase letter or digit identifying the active OpenSandbox ingress signing key. Change it as part of a coordinated key rotation. |
| `ASTRABOX_SANDBOX_SERVER_KUBE_IMAGE_PULL_POLICY` | `IfNotPresent` | imagePullPolicy for the sandbox container on the kubernetes runtime: Always, IfNotPresent or Never. Validated at startup because upstream copies the string into the Pod spec unchecked, so a typo would otherwise be a rejected create. |
| `ASTRABOX_SANDBOX_SERVER_KUBE_INFORMER` | `true` | Whether the lifecycle server keeps a watch-backed cache of sandbox workloads (upstream's [beta] kubernetes.informer_enabled, on by default there too). It trades a persistent watch for far fewer API reads; turn it off to take API pressure off a shared control plane. |
| `ASTRABOX_SANDBOX_SERVER_PORT` | `8990` | Loopback port the bundled OpenSandbox lifecycle server listens on, inside the same container as AstraBox. Nothing outside that container can reach it, so this only needs changing if another process in the container wants the port. |
| `ASTRABOX_SANDBOX_SERVER_START_TIMEOUT_SECONDS` | `300` | How long the container entry point waits for a bundled sidecar (the lifecycle server, channel gateway, or model gateway) to answer its health route before giving up. The default covers the model gateway's first boot, which runs its database migrations. A child that exits is reported immediately with its own redacted output. |
| `ASTRABOX_SESSION_CREATION_TIMEOUT_SECONDS` | `300` | Timeout for the full Session creation pipeline (provision and connect). The request returns a timeout error after this limit. |
| `ASTRABOX_STARTUP_SETTLE_RETRY_WINDOW_SECONDS` | `120` | How long the lifecycle worker keeps retrying while a session settles at startup. |
| `ASTRABOX_STREAM_START_TIMEOUT_SECONDS` | `5` | How long the stream-start endpoint waits for the first SSE event before giving up. |
| `ASTRABOX_TITLE_MODEL_MAX_TOKENS` | `256` | Max output tokens for title and process-summary completions, which explicitly disable reasoning. |
| `DATABASE_URL` | *(none)* | Database URL consumed by the embedded LiteLLM process. The maintained Compose launcher builds it from LiteLLM's service-scoped password file; an explicit value is reserved for externally managed deployments. |
| `HOSTNAME` | *(none)* | Container hostname, used as the machine-id fallback when set (falls back further to socket.gethostname() when absent). |
| `LITELLM_DATABASE_HOST` | `postgres` | PostgreSQL service hostname used only with LITELLM_DATABASE_PASSWORD_FILE. |
| `LITELLM_DATABASE_PASSWORD_FILE` | *(none)* | Compose secret file containing only the LiteLLM PostgreSQL role password. The bundled launcher builds LiteLLM's DATABASE_URL at process start; an explicit DATABASE_URL takes precedence. |
| `LITELLM_DATABASE_PORT` | `5432` | PostgreSQL service port used only with LITELLM_DATABASE_PASSWORD_FILE. |

## Sandbox runtime variables

Values prepared by the AstraBox service and read by code inside a sandbox. If one is renamed, update both sides and the matching tests.

| Variable | Default | Description |
| --- | --- | --- |
| `ASTRABOX_HERMES_CONFIG_DEFAULTS` | *(none)* | User-overridable JSON object merged (setdefault mode) into config.yaml; already-present keys are left untouched. |
| `ASTRABOX_HERMES_CONFIG_OVERWRITE` | *(none)* | Platform-owned JSON object force-merged (overwrite mode) into the Hermes profile's config.yaml at every nested key; lists replace wholesale. |
| `ASTRABOX_HERMES_CRON_JOBS_B64` | *(none)* | Base64-encoded JSON array of platform-owned Hermes Cron jobs, reconciled into $HERMES_HOME/cron/jobs.json. |
| `ASTRABOX_HERMES_MODEL_API_KEY` | *(none)* | Credential value read by Hermes' configured custom model provider. With Credential Vault enabled this is a non-secret placeholder and the outbound proxy substitutes the operator-managed credential on the matching outbound request. An explicit Vault opt-out writes the real credential. |
| `ASTRABOX_HERMES_PLUGIN_FILES_B64` | *(none)* | Base64-encoded JSON object of {profile-home-relative path: JSON payload} plugin config files; the profile setup script decodes it and writes each file under the profile home before TUI start. Set host-side from _build_hermes_plugins(...).config_files when the resolved Hermes plugin config declares config files. |
| `ASTRABOX_HERMES_PROFILE_HOME` | *(none)* | Per-profile home directory prepared for the workload account before the conversation's Hermes TUI process starts. |
| `ASTRABOX_HERMES_PROFILE_LINUX_USER` | *(none)* | Per-profile Linux account that owns the profile and runs the Hermes TUI process. Set from the provisioned runtime identity. |
| `ASTRABOX_HERMES_SKILL_SOURCE_DIRS` | *(none)* | JSON array of platform-owned shared skill source directories copied into $HERMES_HOME/skills (only when the user's own copy is missing). |
| `ASTRABOX_HERMES_SOUL_B64` | *(none)* | Base64-encoded UTF-8 SOUL.md content materialized into the Hermes profile home. |
| `ASTRABOX_HERMES_WORKSPACE` | *(none)* | Absolute path of the profile workspace. The setup script creates it and the per-conversation TUI launcher uses it as Hermes' working directory. |
| `ASTRABOX_RUNNER_PORT` | `8000` | Port of the resident Agent runner's WebSocket endpoint inside the sandbox. |
| `ASTRABOX_RUNNER_SPOOL_DIR` | `/tmp/astrabox-runner-spool` | Sandbox directory that buffers Session event batches until they are saved to the platform store. A restarted runner resumes pending transfers from this directory. |
| `ASTRABOX_STORAGE_ENGINE_CONFIG_DIR` | *(none)* | Engine config directory name the image-owned shared-workspace storage helper creates beneath the assistant profile root. |
| `ASTRABOX_STORAGE_LOCAL_ROOT` | *(none)* | Local mount root the image-owned assistant-workspace storage helper probes or verifies. The host supplies /home/conversations for the duration of one helper invocation. |
| `ASTRABOX_STORAGE_PROFILE_ROOT` | *(none)* | Assistant profile directory the image-owned shared-workspace storage helper creates and verifies after the common storage root is mounted. |
| `ASTRABOX_TRANSCRIPT_MIRROR_BATCH_BYTES` | `1048576` | Cap on how many bytes of rollout the transcript mirror sends in one append. A cap, not a quantum: the batch is whatever whole lines fit under it, and its append_id names that exact byte range. |
| `ASTRABOX_TRANSCRIPT_MIRROR_GLOB` | *(none)* | Filenames under the root that are session logs (the engine decides; both current images write `*.jsonl`). Set by the image, required. |
| `ASTRABOX_TRANSCRIPT_MIRROR_NAMESPACE` | *(none)* | Prefix every mirrored scope takes in the platform's transcript store, keeping one engine's logs apart from another's inside one session. Set by the image, required; the rest of a scope is the log's path relative to the root, which is what lets a restore be transcription. |
| `ASTRABOX_TRANSCRIPT_MIRROR_POLL_SECONDS` | `0.5` | How long the transcript mirror waits after finding nothing new. It bounds how much of a session a reclaimed box can take with it, but not below one rollout item: Codex writes a line when an item completes, never per streamed delta. |
| `ASTRABOX_TRANSCRIPT_MIRROR_ROOT` | *(none)* | Absolute directory in the box under which this image's engine writes its session logs. Set by the image, required: the relay holds no engine knowledge, and an unset root would mirror nothing while looking healthy. |
| `ASTRABOX_TRANSCRIPT_MIRROR_STATE_DIR` | `/tmp/astrabox-transcript-mirror` | In-box directory where the transcript mirror records, per rollout file, the byte offset already stored and the range of any batch in flight. The in-flight record is written (fsync) before the request, so a restarted mirror re-sends that exact range under its original append_id rather than opening a gap or a duplicate. |
| `ASTRABOX_TRANSCRIPT_MIRROR_TARGET_FILE` | *(none)* | In-box file the transcript mirror reads its per-session target from, for a box prepared before its Session exists: the create names this file instead of the three `_ASTRABOX_TRANSCRIPT_*` values, the claim writes the same values into it before the engine conversation is created, and the mirror relays nothing until the file holds a usable target. Mutually exclusive with the per-session variables. |
| `ASTRABOX_TRANSCRIPT_MIRROR_TARGET_GRACE_SECONDS` | `120` | How long the transcript mirror lets a session log (or an unusable target file) exist without a usable deferred target before exiting FATAL. In a healthy claim the target is written before the engine conversation is created, so this clock never starts; expiring means conversation bytes exist with no destination, which must show as a failed service rather than an idle one. |
| `ASTRABOX_TRANSCRIPT_MIRROR_TIMEOUT_SECONDS` | `30` | Per-request timeout for the transcript mirror's appends. A timeout is retried from the same offset under the same append_id, which the store answers with the sequence it already assigned. |
| `CONV_CACHE` | *(none)* | Conversation-scoped writable cache directory prepared by the bootstrap. |
| `CONV_CONFIG` | *(none)* | Optional conversation-scoped engine configuration directory prepared by the bootstrap. |
| `CONV_DEFAULT_REPO_BRANCH` | *(none)* | Optional branch selected for the conversation's default repository clone. |
| `CONV_DEFAULT_REPO_DEPTH` | *(none)* | Optional positive clone depth for the conversation's default repository. |
| `CONV_DEFAULT_REPO_HTTPS_TOKEN` | *(none)* | HTTPS credential used only while cloning the default repository; bootstrap diagnostics redact it before reporting a failure. |
| `CONV_DEFAULT_REPO_KEY_B64` | *(none)* | Base64-encoded SSH deploy key used only while cloning the default repository. |
| `CONV_DEFAULT_REPO_TARGET` | `derived` | Default repository checkout target, derived from CONV_WORKSPACE when omitted. |
| `CONV_DEFAULT_REPO_URL` | *(none)* | Default repository URL cloned into a newly prepared conversation workspace. |
| `CONV_GID` | *(none)* | Optional numeric gid allocated for a conversation in a shared sandbox; it must be supplied together with CONV_UID. |
| `CONV_HOME` | *(none)* | Absolute home directory assigned to the conversation workload account. |
| `CONV_PLUGIN_CACHE_DIR` | *(none)* | Prepared Agent plugin cache directory verified before conversation links are made. |
| `CONV_PLUGIN_CACHE_HASH` | *(none)* | Expected content hash of the prepared Agent plugin cache. |
| `CONV_PLUGIN_LINKS_B64` | *(none)* | Base64-encoded table of prepared Agent plugin-cache paths linked into the conversation config tree. |
| `CONV_SKILL_CACHE_DIR` | `/opt/conversation-runtime/claude-skills-cache` | Prepared Agent Skill cache directory linked into the conversation config tree. |
| `CONV_SKILL_MANIFEST_B64` | *(none)* | Base64-encoded Skill manifest used to verify and link the prepared Agent Skill cache. |
| `CONV_TMP` | *(none)* | Conversation-scoped temporary directory prepared by the bootstrap. |
| `CONV_UID` | *(none)* | Optional numeric uid allocated for a conversation in a shared sandbox; it must be supplied together with CONV_GID. |
| `CONV_USER` | *(none)* | Linux account the image-owned conversation bootstrap verifies or creates. |
| `CONV_WORKSPACE` | *(none)* | Absolute writable workspace the conversation bootstrap prepares and verifies. |
| `GIT_HTTPS_TOKEN` | *(none)* | Invocation-local copy of CONV_DEFAULT_REPO_HTTPS_TOKEN read by the temporary Git askpass helper; it is not a deployment setting. |
| `HERMES_DASHBOARD_SESSION_TOKEN` | *(none)* | Credential the in-box Hermes backend checks a WebSocket upgrade against, written into the Assistant profile's env file and read by the image-owned service that starts `hermes serve`. Vendor-named: it is the variable Hermes' own desktop shell injects for the same purpose. Absent, the backend mints a random one per start and no caller can present it. |
| `HERMES_HOME` | `/root/.hermes` | Root directory of the Hermes profile the config-merge runtime materializes into. |
| `REDACT_SECRET` | *(none)* | Invocation-local secret used to redact the repository HTTPS credential from bootstrap diagnostics. |

## Local development

Options reserved for local development and debugging. Production security settings are listed under Deployment settings.

| Variable | Default | Description |
| --- | --- | --- |
| `ASTRABOX_ALLOW_PLAINTEXT_MODEL_API_KEY` | `false` | Gate that allows a plaintext model API key stored inside the environment's provider_access to be honored outside local mode (local mode already implies it). |
| `ASTRABOX_ALLOW_PLAINTEXT_SANDBOX_API_KEY` | `false` | Gate that allows a plaintext ASTRABOX_SANDBOX_API_KEY to be honored outside local mode (local mode already implies it). |
| `ASTRABOX_ALLOW_UNAUTHENTICATED_BIND` | `false` | Explicit override for the serve-time guard that refuses a non-loopback bind while no identity resolver is configured (the no-auth deployment drives a root-equivalent Docker socket). In-container the guard only logs CRITICAL — the published port decides exposure there. |
| `ASTRABOX_LOCAL_AVATAR_URL` | *(none)* | Avatar URL for the default local identity. |
| `ASTRABOX_LOCAL_DEBUG_USER_ENABLED` | `false` | Enables the env-driven default debug user; only takes effect when ASTRABOX_LOCAL_MODE is also set. |
| `ASTRABOX_LOCAL_DISPLAY_NAME` | *(none)* | Display name for the default local identity; falls back to the resolved user id. |
| `ASTRABOX_LOCAL_EMAIL` | *(none)* | Email for the default local identity. |
| `ASTRABOX_LOCAL_USER_ID` | *(none)* | User id for the default local identity; falls back to $USER, then 'local-user'. |
| `ASTRABOX_SANDBOX_API_KEY` | *(none)* | Plaintext sandbox API key override; only honored when allow_plaintext_sandbox_api_key() is true (local mode or ASTRABOX_ALLOW_PLAINTEXT_SANDBOX_API_KEY), else logged and ignored. |
| `USER` | *(none)* | POSIX username; a fallback for the default local identity's user id. |

## Automated tests

Fault-injection switches reserved for the automated end-to-end test suite.

| Variable | Default | Description |
| --- | --- | --- |
| `ASTRABOX_E2E_FAULTS` | `false` | Arms E2E fault-injection support (turn-terminal-drop, transcript-append-5xx, sandbox-egress). This setting is reserved for the automated test deployment. |
| `ASTRABOX_E2E_TURN_TERMINAL_DROP_FAULT_FILE` | `/tmp/astrabox-e2e-turn-terminal-drop-faults.json` | Shared E2E fault-file base path; the backend also scans <base>.d/*.json. Only read when ASTRABOX_E2E_FAULTS is armed. |

## Dynamic / pattern-based

Entries below have names assembled at runtime. Each naming pattern is listed
once.

| Variable | Default | Description |
| --- | --- | --- |
| `<SECRET_NAME_UPPER>` | *(none)* | Dynamic: SecretProvider.get_secret(secret_name) reads secret_name.upper().replace('-', '_') from the environment for whatever secret_name a *_secret_name setting names (ASTRABOX_SANDBOX_API_KEY_SECRET_NAME, ASTRABOX_MODEL_API_KEY_SECRET_NAME, ASTRABOX_GIT_HTTPS_TOKEN_SECRET_NAME, ASTRABOX_TITLE_MODEL_API_KEY_SECRET_NAME, or an environment's own provider_access.api_key_secret_name) — the operator picks the name, so no single literal env var represents it. |
