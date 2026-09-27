"""The all-in-one entry point's refusals and the state it guards in ``/data``.

What is worth nailing here is the logic a real container run cannot vary
cheaply: which supplied settings the fixed topology refuses (every one at once,
before anything touches the volume), that database passwords are created once
and never replaced, that a data directory from another PostgreSQL major is
refused, that a second process cannot take the volume, and which sandbox edge
this image keeps, replaces or refuses to touch. Starting the real PostgreSQL
and Valkey, the root phase, the shutdown order and the sandbox boundary in a
container are proven on a Docker host, not here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from astrabox.deploy import all_in_one
from astrabox.deploy.onebox import OneBoxError

#: What the server image bakes with ENV; the all-in-one inherits these.
_IMAGE_DEFAULTS = {
    "ASTRABOX_HOST": "0.0.0.0",
    "ASTRABOX_PORT": "8000",
    "ASTRABOX_SANDBOX_BACKEND": "open_sandbox",
    "ASTRABOX_STATE_DIR": "/data",
    "ASTRABOX_DB_BACKEND": "postgresql",
}


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_the_image_defaults_and_ordinary_settings_are_accepted() -> None:
    environ = {
        **_IMAGE_DEFAULTS,
        "ANTHROPIC_API_KEY": "sk-ant-example",
        "ANTHROPIC_MODEL": "claude-sonnet",
        "LANGFUSE_PUBLIC_KEY": "pk",
        "ASTRABOX_SANDBOX_SERVER_PORT_RANGE": "21000-22000",
        "ASTRABOX_SANDBOX_CREDENTIAL_VAULT": "TRUE",
    }
    assert all_in_one.refusals(environ) == []


def test_an_external_database_is_refused_with_the_compose_path() -> None:
    problems = all_in_one.refusals(
        {**_IMAGE_DEFAULTS, "ASTRABOX_DB_URL": "postgresql+asyncpg://a:b@db/astrabox"}
    )
    assert len(problems) == 1
    assert problems[0].startswith("ASTRABOX_DB_URL is set")
    assert "Docker Compose" in problems[0]
    assert "a:b@db" not in problems[0], "a refusal must not echo a credential-bearing value"


def test_every_refused_setting_is_reported_at_once() -> None:
    problems = all_in_one.refusals(
        {
            **_IMAGE_DEFAULTS,
            "ASTRABOX_AGENT_PREWARM_REDIS_URL": "redis://redis:6379/0",
            "ASTRABOX_SANDBOX_CREDENTIAL_VAULT": "false",
            "ASTRABOX_SANDBOX_SERVER_RUNTIME": "kubernetes",
        }
    )
    named = sorted(problem.split(" ", 1)[0].split("=", 1)[0] for problem in problems)
    assert named == [
        "ASTRABOX_AGENT_PREWARM_REDIS_URL",
        "ASTRABOX_SANDBOX_CREDENTIAL_VAULT",
        "ASTRABOX_SANDBOX_SERVER_RUNTIME",
    ]


def test_an_empty_value_counts_as_unset() -> None:
    # `-e NAME=` and Compose's `${NAME:-}` both render an empty string; onebox
    # treats that as "not configured" everywhere, and so does the refusal.
    assert all_in_one.refusals({**_IMAGE_DEFAULTS, "ASTRABOX_DB_URL": "  "}) == []


@pytest.mark.parametrize(
    ("value", "refused"),
    [("/data", False), ("/data/", False), ("/srv/astrabox", True), ("/DATA", True)],
)
def test_the_state_directory_must_be_the_data_volume(value: str, refused: bool) -> None:
    # Keys written anywhere else would not be in the volume a backup or an
    # upgrade carries; a path is compared as a path, not case-insensitively.
    problems = all_in_one.refusals({**_IMAGE_DEFAULTS, "ASTRABOX_STATE_DIR": value})
    assert bool(problems) is refused


def test_a_refused_setting_stops_the_container_before_the_volume_is_touched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_DB_URL", "postgresql+asyncpg://a:b@db/astrabox")
    monkeypatch.setattr(
        all_in_one, "drop_privileges", lambda **_: pytest.fail("touched /data")
    )
    with pytest.raises(OneBoxError, match="ASTRABOX_DB_URL"):
        all_in_one.main()


# ---------------------------------------------------------------------------
# database secrets
# ---------------------------------------------------------------------------


def test_database_passwords_are_created_once_private_and_never_replaced(
    tmp_path: Path,
) -> None:
    secrets_dir = all_in_one.ensure_database_secrets(tmp_path)

    first = {}
    for name in all_in_one.DATABASE_SECRET_NAMES:
        path = secrets_dir / name
        first[name] = path.read_text(encoding="ascii")
        assert len(first[name].strip()) == 64
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700
    assert len(set(first.values())) == len(first), "each role has its own password"

    all_in_one.ensure_database_secrets(tmp_path)
    assert {
        name: (secrets_dir / name).read_text(encoding="ascii") for name in first
    } == first, "a restart must not rotate the passwords behind PostgreSQL's back"


def test_a_malformed_or_linked_password_file_is_refused_not_replaced(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(mode=0o700)
    malformed = secrets_dir / all_in_one.ASTRABOX_PASSWORD_FILE
    malformed.write_text("not-a-generated-value\n", encoding="ascii")
    with pytest.raises(OneBoxError, match="invalid format"):
        all_in_one.ensure_database_secrets(tmp_path)
    assert malformed.read_text(encoding="ascii") == "not-a-generated-value\n"

    malformed.unlink()
    target = tmp_path / "elsewhere"
    target.write_text("a" * 64 + "\n", encoding="ascii")
    malformed.symlink_to(target)
    with pytest.raises(OneBoxError, match="symlink"):
        all_in_one.ensure_database_secrets(tmp_path)


def test_a_cluster_whose_passwords_are_missing_is_refused_not_rekeyed(
    tmp_path: Path,
) -> None:
    all_in_one.ensure_database_secrets(tmp_path)
    (tmp_path / all_in_one.POSTGRES_DATA_DIRNAME).mkdir()
    missing = tmp_path / "secrets" / all_in_one.LITELLM_PASSWORD_FILE
    missing.unlink()

    with pytest.raises(OneBoxError, match=all_in_one.LITELLM_PASSWORD_FILE):
        all_in_one.ensure_database_secrets(tmp_path)
    assert not missing.exists(), "a new password would not match the existing role"


# ---------------------------------------------------------------------------
# the PostgreSQL major gate and a leftover lock file
# ---------------------------------------------------------------------------


def _cluster(tmp_path: Path, version: str | None) -> Path:
    cluster = tmp_path / all_in_one.POSTGRES_DATA_DIRNAME
    cluster.mkdir()
    if version is not None:
        (cluster / "PG_VERSION").write_text(f"{version}\n", encoding="ascii")
    return cluster


def test_a_data_directory_from_another_major_is_refused(tmp_path: Path) -> None:
    cluster = _cluster(tmp_path, "16")
    (cluster / "postmaster.pid").write_text("42\n", encoding="ascii")
    layout = all_in_one.PostgresLayout(data_dir=tmp_path, secrets_dir=tmp_path / "secrets")

    with pytest.raises(OneBoxError) as error:
        all_in_one.prepare_postgres(layout)
    message = str(error.value)
    assert "PostgreSQL 16" in message and f"PostgreSQL {all_in_one.POSTGRES_MAJOR}" in message
    assert "pg_dump" in message
    assert (cluster / "postmaster.pid").exists(), "a refused cluster is left as found"


def test_a_directory_that_is_not_a_cluster_is_refused(tmp_path: Path) -> None:
    _cluster(tmp_path, None)
    layout = all_in_one.PostgresLayout(data_dir=tmp_path, secrets_dir=tmp_path / "secrets")
    with pytest.raises(OneBoxError, match="not a PostgreSQL data directory"):
        all_in_one.prepare_postgres(layout)


def test_a_matching_cluster_loses_only_its_stale_postmaster_pid(tmp_path: Path) -> None:
    # Under the volume lock no live server can own the file; left in place, a
    # reused PID makes PostgreSQL refuse with "lock file already exists".
    cluster = _cluster(tmp_path, all_in_one.POSTGRES_MAJOR)
    (cluster / "postmaster.pid").write_text("42\n", encoding="ascii")
    layout = all_in_one.PostgresLayout(data_dir=tmp_path, secrets_dir=tmp_path / "secrets")

    all_in_one.prepare_postgres(layout)

    assert not (cluster / "postmaster.pid").exists()
    assert (cluster / "PG_VERSION").exists()


# ---------------------------------------------------------------------------
# single writer
# ---------------------------------------------------------------------------


_TRY_LOCK = (
    "import sys\n"
    "from pathlib import Path\n"
    "from astrabox.deploy import all_in_one\n"
    "from astrabox.deploy.onebox import OneBoxError\n"
    "try:\n"
    "    all_in_one.acquire_data_lock(Path(sys.argv[1]))\n"
    "except OneBoxError as error:\n"
    "    print(error)\n"
    "    raise SystemExit(1)\n"
)


def _try_lock(data_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _TRY_LOCK, str(data_dir)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_second_process_cannot_take_a_volume_that_is_in_use(tmp_path: Path) -> None:
    descriptor = all_in_one.acquire_data_lock(tmp_path)
    try:
        second = _try_lock(tmp_path)
        assert second.returncode == 1
        assert "another AstraBox container is using the data volume" in second.stdout
    finally:
        os.close(descriptor)

    # The lock belongs to the holder's lifetime, not to the file: once released,
    # the next start proceeds without anyone deleting anything.
    assert _try_lock(tmp_path).returncode == 0


# ---------------------------------------------------------------------------
# root phase and the Docker engine
# ---------------------------------------------------------------------------


def test_a_non_root_start_is_refused_with_the_reason(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("the refusal is for a start that is not root")
    with pytest.raises(OneBoxError, match="--user"):
        all_in_one.drop_privileges(
            data_dir=tmp_path,
            docker_socket=tmp_path / "docker.sock",
            postgres_socket_dir=tmp_path / "run",
        )


def test_a_foreign_owned_data_directory_with_files_is_refused(tmp_path: Path) -> None:
    (tmp_path / "vault.key").write_text("someone's key\n", encoding="ascii")
    with pytest.raises(OneBoxError, match="does not change the owner"):
        all_in_one.claim_data_dir(tmp_path, uid=os.getuid() + 1, gid=os.getgid())
    # The runtime user's own directory is left exactly as it is.
    all_in_one.claim_data_dir(tmp_path, uid=os.getuid(), gid=os.getgid())


class _FakeEngine:
    def __init__(self, components: list[str], security_options: list[str]) -> None:
        self._components = components
        self._security_options = security_options

    def version(self) -> dict[str, Any]:
        return {
            "Platform": {"Name": "test engine"},
            "Components": [{"Name": name} for name in self._components],
        }

    def info(self) -> dict[str, Any]:
        return {"SecurityOptions": self._security_options}


def test_rootful_docker_is_accepted() -> None:
    all_in_one.check_docker_engine(
        _FakeEngine(["Engine", "containerd", "runc"], ["name=seccomp,profile=builtin"])
    )


def test_rootless_docker_is_refused() -> None:
    with pytest.raises(OneBoxError, match="rootless"):
        all_in_one.check_docker_engine(
            _FakeEngine(["Engine"], ["name=seccomp,profile=builtin", "name=rootless"])
        )


def test_an_engine_that_is_not_docker_is_refused() -> None:
    with pytest.raises(OneBoxError, match="not Docker"):
        all_in_one.check_docker_engine(_FakeEngine(["Podman Engine"], []))


# ---------------------------------------------------------------------------
# hand-over
# ---------------------------------------------------------------------------


def test_the_embedded_services_are_what_astrabox_and_litellm_are_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from astrabox.deploy import onebox

    for name in (
        "ASTRABOX_DB_URL",
        "DATABASE_URL",
        "ASTRABOX_DB_PASSWORD_FILE",
        "ASTRABOX_DB_HOST",
        "ASTRABOX_DB_PORT",
        "LITELLM_DATABASE_PASSWORD_FILE",
        "LITELLM_DATABASE_HOST",
        "LITELLM_DATABASE_PORT",
        "ASTRABOX_AGENT_PREWARM_REDIS_URL",
    ):
        # Recorded first, so the raw os.environ writes below are undone.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    secrets_dir = all_in_one.ensure_database_secrets(tmp_path)
    layout = all_in_one.PostgresLayout(data_dir=tmp_path, secrets_dir=secrets_dir)

    all_in_one.export_wiring(layout)
    onebox.ensure_database_wiring()

    astrabox_password = (secrets_dir / all_in_one.ASTRABOX_PASSWORD_FILE).read_text().strip()
    litellm_password = (secrets_dir / all_in_one.LITELLM_PASSWORD_FILE).read_text().strip()
    assert os.environ["ASTRABOX_DB_URL"] == (
        f"postgresql+asyncpg://astrabox:{astrabox_password}@127.0.0.1:5432/astrabox"
    )
    assert os.environ["DATABASE_URL"] == (
        f"postgresql://litellm:{litellm_password}@127.0.0.1:5432/litellm"
    )
    assert os.environ["ASTRABOX_AGENT_PREWARM_REDIS_URL"] == "redis://127.0.0.1:6379/0"


# ---------------------------------------------------------------------------
# refusals of the sandbox boundary
# ---------------------------------------------------------------------------


_BOUNDARY_VARIABLES = (
    "ASTRABOX_SANDBOX_EDGE_SERVICE",
    "ASTRABOX_SANDBOX_DNS_EDGE_SERVICE",
    "ASTRABOX_SANDBOX_EDGE_NETWORK",
    "ASTRABOX_SANDBOX_EDGE_CALLBACK_PORT",
    "ASTRABOX_SANDBOX_GATEWAY_IP",
    "ASTRABOX_MCP_PROXY_BASE_URL",
    "ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM",
    "ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM_DEFAULT",
    "ASTRABOX_SANDBOX_EGRESS_DENY_CIDRS",
    "ASTRABOX_PUBLISH_HOST_IP",
)


@pytest.mark.parametrize("name", _BOUNDARY_VARIABLES)
def test_a_supplied_sandbox_boundary_setting_is_refused(name: str) -> None:
    # Each would replace an address or list derived from the edges this image
    # owns; a callback base or deny list from elsewhere reopens the boundary.
    problems = all_in_one.refusals({**_IMAGE_DEFAULTS, name: "10.0.0.5"})
    assert len(problems) == 1
    assert problems[0].startswith(f"{name} is set")
    assert "10.0.0.5" not in problems[0]


@pytest.mark.parametrize(
    ("name", "accepted", "refused"),
    [
        ("ASTRABOX_SANDBOX_SERVER_NETWORK_MODE", "bridge", "astrabox-net"),
        # The edge's callback upstream and the console publication name :8000.
        ("ASTRABOX_PORT", "8000", "9000"),
        ("ASTRABOX_HOST", "0.0.0.0", "127.0.0.1"),
    ],
)
def test_a_setting_the_boundary_depends_on_accepts_only_its_value(
    name: str, accepted: str, refused: str
) -> None:
    assert all_in_one.refusals({**_IMAGE_DEFAULTS, name: accepted}) == []
    problems = all_in_one.refusals({**_IMAGE_DEFAULTS, name: refused})
    assert len(problems) == 1 and problems[0].startswith(f"{name}={refused!r}")


# ---------------------------------------------------------------------------
# the sandbox boundary: a Docker daemon in memory
# ---------------------------------------------------------------------------


class _NotFound(Exception):
    pass


class _ImageNotFound(_NotFound):
    pass


class _Container:
    def __init__(
        self,
        daemon: "_Daemon",
        *,
        name: str,
        labels: dict[str, str],
        status: str = "created",
        address: str = "172.17.0.23",
        image: str = "",
        environment: dict[str, str] | None = None,
        network_mode: str = "bridge",
        restart_policy: dict[str, str] | None = None,
    ) -> None:
        self.daemon = daemon
        self.name = name
        self.id = hashlib.sha256(name.encode("utf-8")).hexdigest()
        self.image = image
        self.environment = environment or {}
        self.network_mode = network_mode
        self.restart_policy = restart_policy
        self.archives: list[tuple[str, bytes]] = []
        self.address = address
        self.log_text = b""
        self.attrs: dict[str, Any] = {
            "Config": {"Labels": dict(labels)},
            "State": {"Status": status},
            "NetworkSettings": {
                "Networks": {"bridge": {"IPAddress": address}} if status == "running" else {},
                "Ports": {},
            },
        }

    @property
    def status(self) -> str:
        return str(self.attrs["State"]["Status"])

    def start(self) -> None:
        self.attrs["State"]["Status"] = "running"
        self.attrs["NetworkSettings"]["Networks"].setdefault(
            "bridge", {"IPAddress": self.address}
        )
        self.daemon.log.append(f"start {self.name}")

    def remove(self, *, force: bool = False) -> None:
        assert force
        self.daemon.containers_by_name.pop(self.name, None)
        self.daemon.log.append(f"remove {self.name}")

    def put_archive(self, path: str, data: bytes) -> bool:
        self.archives.append((path, data))
        return self.daemon.accept_archives

    def reload(self) -> None:
        pass

    def logs(self, *, tail: int) -> bytes:
        return self.log_text


class _Network:
    def __init__(self, name: str, labels: dict[str, str], *, internal: bool) -> None:
        self.name = name
        self.id = f"network-{name}"
        self.attrs: dict[str, Any] = {
            "Name": name,
            "Labels": dict(labels),
            "Internal": internal,
            "Containers": {},
        }
        self.connected: list[tuple[str, list[str]]] = []

    def connect(self, container: _Container, *, aliases: list[str] | None = None) -> None:
        self.connected.append((container.name, list(aliases or [])))
        container.attrs["NetworkSettings"]["Networks"][self.name] = {"Aliases": aliases}
        self.attrs["Containers"][container.id] = {"Name": container.name}
        container.daemon.log.append(f"connect {container.name} to {self.name}")

    def disconnect(self, container: _Container) -> None:
        container.attrs["NetworkSettings"]["Networks"].pop(self.name)
        self.attrs["Containers"].pop(container.id, None)
        container.daemon.log.append(f"disconnect {container.name} from {self.name}")

    def reload(self) -> None:
        pass


class _Daemon:
    """The slice of docker-py the entry point uses, with a log of what changed."""

    def __init__(self, *, images: tuple[str, ...] = (all_in_one.EDGE_IMAGE,)) -> None:
        self.log: list[str] = []
        self.containers_by_name: dict[str, _Container] = {}
        self.networks_by_name: dict[str, _Network] = {}
        self.images_present = set(images)
        self.pull_events: list[dict[str, Any]] = []
        self.accept_archives = True
        bridge = _Network("bridge", {}, internal=False)
        bridge.attrs["IPAM"] = {"Config": [{"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.1"}]}
        self.networks_by_name["bridge"] = bridge
        self.containers = SimpleNamespace(get=self._get_container, create=self._create_container)
        self.networks = SimpleNamespace(get=self._get_network, create=self._create_network)
        self.images = SimpleNamespace(get=self._get_image)
        self.api = SimpleNamespace(pull=self._pull)

    def add_container(self, name: str, labels: dict[str, str], **kwargs: Any) -> _Container:
        container = _Container(self, name=name, labels=labels, **kwargs)
        self.containers_by_name[name] = container
        return container

    def _get_container(self, key: str) -> _Container:
        for container in self.containers_by_name.values():
            if container.name == key or container.id.startswith(key):
                return container
        raise _NotFound(key)

    def _create_container(self, image: str, **kwargs: Any) -> _Container:
        self.log.append(f"create {kwargs['name']}")
        return self.add_container(
            kwargs["name"],
            kwargs["labels"],
            image=image,
            environment=kwargs["environment"],
            network_mode=kwargs["network_mode"],
            restart_policy=kwargs["restart_policy"],
        )

    def _get_network(self, name: str) -> _Network:
        if name not in self.networks_by_name:
            raise _NotFound(name)
        return self.networks_by_name[name]

    def _create_network(self, name: str, **kwargs: Any) -> _Network:
        assert kwargs["driver"] == "bridge"
        self.log.append(f"create network {name}")
        network = _Network(name, kwargs["labels"], internal=kwargs["internal"])
        self.networks_by_name[name] = network
        return network

    def _get_image(self, reference: str) -> Any:
        if reference not in self.images_present:
            raise _ImageNotFound(reference)
        return SimpleNamespace(id=reference)

    def _pull(self, reference: str, *, stream: bool, decode: bool) -> Iterator[dict[str, Any]]:
        assert stream and decode
        self.log.append(f"pull {reference}")
        yield from self.pull_events
        self.images_present.add(reference)


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> _Daemon:
    """An in-memory daemon, and docker-py's error types for the code under test."""

    errors = SimpleNamespace(NotFound=_NotFound, ImageNotFound=_ImageNotFound)
    monkeypatch.setitem(sys.modules, "docker", SimpleNamespace(errors=errors))
    monkeypatch.setitem(sys.modules, "docker.errors", errors)
    return _Daemon()


