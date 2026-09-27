"""The all-in-one image's entry point: AstraBox with its database inside.

``python -m astrabox.deploy.all_in_one`` is the entry point of
``containers/all-in-one/Dockerfile``, which is the server image plus
PostgreSQL 17 and Valkey. It prepares what the Compose stack's ``postgres``
and ``redis`` services would otherwise provide, then hands over to
:func:`astrabox.deploy.onebox.main` with both as foundation services: started
and ready before every other child, stopped after all of them, each bound to
the container's loopback only. Design and decisions:
``docs/maintainers/all-in-one-image.md``.

Start order, each step before anything that depends on it:

1. **Refusal.** The image is one fixed topology. A setting that selects
   another database, pool store, sandbox backend or gateway, or a mode this
   shape does not verify, stops the container with the reason and the Compose
   path instead (:func:`refusals`). Nothing has been touched yet.
2. **Root phase.** The image starts as root only to give the ``astrabox``
   user the mounted Docker socket's group (so ``docker run`` needs no
   ``--group-add``), to take ownership of an empty ``/data``, and to create a
   fresh private socket directory for PostgreSQL. It then drops to
   ``astrabox`` for good; PostgreSQL refuses to run as root regardless.
3. **Docker engine and this container.** Rootless Docker and engines that are
   not Docker are refused: the egress sidecar's ``dns+nft`` enforcement is
   verified only on rootful Docker Engine and Docker Desktop. The container
   finds itself through the socket by its hostname, which must be its
   container ID, so ``--hostname`` and ``--network host`` are refused.
4. **Single writer.** An exclusive ``flock`` on ``/data/.astrabox.lock``.
   PostgreSQL's own ``postmaster.pid`` interlock cannot see a postmaster in
   another container's PID and IPC namespaces, so two containers on one
   volume would otherwise run two postmasters on one data directory.
5. **Database secrets**, generated once into ``/data/secrets`` (0600) by the
   rules :mod:`astrabox.deploy.secret_files` shares with the Compose script.
   A data directory whose passwords are missing is refused, never re-keyed.
6. **PostgreSQL data directory.** A first start initialises a staging
   directory and renames it into place only when complete, so a crash never
   leaves a half-initialised cluster behind. A later start checks that the
   directory's major version is this image's and removes a leftover
   ``postmaster.pid``, which under the lock can only belong to a dead server.
7. **Sandbox boundary** (:func:`prepare_sandbox_boundary`). The Compose
   stack's two edge containers, created and owned here through the Docker
   socket from the same image and configuration files, on Docker's built-in
   bridge; a private ``internal`` network that only they and this container
   join, where this container answers to the Compose service name
   ``server`` that both edge configurations forward to; and a network of
   this container's own, to which it moves from the default bridge so that no
   other container can reach its ports. onebox then connects the edges to the
   internal network and derives every address sandboxes are given, exactly as
   it does for Compose.
8. **The Claude Code sandbox image**, pulled before anything serves if the
   Docker host does not have it: a Session's sandbox create is one bounded
   request, and a multi-gigabyte pull inside it would fail the first Session.
9. **Hand-over** to ``onebox`` with PostgreSQL and Valkey as foundation
   services and the edges' owner, whose addresses onebox watches as it does
   Compose's, then a log line with the console's published address once it serves.

The key files AstraBox and LiteLLM already generate once into the state
directory (``vault.key``, ``auth-session.key``, ``litellm.key``) are created by
their own code on first use, into the same volume.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import ipaddress
import json
import logging
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from astrabox.common.logger.logger_factory import get_logger
from astrabox.deploy import onebox, secret_files
from astrabox.deploy.onebox import OneBoxError

logger = get_logger(__name__)

DATA_DIR = Path("/data")
LOCK_FILENAME = ".astrabox.lock"
SECRETS_DIRNAME = "secrets"
RUNTIME_USER = "astrabox"
DOCKER_SOCKET = Path("/var/run/docker.sock")
#: AstraBox's port inside this container: the console publication and the
#: sandbox edge's callback upstream both name it.
ASTRABOX_PORT = 8000

POSTGRES_MAJOR = "17"
POSTGRES_BIN_DIR = Path("/usr/lib/postgresql/17/bin")
POSTGRES_DATA_DIRNAME = "postgres"
POSTGRES_STAGING_DIRNAME = "postgres.initializing"
POSTGRES_SOCKET_DIR = Path("/run/astrabox/postgresql")
POSTGRES_HOST = "127.0.0.1"
POSTGRES_PORT = 5432
POSTGRES_SUPERUSER = "postgres"
INIT_DATABASES_SCRIPT = Path("/opt/astrabox/postgres/init-databases.sh")

POSTGRES_ADMIN_PASSWORD_FILE = "postgres_admin_password"
ASTRABOX_PASSWORD_FILE = "astrabox_password"
LITELLM_PASSWORD_FILE = "litellm_password"
DATABASE_SECRET_NAMES = (
    POSTGRES_ADMIN_PASSWORD_FILE,
    ASTRABOX_PASSWORD_FILE,
    LITELLM_PASSWORD_FILE,
)

VALKEY_BIN = "/usr/bin/valkey-server"
VALKEY_HOST = "127.0.0.1"
VALKEY_PORT = 6379

#: How long one readiness probe may take; a slow answer means "not yet".
_PROBE_TIMEOUT_SECONDS = 2.0
#: Read timeout of every Docker API call this module makes.
_DOCKER_TIMEOUT_SECONDS = 300
#: Bound for ``initdb`` and the init script on a first start.
_INIT_STEP_TIMEOUT_SECONDS = 300.0

COMPOSE_INSTEAD = "the server image with Docker Compose (https://www.astrabox.ai/docs/deploy)"
_DERIVED_FROM_EDGES = (
    "this image creates the sandbox edges and derives what sandboxes are given "
    "from them"
)

# ── what this shape refuses ────────────────────────────────────────────────
# One row per variable: (name, the values this image accepts, why, what to use
# instead). An empty value counts as unset, as it does for every other
# variable onebox hands to a child. ``()`` means any value is refused. Names
# and booleans compare case-insensitively; a path compares after
# normalisation. scripts/check_all_in_one.py keeps this table in step with
# containers/compose.yaml's server environment.
REFUSED: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("ASTRABOX_DB_BACKEND", ("postgresql",), "the database is the embedded PostgreSQL", COMPOSE_INSTEAD),
    ("ASTRABOX_DB_URL", (), "the database is the embedded PostgreSQL", COMPOSE_INSTEAD),
    ("ASTRABOX_DB_PASSWORD_FILE", (), "the database is the embedded PostgreSQL", COMPOSE_INSTEAD),
    ("ASTRABOX_DB_HOST", (), "the database is the embedded PostgreSQL", COMPOSE_INSTEAD),
    ("ASTRABOX_DB_PORT", (), "the database is the embedded PostgreSQL", COMPOSE_INSTEAD),
    ("DATABASE_URL", (), "the model gateway's database is the embedded PostgreSQL", COMPOSE_INSTEAD),
    ("LITELLM_DATABASE_PASSWORD_FILE", (), "the model gateway's database is the embedded PostgreSQL", COMPOSE_INSTEAD),
    ("LITELLM_DATABASE_HOST", (), "the model gateway's database is the embedded PostgreSQL", COMPOSE_INSTEAD),
    ("LITELLM_DATABASE_PORT", (), "the model gateway's database is the embedded PostgreSQL", COMPOSE_INSTEAD),
    ("ASTRABOX_STATE_DIR", ("/data",), "keys and data live in the /data volume, which is what a backup and an upgrade carry", COMPOSE_INSTEAD),
    ("ASTRABOX_AGENT_PREWARM_REDIS_URL", (), "the pool store is the embedded Valkey", COMPOSE_INSTEAD),
    ("ASTRABOX_SANDBOX_BACKEND", ("open_sandbox",), "sandboxes run on this Docker host through the bundled lifecycle server", "the server image with Docker Compose and the Kubernetes overlay"),
    ("ASTRABOX_SANDBOX_OPENAPI_BASE_URL", (), "sandboxes run on this Docker host through the bundled lifecycle server", "the server image with Docker Compose and the Kubernetes overlay"),
    ("ASTRABOX_SANDBOX_SERVER_RUNTIME", ("docker",), "sandboxes run on this Docker host through the bundled lifecycle server", "the server image with Docker Compose and the Kubernetes overlay"),
    ("ASTRABOX_LITELLM_BASE_URL", (), "external gateways are the team topology; this image runs the embedded model gateway", "the server image with Docker Compose and compose.team-gateway.yaml"),
    ("ASTRABOX_LITELLM_SERVER_BASE_URL", (), "external gateways are the team topology; this image runs the embedded model gateway", "the server image with Docker Compose and compose.team-gateway.yaml"),
    ("ASTRABOX_LITELLM_API_KEY", (), "external gateways are the team topology; this image runs the embedded model gateway", "the server image with Docker Compose and compose.team-gateway.yaml"),
    ("ASTRABOX_LITELLM_ADMIN_URL", (), "external gateways are the team topology; this image runs the embedded model gateway", "the server image with Docker Compose and compose.team-gateway.yaml"),
    ("ASTRABOX_MODEL_GATEWAY_REQUIRE_HTTPS", ("0", "false", "no", "off"), "the embedded model gateway is HTTP on the container network; an HTTPS gateway is the team topology", "the server image with Docker Compose and compose.team-gateway.yaml"),
    ("ASTRABOX_CHANNEL_GATEWAY_BASE_URL", (), "external gateways are the team topology; this image runs the embedded channel gateway", "the server image with Docker Compose and compose.team-gateway.yaml"),
    ("ASTRABOX_SANDBOX_WORKSPACE_VOLUME", (), "persistent workspaces are not verified in this shape; the mount helper needs Linux 6.9+, amd64, /dev/fuse and shared mount propagation", COMPOSE_INSTEAD),
    ("ASTRABOX_SANDBOX_CREDENTIAL_VAULT", ("1", "true", "yes", "on"), "this shape verifies only the protected default, where the model credential stays in the egress sidecar", COMPOSE_INSTEAD),
    # The sandbox boundary: the edges, their private network and every address
    # sandboxes are given, which prepare_sandbox_boundary and onebox derive.
    ("ASTRABOX_SANDBOX_EDGE_SERVICE", (), _DERIVED_FROM_EDGES, COMPOSE_INSTEAD),
    ("ASTRABOX_SANDBOX_DNS_EDGE_SERVICE", (), _DERIVED_FROM_EDGES, COMPOSE_INSTEAD),
    ("ASTRABOX_SANDBOX_EDGE_NETWORK", (), _DERIVED_FROM_EDGES, COMPOSE_INSTEAD),
    ("ASTRABOX_SANDBOX_EDGE_CALLBACK_PORT", (), _DERIVED_FROM_EDGES, COMPOSE_INSTEAD),
    ("ASTRABOX_SANDBOX_GATEWAY_IP", (), _DERIVED_FROM_EDGES, COMPOSE_INSTEAD),
    ("ASTRABOX_MCP_PROXY_BASE_URL", (), _DERIVED_FROM_EDGES, COMPOSE_INSTEAD),
    ("ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM", (), _DERIVED_FROM_EDGES, COMPOSE_INSTEAD),
    ("ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM_DEFAULT", (), _DERIVED_FROM_EDGES, COMPOSE_INSTEAD),
    ("ASTRABOX_SANDBOX_EGRESS_DENY_CIDRS", (), _DERIVED_FROM_EDGES, COMPOSE_INSTEAD),
    ("ASTRABOX_PUBLISH_HOST_IP", (), "sandbox ports are published on Docker's bridge gateway, which this image reads from Docker", COMPOSE_INSTEAD),
    ("ASTRABOX_SANDBOX_SERVER_NETWORK_MODE", ("bridge",), "OpenSandbox enforces sandbox network policy only on Docker's built-in bridge, where the edges are", COMPOSE_INSTEAD),
    ("ASTRABOX_PORT", (str(ASTRABOX_PORT),), f"the console publication and the sandbox edge reach AstraBox on port {ASTRABOX_PORT}", COMPOSE_INSTEAD),
    ("ASTRABOX_HOST", ("0.0.0.0",), "the console publication and the sandbox edge reach AstraBox on this container's network addresses", COMPOSE_INSTEAD),
)


def _accepted(value: str, accepted: tuple[str, ...]) -> bool:
    if any(item.startswith("/") for item in accepted):
        return os.path.normpath(value) in accepted
    return value.lower() in accepted


def refusals(environ: Mapping[str, str]) -> list[str]:
    """Every supplied setting this image refuses, one sentence each."""

    problems: list[str] = []
    for name, accepted, reason, instead in REFUSED:
        value = str(environ.get(name) or "").strip()
        if not value or _accepted(value, accepted):
            continue
        if accepted:
            allowed = " or ".join(repr(item) for item in accepted)
            problems.append(
                f"{name}={value!r} is not supported here ({reason}; this image accepts "
                f"only {allowed}). Use {instead} instead."
            )
        else:
            problems.append(
                f"{name} is set, but {reason}. Unset it, or use {instead} instead."
            )
    return problems


# ── root phase ─────────────────────────────────────────────────────────────


def claim_data_dir(data_dir: Path, *, uid: int, gid: int) -> None:
    """Give an empty foreign-owned ``data_dir`` to the runtime user; refuse a full one.

    A fresh named volume copies the image's ``/data``, which the runtime user
    already owns. A new bind-mounted host directory arrives owned by root and
    empty, and is taken over. A non-empty directory owned by someone else is
    somebody's data: recursively changing its owner could break whatever else
    uses it, so it is refused instead.
    """

    status = data_dir.stat()
    if status.st_uid == uid:
        return
    if any(data_dir.iterdir()):
        raise OneBoxError(
            f"{data_dir} is owned by uid {status.st_uid}, not the {RUNTIME_USER} "
            f"user (uid {uid}), and is not empty. This image does not change the "
            "owner of existing files. Mount a new volume, or give the directory "
            f"to uid {uid} yourself."
        )
    os.chown(data_dir, uid, gid)
    logger.info("took ownership of the empty data directory %s", data_dir)


def drop_privileges(
    *,
    data_dir: Path,
    docker_socket: Path,
    postgres_socket_dir: Path,
    user: str = RUNTIME_USER,
) -> None:
    """Run the root-only steps, then become ``user`` for the rest of the process."""

    if os.geteuid() != 0:
        raise OneBoxError(
            "the all-in-one image starts as root to give its runtime user the "
            "Docker socket's group, then drops to that user before any service "
            "starts. Do not pass --user."
        )
    account = pwd.getpwnam(user)
    try:
        socket_gid = os.stat(docker_socket).st_gid
    except OSError as exc:
        raise OneBoxError(
            f"cannot find the Docker socket at {docker_socket} ({exc}). Every "
            "sandbox is a container on this Docker host: run the image with "
            f"-v /var/run/docker.sock:{docker_socket}."
        ) from exc
    claim_data_dir(data_dir, uid=account.pw_uid, gid=account.pw_gid)
    # Container-private and recreated on every start, so a socket or lock file
    # a killed server left behind cannot make the next start refuse.
    shutil.rmtree(postgres_socket_dir, ignore_errors=True)
    postgres_socket_dir.mkdir(parents=True, mode=0o700)
    os.chown(postgres_socket_dir, account.pw_uid, account.pw_gid)

    os.setgroups(sorted({account.pw_gid, socket_gid}))
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)
    # The process environment every child inherits, as a login as `user` sets it.
    os.environ.update({"HOME": account.pw_dir, "USER": user, "LOGNAME": user})
    logger.info(
        "running as %s with the Docker socket's group %s", user, socket_gid
    )


def check_docker_engine(client: Any) -> None:
    """Refuse a Docker engine the egress boundary is not verified on."""

    version = client.version()
    components = [str(item.get("Name") or "") for item in version.get("Components") or []]
    platform = str((version.get("Platform") or {}).get("Name") or "unknown")
    if "Engine" not in components:
        raise OneBoxError(
            f"the container engine behind the Docker socket is {platform!r} "
            f"(components {components}), not Docker. The sandbox egress "
            "sidecar's dns+nft enforcement is verified only on rootful Docker "
            "Engine and Docker Desktop."
        )
    options = [str(item) for item in client.info().get("SecurityOptions") or []]
    if any(option.split(",", 1)[0] == "name=rootless" for option in options):
        raise OneBoxError(
            "the Docker daemon runs rootless. The sandbox egress sidecar's "
            "dns+nft enforcement is verified only on rootful Docker Engine and "
            "Docker Desktop."
        )


def open_docker(docker_socket: Path) -> Any:
    """A client on the mounted socket, once the engine behind it is one this image supports.

    Its read timeout is long enough for a slow daemon and for the gaps
    between the progress messages of a large image pull.
    """

    try:
        import docker

        client = docker.DockerClient(
            base_url=f"unix://{docker_socket}", timeout=_DOCKER_TIMEOUT_SECONDS
        )
        check_docker_engine(client)
        return client
    except OneBoxError:
        raise
    except Exception as exc:
        raise OneBoxError(
            f"cannot use the Docker socket at {docker_socket}: {exc}. On Docker "
            "Desktop, Enhanced Container Isolation blocks mounting it."
        ) from exc


def own_container(client: Any, *, hostname: str | None = None) -> Any:
    """This container, found through the socket by its hostname.

    Docker makes a container's hostname the first twelve characters of its ID,
    and onebox finds the server container the same way. ``--hostname`` or
    ``--network host`` gives it another name, which may be nobody's or another
    container's, so anything else is refused rather than trusted.
    """

    import docker.errors

    name = hostname if hostname is not None else socket.gethostname()
    try:
        container = client.containers.get(name)
    except docker.errors.NotFound:
        container = None
    if container is None or not str(container.id).startswith(name):
        raise OneBoxError(
            f"this container's hostname {name!r} is not its container ID. The "
            "image finds its own container through the Docker socket by that ID "
            "to connect itself to the sandbox edges' private network. Do not "
            "pass --hostname or --network host."
        )
    return container


# ── single writer ──────────────────────────────────────────────────────────


def acquire_data_lock(data_dir: Path) -> int:
    """Hold an exclusive lock on the data volume for this process's lifetime.

    The descriptor is returned so it stays open; it is not inherited by child
    processes, so the lock is released exactly when this supervisor exits.
    """

    path = data_dir / LOCK_FILENAME
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise OneBoxError(
            f"another AstraBox container is using the data volume at {data_dir} "
            f"(it holds {path}). Two containers on one volume would run two "
            "PostgreSQL servers on one data directory. Stop the other container "
            "first."
        ) from None
    return descriptor


# ── database secrets ───────────────────────────────────────────────────────


def ensure_database_secrets(data_dir: Path) -> Path:
    """Create the database passwords once; refuse a cluster that lost them."""

    directory = data_dir / SECRETS_DIRNAME
    if (data_dir / POSTGRES_DATA_DIRNAME).exists():
        missing = [
            name for name in DATABASE_SECRET_NAMES if not os.path.lexists(directory / name)
        ]
        if missing:
            raise OneBoxError(
                f"{data_dir / POSTGRES_DATA_DIRNAME} exists but its password "
                f"files are missing from {directory}: {', '.join(missing)}. "
                "New passwords would not match the database roles. Restore the "
                "files from the backup of this volume."
            )
    try:
        secret_files.ensure_secret_files(directory, DATABASE_SECRET_NAMES, mode=0o600)
    except RuntimeError as exc:
        raise OneBoxError(str(exc)) from exc
    return directory


# ── PostgreSQL ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PostgresLayout:
    """Where one data volume keeps its cluster, and where this image keeps its binaries."""

    data_dir: Path
    secrets_dir: Path
    bin_dir: Path = POSTGRES_BIN_DIR
    socket_dir: Path = POSTGRES_SOCKET_DIR
    init_script: Path = INIT_DATABASES_SCRIPT

    @property
    def cluster(self) -> Path:
        return self.data_dir / POSTGRES_DATA_DIRNAME

    @property
    def staging(self) -> Path:
        return self.data_dir / POSTGRES_STAGING_DIRNAME

    def binary(self, name: str) -> str:
        return str(self.bin_dir / name)


def check_postgres_major(cluster: Path, *, expected: str = POSTGRES_MAJOR) -> None:
    """Refuse a data directory written by another PostgreSQL major version."""

    version_file = cluster / "PG_VERSION"
    try:
        found = version_file.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise OneBoxError(
            f"{cluster} is not a PostgreSQL data directory ({version_file}: {exc}). "
            "Restore this volume from its backup."
        ) from exc
    if found != expected:
        raise OneBoxError(
            f"the database in {cluster} was written by PostgreSQL {found}; this "
            f"image runs PostgreSQL {expected} and does not upgrade a data "
            "directory in place. Start the image version that wrote this volume, "
            "dump the astrabox and litellm databases with pg_dump, and restore "
            "them into a new volume under this image."
        )


def remove_stale_postmaster_pid(cluster: Path) -> None:
    """Delete a ``postmaster.pid`` that, under the volume lock, only a dead server left.

    PostgreSQL refuses to start when the file names a live process, and in a
    restarted container the recorded PID is often reused by an unrelated
    process, which is the known "lock file already exists" restart failure.
    """

    pid_file = cluster / "postmaster.pid"
    if pid_file.exists():
        pid_file.unlink()
        logger.info("removed %s left by a server that did not shut down cleanly", pid_file)


def _run_step(argv: Sequence[str], *, what: str, env: Mapping[str, str] | None = None) -> None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(argv),
            capture_output=True,
            text=True,
            env=dict(env) if env is not None else None,
            timeout=_INIT_STEP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OneBoxError(f"{what} failed: {exc}") from exc
    if completed.returncode != 0:
        output = onebox._redact_child_output(
            (completed.stdout + completed.stderr).strip()
        )
        raise OneBoxError(f"{what} exited with code {completed.returncode}:\n{output}")


def _postgres_ready(bin_dir: Path, *, host: str, port: int) -> bool:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                str(bin_dir / "pg_isready"),
                "--host", host,
                "--port", str(port),
                "--quiet",
                "--timeout", str(int(_PROBE_TIMEOUT_SECONDS)),
            ],
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECONDS + 3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def initialize_postgres(layout: PostgresLayout) -> None:
    """Create the cluster, its roles and databases in staging, then rename it into place."""

    staging = layout.staging
    if staging.exists():
        logger.warning("discarding %s left by an interrupted first start", staging)
        shutil.rmtree(staging)
    staging.mkdir(mode=0o700)
    logger.info("initialising PostgreSQL %s in %s", POSTGRES_MAJOR, staging)
    # The builtin locale provider (PostgreSQL 17) makes text ordering part of
    # PostgreSQL itself, so a later base image's glibc or ICU cannot reorder
    # an existing index.
    _run_step(
        [
            layout.binary("initdb"),
            "--pgdata", str(staging),
            "--username", POSTGRES_SUPERUSER,
            "--pwfile", str(layout.secrets_dir / POSTGRES_ADMIN_PASSWORD_FILE),
            "--auth", "scram-sha-256",
            "--encoding", "UTF8",
            "--locale", "C.UTF-8",
            "--locale-provider", "builtin",
            "--builtin-locale", "C.UTF-8",
        ],
        what="initdb",
    )
    with tempfile.TemporaryDirectory(prefix="astrabox-postgres-init-") as private_socket:
        temporary = onebox._Child(
            "postgres-init",
            (
                layout.binary("postgres"),
                "-D", str(staging),
                "-c", "listen_addresses=",
                "-c", f"unix_socket_directories={private_socket}",
            ),
            prefix_output=True,
            stop_signal=signal.SIGINT,
        )
        try:
            onebox.wait_until_ready(
                temporary,
                lambda: _postgres_ready(layout.bin_dir, host=private_socket, port=POSTGRES_PORT),
                pending="did not accept connections on its private socket",
                timeout=onebox.start_timeout_seconds(),
            )
            admin_password = (
                (layout.secrets_dir / POSTGRES_ADMIN_PASSWORD_FILE)
                .read_text(encoding="ascii")
                .strip()
            )
            _run_step(
                ["sh", str(layout.init_script)],
                what=str(layout.init_script),
                env={
                    "PATH": f"{layout.bin_dir}:/usr/local/bin:/usr/bin:/bin",
                    "PGHOST": private_socket,
                    "PGPORT": str(POSTGRES_PORT),
                    "PGPASSWORD": admin_password,
                    "POSTGRES_USER": POSTGRES_SUPERUSER,
                    "POSTGRES_DB": "postgres",
                    "ASTRABOX_POSTGRES_PASSWORD_FILE": str(
                        layout.secrets_dir / ASTRABOX_PASSWORD_FILE
                    ),
                    "LITELLM_POSTGRES_PASSWORD_FILE": str(
                        layout.secrets_dir / LITELLM_PASSWORD_FILE
                    ),
                },
            )
        finally:
            temporary.stop()
        if temporary.process.returncode != 0:
            raise OneBoxError(
                f"the initialising PostgreSQL server exited with code "
                f"{temporary.process.returncode} instead of shutting down "
                f"cleanly. Its output:\n{temporary.output_tail()}"
            )
    os.rename(staging, layout.cluster)
    directory = os.open(layout.data_dir, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    logger.info("PostgreSQL cluster ready at %s", layout.cluster)


def prepare_postgres(layout: PostgresLayout) -> None:
    """Initialise the cluster on a first start; otherwise check it can be started."""

    if not layout.cluster.exists():
        initialize_postgres(layout)
        return
    check_postgres_major(layout.cluster)
    remove_stale_postmaster_pid(layout.cluster)


def postgres_service(layout: PostgresLayout) -> onebox.FoundationService:
    return onebox.FoundationService(
        name="postgres",
        argv=(
            layout.binary("postgres"),
            "-D", str(layout.cluster),
            "-c", f"listen_addresses={POSTGRES_HOST}",
            "-c", f"port={POSTGRES_PORT}",
            "-c", f"unix_socket_directories={layout.socket_dir}",
        ),
        ready=lambda: _postgres_ready(layout.bin_dir, host=POSTGRES_HOST, port=POSTGRES_PORT),
        # SIGTERM is PostgreSQL's smart shutdown, which waits for every client
        # to disconnect; SIGINT is the fast shutdown the official image uses.
        stop_signal=signal.SIGINT,
    )


# ── Valkey ─────────────────────────────────────────────────────────────────


def _valkey_ready(*, host: str = VALKEY_HOST, port: int = VALKEY_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_SECONDS) as conn:
            conn.sendall(b"PING\r\n")
            return conn.recv(64).startswith(b"+PONG")
    except OSError:
        return False


def valkey_service() -> onebox.FoundationService:
    """OpenSandbox's pool state for Agent prewarming, in memory on loopback.

    Nothing is persisted, as in Compose, which keeps no Redis volume: a restart
    discards idle warm capacity only.
    """

    return onebox.FoundationService(
        name="valkey",
        argv=(
            VALKEY_BIN,
            "--bind", VALKEY_HOST,
            "--port", str(VALKEY_PORT),
            "--protected-mode", "yes",
            "--save", "",
            "--appendonly", "no",
        ),
        ready=_valkey_ready,
    )


# ── the sandbox boundary ───────────────────────────────────────────────────
#
# OpenSandbox's egress policy names hosts, not ports, so a sandbox may reach
# exactly one platform address: the HTTP edge, an nginx container on Docker's
# built-in bridge that forwards only the model route and three callback
# prefixes. The DNS edge is its resolver. Both reach this container over a
# private `internal` network no sandbox joins, where this container answers to
# the name `server`, Compose's service name, which both edge configurations
# name. The edges are Compose's: the same image, the same two files, the same
# environment (scripts/check_all_in_one.py keeps them equal). Because onebox
# finds them by labels, this module labels them with the installation and
# gives them Compose's service names as roles; everything from there on
# (the network connection, the addresses sandboxes are given, the bridge deny
# list) is onebox's Compose path.

INSTALL_ID_FILENAME = "install-id"
INSTALL_LABEL = "astrabox.all-in-one.install"
ROLE_LABEL = "astrabox.all-in-one.role"
SPEC_LABEL = "astrabox.all-in-one.spec"
#: The edges' roles and the network's key: Compose's service and network names.
EDGE_ROLE = "sandbox-edge"
DNS_EDGE_ROLE = "sandbox-dns-edge"
EDGE_NETWORK_ROLE = "sandbox-edges"
#: This container's own network: Compose's key for the server's network.
PLATFORM_NETWORK_ROLE = "platform"
#: The name both edge configurations forward to: Compose's service name.
SERVER_ALIAS = "server"
#: The image of both edges; containers/compose.yaml must name the same one.
EDGE_IMAGE = "nginx:1.27-alpine"
#: Where the image bakes containers/sandbox-edge/.
EDGE_CONFIG_DIR = Path("/opt/astrabox/sandbox-edge")
#: Compose's restart policy for its two edges: Docker restarts a crashed edge,
#: and onebox's watch restarts this container if one came back elsewhere.
EDGE_RESTART_POLICY = {"Name": "unless-stopped"}
#: How often a long image pull reports its progress.
_PULL_REPORT_SECONDS = 15.0
_PULL_HINT = (
    "Check this host's network and the registry named by ASTRABOX_IMAGE_PREFIX, "
    "if set; for a registry that needs a login, run `docker pull {reference}` "
    "on the Docker host and start this container again"
)


@dataclass(frozen=True)
class EdgeFile:
    """A configuration file copied into an edge before it starts.

    ``directory`` exists in the edge image; ``name`` is relative to it, and
    any directory in ``name`` is created by the copy.
    """

    source: Path
    directory: str
    name: str

    @property
    def target(self) -> str:
        return str(PurePosixPath(self.directory) / self.name)


@dataclass(frozen=True)
class EdgeSpec:
    """One edge container: its role and what Compose gives the same service."""

    role: str
    files: tuple[EdgeFile, ...]
    environment: tuple[tuple[str, str], ...] = ()


#: Compose's `sandbox-edge` and `sandbox-dns-edge` services, file for file.
EDGE_SPECS: tuple[EdgeSpec, ...] = (
    EdgeSpec(
        role=EDGE_ROLE,
        files=(
            EdgeFile(
                EDGE_CONFIG_DIR / "default.conf.template",
                "/etc/nginx",
                "templates/default.conf.template",
            ),
        ),
        environment=(
            ("NGINX_ENVSUBST_FILTER", "^ASTRABOX_EDGE_"),
            ("ASTRABOX_EDGE_UPSTREAM_HOST", SERVER_ALIAS),
        ),
    ),
    EdgeSpec(
        role=DNS_EDGE_ROLE,
        files=(
            EdgeFile(EDGE_CONFIG_DIR / "dns-edge.nginx.conf", "/etc/nginx", "nginx.conf"),
        ),
    ),
)


def ensure_install_id(data_dir: Path) -> str:
    """The installation's identity, created once in the data volume.

    It names and labels the edges and their network, so two installations on
    one Docker daemon never share them, and a restarted or upgraded container
    finds its own. Not a secret; created and validated by the same
    generate-once rules as the secrets.
    """

    path = data_dir / INSTALL_ID_FILENAME
    try:
        value = secret_files.read_existing_secret(path, mode=0o644)
        if value is None:
            secret_files.write_new_secret(path, mode=0o644)
            value = secret_files.read_existing_secret(path, mode=0o644)
    except RuntimeError as exc:
        raise OneBoxError(str(exc)) from exc
    assert value is not None
    return value


def resource_name(install_id: str, role: str) -> str:
    """The Docker name of one of this installation's edges or of their network."""

    return f"astrabox-{install_id[:12]}-{role}"


