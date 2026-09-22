"""Run OpenSandbox's lifecycle API as the AstraBox sandbox control plane.

The launcher renders upstream configuration from ``ASTRABOX_*`` settings and
runs the bundled server on loopback. OpenSandbox remains a separate process so
Docker and Kubernetes share its published HTTP contract while its synchronous
SDK work stays outside the AstraBox event loop.

This module owns four integration boundaries:

* configuration rendering and validation for the selected runtime;
* Docker-only hardening for published ports and metadata storage where upstream
  exposes no public setting;
* a Docker proxy-host compatibility hook for OpenSandbox 0.2.x; and
* Kubernetes startup checks for the API server, namespace, and workload CRD.

On one Docker host, sandbox traffic uses OpenSandbox execd's native port proxy.
On Kubernetes, operators can deploy OpenSandbox's ingress gateway and configure
this launcher to return its cluster-wide routes. The bundled lifecycle server
binds to ``127.0.0.1`` because its control API is not exposed as an authenticated
public service. Deployments using a separately operated server configure
``ASTRABOX_SANDBOX_OPENAPI_BASE_URL`` instead of this launcher.

Per-sandbox resource limits belong to create requests. Kubernetes security
contexts, runtime classes, and PID limits belong to the cluster workload
template. Keeping those settings at their owning layer prevents this launcher
from presenting configuration that upstream cannot apply.

The Docker hardening hooks rebind upstream module constants and verify the
result through its public API. Replace each hook with public configuration when
upstream provides the corresponding field.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Final, Iterable
from uuid import uuid4

from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)

#: The optional extra that installs ``opensandbox-server`` (and its docker /
#: kubernetes / redis dependency tail).
EXTRA_NAME = "sandbox-server"

#: Bind address of the lifecycle API. Not an env knob — see the module docstring.
SERVER_BIND_HOST = "127.0.0.1"

SERVER_PORT_ENV = "ASTRABOX_SANDBOX_SERVER_PORT"
#: Loopback-inside-the-container, so a collision is only possible with another
#: process in the same container. Deliberately not upstream's 8080, which
#: collides with half of everything when the launcher is run on a dev host.
DEFAULT_SERVER_PORT = "8990"

METADATA_DIR_ENV = "ASTRABOX_SANDBOX_SERVER_METADATA_DIR"
#: Directory under AstraBox's ``state_dir`` used when the env is unset.
DEFAULT_METADATA_DIRNAME = "opensandbox/metadata"
#: Filename under the same parent for upstream's ``store`` (see
#: :func:`config_document` for why it is set but unused).
SNAPSHOT_STORE_FILENAME = "opensandbox.db"
#: Filename of the rendered TOML, next to the metadata directory.
CONFIG_FILENAME = "server.toml"
#: The Kubernetes ingress signing key is rendered into this file, so it is a
#: secret-bearing deployment artifact whenever Secure Access is enabled.
CONFIG_FILE_MODE = 0o600

RUNTIME_ENV = "ASTRABOX_SANDBOX_SERVER_RUNTIME"
#: Sandboxes are containers on the daemon whose socket this container shares.
RUNTIME_DOCKER = "docker"
#: Sandboxes are Pods, reconciled from ``BatchSandbox`` custom resources by
#: upstream's controller, which the cluster runs.
RUNTIME_KUBERNETES = "kubernetes"
#: Default runtime when ``ASTRABOX_SANDBOX_SERVER_RUNTIME`` is unset.
DEFAULT_RUNTIME = RUNTIME_DOCKER
#: Exactly upstream's own ``RuntimeConfig.type`` literal set.
_RUNTIMES = (RUNTIME_DOCKER, RUNTIME_KUBERNETES)

NETWORK_MODE_ENV = "ASTRABOX_SANDBOX_SERVER_NETWORK_MODE"
#: ``bridge``, not upstream's ``host`` default — and ``host`` is not merely a bad
#: default here, it is refused outright (see :func:`network_mode`).
DEFAULT_NETWORK_MODE = "bridge"

CREDENTIAL_VAULT_ENV = "ASTRABOX_SANDBOX_CREDENTIAL_VAULT"
EGRESS_DNS_UPSTREAM_ENV = "ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM"
# Upstream's sidecar-facing name. AstraBox never accepts this name from an
# Environment; the executor derives it from EGRESS_DNS_UPSTREAM_ENV.
UPSTREAM_DNS_ENV = "OPENSANDBOX_EGRESS_DNS_UPSTREAM"

EGRESS_IMAGE_ENV = "ASTRABOX_SANDBOX_EGRESS_IMAGE"
EGRESS_MODE_ENV = "ASTRABOX_SANDBOX_EGRESS_MODE"

#: The sidecar upstream publishes, pinned like every other image this module
#: names. It is available by default but is added to a sandbox only when that
#: sandbox requests provider network enforcement or protected credentials.
DEFAULT_EGRESS_IMAGE = "opensandbox/egress:v1.1.7"

#: Upstream's own two modes. `dns` resolves names and filters on them; `dns+nft`
#: adds packet-level rules, and is the only one the Credential Vault will run
#: under — a direct-IP connection bypasses a DNS-only policy, and a credential
#: broker that can be bypassed is worse than none.
_EGRESS_MODES = ("dns", "dns+nft")

SECURE_RUNTIME_ENV = "ASTRABOX_SANDBOX_SECURE_RUNTIME"

#: Upstream's own vocabulary (`[secure_runtime] type`), not a second naming.
#: Empty is runc, which is what a deployment gets by not choosing.
_SECURE_RUNTIME_TYPES = ("", "gvisor", "kata", "firecracker")

#: What the chosen type is called to each runtime. Upstream keeps two fields
#: because Docker names a runtime and Kubernetes names a RuntimeClass, and the
#: conventional name is the same string in both.
_SECURE_RUNTIME_DOCKER_NAME = {
    "gvisor": "runsc",
    "kata": "kata-runtime",
    "firecracker": "firecracker",
}

EXECD_IMAGE_ENV = "ASTRABOX_SANDBOX_SERVER_EXECD_IMAGE"
#: The official execd init image paired with the supported OpenSandbox server.
#: Every standard create receives this server-side injection, including creates
#: issued by AstraBox's OpenSandbox SDK client-pool creator.
DEFAULT_EXECD_IMAGE = "opensandbox/execd:v1.1.0"

PORT_RANGE_ENV = "ASTRABOX_SANDBOX_SERVER_PORT_RANGE"
#: Below the kernel's ephemeral range, deliberately. Each sandbox consumes 2-3
#: host ports, and a published port and an outgoing connection's source port
#: come from the same 65535 numbers: overlap the two pools and `docker start`
#: eventually loses the race to a socket the kernel handed out a millisecond
#: earlier, with "address already in use" on a port nothing appears to own.
#: Upstream's own span (40000-60000) sits entirely inside the Linux default
#: (32768-60999), so the collision is a function of load, not luck.
DEFAULT_PORT_RANGE = "20000-32000"
#: Where Linux publishes the range it draws outgoing source ports from.
EPHEMERAL_PORT_RANGE_FILE = "/proc/sys/net/ipv4/ip_local_port_range"

#: Backend-neutral env names describing the container the agent runs in, read
#: here so a deployment that caps PIDs or moves the publish interface says it
#: once. Upstream would otherwise fill these from its own,
#: different defaults (4096 PIDs; a 0.0.0.0 publish).
PIDS_LIMIT_ENV = "ASTRABOX_SANDBOX_PIDS_LIMIT"
DEFAULT_PIDS_LIMIT = "512"
PUBLISH_HOST_IP_ENV = "ASTRABOX_PUBLISH_HOST_IP"
#: Loopback by default: see :func:`publish_host_ip` for why.
DEFAULT_PUBLISH_HOST_IP = "127.0.0.1"

LOG_LEVEL_ENV = "ASTRABOX_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "info"

# --- the Kubernetes runtime's own knobs -------------------------------------
# All inert under the Docker runtime, and a set-but-inert knob is warned about
# rather than left to look effective (see :func:`warn_about_inert_knobs`).

KUBECONFIG_ENV = "ASTRABOX_SANDBOX_SERVER_KUBECONFIG"
#: Filename of the derived kubeconfig, next to the rendered ``server.toml``. Only
#: written when :data:`KUBE_API_SERVER_ENV` asks for a different API-server
#: address; see :func:`kubeconfig_for_server`.
KUBECONFIG_FILENAME = "kubeconfig.yaml"
#: Mode of that derived file. It carries whatever credentials the source carried.
_KUBECONFIG_MODE = 0o600

KUBE_API_SERVER_ENV = "ASTRABOX_SANDBOX_SERVER_KUBE_API_SERVER"
#: Unset — the kubeconfig is used exactly as provided. There is no default
#: address to rewrite to, because no address is safe to guess: see
#: :func:`kubeconfig_for_server`.
DEFAULT_KUBE_API_SERVER = ""

KUBE_NAMESPACE_ENV = "ASTRABOX_SANDBOX_SERVER_KUBE_NAMESPACE"
#: Upstream's own example value. It must already exist — see
#: :func:`verify_kubernetes_access`.
DEFAULT_KUBE_NAMESPACE = "opensandbox"

KUBE_WORKLOAD_PROVIDER_ENV = "ASTRABOX_SANDBOX_SERVER_KUBE_WORKLOAD_PROVIDER"
#: The provider upstream registers first, ships a controller Helm chart for, and
#: writes its Kubernetes example config against. It is the one this deployment
#: has been exercised on.
DEFAULT_KUBE_WORKLOAD_PROVIDER = "batchsandbox"
#: The custom resource each registered provider creates, as ``(group, version,
#: plural)`` — read off the providers themselves, which set exactly these three
#: on ``self``. Held here because :func:`verify_kubernetes_access` must ask the
#: API server about the right resource before upstream's provider is built, and
#: because an unknown provider name has to be refused with the known ones named
#: rather than deferred to a create-time ``ValueError``.
WORKLOAD_RESOURCES: dict[str, tuple[str, str, str]] = {
    "batchsandbox": ("sandbox.opensandbox.io", "v1alpha1", "batchsandboxes"),
    "agent-sandbox": ("agents.x-k8s.io", "v1alpha1", "sandboxes"),
}

KUBE_IMAGE_PULL_POLICY_ENV = "ASTRABOX_SANDBOX_SERVER_KUBE_IMAGE_PULL_POLICY"
DEFAULT_KUBE_IMAGE_PULL_POLICY = "IfNotPresent"
#: Kubernetes' own three; upstream passes the string through to the Pod spec, so
#: a typo would be a create-time API rejection instead of a startup refusal.
_IMAGE_PULL_POLICIES = ("Always", "IfNotPresent", "Never")

KUBE_INFORMER_ENV = "ASTRABOX_SANDBOX_SERVER_KUBE_INFORMER"
#: Upstream's own default for ``kubernetes.informer_enabled``.
DEFAULT_KUBE_INFORMER = "true"

KUBE_CREATE_TIMEOUT_ENV = "ASTRABOX_SANDBOX_SERVER_KUBE_CREATE_TIMEOUT_SECONDS"
#: Upstream's own default for ``kubernetes.sandbox_create_timeout_seconds``.
#: Kept explicit because a cold Kubernetes node can spend longer than this
#: joining the cluster and pulling the agent image.
DEFAULT_KUBE_CREATE_TIMEOUT_SECONDS = "60"

# OpenSandbox's multi-node data plane. The gateway component is installed in
# the cluster; this launcher configures the lifecycle server to return its
# routes and to mint OSEP-0011 signed URLs.
INGRESS_MODE_ENV = "ASTRABOX_SANDBOX_SERVER_INGRESS_MODE"
DEFAULT_INGRESS_MODE = "direct"
_INGRESS_MODES = ("direct", "gateway")
INGRESS_GATEWAY_ADDRESS_ENV = "ASTRABOX_SANDBOX_SERVER_INGRESS_GATEWAY_ADDRESS"
INGRESS_ROUTE_MODE_ENV = "ASTRABOX_SANDBOX_SERVER_INGRESS_ROUTE_MODE"
DEFAULT_INGRESS_ROUTE_MODE = "uri"
_BROWSER_INGRESS_ROUTE_MODES = ("uri", "wildcard")
SECURE_ACCESS_ENV = "ASTRABOX_SANDBOX_SECURE_ACCESS"
INGRESS_SIGNING_KEY_ENV = "ASTRABOX_SANDBOX_SERVER_INGRESS_SIGNING_KEY"
INGRESS_SIGNING_KEY_ID_ENV = "ASTRABOX_SANDBOX_SERVER_INGRESS_SIGNING_KEY_ID"
DEFAULT_INGRESS_SIGNING_KEY_ID = "a"

#: Knobs that only mean something under one runtime, so the other can say so.
_DOCKER_ONLY_ENV = (
    NETWORK_MODE_ENV,
    PORT_RANGE_ENV,
    PIDS_LIMIT_ENV,
    PUBLISH_HOST_IP_ENV,
    EGRESS_DNS_UPSTREAM_ENV,
)
_KUBERNETES_ONLY_ENV = (
    KUBECONFIG_ENV,
    KUBE_API_SERVER_ENV,
    KUBE_NAMESPACE_ENV,
    KUBE_WORKLOAD_PROVIDER_ENV,
    KUBE_IMAGE_PULL_POLICY_ENV,
    KUBE_INFORMER_ENV,
    INGRESS_MODE_ENV,
    INGRESS_GATEWAY_ADDRESS_ENV,
    INGRESS_ROUTE_MODE_ENV,
    INGRESS_SIGNING_KEY_ENV,
    INGRESS_SIGNING_KEY_ID_ENV,
)

#: Upstream's non-interactive acknowledgement that ``server.api_key`` is empty.
#: Without it, startup is blocked (it prompts on a TTY and refuses otherwise).
#: Accurate here rather than a rubber stamp: the API is bound to loopback inside
#: one container and AstraBox is the only process that can reach it, so a key
#: would have to live beside its only client and would guard nothing.
_INSECURE_SERVER_ENV = "OPENSANDBOX_INSECURE_SERVER"
_INSECURE_SERVER_ACK = "YES"
#: Upstream's config-file locator.
_CONFIG_PATH_ENV = "SANDBOX_CONFIG_PATH"


class SandboxServerConfigError(RuntimeError):
    """This deployment is misconfigured; the sandbox server must not start.

    One failure shape for every refusal below, so the orchestrator can report
    "the sandbox server could not be configured" with the message intact.
    """


def _env(name: str, default: str) -> str:
    return str(os.environ.get(name) or default).strip()


#: Spellings accepted for a boolean knob, matching what the rest of AstraBox's
#: settings layer accepts. Anything else is refused rather than read as false —
#: ``informer=off`` silently meaning "on" is the failure this prevents.
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _flag(name: str, default: str) -> bool:
    configured = _env(name, default)
    lowered = configured.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise SandboxServerConfigError(
        f"{name}={configured!r} must be a boolean "
        f"({'/'.join(sorted(_TRUE))} or {'/'.join(sorted(_FALSE))})"
    )


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def sandbox_runtime() -> str:
    """Which upstream runtime this server drives: ``docker`` or ``kubernetes``.

    The whole selector for the two shapes. Validated against upstream's own
    ``RuntimeConfig.type`` literal set here, so a typo is a refusal naming
    :data:`RUNTIME_ENV` rather than a pydantic error naming ``runtime.type``.
    """
    configured = _env(RUNTIME_ENV, DEFAULT_RUNTIME)
    runtime = configured.lower()
    if runtime not in _RUNTIMES:
        raise SandboxServerConfigError(
            f"{RUNTIME_ENV}={configured!r} is not a sandbox runtime. Use "
            f"{' or '.join(repr(name) for name in _RUNTIMES)} "
            f"(default {DEFAULT_RUNTIME!r})."
        )
    return runtime


def execd_image() -> str:
    """Image that supplies execd to every standard sandbox create.

    OpenSandbox's init container runs ``./execd`` from the init image's own
    working directory, so an arbitrary Agent image is not a valid substitute
    merely because it also contains that binary. Use the upstream image by
    default; an operator may override it with a compatible registry mirror.
    """

    return _env(EXECD_IMAGE_ENV, DEFAULT_EXECD_IMAGE)


def server_port() -> int:
    """The loopback port the lifecycle API listens on."""
    configured = _env(SERVER_PORT_ENV, DEFAULT_SERVER_PORT)
    try:
        port = int(configured)
    except ValueError as exc:
        raise SandboxServerConfigError(
            f"{SERVER_PORT_ENV}={configured!r} must be a TCP port number"
        ) from exc
    if not 1 <= port <= 65535:
        raise SandboxServerConfigError(
            f"{SERVER_PORT_ENV}={configured!r} must be between 1 and 65535"
        )
    return port


def lifecycle_base_url() -> str:
    """The base URL AstraBox's ``open_sandbox`` backend must be pointed at.

    The one place the loopback bind and the configured port are composed, so the
    launcher and the orchestrator cannot disagree about the address. No ``/v1``:
    upstream mounts its routers both bare and under that prefix, and the SDK
    appends it itself.
    """
    return f"http://{SERVER_BIND_HOST}:{server_port()}"


#: Upstream's liveness route. Mounted at the root (not under ``/v1``) and listed
#: in the auth middleware's exempt paths, so it answers before any API key would.
HEALTH_PATH = "/health"


def health_url() -> str:
    """The URL :mod:`astrabox.deploy.onebox` polls to know the server is up."""
    return f"{lifecycle_base_url()}{HEALTH_PATH}"


#: Modes that place every sandbox in a single shared network namespace.
_SHARED_NETNS_MODES = frozenset({"host"})
_SHARED_NETNS_PREFIX = "container:"
#: Publishes no ports at all, so nothing can reach the in-box control server.
_UNREACHABLE_MODES = frozenset({"none"})


def network_mode() -> str:
    """The validated Docker network mode: ``bridge`` or a user-defined network.

    ``host`` and ``container:<id>`` are refused, not merely defaulted away from.
    Every sandbox binds the same fixed in-box ports (execd on 44772, the AIO
    file/terminal server on 8080) and upstream resolves a host-networked
    sandbox's endpoint to a bare ``<host>:<in-box port>`` carrying no sandbox
    identity, so under a shared network namespace every live sandbox resolves to
    the same address: session B's data plane, file panel and in-box control
    calls would all land in session A's box. That is cross-session data access,
    not a port collision, and no configuration of this deployment can make it
    safe while the in-box ports are fixed.

    ``none`` is refused for the opposite reason: it publishes no host ports, so
    the sandbox is created and then unreachable.
    """
    configured = _env(NETWORK_MODE_ENV, DEFAULT_NETWORK_MODE)
    # Upstream lower-cases the mode before comparing, so this check must too.
    mode = configured.lower()
    if mode in _SHARED_NETNS_MODES or mode.startswith(_SHARED_NETNS_PREFIX):
        raise SandboxServerConfigError(
            f"{NETWORK_MODE_ENV}={configured!r} is not usable: it puts every "
            "sandbox in one shared network namespace, and because every sandbox "
            "binds the same fixed in-box ports, endpoint resolution would return "
            "the SAME address for all of them — one session's data plane would "
            "reach another session's box. Use 'bridge' (the default) or the name "
            "of a user-defined Docker network."
        )
    if mode in _UNREACHABLE_MODES:
        raise SandboxServerConfigError(
            f"{NETWORK_MODE_ENV}={configured!r} is not usable: it publishes no "
            "host ports, so a created sandbox's in-box control server would be "
            "unreachable. Use 'bridge' (the default) or the name of a "
            "user-defined Docker network."
        )
    if mode != DEFAULT_NETWORK_MODE and _flag(CREDENTIAL_VAULT_ENV, "true"):
        raise SandboxServerConfigError(
            f"{NETWORK_MODE_ENV}={configured!r} cannot be used while Credential "
            "Vault is on: OpenSandbox 0.2.x supports networkPolicy and its egress "
            "sidecar only on Docker's 'bridge' network. Use 'bridge', or turn "
            f"{CREDENTIAL_VAULT_ENV} off explicitly."
        )
    return configured


def port_range() -> tuple[int, int]:
    """The host port range sandbox ports are published into, as ``min-max``.

    Validated against upstream's own bounds here so the failure names
    ``ASTRABOX_SANDBOX_SERVER_PORT_RANGE`` instead of the ``docker.port_range_*``
    TOML keys no operator of this deployment ever wrote.
    """
    configured = _env(PORT_RANGE_ENV, DEFAULT_PORT_RANGE)
    parts = configured.split("-")
    values: list[int] = []
    if len(parts) == 2:
        try:
            values = [int(part.strip()) for part in parts]
        except ValueError:
            values = []
    if len(values) != 2:
        raise SandboxServerConfigError(
            f"{PORT_RANGE_ENV}={configured!r} must be two port numbers as "
            f"'min-max' (default {DEFAULT_PORT_RANGE})"
        )
    low, high = values
    if not (1024 <= low <= 65535 and 1024 <= high <= 65535):
        raise SandboxServerConfigError(
            f"{PORT_RANGE_ENV}={configured!r}: both ends must be between 1024 and 65535"
        )
    if high - low < 100:
        raise SandboxServerConfigError(
            f"{PORT_RANGE_ENV}={configured!r} must span at least 100 ports "
            "(each sandbox consumes 2-3 of them)"
        )
    _refuse_ephemeral_overlap(configured, low, high)
    return low, high


def _refuse_ephemeral_overlap(configured: str, low: int, high: int) -> None:
    """Refuse a range the kernel also hands out as outgoing source ports.

    Overlap does not fail here; it fails much later, on one sandbox out of
    however many the deployment happens to start at once, as a Docker
    "address already in use" for a port no container holds. Reading the
    kernel's own answer turns that into a startup error naming the variable.

    A range the host does not publish is left unchecked: without the proc
    entry there is no answer to compare against, and inventing one would refuse
    correct configurations.
    """

    try:
        raw = Path(EPHEMERAL_PORT_RANGE_FILE).read_text()
    except OSError:
        return
    parts = raw.split()
    if len(parts) != 2:
        return
    try:
        ephemeral_low, ephemeral_high = (int(part) for part in parts)
    except ValueError:
        return
    if high < ephemeral_low or low > ephemeral_high:
        return
    raise SandboxServerConfigError(
        f"{PORT_RANGE_ENV}={configured!r} overlaps this host's ephemeral port "
        f"range ({ephemeral_low}-{ephemeral_high}, from "
        f"{EPHEMERAL_PORT_RANGE_FILE}). Published sandbox ports and outgoing "
        "connections would be drawn from the same pool, and sandbox creation "
        "fails under concurrency with 'address already in use'. Choose a span "
        f"outside it (default {DEFAULT_PORT_RANGE}), or widen the kernel range."
    )


def pids_limit() -> int:
    """``docker.pids_limit`` — the per-sandbox PID cap this deployment applies."""
    configured = _env(PIDS_LIMIT_ENV, DEFAULT_PIDS_LIMIT)
    try:
        value = int(configured)
    except ValueError as exc:
        raise SandboxServerConfigError(
            f"{PIDS_LIMIT_ENV}={configured!r} must be an integer number of processes"
        ) from exc
    if value <= 0:
        raise SandboxServerConfigError(f"{PIDS_LIMIT_ENV}={configured!r} must be positive")
    return value


def egress() -> dict[str, Any]:
    """``[egress]`` — the per-sandbox sidecar that can enforce a network policy.

    Configuring it does not put a sidecar in anything: upstream injects one only
    when a create asks for provider network enforcement or protected credential
    delivery, so this is the deployment saying which image it would use and how
    strictly, not turning something on.

    Off by default, and off means the block is absent rather than present and
    empty — upstream validates ``image`` as non-empty when the block is there.
    """
    image = _env(EGRESS_IMAGE_ENV, DEFAULT_EGRESS_IMAGE).strip()
    mode = _env(EGRESS_MODE_ENV, "dns+nft").strip().lower()
    if mode not in _EGRESS_MODES:
        raise SandboxServerConfigError(
            f"{EGRESS_MODE_ENV}={mode!r} must be one of {', '.join(repr(m) for m in _EGRESS_MODES)}"
        )
    if not image:
        return {}
    return {"image": image, "mode": mode}


def secure_runtime() -> dict[str, str]:
    """``[secure_runtime]`` — run every sandbox under a hardened runtime.

    This is upstream's answer to "how isolated is a sandbox", and it is a
    deployment-wide choice, not a per-sandbox one: the create request carries no
    capability, privilege or `no_new_privileges` field at all. A deployment that
    needs two hardening levels needs two OpenSandbox deployments. An SDK client
    pool issues the same standard creates against its selected deployment; it
    does not create a second runtime-policy boundary.

    Empty means runc, which is what not choosing gets. Anything else must be a
    runtime the host or cluster actually has: the lifecycle server validates that
    at startup and refuses to run without it, so a misconfiguration is a server
    that will not boot rather than sandboxes that quietly run unhardened.
    """
    configured = _env(SECURE_RUNTIME_ENV, "").strip().lower()
    if configured not in _SECURE_RUNTIME_TYPES:
        raise SandboxServerConfigError(
            f"{SECURE_RUNTIME_ENV}={configured!r} must be one of "
            f"{', '.join(repr(t) for t in _SECURE_RUNTIME_TYPES if t)} (or empty for runc)"
        )
    if not configured:
        return {}
    return {
        "type": configured,
        "docker_runtime": _SECURE_RUNTIME_DOCKER_NAME[configured],
        "k8s_runtime_class": configured,
    }


def metadata_dir() -> Path:
    """Directory for OpenSandbox's per-sandbox metadata store.

    The default is below AstraBox's resolved ``state_dir`` so lease-renewal
    metadata shares the deployment's persistent state root with the SQLite
    database and vault key. Deployments that require renewed sandbox leases to
    survive restarts must place that root, or an explicit metadata directory,
    on persistent storage (see :func:`warn_if_metadata_dir_is_ephemeral`).
    """
    configured = _env(METADATA_DIR_ENV, "")
    if configured:
        return Path(configured).expanduser()
    from astrabox.config.settings import get_settings

    return get_settings().resolved_state_dir() / DEFAULT_METADATA_DIRNAME


def publish_host_ip() -> str:
    """The host interface published sandbox ports bind to.

    A host-side lifecycle server can use the loopback default. A lifecycle
    server inside a container must set this to a Docker-host address it can
    reach, normally the bridge gateway. OpenSandbox routes protected sandboxes
    through host-mapped sidecar ports, so that address is used both for the
    Docker bind and for the lifecycle server's internal proxy connection.

    Keep it narrower than ``0.0.0.0``. The published ports include the sandbox
    HTTP service and execd. ``0.0.0.0`` is accepted for deployments that put
    their own authenticated proxy or firewall in front, but emits a warning.

    An explicit value must be an IP address (Docker's ``HostIp`` is an address,
    not a name).
    """
    configured = _env(PUBLISH_HOST_IP_ENV, DEFAULT_PUBLISH_HOST_IP)
    try:
        address = ipaddress.ip_address(configured)
    except ValueError as exc:
        raise SandboxServerConfigError(
            f"{PUBLISH_HOST_IP_ENV}={configured!r} must be an IP address "
            "(Docker publishes to an address, not a hostname)."
        ) from exc
    if address.is_unspecified:
        logger.warning(
            "%s=%s publishes every sandbox's host ports on ALL interfaces. "
            "Those ports carry execd and the in-box file/terminal server, "
            "which have no authentication: on a host with a LAN interface this "
            "lets anyone on the network read the workspace and drive the agent. "
            "AstraBox itself does not need them — it reaches sandboxes through "
            "the lifecycle server's proxy — so leave %s unset unless something "
            "else fronts those ports with real auth.",
            PUBLISH_HOST_IP_ENV,
            configured,
            PUBLISH_HOST_IP_ENV,
        )
    return configured


# --------------------------------------------------------------------------
# the Kubernetes runtime
# --------------------------------------------------------------------------


def warn_about_inert_knobs(runtime: str) -> None:
    """Say so when a knob the other runtime owns has been set explicitly.

    Every variable here is real; none is real in both runtimes. Silence would
    leave an operator who set ``ASTRABOX_SANDBOX_SERVER_KUBE_NAMESPACE`` while
    still on the Docker runtime believing they had configured something. A
    warning rather than a refusal: carrying both runtimes' settings in one
    environment file and switching between them with :data:`RUNTIME_ENV` is a
    legitimate way to run this, and refusing would break it.
    """
    inert = _DOCKER_ONLY_ENV if runtime == RUNTIME_KUBERNETES else _KUBERNETES_ONLY_ENV
    # Membership, not a value read: the question is whether the operator set it,
    # and reading the value here would be a second read site for a name whose
    # only real reader is its own accessor above.
    ignored = [name for name in inert if name in os.environ]
    if ignored:
        logger.warning(
            "%s present in the environment but with no effect under %s=%s: %s. "
            "They configure the other sandbox runtime.",
            "one variable is" if len(ignored) == 1 else f"{len(ignored)} variables are",
            RUNTIME_ENV,
            runtime,
            ", ".join(ignored),
        )


def kube_namespace() -> str:
    """The namespace sandbox workloads are created in.

    It must already exist: upstream addresses a namespace, it does not create
    one, and its multi-tenant documentation says so directly. That is a sound
    boundary — creating namespaces is a cluster-scoped privilege, and a control
    plane that holds it can reach far outside its own workloads — so this module
    checks rather than creates (:func:`verify_kubernetes_access`).
    """
    configured = _env(KUBE_NAMESPACE_ENV, DEFAULT_KUBE_NAMESPACE)
    if not configured:
        raise SandboxServerConfigError(
            f"{KUBE_NAMESPACE_ENV} is empty; sandbox workloads need a namespace "
            f"to be created in (default {DEFAULT_KUBE_NAMESPACE!r})."
        )
    return configured


def kube_workload_provider() -> str:
    """Which upstream workload provider builds the sandbox custom resource.

    Refused here rather than at the provider factory, because the factory runs
    when the app module is imported — after uvicorn has started — and its
    ``ValueError`` names upstream's registry instead of the AstraBox variable
    that was typed.
    """
    configured = _env(KUBE_WORKLOAD_PROVIDER_ENV, DEFAULT_KUBE_WORKLOAD_PROVIDER)
    provider = configured.lower()
    if provider not in WORKLOAD_RESOURCES:
        known = ", ".join(repr(name) for name in sorted(WORKLOAD_RESOURCES))
        raise SandboxServerConfigError(
            f"{KUBE_WORKLOAD_PROVIDER_ENV}={configured!r} is not a workload "
            f"provider opensandbox-server registers. Known: {known} "
            f"(default {DEFAULT_KUBE_WORKLOAD_PROVIDER!r}, which is the one this "
            "deployment is exercised against and the one the controller Helm "
            "chart reconciles)."
        )
    return provider


def kube_image_pull_policy() -> str:
    """``kubernetes.image_pull_policy`` for the sandbox container.

    Validated here because upstream types the field as a free string and copies
    it into the Pod spec, so a typo would surface as a rejected create rather
    than as a startup refusal naming the variable.
    """
    configured = _env(KUBE_IMAGE_PULL_POLICY_ENV, DEFAULT_KUBE_IMAGE_PULL_POLICY)
    if configured not in _IMAGE_PULL_POLICIES:
        raise SandboxServerConfigError(
            f"{KUBE_IMAGE_PULL_POLICY_ENV}={configured!r} is not a Kubernetes "
            f"image pull policy. Use one of {', '.join(_IMAGE_PULL_POLICIES)} "
            f"(default {DEFAULT_KUBE_IMAGE_PULL_POLICY})."
        )
    return configured


def kube_create_timeout_seconds() -> int:
    """``kubernetes.sandbox_create_timeout_seconds`` — how long a create waits.

    Upstream waits this long for a new sandbox to report an IP and gives up
    otherwise. Its default of 60 is comfortable on a warm node and too short on
    a cold one: a node that has to join and then pull the agent image spends
    minutes before the Pod can start, so the create fails while the Pod it
    asked for is still being scheduled, and the error describes a timeout
    rather than a queue.

    Refused rather than coerced. A non-integer or non-positive value cannot be
    silently rounded into something sane — upstream constrains the field to
    ``>= 1``, so a bad value would be rejected on the far side of the seam,
    where the message names upstream's schema instead of the variable that was
    typed.
    """
    configured = _env(KUBE_CREATE_TIMEOUT_ENV, DEFAULT_KUBE_CREATE_TIMEOUT_SECONDS)
    try:
        seconds = int(configured)
    except ValueError:
        raise SandboxServerConfigError(
            f"{KUBE_CREATE_TIMEOUT_ENV}={configured!r} is not a whole number of "
            f"seconds (default {DEFAULT_KUBE_CREATE_TIMEOUT_SECONDS})."
        ) from None
    if seconds < 1:
        raise SandboxServerConfigError(
            f"{KUBE_CREATE_TIMEOUT_ENV}={configured!r} must be at least 1 second; "
            "upstream rejects anything lower."
        )
    return seconds


def secure_access_enabled() -> bool:
    """Whether every newly allocated sandbox requires ingress credentials."""
    return _flag(SECURE_ACCESS_ENV, "false")


def kubernetes_ingress() -> dict[str, Any]:
    """Render OpenSandbox's native multi-node ingress configuration.

    AstraBox hands the returned endpoint directly to a browser, so only URI and
    wildcard routing are usable. Header mode is valid OpenSandbox configuration
    for an SDK client, but an address-bar navigation cannot add
    ``OpenSandbox-Ingress-To`` and would receive a dead link.
    """
    configured_mode = _env(INGRESS_MODE_ENV, DEFAULT_INGRESS_MODE)
    mode = configured_mode.lower()
    if mode not in _INGRESS_MODES:
        raise SandboxServerConfigError(
            f"{INGRESS_MODE_ENV}={configured_mode!r} must be 'direct' or 'gateway'"
        )

    secure = secure_access_enabled()
    gateway_specific = (
        INGRESS_GATEWAY_ADDRESS_ENV,
        INGRESS_ROUTE_MODE_ENV,
        INGRESS_SIGNING_KEY_ENV,
        INGRESS_SIGNING_KEY_ID_ENV,
    )
    if mode == "direct":
        if secure:
            raise SandboxServerConfigError(
                f"{SECURE_ACCESS_ENV}=true requires {INGRESS_MODE_ENV}=gateway; "
                "OpenSandbox rejects Secure Access on direct Kubernetes endpoints"
            )
        inert = [name for name in gateway_specific if name in os.environ]
        if inert:
            raise SandboxServerConfigError(
                f"{', '.join(inert)} require {INGRESS_MODE_ENV}=gateway; they "
                "would have no effect in direct mode"
            )
        return {"mode": "direct"}

    address = _env(INGRESS_GATEWAY_ADDRESS_ENV, "")
    if not address:
        raise SandboxServerConfigError(
            f"{INGRESS_MODE_ENV}=gateway requires {INGRESS_GATEWAY_ADDRESS_ENV}"
        )
    if "://" in address:
        raise SandboxServerConfigError(
            f"{INGRESS_GATEWAY_ADDRESS_ENV}={address!r} must not contain a URL "
            "scheme; configure browser HTTP/HTTPS with ASTRABOX_SANDBOX_ENDPOINT_SCHEME"
        )

    configured_route = _env(INGRESS_ROUTE_MODE_ENV, DEFAULT_INGRESS_ROUTE_MODE)
    route_mode = configured_route.lower()
    if route_mode not in _BROWSER_INGRESS_ROUTE_MODES:
        raise SandboxServerConfigError(
            f"{INGRESS_ROUTE_MODE_ENV}={configured_route!r} cannot produce a "
            "browser-openable AstraBox link. Use 'uri' or 'wildcard'; OpenSandbox "
            "header mode requires a request header an address bar cannot add."
        )
    if route_mode == "wildcard" and not address.startswith("*."):
        raise SandboxServerConfigError(
            f"{INGRESS_GATEWAY_ADDRESS_ENV}={address!r} must start with '*.' "
            "when the route mode is wildcard"
        )
    if route_mode == "uri" and "*" in address:
        raise SandboxServerConfigError(
            f"{INGRESS_GATEWAY_ADDRESS_ENV}={address!r} cannot contain '*' when "
            "the route mode is uri"
        )

    ingress: dict[str, Any] = {
        "mode": "gateway",
        "gateway": {
            "address": address,
            "route": {"mode": route_mode},
        },
    }
    signing_key = _env(INGRESS_SIGNING_KEY_ENV, "")
    signing_key_id = _env(INGRESS_SIGNING_KEY_ID_ENV, DEFAULT_INGRESS_SIGNING_KEY_ID).lower()
    if not secure:
        if INGRESS_SIGNING_KEY_ENV in os.environ or INGRESS_SIGNING_KEY_ID_ENV in os.environ:
            raise SandboxServerConfigError(
                f"{INGRESS_SIGNING_KEY_ENV} is configured while {SECURE_ACCESS_ENV} "
                "is false; the key would not be used"
            )
        return ingress
    if not signing_key:
        raise SandboxServerConfigError(
            f"{SECURE_ACCESS_ENV}=true requires {INGRESS_SIGNING_KEY_ENV}; generate "
            "one with `openssl rand -base64 32` and configure the same key on "
            "the OpenSandbox ingress gateway"
        )
    if len(signing_key_id) != 1 or signing_key_id not in "0123456789abcdefghijklmnopqrstuvwxyz":
        raise SandboxServerConfigError(
            f"{INGRESS_SIGNING_KEY_ID_ENV}={signing_key_id!r} must be one lowercase letter or digit"
        )
    try:
        padded_key = signing_key + "=" * ((4 - len(signing_key) % 4) % 4)
        decoded = base64.b64decode(padded_key, validate=True)
    except Exception as exc:
        raise SandboxServerConfigError(
            f"{INGRESS_SIGNING_KEY_ENV} must be a base64-encoded secret"
        ) from exc
    if not decoded:
        raise SandboxServerConfigError(
            f"{INGRESS_SIGNING_KEY_ENV} must decode to at least one byte"
        )
    ingress["secure_access"] = {
        "active_key": signing_key_id,
        "keys": [{"key_id": signing_key_id, "key": signing_key}],
    }
    return ingress


def kubeconfig_source() -> Path | None:
    """The kubeconfig the deployment provided, or None for in-cluster credentials.

    Unset is upstream's own meaning for ``kubernetes.kubeconfig_path``: fall back
    to the ServiceAccount mounted into a Pod. That is the right (and only)
    answer when AstraBox itself runs in the cluster it creates sandboxes in, so
    it is kept rather than forbidden — and :func:`verify_kubernetes_access` proves
    it works before the server serves, so an operator who meant to mount a file
    and forgot finds out at startup with both options named.
    """
    configured = _env(KUBECONFIG_ENV, "")
    return Path(configured).expanduser() if configured else None


def kube_api_server() -> str:
    """An explicit API-server address to substitute into the kubeconfig, or "".

    **Empty by default, and that default is load-bearing.** The obvious
    convenience — detect a loopback ``server:`` and rewrite it to something
    container-reachable — is not a safe mechanical transform, because the address
    in a kubeconfig is not only a route, it is the IDENTITY the TLS handshake
    verifies. Rewriting ``https://127.0.0.1:6443`` to
    ``host.docker.internal`` can make the API server reachable and then fail every
    request with ``CERTIFICATE_VERIFY_FAILED ... certificate is not valid for
    'host.docker.internal'``, because that name is not in the API server
    certificate's SAN list (k3s issues one for the node's hostname and IPs,
    ``localhost``, ``kubernetes.default.svc.cluster.local``, ``127.0.0.1``,
    ``::1`` and the service IP — no Docker-gateway name, no ``172.17.0.1``).
    Guessing therefore trades one failure for a later, more confusing one.

    So the deployment provides a kubeconfig that already works inside the
    container, and this variable exists for the case where only the ADDRESS is
    wrong: set it to an address that is both routable from the container and
    present in the API server certificate's SAN — the node's own IP is the usual
    answer, and is what has been verified to work. Everything else in the
    kubeconfig (CA bundle, client credentials, contexts) is used untouched, and
    the original file is never written to; see :func:`kubeconfig_for_server`.

    A scheme is required, and that check is not pedantry. The kubernetes client
    decides whether to load the CA bundle and client certificate at all by
    testing ``host.startswith("https")``; a scheme-less ``10.0.0.5:6443`` — the
    natural thing to type — therefore silently drops TLS entirely and sends the
    bearer token over plaintext HTTP, surfacing as a connection reset with no
    status code rather than as the configuration mistake it is.
    """
    configured = _env(KUBE_API_SERVER_ENV, DEFAULT_KUBE_API_SERVER)
    if not configured:
        return configured
    if not configured.startswith(("https://", "http://")):
        raise SandboxServerConfigError(
            f"{KUBE_API_SERVER_ENV}={configured!r} has no URL scheme. Write it as "
            f"'https://{configured}' — without a scheme the Kubernetes client "
            "loads neither the CA bundle nor the client certificate from the "
            "kubeconfig and talks plaintext HTTP, which fails as an unexplained "
            "connection reset and would put the credentials on the wire."
        )
    if configured.startswith("http://"):
        logger.warning(
            "%s=%s is plaintext HTTP: the Kubernetes client will not load the "
            "kubeconfig's CA bundle or client certificate, and any bearer token "
            "travels unencrypted. Use https:// unless this points at a local "
            "authenticating proxy.",
            KUBE_API_SERVER_ENV,
            configured,
        )
    return configured


def _substitute_api_server(document: Any, api_server: str) -> int:
    """Point every cluster entry at ``api_server``; return how many changed."""
    clusters = document.get("clusters") if isinstance(document, dict) else None
    if not isinstance(clusters, list):
        raise SandboxServerConfigError(
            f"the kubeconfig has no 'clusters' list, so {KUBE_API_SERVER_ENV} has "
            "nothing to apply to."
        )
    changed = 0
    for entry in clusters:
        cluster = entry.get("cluster") if isinstance(entry, dict) else None
        if isinstance(cluster, dict) and "server" in cluster:
            cluster["server"] = api_server
            changed += 1
    if not changed:
        raise SandboxServerConfigError(
            f"the kubeconfig has no cluster with a 'server' address, so "
            f"{KUBE_API_SERVER_ENV} has nothing to apply to."
        )
    return changed


#: kubeconfig keys holding a filesystem path that the Kubernetes client resolves
#: RELATIVE TO THE KUBECONFIG'S OWN DIRECTORY (``kube_config._get_base_path``).
#: Moving the file therefore moves what these names point at, which is why
#: :func:`_absolutise_file_references` runs before the derived copy is written.
_KUBECONFIG_PATH_KEYS: Final = (
    ("clusters", "cluster", ("certificate-authority",)),
    ("users", "user", ("client-certificate", "client-key", "tokenFile")),
)


def _absolutise_file_references(document: Any, base: Path) -> list[str]:
    """Resolve every relative path in ``document`` against ``base``.

    The derived kubeconfig is written beside ``server.toml``, not beside the
    source, so a relative ``certificate-authority: ca.crt`` that resolved to the
    mounted CA before the copy would resolve to a non-existent sibling of the
    copy after it — reported by the client as a bare "File does not exist" for a
    path the operator never configured. Returns the keys it rewrote, for the log
    line, so this stays visible rather than silently helpful.
    """
    rewritten: list[str] = []
    if not isinstance(document, dict):
        return rewritten
    for section, holder, keys in _KUBECONFIG_PATH_KEYS:
        entries = document.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            body = entry.get(holder) if isinstance(entry, dict) else None
            if not isinstance(body, dict):
                continue
            for key in keys:
                value = body.get(key)
                if isinstance(value, str) and value and not os.path.isabs(value):
                    body[key] = str((base / value).resolve())
                    rewritten.append(key)
    return rewritten


def kubeconfig_for_server() -> Path | None:
    """The kubeconfig path written into the server's config, or None (in-cluster).

    With :func:`kube_api_server` unset this is the provided file, VERBATIM —
    nothing is copied, nothing is parsed, and no credentials are duplicated
    anywhere. With it set, the file is parsed, every cluster's ``server`` is
    replaced, and the result is written next to the rendered ``server.toml`` as
    :data:`KUBECONFIG_FILENAME`. The source is opened read-only and never
    written back: a kubeconfig is the operator's file (and is typically mounted
    read-only, which would make an in-place edit fail anyway), while the derived
    one is this deployment's, regenerated on every boot and inspectable with
    ``cat <state_dir>/opensandbox/kubeconfig.yaml``.

    The derived file is created ``0600`` because it carries whatever credentials
    the source carried. It is written by this process, which is the same process
    that later reads it, so its owner is right by construction — unlike the
    mounted source, whose ownership belongs to whoever ran ``docker run`` and is
    the one thing here that can fail on permissions (reported by name below: the
    server image runs as the unprivileged ``astrabox`` user, not as root or as
    the host user who created the file).
    """
    source = kubeconfig_source()
    destination = metadata_dir().parent / KUBECONFIG_FILENAME
    if source is None:
        # In-cluster credentials: there is no document to substitute into, so an
        # address set here reaches nothing. Saying so beats letting an operator
        # believe they had redirected the client.
        if kube_api_server():
            logger.warning(
                "%s is set but %s is not, so there is no kubeconfig to apply it "
                "to: the server will use the in-cluster ServiceAccount and talk "
                "to kubernetes.default.svc. Mount a kubeconfig and point %s at "
                "it, or unset the address.",
                KUBE_API_SERVER_ENV,
                KUBECONFIG_ENV,
                KUBECONFIG_ENV,
            )
        _discard_stale_kubeconfig(destination)
        return None
    api_server = kube_api_server()
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise SandboxServerConfigError(
            f"cannot read the kubeconfig at {source} ({exc}). This process runs "
            f"as uid {os.getuid()}:{os.getgid()} — the server image's "
            "unprivileged 'astrabox' user, not root and not the host user who "
            "created the file — so a kubeconfig mounted 0600 and owned by "
            "someone else is unreadable here. Mount it readable by that uid "
            f"(or set {KUBECONFIG_ENV} to a copy that is)."
        ) from exc
    if not api_server:
        _discard_stale_kubeconfig(destination)
        return source

    import yaml

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SandboxServerConfigError(
            f"the kubeconfig at {source} is not valid YAML ({exc}), so "
            f"{KUBE_API_SERVER_ENV} cannot be applied to it."
        ) from exc
    _substitute_api_server(document, api_server)
    rebased = _absolutise_file_references(document, source.parent)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Created 0600 BEFORE any credential reaches it — writing first and
        # chmod-ing after would leave the cluster credentials world-readable for
        # the width of the write.
        with os.fdopen(
            os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _KUBECONFIG_MODE),
            "w",
            encoding="utf-8",
        ) as handle:
            yaml.safe_dump(document, handle, default_flow_style=False, sort_keys=False)
        # O_CREAT keeps the mode of a file that already existed, so state it.
        os.chmod(destination, _KUBECONFIG_MODE)
    except OSError as exc:
        raise SandboxServerConfigError(
            f"cannot write the derived kubeconfig to {destination}: {exc}. Set "
            f"{METADATA_DIR_ENV} to a writable directory."
        ) from exc
    logger.info(
        "%s=%s applied: %s points at that API server address (derived from %s; "
        "the source file was not modified).",
        KUBE_API_SERVER_ENV,
        api_server,
        destination,
        source,
    )
    if rebased:
        logger.info(
            "the derived kubeconfig sits in a different directory than %s, so "
            "its relative %s reference(s) were rewritten to absolute paths under "
            "%s; the files they name must be readable from this container.",
            source,
            ", ".join(sorted(set(rebased))),
            source.parent,
        )
    return destination


def _discard_stale_kubeconfig(destination: Path) -> None:
    """Remove a derived kubeconfig this boot is not going to write.

    It holds whatever credentials the source held and lives on the persistent
    state volume, so leaving it behind after the deployment stops asking for it
    (address unset, or switched back to the Docker runtime) would keep cluster
    credentials in backups and images of that volume for no reader at all.
    """
    try:
        destination.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:  # pragma: no cover - unlink on a writable state dir
        logger.warning("could not remove the stale %s: %s", destination, exc)
        return
    logger.info(
        "removed the stale derived kubeconfig at %s: nothing reads it now that "
        "%s is unset, and it carried cluster credentials.",
        destination,
        KUBE_API_SERVER_ENV,
    )


def config_document() -> dict[str, Any]:
    """The whole ``AppConfig`` document, rendered from ``ASTRABOX_*``.

    One document per runtime, and :func:`sandbox_runtime` picks which. They share
    ``server``, ``log``, ``runtime.execd_image``, ``storage`` and ``store``, and
    nothing else: the per-runtime blocks are what upstream's own ``AppConfig``
    validator wants exclusive (it refuses a ``[kubernetes]`` block under
    ``runtime.type = "docker"``), and the knobs behind them do not translate.
    """
    if sandbox_runtime() == RUNTIME_KUBERNETES:
        return _kubernetes_config_document()
    return _docker_config_document()


def _docker_config_document() -> dict[str, Any]:
    """The ``AppConfig`` document for ``runtime.type = "docker"``.

    ``server.host`` stays on loopback because the lifecycle API can control the
    host Docker daemon and has no public authentication in the bundled setup.
    ``docker.host_ip`` is the address used for Docker host-port mappings. It can
    be loopback for a host process or the bridge gateway for a containerized
    process. ``server.eip`` remains unset because upstream treats it as a full
    URL base rather than an IP address.

    The ``docker`` block states the container hardening profile required by
    agent workloads:

    * ``drop_capabilities: []`` avoids removing capabilities used by debugger
      and network-tool workloads, including ``SYS_PTRACE`` and ``NET_RAW``.
    * ``no_new_privileges: True`` prevents processes from gaining privileges
      through executable-file metadata.
    * ``pids_limit`` forwards ``ASTRABOX_SANDBOX_PIDS_LIMIT`` (512 by default)
      to bound each sandbox's process consumption.

    ``apparmor_profile`` and ``seccomp_profile`` stay unset, matching upstream's
    default of Docker's built-in seccomp profile with no AppArmor override.

    ``storage.allowed_host_paths = []`` denies every host bind mount because
    ``ensure_valid_host_path`` has no allowed prefix. The ``open_sandbox``
    backend declares ``uses_create_oss_mounts`` and never requests a host mount,
    so there is no deployment knob for this allowlist.

    ``store`` is OpenSandbox's snapshot-repository SQLite path. It stays under
    AstraBox's state root even though this deployment does not use that snapshot
    repository. Per-sandbox metadata has no ``AppConfig`` field and is redirected
    separately by :func:`redirect_metadata_store_root`.
    """
    if secure_access_enabled():
        raise SandboxServerConfigError(
            f"{SECURE_ACCESS_ENV}=true is not supported by OpenSandbox's Docker "
            "runtime. Use the native execd proxy with this setting off, or run "
            "Kubernetes with the OpenSandbox ingress gateway."
        )
    low, high = port_range()
    metadata_root = metadata_dir()
    return {
        "server": {
            "host": SERVER_BIND_HOST,
            "port": server_port(),
        },
        "log": {"level": _env(LOG_LEVEL_ENV, DEFAULT_LOG_LEVEL).upper()},
        "runtime": {
            "type": "docker",
            "execd_image": execd_image(),
        },
        "docker": {
            "network_mode": network_mode(),
            "drop_capabilities": [],
            "no_new_privileges": True,
            "pids_limit": pids_limit(),
            "port_range_min": low,
            "port_range_max": high,
            "host_ip": publish_host_ip(),
        },
        "storage": {"allowed_host_paths": []},
        "store": {
            "type": "sqlite",
            "path": str(metadata_root.parent / SNAPSHOT_STORE_FILENAME),
        },
        **({"secure_runtime": hardened} if (hardened := secure_runtime()) else {}),
        **({"egress": sidecar} if (sidecar := egress()) else {}),
    }


def _kubernetes_config_document() -> dict[str, Any]:
    """The ``AppConfig`` document for ``runtime.type = "kubernetes"``.

    No ``docker`` block, and its absence is required rather than tidy: upstream's
    ``AppConfig`` validator makes the two runtime blocks exclusive, and every
    field in the Docker one describes something Kubernetes does not have — there
    is no network mode to pick, no host port range to publish into and no host
    interface to publish on. :func:`prepare` skips the publish-host rebind here
    for the same reason, by runtime rather than by the block's absence.

    The container hardening profile the Docker block states explicitly has no
    counterpart in upstream's ``[kubernetes]`` block, which carries no
    capability, ``no_new_privileges`` or PID-limit field at all. That layer is
    not silently dropped, it is somewhere else in Kubernetes — the sandbox
    container's ``securityContext``, a ``RuntimeClass``, the kubelet's
    ``podPidsLimit`` — and all of it is cluster-operator surface; see the module
    docstring. Rendering AstraBox env into fields upstream does not read would be
    an inert knob, so there is none.

    ``ingress`` is explicit because it decides the shape of every endpoint.
    Direct mode returns a Pod address for private-cluster deployments. Gateway
    mode returns the separately deployed OpenSandbox ingress address and, when
    Secure Access is enabled, lets the lifecycle API mint OSEP-0011 signed URLs.

    ``kubeconfig_path`` is omitted entirely when :data:`KUBECONFIG_ENV` is unset,
    which is how upstream spells "use the in-cluster ServiceAccount".
    """
    metadata_root = metadata_dir()
    kubeconfig = kubeconfig_for_server()
    kubernetes: dict[str, Any] = {}
    if kubeconfig is not None:
        kubernetes["kubeconfig_path"] = str(kubeconfig)
    kubernetes.update(
        {
            "namespace": kube_namespace(),
            "workload_provider": kube_workload_provider(),
            "image_pull_policy": kube_image_pull_policy(),
            "informer_enabled": _flag(KUBE_INFORMER_ENV, DEFAULT_KUBE_INFORMER),
            "sandbox_create_timeout_seconds": kube_create_timeout_seconds(),
        }
    )
    return {
        "server": {
            "host": SERVER_BIND_HOST,
            "port": server_port(),
        },
        "log": {"level": _env(LOG_LEVEL_ENV, DEFAULT_LOG_LEVEL).upper()},
        "runtime": {
            "type": "kubernetes",
            "execd_image": execd_image(),
        },
        "kubernetes": kubernetes,
        "ingress": kubernetes_ingress(),
        "storage": {"allowed_host_paths": []},
        "store": {
            "type": "sqlite",
            "path": str(metadata_root.parent / SNAPSHOT_STORE_FILENAME),
        },
        **({"secure_runtime": hardened} if (hardened := secure_runtime()) else {}),
        **({"egress": sidecar} if (sidecar := egress()) else {}),
    }


def _toml_value(value: object) -> str:
    """One TOML scalar, inline table, or inline array.

    ``json.dumps`` renders the string case: JSON's string escapes (``\\"``,
    ``\\\\``, ``\\b``, ``\\f``, ``\\n``, ``\\r``, ``\\t``, ``\\uXXXX``) are all
    valid TOML basic-string escapes and JSON emits no others, so the two agree
    over every value this document can hold (ports, paths, image references, IP
    addresses, a log level).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, dict):
        return (
            "{ " + ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items()) + " }"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise SandboxServerConfigError(
        f"cannot render {value!r} ({type(value).__name__}) into the sandbox server config file"
    )