_INSTALL_ID = "a1b2c3d4e5f6" + "0" * 52
_REPO_EDGE_DIR = Path(__file__).resolve().parent.parent / "containers" / "sandbox-edge"


def _spec(role: str) -> all_in_one.EdgeSpec:
    """The image's spec for ``role``, reading the repository's copy of its files."""

    spec = next(item for item in all_in_one.EDGE_SPECS if item.role == role)
    return dataclasses.replace(
        spec,
        files=tuple(
            dataclasses.replace(item, source=_REPO_EDGE_DIR / item.source.name)
            for item in spec.files
        ),
    )


def _ours(role: str, **extra: str) -> dict[str, str]:
    return {all_in_one.INSTALL_LABEL: _INSTALL_ID, all_in_one.ROLE_LABEL: role, **extra}


_NETWORK = all_in_one.resource_name(_INSTALL_ID, all_in_one.EDGE_NETWORK_ROLE)
_EDGE = all_in_one.resource_name(_INSTALL_ID, all_in_one.EDGE_ROLE)


def test_a_missing_edge_is_created_the_way_compose_runs_it(daemon: _Daemon) -> None:
    spec = _spec(all_in_one.EDGE_ROLE)

    container = all_in_one.converge_edge(
        daemon, install_id=_INSTALL_ID, spec=spec, network_name=_NETWORK
    )

    assert daemon.log == [f"create {_EDGE}", f"start {_EDGE}"]
    assert container.image == "nginx:1.27-alpine"
    # On the built-in bridge, where sandboxes may reach it; restarted by Docker
    # as Compose's edges are, with onebox watching for a move.
    assert container.network_mode == "bridge"
    assert container.restart_policy == {"Name": "unless-stopped"}
    assert container.environment == {
        "NGINX_ENVSUBST_FILTER": "^ASTRABOX_EDGE_",
        "ASTRABOX_EDGE_UPSTREAM_HOST": "server",
    }
    assert container.attrs["Config"]["Labels"] == _ours(
        all_in_one.EDGE_ROLE,
        **{all_in_one.SPEC_LABEL: all_in_one.edge_spec_digest(spec, _NETWORK)},
    )
    # The configuration arrives before the start, as the file Compose mounts.
    [(directory, data)] = container.archives
    assert directory == "/etc/nginx"
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = {member.name: member for member in archive.getmembers()}
        assert members["templates"].isdir()
        content = archive.extractfile(members["templates/default.conf.template"])
        assert content is not None
        assert content.read() == (_REPO_EDGE_DIR / "default.conf.template").read_bytes()


