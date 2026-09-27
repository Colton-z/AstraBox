"""The container entry point: AstraBox and the internal services it needs.

``python -m astrabox.deploy.onebox`` is the image's entry point. It configures
database and gateway credentials, then supervises the services independently
selected by the deployment:

* ``open_sandbox`` without ``ASTRABOX_SANDBOX_OPENAPI_BASE_URL`` starts the
  bundled lifecycle server.
* ``litellm`` without ``ASTRABOX_LITELLM_BASE_URL`` starts the bundled model
  proxy. Protected embedded model delivery also starts its private resolver.
* An unset ``ASTRABOX_CHANNEL_GATEWAY_BASE_URL`` starts the bundled channel
  adapter gateway, including when the sandbox service is external.

When none of these services is selected, the entry point replaces itself with
AstraBox through ``exec`` after completing configuration. Otherwise it starts
the selected children and AstraBox under one supervisor.

Supervision contract
--------------------
* **Start order is load-bearing.** The server must be answering before AstraBox
  boots, because AstraBox resolves its sandbox backend during startup. So the
  server comes up first and is polled until healthy, with a bounded wait.
* **The wait fails loud, with evidence.** A server that dies during startup
  (unwritable metadata directory, an upstream constant that moved, no Docker
  socket) exits within a second or two, and the reason is on the server's own
  stderr. Both the exit and the timeout report that output rather than a bare
  "unhealthy" — otherwise the operator gets a wrapper's opinion instead of the
  error.
* **Any child exiting ends the container.** No restart-in-place: a supervisor
  that restarted the server would hide a crash loop behind a healthy-looking
  container, and one that outlived AstraBox would hold the port open with nothing
  serving. Both are cases the container runtime's own restart policy handles
  better, and it can only do so if the container actually exits. The sandbox
  edges are sibling containers, not children, restarted by Docker; a periodic
  read of their addresses ends the container when one moved.
* **Shutdown runs in dependency order.** A signal goes to AstraBox alone, so it
  gets the graceful shutdown its lifespan hook is written for while every
  service it calls is still up. When AstraBox has exited, the other children
  are stopped together, and then the foundation services (the all-in-one
  image's database and pool store) one at a time, each with its own signal.
  The image pairs this with ``tini`` as PID 1, which reaps and forwards; this
  module never needs to be PID 1 itself, only to pass on what it receives.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import pathlib
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from types import FrameType
from typing import Any, Callable, Sequence
from urllib.parse import quote, urlsplit

from astrabox.common.logger.logger_factory import get_logger
from astrabox.deploy import sandbox_server

logger = get_logger(__name__)

#: The backend name that needs a lifecycle server.
OPEN_SANDBOX_BACKEND = "open_sandbox"
BACKEND_ENV = "ASTRABOX_SANDBOX_BACKEND"
BASE_URL_ENV = "ASTRABOX_SANDBOX_OPENAPI_BASE_URL"

START_TIMEOUT_ENV = "ASTRABOX_SANDBOX_SERVER_START_TIMEOUT_SECONDS"
#: Sized for the slowest child: the bundled LiteLLM runs its database
#: migrations on first boot, which took longer than a minute on an 8-vCPU host
#: and 57 seconds even on a later boot. A child that exits is reported at once,
#: so the bound only decides how long a hung child goes unreported.
DEFAULT_START_TIMEOUT_SECONDS = "300"
#: Gap between health polls while the server boots.
_HEALTH_POLL_INTERVAL_SECONDS = 0.25
#: Per-poll HTTP timeout. Short: it is a loopback GET against a route with no
#: dependencies, so a slow answer means "not up yet", not "up but busy".
_HEALTH_REQUEST_TIMEOUT_SECONDS = 2.0
#: How long a child gets to exit on its own after SIGTERM before SIGKILL.
_CHILD_SHUTDOWN_GRACE_SECONDS = 10.0
#: Gap between reads of the sandbox edges' bridge addresses while serving.
_EDGE_WATCH_INTERVAL_SECONDS = 5.0
#: Lines of a child's output kept for the fail-loud message.
_OUTPUT_TAIL_LINES = 40

# Child processes include third-party services. Their error messages are not a
# safe logging boundary: LiteLLM, for example, can include a rejected bearer
# token in an authentication error. Redact both values inherited from
# credential-shaped environment variables and common credential forms before a
# line reaches stdout *or* the diagnostic tail kept in memory.
_SENSITIVE_ENV_NAME = re.compile(
    r"(?:^|_)(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|TOKEN|SECRET|PASSWORD|MASTER_KEY|DATABASE_URL)(?:$|_)",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[ _-]?key|auth[ _-]?token|access[ _-]?token|master[ _-]?key|password)"
    r"\b[\"']?\s*(?:=|:)\s*)([\"']?)([^\s,\"'}]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}")
# A credential in a URL's `token` query parameter. With sandbox endpoints relayed
# through the lifecycle server (ASTRABOX_SANDBOX_ENDPOINT_VIA_SERVER_PROXY), the
# DeepSeek Harness launch-token exchange, `GET <relay>/?token=...`, is a request
# line in that server's access log. The token is 43 base64url characters, which
# no other pattern here matches.
_URL_TOKEN_QUERY = re.compile(r"(?i)([?&]token=)[^\s&#\"']+")
_PROVIDER_KEY = re.compile(r"\bsk-[A-Za-z0-9._~+/=-]{8,}\b")
_LONG_HEX_TOKEN = re.compile(r"\b[0-9a-fA-F]{48,}\b")
_DATABASE_PASSWORD = re.compile(
    r"(?i)(\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis)://[^:\s/@]+:)"
    r"[^@\s/]+(@)"
)
_REDACTED = "[redacted]"


def _sensitive_environment_values() -> tuple[str, ...]:
    """Return credential-shaped values inherited by supervised children.

    Very short values are deliberately omitted: replacing a value such as
    ``test`` or ``true`` throughout a log makes the evidence unreadable. Real
    provider keys, access tokens, generated database passwords and URLs are all
    substantially longer than this floor; common token/URL forms also have the
    structural fallbacks in :func:`_redact_child_output`.
    """

    values = {
        value
        for name, value in os.environ.items()
        if len(value) >= 8 and _SENSITIVE_ENV_NAME.search(name)
    }
    return tuple(sorted(values, key=len, reverse=True))


def _redact_child_output(text: str, *, secret_values: Sequence[str] = ()) -> str:
    """Remove credential material before third-party output reaches the server log."""

    redacted = text
    for value in secret_values:
        redacted = redacted.replace(value, _REDACTED)
    redacted = _DATABASE_PASSWORD.sub(rf"\1{_REDACTED}\2", redacted)
    redacted = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}",
        redacted,
    )
    redacted = _BEARER_TOKEN.sub(rf"\1{_REDACTED}", redacted)
    redacted = _URL_TOKEN_QUERY.sub(rf"\1{_REDACTED}", redacted)
    redacted = _PROVIDER_KEY.sub(_REDACTED, redacted)
    return _LONG_HEX_TOKEN.sub(_REDACTED, redacted)

#: The command that runs AstraBox itself (the console_script from pyproject).
ASTRABOX_COMMAND = ("astrabox", "serve")

# ── the embedded channel adapter gateway ───────────────────────────────────
# Literal mirrors of astrabox.providers.channel_gateway's env names keep the
# launch decision import-light and let the env-registry scanner see every read.
CHANNEL_GATEWAY_BASE_URL_ENV = "ASTRABOX_CHANNEL_GATEWAY_BASE_URL"
CHANNEL_GATEWAY_TOKEN_ENV = "ASTRABOX_CHANNEL_GATEWAY_TOKEN"
CHANNEL_GATEWAY_HOST_ENV = "ASTRABOX_CHANNEL_GATEWAY_HOST"
CHANNEL_GATEWAY_PORT_ENV = "ASTRABOX_CHANNEL_GATEWAY_PORT"
CHANNEL_GATEWAY_MANIFEST_ENV = "ASTRABOX_CHANNEL_GATEWAY_MANIFEST"
CHANNEL_GATEWAY_HOST = "127.0.0.1"
CHANNEL_GATEWAY_PORT = 8765
CHANNEL_GATEWAY_BIN = "/opt/astrabox/channel-gateway/bin/node"
CHANNEL_GATEWAY_SERVER_PATH = "/opt/astrabox/channel-gateway/src/server.mjs"
CHANNEL_GATEWAY_MANIFEST_PATH = (
    "/opt/astrabox/channel-gateway/channel_gateway_manifest.json"
)

# ── the embedded LiteLLM gateway ────────────────────────────────────────────
#: Env names + port come from the provider module so the address the gateway
#: binds and the address the platform derives cannot drift apart.
MODEL_PROVIDER_ENV = "ASTRABOX_MODEL_ENDPOINT_PROVIDER"
#: The provider name whose gateway this container embeds (pinned against
#: Settings' default by a test, like OPEN_SANDBOX_BACKEND above).
LITELLM_PROVIDER = "litellm"
LITELLM_MASTER_KEY_ENV = "LITELLM_MASTER_KEY"
AUTH_SESSION_SECRET_ENV = "ASTRABOX_AUTH_SESSION_SECRET"
#: Literal mirrors of astrabox.providers.model's env names (module-level
#: literals so the env-registry scanner resolves every read here; pinned
#: against the provider module by a test).
LITELLM_BASE_URL_ENV_NAME = "ASTRABOX_LITELLM_BASE_URL"
LITELLM_API_KEY_ENV_NAME = "ASTRABOX_LITELLM_API_KEY"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_AUTH_TOKEN_ENV = "ANTHROPIC_AUTH_TOKEN"
ANTHROPIC_BASE_URL_ENV = "ANTHROPIC_BASE_URL"
ANTHROPIC_MODEL_ENV = "ANTHROPIC_MODEL"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
#: Literal mirrors of the title-model settings (astrabox.common.utils.settings).
TITLE_MODEL_NAME_ENV = "ASTRABOX_TITLE_MODEL_NAME"
TITLE_MODEL_BASE_URL_ENV = "ASTRABOX_TITLE_MODEL_BASE_URL"
#: The bundled gateway's native DeepSeek route prefix (containers/litellm/config.yaml).
DEEPSEEK_NATIVE_ROUTE_PREFIX = "deepseek/"
#: Where the image bakes the proxy venv + config (containers/server/Dockerfile).
LITELLM_BIN = "/opt/litellm/bin/litellm"
LITELLM_CONFIG_PATH = "/opt/astrabox/litellm/config.yaml"
#: The generated master key's file, under the state dir (vault.key shape).
LITELLM_KEY_FILENAME = "litellm.key"

# ── local Compose database secrets ─────────────────────────────────────────
# Docker Compose mounts these as service-scoped files. The launcher turns each
# password into the URL consumed by its child only after the container starts,
# so ``docker inspect`` contains file paths rather than database credentials.
ASTRABOX_DB_URL_ENV = "ASTRABOX_DB_URL"
ASTRABOX_DB_PASSWORD_FILE_ENV = "ASTRABOX_DB_PASSWORD_FILE"
ASTRABOX_DB_HOST_ENV = "ASTRABOX_DB_HOST"
ASTRABOX_DB_PORT_ENV = "ASTRABOX_DB_PORT"
LITELLM_DATABASE_URL_ENV = "DATABASE_URL"
LITELLM_DATABASE_PASSWORD_FILE_ENV = "LITELLM_DATABASE_PASSWORD_FILE"
LITELLM_DATABASE_HOST_ENV = "LITELLM_DATABASE_HOST"
LITELLM_DATABASE_PORT_ENV = "LITELLM_DATABASE_PORT"

# ── private DNS for the protected embedded gateway ─────────────────────────
CREDENTIAL_VAULT_ENV = "ASTRABOX_SANDBOX_CREDENTIAL_VAULT"
EGRESS_DNS_UPSTREAM_ENV = "ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM"
EGRESS_DNS_UPSTREAM_DEFAULT_ENV = "ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM_DEFAULT"
GATEWAY_DNS_ADDRESS_ENV = "ASTRABOX_SANDBOX_GATEWAY_IP"
SANDBOX_EDGE_SERVICE_ENV = "ASTRABOX_SANDBOX_EDGE_SERVICE"
SANDBOX_DNS_EDGE_SERVICE_ENV = "ASTRABOX_SANDBOX_DNS_EDGE_SERVICE"
SANDBOX_EDGE_CALLBACK_PORT_ENV = "ASTRABOX_SANDBOX_EDGE_CALLBACK_PORT"
SANDBOX_EDGE_NETWORK_ENV = "ASTRABOX_SANDBOX_EDGE_NETWORK"
EGRESS_DENY_CIDRS_ENV = "ASTRABOX_SANDBOX_EGRESS_DENY_CIDRS"
MCP_PROXY_BASE_URL_ENV = "ASTRABOX_MCP_PROXY_BASE_URL"
SANDBOX_SERVER_RUNTIME_ENV = "ASTRABOX_SANDBOX_SERVER_RUNTIME"
GATEWAY_DNS_PORT = 5353
DEFAULT_SANDBOX_EDGE_CALLBACK_PORT = 8000
COREDNS_BIN = "/opt/coredns/coredns"
COREDNS_CONFIG_PATH = "/opt/astrabox/coredns/Corefile"


class OneBoxError(RuntimeError):
    """The container cannot be brought up; the message is the operator's answer."""


