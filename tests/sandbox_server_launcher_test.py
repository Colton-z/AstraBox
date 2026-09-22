"""The sandbox-server launcher: configuration refusals and compatibility checks.

Four layers, because they fail for different reasons:

1. **Configuration.** Every value AstraBox renders into the lifecycle server's
   TOML, and every shape that must be REFUSED rather than passed through —
   upstream's posture on a bad value is a warning and a silent fallback, which
   is how a typo becomes a container with no cgroup or a sandbox nobody can
   reach.
2. **The compatibility hooks, against stubs.** ``redirect_publish_host``,
   ``redirect_proxy_host``, ``redirect_public_endpoint_host`` and
   ``redirect_metadata_store_root`` reach into
   upstream internals, so each
   is nailed against a stub carrying upstream's shape: the nail fires in the
   unit lane WITHOUT the optional extra, and it fires for a rename, for a
   collaborator that stops consulting the constant, and for an unwritable
   directory.
3. **The same hooks against the REAL modules**, wherever the extra is installed
   (skipped otherwise) — a stub can only prove the code is self-consistent; only
   the real module can prove the constant is still there, still spelled that way,
   and still carrying the wrong-for-us default that makes the rebind necessary.
4. **The Kubernetes runtime**: its document, its refusals, and its startup proof
   — the last against stubs carrying the Kubernetes client's shape, so it fires
   without a cluster. The Docker document is pinned as RENDERED BYTES in this
   layer, because "the second runtime did not move the first one" is the claim
   the whole split rests on.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import os
import stat
import sys
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Iterator

import pytest

from astrabox.deploy import sandbox_server as launcher

_ALL_ENV = (
    launcher.SERVER_PORT_ENV,
    launcher.METADATA_DIR_ENV,
    launcher.NETWORK_MODE_ENV,
    launcher.EXECD_IMAGE_ENV,
    "ASTRABOX_AGENT_IMAGE",
    launcher.PORT_RANGE_ENV,
    launcher.PIDS_LIMIT_ENV,
    launcher.PUBLISH_HOST_IP_ENV,
    launcher.CREDENTIAL_VAULT_ENV,
    launcher.EGRESS_DNS_UPSTREAM_ENV,
    launcher.LOG_LEVEL_ENV,
    launcher.RUNTIME_ENV,
    launcher.KUBECONFIG_ENV,
    launcher.KUBE_API_SERVER_ENV,
    launcher.KUBE_NAMESPACE_ENV,
    launcher.KUBE_WORKLOAD_PROVIDER_ENV,
    launcher.KUBE_IMAGE_PULL_POLICY_ENV,
    launcher.KUBE_INFORMER_ENV,
    launcher.INGRESS_MODE_ENV,
    launcher.INGRESS_GATEWAY_ADDRESS_ENV,
    launcher.INGRESS_ROUTE_MODE_ENV,
    launcher.INGRESS_SIGNING_KEY_ENV,
    launcher.INGRESS_SIGNING_KEY_ID_ENV,
    launcher.SECURE_ACCESS_ENV,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from "nothing configured" so defaults are the defaults."""
    for name in _ALL_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point AstraBox's ``state_dir`` at a tmp path (the metadata-dir default)."""
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr(
        launcher,
        "metadata_dir",
        lambda: root / launcher.DEFAULT_METADATA_DIRNAME,
    )
    return root


# ---------------------------------------------------------------------------
# 1. configuration
# ---------------------------------------------------------------------------


def test_server_port_defaults_and_composes_the_base_url() -> None:
    assert launcher.server_port() == int(launcher.DEFAULT_SERVER_PORT)
    assert launcher.lifecycle_base_url() == (
        f"http://{launcher.SERVER_BIND_HOST}:{launcher.DEFAULT_SERVER_PORT}"
    )


def test_server_port_is_loopback_only_and_never_configurable() -> None:
    # The bind address is a constant, not a knob: the API has no auth and can
    # create privileged containers on the host daemon.
    assert launcher.SERVER_BIND_HOST == "127.0.0.1"
    assert launcher.lifecycle_base_url().startswith("http://127.0.0.1:")