def test_a_matching_edge_is_kept_so_live_sandboxes_keep_its_address(daemon: _Daemon) -> None:
    spec = _spec(all_in_one.EDGE_ROLE)
    digest = all_in_one.edge_spec_digest(spec, _NETWORK)
    running = daemon.add_container(
        _EDGE, _ours(all_in_one.EDGE_ROLE, **{all_in_one.SPEC_LABEL: digest}), status="running"
    )

    assert all_in_one.converge_edge(
        daemon, install_id=_INSTALL_ID, spec=spec, network_name=_NETWORK
    ) is running
    assert daemon.log == []

    running.attrs["State"]["Status"] = "exited"
    assert all_in_one.converge_edge(
        daemon, install_id=_INSTALL_ID, spec=spec, network_name=_NETWORK
    ) is running
    assert daemon.log == [f"start {_EDGE}"]


def test_an_edge_whose_configuration_changed_is_replaced(daemon: _Daemon) -> None:
    spec = _spec(all_in_one.EDGE_ROLE)
    daemon.add_container(
        _EDGE, _ours(all_in_one.EDGE_ROLE, **{all_in_one.SPEC_LABEL: "a-previous-release"}),
        status="running",
    )

    replacement = all_in_one.converge_edge(
        daemon, install_id=_INSTALL_ID, spec=spec, network_name=_NETWORK
    )

    assert daemon.log == [f"remove {_EDGE}", f"create {_EDGE}", f"start {_EDGE}"]
    assert replacement.attrs["Config"]["Labels"][all_in_one.SPEC_LABEL] == (
        all_in_one.edge_spec_digest(spec, _NETWORK)
    )