def edge_owner(install_id: str) -> onebox.EdgeOwner:
    """How onebox finds this installation's edges and network."""

    return onebox.EdgeOwner(
        labels=(f"{INSTALL_LABEL}={install_id}",),
        service_key=ROLE_LABEL,
        network_key=ROLE_LABEL,
        description=f"all-in-one installation {install_id[:12]}",
    )


def edge_spec_digest(spec: EdgeSpec, network_name: str) -> str:
    """What decides whether an existing edge is kept or replaced.

    An edge with the same image, files, environment, network and restart
    policy is kept, so its bridge address, which live sandboxes' network
    policies name, survives a restart or an upgrade that did not change it.
    """

    document = {
        "image": EDGE_IMAGE,
        "role": spec.role,
        "network": network_name,
        "restart_policy": EDGE_RESTART_POLICY,
        "environment": [list(item) for item in spec.environment],
        "files": [
            [item.target, hashlib.sha256(item.source.read_bytes()).hexdigest()]
            for item in spec.files
        ],
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _labels(resource: Any) -> dict[str, str]:
    attrs = resource.attrs or {}
    labels = attrs.get("Labels")
    if labels is None:
        labels = (attrs.get("Config") or {}).get("Labels")
    return dict(labels or {})


def _refuse_foreign(kind: str, name: str, labels: Mapping[str, str], install_id: str, role: str) -> None:
    if labels.get(INSTALL_LABEL) == install_id and labels.get(ROLE_LABEL) == role:
        return
    owner = labels.get(INSTALL_LABEL)
    raise OneBoxError(
        f"a Docker {kind} named {name} exists but "
        + (f"belongs to all-in-one installation {owner[:12]}" if owner else "was not created by AstraBox")
        + f", not to this one ({install_id[:12]}, /data/{INSTALL_ID_FILENAME}). This "
        "image never changes or removes a Docker resource it does not own. Remove "
        "or rename it, or give this container the data volume it belongs to."
    )


def _ensure_network(client: Any, install_id: str, role: str, *, internal: bool, purpose: str) -> Any:
    """One of this installation's networks, created once, refused if foreign or changed."""

    import docker.errors

    name = resource_name(install_id, role)
    try:
        network = client.networks.get(name)
    except docker.errors.NotFound:
        network = None
    if network is not None:
        _refuse_foreign("network", name, _labels(network), install_id, role)
        if bool(network.attrs.get("Internal")) != internal:
            raise OneBoxError(
                f"the Docker network {name} is {'not ' if internal else ''}internal, "
                f"but it must {'' if internal else 'not '}be: it is {purpose}. Remove "
                "it; the next start creates it again."
            )
        return network
    network = client.networks.create(
        name,
        driver="bridge",
        internal=internal,
        labels={INSTALL_LABEL: install_id, ROLE_LABEL: role},
    )
    logger.info("created %s, %s", name, purpose)
    return network


def ensure_edge_network(client: Any, install_id: str) -> Any:
    """The private network the edges reach this container on; created once.

    Internal: it must give the edges no route off this host.
    """

    return _ensure_network(
        client,
        install_id,
        EDGE_NETWORK_ROLE,
        internal=True,
        purpose="the sandbox edges' private network to this container",
    )


def ensure_platform_network(client: Any, install_id: str) -> Any:
    """This container's own network, which no other container joins; created once.

    Not internal: the model gateway, image pulls and every outbound call leave
    through it, and it carries the console's host publication.
    """

    return _ensure_network(
        client,
        install_id,
        PLATFORM_NETWORK_ROLE,
        internal=False,
        purpose="this container's own network, in place of Docker's default bridge",
    )


def leave_default_bridge(client: Any, network: Any, own: Any) -> None:
    """Move this container from Docker's default bridge to its own network.

    ``docker run`` attaches the container to the default bridge, which every
    other container started without ``--network`` shares. There the no-login
    API, the model gateway and the resolver would be open to all of them;
    Compose keeps its server off that bridge for the same reason. The
    container joins its own network first, so the console's host publication
    always has a network to follow, then leaves the bridge: Docker moves the
    port mapping to the new network. The move persists across restarts of the
    same container, so it happens on the first start of a new one.
    """

    own.reload()
    attached = (own.attrs.get("NetworkSettings") or {}).get("Networks") or {}
    if network.name not in attached:
        network.connect(own)
        logger.info("connected this container to its own network %s", network.name)
    if "bridge" in attached:
        client.networks.get("bridge").disconnect(own)
        logger.info("left Docker's default bridge, which other containers share")


def attach_server(network: Any, own: Any, *, install_id: str) -> None:
    """Join the private network as ``server``, the name both edge configurations use.

    A restarted container keeps the connection and its alias, so this changes
    something only on the first start of a new container. Only the two edges
    may be running on the network besides this container. Another container
    there is an AstraBox started on a copy of this volume (a restored backup
    carries the installation identity), and two containers answering as
    ``server`` would split the edges' traffic between them.
    """

    network.reload()
    edges = {resource_name(install_id, spec.role) for spec in EDGE_SPECS}
    others = sorted(
        str(endpoint.get("Name") or container_id[:12])
        for container_id, endpoint in (network.attrs.get("Containers") or {}).items()
        if container_id != own.id and str(endpoint.get("Name") or "") not in edges
    )
    if others:
        raise OneBoxError(
            f"another container is running on this installation's network "
            f"{network.name}: {', '.join(others)}. It is an AstraBox started on a "
            "copy of this data volume, such as a restored backup; stop it first, "
            "because two copies of one installation cannot run at once."
        )
    own.reload()
    attached = (own.attrs.get("NetworkSettings") or {}).get("Networks") or {}
    if network.name in attached:
        return
    network.connect(own, aliases=[SERVER_ALIAS])
    logger.info("connected this container to %s as %r", network.name, SERVER_ALIAS)


def ensure_image(client: Any, reference: str, *, purpose: str) -> None:
    """Pull ``reference`` unless the Docker host has it, logging progress.

    The pull runs in the daemon, without the host user's registry logins, so
    an image behind a login must be pulled on the host first.
    """

    import docker.errors

    try:
        client.images.get(reference)
        return
    except docker.errors.ImageNotFound:
        pass
    logger.info("pulling %s, %s; this Docker host does not have it", reference, purpose)
    started = time.monotonic()
    reported = started
    sizes: dict[str, tuple[int, int]] = {}
    finished: set[str] = set()
    try:
        for event in client.api.pull(reference, stream=True, decode=True):
            if event.get("error"):
                raise OneBoxError(f"cannot pull {reference}: {event['error']}")
            layer = str(event.get("id") or "")
            status = str(event.get("status") or "")
            detail = event.get("progressDetail") or {}
            if status == "Downloading" and detail.get("total"):
                sizes[layer] = (int(detail.get("current") or 0), int(detail["total"]))
            elif status in {"Download complete", "Pull complete", "Already exists"}:
                finished.add(layer)
                if layer in sizes:
                    sizes[layer] = (sizes[layer][1], sizes[layer][1])
            now = time.monotonic()
            if now - reported >= _PULL_REPORT_SECONDS:
                reported = now
                done = sum(current for current, _ in sizes.values())
                total = sum(size for _, size in sizes.values())
                logger.info(
                    "pulling %s: %d layers finished, %.0f of %.0f MB downloaded so far",
                    reference,
                    len(finished),
                    done / 1e6,
                    total / 1e6,
                )
        client.images.get(reference)
    except OneBoxError as exc:
        raise OneBoxError(f"{exc}. {_PULL_HINT.format(reference=reference)}") from exc
    except Exception as exc:
        raise OneBoxError(
            f"cannot pull {reference}: {exc}. {_PULL_HINT.format(reference=reference)}"
        ) from exc
    logger.info("pulled %s in %.0fs", reference, time.monotonic() - started)



def _config_archive(files: Sequence[EdgeFile]) -> bytes:
    """A tar of ``files`` for the Docker archive API, rooted at their directory."""

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        directories: set[str] = set()
        for item in files:
            parents = list(PurePosixPath(item.name).parents)[:-1]
            for parent in reversed(parents):
                if str(parent) in directories:
                    continue
                directories.add(str(parent))
                entry = tarfile.TarInfo(str(parent))
                entry.type = tarfile.DIRTYPE
                entry.mode = 0o755
                archive.addfile(entry)
            content = item.source.read_bytes()
            entry = tarfile.TarInfo(item.name)
            entry.size = len(content)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(content))
    return buffer.getvalue()