def render_config_toml(document: dict[str, Any]) -> str:
    """Serialise :func:`config_document`'s output as TOML.

    Hand-rolled because the standard library reads TOML and does not write it.
    Nested mappings become dotted tables; lists of mappings use TOML inline
    tables, which is the shape OpenSandbox expects for signing-key rotation.
    """
    lines = [
        "# Generated by astrabox.deploy.sandbox_server — DO NOT EDIT.",
        "# Rewritten from the ASTRABOX_* environment on every start; edits are lost.",
    ]

    def append_table(path: tuple[str, ...], entries: dict[str, Any]) -> None:
        lines.append("")
        lines.append(f"[{'.'.join(path)}]")
        nested: list[tuple[str, dict[str, Any]]] = []
        for key, value in entries.items():
            if isinstance(value, dict):
                nested.append((str(key), value))
                continue
            lines.append(f"{key} = {_toml_value(value)}")
        for key, value in nested:
            append_table((*path, key), value)

    for table, entries in document.items():
        if not isinstance(entries, dict):
            raise SandboxServerConfigError(f"top-level TOML entry {table!r} must be a table")
        append_table((str(table),), entries)
    return "\n".join(lines) + "\n"


def write_config_file(document: dict[str, Any]) -> Path:
    """Materialise the config where upstream's loader will find it.

    Next to the metadata directory rather than in a temporary file, so a running
    deployment can be inspected (``cat <state_dir>/opensandbox/server.toml``)
    and the effective configuration is never a guess.
    """
    path = metadata_dir().parent / CONFIG_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, CONFIG_FILE_MODE),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write(render_config_toml(document))
        # O_CREAT preserves the mode of an existing file.
        os.chmod(path, CONFIG_FILE_MODE)
    except OSError as exc:
        raise SandboxServerConfigError(
            f"cannot write the sandbox server config to {path}: {exc}. Set "
            f"{METADATA_DIR_ENV} to a writable, persistent directory."
        ) from exc
    return path