def test_an_edge_that_never_started_is_replaced_not_started(daemon: _Daemon) -> None:
    # A start killed between creating the edge and copying its configuration
    # in leaves a container that would serve nginx's default page.
    spec = _spec(all_in_one.EDGE_ROLE)
    digest = all_in_one.edge_spec_digest(spec, _NETWORK)
    daemon.add_container(
        _EDGE, _ours(all_in_one.EDGE_ROLE, **{all_in_one.SPEC_LABEL: digest}), status="created"
    )

    replacement = all_in_one.converge_edge(
        daemon, install_id=_INSTALL_ID, spec=spec, network_name=_NETWORK
    )

    assert daemon.log == [f"remove {_EDGE}", f"create {_EDGE}", f"start {_EDGE}"]
    assert replacement.archives, "the replacement carries its configuration"


def test_a_changed_configuration_file_changes_what_is_kept(tmp_path: Path) -> None:
    spec = _spec(all_in_one.DNS_EDGE_ROLE)
    changed = tmp_path / "dns-edge.nginx.conf"
    changed.write_bytes(spec.files[0].source.read_bytes() + b"# changed\n")
    edited = dataclasses.replace(
        spec, files=(dataclasses.replace(spec.files[0], source=changed),)
    )
    assert all_in_one.edge_spec_digest(edited, _NETWORK) != all_in_one.edge_spec_digest(
        spec, _NETWORK
    )


