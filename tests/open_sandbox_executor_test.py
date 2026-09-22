"""OpenSandbox's box-level create, connect, and endpoint contracts.

Hand-rolled SDK fakes pin the provider translation: explicit image entrypoint,
lease-sized TTL, caller-owned environment, durable reverse-lookup metadata,
command and service readiness, cleanup, and secret hygiene. Session placement
and engine startup are deliberately absent: those decisions belong to the
platform, while this module tests only the complete capability the supplier
exposes beneath that boundary.
"""

from __future__ import annotations

import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock

import httpx
import pytest
from opensandbox.exceptions import SandboxApiException

from astrabox.common.utils.errors import APIError
from astrabox.config.release_images import release_image
from astrabox.core.service.orchestrator.stream_errors import is_sandbox_gone_error

import astrabox.providers.open_sandbox.executor as executor_module
import astrabox.providers.open_sandbox.sandbox as sandbox_module
from astrabox.providers.open_sandbox.sandbox import (
    OpenSandboxHandle,
    OpenSandboxSandboxProvider,
)
from astrabox.providers.sandbox_image import AGENT_IMAGE_COMPONENT
from astrabox.seams.sandbox import SandboxCreateSpec, SandboxNetworkPolicy

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


def _execution(*, exit_code: int | None, stdout: str = "", stderr: str = "") -> Any:
    from opensandbox.models.execd import Execution, ExecutionLogs, OutputMessage

    logs = ExecutionLogs(
        stdout=([OutputMessage(text=stdout, timestamp=0, is_error=False)] if stdout else []),
        stderr=([OutputMessage(text=stderr, timestamp=0, is_error=True)] if stderr else []),
    )
    return Execution(exit_code=exit_code, logs=logs, error=None)


class _FakeCommands:
    """Scripted execd: results keyed by substring of the command, else default."""

    def __init__(
        self,
        *,
        by_substring: dict[str, Any] | None = None,
        default: Any = None,
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._by_substring = by_substring or {}
        self._default = default

    async def run(self, command: str, *, opts: Any = None, handlers: Any = None) -> Any:
        self.calls.append((command, opts))
        for needle, execution in self._by_substring.items():
            if needle in command:
                return execution
        return self._default if self._default is not None else _execution(exit_code=0)


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
        commands: _FakeCommands | None = None,
        files: _FakeFiles | None = None,
    ) -> None:
        self.id = "sb-1"
        self.commands = commands or _FakeCommands()
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