# --------------------------------------------------------------------------
# the optional extra
# --------------------------------------------------------------------------


def missing_extra_error(exc: Exception) -> SandboxServerConfigError:
    """The one loud shape for "the sandbox-server extra is not installed"."""
    return SandboxServerConfigError(
        "the OpenSandbox lifecycle server is not installed in this environment "
        f"({type(exc).__name__}: {exc}). Install it with "
        f"`pip install 'astrabox[{EXTRA_NAME}]'`, or point "
        "ASTRABOX_SANDBOX_OPENAPI_BASE_URL at a server you run separately."
    )


#: The upstream names this module rebinds or verifies. Held as strings so a
#: rename upstream is a loud startup failure instead of a silent write to an
#: attribute the server does not read.
PUBLISH_HOST_ATTR = "DOCKER_PUBLISH_HOST"
ALLOCATE_PORTS_ATTR = "allocate_port_bindings"
NETWORKING_MIXIN_ATTR = "DockerNetworkingMixin"
RESOLVE_PROXY_HOST_ATTR = "_resolve_proxy_host"
RESOLVE_PUBLIC_HOST_ATTR = "_resolve_public_host"
METADATA_DEFAULT_ROOT_ATTR = "DEFAULT_STORE_DIR"
METADATA_STORE_CLS_ATTR = "DockerMetadataStore"