def _env(name: str, default: str) -> str:
    return str(os.environ.get(name) or default).strip()


def _read_database_password(configured: str, *, env_name: str) -> str:
    if not configured:
        return ""
    path = pathlib.Path(configured)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise OneBoxError(
            f"{env_name} points at an unreadable database secret file: {path} ({exc})"
        ) from exc
    if not value:
        raise OneBoxError(f"{env_name} points at an empty database secret file: {path}")
    return value


def _configured_tcp_port(configured: str, *, env_name: str) -> int:
    try:
        port = int(configured)
    except ValueError as exc:
        raise OneBoxError(f"{env_name}={configured!r} must be a TCP port") from exc
    if not 1 <= port <= 65535:
        raise OneBoxError(f"{env_name}={configured!r} must be between 1 and 65535")
    return port


def ensure_database_wiring() -> None:
    """Build child-process database URLs from Compose secret files.

    Explicit URLs always win. File-based wiring is the maintained local
    deployment path: it keeps plaintext values out of the Compose model while
    still giving AstraBox and the embedded LiteLLM process separate roles.
    """

    if not _env(ASTRABOX_DB_URL_ENV, ""):
        password = _read_database_password(
            _env(ASTRABOX_DB_PASSWORD_FILE_ENV, ""),
            env_name=ASTRABOX_DB_PASSWORD_FILE_ENV,
        )
        if password:
            host = _env(ASTRABOX_DB_HOST_ENV, "postgres")
            port = _configured_tcp_port(
                _env(ASTRABOX_DB_PORT_ENV, "5432"),
                env_name=ASTRABOX_DB_PORT_ENV,
            )
            os.environ[ASTRABOX_DB_URL_ENV] = (
                "postgresql+asyncpg://astrabox:"
                f"{quote(password, safe='')}@{host}:{port}/astrabox"
            )

    if not _env(LITELLM_DATABASE_URL_ENV, ""):
        password = _read_database_password(
            _env(LITELLM_DATABASE_PASSWORD_FILE_ENV, ""),
            env_name=LITELLM_DATABASE_PASSWORD_FILE_ENV,
        )
        if password:
            host = _env(LITELLM_DATABASE_HOST_ENV, "postgres")
            port = _configured_tcp_port(
                _env(LITELLM_DATABASE_PORT_ENV, "5432"),
                env_name=LITELLM_DATABASE_PORT_ENV,
            )
            os.environ[LITELLM_DATABASE_URL_ENV] = (
                "postgresql://litellm:"
                f"{quote(password, safe='')}@{host}:{port}/litellm"
            )


def needs_sandbox_server() -> bool:
    """Whether this container must run a lifecycle server of its own.

    True only for the ``open_sandbox`` backend with no lifecycle base URL
    configured. An operator who points at their own server keeps it: this module
    does not start a competitor, and the address they set is the one used.

    An unset backend variable resolves to the same default the app resolves
    (:data:`OPEN_SANDBOX_BACKEND`, pinned against ``Settings`` by a test). Reading
    it as "no backend" here would be the one incoherent combination: the app
    would boot on ``open_sandbox`` while this decided nothing needed supervising,
    and the deployment would come up with no lifecycle server to talk to.
    """
    from astrabox.config.settings import normalize_configured_sandbox_backend

    backend = normalize_configured_sandbox_backend(
        _env(BACKEND_ENV, OPEN_SANDBOX_BACKEND)
    )
    if backend != OPEN_SANDBOX_BACKEND:
        return False
    return not _env(BASE_URL_ENV, "")


def needs_litellm_gateway() -> bool:
    """Whether this container must run the embedded LiteLLM proxy.

    True for the default ``litellm`` endpoint provider with no external
    ``ASTRABOX_LITELLM_BASE_URL`` configured. An operator who points at their
    own gateway keeps it — nothing extra starts, exactly the lifecycle-server
    rule above applied to the model plane.
    """
    provider = _env(MODEL_PROVIDER_ENV, LITELLM_PROVIDER).lower()
    if provider != LITELLM_PROVIDER:
        return False
    return not _env(LITELLM_BASE_URL_ENV_NAME, "")


def needs_channel_gateway() -> bool:
    """Whether this container must run its bundled channel adapter gateway."""

    return not _env(CHANNEL_GATEWAY_BASE_URL_ENV, "")