@pytest.mark.parametrize(
    "labels",
    [
        {},
        {all_in_one.INSTALL_LABEL: "f" * 64, all_in_one.ROLE_LABEL: all_in_one.EDGE_ROLE},
    ],
    ids=["unlabelled", "another-installation"],
)
def test_a_container_this_installation_does_not_own_is_refused_untouched(
    daemon: _Daemon, labels: dict[str, str]
) -> None:
    daemon.add_container(_EDGE, labels, status="running")

    with pytest.raises(OneBoxError, match="never changes or removes"):
        all_in_one.converge_edge(
            daemon,
            install_id=_INSTALL_ID,
            spec=_spec(all_in_one.EDGE_ROLE),
            network_name=_NETWORK,
        )
    assert daemon.log == []


def test_an_edge_whose_configuration_docker_refuses_is_not_left_behind(daemon: _Daemon) -> None:
    daemon.accept_archives = False
    with pytest.raises(OneBoxError, match="refused the configuration"):
        all_in_one.converge_edge(
            daemon,
            install_id=_INSTALL_ID,
            spec=_spec(all_in_one.EDGE_ROLE),
            network_name=_NETWORK,
        )
    assert daemon.log == [f"create {_EDGE}", f"remove {_EDGE}"]


def test_the_edge_image_is_pulled_only_when_the_host_lacks_it(daemon: _Daemon) -> None:
    daemon.images_present.clear()
    all_in_one.converge_edge(
        daemon, install_id=_INSTALL_ID, spec=_spec(all_in_one.EDGE_ROLE), network_name=_NETWORK
    )
    assert daemon.log[0] == "pull nginx:1.27-alpine"