def allow_host_controlled_egress_dns_upstream(module: ModuleType) -> None:
    """Permit AstraBox's private resolver through upstream's env splitter.

    OpenSandbox's egress component documents ``OPENSANDBOX_EGRESS_DNS_UPSTREAM``
    but server 0.2.3 omits it from the create-env allowlist. AstraBox's lifecycle
    API is loopback-only, and the executor rejects every sandbox-provided
    ``OPENSANDBOX_EGRESS_*`` name before the request, so widening this single
    host-owned value does not hand DNS control to sandbox code.
    """

    _require_attributes(
        module,
        ("ALLOWED_EGRESS_ENV_VARS", "split_egress_env"),
        "forward its host-controlled DNS resolver to the egress sidecar",
    )
    allowed = frozenset(getattr(module, "ALLOWED_EGRESS_ENV_VARS")) | {UPSTREAM_DNS_ENV}
    setattr(module, "ALLOWED_EGRESS_ENV_VARS", frozenset(allowed))
    try:
        sandbox_env, egress_env = getattr(module, "split_egress_env")(
            {"ASTRABOX_PROBE": "kept", UPSTREAM_DNS_ENV: "127.0.0.1:5353"}
        )
    except Exception as exc:
        raise SandboxServerConfigError(
            "the installed opensandbox-server refused the host-controlled "
            f"{UPSTREAM_DNS_ENV} value after AstraBox enabled it: {exc}"
        ) from exc
    if sandbox_env != {"ASTRABOX_PROBE": "kept"} or egress_env != {
        UPSTREAM_DNS_ENV: "127.0.0.1:5353"
    }:
        raise SandboxServerConfigError(
            "the installed opensandbox-server did not route the host-controlled "
            f"{UPSTREAM_DNS_ENV} value exclusively to the egress sidecar"
        )