def ensure_channel_gateway_wiring(*, embedded: bool | None = None) -> str:
    """Validate external wiring or export private wiring for the bundled gateway."""

    from astrabox.providers.channel_gateway import (
        channel_gateway_base_url,
        channel_gateway_token,
    )

    run_embedded = needs_channel_gateway() if embedded is None else embedded
    if not run_embedded:
        try:
            base_url = channel_gateway_base_url(_env(CHANNEL_GATEWAY_BASE_URL_ENV, ""))
            channel_gateway_token(_env(CHANNEL_GATEWAY_TOKEN_ENV, ""))
        except RuntimeError as exc:
            raise OneBoxError(str(exc)) from exc
        os.environ[CHANNEL_GATEWAY_BASE_URL_ENV] = base_url
        return base_url

    host = _env(CHANNEL_GATEWAY_HOST_ENV, CHANNEL_GATEWAY_HOST).lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise OneBoxError(
            f"the embedded {CHANNEL_GATEWAY_HOST_ENV} must be loopback; use "
            f"{CHANNEL_GATEWAY_BASE_URL_ENV} for an external channel gateway"
        )
    port = _configured_tcp_port(
        _env(CHANNEL_GATEWAY_PORT_ENV, str(CHANNEL_GATEWAY_PORT)),
        env_name=CHANNEL_GATEWAY_PORT_ENV,
    )
    token = _env(CHANNEL_GATEWAY_TOKEN_ENV, "")
    if token and len(token) < 32:
        raise OneBoxError(
            f"{CHANNEL_GATEWAY_TOKEN_ENV} must contain at least 32 characters"
        )
    if not token:
        import secrets

        token = secrets.token_urlsafe(32)
        os.environ[CHANNEL_GATEWAY_TOKEN_ENV] = token

    display_host = f"[{host}]" if ":" in host else host
    base_url = channel_gateway_base_url(f"http://{display_host}:{port}")
    os.environ[CHANNEL_GATEWAY_BASE_URL_ENV] = base_url
    os.environ[CHANNEL_GATEWAY_HOST_ENV] = host
    os.environ[CHANNEL_GATEWAY_PORT_ENV] = str(port)
    export_default(CHANNEL_GATEWAY_MANIFEST_ENV, CHANNEL_GATEWAY_MANIFEST_PATH)
    try:
        channel_gateway_token(token)
    except RuntimeError as exc:  # pragma: no cover - length checked above
        raise OneBoxError(str(exc)) from exc
    return base_url


def _credential_vault_enabled() -> bool:
    value = _env(CREDENTIAL_VAULT_ENV, "true").lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise OneBoxError(
        f"{CREDENTIAL_VAULT_ENV}={value!r} must be a boolean "
        "(1/true/yes/on or 0/false/no/off)"
    )


def needs_gateway_dns() -> bool:
    """The embedded HTTP gateway needs a private FQDN while Vault is on."""

    return needs_litellm_gateway() and _credential_vault_enabled()


@dataclass(frozen=True)
class EdgeOwner:
    """The Docker labels that find one deployment's sandbox edges and their network.

    Compose labels every container and network of a project with the project
    and the service or network key it declares (:func:`_compose_edge_owner`).
    The all-in-one image creates its edges and their network itself and labels
    them with its installation and a role (:mod:`astrabox.deploy.all_in_one`).
    Finding them is the only step the two shapes do differently: the
    connection to the private network, the addresses sandboxes are given and
    the bridge deny list are one path. ``ASTRABOX_SANDBOX_EDGE_SERVICE``,
    ``ASTRABOX_SANDBOX_DNS_EDGE_SERVICE`` and ``ASTRABOX_SANDBOX_EDGE_NETWORK``
    are the values of ``service_key`` and ``network_key`` under these labels.
    """

    #: ``key=value`` labels every edge and the network carry.
    labels: tuple[str, ...]
    #: The label whose value is an edge's service or role name.
    service_key: str
    #: The label whose value is the private network's key.
    network_key: str
    #: Who owns them, for error messages.
    description: str


def _compose_edge_owner(client: Any) -> EdgeOwner:
    """The Compose project this server container belongs to, from its labels."""

    current = client.containers.get(socket.gethostname())
    labels = dict(current.attrs.get("Config", {}).get("Labels") or {})
    project = str(labels.get("com.docker.compose.project") or "").strip()
    if not project:
        raise OneBoxError(
            "cannot discover the sandbox edges: the server container has no "
            "com.docker.compose.project label"
        )
    return EdgeOwner(
        labels=(f"com.docker.compose.project={project}",),
        service_key="com.docker.compose.service",
        network_key="com.docker.compose.network",
        description=f"Compose project {project!r}",
    )


def _edge_container(client: Any, owner: EdgeOwner, service_name: str) -> Any:
    """The one running container of a sandbox edge."""

    matches = client.containers.list(
        filters={
            "status": "running",
            "label": [*owner.labels, f"{owner.service_key}={service_name}"],
        }
    )
    if len(matches) != 1:
        raise OneBoxError(
            f"cannot discover sandbox edge {service_name!r} of {owner.description}: "
            f"expected one running container, found {len(matches)}"
        )
    return matches[0]


def _detect_edge_ip(service_name: str, owner: EdgeOwner | None) -> str:
    """Resolve one sandbox edge's address on Docker's built-in bridge.

    The edges deliberately live on the built-in bridge because OpenSandbox's
    Docker egress sidecar enforces networkPolicy only there. That network has
    no DNS of its own and assigns addresses dynamically, so the server finds an
    edge by the labels its owner put on it (``None``: this container's Compose
    project). No fixed container name or bridge IP is needed.
    """

    try:
        import docker

        client = docker.from_env()
        try:
            container = _edge_container(
                client, owner or _compose_edge_owner(client), service_name
            )
            networks = dict(container.attrs.get("NetworkSettings", {}).get("Networks") or {})
            address = str((networks.get("bridge") or {}).get("IPAddress") or "").strip()
            if not address:
                raise OneBoxError(
                    f"sandbox edge {service_name!r} is not attached to Docker's "
                    "built-in bridge"
                )
            return address
        finally:
            client.close()
    except OneBoxError:
        raise
    except Exception as exc:
        raise OneBoxError(
            f"cannot discover sandbox edge {service_name!r} through the Docker socket: {exc}"
        ) from exc


def ensure_sandbox_edge_network(owner: EdgeOwner | None = None) -> str:
    """Connect the sandbox edges to the private network they reach the server on.

    Sandboxes live on Docker's built-in bridge, where the only platform address
    they may reach is the edge. The edges forward to this server by the name
    ``server`` over an ``internal`` network no sandbox joins, so no server port
    is published anywhere a sandbox's egress can reach. Compose cannot put one
    service on the built-in bridge and a second network: it gives every
    endpoint network-scoped aliases, which Docker refuses on the built-in
    bridge. The edges are therefore created on the bridge, and this step
    connects them to the private network through the Docker API; the
    connection survives an edge restart. Under Compose a recreated edge is
    connected again when the server restarts with it (the server's
    ``depends_on`` on the edges sets ``restart: true``); the all-in-one image
    recreates its edges only while it starts.

    ``owner`` finds the edges and the network; ``None`` is this container's
    Compose project.
    """

    services = [
        name
        for name in (
            _env(SANDBOX_EDGE_SERVICE_ENV, ""),
            _env(SANDBOX_DNS_EDGE_SERVICE_ENV, ""),
        )
        if name
    ]
    if not services:
        return ""
    network_key = _env(SANDBOX_EDGE_NETWORK_ENV, "")
    if not network_key:
        raise OneBoxError(
            f"{SANDBOX_EDGE_SERVICE_ENV} is set but {SANDBOX_EDGE_NETWORK_ENV} is "
            "not: the sandbox edges reach this server only over that private "
            "network"
        )
    try:
        import docker

        client = docker.from_env()
        try:
            owner = owner or _compose_edge_owner(client)
            networks = client.networks.list(
                filters={"label": [*owner.labels, f"{owner.network_key}={network_key}"]}
            )
            if len(networks) != 1:
                raise OneBoxError(
                    f"cannot find network {network_key!r} of {owner.description}: "
                    f"expected one, found {len(networks)}"
                )
            network = networks[0]
            own = client.containers.get(socket.gethostname())
            own_networks = dict(own.attrs.get("NetworkSettings", {}).get("Networks") or {})
            if network.name not in own_networks:
                raise OneBoxError(
                    f"the server container is not attached to {network.name!r}, so "
                    "the sandbox edges could not reach it there"
                )
            for service_name in services:
                container = _edge_container(client, owner, service_name)
                attached = dict(
                    container.attrs.get("NetworkSettings", {}).get("Networks") or {}
                )
                if network.name not in attached:
                    network.connect(container)
                    logger.info(
                        "connected sandbox edge %r to private network %s",
                        service_name,
                        network.name,
                    )
            return str(network.name)
        finally:
            client.close()
    except OneBoxError:
        raise
    except Exception as exc:
        raise OneBoxError(
            "cannot connect the sandbox edges to their private network through "
            f"the Docker socket: {exc}"
        ) from exc