@pytest.mark.parametrize("value", ["", "0", "70000", "-1", "http://x", "8990.5"])
def test_server_port_refuses_a_non_port(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(launcher.SERVER_PORT_ENV, value or "not-a-port")
    with pytest.raises(launcher.SandboxServerConfigError, match=launcher.SERVER_PORT_ENV):
        launcher.server_port()


def test_network_mode_defaults_to_bridge() -> None:
    assert launcher.network_mode() == "bridge"


@pytest.mark.parametrize("value", ["host", "HOST", " host ", "container:abc123", "Container:x"])
def test_network_mode_refuses_a_shared_network_namespace(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # Every sandbox binds the SAME fixed in-box ports, and a host-networked
    # sandbox's endpoint carries no sandbox identity, so a shared netns makes one
    # session's data plane land in another session's box.
    monkeypatch.setenv(launcher.NETWORK_MODE_ENV, value)
    with pytest.raises(launcher.SandboxServerConfigError, match="shared network namespace"):
        launcher.network_mode()


def test_network_mode_refuses_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(launcher.NETWORK_MODE_ENV, "none")
    with pytest.raises(launcher.SandboxServerConfigError, match="publishes no"):
        launcher.network_mode()


def test_default_vault_refuses_a_user_defined_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(launcher.NETWORK_MODE_ENV, "astrabox-sandboxes")
    with pytest.raises(launcher.SandboxServerConfigError) as error:
        launcher.network_mode()
    assert "Credential Vault" in str(error.value)
    assert "bridge" in str(error.value)


def test_user_defined_network_remains_available_when_vault_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(launcher.CREDENTIAL_VAULT_ENV, "false")
    monkeypatch.setenv(launcher.NETWORK_MODE_ENV, "astrabox-sandboxes")
    assert launcher.network_mode() == "astrabox-sandboxes"


def test_the_bundled_server_allows_only_the_host_owned_dns_upstream() -> None:
    helpers = ModuleType("opensandbox_server.services.helpers")
    helpers.ALLOWED_EGRESS_ENV_VARS = frozenset({"OPENSANDBOX_EGRESS_LOG_LEVEL"})

    def split_egress_env(env: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        allowed = helpers.ALLOWED_EGRESS_ENV_VARS
        for key in env:
            if key.startswith("OPENSANDBOX_EGRESS_") and key not in allowed:
                raise ValueError(key)
        return (
            {key: value for key, value in env.items() if not key.startswith("OPENSANDBOX_EGRESS_")},
            {key: value for key, value in env.items() if key.startswith("OPENSANDBOX_EGRESS_")},
        )

    helpers.split_egress_env = split_egress_env  # type: ignore[attr-defined]
    launcher.allow_host_controlled_egress_dns_upstream(helpers)
    sandbox_env, egress_env = helpers.split_egress_env(  # type: ignore[attr-defined]
        {"USER_ENV": "kept", launcher.UPSTREAM_DNS_ENV: "172.17.0.1:5353"}
    )
    assert sandbox_env == {"USER_ENV": "kept"}
    assert egress_env == {launcher.UPSTREAM_DNS_ENV: "172.17.0.1:5353"}


def _kernel_ephemeral_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, low: int, high: int
) -> None:
    """Pin what the host says its outgoing source-port range is."""

    proc = tmp_path / "ip_local_port_range"
    proc.write_text(f"{low}\t{high}\n")
    monkeypatch.setattr(launcher, "EPHEMERAL_PORT_RANGE_FILE", str(proc))


def test_port_range_defaults_below_the_kernels_ephemeral_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _kernel_ephemeral_range(monkeypatch, tmp_path, 32768, 60999)
    assert launcher.port_range() == (20000, 32000)


def test_port_range_refuses_a_span_the_kernel_also_hands_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The span this deployment shipped before, against a stock Linux host.

    Published sandbox ports and outgoing connections came from one pool, so
    `docker start` lost the race whenever enough sandboxes were created at
    once: "failed to bind host port 0.0.0.0:42838/tcp: address already in
    use", on a port no container held.
    """

    _kernel_ephemeral_range(monkeypatch, tmp_path, 32768, 60999)
    monkeypatch.setenv(launcher.PORT_RANGE_ENV, "40000-60000")
    with pytest.raises(launcher.SandboxServerConfigError, match=launcher.PORT_RANGE_ENV):
        launcher.port_range()


@pytest.mark.parametrize(
    "value",
    [
        "32000-40000",  # starts below, runs into it
        "50000-61000",  # starts inside, runs past the end
        "20000-65000",  # contains it
        "40000-41000",  # wholly inside
    ],
)
def test_port_range_refuses_every_kind_of_overlap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    _kernel_ephemeral_range(monkeypatch, tmp_path, 32768, 60999)
    monkeypatch.setenv(launcher.PORT_RANGE_ENV, value)
    with pytest.raises(launcher.SandboxServerConfigError, match="ephemeral port"):
        launcher.port_range()


def test_port_range_accepts_a_span_above_a_narrowed_kernel_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A host may move its own range; only the actual overlap is refused."""

    _kernel_ephemeral_range(monkeypatch, tmp_path, 10000, 20000)
    monkeypatch.setenv(launcher.PORT_RANGE_ENV, "40000-60000")
    assert launcher.port_range() == (40000, 60000)


def test_port_range_is_unchecked_where_the_kernel_does_not_publish_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No answer to compare against is not a reason to refuse a valid span."""

    monkeypatch.setattr(
        launcher, "EPHEMERAL_PORT_RANGE_FILE", str(tmp_path / "absent")
    )
    monkeypatch.setenv(launcher.PORT_RANGE_ENV, "40000-60000")
    assert launcher.port_range() == (40000, 60000)


@pytest.mark.parametrize(
    "value",
    [
        "40000",  # not a range
        "40000-",  # missing end
        "a-b",  # not numbers
        "40000-40050",  # narrower than 100 ports
        "60000-40000",  # inverted (also narrower than 100)
        "80-9000",  # below 1024
        "40000-70000",  # above 65535
    ],
)
def test_port_range_refuses_a_range_upstream_would_reject(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # Upstream validates this too, but its ValidationError names
    # docker.port_range_min — a TOML key nobody in this deployment ever wrote.
    monkeypatch.setenv(launcher.PORT_RANGE_ENV, value)
    with pytest.raises(launcher.SandboxServerConfigError, match=launcher.PORT_RANGE_ENV):
        launcher.port_range()


def test_port_range_accepts_a_narrowed_firewall_span(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _kernel_ephemeral_range(monkeypatch, tmp_path, 32768, 60999)
    monkeypatch.setenv(launcher.PORT_RANGE_ENV, " 21000 - 21500 ")
    assert launcher.port_range() == (21000, 21500)


def test_pids_limit_defaults_to_512() -> None:
    # Deliberately below upstream's own 4096: a fork bomb inside one sandbox
    # must not be able to exhaust the host's process table.
    assert launcher.pids_limit() == 512
    assert int(launcher.DEFAULT_PIDS_LIMIT) == 512


@pytest.mark.parametrize("value", ["0", "-1", "many"])
def test_pids_limit_refuses_a_non_positive_integer(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(launcher.PIDS_LIMIT_ENV, value)
    with pytest.raises(launcher.SandboxServerConfigError, match=launcher.PIDS_LIMIT_ENV):
        launcher.pids_limit()


def test_metadata_dir_derives_from_state_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "astrabox.config.settings.get_settings",
        lambda: SimpleNamespace(resolved_state_dir=lambda: tmp_path / "state"),
    )
    assert launcher.metadata_dir() == tmp_path / "state" / launcher.DEFAULT_METADATA_DIRNAME


def test_metadata_dir_honours_its_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(launcher.METADATA_DIR_ENV, str(tmp_path / "elsewhere"))
    assert launcher.metadata_dir() == tmp_path / "elsewhere"


def test_publish_host_ip_accepts_an_explicit_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(launcher.PUBLISH_HOST_IP_ENV, "172.17.0.1")
    assert launcher.publish_host_ip() == "172.17.0.1"


def test_publish_host_ip_refuses_a_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    # Docker's HostIp is an address, not a name, and this variable reaches the
    # daemon verbatim.
    monkeypatch.setenv(launcher.PUBLISH_HOST_IP_ENV, "host.docker.internal")
    with pytest.raises(launcher.SandboxServerConfigError, match="must be an IP address"):
        launcher.publish_host_ip()


def test_publish_host_ip_warns_when_told_to_publish_to_every_interface(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(launcher.PUBLISH_HOST_IP_ENV, "0.0.0.0")
    with caplog.at_level(logging.WARNING, logger=launcher.__name__):
        assert launcher.publish_host_ip() == "0.0.0.0"
    assert "no authentication" in caplog.text


def test_publish_host_ip_defaults_to_loopback() -> None:
    # A host-side lifecycle server can reach host-mapped ports on loopback. A
    # containerized server sets the bridge gateway explicitly in Compose.
    assert launcher.publish_host_ip() == "127.0.0.1"
    assert launcher.DEFAULT_PUBLISH_HOST_IP == "127.0.0.1"


def test_publish_host_ip_needs_no_docker_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guards against a daemon dependency: publish_host_ip must never need the
    # Docker daemon to resolve the sandbox network's gateway, because
    # sandboxes are reached through the server proxy, never a host-mapped
    # endpoint. Importing `docker` here must fail the test, not be tolerated.
    monkeypatch.setitem(sys.modules, "docker", None)
    assert launcher.publish_host_ip() == "127.0.0.1"


# ---------------------------------------------------------------------------
# the rendered document
# ---------------------------------------------------------------------------


@pytest.fixture
def document(monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> dict[str, Any]:
    return launcher.config_document()


def test_document_states_the_hardening_profile_explicitly(document: dict[str, Any]) -> None:
    docker = document["docker"]
    # Drop NOTHING: capability drops are too likely to break legitimate agent
    # workloads. Upstream's own default drops nine including SYS_PTRACE and
    # NET_RAW, which this deployment deliberately overrides.
    assert docker["drop_capabilities"] == []
    assert docker["no_new_privileges"] is True
    assert docker["pids_limit"] == 512
    assert docker["network_mode"] == "bridge"
    assert (docker["port_range_min"], docker["port_range_max"]) == (20000, 32000)
    # Left unset, so Docker's own default seccomp profile applies and no
    # AppArmor profile is imposed.
    assert "apparmor_profile" not in docker
    assert "seccomp_profile" not in docker


def test_document_binds_loopback_and_sets_no_endpoint_host(
    document: dict[str, Any],
) -> None:
    assert document["server"]["host"] == "127.0.0.1"
    assert document["server"]["port"] == int(launcher.DEFAULT_SERVER_PORT)
    # `eip` must stay UNSET. Upstream uses it verbatim as the base of the
    # server-proxy URL, while `docker.host_ip` is only the Docker host address.
    # Upstream uses it verbatim as the base of the server-proxy URL it hands back
    # (`f"{eip}/sandboxes/{id}/proxy/{port}"`), so an IP with no port there would
    # point every sandbox's data plane, file panel and WebSocket at port 80 —
    # created successfully, then silently unreachable.
    assert "eip" not in document["server"]
    assert document["docker"]["host_ip"] == "127.0.0.1"


def test_document_denies_every_host_bind_mount(document: dict[str, Any]) -> None:
    # An empty allowlist REJECTS (upstream's shipped example config comments the
    # opposite meaning for the same value); the open_sandbox backend never
    # requests a host mount.
    assert document["storage"]["allowed_host_paths"] == []


def test_document_keeps_upstreams_snapshot_store_out_of_home(
    document: dict[str, Any], state_dir: Path
) -> None:
    path = Path(document["store"]["path"])
    assert path.name == launcher.SNAPSHOT_STORE_FILENAME
    assert str(state_dir) in str(path)


def test_document_carries_no_resource_limits(document: dict[str, Any]) -> None:
    """Per-sandbox CPU/memory caps are a create-REQUEST field, not config.

    There is nowhere in ``AppConfig`` to put them, so an env var here would be
    inert; the gap is the provider's create call, not this document.
    """
    flattened = json.dumps(document)
    assert "resource" not in flattened
    assert "mem_limit" not in flattened


def test_document_pins_the_docker_runtime_and_execd_image(document: dict[str, Any]) -> None:
    assert document["runtime"] == {
        "type": "docker",
        "execd_image": launcher.DEFAULT_EXECD_IMAGE,
    }


def test_execd_image_does_not_treat_an_agent_image_as_an_init_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = "registry.example/astrabox/agent:release-7"
    monkeypatch.setenv("ASTRABOX_AGENT_IMAGE", image)
    assert launcher.execd_image() == "opensandbox/execd:v1.1.0"
    assert launcher.config_document()["runtime"]["execd_image"] == (
        "opensandbox/execd:v1.1.0"
    )


def test_explicit_execd_image_overrides_the_upstream_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_AGENT_IMAGE", "registry.example/agent:7")
    monkeypatch.setenv(launcher.EXECD_IMAGE_ENV, "registry.example/execd:fixed")
    assert launcher.execd_image() == "registry.example/execd:fixed"


def test_rendered_toml_round_trips_through_the_stdlib_parser(document: dict[str, Any]) -> None:
    assert tomllib.loads(launcher.render_config_toml(document)) == document


def test_rendered_toml_escapes_a_string_that_would_break_the_file() -> None:
    rendered = launcher.render_config_toml({"server": {"host": 'a"b\\c'}, "log": {"level": "INFO"}})
    assert tomllib.loads(rendered)["server"]["host"] == 'a"b\\c'


def test_rendered_toml_refuses_a_value_it_cannot_render() -> None:
    with pytest.raises(launcher.SandboxServerConfigError, match="cannot render"):
        launcher.render_config_toml({"server": {"host": 1.5}})


def test_write_config_file_lands_beside_the_metadata_directory(
    document: dict[str, Any], state_dir: Path
) -> None:
    path = launcher.write_config_file(document)
    assert path == launcher.metadata_dir().parent / launcher.CONFIG_FILENAME
    assert tomllib.loads(path.read_text(encoding="utf-8")) == document
    assert "DO NOT EDIT" in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_config_file_fails_loud_on_an_unwritable_directory(
    document: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(launcher, "metadata_dir", lambda: blocker / "metadata")
    with pytest.raises(launcher.SandboxServerConfigError, match=launcher.METADATA_DIR_ENV):
        launcher.write_config_file(document)


# ---------------------------------------------------------------------------
# 2. the three compatibility hooks, against stubs carrying upstream's shape
# ---------------------------------------------------------------------------


def _port_allocator_stub(*, honours_constant: bool = True) -> ModuleType:
    """A stub with upstream's shape: a module constant its allocator reads."""
    module = ModuleType("opensandbox_server.services.docker.port_allocator")
    module.DOCKER_PUBLISH_HOST = "0.0.0.0"  # type: ignore[attr-defined]
    module.PORT_PROBE_HOST = "0.0.0.0"  # type: ignore[attr-defined]

    def allocate_port_bindings(
        container_ports: list[str], min_port: int = 40000, max_port: int = 60000
    ) -> dict[str, tuple[str, int]]:
        host = module.DOCKER_PUBLISH_HOST if honours_constant else "0.0.0.0"
        return {port: (host, min_port) for port in container_ports}

    module.allocate_port_bindings = allocate_port_bindings  # type: ignore[attr-defined]
    return module


def test_redirect_publish_host_rebinds_and_proves_it_through_the_allocator() -> None:
    module = _port_allocator_stub()
    launcher.redirect_publish_host(module, "127.0.0.1", ports=(40000, 60000))
    assert module.DOCKER_PUBLISH_HOST == "127.0.0.1"
    assert module.allocate_port_bindings(["8080"])["8080"][0] == "127.0.0.1"


def test_redirect_publish_host_leaves_the_wider_probe_host_alone() -> None:
    # Upstream's probe must keep the WIDER scope: probing the narrow address
    # would hand out a port already bound on another interface, which Docker
    # then fails to publish.
    module = _port_allocator_stub()
    launcher.redirect_publish_host(module, "127.0.0.1", ports=(40000, 60000))
    assert module.PORT_PROBE_HOST == "0.0.0.0"


@pytest.mark.parametrize("attribute", [launcher.PUBLISH_HOST_ATTR, launcher.ALLOCATE_PORTS_ATTR])
def test_redirect_publish_host_fails_loud_when_upstream_renames_it(attribute: str) -> None:
    module = _port_allocator_stub()
    delattr(module, attribute)
    with pytest.raises(launcher.SandboxServerConfigError, match=attribute):
        launcher.redirect_publish_host(module, "127.0.0.1", ports=(40000, 60000))


def test_redirect_publish_host_fails_loud_when_the_allocator_ignores_the_constant() -> None:
    # The dangerous shape: the constant is still there and still assignable, but
    # the allocator does not consult it — so the rebind "succeeds" and every
    # sandbox port goes to 0.0.0.0 anyway.
    module = _port_allocator_stub(honours_constant=False)
    with pytest.raises(launcher.SandboxServerConfigError, match="still binds"):
        launcher.redirect_publish_host(module, "127.0.0.1", ports=(40000, 60000))


def test_redirect_publish_host_fails_loud_when_the_allocator_raises() -> None:
    module = _port_allocator_stub()

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no free ports")

    module.allocate_port_bindings = _boom  # type: ignore[attr-defined]
    with pytest.raises(launcher.SandboxServerConfigError, match="port allocator failed"):
        launcher.redirect_publish_host(module, "127.0.0.1", ports=(40000, 60000))


def _networking_stub() -> ModuleType:
    module = ModuleType("opensandbox_server.services.docker.networking")

    class DockerNetworkingMixin:
        def _resolve_proxy_host(self) -> str:
            return "127.0.0.1"

        def _resolve_public_host(self) -> str:
            return "127.0.0.1"

    module.DockerNetworkingMixin = DockerNetworkingMixin  # type: ignore[attr-defined]
    return module


def test_redirect_proxy_host_uses_the_docker_host_address() -> None:
    module = _networking_stub()
    launcher.redirect_proxy_host(module, "172.17.0.1")
    instance = object.__new__(module.DockerNetworkingMixin)
    assert instance._resolve_proxy_host() == "172.17.0.1"


@pytest.mark.parametrize(
    "attribute", [launcher.NETWORKING_MIXIN_ATTR, launcher.RESOLVE_PROXY_HOST_ATTR]
)
def test_redirect_proxy_host_fails_loud_when_upstream_renames_it(attribute: str) -> None:
    module = _networking_stub()
    if attribute == launcher.NETWORKING_MIXIN_ATTR:
        delattr(module, attribute)
    else:
        delattr(module.DockerNetworkingMixin, attribute)
    with pytest.raises(launcher.SandboxServerConfigError, match=attribute):
        launcher.redirect_proxy_host(module, "172.17.0.1")


def test_redirect_public_endpoint_host_uses_the_published_port_address() -> None:
    module = _networking_stub()
    launcher.redirect_public_endpoint_host(module, "172.17.0.1")
    instance = object.__new__(module.DockerNetworkingMixin)
    assert instance._resolve_public_host() == "172.17.0.1"


@pytest.mark.parametrize(
    "attribute", [launcher.NETWORKING_MIXIN_ATTR, launcher.RESOLVE_PUBLIC_HOST_ATTR]
)
def test_redirect_public_endpoint_host_fails_loud_when_upstream_renames_it(
    attribute: str,
) -> None:
    module = _networking_stub()
    if attribute == launcher.NETWORKING_MIXIN_ATTR:
        delattr(module, attribute)
    else:
        delattr(module.DockerNetworkingMixin, attribute)
    with pytest.raises(launcher.SandboxServerConfigError, match=attribute):
        launcher.redirect_public_endpoint_host(module, "172.17.0.1")


def _metadata_stub(
    *,
    honours_constant: bool = True,
    writable: bool = True,
    corrupt_read: bool = False,
    home: Path | None = None,
) -> ModuleType:
    """A stub with upstream's shape: a store defaulting to a module constant.

    ``home`` stands in for upstream's ``$HOME``-rooted default. It is a real,
    WRITABLE directory on purpose: the failure being nailed is a store that
    happily writes somewhere else, which is invisible unless the probe checks
    where the bytes landed rather than whether the write raised.
    """
    module = ModuleType("opensandbox_server.services.docker.metadata")
    upstream_home = home or Path("/nonexistent/home/.opensandbox/metadata")
    module.DEFAULT_STORE_DIR = upstream_home  # type: ignore[attr-defined]

    class DockerMetadataStore:
        def __init__(self, root: Path | None = None) -> None:
            fallback = module.DEFAULT_STORE_DIR if honours_constant else upstream_home
            self._root = root or fallback

        def _path(self, sandbox_id: str) -> Path:
            return Path(self._root) / f"{sandbox_id}.expiration.json"

        def set_expiration(self, sandbox_id: str, when: Any) -> None:
            if not writable:
                raise OSError(13, "Permission denied")
            path = self._path(sandbox_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"expires_at": when.isoformat()}), encoding="utf-8")

        def get_expiration(self, sandbox_id: str) -> str | None:
            if corrupt_read:
                return "1999-01-01T00:00:00+00:00"
            path = self._path(sandbox_id)
            if not path.exists():
                return None
            value = json.loads(path.read_text(encoding="utf-8"))["expires_at"]
            return str(value)

        def delete(self, sandbox_id: str) -> None:
            self._path(sandbox_id).unlink(missing_ok=True)

    module.DockerMetadataStore = DockerMetadataStore  # type: ignore[attr-defined]
    return module