def converge_edge(client: Any, *, install_id: str, spec: EdgeSpec, network_name: str) -> Any:
    """Keep this installation's edge if it matches ``spec``, otherwise (re)create it.

    A matching edge is kept, and started if it exited. An edge of this
    installation whose image, files, environment or network changed is
    replaced, and so is one that never started: a start interrupted between
    creating it and copying its configuration in leaves a container that
    would serve nginx's default page. A container with the edge's name that
    another owner labelled is refused, never touched.
    """

    import docker.errors

    name = resource_name(install_id, spec.role)
    digest = edge_spec_digest(spec, network_name)
    try:
        existing = client.containers.get(name)
    except docker.errors.NotFound:
        existing = None
    if existing is not None:
        labels = _labels(existing)
        _refuse_foreign("container", name, labels, install_id, spec.role)
        if labels.get(SPEC_LABEL) != digest:
            reason = "its image or configuration changed"
        elif existing.status == "running":
            logger.info("kept the running sandbox edge %s", name)
            return existing
        elif existing.status == "exited":
            existing.start()
            logger.info("started the stopped sandbox edge %s", name)
            return existing
        else:
            reason = f"it is {existing.status}"
        logger.info("replacing the sandbox edge %s: %s", name, reason)
        existing.remove(force=True)
    ensure_image(client, EDGE_IMAGE, purpose="the sandbox edges' proxy")
    container = client.containers.create(
        EDGE_IMAGE,
        name=name,
        labels={INSTALL_LABEL: install_id, ROLE_LABEL: spec.role, SPEC_LABEL: digest},
        environment=dict(spec.environment),
        network_mode="bridge",
        restart_policy=EDGE_RESTART_POLICY,
    )
    try:
        by_directory: dict[str, list[EdgeFile]] = {}
        for item in spec.files:
            by_directory.setdefault(item.directory, []).append(item)
        for directory, files in by_directory.items():
            if not container.put_archive(directory, _config_archive(files)):
                raise OneBoxError(f"Docker refused the configuration of {name} in {directory}")
        container.start()
    except BaseException:
        try:
            container.remove(force=True)
        except Exception:
            logger.warning("could not remove the half-created sandbox edge %s", name, exc_info=True)
        raise
    logger.info("created the sandbox edge %s", name)
    return container


