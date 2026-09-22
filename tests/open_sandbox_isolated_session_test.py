"""``open_sandbox``'s half of the isolated-session seam.

What is worth pinning here is not that the calls happen but WHAT IS ASKED FOR,
because two of the request's fields were chosen against measured behaviour of
the real box and a later edit that "tidied" either would break every
conversation in a way no unit test would otherwise notice:

* ``mode="rw"`` — overlay is upstream's default and the one that would give a
  private layer over a shared base, and on this stack every overlay create dies
  at bwrap start. Asking for it would fail conversations rather than isolate
  them.
* ``idle_timeout_seconds=0`` — a conversation is idle between turns by
  definition, and execd's reaper would collect the session (and the resident
  runner inside it) while its user was reading.

Plus the honesty of the capability read: the box's answer is carried across
unchanged, and a box that cannot answer becomes a reasoned ``available=False``
rather than an exception, because callers gate on it.
"""

from __future__ import annotations

import asyncio

import httpx
from types import SimpleNamespace
from typing import Any

import pytest
from opensandbox.models.execd import Execution, ExecutionLogs, OutputMessage

from astrabox.common.utils.errors import APIError
from astrabox.providers.open_sandbox.sandbox import (
    OpenSandboxHandle,
    OpenSandboxSandboxProvider,
)