def _require_attributes(module: ModuleType, attributes: Iterable[str], purpose: str) -> None:
    for attribute in attributes:
        if not hasattr(module, attribute):
            raise SandboxServerConfigError(
                f"the installed opensandbox-server no longer exposes "
                f"{module.__name__}.{attribute}, so AstraBox cannot {purpose}. "
                "Pin opensandbox-server to a 0.2.x release, or update "
                "astrabox/deploy/sandbox_server.py to whatever replaced it."
            )


def redirect_publish_host(module: ModuleType, host_ip: str, *, ports: tuple[int, int]) -> None:
    """Bind published sandbox ports to ``host_ip`` instead of every interface.

    Upstream publishes each sandbox's host ports from
    ``port_allocator.DOCKER_PUBLISH_HOST = "0.0.0.0"`` — a module constant with
    no config field, no env var and no parameter behind it (``allocate_port_
    bindings`` reads it directly). Those ports carry execd and the AIO
    file/terminal server, an UNAUTHENTICATED in-box control surface, so on any
    host with a second interface that default hands anyone on the network the
    ability to read the workspace and drive the agent, which is why this
    deployment binds them to loopback.

    Upstream's default is right for the deployment upstream documents — a server
    in a controlled environment with clients connecting from elsewhere, where a
    loopback bind would make sandboxes unreachable, and where the API key and
    execd access-token mechanisms it ships are the operator's to turn on. This
    deployment is the other shape: AstraBox is the only client, it shares the
    container with the server, and it reaches sandboxes through the server's
    proxy rather than through these ports. Narrowing the bind is therefore a
    configuration decision this deployment gets to make, and one that costs it
    nothing — see the module docstring.

    The rebind is asserted and then PROVEN: upstream's own allocator is called
    and the bind host is read back out of its result, so a rename, a refactor
    that stops consulting the constant, or a wrapper that overrides it all fail
    here instead of quietly publishing to the world.

    ``PORT_PROBE_HOST`` is deliberately left alone. It is a separate constant
    initialised from this one at import time, used only to test whether a
    candidate port is free, and upstream's comment is right that the probe must
    keep the WIDER scope: probing the narrow address would hand out a port
    already bound on another interface, which Docker then fails to publish.
    """
    _require_attributes(
        module,
        (PUBLISH_HOST_ATTR, ALLOCATE_PORTS_ATTR),
        "keep sandbox ports off every host interface",
    )
    setattr(module, PUBLISH_HOST_ATTR, host_ip)
    if getattr(module, PUBLISH_HOST_ATTR) != host_ip:
        raise SandboxServerConfigError(  # pragma: no cover - defends the assignment itself
            f"setting {module.__name__}.{PUBLISH_HOST_ATTR} to {host_ip!r} did not take"
        )
    low, high = ports
    try:
        allocated = getattr(module, ALLOCATE_PORTS_ATTR)(["8080"], min_port=low, max_port=high)
        bound_to = str(allocated["8080"][0])
    except Exception as exc:
        raise SandboxServerConfigError(
            "cannot verify where sandbox ports will be published: the "
            f"opensandbox-server port allocator failed ({exc})"
        ) from exc
    if bound_to != host_ip:
        raise SandboxServerConfigError(
            f"the opensandbox-server port allocator ignored "
            f"{module.__name__}.{PUBLISH_HOST_ATTR}: it still binds published "
            f"sandbox ports to {bound_to!r}, which would expose every sandbox's "
            "unauthenticated in-box control server on that interface. Pin "
            "opensandbox-server to a 0.2.x release, or update "
            "astrabox/deploy/sandbox_server.py."
        )