def _detect_bridge_subnets() -> list[ipaddress.IPv4Network]:
    """The IPv4 subnets of Docker's built-in bridge, from its IPAM config."""

    try:
        import docker

        client = docker.from_env()
        try:
            configs = list(
                (client.networks.get("bridge").attrs.get("IPAM") or {}).get("Config") or []
            )
        finally:
            client.close()
    except Exception as exc:
        raise OneBoxError(
            f"cannot read Docker's built-in bridge subnet through the Docker socket: {exc}"
        ) from exc
    subnets = []
    for config in configs:
        network = ipaddress.ip_network(str((config or {}).get("Subnet") or ""), strict=False)
        if isinstance(network, ipaddress.IPv4Network):
            subnets.append(network)
    if not subnets:
        raise OneBoxError("Docker's built-in bridge reports no IPv4 subnet")
    return subnets


def ensure_sandbox_bridge_isolation(edge_address: str) -> str:
    """Deny every sandbox the rest of Docker's built-in bridge.

    All sandboxes share the built-in bridge, and its gateway is where each
    sandbox's execd and file server are published (``ASTRABOX_PUBLISH_HOST_IP``).
    OpenSandbox's egress sidecar in ``dns+nft`` mode drops IP/CIDR deny
    targets before it consults any allow set, so denying the bridge subnets
    minus the HTTP edge keeps a sandbox off other sandboxes and off the gateway
    in every networking mode: an Unrestricted Environment, and any allow-list
    entry that names a bridge address, included. The edge stays reachable. The
    DNS edge needs no exception: the sidecar's DNS proxy marks its upstream
    traffic, and the sidecar accepts marked packets before the deny set.

    An explicit ``ASTRABOX_SANDBOX_EGRESS_DENY_CIDRS`` is the operator's list and
    is kept as given. Either way, a callback base whose address the list denies
    is refused here: sandboxes could never reach it, and every platform MCP call
    and transcript flush would time out instead.
    """

    value = _env(EGRESS_DENY_CIDRS_ENV, "") or _derived_bridge_deny_cidrs(edge_address)
    os.environ[EGRESS_DENY_CIDRS_ENV] = value
    try:
        denied = [
            ipaddress.ip_network(item.strip(), strict=True)
            for item in value.split(",")
            if item.strip()
        ]
    except ValueError as exc:
        raise OneBoxError(
            f"{EGRESS_DENY_CIDRS_ENV}={value!r} is not a list of networks: {exc}"
        ) from exc
    callback_host = urlsplit(_env(MCP_PROXY_BASE_URL_ENV, "")).hostname or ""
    try:
        callback_address = ipaddress.ip_address(callback_host)
    except ValueError:
        return value
    if any(callback_address in network for network in denied):
        raise OneBoxError(
            f"{MCP_PROXY_BASE_URL_ENV} names {callback_address}, which "
            f"{EGRESS_DENY_CIDRS_ENV} denies to every sandbox. Leave "
            f"{MCP_PROXY_BASE_URL_ENV} unset so sandboxes call back through the "
            "sandbox edge."
        )
    return value


def _derived_bridge_deny_cidrs(edge_address: str) -> str:
    """Docker's built-in bridge subnets minus the sandbox edge, comma-joined."""

    try:
        edge = ipaddress.IPv4Address(edge_address)
    except ValueError as exc:
        raise OneBoxError(
            f"the sandbox edge address {edge_address!r} is not an IPv4 address"
        ) from exc
    denied: list[ipaddress.IPv4Network] = []
    for subnet in _detect_bridge_subnets():
        if edge in subnet:
            denied.extend(subnet.address_exclude(ipaddress.IPv4Network(f"{edge}/32")))
        else:
            denied.append(subnet)
    logger.info(
        "sandbox egress denies Docker's built-in bridge except the sandbox edge %s",
        edge,
    )
    return ",".join(str(network) for network in sorted(denied))


def ensure_sandbox_edge_wiring(owner: EdgeOwner | None = None) -> str:
    """Route sandbox callbacks through a single-purpose bridge container.

    A networkPolicy rule names a host, not a port. Allowing the Docker bridge
    gateway for callbacks would therefore also allow every unrelated service a
    host operator published there (for example ``0.0.0.0:5432``). The maintained
    Compose stack and the all-in-one image instead permit a tiny proxy
    container that exposes only the model and capability-scoped callback
    surfaces. ``owner`` finds it (:func:`ensure_sandbox_edge_network`).

    Deployments that do not set ``ASTRABOX_SANDBOX_EDGE_SERVICE`` keep their
    explicit callback/model topology unchanged.
    """

    service_name = _env(SANDBOX_EDGE_SERVICE_ENV, "")
    if not service_name:
        return ""
    if _env(SANDBOX_SERVER_RUNTIME_ENV, "docker").lower() != "docker":
        raise OneBoxError(
            f"{SANDBOX_EDGE_SERVICE_ENV} is a Docker Compose boundary and cannot "
            "be used with a non-Docker OpenSandbox runtime"
        )

    address = _env(GATEWAY_DNS_ADDRESS_ENV, "") or _detect_edge_ip(service_name, owner)
    os.environ[GATEWAY_DNS_ADDRESS_ENV] = address
    if not _env(MCP_PROXY_BASE_URL_ENV, ""):
        port = _configured_tcp_port(
            _env(
                SANDBOX_EDGE_CALLBACK_PORT_ENV,
                str(DEFAULT_SANDBOX_EDGE_CALLBACK_PORT),
            ),
            env_name=SANDBOX_EDGE_CALLBACK_PORT_ENV,
        )
        os.environ[MCP_PROXY_BASE_URL_ENV] = f"http://{address}:{port}"
    return address


def ensure_sandbox_dns_edge_wiring(owner: EdgeOwner | None = None) -> str:
    """Use a single-purpose bridge container as the sandbox DNS upstream.

    OpenSandbox's Docker egress policy treats an allowed DNS-upstream host as a
    reachable host. Pointing it at the Docker bridge gateway therefore exposes
    unrelated services on that gateway. The maintained Compose stack and the
    all-in-one image insert a DNS-only forwarder and let that container, not
    the Agent, reach the server's resolver over the private network. ``owner``
    finds it (:func:`ensure_sandbox_edge_network`).
    """

    service_name = _env(SANDBOX_DNS_EDGE_SERVICE_ENV, "")
    if not service_name:
        return ""
    if _env(SANDBOX_SERVER_RUNTIME_ENV, "docker").lower() != "docker":
        raise OneBoxError(
            f"{SANDBOX_DNS_EDGE_SERVICE_ENV} is a Docker Compose boundary and "
            "cannot be used with a non-Docker OpenSandbox runtime"
        )
    address = _detect_edge_ip(service_name, owner)
    if not _env(EGRESS_DNS_UPSTREAM_ENV, ""):
        os.environ[EGRESS_DNS_UPSTREAM_ENV] = f"{address}:53"
    return address


def ensure_gateway_dns_wiring() -> str:
    """Point sidecars at the bundled resolver and map its private gateway IP."""

    runtime = _env(SANDBOX_SERVER_RUNTIME_ENV, "docker").lower()
    if runtime != "docker":
        raise OneBoxError(
            "the embedded protected model gateway is available only when the "
            "bundled sandbox server uses Docker. Kubernetes sandboxes need an "
            "external model gateway with a cluster-resolvable FQDN (set "
            f"{LITELLM_BASE_URL_ENV_NAME})."
        )
    # The address the private gateway name resolves to, and so a host every
    # sandbox may reach. The Compose stack sets it to the sandbox edge
    # (ensure_sandbox_edge_wiring). It is never this container's own address:
    # a sandbox allowed that host could reach every port the server listens
    # on, the unauthenticated API included.
    address = _env(GATEWAY_DNS_ADDRESS_ENV, "")
    if not address:
        raise OneBoxError(
            f"{GATEWAY_DNS_ADDRESS_ENV} is unset, so the protected embedded model "
            "gateway has no address sandboxes may reach. Run the maintained "
            f"Compose stack, whose sandbox edge supplies it ({SANDBOX_EDGE_SERVICE_ENV}), "
            f"set {GATEWAY_DNS_ADDRESS_ENV} to a single-purpose proxy's address, use "
            f"an external model gateway with a resolvable FQDN ({LITELLM_BASE_URL_ENV_NAME}), "
            f"or turn {CREDENTIAL_VAULT_ENV} off explicitly."
        )
    os.environ[GATEWAY_DNS_ADDRESS_ENV] = address
    if not _env(EGRESS_DNS_UPSTREAM_ENV, ""):
        os.environ[EGRESS_DNS_UPSTREAM_ENV] = _env(
            EGRESS_DNS_UPSTREAM_DEFAULT_ENV,
            f"{address}:{GATEWAY_DNS_PORT}",
        )
    return address