class _FakeIsolation:
    def __init__(
        self,
        *,
        capabilities: Any = None,
        capabilities_error: Exception | None = None,
        create_result: Any = None,
        create_error: Exception | None = None,
        attach_result: Any = None,
        attach_error: Exception | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._capabilities_error = capabilities_error
        self._create_result = create_result
        self._create_error = create_error
        self._attach_result = attach_result
        self._attach_error = attach_error
        self.create_requests: list[Any] = []
        self.attached: list[str] = []

    async def capabilities(self) -> Any:
        if self._capabilities_error:
            raise self._capabilities_error
        return self._capabilities

    async def create(self, request: Any) -> Any:
        self.create_requests.append(request)
        if self._create_error:
            raise self._create_error
        return self._create_result

    async def attach(self, session_id: str) -> Any:
        self.attached.append(session_id)
        if self._attach_error:
            raise self._attach_error
        return self._attach_result


class _FakeSession:
    def __init__(
        self,
        session_id: str,
        *,
        delete_error: Exception | None = None,
        execution: Any = None,
    ) -> None:
        self.session_id = session_id
        self._delete_error = delete_error
        self._execution = execution
        self.deleted = 0
        self.ran: list[str] = []

    async def delete(self) -> None:
        self.deleted += 1
        if self._delete_error:
            raise self._delete_error

    async def run(self, code: str) -> Any:
        self.ran.append(code)
        return self._execution


def _execution(
    *, stdout: str = "", stderr: str = "", exit_code: int | None = 0, error: Any = None
) -> Any:
    """An execd Execution as the SDK hands it back: per-LINE text events."""
    return SimpleNamespace(
        logs=SimpleNamespace(
            stdout=[SimpleNamespace(text=stdout)] if stdout else [],
            stderr=[SimpleNamespace(text=stderr)] if stderr else [],
        ),
        exit_code=exit_code,
        error=error,
    )


@pytest.fixture(autouse=True)
def _no_secret_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider resolves the API key only to scrub it out of messages.
    Reaching the secret store is not what any of this is about."""
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.sandbox._config.resolve_api_key",
        lambda settings: "",
    )


def _provider(isolation: _FakeIsolation) -> OpenSandboxSandboxProvider:
    provider = OpenSandboxSandboxProvider()

    async def _connect(sandbox_id: str) -> Any:
        return SimpleNamespace(sidecar_faces=SimpleNamespace(isolation=isolation))

    provider.connect = _connect  # type: ignore[method-assign]
    provider._settings = lambda: SimpleNamespace()  # type: ignore[method-assign]
    return provider


def test_the_box_answer_is_carried_across_unchanged() -> None:
    isolation = _FakeIsolation(
        capabilities=SimpleNamespace(
            available=True,
            isolator="bwrap",
            version="0.6.1",
            setpriv_available=True,
            userns_available=False,
            commit_supported=False,
            diff_supported=False,
            message=None,
        )
    )
    reported = asyncio.run(_provider(isolation).read_isolation_capability("sbx-1"))
    assert reported.available is True
    assert (reported.isolator, reported.version) == ("bwrap", "0.6.1")
    # This provider does not implement commit/diff and must report both off; a
    # true default would advertise publication operations that return 503.
    assert reported.commit_supported is False
    assert reported.diff_supported is False
    assert reported.setpriv_available is True


def test_a_box_that_cannot_host_reports_no_with_its_own_reason() -> None:
    isolation = _FakeIsolation(
        capabilities=SimpleNamespace(
            available=False,
            isolator=None,
            version=None,
            setpriv_available=False,
            userns_available=False,
            commit_supported=False,
            diff_supported=False,
            message="Creating new namespace failed: Operation not permitted",
        )
    )
    reported = asyncio.run(_provider(isolation).read_isolation_capability("sbx-1"))
    assert reported.available is False
    assert reported.detail == "Creating new namespace failed: Operation not permitted"


def test_a_box_that_does_not_answer_is_a_finding_not_an_exception() -> None:
    isolation = _FakeIsolation(capabilities_error=RuntimeError("connection refused"))
    reported = asyncio.run(_provider(isolation).read_isolation_capability("sbx-1"))
    assert reported.available is False
    assert reported.detail and "connection refused" in reported.detail


def test_the_session_is_asked_for_in_the_shape_the_box_can_deliver() -> None:
    isolation = _FakeIsolation(create_result=_FakeSession("iso-7"))
    opened = asyncio.run(
        _provider(isolation).open_isolated_session(
            "sbx-1",
            workspace_dir="/workspace",
            workspace_source_dir="/home/conversations/conv_1/workspace",
            uid=2001,
            gid=2001,
            extra_writable=["/home/agent"],
        )
    )
    assert opened.session_id == "iso-7"
    assert (opened.uid, opened.gid) == (2001, 2001)
    assert opened.workspace_dir == "/workspace"
    assert opened.workspace_source_dir == "/home/conversations/conv_1/workspace"
    request = isolation.create_requests[0]
    assert request.workspace.path == "/home/conversations/conv_1/workspace"
    assert request.workspace.mode == "rw", "overlay does not start on this stack"
    assert request.binds is not None
    assert request.binds[0].source == "/home/conversations/conv_1/workspace"
    assert request.binds[0].dest == "/workspace"
    assert request.binds[0].readonly is False
    assert request.idle_timeout_seconds == 0, "a conversation is idle between turns"
    assert request.share_net is True, "the host reaches the runner over the box's netns"
    assert (request.uid, request.gid) == (2001, 2001)
    assert request.extra_writable == ["/home/agent"]


def test_a_dedicated_sandbox_uses_workspace_directly_without_a_bind() -> None:
    isolation = _FakeIsolation(create_result=_FakeSession("iso-8"))

    opened = asyncio.run(
        _provider(isolation).open_isolated_session(
            "sbx-1",
            workspace_dir="/workspace",
            workspace_source_dir="/workspace",
            uid=1000,
            gid=1000,
        )
    )

    request = isolation.create_requests[0]
    assert opened.workspace_dir == "/workspace"
    assert opened.workspace_source_dir == "/workspace"
    assert request.workspace.path == "/workspace"
    assert request.binds is None


def test_a_session_with_no_id_is_refused_rather_than_returned() -> None:
    """The id is the whole handle. One that cannot be addressed cannot be
    closed either, so it would leak a session AND its processes."""
    isolation = _FakeIsolation(create_result=SimpleNamespace(session_id=""))
    with pytest.raises(APIError) as caught:
        asyncio.run(
            _provider(isolation).open_isolated_session(
                "sbx-1", workspace_dir="/w", workspace_source_dir="/source"
            )
        )
    assert "session id" in str(caught.value.message)


def test_opening_without_a_workspace_is_refused_before_the_box_is_touched() -> None:
    isolation = _FakeIsolation(create_result=_FakeSession("iso-7"))
    with pytest.raises(APIError):
        asyncio.run(
            _provider(isolation).open_isolated_session(
                "sbx-1", workspace_dir=" ", workspace_source_dir="/source"
            )
        )
    assert isolation.create_requests == []


def test_closing_a_session_that_is_already_gone_is_success() -> None:
    isolation = _FakeIsolation(attach_error=RuntimeError("404 not found"))
    asyncio.run(_provider(isolation).close_isolated_session("sbx-1", "iso-7"))
    assert isolation.attached == ["iso-7"]


def test_closing_a_session_whose_box_is_already_gone_is_success() -> None:
    isolation = _FakeIsolation()
    provider = _provider(isolation)

    async def gone(_sandbox_id: str) -> Any:
        raise APIError(
            code="SANDBOX_GONE",
            message="sandbox no longer exists",
            status_code=404,
        )

    provider.connect = gone  # type: ignore[method-assign]

    asyncio.run(provider.close_isolated_session("sbx-gone", "iso-7"))
    assert isolation.attached == []


def test_a_delete_that_fails_is_NOT_reported_as_torn_down() -> None:
    """'I could not ask' and 'it is not there' are different facts, and only
    the second one is success — collapsing them reports a live session, with a
    live runner inside it, as destroyed."""
    session = _FakeSession("iso-7", delete_error=RuntimeError("boom"))
    isolation = _FakeIsolation(attach_result=session)
    with pytest.raises(APIError) as caught:
        asyncio.run(_provider(isolation).close_isolated_session("sbx-1", "iso-7"))
    assert caught.value.status_code == 502
    assert session.deleted == 1


def test_closing_an_empty_session_id_touches_nothing() -> None:
    isolation = _FakeIsolation()
    asyncio.run(_provider(isolation).close_isolated_session("sbx-1", ""))
    assert isolation.attached == []


def test_a_run_returns_the_box_answer() -> None:
    session = _FakeSession("iso-7", execution=_execution(stdout="uid=2001", exit_code=0))
    isolation = _FakeIsolation(attach_result=session)
    rc, out, err = asyncio.run(
        _provider(isolation).run_in_isolated_session("sbx-1", "iso-7", code="id")
    )
    assert (rc, out.strip(), err) == (0, "uid=2001", "")
    assert session.ran == ["id"]


def test_a_run_passes_only_the_requested_per_run_environment() -> None:
    class _EnvironmentSession(_FakeSession):
        async def run(self, code: str, *, opts: Any) -> Any:
            self.ran.append(code)
            assert opts.envs == {
                "PRIVATE_API_TOKEN": "ASTRABOX-VAULT-CRED::credential-1::opaque"
            }
            assert opts.timeout_seconds == 60
            return _execution(stdout="ok", exit_code=0)

    session = _EnvironmentSession("iso-7")
    isolation = _FakeIsolation(attach_result=session)
    rc, out, err = asyncio.run(
        _provider(isolation).run_in_isolated_session(
            "sbx-1",
            "iso-7",
            code="printenv PRIVATE_API_TOKEN",
            envs={
                "PRIVATE_API_TOKEN": "ASTRABOX-VAULT-CRED::credential-1::opaque"
            },
        )
    )
    assert (rc, out.strip(), err) == (0, "ok", "")


def test_a_nonzero_exit_is_reported_not_raised() -> None:
    """A command that fails is an ANSWER. Raising would make every caller wrap
    a try/except around a normal outcome."""
    session = _FakeSession("iso-7", execution=_execution(stderr="nope", exit_code=3))
    isolation = _FakeIsolation(attach_result=session)
    rc, _out, err = asyncio.run(
        _provider(isolation).run_in_isolated_session("sbx-1", "iso-7", code="false")
    )
    assert rc == 3
    assert "nope" in err


def test_an_exit_code_hidden_in_an_execd_error_is_recovered() -> None:
    """execd reports a plain non-zero exit as an error payload with no
    exit_code; reading that as success would call a failed launch fine."""
    session = _FakeSession(
        "iso-7",
        execution=_execution(
            exit_code=None,
            error=SimpleNamespace(name="ExitError", value="command exited with code 7"),
        ),
    )
    isolation = _FakeIsolation(attach_result=session)
    rc, _out, err = asyncio.run(
        _provider(isolation).run_in_isolated_session("sbx-1", "iso-7", code="x")
    )
    assert rc == 7
    assert "ExitError" in err


def test_running_in_a_session_that_is_gone_is_a_conflict_not_a_crash() -> None:
    """409, and distinctly: the session was closed or reaped while still in use,
    and the caller's response is to open a new one — not to retry this."""
    isolation = _FakeIsolation(attach_error=RuntimeError("404 not found"))
    with pytest.raises(APIError) as caught:
        asyncio.run(
            _provider(isolation).run_in_isolated_session("sbx-1", "iso-7", code="id")
        )
    assert caught.value.status_code == 409


def test_a_hung_run_times_out_loudly() -> None:
    class _Hangs(_FakeSession):
        async def run(self, code: str) -> Any:
            await asyncio.sleep(10)

    isolation = _FakeIsolation(attach_result=_Hangs("iso-7"))
    with pytest.raises(TimeoutError):
        asyncio.run(
            _provider(isolation).run_in_isolated_session(
                "sbx-1", "iso-7", code="sleep 999", timeout_s=0.05
            )
        )


def test_an_isolated_run_streams_through_the_provider_boundary() -> None:
    class _StreamingSession(_FakeSession):
        async def run(self, code: str, *, opts: Any, handlers: Any) -> Any:
            self.ran.append(code)
            assert opts.timeout_seconds == 45
            assert opts.envs == {
                "PRIVATE_API_TOKEN": "ASTRABOX-VAULT-CRED::credential-1::opaque"
            }
            await handlers.on_stdout(SimpleNamespace(text="uid=2001"))
            await handlers.on_stdout(SimpleNamespace(text="HOME=/home/conversations/a"))
            return _execution(exit_code=0)

    session = _StreamingSession("iso-7")
    isolation = _FakeIsolation(attach_result=session)

    async def _collect() -> list[dict[str, Any]]:
        return [
            event
            async for event in _provider(isolation).stream_in_isolated_session(
                "sbx-1",
                "iso-7",
                code="id -u; printf 'HOME=%s\\n' \"$HOME\"",
                envs={
                    "PRIVATE_API_TOKEN": "ASTRABOX-VAULT-CRED::credential-1::opaque"
                },
                timeout_s=45,
            )
        ]

    events = asyncio.run(_collect())
    assert events == [
        {"type": "stdout", "text": "uid=2001\n"},
        {"type": "stdout", "text": "HOME=/home/conversations/a\n"},
        {"type": "__done__", "exit_code": 0},
    ]
    assert isolation.attached == ["iso-7"]


def _headroom_provider(stdout: bytes) -> OpenSandboxSandboxProvider:
    """A provider whose box answers a command the way the real one does."""

    provider = OpenSandboxSandboxProvider()
    execution = Execution(
        logs=ExecutionLogs(
            stdout=[
                OutputMessage(text=line, timestamp=0)
                for line in stdout.decode("utf-8").splitlines()
            ],
            stderr=[],
        ),
        exit_code=0,
        error=None,
    )
    handle = OpenSandboxHandle(
        SimpleNamespace(  # type: ignore[arg-type]
            commands=SimpleNamespace(
                run=_async_returning(execution),
            ),
            close=_async_returning(None),
        )
    )

    async def _connect(sandbox_id: str) -> Any:
        return handle

    provider.connect = _connect  # type: ignore[method-assign]
    provider._settings = lambda: SimpleNamespace()  # type: ignore[method-assign]
    return provider


def _async_returning(value: Any):
    async def _call(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return _call


def test_the_memory_probe_reads_the_bytes_the_vendor_actually_returns() -> None:
    """The packing gate is only as true as this number.

    Measured on a real pooled box: `memory.max` 2 GiB, `memory.current` ~600 MB.
    """

    limit, current = asyncio.run(
        _headroom_provider(b"2147483648\n632737792\n").read_memory_headroom("sbx-1")
    )
    assert (limit, current) == (2147483648, 632737792)


def test_an_unparseable_memory_answer_proves_no_headroom() -> None:
    """`memory.max` is the literal `max` when the template set no limit.

    Refusing to pack there is deliberate rather than an oversight: with no
    limit the container cannot be reasoned about, and the node would pay for a
    wrong guess instead of the container.
    """

    assert asyncio.run(
        _headroom_provider(b"max\n632737792\n").read_memory_headroom("sbx-1")
    ) == (0, 0)
    assert asyncio.run(_headroom_provider(b"").read_memory_headroom("sbx-1")) == (0, 0)


def test_an_unreachable_box_reports_gone_rather_than_a_missing_session() -> None:
    """The typed code is the mechanism; the text is not a contract.

    The SDK reports a dead route as `SandboxInternalException` — the same class
    it uses for a genuine internal fault — and puts the transport error in
    `__cause__`. Reported as a 409 about the session, a destroyed box left the
    conversation retrying an attach to something that cannot come back; only
    SANDBOX_GONE licenses giving it a new box.
    """

    from astrabox.providers.open_sandbox.sandbox import box_is_unreachable

    dead_route = Exception("Network connectivity error: All connection attempts failed")
    dead_route.__cause__ = httpx.ConnectError("All connection attempts failed")

    assert box_is_unreachable(dead_route) is True


def test_a_session_that_is_simply_absent_is_not_a_gone_box() -> None:
    """An answering box without this session is a 409, not a gone box.

    The control for the rule above, which without it reads as "any attach
    failure means the box died" and rebuilds a healthy box for every stale
    session id handed to it.
    """

    from astrabox.providers.open_sandbox.sandbox import box_is_unreachable

    absent = Exception("isolated session 'iso-1' not found")

    assert box_is_unreachable(absent) is False


def test_the_cause_chain_is_walked_not_just_the_outermost_error() -> None:
    """The transport error is never the exception the caller sees.

    It is wrapped at least once by the SDK, so a check on the outermost class
    finds nothing and reports every dead box as a missing session.
    """

    from astrabox.providers.open_sandbox.sandbox import box_is_unreachable

    inner = httpx.ConnectTimeout("timed out")
    middle = Exception("sdk wrapper")
    middle.__cause__ = inner
    outer = Exception("another layer")
    outer.__cause__ = middle

    assert box_is_unreachable(outer) is True


def test_extra_binds_ride_along_with_the_workspace_bind() -> None:
    """A caller can narrow a path the backend already provides.

    The shared tenancy gives every conversation a /tmp of its own this way:
    execd mounts a root-owned tmpfs there that nothing running as the
    conversation can write, and an engine that sandboxes its own commands
    dies preparing mounts under it. Asserted on the request rather than on
    the result, because the bind list IS the whole mechanism.
    """

    isolation = _FakeIsolation(create_result=_FakeSession("iso-9"))
    asyncio.run(
        _provider(isolation).open_isolated_session(
            "sbx-1",
            workspace_dir="/workspace",
            workspace_source_dir="/home/conversations/conv_1/workspace",
            uid=2001,
            gid=2001,
            extra_writable=["/home/conversations/conv_1"],
            extra_binds=[("/home/conversations/conv_1/tmp", "/tmp")],
        )
    )

    request = isolation.create_requests[0]
    assert request.binds is not None
    assert [(bind.source, bind.dest, bind.readonly) for bind in request.binds] == [
        ("/home/conversations/conv_1/workspace", "/workspace", False),
        ("/home/conversations/conv_1/tmp", "/tmp", False),
    ]


def test_an_extra_bind_is_carried_even_without_a_workspace_bind() -> None:
    """A dedicated box binds nothing for its workspace, and still gets these."""

    isolation = _FakeIsolation(create_result=_FakeSession("iso-10"))
    asyncio.run(
        _provider(isolation).open_isolated_session(
            "sbx-1",
            workspace_dir="/workspace",
            workspace_source_dir="/workspace",
            uid=1000,
            gid=1000,
            extra_binds=[("/home/agent/tmp", "/tmp")],
        )
    )

    request = isolation.create_requests[0]
    assert request.binds is not None
    assert [(bind.source, bind.dest) for bind in request.binds] == [
        ("/home/agent/tmp", "/tmp")
    ]