def redirect_proxy_host(module: ModuleType, host_ip: str) -> None:
    """Make OpenSandbox's server proxy use the configured Docker host address.

    OpenSandbox 0.2.x normally returns ``server.host`` from
    ``_resolve_proxy_host`` whenever that host is explicit. The bundled server
    must bind its unauthenticated lifecycle API to ``127.0.0.1``, but that
    loopback belongs to the server container and cannot reach ports published
    on the Docker host. Protected sandboxes therefore time out while the server
    checks the egress sidecar, even when ``docker.host_ip`` is configured.

    This hook changes only the server's outbound target. It does not widen the
    lifecycle API bind. Remove it when upstream makes ``_resolve_proxy_host``
    honor ``docker.host_ip`` with an explicit loopback server bind.
    """
    _require_attributes(
        module,
        (NETWORKING_MIXIN_ATTR,),
        "route protected sandbox traffic through the Docker host",
    )
    mixin = getattr(module, NETWORKING_MIXIN_ATTR)
    if not hasattr(mixin, RESOLVE_PROXY_HOST_ATTR):
        raise SandboxServerConfigError(
            "the installed opensandbox-server no longer exposes "
            f"{module.__name__}.{NETWORKING_MIXIN_ATTR}."
            f"{RESOLVE_PROXY_HOST_ATTR}, so AstraBox cannot route protected "
            "sandbox traffic through the Docker host. Pin opensandbox-server "
            "to a 0.2.x release, or update astrabox/deploy/sandbox_server.py."
        )

    def _astrabox_resolve_proxy_host(_self: Any) -> str:
        return host_ip

    setattr(mixin, RESOLVE_PROXY_HOST_ATTR, _astrabox_resolve_proxy_host)
    try:
        resolved = getattr(object.__new__(mixin), RESOLVE_PROXY_HOST_ATTR)()
    except Exception as exc:
        raise SandboxServerConfigError(
            "cannot verify the OpenSandbox proxy address after applying the "
            f"container-network compatibility hook: {exc}"
        ) from exc
    if resolved != host_ip:
        raise SandboxServerConfigError(
            "the installed opensandbox-server ignored AstraBox's protected "
            f"sandbox proxy address: expected {host_ip!r}, got {resolved!r}."
        )


def redirect_public_endpoint_host(module: ModuleType, host_ip: str) -> None:
    """Make native Docker endpoint URLs use the address that owns their ports.

    The lifecycle API stays on loopback, while Docker may publish execd ports on
    the host's bridge address. OpenSandbox otherwise derives the returned host
    from ``server.host`` and produces a URL whose port is bound on a different
    interface. This changes only endpoint generation; traffic still goes
    directly to OpenSandbox execd's ``/proxy/{port}`` route.
    """
    _require_attributes(
        module,
        (NETWORKING_MIXIN_ATTR,),
        "return the address that owns native sandbox ports",
    )
    mixin = getattr(module, NETWORKING_MIXIN_ATTR)
    if not hasattr(mixin, RESOLVE_PUBLIC_HOST_ATTR):
        raise SandboxServerConfigError(
            "the installed opensandbox-server no longer exposes "
            f"{module.__name__}.{NETWORKING_MIXIN_ATTR}."
            f"{RESOLVE_PUBLIC_HOST_ATTR}, so AstraBox cannot return reachable "
            "native sandbox URLs. Pin opensandbox-server to a 0.2.x release, "
            "or update astrabox/deploy/sandbox_server.py."
        )

    def _astrabox_resolve_public_host(_self: Any) -> str:
        return host_ip

    setattr(mixin, RESOLVE_PUBLIC_HOST_ATTR, _astrabox_resolve_public_host)
    try:
        resolved = getattr(object.__new__(mixin), RESOLVE_PUBLIC_HOST_ATTR)()
    except Exception as exc:
        raise SandboxServerConfigError(
            "cannot verify the OpenSandbox public endpoint address after "
            f"applying the compatibility hook: {exc}"
        ) from exc
    if resolved != host_ip:
        raise SandboxServerConfigError(
            "the installed opensandbox-server ignored AstraBox's public "
            f"sandbox endpoint address: expected {host_ip!r}, got {resolved!r}."
        )


def redirect_metadata_store_root(module: ModuleType, root: Path) -> None:
    """Point upstream's per-sandbox metadata store at ``root``, and prove it took.

    ``DockerMetadataStore.__init__`` accepts a root, but
    ``DockerSandboxService.__init__`` constructs it with no argument and there
    is no injection point, no config field and no env var upstream reads for it:
    the only seam is the module-level ``DEFAULT_STORE_DIR`` the constructor
    falls back to (``Path.home()/".opensandbox"/"metadata"``). Rebinding THAT,
    rather than replacing the service's store afterwards, is also the only
    correct seam — the service's constructor already uses its store
    (``_restore_existing_sandboxes`` reads persisted expirations to re-arm
    timers), so a post-construction swap would read the wrong directory on
    exactly the restart path this exists to fix.

    Why it matters more than tidiness: this directory is the ONLY durable record
    of a lease RENEWAL. ``renew_expiration`` writes the new expiry here and to
    an in-process timer, and also tries to rewrite the container's
    ``expires_at`` label — but Docker's container-update API does not accept
    labels, so that third write is a no-op it merely logs. On restart the
    service re-arms every expiration timer from this store, falling back to the
    container's ORIGINAL label without it. A ``$HOME`` that does not persist
    therefore kills live, repeatedly-renewed sandboxes mid-conversation at the
    lease they were born with.

    Proven through upstream's own PUBLIC store API — write, locate, read back,
    delete — before the service is built. An upstream rename, an upstream change
    of which store the service uses, or an unwritable directory each fail loud
    here instead of quietly resuming writes to ``$HOME``.
    """
    _require_attributes(
        module,
        (METADATA_DEFAULT_ROOT_ATTR, METADATA_STORE_CLS_ATTR),
        "redirect its sandbox metadata out of $HOME",
    )
    setattr(module, METADATA_DEFAULT_ROOT_ATTR, root)
    if getattr(module, METADATA_DEFAULT_ROOT_ATTR) != root:
        raise SandboxServerConfigError(  # pragma: no cover - defends the assignment itself
            f"setting {module.__name__}.{METADATA_DEFAULT_ROOT_ATTR} to {root} did not take"
        )

    probe_id = f"__astrabox-probe-{os.getpid()}-{uuid4().hex}__"
    stamp = datetime(2001, 1, 1, tzinfo=timezone.utc)
    before = set(root.rglob("*"))
    store = getattr(module, METADATA_STORE_CLS_ATTR)()
    try:
        try:
            store.set_expiration(probe_id, stamp)
        except OSError as exc:
            raise SandboxServerConfigError(
                f"the sandbox metadata directory {root} is not writable ({exc}). "
                "The lifecycle server keeps every sandbox's renewed lease there; "
                "without it a restart kills live sandboxes at their original "
                f"TTL. Point {METADATA_DIR_ENV} at a writable, persistent directory."
            ) from exc
        landed = set(root.rglob("*")) - before
        read_back = store.get_expiration(probe_id)
    finally:
        try:
            store.delete(probe_id)
        except Exception:
            logger.debug("could not remove the metadata probe %s", probe_id, exc_info=True)
    if not landed:
        raise SandboxServerConfigError(
            f"the opensandbox-server metadata store ignored "
            f"{module.__name__}.{METADATA_DEFAULT_ROOT_ATTR}: a probe write left "
            f"nothing under {root}, so sandbox metadata would still go to "
            "upstream's $HOME default and lease renewals would be lost on "
            "restart. Pin opensandbox-server to a 0.2.x release, or update "
            "astrabox/deploy/sandbox_server.py."
        )
    if read_back != stamp.isoformat():
        raise SandboxServerConfigError(
            f"the sandbox metadata directory {root} did not round-trip a probe "
            f"write (read back {read_back!r}); the lifecycle server cannot "
            "persist sandbox lease renewals there."
        )


