"""The container entry point keeps secrets out of its output and sandboxes off its own address.

Pinned here: a child's output is redacted before it reaches stdout or the kept
tail; the channel gateway is private and authenticated, and an external one must
use HTTPS; sandboxes are never pointed at this container and are denied the rest
of the bridge; another installation's edges are never taken for this one; and
the LiteLLM master key stays server-side and is never handed to sandboxes.

Real subprocesses are used wherever the behaviour under test IS process
behaviour, because a mocked Popen cannot fail the way a process fails. Nothing
here starts a container or a real sandbox server.
"""

from __future__ import annotations

import ipaddress
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from astrabox.deploy import onebox

#: The sandbox reach mode AstraBox reads; the entry point must not set it.
RELAY_ENV = "ASTRABOX_SANDBOX_ENDPOINT_VIA_SERVER_PROXY"

_ALL_ENV = (
    onebox.BACKEND_ENV,
    onebox.BASE_URL_ENV,
    RELAY_ENV,
    onebox.START_TIMEOUT_ENV,
    onebox.MODEL_PROVIDER_ENV,
    onebox.LITELLM_MASTER_KEY_ENV,
    onebox.CREDENTIAL_VAULT_ENV,
    onebox.EGRESS_DNS_UPSTREAM_ENV,
    onebox.EGRESS_DNS_UPSTREAM_DEFAULT_ENV,
    onebox.GATEWAY_DNS_ADDRESS_ENV,
    onebox.SANDBOX_EDGE_SERVICE_ENV,
    onebox.SANDBOX_DNS_EDGE_SERVICE_ENV,
    onebox.SANDBOX_EDGE_CALLBACK_PORT_ENV,
    onebox.SANDBOX_EDGE_NETWORK_ENV,
    onebox.EGRESS_DENY_CIDRS_ENV,
    onebox.MCP_PROXY_BASE_URL_ENV,
    onebox.ANTHROPIC_API_KEY_ENV,
    onebox.ANTHROPIC_AUTH_TOKEN_ENV,
    onebox.ANTHROPIC_BASE_URL_ENV,
    onebox.ANTHROPIC_MODEL_ENV,
    onebox.ASTRABOX_DB_URL_ENV,
    onebox.ASTRABOX_DB_PASSWORD_FILE_ENV,
    onebox.ASTRABOX_DB_HOST_ENV,
    onebox.ASTRABOX_DB_PORT_ENV,
    onebox.LITELLM_DATABASE_URL_ENV,
    onebox.LITELLM_DATABASE_PASSWORD_FILE_ENV,
    onebox.LITELLM_DATABASE_HOST_ENV,
    onebox.LITELLM_DATABASE_PORT_ENV,
    onebox.CHANNEL_GATEWAY_BASE_URL_ENV,
    onebox.CHANNEL_GATEWAY_TOKEN_ENV,
    onebox.CHANNEL_GATEWAY_HOST_ENV,
    onebox.CHANNEL_GATEWAY_PORT_ENV,
    onebox.CHANNEL_GATEWAY_MANIFEST_ENV,
    "DEEPSEEK_API_KEY",
    onebox.TITLE_MODEL_NAME_ENV,
    onebox.TITLE_MODEL_BASE_URL_ENV,
    "ASTRABOX_LITELLM_BASE_URL",
    "ASTRABOX_LITELLM_API_KEY",
    "ASTRABOX_STATE_DIR",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from astrabox.config.settings import get_settings

    get_settings.cache_clear()
    original = {name: os.environ.get(name) for name in _ALL_ENV}
    for name in _ALL_ENV:
        monkeypatch.delenv(name, raising=False)
    # The sandbox-server-shape cases isolate that leg: an external gateway
    # address keeps LiteLLM startup outside their frame.
    monkeypatch.setenv("ASTRABOX_LITELLM_BASE_URL", "http://external-gateway.test:4000")
    monkeypatch.setenv(onebox.LITELLM_API_KEY_ENV_NAME, "sk-external-scoped")
    monkeypatch.setenv(
        onebox.CHANNEL_GATEWAY_BASE_URL_ENV,
        "https://channels.example.test",
    )
    monkeypatch.setenv(onebox.CHANNEL_GATEWAY_TOKEN_ENV, "c" * 32)
    yield

    # Several functions under test deliberately write straight to os.environ.
    # When a key was absent above, monkeypatch.delenv had nothing to record and
    # therefore cannot undo a later raw write. Restore the process environment
    # explicitly so this module cannot change the posture of tests collected
    # after it in the same pytest process.
    for name, value in original.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 3. bringing the server up
# ---------------------------------------------------------------------------


def _python_child(script: str) -> onebox._Child:
    """A real child process running ``script``, with its output pumped."""
    return onebox._Child("sandbox-server", (sys.executable, "-c", script), prefix_output=True)


@pytest.fixture
def child_reaper() -> Iterator[list[onebox._Child]]:
    """Guarantee every spawned child is gone when the test ends."""
    spawned: list[onebox._Child] = []
    yield spawned
    for child in spawned:
        child.stop()


# ---------------------------------------------------------------------------
# 5. supervision
# ---------------------------------------------------------------------------


def _await_line(child: onebox._Child, needle: str, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in child.output_tail():
            return
        time.sleep(0.05)
    raise AssertionError(f"{child.name} never printed {needle!r}")


def test_pumped_third_party_output_is_redacted_before_stdout_and_tail(
    monkeypatch: pytest.MonkeyPatch,
    child_reaper: list[onebox._Child],
    capsys: pytest.CaptureFixture[str],
) -> None:
    inherited_secret = "provider-credential-value-12345"
    rejected_key = "sk-test-redaction-abcdef1234567890"
    key_hash = "a" * 47 + "2" + "b" * 16
    monkeypatch.setenv("ACME_ACCESS_TOKEN", inherited_secret)
    child = _python_child(
        "import time\n"
        f"print('opaque {inherited_secret}', flush=True)\n"
        f"print('Received API Key = {rejected_key}', flush=True)\n"
        f"print('hash={key_hash}', flush=True)\n"
        "time.sleep(120)\n"
    )
    child_reaper.append(child)
    _await_line(child, "hash=[redacted]")

    tail = child.output_tail()
    stdout = capsys.readouterr().out
    for secret in (inherited_secret, rejected_key, key_hash):
        assert secret not in tail
        assert secret not in stdout
    assert "opaque [redacted]" in tail
    assert "Received API Key = [redacted]" in tail


def test_the_relayed_deepseek_harness_launch_token_is_redacted(
    child_reaper: list[onebox._Child],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With sandbox endpoints relayed through the lifecycle server, the DeepSeek
    Harness adapter's token exchange is a request line in that server's access
    log. The line below is what opensandbox-server 0.2.3, started as onebox
    starts it, printed for that request, colour codes included. The token here
    is synthetic; the request shape matches the harness exchange."""

    token = "onebox-log-redaction-fixture-token-0000000000"
    line = (
        "\x1b[32mINFO\x1b[0m:     2026-09-23 10:05:55+0000 [-] uvicorn.access: "
        '127.0.0.1:54544 - "GET /sandboxes/0d4c2b1e-relay-probe/proxy/44780/'
        f'?token={token} HTTP/1.1" 404'
    )
    child = _python_child(f"import time\nprint({line!r}, flush=True)\ntime.sleep(120)\n")
    child_reaper.append(child)
    _await_line(child, "uvicorn.access")

    tail = child.output_tail()
    stdout = capsys.readouterr().out
    assert token not in tail
    assert token not in stdout
    assert "/proxy/44780/?token=[redacted] HTTP/1.1\" 404" in tail


# ---------------------------------------------------------------------------
# 5. the embedded channel gateway leg
# ---------------------------------------------------------------------------


def test_embedded_channel_gateway_wiring_is_private_and_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(onebox.CHANNEL_GATEWAY_BASE_URL_ENV, raising=False)
    monkeypatch.delenv(onebox.CHANNEL_GATEWAY_TOKEN_ENV, raising=False)
    monkeypatch.setenv(onebox.CHANNEL_GATEWAY_PORT_ENV, "18765")

    assert onebox.ensure_channel_gateway_wiring() == "http://127.0.0.1:18765"
    assert len(os.environ[onebox.CHANNEL_GATEWAY_TOKEN_ENV]) >= 32
    assert (
        os.environ[onebox.CHANNEL_GATEWAY_MANIFEST_ENV]
        == onebox.CHANNEL_GATEWAY_MANIFEST_PATH
    )


def test_external_channel_gateway_refuses_plaintext_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        onebox.CHANNEL_GATEWAY_BASE_URL_ENV,
        "http://channels.example.test",
    )
    with pytest.raises(onebox.OneBoxError, match="must use HTTPS"):
        onebox.ensure_channel_gateway_wiring()


# ---------------------------------------------------------------------------
# Embedded LiteLLM gateway
# ---------------------------------------------------------------------------


def test_private_gateway_dns_never_points_sandboxes_at_this_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sandboxes may reach the address the gateway name resolves to; this
    # server's own address would open every port it listens on to them.
    monkeypatch.delenv(onebox.GATEWAY_DNS_ADDRESS_ENV, raising=False)
    with pytest.raises(onebox.OneBoxError, match=onebox.GATEWAY_DNS_ADDRESS_ENV):
        onebox.ensure_gateway_dns_wiring()
    assert onebox.GATEWAY_DNS_ADDRESS_ENV not in os.environ
    assert onebox.EGRESS_DNS_UPSTREAM_ENV not in os.environ


class _FakeContainer:
    def __init__(self, name: str, networks: dict[str, str], labels: dict[str, str]) -> None:
        self.name = name
        self.attrs: dict[str, Any] = {
            "Config": {"Labels": labels},
            "NetworkSettings": {
                "Networks": {net: {"IPAddress": ip} for net, ip in networks.items()}
            },
        }


class _FakeNetwork:
    def __init__(self, name: str, labels: dict[str, str]) -> None:
        self.name = name
        self.labels = labels
        self.connected: list[str] = []

    def connect(self, container: _FakeContainer) -> None:
        self.connected.append(container.name)
        container.attrs["NetworkSettings"]["Networks"][self.name] = {"IPAddress": "10.9.0.9"}


def _labelled(labels: dict[str, str], wanted: list[str]) -> bool:
    """Docker's label filter: every ``key=value`` given must be on the resource."""
    return all(labels.get(key) == value for key, _, value in (item.partition("=") for item in wanted))


_COMPOSE = {
    "server": {"com.docker.compose.project": "proj"},
    "network": {
        "com.docker.compose.project": "proj",
        "com.docker.compose.network": "sandbox-edges",
    },
    "edge": lambda service: {
        "com.docker.compose.project": "proj",
        "com.docker.compose.service": service,
    },
}


class _FakeDocker:
    """The slice of docker-py onebox uses: labels, networks, the bridge IPAM."""

    def __init__(
        self,
        *,
        server_networks: tuple[str, ...],
        edge_networks: dict[str, tuple[str, ...]],
        bridge_subnets: tuple[str, ...] = ("172.17.0.0/16",),
        labels: dict[str, Any] = _COMPOSE,
    ) -> None:
        self.private = _FakeNetwork("proj_sandbox-edges", labels["network"])
        self.server = _FakeContainer(
            "server", {net: "10.9.0.2" for net in server_networks}, labels["server"]
        )
        self.edges = {
            service: _FakeContainer(
                service,
                {"bridge": "172.17.0.23", **{net: "10.9.0.3" for net in nets}},
                labels["edge"](service),
            )
            for service, nets in edge_networks.items()
        }
        bridge = SimpleNamespace(
            attrs={"IPAM": {"Config": [{"Subnet": subnet} for subnet in bridge_subnets]}}
        )
        self.containers = SimpleNamespace(
            get=lambda _hostname: self.server, list=self._list_containers
        )
        self.networks = SimpleNamespace(
            list=self._list_networks,
            get=lambda name: bridge if name == "bridge" else pytest.fail(name),
        )

    def _list_containers(self, *, filters: dict[str, Any]) -> list[_FakeContainer]:
        assert filters["status"] == "running"
        return [
            edge
            for edge in self.edges.values()
            if _labelled(edge.attrs["Config"]["Labels"], filters["label"])
        ]

    def _list_networks(self, *, filters: dict[str, Any]) -> list[_FakeNetwork]:
        return [self.private] if _labelled(self.private.labels, filters["label"]) else []

    def close(self) -> None:
        pass


def _install_fake_docker(monkeypatch: pytest.MonkeyPatch, client: _FakeDocker) -> None:
    monkeypatch.setitem(sys.modules, "docker", SimpleNamespace(from_env=lambda: client))


_ALL_IN_ONE = {
    "server": {},
    "network": {"astrabox.all-in-one.install": "abc123", "astrabox.all-in-one.role": "sandbox-edges"},
    "edge": lambda role: {"astrabox.all-in-one.install": "abc123", "astrabox.all-in-one.role": role},
}


def test_another_installations_edges_are_not_taken_for_this_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(onebox.SANDBOX_EDGE_SERVICE_ENV, "sandbox-edge")
    monkeypatch.setenv(onebox.SANDBOX_EDGE_NETWORK_ENV, "sandbox-edges")
    client = _FakeDocker(
        server_networks=("proj_sandbox-edges",),
        edge_networks={"sandbox-edge": ()},
        labels=_ALL_IN_ONE,
    )
    _install_fake_docker(monkeypatch, client)
    other = onebox.EdgeOwner(
        labels=("astrabox.all-in-one.install=def456",),
        service_key="astrabox.all-in-one.role",
        network_key="astrabox.all-in-one.role",
        description="all-in-one installation def456",
    )

    with pytest.raises(onebox.OneBoxError, match="found 0"):
        onebox.ensure_sandbox_edge_network(other)
    assert client.private.connected == []


def test_bridge_isolation_denies_the_whole_bridge_except_the_sandbox_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every other bridge address (gateway, sibling sandboxes, DNS edge) is denied."""
    client = _FakeDocker(server_networks=(), edge_networks={})
    _install_fake_docker(monkeypatch, client)

    value = onebox.ensure_sandbox_bridge_isolation("172.17.0.23")

    assert os.environ[onebox.EGRESS_DENY_CIDRS_ENV] == value
    denied = [ipaddress.ip_network(item) for item in value.split(",")]
    assert sum(network.num_addresses for network in denied) == 2**16 - 1
    assert not any(ipaddress.ip_address("172.17.0.23") in net for net in denied)
    for address in ("172.17.0.1", "172.17.0.2", "172.17.0.22", "172.17.0.24", "172.17.255.255"):
        assert any(ipaddress.ip_address(address) in net for net in denied), address


def test_master_key_is_generated_persisted_and_kept_server_side(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.delenv(onebox.LITELLM_MASTER_KEY_ENV, raising=False)
    monkeypatch.delenv("ASTRABOX_LITELLM_API_KEY", raising=False)
    key = onebox.ensure_litellm_master_key()
    key_file = tmp_path / onebox.LITELLM_KEY_FILENAME
    assert key_file.read_text().strip() == key
    assert (key_file.stat().st_mode & 0o777) == 0o600
    # Exported for the proxy and server-side management only. Sandboxes derive
    # a separate LiteLLM virtual key after the gateway is healthy.
    assert os.environ[onebox.LITELLM_MASTER_KEY_ENV] == key
    assert "ASTRABOX_LITELLM_API_KEY" not in os.environ
    # Idempotent across restarts: the persisted key is reused, not regenerated.
    monkeypatch.delenv(onebox.LITELLM_MASTER_KEY_ENV, raising=False)
    assert onebox.ensure_litellm_master_key() == key


def test_operator_master_key_wins_and_is_not_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv(onebox.LITELLM_MASTER_KEY_ENV, "sk-operator-owned")
    monkeypatch.setenv("ASTRABOX_LITELLM_API_KEY", "sk-operator-sandbox-facing")
    assert onebox.ensure_litellm_master_key() == "sk-operator-owned"
    assert not (tmp_path / onebox.LITELLM_KEY_FILENAME).exists()
    # A non-empty operator sandbox-facing value stays.
    assert os.environ["ASTRABOX_LITELLM_API_KEY"] == "sk-operator-sandbox-facing"


def test_empty_compose_inference_key_is_not_replaced_by_the_master_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.delenv(onebox.LITELLM_MASTER_KEY_ENV, raising=False)
    # Compose interpolation produces this exact shape when the operator leaves
    # the optional override unset: the name exists, but its value is empty.
    monkeypatch.setenv(onebox.LITELLM_API_KEY_ENV_NAME, "")

    key = onebox.ensure_litellm_master_key()

    assert key.startswith("sk-astrabox-")
    assert os.environ[onebox.LITELLM_API_KEY_ENV_NAME] == ""