def ensure_litellm_provider_wiring() -> None:
    """Configure the bundled gateway for an Anthropic-compatible upstream.

    Claude Code commonly uses ``ANTHROPIC_AUTH_TOKEN`` while LiteLLM expects
    ``ANTHROPIC_API_KEY``. Keep an explicit API key when both are present.

    ``ANTHROPIC_MODEL`` is the deployment default used only when an Agent does
    not select a model. Normalize that default to the bundled gateway's
    ``anthropic/*`` route; an explicit Agent or Assistant model remains the
    routing authority. DeepSeek publishes both Anthropic Messages and OpenAI
    routes, so different engines can select different routes in the same proxy.

    Titles and process summaries send ``reasoning_effort: "none"``. On the
    Anthropic route LiteLLM drops that option, and DeepSeek then thinks by
    default until the small label budget runs out. For DeepSeek the default
    title route is therefore ``deepseek/<model>``: LiteLLM's native DeepSeek
    adapter maps ``none`` to ``thinking: {"type": "disabled"}``. An explicit
    ``ASTRABOX_TITLE_MODEL_NAME``, or a title endpoint of the operator's own
    (``ASTRABOX_TITLE_MODEL_BASE_URL``), is kept.
    """

    api_key = _env(ANTHROPIC_API_KEY_ENV, "")
    auth_token = _env(ANTHROPIC_AUTH_TOKEN_ENV, "")
    if not api_key and auth_token:
        os.environ[ANTHROPIC_API_KEY_ENV] = auth_token

    base_url = _env(ANTHROPIC_BASE_URL_ENV, "")
    if not base_url:
        return
    deepseek = (urlsplit(base_url).hostname or "").lower() == "api.deepseek.com"
    if deepseek:
        deepseek_key = _env(DEEPSEEK_API_KEY_ENV, "") or api_key or auth_token
        if deepseek_key:
            os.environ[DEEPSEEK_API_KEY_ENV] = deepseek_key

    upstream_model = _env(ANTHROPIC_MODEL_ENV, "")
    if not upstream_model:
        return
    route_model = upstream_model.removeprefix("anthropic/")
    os.environ[ANTHROPIC_MODEL_ENV] = f"anthropic/{route_model}"
    if deepseek and not _env(TITLE_MODEL_BASE_URL_ENV, ""):
        export_default(TITLE_MODEL_NAME_ENV, f"{DEEPSEEK_NATIVE_ROUTE_PREFIX}{route_model}")


def ensure_litellm_master_key() -> str:
    """Resolve the bundled provider service credential.

    The proxy must never run open — a sandbox executes untrusted code and can
    reach the gateway port, so an unauthenticated proxy would hand every tenant
    the deployment's provider credentials. An explicit ``LITELLM_MASTER_KEY``
    wins. A one-node deployment otherwise persists the credential at
    ``<state_dir>/litellm.key`` with mode 0600 and exports it to the proxy and
    server-side gateway administration.
    """
    import base64
    import secrets

    key = _env(LITELLM_MASTER_KEY_ENV, "")
    if not key:
        from astrabox.config.settings import get_settings

        state_dir = get_settings().resolved_state_dir()
        key_path = state_dir / LITELLM_KEY_FILENAME
        if key_path.exists():
            key = key_path.read_text(encoding="ascii").strip()
        if not key:
            key = "sk-astrabox-" + base64.urlsafe_b64encode(secrets.token_bytes(24)).decode("ascii").rstrip("=")
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_text(key + "\n", encoding="ascii")
            key_path.chmod(0o600)
            logger.info("litellm master key generated at %s", key_path)
        os.environ[LITELLM_MASTER_KEY_ENV] = key
    # The inference credential is distinct from the master key, which the
    # gateway maps to PROXY_ADMIN. The model provider mints a scoped capability;
    # ASTRABOX_LITELLM_API_KEY stays unset unless an operator points this
    # deployment at a gateway with its own key system.
    return key


def _verify_sandbox_inference_key(
    key: str, call: Callable[..., tuple[int, str]]
) -> str:
    """Spend the key once, on the cheapest route, before the deployment serves.

    Creating the key proves the proxy's key table holds it. It does not prove a
    request carrying it is accepted, and that is a separate mechanism: the
    adapter declines credentials that are not AstraBox's, and LiteLLM only falls
    back to its own key authentication because ``custom_auth_settings.mode`` is
    ``auto``. Drop the setting, or ship an image without the enterprise package
    the setting is implemented in, and every sandbox's first model call fails
    while key creation still succeeds.

    So the startup check is a real request with the real credential. It costs
    one call and it fails here, loudly, instead of once per conversation as a
    model outage.
    """

    status, body = call("/v1/models", None, credential=key)
    if status == 200:
        return key
    raise RuntimeError(
        f"the model gateway does not accept the key this deployment gives its "
        f"sandboxes (/v1/models returned {status} {body.strip()[:200]}). Every "
        f"turn would fail on its first model call. Check that "
        f"general_settings.custom_auth_settings.mode is 'auto' and that the "
        f"proxy image carries litellm-enterprise, which implements it"
    )