def bridge_gateway(client: Any) -> str:
    """The IPv4 gateway of Docker's built-in bridge, where sandbox ports are published."""

    configs = (client.networks.get("bridge").attrs.get("IPAM") or {}).get("Config") or []
    for config in configs:
        gateway = str((config or {}).get("Gateway") or "").strip()
        try:
            if isinstance(ipaddress.ip_address(gateway), ipaddress.IPv4Address):
                return gateway
        except ValueError:
            continue
    raise OneBoxError(
        "Docker's built-in bridge reports no IPv4 gateway. OpenSandbox attaches "
        "every sandbox to that network and publishes its ports on the gateway."
    )


def prepare_sandbox_boundary(client: Any, *, own: Any, install_id: str) -> onebox.EdgeOwner:
    """Converge the edges and the two networks, and export what onebox reads.

    This container ends up only on its own network and on the edges' private
    network, as Compose's server is only on user-defined networks. onebox
    connects the edges to the private network, derives the callback base, the
    gateway address, the DNS upstream and the bridge deny list from them, and
    watches their addresses while serving, through the same code as Compose;
    the returned :class:`astrabox.deploy.onebox.EdgeOwner` is how it finds
    them.
    """

    try:
        network = ensure_edge_network(client, install_id)
        attach_server(network, own, install_id=install_id)
        leave_default_bridge(client, ensure_platform_network(client, install_id), own)
        for spec in EDGE_SPECS:
            converge_edge(client, install_id=install_id, spec=spec, network_name=network.name)
        gateway = bridge_gateway(client)
    except OneBoxError:
        raise
    except Exception as exc:
        raise OneBoxError(
            f"cannot prepare the sandbox edges through the Docker socket: {exc}. "
            f"Their containers and network are named astrabox-{install_id[:12]}-*; "
            "removing them lets the next start create them again."
        ) from exc
    # Literal names, as export_wiring writes them, so the environment
    # registry's scanner resolves every write; tests pin them to onebox's and
    # the launcher's constants.
    os.environ["ASTRABOX_SANDBOX_EDGE_SERVICE"] = EDGE_ROLE
    os.environ["ASTRABOX_SANDBOX_DNS_EDGE_SERVICE"] = DNS_EDGE_ROLE
    os.environ["ASTRABOX_SANDBOX_EDGE_NETWORK"] = EDGE_NETWORK_ROLE
    os.environ["ASTRABOX_PUBLISH_HOST_IP"] = gateway
    return edge_owner(install_id)