#: Filesystem types whose contents do not survive a restart of whatever hosts
#: this process: tmpfs/ramfs are RAM, and a container's ``overlay`` root dies
#: with the container.
_EPHEMERAL_FILESYSTEMS = frozenset({"tmpfs", "ramfs", "overlay", "overlayfs"})
#: Path prefixes conventionally cleared on boot even on a persistent filesystem.
_EPHEMERAL_PATH_PREFIXES = ("/tmp", "/var/tmp", "/dev/shm", "/run")


def _filesystem_type(path: Path) -> str | None:
    """The filesystem type ``path`` lives on, or None when it cannot be told.

    Reads ``/proc/mounts`` and takes the longest matching mount point. None on a
    platform without procfs — the persistence check then degrades to its
    path-prefix half rather than guessing.
    """
    try:
        raw = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        target = path.resolve()
    except OSError:  # pragma: no cover - resolve() on a live path
        target = path
    best: tuple[int, str] | None = None
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        # /proc/mounts octal-escapes whitespace inside the mount point.
        mount = Path(fields[1].replace("\\040", " ").replace("\\011", "\t"))
        if target != mount and mount not in target.parents:
            continue
        depth = len(mount.parts)
        if best is None or depth > best[0]:
            best = (depth, fields[2])
    return None if best is None else best[1]


def warn_if_metadata_dir_is_ephemeral(root: Path) -> None:
    """WARN, with the consequence spelled out, if this directory dies on restart.

    The consequence is worse than "some metadata is lost" — see
    :func:`redirect_metadata_store_root`. The single most likely way to hit it
    is running the container without a volume on ``/data``, where the metadata
    lands on the container's own ``overlay`` root.

    A WARNING rather than a refusal: an ephemeral store is a legitimate posture
    for a throwaway dev box (nothing is renewed long enough to matter), and this
    module cannot tell that deployment from a production one. Unwritable IS a
    refusal, and that check lives in :func:`redirect_metadata_store_root`.
    """
    fstype = _filesystem_type(root)
    reason = ""
    if fstype is not None and fstype.lower() in _EPHEMERAL_FILESYSTEMS:
        reason = f"it is on a {fstype!r} filesystem"
    elif str(root).startswith(_EPHEMERAL_PATH_PREFIXES):
        reason = "it is under a path conventionally cleared on boot"
    if not reason:
        return
    logger.warning(
        "the sandbox metadata directory %s will probably NOT survive a restart "
        "(%s). It is the only durable record of a sandbox lease RENEWAL — Docker "
        "cannot update a running container's labels, so a restart without it "
        "re-arms every expiration timer from the container's original expires_at "
        "label and kills long-lived, repeatedly-renewed sandboxes at their birth "
        "lease. Mount a volume on the state directory, or point %s at persistent "
        "storage.",
        root,
        reason,
        METADATA_DIR_ENV,
    )


# --------------------------------------------------------------------------
# the Kubernetes startup proof
# --------------------------------------------------------------------------


def _api_status(exc: BaseException) -> int | None:
    """The HTTP status of a Kubernetes client error, or None if it never got one.

    The client raises ``ApiException`` (carrying ``.status``) once the API server
    has answered, and a urllib3 transport error when it has not. Read as an
    attribute rather than by isinstance so this stays free of both imports —
    which is also what lets :func:`verify_kubernetes_access` be exercised
    without the optional extra installed.
    """
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) else None


def _unreachable_error(api_server: str, exc: BaseException) -> SandboxServerConfigError:
    """Describe an API-server address that cannot connect or pass TLS verification.

    The kubeconfig server address must satisfy two independent constraints:

    * **Routable from inside this container.** A single-node installer writes the
      API server address for the HOST (k3s: ``https://127.0.0.1:6443``), and
      ``127.0.0.1`` inside a container is that container's own stack.
    * **Present in the API server certificate's SAN.** The Kubernetes client
      verifies the configured hostname or IP against the certificate during the
      TLS handshake. A host-gateway name can make the server routable and still
      fail verification when that name is absent from the SAN list.

    The node's own IP commonly satisfies both constraints.
    """
    return SandboxServerConfigError(
        f"cannot use the Kubernetes API server at {api_server} ({exc}). The "
        "address has to be BOTH routable from inside this container AND covered "
        "by the API server certificate's SAN list — a TLS 'certificate is not "
        "valid for ...' failure means the second half. A single-node installer "
        "writes a loopback address that is neither from in here, and the "
        "host-gateway name that fixes the routing is usually NOT in the "
        f"certificate. Point {KUBE_API_SERVER_ENV} at an address that satisfies "
        "both (the node's own IP normally does; "
        "`openssl s_client -connect <host>:6443 </dev/null | openssl x509 "
        "-noout -text | grep -A1 'Subject Alternative Name'` lists what the "
        f"certificate covers), or set {KUBECONFIG_ENV} to a kubeconfig that "
        "already works inside this container. A name-based address additionally "
        "has to resolve here — `--add-host host.docker.internal:host-gateway` is "
        "what makes Docker's own host name resolve on Linux, and the documented "
        "`docker run` passes it."
    )


def _rejected_credentials_error(api_server: str, exc: BaseException) -> SandboxServerConfigError:
    """HTTP 401: the API server did not accept these credentials at all.

    Distinguished from 403 on purpose. A 403 is one identity being told it may
    not do one thing, which several probes here survive by design; a 401 means
    nothing authenticated, so every later probe fails the same way — including
    the access review, which degrades to a warning when it cannot ask. Refusing
    at the first 401 is what keeps that from adding up to a server that starts.
    """
    return SandboxServerConfigError(
        f"the Kubernetes API server at {api_server} rejected these credentials "
        f"(HTTP 401: {exc}). Nothing authenticated, so no sandbox could be "
        f"created. Check that {KUBECONFIG_ENV} points at a kubeconfig whose "
        "client credentials are still valid for this cluster — a copied "
        "kubeconfig whose certificate has expired, or one issued by a different "
        "cluster, both land here."
    )


def verify_kubernetes_access(
    core_api: Any,
    custom_api: Any,
    authorization_api: Any,
    *,
    api_server: str,
    namespace: str,
    workload_provider: str,
) -> None:
    """Prove, before serving, that this server can actually create sandboxes.

    The Docker runtime proves its compatibility hooks before uvicorn binds;
    this is the same obligation for the other runtime. Everything checked here
    otherwise surfaces as a failed create in the middle of someone's first turn,
    behind a 400 or a 503 whose text is about a Kubernetes API call rather than
    about the deployment's configuration.

    Three probes, because they fail for three different reasons and each has a
    different answer:

    1. **The namespace exists.** Upstream creates sandbox workloads in it and
       does not create the namespace itself — Kubernetes namespaces are a
       cluster-administration boundary, not a per-workload resource, and this
       module does not cross it either. A 404 is therefore refused with the
       ``kubectl create namespace`` line to run.
    2. **The workload CRD is installed and readable in that namespace.** The
       custom resource a sandbox IS comes from upstream's controller, which is
       deployed into the cluster separately (its Helm chart is the artefact
       upstream publishes for it). Listing the resource proves the definition is
       registered, using only namespace-scoped permission.
    3. **This identity may create one.** A ``SelfSubjectAccessReview`` — the same
       question ``kubectl auth can-i`` asks — so a read-only kubeconfig fails
       here rather than at the first create.

    A 403 on probes 1 and 2 is WARNED about, not refused, and the asymmetry is
    deliberate: a namespace-scoped ServiceAccount legitimately cannot ``get`` the
    Namespace object or list cluster-wide, so a refusal there would reject a
    perfectly good deployment for lacking permission it does not need. A 404 is
    unambiguous and stays a refusal. Probe 3 likewise degrades to a warning when
    the authorizer reports an evaluation error (it could not decide), and refuses
    only on a decided "no".

    **401 is not 403 here.** 403 says "you, specifically, may not do this one
    thing", which is survivable; 401 says the API server did not accept the
    credentials at all, so every later probe would fail the same way and probe 3
    would degrade to a warning on its way past. It is refused at the first probe
    that sees it, before that can happen.
    """
    group, version, plural = WORKLOAD_RESOURCES[workload_provider]

    try:
        core_api.read_namespace(namespace)
    except Exception as exc:
        status = _api_status(exc)
        if status is None:
            raise _unreachable_error(api_server, exc) from exc
        if status == 404:
            raise SandboxServerConfigError(
                f"the sandbox namespace {namespace!r} does not exist on the "
                f"cluster at {api_server}. opensandbox-server creates sandbox "
                "workloads in it but does not create the namespace, and neither "
                "does AstraBox — that is the cluster administrator's boundary. "
                f"Create it and restart:\n    kubectl create namespace {namespace}\n"
                f"Or point {KUBE_NAMESPACE_ENV} at a namespace that already exists."
            ) from exc
        if status == 401:
            raise _rejected_credentials_error(api_server, exc)
        if status == 403:
            logger.warning(
                "cannot read the namespace %s to confirm it exists (HTTP 403). A "
                "namespace-scoped ServiceAccount is not normally allowed to, so "
                "this is not treated as a failure; a missing namespace will "
                "surface on the first sandbox create instead.",
                namespace,
            )
        else:
            raise SandboxServerConfigError(
                f"the Kubernetes API server at {api_server} refused a read of "
                f"namespace {namespace!r} (HTTP {status}): {exc}"
            ) from exc

    try:
        custom_api.list_namespaced_custom_object(
            group=group, version=version, namespace=namespace, plural=plural, limit=1
        )
    except Exception as exc:
        status = _api_status(exc)
        if status is None:
            raise _unreachable_error(api_server, exc) from exc
        if status == 404:
            raise SandboxServerConfigError(
                f"the cluster at {api_server} has no {plural}.{group} resource, so "
                f"the {workload_provider!r} workload provider has nothing to "
                "create. That custom resource comes from the OpenSandbox "
                "controller, which is installed INTO the cluster (upstream "
                "publishes a Helm chart for it) separately from this server. "
                "Install it and restart — scripts/k8s-testbed.sh does exactly "
                "that for a throwaway cluster."
            ) from exc
        if status == 401:
            raise _rejected_credentials_error(api_server, exc)
        if status == 403:
            logger.warning(
                "cannot list %s.%s in namespace %s to confirm the OpenSandbox "
                "controller's custom resource is installed (HTTP 403); continuing.",
                plural,
                group,
                namespace,
            )
        else:
            raise SandboxServerConfigError(
                f"the Kubernetes API server at {api_server} refused a list of "
                f"{plural}.{group} in namespace {namespace!r} (HTTP {status}): {exc}"
            ) from exc

    try:
        review = authorization_api.create_self_subject_access_review(
            {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SelfSubjectAccessReview",
                "spec": {
                    "resourceAttributes": {
                        "namespace": namespace,
                        "group": group,
                        "resource": plural,
                        "verb": "create",
                    }
                },
            }
        )
    except Exception as exc:
        logger.warning(
            "could not ask the cluster whether this identity may create %s.%s in "
            "namespace %s (%s); continuing without that proof.",
            plural,
            group,
            namespace,
            exc,
        )
        return
    review_status = getattr(review, "status", None)
    evaluation_error = getattr(review_status, "evaluation_error", None)
    if evaluation_error:
        logger.warning(
            "the cluster could not decide whether this identity may create %s.%s "
            "in namespace %s (%s); continuing without that proof.",
            plural,
            group,
            namespace,
            evaluation_error,
        )
        return
    if getattr(review_status, "allowed", False):
        return
    reason = getattr(review_status, "reason", None) or "no reason given"
    raise SandboxServerConfigError(
        f"the credentials in use may not create {plural}.{group} in namespace "
        f"{namespace!r} on the cluster at {api_server} ({reason}). Every sandbox "
        "is one of those, so nothing would ever start. Grant the identity that "
        f"verb in {namespace!r} — `kubectl auth can-i create {plural}.{group} "
        f"-n {namespace}` asks the same question — or point {KUBECONFIG_ENV} at "
        "credentials that already have it."
    )