def ensure_sandbox_inference_key() -> str:
    """Create the sandbox's LiteLLM virtual key if the proxy does not hold it.

    Runs once, after the gateway reports healthy and before AstraBox serves. A
    sandbox is handed this key when its box is created, so a deployment that
    served a turn before the key existed would 401 on its first model call. If
    this cannot be done, the deployment must not come up.

    ``key_type: llm_api`` is LiteLLM's own name for "may call the LLM API, may
    not manage the proxy" — ``handle_key_type`` turns it into the maintained
    ``llm_api_routes`` preset. That is what keeps a sandbox off the management
    surface, which sits under the same ``/v1`` prefix the sandbox's model
    binding admits. Measured against a deployed gateway: creating keys or users,
    adding models, reading spend, and writing MCP servers are all 403. The one
    management path such a key may read is ``GET /v1/mcp/server``, which LiteLLM
    serves to any authenticated key with ``url``, ``command``, ``extra_headers``
    and ``mcp_info`` redacted — a key that may call MCP tools may see which
    servers exist, and no credential material.

    The value is supplied rather than generated: ``/key/generate`` accepts a
    caller-supplied ``key``, and deriving it from the signing secret is what
    lets every replica name the same key without one of them storing it.
    """

    import json

    from astrabox.identity.session_signing import session_signing_secret
    from astrabox.providers.model import LiteLLMModelEndpointProvider
    from astrabox.providers.litellm_shared_auth import (
        SANDBOX_INFERENCE_KEY_ALIAS,
        SANDBOX_INFERENCE_KEY_TYPE,
        sandbox_inference_key,
    )

    master_key = _env(LITELLM_MASTER_KEY_ENV, "")
    if not master_key:
        raise RuntimeError(
            "cannot provision the sandbox model key without LITELLM_MASTER_KEY"
        )
    key = sandbox_inference_key(session_signing_secret())
    base = LiteLLMModelEndpointProvider.server_side_base_url().rstrip("/")
    headers = {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json",
    }

    def _call(
        path: str, payload: dict[str, str] | None, *, credential: str = ""
    ) -> tuple[int, str]:
        request = urllib.request.Request(
            f"{base}{path}",
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            headers=headers if not credential else {**headers, "Authorization": f"Bearer {credential}"},
            method="GET" if payload is None else "POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    probe = f"/key/info?key={quote(key, safe='')}"
    status, _ = _call(probe, None)
    if status == 200:
        logger.info(
            "sandbox model key %s is already provisioned", SANDBOX_INFERENCE_KEY_ALIAS
        )
        return _verify_sandbox_inference_key(key, _call)

    status, body = _call(
        "/key/generate",
        {
            "key": key,
            "key_alias": SANDBOX_INFERENCE_KEY_ALIAS,
            "key_type": SANDBOX_INFERENCE_KEY_TYPE,
            "user_id": "astrabox-sandbox",
        },
    )
    if status // 100 == 2:
        logger.info("sandbox model key %s provisioned", SANDBOX_INFERENCE_KEY_ALIAS)
        return _verify_sandbox_inference_key(key, _call)

    # A second replica may have created it between the two calls. Re-ask rather
    # than reading the failure text: the question is whether the key exists, and
    # /key/info answers exactly that.
    recheck, _ = _call(probe, None)
    if recheck == 200:
        logger.info(
            "sandbox model key %s was provisioned concurrently",
            SANDBOX_INFERENCE_KEY_ALIAS,
        )
        return _verify_sandbox_inference_key(key, _call)
    raise RuntimeError(
        f"could not provision the sandbox model key: /key/generate returned "
        f"{status} {body.strip()[:400]}"
    )


def export_default(name: str, value: str) -> None:
    """Put a value in the environment children inherit, unless one is set.

    ``os.environ.setdefault`` is the wrong primitive here. Compose renders an
    unset ``${VAR:-}`` as an *empty string* (``containers/compose.yaml``), so
    the key exists and setdefault keeps that empty value. A child then reads
    "configured as empty" where the deployment means "not configured" — and a
    child in its own virtualenv, as LiteLLM is, cannot fall back to the code
    path that would have derived the value, because that path imports
    ``astrabox``.

    Treating an empty value as absent is therefore the contract for anything
    handed to a child through the environment.
    """

    if not str(os.environ.get(name) or "").strip():
        os.environ[name] = value


def ensure_shared_identity_key() -> str:
    """Materialize the existing AstraBox session key before adapters start.

    LiteLLM runs in a separate venv and reads the same key from its inherited
    environment. The key signs typed browser capabilities as well as AstraBox's
    OIDC session cookie; token purpose and audience keep those uses separate.
    """

    from astrabox.identity.session_signing import session_signing_secret

    secret = session_signing_secret()
    export_default(AUTH_SESSION_SECRET_ENV, secret)
    return secret


def litellm_health_url() -> str:
    from astrabox.providers.model import EMBEDDED_LITELLM_PORT

    return f"http://127.0.0.1:{EMBEDDED_LITELLM_PORT}/health/liveliness"


def channel_gateway_health_url() -> str:
    """The dependency-free health route of the selected embedded gateway."""

    return f"{_env(CHANNEL_GATEWAY_BASE_URL_ENV, '')}/healthz"


def start_timeout_seconds() -> float:
    """Bound on startup health waits for onebox-managed services."""
    configured = _env(START_TIMEOUT_ENV, DEFAULT_START_TIMEOUT_SECONDS)
    try:
        value = float(configured)
    except ValueError as exc:
        raise OneBoxError(
            f"{START_TIMEOUT_ENV}={configured!r} must be a number of seconds"
        ) from exc
    if value <= 0:
        raise OneBoxError(f"{START_TIMEOUT_ENV}={configured!r} must be positive")
    return value


class _Child:
    """One supervised process, with its output pumped and its tail remembered.

    The output pump exists for the sandbox server, whose logs would otherwise be
    indistinguishable from AstraBox's in ``docker logs``, and whose last lines
    are the only useful thing to say when it fails to come up. AstraBox itself is
    not pumped — see :func:`_spawn_astrabox`.
    """

    process: subprocess.Popen[str]

    def __init__(
        self,
        name: str,
        argv: Sequence[str],
        *,
        prefix_output: bool,
        stop_signal: int = signal.SIGTERM,
        own_session: bool = False,
    ) -> None:
        self.name = name
        self.argv = list(argv)
        self.stop_signal = stop_signal
        self._secret_values = _sensitive_environment_values()
        self._tail: deque[str] = deque(maxlen=_OUTPUT_TAIL_LINES)
        self._lock = threading.Lock()
        self._pump: threading.Thread | None = None
        # A foundation service runs in its own session so that a terminal's
        # Ctrl-C, which reaches the whole foreground process group, cannot stop
        # the database before the processes that write to it. Its stop signal
        # then comes only from the supervisor, in order.
        if prefix_output:
            # PYTHONUNBUFFERED is not optional here. A pipe makes the child's
            # stdout block-buffered, so its log lines sit in a buffer that a
            # SIGTERM or a crash never flushes — and this output is exactly what
            # the fail-loud messages quote. Without it, "here is the server's
            # output" comes back empty in the cases that need it most. Set on the
            # child rather than assumed from the image, so running the
            # orchestrator anywhere behaves the same.
            self.process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                self.argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                start_new_session=own_session,
            )
            self._pump = threading.Thread(
                target=self._pump_output, name=f"{name}-logs", daemon=True
            )
            self._pump.start()
        else:
            # No pipes: the child inherits this process's stdout and stderr, so
            # `text` describes streams that do not exist here. Passed anyway to
            # keep one declared type for `process` across both branches.
            self.process = subprocess.Popen(  # noqa: S603 - fixed argv
                self.argv, text=True, start_new_session=own_session
            )

    def _pump_output(self) -> None:
        """Copy the child's merged output to this process's own log, one prefixed line at a time."""
        stream = self.process.stdout
        if stream is None:  # pragma: no cover - PIPE was requested
            return
        try:
            for line in stream:
                text = _redact_child_output(
                    line.rstrip("\n"), secret_values=self._secret_values
                )
                with self._lock:
                    self._tail.append(text)
                print(f"[{self.name}] {text}", flush=True)
        except Exception:  # pragma: no cover - a closed pipe during shutdown
            logger.debug("stopped reading %s output", self.name, exc_info=True)
        finally:
            try:
                stream.close()
            except Exception:  # pragma: no cover
                pass

    def output_tail(self) -> str:
        """The last lines the child printed, for a failure message."""
        with self._lock:
            lines = list(self._tail)
        if not lines:
            return f"({self.name} produced no output)"
        return "\n".join(f"    {line}" for line in lines)

    def request_stop(self) -> None:
        """Send the child its stop signal once, without waiting for it."""
        if self.process.poll() is not None:
            return
        try:
            self.process.send_signal(self.stop_signal)
        except ProcessLookupError:  # pragma: no cover - exited between the two calls
            return

    def stop(self) -> None:
        """Signal the child, then escalate to SIGKILL if it overstays the grace."""
        if self.process.poll() is not None:
            return
        self.request_stop()
        self.await_exit()

    def await_exit(self) -> None:
        """Wait out the grace for a child already signalled, then kill it.

        The stop signal is not repeated: uvicorn reads a second SIGINT as an
        instruction to abandon its graceful shutdown.
        """
        if self.process.poll() is not None:
            return
        try:
            self.process.wait(timeout=_CHILD_SHUTDOWN_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning(
                "%s did not exit %.0fs after a signal; killing it",
                self.name,
                _CHILD_SHUTDOWN_GRACE_SECONDS,
            )
            self.process.kill()
            try:
                self.process.wait(timeout=_CHILD_SHUTDOWN_GRACE_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL ignored
                logger.error("%s survived SIGKILL", self.name)


def _spawn_sandbox_server() -> _Child:
    """Start the lifecycle server as a child of this process.

    ``sys.executable -m`` rather than a console script: the two rebinds in
    :mod:`astrabox.deploy.sandbox_server` must happen inside the process that
    serves, and running the module directly is what guarantees the interpreter is
    this one and the patches land before upstream's app is imported.
    """
    return _Child(
        "sandbox-server",
        (sys.executable, "-m", sandbox_server.__name__),
        prefix_output=True,
    )


def _spawn_litellm() -> _Child:
    """Start the embedded LiteLLM proxy from its own baked venv.

    A separate venv (`/opt/litellm`), not this interpreter: the proxy's large,
    weekly-moving dependency tree must never contend with the platform's pins.
    Binding 0.0.0.0 is correct here — the port is not published; it is reachable
    only in-container and from sandboxes over the container network, which is
    exactly who the gateway serves, and it authenticates every call
    (see ensure_litellm_master_key).
    """
    from astrabox.providers.model import EMBEDDED_LITELLM_PORT

    if not os.path.exists(LITELLM_BIN):
        raise OneBoxError(
            f"the embedded LiteLLM gateway is selected but {LITELLM_BIN} does not "
            "exist — this image was built without it, which is not a shape this "
            "platform ships. Rebuild containers/server/Dockerfile, or point "
            "ASTRABOX_LITELLM_BASE_URL at a gateway you run."
        )
    return _Child(
        "litellm",
        (
            LITELLM_BIN,
            "--config", LITELLM_CONFIG_PATH,
            "--host", "0.0.0.0",
            "--port", str(EMBEDDED_LITELLM_PORT),
        ),
        prefix_output=True,
    )


def _spawn_channel_gateway() -> _Child:
    """Start the private Node adapter runtime baked into the server image."""

    required = (
        CHANNEL_GATEWAY_BIN,
        CHANNEL_GATEWAY_SERVER_PATH,
        _env(CHANNEL_GATEWAY_MANIFEST_ENV, CHANNEL_GATEWAY_MANIFEST_PATH),
    )
    missing = [path for path in required if not os.path.exists(path)]
    if missing:
        raise OneBoxError(
            "the embedded channel gateway is selected but the image is missing: "
            + ", ".join(missing)
            + f". Rebuild containers/server/Dockerfile, or set {CHANNEL_GATEWAY_BASE_URL_ENV} "
            "to an external gateway."
        )
    return _Child(
        "channel-gateway",
        (CHANNEL_GATEWAY_BIN, CHANNEL_GATEWAY_SERVER_PATH),
        prefix_output=True,
    )


def _spawn_gateway_dns() -> _Child:
    """Start the private resolver baked beside the embedded gateway."""

    if not os.path.exists(COREDNS_BIN):
        raise OneBoxError(
            f"protected embedded model delivery needs {COREDNS_BIN}, but this "
            "image does not contain it. Rebuild containers/server/Dockerfile, "
            f"set {LITELLM_BASE_URL_ENV_NAME} to an external FQDN, or turn "
            f"{CREDENTIAL_VAULT_ENV} off explicitly."
        )
    if not os.path.exists(COREDNS_CONFIG_PATH):
        raise OneBoxError(
            f"protected embedded model delivery needs {COREDNS_CONFIG_PATH}, "
            "but this image does not contain it. Rebuild the server image."
        )
    return _Child(
        "gateway-dns",
        (
            COREDNS_BIN,
            "-conf",
            COREDNS_CONFIG_PATH,
            "-dns.port",
            str(GATEWAY_DNS_PORT),
        ),
        prefix_output=True,
    )


def _spawn_astrabox(argv: Sequence[str]) -> _Child:
    """Start AstraBox, letting its output through untouched.

    Deliberately not prefixed, unlike the sandbox server. AstraBox's log lines
    are already self-identifying, and ``ASTRABOX_LOG_FORMAT=json`` makes them
    structured records an aggregator parses — pasting a prefix in front of those
    would corrupt every one of them. It inherits this process's stdout and stderr
    instead, so what reaches ``docker logs`` is byte-identical to the unsupervised
    shape. The guest process is the one that gets marked.
    """
    return _Child("astrabox", argv, prefix_output=False)


def _health_probe(url: str) -> bool:
    """One loopback GET; True on a 2xx. Any failure means "not yet"."""
    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed http loopback URL
            url, timeout=_HEALTH_REQUEST_TIMEOUT_SECONDS
        ) as response:
            return 200 <= int(response.status) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_until_healthy(child: _Child, url: str, *, timeout: float) -> None:
    """Poll ``url`` until the server answers, it dies, or the bound expires.

    The died-during-startup case is checked first on every pass and reported
    with the child's own output, because it is both the likeliest failure here
    and the one where a generic timeout would throw away the answer: an unwritable
    metadata directory, a moved upstream constant and a missing Docker socket all
    exit within a second or two with the reason on stderr.
    """
    wait_until_ready(
        child, lambda: _health_probe(url), pending=f"did not answer {url}", timeout=timeout
    )


def wait_until_ready(
    child: _Child, probe: Callable[[], bool], *, pending: str, timeout: float
) -> None:
    """Poll ``probe`` until it passes, the child dies, or the bound expires.

    ``pending`` completes the timeout message after the child's name, for
    example ``did not answer http://127.0.0.1:8990/health``.
    """
    deadline = time.monotonic() + timeout
    while True:
        exit_code = child.process.poll()
        if exit_code is not None:
            raise OneBoxError(
                f"{child.name} exited with code {exit_code} before it "
                f"became healthy. Its output:\n{child.output_tail()}"
            )
        if probe():
            return
        if time.monotonic() >= deadline:
            child.stop()
            raise OneBoxError(
                f"{child.name} {pending} within {timeout:.0f}s "
                f"({START_TIMEOUT_ENV}). Its output:\n{child.output_tail()}"
            )
        time.sleep(_HEALTH_POLL_INTERVAL_SECONDS)


@dataclass(frozen=True)
class FoundationService:
    """A service every other child depends on: started first, stopped last.

    The all-in-one image runs its PostgreSQL and Valkey this way
    (:mod:`astrabox.deploy.all_in_one`). They start, and must pass ``ready``,
    before anything that connects to them. At shutdown they stop only after
    AstraBox and the other children have exited, one at a time in the order
    given, each with its own ``stop_signal``: PostgreSQL needs SIGINT, its fast
    shutdown, because SIGTERM is its smart shutdown and waits for clients. A
    foundation service that exits on its own ends the container, like any
    other child.
    """

    name: str
    argv: tuple[str, ...]
    ready: Callable[[], bool]
    stop_signal: int = signal.SIGTERM


def _start_foundation(service: FoundationService) -> _Child:
    return _Child(
        service.name,
        service.argv,
        prefix_output=True,
        stop_signal=service.stop_signal,
        own_session=True,
    )


def _stop_together(children: Sequence[_Child]) -> None:
    """Signal every child at once, then wait for each within the grace."""
    for child in children:
        child.request_stop()
    for child in children:
        child.await_exit()


def _stop_in_order(children: Sequence[_Child]) -> None:
    """Stop each child, waiting for it to exit before signalling the next."""
    for child in children:
        child.stop()


def _export_backend_wiring() -> str:
    """Point AstraBox's backend at the server this process is about to run.

    The base URL is composed by the launcher module, not spelled again here, so
    the address the server binds and the address AstraBox dials cannot drift.

    The sandbox reach mode is left at its direct default. The bundled server is
    a child of this process and shares its network namespace, so its relay
    would dial the same published sandbox address that AstraBox reaches itself
    (``sandbox_server.publish_host_ip``). The relay also drops ``Cookie`` and
    ``Authorization`` and rewrites ``Host`` on every request it forwards
    (``opensandbox_server/api/proxy.py``), which the DeepSeek Harness browser
    session and Hermes' loopback ``Host`` check cannot pass through.
    """
    base_url = sandbox_server.lifecycle_base_url()
    os.environ[BASE_URL_ENV] = base_url
    return base_url


def _forward_signals(app: _Child) -> None:
    """Pass SIGTERM/SIGINT to AstraBox alone, which starts an ordered shutdown.

    The handler only signals; it does not wait or exit. AstraBox's lifespan
    shutdown still reaches the lifecycle server, the model gateway and the
    database, so none of them is signalled here. The supervisor loop notices
    AstraBox leaving and stops the rest in dependency order, which keeps one
    exit path instead of one per signal.
    """

    def _handler(signal_number: int, _frame: FrameType | None) -> None:
        logger.info(
            "received %s; stopping astrabox first",
            signal.Signals(signal_number).name,
        )
        if app.process.poll() is None:
            try:
                app.process.send_signal(signal_number)
            except ProcessLookupError:  # pragma: no cover - already gone
                pass

    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, _handler)