# ── serving ────────────────────────────────────────────────────────────────


def console_url(own: Any, *, port: int = ASTRABOX_PORT) -> str | None:
    """The console's address on the Docker host, from this container's publication.

    ``None`` when the port is not published. An all-interfaces publication is
    reported at the host's loopback, which reaches it too.
    """

    own.reload()
    bindings = ((own.attrs.get("NetworkSettings") or {}).get("Ports") or {}).get(
        f"{port}/tcp"
    ) or []
    for binding in bindings:
        host = str(binding.get("HostIp") or "")
        host_port = str(binding.get("HostPort") or "")
        if not host_port or ":" in host:
            continue
        if host in {"", "0.0.0.0"}:
            logger.warning(
                "the console is published on every interface of the Docker host "
                "(-p %s:%s). This shape has no login: publish it on loopback "
                "(-p 127.0.0.1:%s:%s), or use the Compose deployment with team login.",
                host_port,
                port,
                host_port,
                port,
            )
            host = "127.0.0.1"
        return f"http://{host}:{host_port}"
    return None


def announce_when_ready(url: str | None, *, port: int = ASTRABOX_PORT) -> threading.Thread:
    """Log where the console is once AstraBox serves it, from a daemon thread."""

    def _wait() -> None:
        base = f"http://127.0.0.1:{port}"
        while not (onebox._health_probe(f"{base}/healthz") and onebox._health_probe(f"{base}/")):
            time.sleep(1.0)
        if url is None:
            logger.warning(
                "AstraBox is ready, but its port %s is not published to the Docker "
                "host. Run the container with -p 127.0.0.1:8088:%s.",
                port,
                port,
            )
        else:
            logger.info("AstraBox is ready at %s", url)

    thread = threading.Thread(target=_wait, name="ready-announcement", daemon=True)
    thread.start()
    return thread