async def test_create_kwargs_pin_the_activation_design(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_LEASE_SECONDS", "7777")
    monkeypatch.setenv("ASTRABOX_SANDBOX_READY_TIMEOUT_SECONDS", "55")
    for image_setting in ("ASTRABOX_AGENT_IMAGE", "ASTRABOX_IMAGE_PREFIX", "ASTRABOX_IMAGE_TAG"):
        monkeypatch.delenv(image_setting, raising=False)
    fake_sdk = _FakeSdkSandbox()
    spec = _create_spec(
        session_id="sess-1",
        assignment_id="assignment-sess-1",
        cwd="/home/u/workspace",
        env={"IS_SANDBOX": "1", "DISABLE_BROWSER": "true", "ANTHROPIC_MODEL": "m1"},
    )
    with mock.patch.object(executor_module, "Sandbox") as sandbox_cls:
        sandbox_cls.create = AsyncMock(return_value=fake_sdk)
        handle = await _build(spec)

    kwargs = sandbox_cls.create.await_args.kwargs
    # A spec that names no image runs this release's published Claude Code image.
    assert kwargs["image"] == release_image(AGENT_IMAGE_COMPONENT)
    # The SDK swaps a falsy entrypoint for ["tail","-f","/dev/null"]; the AIO
    # boot contract must be explicit.
    assert kwargs["entrypoint"] == ["/opt/astrabox/boot.sh"]
    # TTL is the deployment lease, never the SDK's 600 s default.
    assert kwargs["timeout"] == timedelta(seconds=7777)
    assert kwargs["ready_timeout"] == timedelta(seconds=55)
    env = kwargs["env"]
    # Verbatim: the seam defines this field as the boot environment, so a
    # provider that added or dropped a name would make the spec a lie.
    assert env["IS_SANDBOX"] == "1"
    assert env["DISABLE_BROWSER"] == "true"
    assert env["ANTHROPIC_MODEL"] == "m1"
    # Unfenced backend: a set revision would 502 the owner-bind guard.
    assert "ASTRABOX_SIDECAR_REVISION" not in env
    assert kwargs["metadata"] == {
        "astrabox.session-id": "sess-1",
        "astrabox.managed-by": "astrabox",
        "astrabox.assignment-id": "assignment-sess-1",
    }
    assert kwargs["connection_config"] is not None
    assert kwargs["resource"] == {"cpu": "4", "memory": "4Gi"}
    assert kwargs["resource_requests"] == {"cpu": "200m", "memory": "768Mi"}
    for absent in ("publish_ports", "death_callback_url"):
        assert absent not in kwargs
    assert handle.sandbox_id == "sb-1"


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


async def test_cwd_is_precreated_with_the_sdk_write_entry() -> None:
    fake_sdk = _FakeSdkSandbox()
    with mock.patch.object(executor_module, "Sandbox") as sandbox_cls:
        sandbox_cls.create = AsyncMock(return_value=fake_sdk)
        await _build(_create_spec(cwd="/home/u/workspace"))
    (entries,) = fake_sdk.files.created
    (entry,) = entries
    assert entry.path == "/home/u/workspace"
    assert entry.mode == 755


async def test_no_cwd_skips_the_predir_step() -> None:
    fake_sdk = _FakeSdkSandbox()
    with mock.patch.object(executor_module, "Sandbox") as sandbox_cls:
        sandbox_cls.create = AsyncMock(return_value=fake_sdk)
        await _build(_create_spec())
    assert fake_sdk.files.created == []


@pytest.mark.parametrize(
    "transient_error",
    [
        "empty sse stream",
        (
            "peer closed connection without sending complete message body "
            "(incomplete chunked read)"
        ),
        # One probe's two-second timeout is transient within the overall
        # readiness budget; it does not establish a permanent startup failure.
        (
            "open_sandbox execd command did not complete within 2s: "
            "'sh -lc :'"
        ),
    ],
)
async def test_create_waits_for_execd_command_stream_without_retrying_user_work(
    monkeypatch: pytest.MonkeyPatch,
    transient_error: str,
) -> None:
    monkeypatch.setattr(executor_module, "_EXECD_COMMAND_READY_POLL_SECONDS", 0)
    fake_sdk = _FakeSdkSandbox()
    fake_sdk.commands.run = AsyncMock(
        side_effect=[
            RuntimeError(transient_error),
            _execution(exit_code=0),
        ]
    )
    with mock.patch.object(executor_module, "Sandbox") as sandbox_cls:
        sandbox_cls.create = AsyncMock(return_value=fake_sdk)
        await _build(_create_spec(requires_command_channel=True))

    assert fake_sdk.commands.run.await_count == 2
    assert [call.args for call in fake_sdk.commands.run.await_args_list] == [
        ("sh -lc :",),
        ("sh -lc :",),
    ]


async def test_a_create_that_asks_for_no_command_channel_does_not_prove_one() -> None:
    """The proof is asked for, never inferred from the readiness port.

    They answer different questions — whether the image's own service is
    serving, and whether the platform can run a command in the box at all — and
    a box can have either without the other. Deriving one from the other gives
    every caller that waits on a port a proof it never asked for, and every
    caller that does not, none.
    """
    fake_sdk = _FakeSdkSandbox()
    fake_sdk.commands.run = AsyncMock(return_value=_execution(exit_code=0))
    with mock.patch.object(executor_module, "Sandbox") as sandbox_cls:
        sandbox_cls.create = AsyncMock(return_value=fake_sdk)
        await _build(_create_spec(requires_command_channel=False))

    assert fake_sdk.commands.run.await_count == 0


async def test_execd_command_readiness_does_not_retry_an_unknown_failure() -> None:
    fake_sdk = _FakeSdkSandbox()
    fake_sdk.commands.run = AsyncMock(side_effect=RuntimeError("permission denied"))

    with pytest.raises(RuntimeError, match="readiness probe failed.*permission denied"):
        await executor_module._wait_for_execd_command_stream(
            OpenSandboxHandle(fake_sdk),
            session_id="s1",
        )

    fake_sdk.commands.run.assert_awaited_once()


async def test_execd_command_readiness_retries_its_bounded_probe_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(executor_module, "_EXECD_COMMAND_READY_POLL_SECONDS", 0)
    fake_sdk = _FakeSdkSandbox()
    fake_sdk.commands.run = AsyncMock(
        side_effect=[
            TimeoutError("command stream did not settle in this attempt"),
            _execution(exit_code=0),
        ]
    )

    await executor_module._wait_for_execd_command_stream(
        OpenSandboxHandle(fake_sdk),
        session_id="s1",
    )

    assert fake_sdk.commands.run.await_count == 2
    assert [call.args for call in fake_sdk.commands.run.await_args_list] == [
        ("sh -lc :",),
        ("sh -lc :",),
    ]


class _FakeLifecycleManager:
    """The by-id lifecycle face the create guard destroys through.

    The guard does NOT use the live handle's own ``kill()``: that reports only
    that an SDK call returned, and a destruction nothing observed twice may not
    license forgetting the box. It goes through the provider, which deletes and
    then asks the control plane whether the sandbox is still there.
    """

    def __init__(self, *, gone_after_kill: bool = True) -> None:
        self.killed: list[str] = []
        self._gone_after_kill = gone_after_kill

    @classmethod
    def factory(cls, instance: "_FakeLifecycleManager") -> Any:
        async def create(**_kwargs: Any) -> "_FakeLifecycleManager":
            return instance

        return SimpleNamespace(create=create)

    async def kill_sandbox(self, sandbox_id: str) -> None:
        self.killed.append(sandbox_id)

    async def get_sandbox_info(self, sandbox_id: str) -> Any:
        if self._gone_after_kill and sandbox_id in self.killed:
            raise SandboxApiException("gone", status_code=404)
        return SimpleNamespace(status=SimpleNamespace(state="Running"))

    async def close(self) -> None:
        return None


async def test_post_create_failure_destroys_the_just_created_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No create idempotency key exists on the lifecycle API: without the guard a
    # failed post-create step would leak a box for its full lease.
    manager = _FakeLifecycleManager()
    monkeypatch.setattr(sandbox_module, "SandboxManager", _FakeLifecycleManager.factory(manager))
    fake_sdk = _FakeSdkSandbox(files=_FakeFiles(create_dirs_error=RuntimeError("disk full")))
    with mock.patch.object(executor_module, "Sandbox") as sandbox_cls:
        sandbox_cls.create = AsyncMock(return_value=fake_sdk)
        with pytest.raises(RuntimeError, match="disk full"):
            await _build(_create_spec(cwd="/w"))
    assert manager.killed == ["sb-1"], "the box the guard built is destroyed by id"


async def test_execd_readiness_failure_destroys_the_just_created_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeLifecycleManager()
    monkeypatch.setattr(
        sandbox_module,
        "SandboxManager",
        _FakeLifecycleManager.factory(manager),
    )
    fake_sdk = _FakeSdkSandbox()
    fake_sdk.commands.run = AsyncMock(side_effect=RuntimeError("permission denied"))

    with mock.patch.object(executor_module, "Sandbox") as sandbox_cls:
        sandbox_cls.create = AsyncMock(return_value=fake_sdk)
        with pytest.raises(RuntimeError, match="readiness probe failed"):
            await _build(_create_spec(requires_command_channel=True))

    assert manager.killed == ["sb-1"]


async def test_a_create_guard_that_cannot_destroy_hands_back_the_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A box that survived its own cleanup must not exist only in a log line.

    A failed kill that only logs and re-raises the ORIGINAL exception carries no
    id, so the startup failure path has nothing to persist and the running box
    loses its last name. The guard instead raises on an error that carries the
    id in ``data``, which is exactly where ``_run_startup`` reads it.
    """
    manager = _FakeLifecycleManager(gone_after_kill=False)  # the delete does not take
    monkeypatch.setattr(sandbox_module, "SandboxManager", _FakeLifecycleManager.factory(manager))
    fake_sdk = _FakeSdkSandbox(files=_FakeFiles(create_dirs_error=RuntimeError("disk full")))
    with mock.patch.object(executor_module, "Sandbox") as sandbox_cls:
        sandbox_cls.create = AsyncMock(return_value=fake_sdk)
        with pytest.raises(APIError) as caught:
            await _build(_create_spec(cwd="/w"))
    assert (caught.value.data or {}).get("sandbox_id") == "sb-1"
    assert "disk full" in caught.value.message


# ── provider connect and platform permission proof ──────────────────────────


def _fake_manager(state: str) -> Any:
    return SimpleNamespace(
        get_sandbox_info=AsyncMock(
            return_value=SimpleNamespace(status=SimpleNamespace(state=state))
        ),
        close=AsyncMock(),
    )


async def test_existing_sandbox_id_prechecks_then_connects_without_health_wait() -> None:
    fake_sdk = _FakeSdkSandbox()
    manager = _fake_manager("Running")
    with (
        mock.patch.object(sandbox_module, "SandboxManager") as manager_cls,
        mock.patch.object(sandbox_module, "Sandbox") as sandbox_cls,
    ):
        manager_cls.create = AsyncMock(return_value=manager)
        sandbox_cls.connect = AsyncMock(return_value=fake_sdk)
        sandbox_cls.create = AsyncMock()
        handle = await OpenSandboxSandboxProvider().connect("sb-9")
    manager.get_sandbox_info.assert_awaited_once_with("sb-9")
    manager.close.assert_awaited_once()
    connect_call = sandbox_cls.connect.await_args
    assert connect_call.args == ("sb-9",)
    assert connect_call.kwargs["skip_health_check"] is True
    sandbox_cls.create.assert_not_awaited()
    assert handle.sandbox_id == "sb-1"


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


async def test_existing_sandbox_id_not_running_fails_fast() -> None:
    manager = _fake_manager("Terminated")
    with (
        mock.patch.object(sandbox_module, "SandboxManager") as manager_cls,
        mock.patch.object(sandbox_module, "Sandbox") as sandbox_cls,
    ):
        manager_cls.create = AsyncMock(return_value=manager)
        sandbox_cls.connect = AsyncMock()
        with pytest.raises(APIError) as caught:
            await OpenSandboxSandboxProvider().connect("sb-9")
        sandbox_cls.connect.assert_not_awaited()
    assert caught.value.code == "SANDBOX_GONE"
    assert is_sandbox_gone_error(caught.value)


async def test_existing_sandbox_404_reaches_turn_path_as_sandbox_gone() -> None:
    manager = SimpleNamespace(
        get_sandbox_info=AsyncMock(
            side_effect=SandboxApiException("sandbox not found", status_code=404)
        ),
        close=AsyncMock(),
    )
    with mock.patch.object(sandbox_module, "SandboxManager") as manager_cls:
        manager_cls.create = AsyncMock(return_value=manager)
        with pytest.raises(APIError) as caught:
            await OpenSandboxSandboxProvider().connect("sb-gone")
    manager.close.assert_awaited_once()
    assert caught.value.code == "SANDBOX_GONE"
    assert is_sandbox_gone_error(caught.value)


# ── platform endpoint resolution ────────────────────────────────────────────


async def test_platform_resolves_the_image_started_service_without_launching_it() -> None:
    from astrabox.core.service.orchestrator.engine.provisioning import (
        resolve_sandbox_websocket_endpoint,
    )

    commands = _FakeCommands()
    fake_sdk = _FakeSdkSandbox(endpoint="box.test:9001", commands=commands)
    handle = OpenSandboxHandle(fake_sdk)  # type: ignore[arg-type]

    uri = await resolve_sandbox_websocket_endpoint(handle, 9001)
    again = await resolve_sandbox_websocket_endpoint(handle, 9001)
    await handle.close()

    # The image owns the resident service; endpoint publication never launches
    # or repairs an engine process from the host.
    assert commands.calls == []
    assert uri == again == "ws://box.test:9001"
    assert fake_sdk.kill_calls == 0
    assert fake_sdk.close_calls == 1


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


async def test_endpoint_resolution_refuses_a_signed_uri_that_still_needs_headers() -> None:
    from astrabox.core.service.orchestrator.engine.provisioning import (
        resolve_sandbox_websocket_endpoint,
    )

    unsigned_secret = "unsigned-routing-secret"
    signed_secret = "signed-routing-secret"
    fake_sdk = _FakeSdkSandbox(
        endpoint_headers={"OpenSandbox-Secure-Access": unsigned_secret},
        signed_endpoint_headers={"X-Still-Required": signed_secret},
    )
    handle = OpenSandboxHandle(fake_sdk)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError) as caught:
        await resolve_sandbox_websocket_endpoint(handle, 9001)

    message = str(caught.value)
    assert "signed sandbox endpoint still requires routing headers" in message
    assert "engine transport cannot carry" in message
    assert "sandbox=sb-1" in message
    assert unsigned_secret not in message
    assert signed_secret not in message


async def test_provider_waits_in_box_for_the_image_service_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = monkeypatch
    exec_collect = AsyncMock(return_value=(0, b"", b""))
    handle = SimpleNamespace(exec_collect=exec_collect)

    await OpenSandboxSandboxProvider()._await_inbox_service_ready(
        handle,
        port=9001,
        timeout_seconds=7,
    )

    exec_collect.assert_awaited_once()
    command = exec_collect.await_args.args[0]
    assert command[:2] == ["python3", "-c"]
    assert "socket.create_connection" in command[2]
    assert "time.monotonic()+7" in command[2]
    assert "9001" in command[2]


async def test_image_service_readiness_failure_reports_the_probe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = monkeypatch
    handle = SimpleNamespace(
        exec_collect=AsyncMock(return_value=(7, b"", b"Traceback: boom"))
    )

    with pytest.raises(APIError, match="Traceback: boom") as caught:
        await OpenSandboxSandboxProvider()._await_inbox_service_ready(
            handle,
            port=9001,
            timeout_seconds=1,
        )

    assert caught.value.code == "AGENT_RUNTIME_ERROR"
    assert "9001" in caught.value.message


# ── stop / no-ops / provider wiring ──────────────────────────────────────────


async def test_stop_releases_host_side_state_but_never_kills() -> None:
    fake_sdk = _FakeSdkSandbox()
    handle = OpenSandboxHandle(fake_sdk)  # type: ignore[arg-type]

    await handle.close()

    assert fake_sdk.close_calls == 1
    assert fake_sdk.kill_calls == 0


async def test_provider_wires_box_creation_with_the_injected_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = httpx.MockTransport(lambda request: httpx.Response(500))
    provider = OpenSandboxSandboxProvider(transport=sentinel)
    create = AsyncMock(return_value=SimpleNamespace(sandbox_id="sb-2"))
    monkeypatch.setattr(executor_module, "create_open_sandbox_box", create)

    handle = await provider.create_sandbox(_create_spec(cwd="/w"))

    assert handle.sandbox_id == "sb-2"
    assert create.await_args.kwargs["transport"] is sentinel


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


# ── in-box server launch is deadline-bounded ─────────────────────────────────


async def test_a_service_whose_readiness_probe_hangs_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = monkeypatch
    handle = SimpleNamespace(
        exec_collect=AsyncMock(
            side_effect=TimeoutError("execd stopped returning while the image booted")
        )
    )

    with pytest.raises(APIError, match="probe itself did not return") as caught:
        await OpenSandboxSandboxProvider()._await_inbox_service_ready(
            handle,
            port=9001,
            timeout_seconds=1,
        )

    assert caught.value.code == "AGENT_RUNTIME_ERROR"
    assert "execd stopped returning" in caught.value.message


async def test_a_dead_image_service_fails_without_host_side_revival() -> None:
    """The host detects a dead image service; it never assembles or revives it."""

    exec_collect = AsyncMock(return_value=(7, b"", b"connection refused"))
    handle = SimpleNamespace(exec_collect=exec_collect)

    with pytest.raises(APIError):
        await OpenSandboxSandboxProvider()._await_inbox_service_ready(
            handle,
            port=9001,
            timeout_seconds=1,
        )

    command = exec_collect.await_args.args[0][2]
    assert "socket.create_connection" in command
    assert "serve-conversation" not in command
    assert "sandbox_runner.py" not in command