def _supervise(
    app: _Child,
    sidecars: Sequence[_Child],
    foundation: Sequence[_Child] = (),
    *,
    watch: Callable[[], str | None] | None = None,
) -> int:
    """Wait for any child to exit, stop the rest in order, and report the reason.

    AstraBox's exit code is the container's whenever AstraBox is the one that
    went, because that is the process whose status an operator is reading. A
    sidecar (the sandbox server, the embedded gateway) or a foundation service
    (the all-in-one image's database) that dies under a healthy app is a failure
    of this container even if it exited cleanly, so it never reports success.
    ``watch`` is read every few seconds; a reason it returns ends the container
    the same way, with status 1, so the runtime's restart policy starts it
    again. Whatever ended it, the survivors stop in dependency order: AstraBox,
    then the sidecars together, then the foundation services one by one.
    """
    next_watch = time.monotonic() + _EDGE_WATCH_INTERVAL_SECONDS
    while True:
        if (code := app.process.poll()) is not None:
            logger.info("astrabox exited with code %s; stopping the sidecars", code)
            _stop_together(sidecars)
            _stop_in_order(foundation)
            return _exit_status(code)
        for child in (*sidecars, *foundation):
            if (code := child.process.poll()) is not None:
                logger.error(
                    "%s exited with code %s while astrabox was still running; "
                    "stopping everything. Its output:\n%s",
                    child.name,
                    code,
                    child.output_tail(),
                )
                app.stop()
                _stop_together([other for other in sidecars if other is not child])
                _stop_in_order([other for other in foundation if other is not child])
                return _exit_status(code) or 1
        if watch is not None and time.monotonic() >= next_watch:
            next_watch = time.monotonic() + _EDGE_WATCH_INTERVAL_SECONDS
            if (reason := watch()) is not None:
                logger.error("%s; stopping everything so the container restarts", reason)
                app.stop()
                _stop_together(sidecars)
                _stop_in_order(foundation)
                return 1
        time.sleep(_HEALTH_POLL_INTERVAL_SECONDS)


def _edge_address_watch(
    addresses: dict[str, str], owner: EdgeOwner | None = None
) -> Callable[[], str | None]:
    """Report a sandbox edge that is running at another bridge address.

    Every sandbox created from now on is given the addresses read at startup
    (its DNS upstream, its deny rules, its callback base), and the private
    gateway name resolves to the HTTP edge's. An edge that Docker restarts on
    its own — a crash under ``restart: unless-stopped`` — can come back with a
    different address on the built-in bridge, which assigns them dynamically,
    and every sandbox, new ones included, would then time out against the old
    one. Nothing in this process can re-point them, so the container restarts
    and reads the addresses again; boxes created with the old ones are then
    retired by the platform. An edge that is not running at the moment of a read
    tells nothing yet and is read again. ``owner`` finds the edges as
    :func:`ensure_sandbox_edge_network` does (``None``: this container's
    Compose project).
    """

    def _watch() -> str | None:
        for service, started_with in addresses.items():
            try:
                current = _detect_edge_ip(service, owner)
            except OneBoxError as exc:
                logger.warning("cannot read sandbox edge %r's address: %s", service, exc)
                continue
            if current != started_with:
                return (
                    f"sandbox edge {service!r} is now at {current} on Docker's "
                    f"built-in bridge; this server started with {started_with}"
                )
        return None

    return _watch


def _exit_status(returncode: int) -> int:
    """A child's status as this process's exit status.

    ``Popen`` reports a child killed by signal N as ``-N``; passed to
    ``sys.exit`` that becomes ``256 - N`` (241 for SIGTERM), a number no shell
    convention reads. A signal death is reported as ``128 + N``, as a shell and
    ``tini`` report it, so 137 still reads as SIGKILL.
    """

    return 128 - returncode if returncode < 0 else returncode