def export_wiring(layout: PostgresLayout) -> None:
    """Hand AstraBox, LiteLLM and the pool code the embedded services' addresses."""

    os.environ["ASTRABOX_DB_PASSWORD_FILE"] = str(layout.secrets_dir / ASTRABOX_PASSWORD_FILE)
    os.environ["ASTRABOX_DB_HOST"] = POSTGRES_HOST
    os.environ["ASTRABOX_DB_PORT"] = str(POSTGRES_PORT)
    os.environ["LITELLM_DATABASE_PASSWORD_FILE"] = str(
        layout.secrets_dir / LITELLM_PASSWORD_FILE
    )
    os.environ["LITELLM_DATABASE_HOST"] = POSTGRES_HOST
    os.environ["LITELLM_DATABASE_PORT"] = str(POSTGRES_PORT)
    os.environ["ASTRABOX_AGENT_PREWARM_REDIS_URL"] = f"redis://{VALKEY_HOST}:{VALKEY_PORT}/0"


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=str(os.environ.get("ASTRABOX_LOG_LEVEL") or "info").strip().upper(),
        format="[onebox] %(levelname)s: %(message)s",
    )
    problems = refusals(os.environ)
    if problems:
        raise OneBoxError(
            "this all-in-one image does not support:\n"
            + "\n".join(f"  - {problem}" for problem in problems)
        )
    drop_privileges(
        data_dir=DATA_DIR,
        docker_socket=DOCKER_SOCKET,
        postgres_socket_dir=POSTGRES_SOCKET_DIR,
    )
    client = open_docker(DOCKER_SOCKET)
    own = own_container(client)
    # Held until this process exits; see acquire_data_lock.
    acquire_data_lock(DATA_DIR)
    install_id = ensure_install_id(DATA_DIR)
    layout = PostgresLayout(data_dir=DATA_DIR, secrets_dir=ensure_database_secrets(DATA_DIR))
    prepare_postgres(layout)
    export_wiring(layout)
    owner = prepare_sandbox_boundary(client, own=own, install_id=install_id)
    from astrabox.providers.sandbox_image import resolve_agent_image

    ensure_image(
        client,
        resolve_agent_image(),
        purpose="the sandbox image of the seeded Claude Code Agent; the console serves once it is here",
    )
    announce_when_ready(console_url(own))
    client.close()
    return onebox.main(
        argv,
        foundation=(postgres_service(layout), valkey_service()),
        edge_owner=owner,
    )


