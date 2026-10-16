"""The all-in-one entry point's security refusals and the secrets it guards in ``/data``.

Pinned here: a supplied setting cannot reopen the sandbox boundary or have a
credential echoed back; the database passwords are created once, private and
never replaced, and a symlinked or malformed one is refused; a foreign-owned
data directory is not taken over; and another installation's containers and
networks, or a second copy of this one, are refused untouched. Starting the real
services and the sandbox boundary in a container is proven on a Docker host,
not here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import stat
import sys
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
}


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_an_external_database_is_refused_with_the_compose_path() -> None:
    problems = all_in_one.refusals(
        {**_IMAGE_DEFAULTS, "ASTRABOX_DB_URL": "postgresql+asyncpg://a:b@db/astrabox"}
    )
    assert len(problems) == 1
    assert problems[0].startswith("ASTRABOX_DB_URL is set")
    assert "Docker Compose" in problems[0]
    assert "a:b@db" not in problems[0], "a refusal must not echo a credential-bearing value"


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


# ---------------------------------------------------------------------------
# root phase and the Docker engine
# ---------------------------------------------------------------------------


def test_a_foreign_owned_data_directory_with_files_is_refused(tmp_path: Path) -> None:
    (tmp_path / "vault.key").write_text("someone's key\n", encoding="ascii")
    with pytest.raises(OneBoxError, match="does not change the owner"):
        all_in_one.claim_data_dir(tmp_path, uid=os.getuid() + 1, gid=os.getgid())
    # The runtime user's own directory is left exactly as it is.
    all_in_one.claim_data_dir(tmp_path, uid=os.getuid(), gid=os.getgid())


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
