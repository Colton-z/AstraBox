"""The sandbox-server launcher's hardening and its refusals.

Upstream's posture on a bad value is a warning and a silent fallback, which is
how a typo becomes a sandbox nobody isolated. Pinned here: the server binds
loopback, publishes on loopback and mounts nothing from the host; a shared
network namespace, a missing network policy, a user-defined network under the
default vault, a weak egress mode and an unknown hardened runtime are refused;
execd is published on no other interface; IPv6 is off in every sandbox
namespace; Kubernetes sandboxes drop Docker's capabilities; a derived
kubeconfig is private and removed once nobody reads it; and another
installation's sandboxes and sidecars are left alone.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Iterable, Iterator

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


def test_server_port_is_loopback_only_and_never_configurable() -> None:
    # The bind address is a constant, not a knob: the API has no auth and can
    # create privileged containers on the host daemon.
    assert launcher.SERVER_BIND_HOST == "127.0.0.1"
    assert launcher.lifecycle_base_url().startswith("http://127.0.0.1:")


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


def test_default_vault_refuses_a_user_defined_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(launcher.NETWORK_MODE_ENV, "astrabox-sandboxes")
    with pytest.raises(launcher.SandboxServerConfigError) as error:
        launcher.network_mode()
    assert "Credential Vault" in str(error.value)
    assert "bridge" in str(error.value)


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


def test_publish_host_ip_defaults_to_loopback() -> None:
    # A host-side lifecycle server can reach host-mapped ports on loopback. A
    # containerized server sets the bridge gateway explicitly in Compose.
    assert launcher.publish_host_ip() == "127.0.0.1"
    assert launcher.DEFAULT_PUBLISH_HOST_IP == "127.0.0.1"


# ---------------------------------------------------------------------------
# the rendered document
# ---------------------------------------------------------------------------


@pytest.fixture
def document(monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> dict[str, Any]:
    return launcher.config_document()


def test_document_states_the_hardening_profile_explicitly(document: dict[str, Any]) -> None:
    docker = document["docker"]
    # Drop NET_RAW so a root process in a sandbox cannot open raw/packet
    # sockets and craft traffic that bypasses the egress sidecar's output hook.
    # Other capabilities upstream drops by default (SYS_PTRACE among them) are
    # kept, because debugger and build workloads use them.
    assert docker["drop_capabilities"] == ["NET_RAW"]
    assert docker["drop_capabilities"] == list(launcher.SANDBOX_DROP_CAPABILITIES)
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


# ---------------------------------------------------------------------------
# 2. the three compatibility hooks, against stubs carrying upstream's shape
# ---------------------------------------------------------------------------


def _networking_stub(*, sidecar_uses_normalizer: bool = True) -> ModuleType:
    module = ModuleType("opensandbox_server.services.docker.networking")
    module.normalize_port_bindings = lambda bindings: dict(bindings)  # type: ignore[attr-defined]
    module.EGRESS_SIDECAR_LABEL = "opensandbox.io/egress-sidecar-for"  # type: ignore[attr-defined]

    class DockerNetworkingMixin:
        def _resolve_proxy_host(self) -> str:
            return "127.0.0.1"

        def _resolve_public_host(self) -> str:
            return "127.0.0.1"

        def _wait_for_egress_sidecar_ready(
            self, sandbox_id: str, host_port: int, egress_token: str, timeout_seconds: float
        ) -> None:
            return None

        def _start_egress_sidecar(
            self,
            sandbox_id: str,
            network_policy: Any,
            egress_token: str,
            host_execd_port: int,
            host_http_port: int,
            **_kwargs: Any,
        ) -> Any:
            """Upstream 0.2.3's order: image, policy, literal 0.0.0.0, host config."""
            self._ensure_image_available(self.app_config.egress.image, None, sandbox_id)  # type: ignore[attr-defined]
            network_policy.model_dump(by_alias=True, exclude_none=True)
            bindings = {
                "44772": ("0.0.0.0", host_execd_port),
                "8080": ("0.0.0.0", host_http_port),
            }
            if sidecar_uses_normalizer:
                bindings = module.normalize_port_bindings(bindings)  # type: ignore[attr-defined]
            return self.docker_client.api.create_host_config(  # type: ignore[attr-defined]
                network_mode="bridge", port_bindings=bindings
            )

    module.DockerNetworkingMixin = DockerNetworkingMixin  # type: ignore[attr-defined]
    return module