def test_redirect_metadata_store_root_rebinds_and_proves_a_round_trip(tmp_path: Path) -> None:
    module = _metadata_stub()
    root = tmp_path / "metadata"
    root.mkdir()
    launcher.redirect_metadata_store_root(module, root)
    assert module.DEFAULT_STORE_DIR == root
    # The probe cleans up after itself: a lifecycle server that later lists the
    # directory must not see AstraBox's startup check.
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    "attribute", [launcher.METADATA_DEFAULT_ROOT_ATTR, launcher.METADATA_STORE_CLS_ATTR]
)
def test_redirect_metadata_store_root_fails_loud_when_upstream_renames_it(
    attribute: str, tmp_path: Path
) -> None:
    module = _metadata_stub()
    delattr(module, attribute)
    with pytest.raises(launcher.SandboxServerConfigError, match=attribute):
        launcher.redirect_metadata_store_root(module, tmp_path)


def test_redirect_metadata_store_root_fails_loud_when_the_store_ignores_the_constant(
    tmp_path: Path,
) -> None:
    # Lease renewals would keep going to $HOME, and a restart would kill live
    # sandboxes at the lease they were born with.
    home = tmp_path / "home" / ".opensandbox" / "metadata"
    target = tmp_path / "metadata"
    target.mkdir()
    module = _metadata_stub(honours_constant=False, home=home)
    with pytest.raises(launcher.SandboxServerConfigError, match="left nothing under"):
        launcher.redirect_metadata_store_root(module, target)
    # The write SUCCEEDED — it just went to upstream's $HOME default, which is
    # exactly the failure a "did the write raise?" check cannot see.
    assert home.exists()
    assert list(target.iterdir()) == []


def test_redirect_metadata_store_root_refuses_an_unwritable_directory(tmp_path: Path) -> None:
    module = _metadata_stub(writable=False)
    with pytest.raises(launcher.SandboxServerConfigError, match="not writable"):
        launcher.redirect_metadata_store_root(module, tmp_path)


def test_redirect_metadata_store_root_refuses_a_store_that_reads_back_wrong(
    tmp_path: Path,
) -> None:
    module = _metadata_stub(corrupt_read=True)
    with pytest.raises(launcher.SandboxServerConfigError, match="did not round-trip"):
        launcher.redirect_metadata_store_root(module, tmp_path)


# ---------------------------------------------------------------------------
# the ephemeral-store warning
# ---------------------------------------------------------------------------


