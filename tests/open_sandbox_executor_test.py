"""OpenSandbox's box-level create, connect and endpoint contracts that guard the box.

Hand-rolled SDK fakes pin the provider translation where it protects a box: the
egress DNS upstream is the host's and never the caller's, a reattach proves the
permission level again, an endpoint behind secure access is a signed URL, and
failure text never quotes a secret.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from opensandbox.exceptions import SandboxApiException

from astrabox.common.utils.errors import APIError

import astrabox.providers.open_sandbox.executor as executor_module
import astrabox.providers.open_sandbox.sandbox as sandbox_module
from astrabox.providers.open_sandbox.sandbox import (
    OpenSandboxHandle,
    OpenSandboxSandboxProvider,
)
from astrabox.seams.sandbox import (
    SandboxCreateSpec,
    SandboxNetworkPolicy,
)
from tests._execd_fakes import ScriptedExecdCommands

_BASE_URL = "http://opensandbox.test:8080"


@pytest.fixture(autouse=True)
def _lifecycle_base_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_OPENAPI_BASE_URL", _BASE_URL)
    # Inventory recovery has its own provider-wire coverage. These executor
    # tests pin create/setup behavior and begin from a proven assignment miss.
    monkeypatch.setattr(
        OpenSandboxSandboxProvider,
        "find_sandbox_by_assignment",
        AsyncMock(return_value=None),
    )

# ── SDK fakes ────────────────────────────────────────────────────────────────


class _FakeFiles:
    def __init__(self, *, create_dirs_error: Exception | None = None) -> None:
        self.created: list[list[Any]] = []
        self._create_dirs_error = create_dirs_error

    async def create_directories(self, entries: list[Any]) -> None:
        self.created.append(list(entries))
        if self._create_dirs_error is not None:
            raise self._create_dirs_error


class _FakeSdkSandbox:
    def __init__(
        self,
        *,
        endpoint: str | None = None,
        endpoint_headers: dict[str, str] | None = None,
        signed_endpoint: str | None = None,
        signed_endpoint_headers: dict[str, str] | None = None,
        commands: ScriptedExecdCommands | None = None,
        files: _FakeFiles | None = None,
    ) -> None:
        self.id = "sb-1"
        self.commands = commands or ScriptedExecdCommands()
        self.files = files or _FakeFiles()
        self.kill_calls = 0
        self.close_calls = 0
        self._endpoint = endpoint
        self._endpoint_headers = dict(endpoint_headers or {})
        self._signed_endpoint = signed_endpoint
        self._signed_endpoint_headers = dict(signed_endpoint_headers or {})
        self.signed_endpoint_calls: list[tuple[int, int]] = []

    async def kill(self) -> None:
        self.kill_calls += 1

    async def close(self) -> None:
        self.close_calls += 1

    async def get_endpoint(self, port: int) -> Any:
        return SimpleNamespace(
            endpoint=self._endpoint or f"box.test:{port}",
            headers=dict(self._endpoint_headers),
        )

    async def get_signed_endpoint(self, port: int, expires: int) -> Any:
        self.signed_endpoint_calls.append((int(port), int(expires)))
        return SimpleNamespace(
            endpoint=self._signed_endpoint or f"box.test:{port}?signed=1",
            headers=dict(self._signed_endpoint_headers),
        )


# ── the create's kwargs, frozen ──────────────────────────────────────────────
#
# Asked of `create_sandbox`, the one place this backend builds a box. The
# executor answers which box a session gets — re-attach, a place in an Agent's
# box, a pool borrow, or a create — and delegates the building, so the shape of
# the SDK call is the provider's to keep.


def _create_spec(**overrides: Any) -> SandboxCreateSpec:
    # Every real create carries an assignment — it is the durable identity of
    # one create attempt, and the provider refuses a spec without one — so the
    # default is here and a test names its own only when the assignment is what
    # the test is about.
    fields: dict[str, Any] = {
        "session_id": "s1",
        "assignment_id": "assignment-s1",
        "resource_limits": {"cpu": "4", "memory": "4Gi"},
        "resource_requests": {"cpu": "200m", "memory": "768Mi"},
    }
    fields.update(overrides)
    return SandboxCreateSpec(**fields)


async def _build(spec: SandboxCreateSpec) -> Any:
    return await OpenSandboxSandboxProvider().create_sandbox(spec)


async def test_host_configured_dns_upstream_goes_only_to_the_egress_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM", "172.17.0.1:5353")
    fake_sdk = _FakeSdkSandbox()
    spec = _create_spec(
        session_id="sess-dns",
        assignment_id="assignment-sess-dns",
        network_policy=SandboxNetworkPolicy(mode="limited"),
    )
    with mock.patch.object(executor_module, "Sandbox") as sandbox_cls:
        sandbox_cls.create = AsyncMock(return_value=fake_sdk)
        await _build(spec)

    env = sandbox_cls.create.await_args.kwargs["env"]
    assert env["OPENSANDBOX_EGRESS_DNS_UPSTREAM"] == "172.17.0.1:5353"


async def test_caller_env_cannot_choose_the_sandbox_egress_dns_upstream() -> None:
    """Refused, not quietly dropped.

    These names decide where the box's traffic goes. A spec that names one is
    asking for a boundary the host did not grant, and a create that answered by
    ignoring the field would hand back a box whose egress is not what its
    caller was told.
    """
    fake_sdk = _FakeSdkSandbox()
    spec = _create_spec(
        session_id="sess-untrusted-dns",
        env={"OPENSANDBOX_EGRESS_DNS_UPSTREAM": "203.0.113.7:53"},
        assignment_id="assignment-sess-untrusted-dns",
        network_policy=SandboxNetworkPolicy(mode="limited"),
    )
    with mock.patch.object(executor_module, "Sandbox") as sandbox_cls:
        sandbox_cls.create = AsyncMock(return_value=fake_sdk)
        with pytest.raises(APIError) as caught:
            await _build(spec)

    assert caught.value.code == "SANDBOX_CONFIG_INVALID"
    assert "OPENSANDBOX_EGRESS_DNS_UPSTREAM" in caught.value.message
    sandbox_cls.create.assert_not_awaited()


# ── provider connect and platform permission proof ──────────────────────────


async def test_existing_sandbox_id_reproves_the_requested_permission_level() -> None:
    fake_sdk = _FakeSdkSandbox()
    handle = OpenSandboxHandle(fake_sdk)  # type: ignore[arg-type]
    manager = SimpleNamespace(connect_sandbox_only=AsyncMock(return_value=handle))
    prove = AsyncMock(
        return_value=SimpleNamespace(available=True, detail="isolation available")
    )
    backend = SimpleNamespace(
        name="open_sandbox",
        supported_permission_levels=("default", "advanced"),
        read_isolation_capability=prove,
    )
    with mock.patch(
        "astrabox.seams.sandbox.sandbox_for_sandbox",
        return_value=backend,
    ):
        from astrabox.core.service.orchestrator.engine.provisioning import (
            connect_engine_sandbox,
        )

        connected = await connect_engine_sandbox(
            manager,
            sandbox_id="sb-9",
            template=SimpleNamespace(sandbox_permission_level="advanced"),
        )

    assert connected is handle
    prove.assert_awaited_once_with("sb-9")


async def test_existing_sandbox_id_closes_the_handle_when_permission_proof_fails() -> None:
    fake_sdk = _FakeSdkSandbox()
    handle = OpenSandboxHandle(fake_sdk)  # type: ignore[arg-type]
    manager = SimpleNamespace(connect_sandbox_only=AsyncMock(return_value=handle))
    backend = SimpleNamespace(
        name="open_sandbox",
        supported_permission_levels=("default", "advanced"),
        read_isolation_capability=AsyncMock(
            return_value=SimpleNamespace(available=False, detail="advanced was not proved")
        ),
    )
    with mock.patch(
        "astrabox.seams.sandbox.sandbox_for_sandbox",
        return_value=backend,
    ):
        from astrabox.core.service.orchestrator.engine.provisioning import (
            connect_engine_sandbox,
        )

        with pytest.raises(APIError, match="advanced was not proved"):
            await connect_engine_sandbox(
                manager,
                sandbox_id="sb-9",
                template=SimpleNamespace(sandbox_permission_level="advanced"),
            )

    assert fake_sdk.close_calls == 1


# ── platform endpoint resolution ────────────────────────────────────────────


async def test_endpoint_resolution_mints_a_signed_uri_when_secure_access_requires_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator.engine.provisioning import (
        resolve_sandbox_websocket_endpoint,
    )

    monkeypatch.setenv("ASTRABOX_SANDBOX_ENDPOINT_URL_TTL_SECONDS", "600")
    signature = "runner-bearer-secret"
    routing_secret = "routing-header-secret"
    raw_endpoint = "box.test:9001"
    fake_sdk = _FakeSdkSandbox(
        endpoint=raw_endpoint,
        endpoint_headers={"OpenSandbox-Secure-Access": routing_secret},
        signed_endpoint=f"{raw_endpoint}?signature={signature}",
    )
    handle = OpenSandboxHandle(fake_sdk)  # type: ignore[arg-type]
    earliest_expiry = int(time.time()) + 599
    uri = await resolve_sandbox_websocket_endpoint(handle, 9001)
    latest_expiry = int(time.time()) + 601

    assert uri == f"ws://{raw_endpoint}?signature={signature}"
    assert len(fake_sdk.signed_endpoint_calls) == 1
    port, expires = fake_sdk.signed_endpoint_calls[0]
    assert port == 9001
    assert earliest_expiry <= expires <= latest_expiry
    assert routing_secret not in uri


# ── key hygiene on the executor's lifecycle face ─────────────────────────────


async def test_create_failure_text_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The key-hygiene invariant covers EVERY lifecycle-face exception exit, not
    # only the provider's: a create failure flows verbatim into the runtime's
    # start-failure log line and its 502 message.
    key = "osk-secret-key-1234567890"
    monkeypatch.setenv("ASTRABOX_SANDBOX_API_KEY", key)
    monkeypatch.setenv("ASTRABOX_ALLOW_PLAINTEXT_SANDBOX_API_KEY", "1")
    with mock.patch.object(executor_module, "Sandbox") as sandbox_cls:
        sandbox_cls.create = AsyncMock(side_effect=RuntimeError(f"denied for key {key}"))
        with pytest.raises(RuntimeError) as err:
            await _build(_create_spec())
    assert key not in str(err.value)
    assert key not in repr(err.value)
    assert "***" in str(err.value)


async def test_attach_precheck_failure_text_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "osk-secret-key-1234567890"
    monkeypatch.setenv("ASTRABOX_SANDBOX_API_KEY", key)
    monkeypatch.setenv("ASTRABOX_ALLOW_PLAINTEXT_SANDBOX_API_KEY", "1")
    manager = SimpleNamespace(
        get_sandbox_info=AsyncMock(side_effect=RuntimeError(f"denied for key {key}")),
        close=AsyncMock(),
    )
    with mock.patch.object(sandbox_module, "SandboxManager") as manager_cls:
        manager_cls.create = AsyncMock(return_value=manager)
        with pytest.raises(APIError) as err:
            await OpenSandboxSandboxProvider().connect("sb-9")
    manager.close.assert_awaited_once()
    assert err.value.code == "AGENT_RUNTIME_ERROR"
    assert key not in str(err.value)
    assert "***" in str(err.value)


@pytest.mark.parametrize("signed", [False, True], ids=["endpoint", "signed-endpoint"])
async def test_startup_endpoint_failure_text_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
    signed: bool,
) -> None:
    from astrabox.core.service.orchestrator.engine.provisioning import (
        resolve_sandbox_websocket_endpoint,
    )

    # This is the endpoint seam every engine startup uses. Both the ordinary
    # endpoint lookup and Secure Access signing are lifecycle requests whose
    # SDK exceptions can quote the server's response body into the runtime 502.
    key = "osk-secret-key-1234567890"
    monkeypatch.setenv("ASTRABOX_SANDBOX_API_KEY", key)
    monkeypatch.setenv("ASTRABOX_ALLOW_PLAINTEXT_SANDBOX_API_KEY", "1")
    sdk = _FakeSdkSandbox(
        endpoint_headers={"X-OpenSandbox-Route": "required"} if signed else None,
    )
    failing_method = "get_signed_endpoint" if signed else "get_endpoint"
    monkeypatch.setattr(
        sdk,
        failing_method,
        AsyncMock(
            side_effect=SandboxApiException(
                f"unauthorized for key {key}", status_code=401
            )
        ),
    )
    handle = OpenSandboxHandle(sdk)  # type: ignore[arg-type]

    with pytest.raises(APIError) as err:
        await resolve_sandbox_websocket_endpoint(handle, 9001)

    assert err.value.code == "AGENT_RUNTIME_ERROR"
    assert key not in str(err.value)
    assert key not in repr(err.value)
    assert "***" in str(err.value)