def _recorded_sidecar_hosts(module: ModuleType) -> dict[str, str]:
    bindings = launcher._record_egress_sidecar_port_bindings(
        getattr(module, launcher.NETWORKING_MIXIN_ATTR)
    )
    return {port: str(binding[0]) for port, binding in bindings.items()}


def test_redirect_egress_sidecar_publish_host_narrows_execd_and_http() -> None:
    module = _networking_stub()
    assert _recorded_sidecar_hosts(module) == {"44772": "0.0.0.0", "8080": "0.0.0.0"}

    launcher.redirect_egress_sidecar_publish_host(module, "172.17.0.1")

    assert _recorded_sidecar_hosts(module) == {"44772": "172.17.0.1", "8080": "172.17.0.1"}
    # A binding upstream already narrowed (the allocator's egress API port)
    # passes through unchanged.
    assert module.normalize_port_bindings({"18080": ("10.0.0.5", 3)}) == {  # type: ignore[attr-defined]
        "18080": ("10.0.0.5", 3)
    }


def test_redirect_egress_sidecar_publish_host_fails_loud_when_the_sidecar_bypasses_it() -> None:
    # The dangerous shape: the normalizer is still there and replaceable, but the
    # sidecar stops passing its bindings through it, so every sandbox's execd
    # would go back to 0.0.0.0 while the hook "succeeded".
    module = _networking_stub(sidecar_uses_normalizer=False)
    with pytest.raises(launcher.SandboxServerConfigError, match="ignored"):
        launcher.redirect_egress_sidecar_publish_host(module, "172.17.0.1")


# ---------------------------------------------------------------------------
# 3. the REAL upstream modules (skipped when the extra is absent)
# ---------------------------------------------------------------------------


def _upstream(name: str) -> Any:
    return pytest.importorskip(
        name,
        reason="needs the sandbox-server extra (pip install -e '.[sandbox-server]')",
    )