def test_ephemeral_metadata_dir_warns_with_the_consequence(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The single most likely way to hit this: `docker run` with no volume, so the
    # metadata lands on the container's own overlay root.
    monkeypatch.setattr(launcher, "_filesystem_type", lambda _path: "overlay")
    with caplog.at_level(logging.WARNING, logger=launcher.__name__):
        launcher.warn_if_metadata_dir_is_ephemeral(tmp_path)
    # Not "metadata may be lost": the consequence is that a restart kills live,
    # repeatedly-renewed sandboxes at their birth lease.
    assert "birth" in caplog.text
    assert "RENEWAL" in caplog.text


def test_boot_cleared_metadata_dir_warns_even_on_a_persistent_filesystem(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "_filesystem_type", lambda _path: "ext4")
    with caplog.at_level(logging.WARNING, logger=launcher.__name__):
        launcher.warn_if_metadata_dir_is_ephemeral(Path("/tmp/astrabox/opensandbox/metadata"))
    assert "cleared on boot" in caplog.text


def test_persistent_metadata_dir_is_quiet(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "_filesystem_type", lambda _path: "ext4")
    with caplog.at_level(logging.WARNING, logger=launcher.__name__):
        launcher.warn_if_metadata_dir_is_ephemeral(Path("/data/opensandbox/metadata"))
    assert caplog.text == ""


def test_filesystem_type_reads_the_longest_matching_mount() -> None:
    """The real /proc/mounts reader, on a path that certainly has a mount."""
    assert launcher._filesystem_type(Path("/")) is not None


# ---------------------------------------------------------------------------
# the optional extra
# ---------------------------------------------------------------------------


class _BlockUpstream:
    """A meta-path finder that makes ``opensandbox_server`` un-importable.

    Lets the missing-extra behaviour be asserted in an environment where the
    extra IS installed, so the test means the same thing in both lanes.
    """

    def find_module(self, name: str, path: Any = None) -> Any:  # pragma: no cover - legacy hook
        return None

    def find_spec(self, name: str, path: Any = None, target: Any = None) -> Any:
        if name == "opensandbox_server" or name.startswith("opensandbox_server."):
            raise ImportError(f"blocked for the test: {name}")
        return None


@pytest.fixture
def without_extra(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    blocker = _BlockUpstream()
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])
    for name in [n for n in sys.modules if n.startswith("opensandbox_server")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield


def test_importing_the_launcher_never_needs_the_extra(without_extra: None) -> None:
    """The clean-boot invariant: ``import astrabox`` must not need an extra."""
    module = importlib.reload(sys.modules["astrabox.deploy.sandbox_server"])
    assert module.EXTRA_NAME == "sandbox-server"


def test_prepare_reports_the_missing_extra_actionably(
    without_extra: None,
    document: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher, "config_document", lambda: document)
    with pytest.raises(launcher.SandboxServerConfigError, match=r"astrabox\[sandbox-server\]"):
        launcher.prepare()


# ---------------------------------------------------------------------------
# 3. the REAL upstream modules (skipped when the extra is absent)
# ---------------------------------------------------------------------------


def _upstream(name: str) -> Any:
    return pytest.importorskip(
        name,
        reason="needs the sandbox-server extra (pip install -e '.[sandbox-server]')",
    )


def test_upstream_still_publishes_to_every_interface_from_that_constant() -> None:
    """The reason :func:`redirect_publish_host` exists, asserted upstream.

    If this ever fails because upstream now defaults to loopback, or grew a
    config field, delete the rebind instead of updating this test.
    """
    module = _upstream("opensandbox_server.services.docker.port_allocator")
    assert getattr(module, launcher.PUBLISH_HOST_ATTR) == "0.0.0.0"
    assert callable(getattr(module, launcher.ALLOCATE_PORTS_ATTR))


def test_upstream_port_allocator_still_reads_the_constant_at_call_time() -> None:
    module = _upstream("opensandbox_server.services.docker.port_allocator")
    original = getattr(module, launcher.PUBLISH_HOST_ATTR)
    try:
        launcher.redirect_publish_host(module, "127.0.0.1", ports=(40000, 60000))
    finally:
        setattr(module, launcher.PUBLISH_HOST_ATTR, original)


def test_upstream_proxy_host_ignores_docker_host_ip_when_server_binds_loopback() -> None:
    module = _upstream("opensandbox_server.services.docker.networking")
    mixin = getattr(module, launcher.NETWORKING_MIXIN_ATTR)
    instance = object.__new__(mixin)
    instance.app_config = SimpleNamespace(
        server=SimpleNamespace(host="127.0.0.1"),
        docker=SimpleNamespace(host_ip="172.17.0.1"),
    )
    assert instance._resolve_proxy_host() == "127.0.0.1"


def test_redirect_proxy_host_works_against_the_real_upstream_mixin() -> None:
    module = _upstream("opensandbox_server.services.docker.networking")
    mixin = getattr(module, launcher.NETWORKING_MIXIN_ATTR)
    original = getattr(mixin, launcher.RESOLVE_PROXY_HOST_ATTR)
    try:
        launcher.redirect_proxy_host(module, "172.17.0.1")
        instance = object.__new__(mixin)
        assert instance._resolve_proxy_host() == "172.17.0.1"
    finally:
        setattr(mixin, launcher.RESOLVE_PROXY_HOST_ATTR, original)


def test_redirect_public_endpoint_host_works_against_the_real_upstream_mixin() -> None:
    module = _upstream("opensandbox_server.services.docker.networking")
    mixin = getattr(module, launcher.NETWORKING_MIXIN_ATTR)
    original = getattr(mixin, launcher.RESOLVE_PUBLIC_HOST_ATTR)
    try:
        launcher.redirect_public_endpoint_host(module, "172.17.0.1")
        instance = object.__new__(mixin)
        assert instance._resolve_public_host() == "172.17.0.1"
    finally:
        setattr(mixin, launcher.RESOLVE_PUBLIC_HOST_ATTR, original)


def test_upstream_metadata_store_still_defaults_under_home() -> None:
    module = _upstream("opensandbox_server.services.docker.metadata")
    default = getattr(module, launcher.METADATA_DEFAULT_ROOT_ATTR)
    assert Path.home() in Path(default).parents
    assert hasattr(module, launcher.METADATA_STORE_CLS_ATTR)


def test_upstream_service_still_builds_its_store_with_no_argument() -> None:
    """The rebind's premise: there is no injection point for the store root.

    ``DockerMetadataStore.__init__`` accepts a root but the service passes none,
    and the service's own constructor already reads that store, so a
    post-construction swap would read the wrong directory on exactly the restart
    path the redirection exists to fix.
    """
    import inspect

    service = _upstream("opensandbox_server.services.docker.docker_service")
    source = inspect.getsource(service.DockerSandboxService.__init__)
    assert f"{launcher.METADATA_STORE_CLS_ATTR}()" in source


def test_upstream_config_accepts_the_document_astrabox_renders(
    document: dict[str, Any],
) -> None:
    config_module = _upstream("opensandbox_server.config")
    config = config_module.AppConfig(**document)
    assert config.server.host == "127.0.0.1"
    assert config.docker.drop_capabilities == []
    assert config.docker.pids_limit == 512
    # `eip` remains unset. `docker.host_ip` gives a containerized lifecycle
    # server a routable address for Docker host-port mappings.
    assert config.server.eip is None
    assert config.docker.host_ip == "127.0.0.1"


def test_upstream_server_proxy_url_would_swallow_an_eip_without_a_port() -> None:
    """Why ``server.eip`` must stay unset, asserted against upstream's source.

    The endpoint route builds the server-proxy URL by pasting ``server.eip``
    straight in front of the path, with no port of its own. Since AstraBox asks
    for server-proxied endpoints, setting ``eip`` to a bare host address (the
    natural reading of "bound public IP") would hand back
    ``<ip>/sandboxes/<id>/proxy/<port>`` — scheme-default port 80, nothing
    listening, every sandbox created fine and then silently unreachable.

    If this stops matching because upstream started composing a port, revisit
    :func:`astrabox.deploy.sandbox_server.config_document`; until then the field
    stays out of the document.
    """
    import ast
    import importlib.util

    # Read, not import: the lifecycle module loads the server's config file at
    # import time, and this assertion is about its source, not a running server.
    _upstream("opensandbox_server")
    spec = importlib.util.find_spec("opensandbox_server.api.lifecycle")
    assert spec is not None and spec.origin is not None
    module_source = Path(spec.origin).read_text(encoding="utf-8")
    source = next(
        ast.get_source_segment(module_source, node) or ""
        for node in ast.walk(ast.parse(module_source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "get_sandbox_endpoint"
    )
    assert "server.eip" in source
    assert 'f"{base_url}/sandboxes/{sandbox_id}/proxy/{port}"' in source


def test_upstream_has_no_server_side_resource_defaults() -> None:
    """Why there is no CPU/memory env var: there is nowhere to put one."""
    config_module = _upstream("opensandbox_server.config")
    fields = set(config_module.DockerConfig.model_fields)
    assert not {name for name in fields if "mem" in name or "cpu" in name}


# ---------------------------------------------------------------------------
# 4. the Kubernetes runtime
#
# Same three layers as above, one runtime over: what the document must say, what
# must be REFUSED, and the startup proof — the last one against stubs carrying
# the Kubernetes client's shape, so it fires in the unit lane with neither the
# optional extra nor a cluster.
# ---------------------------------------------------------------------------


@pytest.fixture
def kubernetes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(launcher.RUNTIME_ENV, launcher.RUNTIME_KUBERNETES)


def test_runtime_defaults_to_docker_and_accepts_kubernetes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert launcher.sandbox_runtime() == launcher.RUNTIME_DOCKER
    monkeypatch.setenv(launcher.RUNTIME_ENV, "KUBERNETES")
    assert launcher.sandbox_runtime() == launcher.RUNTIME_KUBERNETES


def test_runtime_refuses_anything_upstream_does_not_implement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Upstream's own RuntimeConfig.type is a two-value Literal; a typo must name
    # the AstraBox variable, not arrive as a pydantic error about runtime.type.
    monkeypatch.setenv(launcher.RUNTIME_ENV, "k8s")
    with pytest.raises(launcher.SandboxServerConfigError, match=launcher.RUNTIME_ENV):
        launcher.sandbox_runtime()


def test_docker_document_includes_the_default_credential_sidecar(
    state_dir: Path,
) -> None:
    """Pin the rendered file, including protected delivery's default sidecar."""
    expected = f"""\
# Generated by astrabox.deploy.sandbox_server — DO NOT EDIT.
# Rewritten from the ASTRABOX_* environment on every start; edits are lost.

[server]
host = "127.0.0.1"
port = 8990

[log]
level = "INFO"

[runtime]
type = "docker"
execd_image = "opensandbox/execd:v1.1.0"

[docker]
network_mode = "bridge"
drop_capabilities = []
no_new_privileges = true
pids_limit = 512
port_range_min = 20000
port_range_max = 32000
host_ip = "127.0.0.1"

[storage]
allowed_host_paths = []

[store]
type = "sqlite"
path = "{state_dir / "opensandbox" / "opensandbox.db"}"

[egress]
image = "opensandbox/egress:v1.1.7"
mode = "dns+nft"
"""
    assert launcher.render_config_toml(launcher.config_document()) == expected


def test_kubernetes_document_carries_the_kubernetes_block_and_no_docker_one(
    kubernetes: None, state_dir: Path
) -> None:
    document = launcher.config_document()
    assert document["runtime"] == {
        "type": "kubernetes",
        "execd_image": launcher.DEFAULT_EXECD_IMAGE,
    }
    # Upstream's AppConfig validator makes the two blocks exclusive, and every
    # docker field describes something Kubernetes does not have.
    assert "docker" not in document
    assert document["kubernetes"] == {
        "namespace": launcher.DEFAULT_KUBE_NAMESPACE,
        "workload_provider": launcher.DEFAULT_KUBE_WORKLOAD_PROVIDER,
        "image_pull_policy": launcher.DEFAULT_KUBE_IMAGE_PULL_POLICY,
        "informer_enabled": True,
        "sandbox_create_timeout_seconds": 60,
    }
    # `direct` decides the SHAPE of a resolved endpoint (<pod IP>:<port>), so it
    # is stated rather than inherited.
    assert document["ingress"] == {"mode": "direct"}
    # Shared with the Docker document, and unchanged by the runtime.
    assert document["server"]["host"] == "127.0.0.1"
    assert document["storage"]["allowed_host_paths"] == []
    assert str(state_dir) in document["store"]["path"]
    assert tomllib.loads(launcher.render_config_toml(document)) == document


def test_docker_refuses_kubernetes_secure_access(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(launcher.SECURE_ACCESS_ENV, "true")

    with pytest.raises(
        launcher.SandboxServerConfigError,
        match="not supported.*Docker",
    ):
        launcher.config_document()


def test_kubernetes_gateway_document_uses_opensandbox_native_ingress(
    kubernetes: None, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(launcher.INGRESS_MODE_ENV, "gateway")
    monkeypatch.setenv(launcher.INGRESS_GATEWAY_ADDRESS_ENV, "gateway.example.com")

    ingress = launcher.config_document()["ingress"]

    assert ingress == {
        "mode": "gateway",
        "gateway": {
            "address": "gateway.example.com",
            "route": {"mode": "uri"},
        },
    }
    assert tomllib.loads(launcher.render_config_toml({"ingress": ingress})) == {"ingress": ingress}


def test_kubernetes_secure_gateway_renders_rotatable_signing_keys(
    kubernetes: None, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signing_key = "c2lnbmVkLWVuZHBvaW50LXRlc3Q="
    monkeypatch.setenv(launcher.INGRESS_MODE_ENV, "gateway")
    monkeypatch.setenv(launcher.INGRESS_GATEWAY_ADDRESS_ENV, "*.sandbox.example.com")
    monkeypatch.setenv(launcher.INGRESS_ROUTE_MODE_ENV, "wildcard")
    monkeypatch.setenv(launcher.SECURE_ACCESS_ENV, "true")
    monkeypatch.setenv(launcher.INGRESS_SIGNING_KEY_ENV, signing_key)
    monkeypatch.setenv(launcher.INGRESS_SIGNING_KEY_ID_ENV, "b")

    ingress = launcher.config_document()["ingress"]

    assert ingress["secure_access"] == {
        "active_key": "b",
        "keys": [{"key_id": "b", "key": signing_key}],
    }
    parsed = tomllib.loads(launcher.render_config_toml({"ingress": ingress}))
    assert parsed == {"ingress": ingress}


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (launcher.INGRESS_MODE_ENV, "unknown", launcher.INGRESS_MODE_ENV),
        (launcher.INGRESS_ROUTE_MODE_ENV, "header", "browser-openable"),
        (launcher.INGRESS_GATEWAY_ADDRESS_ENV, "https://gateway.example.com", "scheme"),
    ],
)
def test_kubernetes_gateway_refuses_an_unusable_browser_route(
    kubernetes: None,
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setenv(launcher.INGRESS_MODE_ENV, "gateway")
    monkeypatch.setenv(launcher.INGRESS_GATEWAY_ADDRESS_ENV, "gateway.example.com")
    monkeypatch.setenv(name, value)

    with pytest.raises(launcher.SandboxServerConfigError, match=message):
        launcher.config_document()


def test_kubernetes_gateway_requires_an_address(
    kubernetes: None, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(launcher.INGRESS_MODE_ENV, "gateway")

    with pytest.raises(
        launcher.SandboxServerConfigError,
        match=launcher.INGRESS_GATEWAY_ADDRESS_ENV,
    ):
        launcher.config_document()


def test_kubernetes_secure_gateway_requires_a_valid_signing_key(
    kubernetes: None, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(launcher.INGRESS_MODE_ENV, "gateway")
    monkeypatch.setenv(launcher.INGRESS_GATEWAY_ADDRESS_ENV, "gateway.example.com")
    monkeypatch.setenv(launcher.SECURE_ACCESS_ENV, "true")

    with pytest.raises(
        launcher.SandboxServerConfigError,
        match=launcher.INGRESS_SIGNING_KEY_ENV,
    ):
        launcher.config_document()

    monkeypatch.setenv(launcher.INGRESS_SIGNING_KEY_ENV, "not base64!")
    with pytest.raises(launcher.SandboxServerConfigError, match="base64"):
        launcher.config_document()


def test_kubernetes_document_states_no_hardening_profile_it_cannot_apply(
    kubernetes: None, state_dir: Path
) -> None:
    """No capability / privilege / PID keys invented for a block that has none.

    Upstream's ``[kubernetes]`` block carries no such field: in Kubernetes the
    same layer is the container securityContext, a RuntimeClass and the
    kubelet's podPidsLimit. Rendering AstraBox env into fields upstream does not
    read would be an inert knob.
    """
    flattened = json.dumps(launcher.config_document())
    for absent in ("drop_capabilities", "no_new_privileges", "pids_limit", "secure_runtime"):
        assert absent not in flattened


def test_kubernetes_document_omits_kubeconfig_path_for_in_cluster_credentials(
    kubernetes: None, state_dir: Path
) -> None:
    # Upstream spells "use the ServiceAccount" as an ABSENT kubeconfig_path, so
    # the key must be missing rather than empty.
    assert "kubeconfig_path" not in launcher.config_document()["kubernetes"]


def test_kubernetes_namespace_and_provider_and_policy_are_configurable(
    kubernetes: None, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(launcher.KUBE_NAMESPACE_ENV, "agents")
    monkeypatch.setenv(launcher.KUBE_WORKLOAD_PROVIDER_ENV, "agent-sandbox")
    monkeypatch.setenv(launcher.KUBE_IMAGE_PULL_POLICY_ENV, "Always")
    monkeypatch.setenv(launcher.KUBE_INFORMER_ENV, "off")
    assert launcher.config_document()["kubernetes"] == {
        "namespace": "agents",
        "workload_provider": "agent-sandbox",
        "image_pull_policy": "Always",
        "informer_enabled": False,
        "sandbox_create_timeout_seconds": 60,
    }


@pytest.mark.parametrize(
    ("env", "value", "match"),
    [
        (launcher.KUBE_WORKLOAD_PROVIDER_ENV, "batch-sandbox", "batchsandbox"),
        (launcher.KUBE_IMAGE_PULL_POLICY_ENV, "ifnotpresent", "IfNotPresent"),
        (launcher.KUBE_INFORMER_ENV, "maybe", "boolean"),
    ],
)
def test_kubernetes_knobs_refuse_a_value_upstream_would_not_check(
    kubernetes: None,
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: str,
    value: str,
    match: str,
) -> None:
    """Each of these reaches a Pod spec unchecked, i.e. fails at CREATE time.

    ``image_pull_policy`` is typed as a free string upstream and copied into the
    container; ``workload_provider`` is looked up in a registry when the app
    module is imported. Refusing here is what makes the message name the
    variable that was typed.
    """
    monkeypatch.setenv(env, value)
    with pytest.raises(launcher.SandboxServerConfigError) as error:
        launcher.config_document()
    assert env in str(error.value)
    assert match in str(error.value)


# --- the kubeconfig ---------------------------------------------------------


_KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters:
- name: default
  cluster:
    server: https://127.0.0.1:6443
    certificate-authority-data: Zm9v
contexts:
- name: default
  context:
    cluster: default
    user: default
current-context: default
users:
- name: default
  user:
    client-certificate-data: YmFy
"""


@pytest.fixture
def kubeconfig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "kubeconfig"
    path.write_text(_KUBECONFIG, encoding="utf-8")
    monkeypatch.setenv(launcher.KUBECONFIG_ENV, str(path))
    return path


def test_kubeconfig_is_used_verbatim_when_no_address_is_overridden(
    kubernetes: None, state_dir: Path, kubeconfig: Path
) -> None:
    """No copy, no parse, no credentials duplicated into the state directory.

    AstraBox does NOT detect a loopback address and rewrite it: the address in a
    kubeconfig is also the identity TLS verifies, and the container-reachable
    name that fixes the routing is normally absent from the API server's
    certificate SAN list — so guessing trades a connection error for a
    certificate error. The deployment provides one that works.
    """
    document = launcher.config_document()
    assert document["kubernetes"]["kubeconfig_path"] == str(kubeconfig)
    assert not (launcher.metadata_dir().parent / launcher.KUBECONFIG_FILENAME).exists()
    assert kubeconfig.read_text(encoding="utf-8") == _KUBECONFIG


def test_kubeconfig_address_override_derives_a_copy_and_leaves_the_source_alone(
    kubernetes: None, state_dir: Path, kubeconfig: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    monkeypatch.setenv(launcher.KUBE_API_SERVER_ENV, "https://10.0.1.7:6443")
    derived = Path(launcher.config_document()["kubernetes"]["kubeconfig_path"])

    assert derived == launcher.metadata_dir().parent / launcher.KUBECONFIG_FILENAME
    written = yaml.safe_load(derived.read_text(encoding="utf-8"))
    assert written["clusters"][0]["cluster"]["server"] == "https://10.0.1.7:6443"
    # Everything else rides along untouched — the CA bundle above all, since the
    # substituted address has to verify against it.
    assert written["clusters"][0]["cluster"]["certificate-authority-data"] == "Zm9v"
    assert written["users"][0]["user"]["client-certificate-data"] == "YmFy"
    # The operator's file is input, not state.
    assert kubeconfig.read_text(encoding="utf-8") == _KUBECONFIG


def test_derived_kubeconfig_absolutises_paths_the_move_would_have_broken(
    kubernetes: None, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relative file references are resolved against the SOURCE's directory.

    The Kubernetes client resolves ``certificate-authority`` and friends against
    the kubeconfig's own directory, so writing the derived copy beside
    ``server.toml`` would silently re-point every relative name at a sibling of
    the copy that does not exist — surfacing as ``File does not exist`` for a
    path the operator never configured, on a run whose only change was setting an
    address.
    """
    import yaml

    source_dir = tmp_path / "kube"
    source_dir.mkdir()
    (source_dir / "ca.crt").write_text("ca", encoding="utf-8")
    (source_dir / "client.key").write_text("key", encoding="utf-8")
    source = source_dir / "kubeconfig"
    source.write_text(
        "apiVersion: v1\nkind: Config\n"
        "clusters:\n- name: default\n  cluster:\n"
        "    server: https://127.0.0.1:6443\n    certificate-authority: ca.crt\n"
        "users:\n- name: default\n  user:\n    client-key: client.key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(launcher.KUBECONFIG_ENV, str(source))
    monkeypatch.setenv(launcher.KUBE_API_SERVER_ENV, "https://10.0.1.7:6443")

    derived = Path(launcher.config_document()["kubernetes"]["kubeconfig_path"])
    written = yaml.safe_load(derived.read_text(encoding="utf-8"))
    assert written["clusters"][0]["cluster"]["certificate-authority"] == str(source_dir / "ca.crt")
    assert written["users"][0]["user"]["client-key"] == str(source_dir / "client.key")


def test_derived_kubeconfig_leaves_absolute_paths_alone(
    kubernetes: None, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    source = tmp_path / "kubeconfig"
    source.write_text(
        "apiVersion: v1\nkind: Config\n"
        "clusters:\n- name: default\n  cluster:\n"
        "    server: https://127.0.0.1:6443\n    certificate-authority: /etc/ssl/ca.crt\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(launcher.KUBECONFIG_ENV, str(source))
    monkeypatch.setenv(launcher.KUBE_API_SERVER_ENV, "https://10.0.1.7:6443")
    derived = Path(launcher.config_document()["kubernetes"]["kubeconfig_path"])
    written = yaml.safe_load(derived.read_text(encoding="utf-8"))
    assert written["clusters"][0]["cluster"]["certificate-authority"] == "/etc/ssl/ca.crt"


@pytest.mark.parametrize("value", ["10.0.0.5:6443", "node.internal:6443", "tcp://x:6443"])
def test_api_server_address_refuses_a_value_with_no_usable_scheme(
    kubernetes: None, state_dir: Path, kubeconfig: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Scheme-less is the natural thing to type and the worst thing to accept.

    The Kubernetes client decides whether to load the CA bundle and client
    certificate at all by testing ``host.startswith("https")``, so a missing
    scheme drops TLS entirely and puts the bearer token on the wire — reaching
    the operator as a connection reset with no status code.
    """
    monkeypatch.setenv(launcher.KUBE_API_SERVER_ENV, value)
    with pytest.raises(launcher.SandboxServerConfigError) as error:
        launcher.config_document()
    assert launcher.KUBE_API_SERVER_ENV in str(error.value)
    assert "scheme" in str(error.value)


def test_api_server_address_without_a_kubeconfig_says_it_reaches_nothing(
    kubernetes: None,
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """In-cluster credentials have no document to substitute into.

    Silence here would leave an operator believing they had redirected the
    client at an external API endpoint while it kept talking to
    ``kubernetes.default.svc``.
    """
    monkeypatch.delenv(launcher.KUBECONFIG_ENV, raising=False)
    monkeypatch.setenv(launcher.KUBE_API_SERVER_ENV, "https://api.internal:6443")
    with caplog.at_level(logging.WARNING):
        document = launcher.config_document()
    assert "kubeconfig_path" not in document["kubernetes"]
    assert launcher.KUBE_API_SERVER_ENV in caplog.text
    assert launcher.KUBECONFIG_ENV in caplog.text


def test_a_derived_kubeconfig_nobody_reads_is_removed(
    kubernetes: None, state_dir: Path, kubeconfig: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It holds cluster credentials and lives on the persistent state volume.

    Unsetting the address (or switching back to the Docker runtime) leaves it
    with no reader at all, and a credential nobody reads still ships in every
    backup and image of that volume.
    """
    monkeypatch.setenv(launcher.KUBE_API_SERVER_ENV, "https://10.0.1.7:6443")
    derived = Path(launcher.config_document()["kubernetes"]["kubeconfig_path"])
    assert derived.exists()

    monkeypatch.delenv(launcher.KUBE_API_SERVER_ENV)
    assert launcher.config_document()["kubernetes"]["kubeconfig_path"] == str(kubeconfig)
    assert not derived.exists()


def test_derived_kubeconfig_is_not_readable_by_anyone_else(
    kubernetes: None, state_dir: Path, kubeconfig: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It carries the source's credentials, so it gets the source's protection."""
    monkeypatch.setenv(launcher.KUBE_API_SERVER_ENV, "https://10.0.1.7:6443")
    derived = Path(launcher.config_document()["kubernetes"]["kubeconfig_path"])
    assert derived.stat().st_mode & 0o777 == 0o600


def test_derived_kubeconfig_is_narrowed_even_when_it_already_existed(
    kubernetes: None, state_dir: Path, kubeconfig: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # O_CREAT does not apply a mode to an existing file, so a copy left behind by
    # an earlier, wider-umask boot would keep its old permissions forever.
    destination = launcher.metadata_dir().parent / launcher.KUBECONFIG_FILENAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("stale", encoding="utf-8")
    destination.chmod(0o644)
    monkeypatch.setenv(launcher.KUBE_API_SERVER_ENV, "https://10.0.1.7:6443")
    launcher.config_document()
    assert destination.stat().st_mode & 0o777 == 0o600


def test_kubeconfig_that_cannot_be_read_names_the_user_this_process_runs_as(
    kubernetes: None, state_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The measured failure: a host file mounted 0600 for a different uid.

    The server runs as the image's unprivileged user, not as root and not as the
    host user who created the file, so ``Permission denied`` here is a mount
    problem with a one-line fix — and the message has to say whose uid to make it
    readable by.
    """
    monkeypatch.setenv(launcher.KUBECONFIG_ENV, str(tmp_path / "absent" / "kubeconfig"))
    with pytest.raises(launcher.SandboxServerConfigError) as error:
        launcher.config_document()
    message = str(error.value)
    assert launcher.KUBECONFIG_ENV in message
    assert f"uid {os.getuid()}:{os.getgid()}" in message


def test_kubeconfig_address_override_refuses_a_file_it_cannot_apply_to(
    kubernetes: None, state_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Silently writing a copy with nothing substituted would produce a server
    # that dials the original, unreachable address anyway.
    empty = tmp_path / "kubeconfig"
    empty.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    monkeypatch.setenv(launcher.KUBECONFIG_ENV, str(empty))
    monkeypatch.setenv(launcher.KUBE_API_SERVER_ENV, "https://10.0.1.7:6443")
    with pytest.raises(launcher.SandboxServerConfigError, match=launcher.KUBE_API_SERVER_ENV):
        launcher.config_document()


def test_kubeconfig_address_override_refuses_a_file_that_is_not_yaml(
    kubernetes: None, state_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broken = tmp_path / "kubeconfig"
    broken.write_text("clusters: [\n", encoding="utf-8")
    monkeypatch.setenv(launcher.KUBECONFIG_ENV, str(broken))
    monkeypatch.setenv(launcher.KUBE_API_SERVER_ENV, "https://10.0.1.7:6443")
    with pytest.raises(launcher.SandboxServerConfigError, match="not valid YAML"):
        launcher.config_document()


# --- the startup proof ------------------------------------------------------


class _ApiError(Exception):
    """The shape the Kubernetes client raises once the API server has answered."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class _CoreStub:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.namespaces: list[str] = []

    def read_namespace(self, namespace: str) -> object:
        self.namespaces.append(namespace)
        if self.error is not None:
            raise self.error
        return object()


class _CustomStub:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def list_namespaced_custom_object(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"items": []}


class _AuthzStub:
    def __init__(
        self,
        *,
        allowed: bool = True,
        reason: str | None = None,
        evaluation_error: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.allowed = allowed
        self.reason = reason
        self.evaluation_error = evaluation_error
        self.error = error
        self.bodies: list[dict[str, Any]] = []

    def create_self_subject_access_review(self, body: dict[str, Any]) -> object:
        self.bodies.append(body)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            status=SimpleNamespace(
                allowed=self.allowed,
                reason=self.reason,
                evaluation_error=self.evaluation_error,
            )
        )


def _verify(
    core: _CoreStub | None = None,
    custom: _CustomStub | None = None,
    authz: _AuthzStub | None = None,
    *,
    namespace: str = "opensandbox",
    provider: str = "batchsandbox",
) -> None:
    launcher.verify_kubernetes_access(
        core or _CoreStub(),
        custom or _CustomStub(),
        authz or _AuthzStub(),
        api_server="https://10.0.1.7:6443",
        namespace=namespace,
        workload_provider=provider,
    )


def test_startup_proof_asks_about_the_resource_the_provider_actually_creates() -> None:
    custom, authz = _CustomStub(), _AuthzStub()
    _verify(custom=custom, authz=authz)
    assert custom.calls[0]["group"] == "sandbox.opensandbox.io"
    assert custom.calls[0]["plural"] == "batchsandboxes"
    attributes = authz.bodies[0]["spec"]["resourceAttributes"]
    assert attributes == {
        "namespace": "opensandbox",
        "group": "sandbox.opensandbox.io",
        "resource": "batchsandboxes",
        "verb": "create",
    }


def test_startup_proof_follows_the_configured_provider_to_its_own_resource() -> None:
    custom = _CustomStub()
    _verify(custom=custom, provider="agent-sandbox")
    assert custom.calls[0]["group"] == "agents.x-k8s.io"
    assert custom.calls[0]["plural"] == "sandboxes"


def test_missing_namespace_fails_loud_with_the_command_that_fixes_it() -> None:
    """Measured: the first create against a cluster without it 400s.

    Upstream addresses a namespace rather than creating one — namespaces are a
    cluster-administration boundary — so this is a refusal with the line to run,
    not an attempt to create it.
    """
    with pytest.raises(launcher.SandboxServerConfigError) as error:
        _verify(_CoreStub(error=_ApiError(404)), namespace="sandboxes")
    message = str(error.value)
    assert "kubectl create namespace sandboxes" in message
    assert launcher.KUBE_NAMESPACE_ENV in message


def test_a_namespace_scoped_identity_that_cannot_read_the_namespace_is_not_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A ServiceAccount scoped to one namespace legitimately cannot `get` the
    # Namespace object; refusing there would reject a working deployment for
    # lacking permission it does not need.
    with caplog.at_level(logging.WARNING):
        _verify(_CoreStub(error=_ApiError(403)))
    assert "cannot read the namespace" in caplog.text


@pytest.mark.parametrize("probe", ["namespace", "crd"])
def test_credentials_the_api_server_rejects_outright_are_not_survivable(probe: str) -> None:
    """401 is not 403, and the difference decides whether the server starts.

    A 403 is one identity being refused one thing, which the namespace and CRD
    probes survive by design. A 401 means nothing authenticated — every later
    probe fails the same way, and the access review degrades to a warning when it
    cannot ask, so three warnings would add up to a server that starts and can
    create nothing.
    """
    stubs: dict[str, Any] = {"core": None, "custom": None}
    if probe == "namespace":
        stubs["core"] = _CoreStub(error=_ApiError(401))
    else:
        stubs["custom"] = _CustomStub(error=_ApiError(401))
    with pytest.raises(launcher.SandboxServerConfigError) as error:
        _verify(stubs["core"], stubs["custom"])
    assert "rejected these credentials" in str(error.value)
    assert launcher.KUBECONFIG_ENV in str(error.value)


def test_a_missing_workload_crd_points_at_the_controller_install() -> None:
    with pytest.raises(launcher.SandboxServerConfigError) as error:
        _verify(custom=_CustomStub(error=_ApiError(404)))
    message = str(error.value)
    assert "batchsandboxes.sandbox.opensandbox.io" in message
    assert "controller" in message


def test_credentials_that_cannot_create_the_workload_fail_before_serving() -> None:
    with pytest.raises(launcher.SandboxServerConfigError) as error:
        _verify(authz=_AuthzStub(allowed=False, reason="no RBAC policy matched"))
    message = str(error.value)
    assert "no RBAC policy matched" in message
    assert "kubectl auth can-i create batchsandboxes.sandbox.opensandbox.io" in message


def test_an_authorizer_with_no_opinion_is_a_warning_not_a_refusal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # `allowed: false` with an evaluationError means the authorizer could not
    # DECIDE, which is not the same answer as "no".
    with caplog.at_level(logging.WARNING):
        _verify(authz=_AuthzStub(allowed=False, evaluation_error="webhook timeout"))
    assert "could not decide" in caplog.text


def test_an_unreachable_api_server_names_both_halves_of_the_address_problem() -> None:
    """A transport failure carries no HTTP status; a TLS failure is one of them.

    The measured trap is that the two are one configuration mistake: the address
    has to be routable from inside the container AND covered by the API server
    certificate's SAN, and the host-gateway name that fixes the first breaks the
    second.
    """
    with pytest.raises(launcher.SandboxServerConfigError) as error:
        _verify(_CoreStub(error=OSError("connection refused")))
    message = str(error.value)
    assert launcher.KUBE_API_SERVER_ENV in message
    assert "SAN" in message
    assert "--add-host host.docker.internal:host-gateway" in message


def test_a_tls_identity_failure_lands_on_that_same_answer() -> None:
    verify_failed = OSError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname "
        "mismatch, certificate is not valid for 'host.docker.internal'"
    )
    with pytest.raises(launcher.SandboxServerConfigError, match="certificate"):
        _verify(_CoreStub(error=verify_failed))


# --- prepare(): which half of the module runs -------------------------------


@pytest.fixture
def fake_upstream(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """``opensandbox_server``'s shape, enough for :func:`prepare` to complete.

    The extra is not installed in the unit lane, and ``prepare`` is where the two
    runtimes' halves are chosen — so the choice is nailed here rather than left
    to the live lane.
    """
    root = ModuleType("opensandbox_server")
    config_module = ModuleType("opensandbox_server.config")
    config_module.load_config = lambda path: SimpleNamespace(  # type: ignore[attr-defined]
        log=SimpleNamespace(level="INFO")
    )
    logging_module = ModuleType("opensandbox_server.logging_config")
    logging_module.configure_logging = lambda log: {"version": 1}  # type: ignore[attr-defined]
    services = ModuleType("opensandbox_server.services")
    docker_package = ModuleType("opensandbox_server.services.docker")
    upstream_home = tmp_path / "upstream-home"
    metadata = _metadata_stub(home=upstream_home)
    port_allocator = _port_allocator_stub()
    networking = _networking_stub()
    docker_package.metadata = metadata  # type: ignore[attr-defined]
    docker_package.networking = networking  # type: ignore[attr-defined]
    docker_package.port_allocator = port_allocator  # type: ignore[attr-defined]
    for name, module in {
        "opensandbox_server": root,
        "opensandbox_server.config": config_module,
        "opensandbox_server.logging_config": logging_module,
        "opensandbox_server.services": services,
        "opensandbox_server.services.docker": docker_package,
        "opensandbox_server.services.docker.networking": networking,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return SimpleNamespace(
        metadata=metadata,
        networking=networking,
        port_allocator=port_allocator,
        upstream_home=upstream_home,
    )


def test_prepare_runs_the_docker_halves_on_the_docker_runtime(
    state_dir: Path,
    fake_upstream: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(launcher.PUBLISH_HOST_IP_ENV, "172.17.0.1")
    launcher.prepare()
    assert fake_upstream.port_allocator.DOCKER_PUBLISH_HOST == "172.17.0.1"
    networking = object.__new__(fake_upstream.networking.DockerNetworkingMixin)
    assert networking._resolve_proxy_host() == "172.17.0.1"
    assert networking._resolve_public_host() == "172.17.0.1"
    assert fake_upstream.metadata.DEFAULT_STORE_DIR == launcher.metadata_dir()


def test_prepare_skips_the_docker_hooks_on_the_kubernetes_runtime(
    kubernetes: None,
    state_dir: Path,
    fake_upstream: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipped BY RUNTIME, not by tripping over the absent ``[docker]`` block.

    The publish-host rebind reads ``document["docker"]["port_range_min"]`` for its
    proof. A Kubernetes document has no such block, so "skipped" must be a
    decision — a KeyError caught somewhere would be the same outcome for the
    wrong reason, and would break the moment the document grew that key.
    """
    seen: list[tuple[Path | None, str]] = []
    monkeypatch.setattr(
        launcher,
        "kubernetes_preflight",
        lambda kubeconfig, document: seen.append((kubeconfig, document["kubernetes"]["namespace"])),
    )
    launcher.prepare()

    assert seen == [(None, launcher.DEFAULT_KUBE_NAMESPACE)]
    # Untouched: nothing publishes a host port, and a renewed lease is written to
    # the workload's own spec.expireTime rather than to a local directory.
    assert fake_upstream.port_allocator.DOCKER_PUBLISH_HOST == "0.0.0.0"
    networking = object.__new__(fake_upstream.networking.DockerNetworkingMixin)
    assert networking._resolve_proxy_host() == "127.0.0.1"
    assert networking._resolve_public_host() == "127.0.0.1"
    assert fake_upstream.metadata.DEFAULT_STORE_DIR == fake_upstream.upstream_home


def test_prepare_hands_the_preflight_the_kubeconfig_the_server_will_open(
    kubernetes: None,
    state_dir: Path,
    kubeconfig: Path,
    fake_upstream: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The proof is worthless if it dials credentials the server will not use.
    monkeypatch.setenv(launcher.KUBE_API_SERVER_ENV, "https://10.0.1.7:6443")
    seen: list[Path | None] = []
    monkeypatch.setattr(
        launcher, "kubernetes_preflight", lambda kubeconfig, document: seen.append(kubeconfig)
    )
    launcher.prepare()
    assert seen == [launcher.metadata_dir().parent / launcher.KUBECONFIG_FILENAME]


def test_prepare_does_not_warn_about_metadata_persistence_under_kubernetes(
    kubernetes: None,
    state_dir: Path,
    fake_upstream: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """That warning's consequence does not exist on this runtime.

    It is about losing the only durable record of a lease RENEWAL, which is a
    Docker-runtime fact (Docker cannot update a running container's labels).
    Kubernetes renewals are written to the workload's spec, in the cluster.
    """
    monkeypatch.setattr(launcher, "metadata_dir", lambda: Path("/tmp/astrabox-k8s/metadata"))
    monkeypatch.setattr(launcher, "kubernetes_preflight", lambda kubeconfig, document: None)
    with caplog.at_level(logging.WARNING):
        launcher.prepare()
    assert "will probably NOT survive a restart" not in caplog.text


@pytest.mark.parametrize(
    ("runtime", "env", "value"),
    [
        (launcher.RUNTIME_KUBERNETES, launcher.PORT_RANGE_ENV, "40000-60000"),
        (launcher.RUNTIME_DOCKER, launcher.KUBE_NAMESPACE_ENV, "agents"),
    ],
)
def test_a_knob_the_other_runtime_owns_is_reported_rather_than_ignored(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
    env: str,
    value: str,
) -> None:
    """The "set it, nothing happens" knob class, closed in both directions."""
    monkeypatch.setenv(env, value)
    with caplog.at_level(logging.WARNING):
        launcher.warn_about_inert_knobs(runtime)
    assert env in caplog.text
    assert "other sandbox runtime" in caplog.text


def test_nothing_is_reported_when_only_the_running_runtimes_knobs_are_set(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(launcher.KUBE_NAMESPACE_ENV, "agents")
    with caplog.at_level(logging.WARNING):
        launcher.warn_about_inert_knobs(launcher.RUNTIME_KUBERNETES)
    assert caplog.text == ""


# --- the REAL upstream package (skipped when the extra is absent) -----------


def test_upstream_still_resolves_a_direct_endpoint_to_the_pod_ip() -> None:
    """Pin the direct-mode endpoint shape exposed by the upstream package.

    ``[ingress] mode = "direct"`` answers with the workload's own
    ``<pod IP>:<port>``. That is dialable from a container that shares the node's
    Pod-CIDR route and not from one that does not. Multi-node deployments use
    gateway mode instead.
    """
    provider = _upstream("opensandbox_server.services.k8s.batchsandbox_provider")
    instance = object.__new__(provider.BatchSandboxProvider)
    instance.ingress_config = SimpleNamespace(mode="direct")
    workload = {
        "metadata": {
            "annotations": {
                "sandbox.opensandbox.io/endpoints": '["10.42.0.17"]',
            }
        }
    }

    endpoint = instance.get_endpoint_info(workload, 44772, "sandbox-id")

    assert endpoint is not None
    assert endpoint.endpoint == "10.42.0.17:44772"
    assert instance.get_endpoint_info({}, 44772, "sandbox-id") is None


def test_upstream_still_registers_the_workload_resources_this_module_names() -> None:
    """The startup proof asks about a group/plural AstraBox holds a copy of.

    It has to: the question is asked before upstream's provider is constructed.
    A rename upstream must therefore fail here, not at the first create.
    """
    factory = _upstream("opensandbox_server.services.k8s.provider_factory")
    client = _upstream("opensandbox_server.services.k8s.client")
    assert set(factory._PROVIDER_REGISTRY) == set(launcher.WORKLOAD_RESOURCES)
    group, version, _ = launcher.WORKLOAD_RESOURCES["batchsandbox"]
    assert (group, version) == (client.OPENSANDBOX_API_GROUP, client.OPENSANDBOX_API_VERSION)


def test_upstream_still_creates_no_namespace_of_its_own() -> None:
    """The reason the namespace precheck refuses instead of creating one."""
    service = _upstream("opensandbox_server.services.k8s.kubernetes_service")
    assert not hasattr(service.KubernetesSandboxService, "create_namespace")
    client = _upstream("opensandbox_server.services.k8s.client")
    assert not hasattr(client.K8sClient, "create_namespace")


def test_upstream_config_accepts_the_kubernetes_document_astrabox_renders(
    kubernetes: None, state_dir: Path, tmp_path: Path
) -> None:
    config = _upstream("opensandbox_server.config")
    document = launcher.config_document()
    path = tmp_path / "k8s.toml"
    path.write_text(launcher.render_config_toml(document), encoding="utf-8")
    parsed = config.load_config(path)
    assert parsed.runtime.type == "kubernetes"
    assert parsed.kubernetes.namespace == launcher.DEFAULT_KUBE_NAMESPACE
    assert parsed.kubernetes.workload_provider == launcher.DEFAULT_KUBE_WORKLOAD_PROVIDER
    assert parsed.ingress.mode == "direct"


def test_upstream_config_accepts_the_secure_gateway_document_astrabox_renders(
    kubernetes: None,
    state_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _upstream("opensandbox_server.config")
    signing_key = "c2lnbmVkLWVuZHBvaW50LXRlc3Q="
    monkeypatch.setenv(launcher.INGRESS_MODE_ENV, "gateway")
    monkeypatch.setenv(launcher.INGRESS_GATEWAY_ADDRESS_ENV, "gateway.example.com")
    monkeypatch.setenv(launcher.SECURE_ACCESS_ENV, "true")
    monkeypatch.setenv(launcher.INGRESS_SIGNING_KEY_ENV, signing_key)
    document = launcher.config_document()
    path = tmp_path / "k8s-secure.toml"
    path.write_text(launcher.render_config_toml(document), encoding="utf-8")

    parsed = config.load_config(path)

    assert parsed.ingress.mode == "gateway"
    assert parsed.ingress.gateway.address == "gateway.example.com"
    assert parsed.ingress.gateway.route.mode == "uri"
    assert parsed.ingress.secure_access.active_key == "a"
    assert parsed.ingress.secure_access.keys[0].key == signing_key


# ── [secure_runtime]: the hardening every sandbox runs under ─────────────────


def test_no_hardened_runtime_is_configured_by_default(state_dir: Path) -> None:
    """Absent means runc — and absent from the DOCUMENT, not present and empty.

    Upstream validates the block when it is there, so emitting an empty one
    would turn "this deployment did not choose" into a configuration the server
    has to accept or reject.
    """
    assert launcher.secure_runtime() == {}
    assert "secure_runtime" not in launcher.config_document()


@pytest.mark.parametrize(
    ("chosen", "docker_runtime"),
    [("gvisor", "runsc"), ("kata", "kata-runtime"), ("firecracker", "firecracker")],
)
def test_a_hardened_runtime_is_named_to_both_substrates(
    chosen: str, docker_runtime: str, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One choice, two names: Docker takes a runtime, Kubernetes a RuntimeClass.

    The conventional RuntimeClass name is the type itself, while Docker's
    runtime name is not (`gvisor` is installed as `runsc`), which is why
    upstream keeps two fields rather than one.
    """
    monkeypatch.setenv(launcher.SECURE_RUNTIME_ENV, chosen)
    block = launcher.secure_runtime()
    assert block == {
        "type": chosen,
        "docker_runtime": docker_runtime,
        "k8s_runtime_class": chosen,
    }
    assert launcher.config_document()["secure_runtime"] == block


def test_the_choice_is_case_and_space_insensitive(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(launcher.SECURE_RUNTIME_ENV, "  GVisor ")
    assert launcher.secure_runtime()["type"] == "gvisor"


def test_an_unknown_runtime_refuses_to_start(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole value of this knob is that a wrong one cannot run unhardened.

    A typo that fell through to runc would leave a deployment believing it was
    isolated when it was not — the one failure this must never have.
    """
    monkeypatch.setenv(launcher.SECURE_RUNTIME_ENV, "gvisr")
    with pytest.raises(launcher.SandboxServerConfigError) as caught:
        launcher.secure_runtime()
    assert "gvisr" in str(caught.value)
    assert "gvisor" in str(caught.value)


def test_upstream_accepts_the_hardened_document(
    kubernetes: None, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Against upstream's own loader, so the block is one it really reads."""
    monkeypatch.setenv(launcher.SECURE_RUNTIME_ENV, "gvisor")
    config = _upstream("opensandbox_server.config")
    path = tmp_path / "hardened.toml"
    path.write_text(launcher.render_config_toml(launcher.config_document()), encoding="utf-8")
    parsed = config.load_config(path)
    assert parsed.secure_runtime.type == "gvisor"
    assert parsed.secure_runtime.k8s_runtime_class == "gvisor"


# ── [egress]: the sidecar a network policy would be enforced by ──────────────


def test_the_pinned_egress_sidecar_is_available_by_default(state_dir: Path) -> None:
    assert launcher.egress() == {
        "image": launcher.DEFAULT_EGRESS_IMAGE,
        "mode": "dns+nft",
    }
    assert launcher.config_document()["egress"]["image"] == launcher.DEFAULT_EGRESS_IMAGE


def test_naming_the_sidecar_renders_the_block(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(launcher.EGRESS_IMAGE_ENV, launcher.DEFAULT_EGRESS_IMAGE)
    assert launcher.egress() == {"image": launcher.DEFAULT_EGRESS_IMAGE, "mode": "dns+nft"}
    assert launcher.config_document()["egress"]["image"] == launcher.DEFAULT_EGRESS_IMAGE


def test_the_strict_mode_is_the_default(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`dns+nft`, not `dns`, and deliberately not upstream's own default.

    A DNS-only policy is bypassed by connecting to an address directly, so a
    deployment that asked for egress control and got name filtering has a
    weaker guarantee than it believes. Opting DOWN is possible; drifting down is
    not.
    """
    monkeypatch.setenv(launcher.EGRESS_IMAGE_ENV, "example/egress:1")
    assert launcher.egress()["mode"] == "dns+nft"
    monkeypatch.setenv(launcher.EGRESS_MODE_ENV, "dns")
    assert launcher.egress()["mode"] == "dns"


def test_an_unknown_egress_mode_refuses_to_start(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(launcher.EGRESS_IMAGE_ENV, "example/egress:1")
    monkeypatch.setenv(launcher.EGRESS_MODE_ENV, "nft")
    with pytest.raises(launcher.SandboxServerConfigError) as caught:
        launcher.egress()
    assert "nft" in str(caught.value)


def test_the_mode_is_refused_even_with_no_sidecar_named(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad value must not wait for someone to also set the image to be noticed.

    Validating only on the path that uses it would let a typo sit in a
    deployment's environment until the day it turns egress on — which is the day
    it least wants to discover one.
    """
    monkeypatch.setenv(launcher.EGRESS_MODE_ENV, "strict")
    with pytest.raises(launcher.SandboxServerConfigError):
        launcher.egress()


def test_upstream_accepts_the_egress_document(
    kubernetes: None, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(launcher.EGRESS_IMAGE_ENV, launcher.DEFAULT_EGRESS_IMAGE)
    config = _upstream("opensandbox_server.config")
    path = tmp_path / "egress.toml"
    path.write_text(launcher.render_config_toml(launcher.config_document()), encoding="utf-8")
    parsed = config.load_config(path)
    assert parsed.egress.image == launcher.DEFAULT_EGRESS_IMAGE
    assert parsed.egress.mode == "dns+nft"


def test_create_timeout_defaults_to_upstreams_own_sixty() -> None:
    """The default is upstream's, restated rather than invented.

    Sixty seconds is also the symptom: it is what a create gives up at on a
    cold node, so a reader who finds this value has found the knob they came
    for.
    """
    assert launcher.kube_create_timeout_seconds() == 60


@pytest.mark.parametrize("value", ["0", "-1", "60s", "1.5"])
def test_create_timeout_refuses_what_upstream_would_reject(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Refused here, where the message can name the variable.

    Upstream constrains the field to ``>= 1``. Passing a bad value through
    would move the rejection to the far side of the seam, where it describes
    upstream's schema to someone who typed an AstraBox variable.
    """
    monkeypatch.setenv(launcher.KUBE_CREATE_TIMEOUT_ENV, value)
    with pytest.raises(launcher.SandboxServerConfigError):
        launcher.kube_create_timeout_seconds()


def test_upstream_accepts_the_create_timeout(
    kubernetes: None, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The field reaches upstream under the name upstream reads.

    A rendered key upstream ignores is an inert knob that looks configured, and
    nothing else in this suite would notice: the TOML would be well-formed and
    the sandbox would go on timing out at sixty. Parsing the document with
    upstream's own loader is what makes the wiring real rather than plausible.
    """
    monkeypatch.setenv(launcher.KUBE_CREATE_TIMEOUT_ENV, "420")
    config = _upstream("opensandbox_server.config")
    path = tmp_path / "kubernetes.toml"
    path.write_text(launcher.render_config_toml(launcher.config_document()), encoding="utf-8")
    parsed = config.load_config(path)
    assert parsed.kubernetes.sandbox_create_timeout_seconds == 420