def test_the_private_network_is_created_internal_once_and_a_foreign_one_refused(
    daemon: _Daemon,
) -> None:
    network = all_in_one.ensure_edge_network(daemon, _INSTALL_ID)

    assert network.name == _NETWORK
    assert network.attrs["Internal"] is True
    assert network.attrs["Labels"] == _ours(all_in_one.EDGE_NETWORK_ROLE)
    assert all_in_one.ensure_edge_network(daemon, _INSTALL_ID) is network
    assert daemon.log == [f"create network {_NETWORK}"]

    daemon.networks_by_name[_NETWORK] = _Network(_NETWORK, {}, internal=True)
    with pytest.raises(OneBoxError, match="was not created by AstraBox"):
        all_in_one.ensure_edge_network(daemon, _INSTALL_ID)


def test_a_hostname_that_is_not_the_container_id_is_refused(daemon: _Daemon) -> None:
    own = daemon.add_container("astrabox", {}, status="running")

    assert all_in_one.own_container(daemon, hostname=own.id[:12]) is own
    # `--hostname astrabox` finds a container by that name, which may not be
    # this one; `--network host` gives the host's name, which finds none.
    for hostname in ("astrabox", "build-host-01"):
        with pytest.raises(OneBoxError, match="--hostname"):
            all_in_one.own_container(daemon, hostname=hostname)