class _FakeDockerDaemon:
    """Docker's side of a create, for upstream's real provisioning code.

    ``held`` are host ports something outside the lifecycle server's network
    namespace holds: invisible to a probe the server runs, visible to Docker,
    which binds in the host namespace. A range binding gets the lowest port of
    its range that is neither held nor already published, as the daemon's
    allocator does; a fixed binding on a held port fails the way Docker does.
    """

    def __init__(self, held: Iterable[int]) -> None:
        self.held = set(held)
        self.created: dict[str, dict[str, Any]] = {}
        self.published: dict[str, dict[str, list[dict[str, str]]]] = {}
        self.removed: list[str] = []
        #: Resource updates applied to a running container, by container id.
        self.updates: dict[str, dict[str, Any]] = {}
        self.api = SimpleNamespace(
            create_host_config=lambda **kwargs: dict(kwargs),
            create_container=self._create_container,
            remove_container=lambda container_id, force=False: self.removed.append(container_id),
            inspect_volume=lambda name: {"Labels": {}},
            remove_volume=lambda name: None,
        )
        self.volumes = SimpleNamespace(create=lambda **kwargs: SimpleNamespace(**kwargs))
        self.containers = SimpleNamespace(get=self._container, list=self._list)

    def _create_container(self, **kwargs: Any) -> dict[str, str]:
        container_id = f"container-{len(self.created)}"
        self.created[container_id] = kwargs
        return {"Id": container_id}

    def add(self, name: str, labels: dict[str, str]) -> None:
        """A container that already exists when the lifecycle server starts."""
        self.created[name] = {"name": name, "labels": labels, "host_config": {}}

    def _list(self, all: bool = False, filters: dict[str, Any] | None = None) -> list[Any]:
        selector = str((filters or {}).get("label") or "")
        key, _, value = selector.partition("=")
        return [
            self._container(container_id)
            for container_id, record in self.created.items()
            if container_id not in self.removed
            and (
                not key
                or (
                    key in (record.get("labels") or {})
                    and (not value or record["labels"][key] == value)
                )
            )
        ]

    def _container(self, reference: str) -> Any:
        from docker.errors import NotFound

        daemon = self
        container_id = next(
            (
                candidate
                for candidate, record in self.created.items()
                if reference in (candidate, record.get("name"))
            ),
            None,
        )
        if container_id is None or container_id in self.removed:
            raise NotFound(f"No such container: {reference}")
        record = self.created[container_id]

        class _Container:
            id = container_id

            @property
            def labels(self) -> dict[str, str]:
                return dict(record.get("labels") or {})

            @property
            def attrs(self) -> dict[str, Any]:
                return {
                    "Config": {"Labels": record.get("labels") or {}},
                    "NetworkSettings": {"Ports": daemon.published.get(container_id, {})},
                    "State": {"Running": True},
                }

            def reload(self) -> None:
                return None

            def kill(self) -> None:
                return None

            def start(self) -> None:
                daemon._publish(container_id, record["host_config"].get("port_bindings") or {})

            def remove(self, force: bool = False) -> None:
                daemon.removed.append(container_id)

            def update(self, **resources: Any) -> None:
                daemon.updates.setdefault(container_id, {}).update(resources)

        return _Container()

    def _publish(self, container_id: str, bindings: dict[str, Any]) -> None:
        from docker.errors import APIError

        in_use = self.held | {
            int(binding["HostPort"])
            for ports in self.published.values()
            for published in ports.values()
            for binding in published
        }
        ports: dict[str, list[dict[str, str]]] = {}
        for container_port, (host_ip, host_port) in bindings.items():
            low, _, high = str(host_port).partition("-")
            candidates = range(int(low), int(high or low) + 1)
            free = [port for port in candidates if port not in in_use]
            if not free:
                raise APIError(
                    "500 Server Error: failed to set up container networking: "
                    f"failed to bind host port {host_ip}:{candidates[-1]}/tcp: "
                    "address already in use"
                )
            in_use.add(free[0])
            key = container_port if "/" in container_port else f"{container_port}/tcp"
            ports[key] = [{"HostIp": host_ip, "HostPort": str(free[0])}]
        self.published[container_id] = ports