def main(
    argv: Sequence[str] | None = None,
    *,
    foundation: Sequence[FoundationService] = (),
    edge_owner: EdgeOwner | None = None,
) -> int:
    """Run AstraBox alone or supervise every bundled service its config selects.

    The all-in-one image adds two things. ``foundation``: services started,
    and ready, before every other child, and stopped after all of them.
    ``edge_owner``: the labels of the sandbox edges it created, in place of
    this container's Compose project; they are found, connected, turned into
    the addresses sandboxes are given and watched by the same code as
    Compose's.
    """
    logging.basicConfig(
        level=_env("ASTRABOX_LOG_LEVEL", "info").upper(),
        format="[onebox] %(levelname)s: %(message)s",
    )
    command = list(argv) if argv else list(ASTRABOX_COMMAND)

    # This must precede the no-supervision exec path as well as every child
    # spawn. An external sandbox/model provider does not make the platform's
    # own persistence optional.
    ensure_database_wiring()

    run_sandbox_server = needs_sandbox_server()
    run_litellm = needs_litellm_gateway()
    run_gateway_dns = needs_gateway_dns()
    run_channel_gateway = needs_channel_gateway()
    ensure_channel_gateway_wiring(embedded=run_channel_gateway)
    if (
        not run_litellm
        and _env(MODEL_PROVIDER_ENV, LITELLM_PROVIDER).lower() == LITELLM_PROVIDER
        and not _env(LITELLM_API_KEY_ENV_NAME, "")
    ):
        # An external LiteLLM does not have a child process to wait for, but it
        # uses the same scoped sandbox key as the embedded gateway. Provision
        # and spend that key before either the exec-only or supervised app path
        # can serve a conversation.
        ensure_shared_identity_key()
        ensure_sandbox_inference_key()
    # The edges whose bridge addresses this start derived, and so must stay put.
    watched_edges: dict[str, str] = {}
    if run_sandbox_server and _env(SANDBOX_EDGE_SERVICE_ENV, ""):
        ensure_sandbox_edge_network(edge_owner)
        derived = not _env(GATEWAY_DNS_ADDRESS_ENV, "")
        edge_address = ensure_sandbox_edge_wiring(edge_owner)
        if derived:
            watched_edges[_env(SANDBOX_EDGE_SERVICE_ENV, "")] = edge_address
        ensure_sandbox_bridge_isolation(edge_address)
    if run_gateway_dns and _env(SANDBOX_DNS_EDGE_SERVICE_ENV, ""):
        derived = not _env(EGRESS_DNS_UPSTREAM_ENV, "")
        dns_edge_address = ensure_sandbox_dns_edge_wiring(edge_owner)
        if derived:
            watched_edges[_env(SANDBOX_DNS_EDGE_SERVICE_ENV, "")] = dns_edge_address
    if (
        not foundation
        and not run_sandbox_server
        and not run_litellm
        and not run_gateway_dns
        and not run_channel_gateway
    ):
        # Configuration is complete and no bundled child needs supervision,
        # so AstraBox can replace this entry-point process.
        os.execvp(command[0], command)  # noqa: S606 - fixed command, no shell
        raise OneBoxError(  # pragma: no cover - execvp returns only on failure
            f"could not exec {command[0]!r}"
        )

    timeout = start_timeout_seconds()
    base: list[_Child] = []
    sidecars: list[_Child] = []
    # Everything after the first spawn runs under this guard. A sidecar is a
    # child process, not a resource Python cleans up: any escape from here
    # without stopping it — a failed health wait, an AstraBox command that does
    # not exist, a KeyboardInterrupt between spawns — would leave it running
    # with the container's main process gone, holding its port (and, for the
    # sandbox server, its Docker client).
    try:
        for service in foundation:
            logger.info("starting %s", service.name)
            child = _start_foundation(service)
            base.append(child)
            wait_until_ready(
                child, service.ready, pending="did not become ready", timeout=timeout
            )
            logger.info("%s is ready", service.name)
        if run_channel_gateway:
            logger.info(
                "no external channel gateway configured: starting the bundled adapter runtime"
            )
            channel_gateway = _spawn_channel_gateway()
            sidecars.append(channel_gateway)
            wait_until_healthy(
                channel_gateway,
                channel_gateway_health_url(),
                timeout=timeout,
            )
            logger.info("embedded channel gateway is healthy")
        if run_gateway_dns:
            address = ensure_gateway_dns_wiring()
            logger.info(
                "protected embedded model delivery is on: starting private "
                "gateway DNS at %s:%s",
                address,
                GATEWAY_DNS_PORT,
            )
            sidecars.append(_spawn_gateway_dns())
        litellm_child: _Child | None = None
        if run_litellm:
            ensure_litellm_provider_wiring()
            ensure_shared_identity_key()
            ensure_litellm_master_key()
            logger.info(
                "model endpoint provider is %s with no external gateway: starting "
                "the embedded LiteLLM proxy",
                LITELLM_PROVIDER,
            )
            litellm_child = _spawn_litellm()
            sidecars.append(litellm_child)
        if run_sandbox_server:
            logger.info(
                "sandbox backend is %s with no %s: starting a lifecycle server on %s",
                OPEN_SANDBOX_BACKEND,
                BASE_URL_ENV,
                sandbox_server.lifecycle_base_url(),
            )
            server = _spawn_sandbox_server()
            sidecars.append(server)
            wait_until_healthy(server, sandbox_server.health_url(), timeout=timeout)
            base_url = _export_backend_wiring()
            logger.info("sandbox server is healthy; astrabox will run against %s", base_url)
        if run_litellm:
            assert litellm_child is not None
            wait_until_healthy(litellm_child, litellm_health_url(), timeout=timeout)
            logger.info("embedded litellm gateway is healthy")
            ensure_sandbox_inference_key()
        app = _spawn_astrabox(command)
    except BaseException:
        _stop_together(sidecars)
        _stop_in_order(base)
        raise
    _forward_signals(app)
    return _supervise(
        app,
        sidecars,
        base,
        watch=_edge_address_watch(watched_edges, edge_owner) if watched_edges else None,
    )


__all__ = [
    "ASTRABOX_COMMAND",
    "ASTRABOX_DB_HOST_ENV",
    "ASTRABOX_DB_PASSWORD_FILE_ENV",
    "ASTRABOX_DB_PORT_ENV",
    "ASTRABOX_DB_URL_ENV",
    "BACKEND_ENV",
    "BASE_URL_ENV",
    "CHANNEL_GATEWAY_BASE_URL_ENV",
    "CHANNEL_GATEWAY_BIN",
    "CHANNEL_GATEWAY_HOST_ENV",
    "CHANNEL_GATEWAY_MANIFEST_ENV",
    "CHANNEL_GATEWAY_PORT_ENV",
    "CHANNEL_GATEWAY_SERVER_PATH",
    "CHANNEL_GATEWAY_TOKEN_ENV",
    "DEFAULT_START_TIMEOUT_SECONDS",
    "OPEN_SANDBOX_BACKEND",
    "LITELLM_PROVIDER",
    "COREDNS_BIN",
    "COREDNS_CONFIG_PATH",
    "CREDENTIAL_VAULT_ENV",
    "EGRESS_DENY_CIDRS_ENV",
    "EGRESS_DNS_UPSTREAM_ENV",
    "EGRESS_DNS_UPSTREAM_DEFAULT_ENV",
    "EdgeOwner",
    "FoundationService",
    "GATEWAY_DNS_ADDRESS_ENV",
    "GATEWAY_DNS_PORT",
    "ANTHROPIC_API_KEY_ENV",
    "ANTHROPIC_AUTH_TOKEN_ENV",
    "ANTHROPIC_BASE_URL_ENV",
    "ANTHROPIC_MODEL_ENV",
    "DEEPSEEK_API_KEY_ENV",
    "MODEL_PROVIDER_ENV",
    "OneBoxError",
    "SANDBOX_DNS_EDGE_SERVICE_ENV",
    "SANDBOX_EDGE_CALLBACK_PORT_ENV",
    "SANDBOX_EDGE_NETWORK_ENV",
    "SANDBOX_EDGE_SERVICE_ENV",
    "ensure_litellm_provider_wiring",
    "ensure_channel_gateway_wiring",
    "ensure_shared_identity_key",
    "export_default",
    "ensure_litellm_master_key",
    "ensure_sandbox_inference_key",
    "ensure_gateway_dns_wiring",
    "ensure_sandbox_bridge_isolation",
    "ensure_sandbox_dns_edge_wiring",
    "ensure_sandbox_edge_network",
    "ensure_database_wiring",
    "needs_gateway_dns",
    "needs_channel_gateway",
    "needs_litellm_gateway",
    "START_TIMEOUT_ENV",
    "main",
    "needs_sandbox_server",
    "start_timeout_seconds",
    "wait_until_healthy",
    "wait_until_ready",
]


if __name__ == "__main__":
    # The image's CMD is the AstraBox command, so it arrives here as arguments and
    # is forwarded verbatim — that is what keeps `docker run <image> serve --help`
    # and any other CMD override working. No arguments means the default.
    try:
        raise SystemExit(main(sys.argv[1:] or None))
    except OneBoxError as error:
        print(f"[onebox] FATAL: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error