def test_the_boundary_hands_onebox_compose_names_under_this_installation(
    daemon: _Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astrabox.deploy import onebox, sandbox_server

    for name in (
        onebox.SANDBOX_EDGE_SERVICE_ENV,
        onebox.SANDBOX_DNS_EDGE_SERVICE_ENV,
        onebox.SANDBOX_EDGE_NETWORK_ENV,
        sandbox_server.PUBLISH_HOST_IP_ENV,
    ):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(
        all_in_one,
        "EDGE_SPECS",
        tuple(_spec(spec.role) for spec in all_in_one.EDGE_SPECS),
    )
    own = daemon.add_container("server-container", {}, status="running", address="172.17.0.2")

    owner = all_in_one.prepare_sandbox_boundary(daemon, own=own, install_id=_INSTALL_ID)

    # This container answers to Compose's service name on the private network,
    # the name both unchanged edge configurations forward to.
    assert daemon.networks_by_name[_NETWORK].connected == [("server-container", ["server"])]
    # And it has left the default bridge for a network of its own.
    platform = all_in_one.resource_name(_INSTALL_ID, all_in_one.PLATFORM_NETWORK_ROLE)
    assert set(own.attrs["NetworkSettings"]["Networks"]) == {_NETWORK, platform}
    assert daemon.networks_by_name[platform].attrs["Internal"] is False
    assert os.environ[onebox.SANDBOX_EDGE_SERVICE_ENV] == "sandbox-edge"
    assert os.environ[onebox.SANDBOX_DNS_EDGE_SERVICE_ENV] == "sandbox-dns-edge"
    assert os.environ[onebox.SANDBOX_EDGE_NETWORK_ENV] == "sandbox-edges"
    # Sandbox ports are published on the bridge gateway, read from Docker.
    assert os.environ[sandbox_server.PUBLISH_HOST_IP_ENV] == "172.17.0.1"
    assert owner.labels == (f"{all_in_one.INSTALL_LABEL}={_INSTALL_ID}",)
    assert owner.service_key == owner.network_key == all_in_one.ROLE_LABEL


def test_a_second_copy_of_the_installation_is_refused(daemon: _Daemon) -> None:
    # A restored backup carries the installation identity; started beside the
    # original, it would answer as `server` on the same network.
    network = all_in_one.ensure_edge_network(daemon, _INSTALL_ID)
    edge = daemon.add_container(_EDGE, {}, status="running")
    original = daemon.add_container("astrabox", {}, status="running")
    network.attrs["Containers"] = {
        edge.id: {"Name": _EDGE},
        original.id: {"Name": "astrabox"},
    }
    restored = daemon.add_container("astrabox-restored", {}, status="running")

    with pytest.raises(OneBoxError, match="running on this installation's network.*astrabox"):
        all_in_one.attach_server(network, restored, install_id=_INSTALL_ID)
    assert network.connected == []

    # The original itself, restarted, finds only its own endpoint and the edge.
    all_in_one.attach_server(network, original, install_id=_INSTALL_ID)


def test_the_container_leaves_the_default_bridge_after_joining_its_own_network(
    daemon: _Daemon,
) -> None:
    # Other containers on the default bridge would reach the no-login API.
    # Joining first keeps a network under the console's port mapping.
    own = daemon.add_container("server-container", {}, status="running")
    platform = all_in_one.ensure_platform_network(daemon, _INSTALL_ID)

    all_in_one.leave_default_bridge(daemon, platform, own)

    assert set(own.attrs["NetworkSettings"]["Networks"]) == {platform.name}
    moves = [entry for entry in daemon.log if entry.startswith(("connect", "disconnect"))]
    assert moves == [
        f"connect server-container to {platform.name}",
        "disconnect server-container from bridge",
    ]

    # The same container restarted is already there: nothing changes.
    daemon.log.clear()
    all_in_one.leave_default_bridge(daemon, platform, own)
    assert daemon.log == []


@pytest.mark.parametrize(
    ("labels", "internal", "message"),
    [
        ({}, False, "was not created by AstraBox"),
        (_ours(all_in_one.PLATFORM_NETWORK_ROLE), True, "is internal, but it must not be"),
    ],
    ids=["foreign", "internal"],
)
def test_a_platform_network_this_image_cannot_use_is_refused(
    daemon: _Daemon, labels: dict[str, str], internal: bool, message: str
) -> None:
    name = all_in_one.resource_name(_INSTALL_ID, all_in_one.PLATFORM_NETWORK_ROLE)
    daemon.networks_by_name[name] = _Network(name, labels, internal=internal)
    with pytest.raises(OneBoxError, match=message):
        all_in_one.ensure_platform_network(daemon, _INSTALL_ID)


def test_the_sandbox_image_is_pulled_only_when_the_host_lacks_it(daemon: _Daemon) -> None:
    image = "ghcr.io/colton-z/astrabox-sandbox-claude-code:0.2.0"
    daemon.images_present.add(image)
    all_in_one.ensure_image(daemon, image, purpose="the test")
    assert daemon.log == []

    daemon.images_present.clear()
    daemon.pull_events = [
        {"status": "Downloading", "id": "l1", "progressDetail": {"current": 5, "total": 10}},
        {"status": "Pull complete", "id": "l1"},
    ]
    all_in_one.ensure_image(daemon, image, purpose="the test")
    assert daemon.log == [f"pull {image}"]


def test_a_failed_pull_stops_the_start_with_the_registry_error(daemon: _Daemon) -> None:
    daemon.images_present.clear()
    daemon.pull_events = [{"error": "manifest unknown"}]
    with pytest.raises(OneBoxError, match="manifest unknown.*docker pull"):
        all_in_one.ensure_image(daemon, "registry.example/astrabox:9", purpose="the test")


@pytest.mark.parametrize(
    ("bindings", "url"),
    [
        ([{"HostIp": "127.0.0.1", "HostPort": "8088"}], "http://127.0.0.1:8088"),
        ([{"HostIp": "::1", "HostPort": "8088"}, {"HostIp": "0.0.0.0", "HostPort": "9000"}],
         "http://127.0.0.1:9000"),
        (None, None),
    ],
    ids=["loopback", "every-interface", "unpublished"],
)
def test_the_ready_line_names_the_published_console_address(
    daemon: _Daemon, bindings: list[dict[str, str]] | None, url: str | None
) -> None:
    own = daemon.add_container("server-container", {}, status="running")
    own.attrs["NetworkSettings"]["Ports"] = {"8000/tcp": bindings}
    assert all_in_one.console_url(own) == url