@pytest.fixture
def upstream_docker_service(
    document: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> Iterator[Any]:
    """Upstream's real Docker service class with every hook this deployment applies.

    Each patched upstream attribute is registered with ``monkeypatch`` first, so
    the real classes are restored after the test.
    """
    service = _upstream("opensandbox_server.services.docker.docker_service")
    networking = _upstream("opensandbox_server.services.docker.networking")
    container_ops = _upstream("opensandbox_server.services.docker.container_ops")
    volumes = _upstream("opensandbox_server.services.docker.volumes")
    constants = _upstream("opensandbox_server.services.constants")
    config_module = _upstream("opensandbox_server.config")
    for owner, attribute in (
        (service, launcher.SERVICE_ALLOCATE_PORTS_ATTR),
        (networking, launcher.SIDECAR_PORT_BINDINGS_ATTR),
        (networking.DockerNetworkingMixin, launcher.START_EGRESS_SIDECAR_ATTR),
        (container_ops.DockerContainerOpsMixin, launcher.CREATE_AND_START_ATTR),
        (volumes.DockerVolumesMixin, launcher.BUILD_VOLUME_BINDS_ATTR),
    ):
        monkeypatch.setattr(owner, attribute, getattr(owner, attribute))
    document["docker"]["host_ip"] = "172.17.0.1"
    document["docker"]["port_range_min"] = 20000
    document["docker"]["port_range_max"] = 20100
    launcher.redirect_egress_sidecar_publish_host(networking, "172.17.0.1")
    launcher.let_docker_assign_published_ports(
        service, networking, container_ops, constants, "172.17.0.1", ports=(20000, 20100)
    )
    launcher.mount_volume_subpaths_through_docker(volumes, container_ops)
    instance = object.__new__(service.DockerSandboxService)
    instance.app_config = config_module.AppConfig(**document)
    instance.network_mode = "bridge"
    instance.docker_runtime = None
    instance.readiness_ports = []
    instance._ensure_image_available = lambda *_args, **_kwargs: None
    instance._prepare_sandbox_runtime = lambda *_args, **_kwargs: None
    instance._resolve_platform_for_container = lambda *_args, **_kwargs: None

    def _record_readiness_port(
        sandbox_id: str, host_port: int, token: str, timeout_seconds: float
    ) -> None:
        instance.readiness_ports.append(host_port)

    instance._wait_for_egress_sidecar_ready = _record_readiness_port
    yield instance


def _provision(
    instance: Any,
    daemon: _FakeDockerDaemon,
    *,
    network_policy: bool = True,
    volumes: list[dict[str, Any]] | None = None,
    pvc_inspect_cache: dict[str, dict[str, Any]] | None = None,
) -> Any:
    schema = _upstream("opensandbox_server.api.schema")
    instance.docker_client = daemon
    request = schema.CreateSandboxRequest(
        image={"uri": "astrabox/sandbox:test"},
        entrypoint=["/bin/sleep", "infinity"],
        resourceLimits={"cpu": "1", "memory": "1Gi"},
        **({"networkPolicy": {"defaultAction": "allow"}} if network_policy else {}),
        **({"volumes": volumes} if volumes else {}),
    )
    return instance._provision_sandbox(
        "sbx", request, datetime.now(timezone.utc), None, pvc_inspect_cache
    )


def test_a_sandbox_without_a_network_policy_is_refused_before_any_container(
    upstream_docker_service: Any,
) -> None:
    # Such a sandbox would publish its own ports, and its endpoint labels are
    # written before Docker chooses them.
    from fastapi import HTTPException

    daemon = _FakeDockerDaemon(held=())
    with pytest.raises(HTTPException) as raised:
        _provision(upstream_docker_service, daemon, network_policy=False)
    assert raised.value.status_code == 400
    assert "networkPolicy" in raised.value.detail["message"]
    assert daemon.created == {}


def test_a_restarted_server_leaves_another_installations_sandbox_running(
    document: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both sandboxes are past their create-time lease; only one is this server's.

    The other installation renews its sandbox through its own lifecycle
    server, so this server's restart must not expire it from the stale label.
    """
    lapsed = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    daemon = _FakeDockerDaemon(held=())
    for sandbox_id, installation in (("ours", "this-installation"), ("theirs", "other")):
        daemon.add(
            f"sandbox-{sandbox_id}",
            {
                "opensandbox.io/id": sandbox_id,
                "opensandbox.io/expires-at": lapsed,
                "astrabox.installation": installation,
            },
        )

    _started_service(document, monkeypatch, tmp_path, daemon)

    assert daemon.removed == ["sandbox-ours"]


def _started_service(
    document: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    daemon: _FakeDockerDaemon,
) -> Any:
    """Upstream's real service, constructed (and so restored) against ``daemon``."""
    service = _upstream("opensandbox_server.services.docker.docker_service")
    networking = _upstream("opensandbox_server.services.docker.networking")
    metadata = _upstream("opensandbox_server.services.docker.metadata")
    config_module = _upstream("opensandbox_server.config")
    monkeypatch.setattr(metadata, launcher.METADATA_DEFAULT_ROOT_ATTR, tmp_path / "metadata")
    for attribute in (
        "__init__",
        launcher.EXPIRE_SANDBOX_ATTR,
        launcher.CLEANUP_SIDECAR_ATTR,
    ):
        monkeypatch.setattr(
            service.DockerSandboxService,
            attribute,
            getattr(service.DockerSandboxService, attribute),
        )
    launcher.leave_other_installations_sandboxes_running(service, networking, "this-installation")

    def _from_env(timeout: int | None = None) -> _FakeDockerDaemon:
        return daemon

    monkeypatch.setattr(service.docker, "from_env", _from_env)
    return service.DockerSandboxService(config=config_module.AppConfig(**document))


def test_a_restarted_server_leaves_another_installations_sidecar(
    document: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sidecars with no sandbox container: this installation's leftover, and
    another installation's create in progress (its sidecar starts first)."""
    daemon = _FakeDockerDaemon(held=())
    for sandbox_id, installation in (("ours", "this-installation"), ("theirs", "other")):
        daemon.add(
            f"sandbox-egress-{sandbox_id}",
            {
                "opensandbox.io/egress-sidecar-for": sandbox_id,
                "astrabox.installation": installation,
            },
        )

    _started_service(document, monkeypatch, tmp_path, daemon)

    assert daemon.removed == ["sandbox-egress-ours"]


def test_the_server_labels_every_sidecar_it_creates_with_its_installation(
    document: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon = _FakeDockerDaemon(held=())
    instance = _started_service(document, monkeypatch, tmp_path, daemon)
    instance._ensure_image_available = lambda *_args, **_kwargs: None
    instance._prepare_sandbox_runtime = lambda *_args, **_kwargs: None
    instance._resolve_platform_for_container = lambda *_args, **_kwargs: None
    instance._wait_for_egress_sidecar_ready = lambda *_args, **_kwargs: None

    _provision(instance, daemon)

    sidecar = next(
        record
        for record in daemon.created.values()
        if "opensandbox.io/egress-sidecar-for" in (record.get("labels") or {})
    )
    assert sidecar["labels"]["astrabox.installation"] == "this-installation"


def test_redirect_egress_sidecar_publish_host_works_against_the_real_upstream_sidecar() -> None:
    module = _upstream("opensandbox_server.services.docker.networking")
    original = getattr(module, launcher.SIDECAR_PORT_BINDINGS_ATTR)
    try:
        launcher.redirect_egress_sidecar_publish_host(module, "172.17.0.1")
        hosts = _recorded_sidecar_hosts(module)
    finally:
        setattr(module, launcher.SIDECAR_PORT_BINDINGS_ATTR, original)
    assert hosts["44772"] == "172.17.0.1"
    assert hosts["8080"] == "172.17.0.1"


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


def test_kubernetes_sandbox_containers_drop_the_docker_capabilities_through_the_template(
    kubernetes: None, state_dir: Path
) -> None:
    """The pinned server has no capability field for Kubernetes; its template does it."""
    template_file = Path(launcher.config_document()["kubernetes"]["batchsandbox_template_file"])

    template = json.loads(template_file.read_text(encoding="utf-8"))

    assert template["kind"] == "BatchSandbox"
    [container] = template["spec"]["template"]["spec"]["containers"]
    # The server picks the container named "sandbox" for its securityContext.
    assert container["name"] == "sandbox"
    assert container["securityContext"] == {
        "capabilities": {"drop": list(launcher.SANDBOX_DROP_CAPABILITIES)}
    }
    assert "NET_RAW" in launcher.SANDBOX_DROP_CAPABILITIES


def _replace_on_conflict(template_sc: dict[str, Any], runtime_sc: dict[str, Any]) -> dict[str, Any]:
    """The pinned server's merge: nested dicts recurse, the runtime wins a leaf."""
    merged = dict(template_sc)
    for key, value in runtime_sc.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _replace_on_conflict(merged[key], value)
        else:
            merged[key] = value
    return merged


def test_the_template_drop_survives_the_network_policy_drop() -> None:
    """A network-policy sandbox's runtime drop must not replace the template's."""
    module = ModuleType("opensandbox_server.services.k8s.batchsandbox_provider")
    module._merge_security_context = _replace_on_conflict  # type: ignore[attr-defined]
    template = {"capabilities": {"drop": ["NET_RAW"]}}
    runtime = {"capabilities": {"drop": ["NET_ADMIN"], "add": ["SYS_ADMIN"]}, "seccompProfile": {"type": "Unconfined"}}
    # What the pinned merge does on its own: the template's drop is gone.
    assert module._merge_security_context(template, runtime)["capabilities"]["drop"] == ["NET_ADMIN"]

    launcher.drop_template_capabilities_with_the_runtime_ones(module)
    merged = module._merge_security_context(template, runtime)  # type: ignore[attr-defined]

    assert merged == {
        "capabilities": {"drop": ["NET_ADMIN", "NET_RAW"], "add": ["SYS_ADMIN"]},
        "seccompProfile": {"type": "Unconfined"},
    }
    # Without a runtime drop the template's stands alone; without a template
    # drop the merge is the server's own.
    assert module._merge_security_context(template, {})["capabilities"] == {"drop": ["NET_RAW"]}  # type: ignore[attr-defined]
    assert module._merge_security_context({}, runtime) == _replace_on_conflict({}, runtime)  # type: ignore[attr-defined]


def test_the_upstream_merge_keeps_the_template_drop_once_hooked() -> None:
    provider = _upstream("opensandbox_server.services.k8s.batchsandbox_provider")
    original = provider._merge_security_context
    template = {"capabilities": {"drop": ["NET_RAW"]}}
    runtime = {"capabilities": {"drop": ["NET_ADMIN"]}}
    try:
        assert original(template, runtime)["capabilities"]["drop"] == ["NET_ADMIN"]
        launcher.drop_template_capabilities_with_the_runtime_ones(provider)
        merged = provider._merge_security_context(template, runtime)
    finally:
        provider._merge_security_context = original
    assert merged["capabilities"]["drop"] == ["NET_ADMIN", "NET_RAW"]


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


# ── [secure_runtime]: the hardening every sandbox runs under ─────────────────


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


# ── [egress]: the sidecar a network policy would be enforced by ──────────────


def test_the_strict_mode_is_the_default(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`dns+nft`, deliberately not upstream's own `dns` default."""
    monkeypatch.setenv(launcher.EGRESS_IMAGE_ENV, "example/egress:1")
    assert launcher.egress()["mode"] == "dns+nft"


def test_the_name_only_mode_is_refused(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`dns` enforces no IP or CIDR rule, and every policy AstraBox writes has them.

    Under it the cloud-metadata deny, the Docker bridge deny and the address
    entries of a Limited allow list reach no packet, so a sandbox connecting to
    an address directly would pass all three.
    """
    monkeypatch.setenv(launcher.EGRESS_IMAGE_ENV, "example/egress:1")
    monkeypatch.setenv(launcher.EGRESS_MODE_ENV, "dns")
    with pytest.raises(launcher.SandboxServerConfigError) as caught:
        launcher.egress()
    assert "no IP or CIDR rule" in str(caught.value)


def test_ipv6_is_disabled_in_every_sandbox_namespace(
    kubernetes: None, state_dir: Path, tmp_path: Path
) -> None:
    """Written, not inherited: upstream's own default could change under a pin bump."""
    config = _upstream("opensandbox_server.config")
    path = tmp_path / "ipv6.toml"
    path.write_text(launcher.render_config_toml(launcher.config_document()), encoding="utf-8")
    assert "disable_ipv6 = true" in path.read_text(encoding="utf-8")
    assert config.load_config(path).egress.disable_ipv6 is True


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