def kubernetes_preflight(kubeconfig: Path | None, document: dict[str, Any]) -> None:
    """Build the API clients the way upstream will, then run the proofs.

    Split from :func:`verify_kubernetes_access` so the probes are testable
    without a cluster and without the optional extra: this half is the part that
    must import ``kubernetes`` and dial, and it does nothing else.

    The clients are built from the SAME kubeconfig path rendered into the
    server's document, so this cannot pass against credentials the server will
    not use. ``new_client_from_config`` returns a client of its own rather than
    mutating the process-wide default, leaving upstream's own load untouched.
    """
    try:
        from kubernetes import client as kubernetes_client
        from kubernetes import config as kubernetes_config
    except Exception as exc:  # ImportError, and any import-time failure inside it
        raise missing_extra_error(exc) from exc

    try:
        if kubeconfig is None:
            kubernetes_config.load_incluster_config()
            api_client = kubernetes_client.ApiClient()
        else:
            api_client = kubernetes_config.new_client_from_config(config_file=str(kubeconfig))
    except Exception as exc:
        if kubeconfig is None:
            raise SandboxServerConfigError(
                "no Kubernetes credentials: AstraBox is not running as a Pod with "
                f"a ServiceAccount ({exc}), and {KUBECONFIG_ENV} is unset. Set it "
                "to a kubeconfig mounted into this container, or run AstraBox in "
                "the cluster."
            ) from exc
        raise SandboxServerConfigError(
            f"the kubeconfig at {kubeconfig} is not usable ({exc})."
        ) from exc

    verify_kubernetes_access(
        kubernetes_client.CoreV1Api(api_client),
        kubernetes_client.CustomObjectsApi(api_client),
        kubernetes_client.AuthorizationV1Api(api_client),
        api_server=str(getattr(api_client.configuration, "host", "the cluster")),
        namespace=str(document["kubernetes"]["namespace"]),
        workload_provider=str(document["kubernetes"]["workload_provider"]),
    )


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def prepare() -> dict[str, Any]:
    """Everything that must happen before upstream's ASGI app is imported.

    Order is load-bearing. The configuration is rendered and validated by
    upstream's own schema first (so a bad value fails before any Docker or
    Kubernetes call), and only then does the runtime-specific half run — for
    Docker, the compatibility hooks are applied BEFORE the app module is
    imported, because importing it constructs the service and that constructor
    already reads the metadata store.

    The runtime-specific halves do not overlap. The Docker hooks and metadata
    persistence warning are about upstream's Docker service, which the Kubernetes
    runtime never constructs and whose lease bookkeeping it does not share (a
    renewal there is written to the workload's own ``spec.expireTime``, in the
    cluster). Running them anyway would prove nothing and warn about a
    consequence that does not exist. The Kubernetes runtime's equivalent — proof
    that the dependency is actually usable — is :func:`kubernetes_preflight`.

    Returns upstream's parsed ``AppConfig`` (as a plain object) for the caller
    to pass to uvicorn.
    """
    runtime = sandbox_runtime()
    warn_about_inert_knobs(runtime)
    document = config_document()
    config_path = write_config_file(document)
    # Handed TO upstream's own process-global config loader and startup guard,
    # not AstraBox configuration: these two names are upstream's, they are
    # written rather than read here, and they carry no AstraBox env-registry row
    # because an operator setting them would be setting a knob this function
    # overwrites on every boot.
    os.environ.update(
        {
            _CONFIG_PATH_ENV: str(config_path),
            _INSECURE_SERVER_ENV: _INSECURE_SERVER_ACK,
        }
    )

    try:
        from opensandbox_server.config import load_config
        from opensandbox_server.logging_config import configure_logging
        from opensandbox_server.services.docker import metadata as metadata_module
        from opensandbox_server.services.docker import networking as networking_module
        from opensandbox_server.services.docker import port_allocator
    except Exception as exc:  # ImportError, and any import-time failure inside it
        raise missing_extra_error(exc) from exc

    try:
        app_config = load_config(config_path)
    except Exception as exc:
        raise SandboxServerConfigError(
            f"the sandbox server configuration AstraBox rendered into "
            f"{config_path} was rejected by opensandbox-server: {exc}"
        ) from exc
    log_config = configure_logging(app_config.log)

    root = metadata_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SandboxServerConfigError(
            f"cannot create the sandbox metadata directory {root}: {exc}. Set "
            f"{METADATA_DIR_ENV} to a writable, persistent path."
        ) from exc
    if runtime == RUNTIME_KUBERNETES:
        # Read back out of the document rather than re-deriving: this is the
        # path the server itself will open, and materialising the derived
        # kubeconfig a second time would only risk the two disagreeing.
        rendered_kubeconfig = document["kubernetes"].get("kubeconfig_path")
        kubernetes_preflight(
            None if rendered_kubeconfig is None else Path(str(rendered_kubeconfig)),
            document,
        )
    else:
        warn_if_metadata_dir_is_ephemeral(root)
        if _env(EGRESS_DNS_UPSTREAM_ENV, ""):
            try:
                from opensandbox_server.services import helpers as helpers_module
            except Exception as exc:
                raise missing_extra_error(exc) from exc
            allow_host_controlled_egress_dns_upstream(helpers_module)
        redirect_metadata_store_root(metadata_module, root)
        host_ip = str(document["docker"]["host_ip"])
        redirect_publish_host(
            port_allocator,
            host_ip,
            ports=(
                int(document["docker"]["port_range_min"]),
                int(document["docker"]["port_range_max"]),
            ),
        )
        redirect_proxy_host(networking_module, host_ip)
        redirect_public_endpoint_host(networking_module, host_ip)
    return {"app_config": app_config, "log_config": log_config}


def main() -> int:
    """Run the lifecycle API on loopback until it is signalled.

    uvicorn is given the already-imported app OBJECT, not an import string, so
    the import cannot happen before :func:`prepare` has applied the upstream
    compatibility hooks. The uvicorn keyword arguments mirror upstream's own CLI so the
    server behaves identically to ``opensandbox-server`` — the difference is
    where its configuration came from, not how it runs.
    """
    logging.basicConfig(
        level=_env(LOG_LEVEL_ENV, DEFAULT_LOG_LEVEL).upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    prepared = prepare()
    app_config = prepared["app_config"]

    import uvicorn

    from opensandbox_server.main import app

    server = app_config.server
    uvicorn.run(
        app,
        host=SERVER_BIND_HOST,
        port=server.port,
        log_config=prepared["log_config"],
        timeout_keep_alive=server.timeout_keep_alive,
        limit_concurrency=server.limit_concurrency,
        backlog=server.backlog,
        loop=server.loop,
        http=server.http,
        timeout_graceful_shutdown=server.timeout_graceful_shutdown,
    )
    return 0


__all__ = [
    "ALLOCATE_PORTS_ATTR",
    "CONFIG_FILENAME",
    "DEFAULT_EXECD_IMAGE",
    "DEFAULT_INGRESS_MODE",
    "DEFAULT_INGRESS_ROUTE_MODE",
    "DEFAULT_INGRESS_SIGNING_KEY_ID",
    "DEFAULT_KUBE_API_SERVER",
    "DEFAULT_KUBE_IMAGE_PULL_POLICY",
    "DEFAULT_KUBE_INFORMER",
    "DEFAULT_KUBE_NAMESPACE",
    "DEFAULT_KUBE_WORKLOAD_PROVIDER",
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_METADATA_DIRNAME",
    "DEFAULT_NETWORK_MODE",
    "DEFAULT_PIDS_LIMIT",
    "DEFAULT_PORT_RANGE",
    "DEFAULT_PUBLISH_HOST_IP",
    "CREDENTIAL_VAULT_ENV",
    "EGRESS_DNS_UPSTREAM_ENV",
    "DEFAULT_RUNTIME",
    "DEFAULT_SERVER_PORT",
    "EXECD_IMAGE_ENV",
    "EXTRA_NAME",
    "HEALTH_PATH",
    "KUBECONFIG_ENV",
    "KUBECONFIG_FILENAME",
    "KUBE_API_SERVER_ENV",
    "KUBE_IMAGE_PULL_POLICY_ENV",
    "KUBE_INFORMER_ENV",
    "KUBE_NAMESPACE_ENV",
    "KUBE_WORKLOAD_PROVIDER_ENV",
    "INGRESS_GATEWAY_ADDRESS_ENV",
    "INGRESS_MODE_ENV",
    "INGRESS_ROUTE_MODE_ENV",
    "INGRESS_SIGNING_KEY_ENV",
    "INGRESS_SIGNING_KEY_ID_ENV",
    "LOG_LEVEL_ENV",
    "METADATA_DEFAULT_ROOT_ATTR",
    "METADATA_DIR_ENV",
    "METADATA_STORE_CLS_ATTR",
    "NETWORK_MODE_ENV",
    "NETWORKING_MIXIN_ATTR",
    "PIDS_LIMIT_ENV",
    "PORT_RANGE_ENV",
    "PUBLISH_HOST_ATTR",
    "PUBLISH_HOST_IP_ENV",
    "RESOLVE_PROXY_HOST_ATTR",
    "RESOLVE_PUBLIC_HOST_ATTR",
    "UPSTREAM_DNS_ENV",
    "RUNTIME_DOCKER",
    "RUNTIME_ENV",
    "RUNTIME_KUBERNETES",
    "SECURE_ACCESS_ENV",
    "SERVER_BIND_HOST",
    "SERVER_PORT_ENV",
    "SNAPSHOT_STORE_FILENAME",
    "WORKLOAD_RESOURCES",
    "SandboxServerConfigError",
    "config_document",
    "health_url",
    "kube_api_server",
    "kube_image_pull_policy",
    "kube_namespace",
    "kube_workload_provider",
    "kubeconfig_for_server",
    "kubeconfig_source",
    "kubernetes_preflight",
    "kubernetes_ingress",
    "lifecycle_base_url",
    "main",
    "metadata_dir",
    "missing_extra_error",
    "network_mode",
    "allow_host_controlled_egress_dns_upstream",
    "pids_limit",
    "port_range",
    "prepare",
    "publish_host_ip",
    "redirect_metadata_store_root",
    "redirect_proxy_host",
    "redirect_public_endpoint_host",
    "redirect_publish_host",
    "render_config_toml",
    "sandbox_runtime",
    "secure_access_enabled",
    "server_port",
    "verify_kubernetes_access",
    "warn_about_inert_knobs",
    "warn_if_metadata_dir_is_ephemeral",
    "write_config_file",
]


if __name__ == "__main__":
    # A misconfiguration here is an operator's answer, not a defect to debug: the
    # message names the ASTRABOX_* variable and what to do about it, and a
    # traceback through upstream's importer would only bury it. Every other
    # exception keeps its traceback.
    # No component prefix: under the container entry point every line of this
    # process's output is already tagged as the sandbox server's, and tagging it
    # twice reads like two different failures.
    try:
        raise SystemExit(main())
    except SandboxServerConfigError as error:
        print(f"FATAL: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error