__all__ = [
    "DATABASE_SECRET_NAMES",
    "DNS_EDGE_ROLE",
    "EDGE_IMAGE",
    "EDGE_RESTART_POLICY",
    "EDGE_NETWORK_ROLE",
    "EDGE_ROLE",
    "EDGE_SPECS",
    "EdgeFile",
    "EdgeSpec",
    "INSTALL_LABEL",
    "POSTGRES_MAJOR",
    "PLATFORM_NETWORK_ROLE",
    "PostgresLayout",
    "REFUSED",
    "ROLE_LABEL",
    "SERVER_ALIAS",
    "SPEC_LABEL",
    "acquire_data_lock",
    "announce_when_ready",
    "attach_server",
    "bridge_gateway",
    "check_docker_engine",
    "check_postgres_major",
    "claim_data_dir",
    "console_url",
    "converge_edge",
    "drop_privileges",
    "edge_owner",
    "edge_spec_digest",
    "ensure_database_secrets",
    "ensure_edge_network",
    "ensure_image",
    "ensure_install_id",
    "ensure_platform_network",
    "export_wiring",
    "initialize_postgres",
    "leave_default_bridge",
    "main",
    "open_docker",
    "own_container",
    "postgres_service",
    "prepare_postgres",
    "prepare_sandbox_boundary",
    "refusals",
    "remove_stale_postmaster_pid",
    "resource_name",
    "valkey_service",
]

if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:] or None))
    except OneBoxError as error:
        print(f"[onebox] FATAL: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error
