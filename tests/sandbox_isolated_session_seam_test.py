"""The seam for running many isolated sessions inside one sandbox.

Three properties, each of which fails silently if it is wrong:

* **the per-box answer is a read, not an inference.** Whether a box can host an
  isolated session depends on what its Pod template granted it, so a provider
  that answered from its own configuration would tell an operator what was
  ASKED FOR rather than what took effect. A negative answer is a normal answer
  and must never raise — callers gate on it, so an exception would turn "not
  this box" into an outage.
* **a declared capability is implemented in full.** A backend that reports
  ``supports_isolated_sessions`` but cannot open one fails at the moment a
  conversation needs a box. Registration refuses it instead.
* **closing is idempotent about the SESSION but loud about the BOX.** "It is
  already gone" is success; "I could not ask" is not, and collapsing the two
  would report a live session as torn down.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.seams.sandbox import (
    SandboxIsolatedSession,
    SandboxIsolationCapability,
    SandboxProvider,
    register_sandbox,
)


class _Bare(SandboxProvider):
    """A backend with no isolation concept — the default posture.

    The abstract lifecycle pairs are stubbed; nothing here reaches a box.
    """

    name = "bare-iso-test"

    def connection_config(self, **kwargs: Any) -> Any:
        return None

    def secret_material(self, *, settings: Any) -> str:
        return ""

    def build_dataplane(self, **kwargs: Any) -> Any:
        raise RuntimeError("no dataplane")

    async def connect(self, sandbox_id: str) -> Any:
        raise RuntimeError("no connect")

    async def kill(self, sandbox_id: str) -> bool:
        raise RuntimeError("no kill")


def test_a_backend_without_the_concept_reports_no_rather_than_raising() -> None:
    reported = asyncio.run(_Bare().read_isolation_capability("sbx-1"))
    assert isinstance(reported, SandboxIsolationCapability)
    assert reported.available is False
    assert reported.sandbox_id == "sbx-1"
    # The reason must be legible; an operator reading an empty answer learns
    # nothing and assumes the better of the two possibilities.
    assert reported.detail and "bare-iso-test" in reported.detail


def test_opening_a_session_on_such_a_backend_fails_loud() -> None:
    """Unlike the READ. By the time a caller opens one it has already decided
    this box can host it, so a quiet failure would hand back a session that
    isolates nothing."""
    with pytest.raises(APIError) as caught:
        asyncio.run(
            _Bare().open_isolated_session(
                "sbx-1", workspace_dir="/w", workspace_source_dir="/source"
            )
        )
    assert caught.value.status_code == 501


def test_declaring_the_capability_without_implementing_it_is_refused() -> None:
    class _HalfDeclared(_Bare):
        name = "half-declared-iso"
        supports_isolated_sessions = True

        async def read_isolation_capability(
            self, sandbox_id: str
        ) -> SandboxIsolationCapability:
            return SandboxIsolationCapability(sandbox_id=sandbox_id, available=True)

        async def prepare_isolated_workspace(
            self, sandbox_id: str, *, workspace_dir: str, uid: int, gid: int
        ) -> None:
            return None

        # open / run / close deliberately left inherited — ONE missing
        # operation is enough, and the refusal must name it.

    with pytest.raises(RuntimeError) as caught:
        register_sandbox(_HalfDeclared())
    assert "open_isolated_session" in str(caught.value)


def test_a_non_bool_capability_flag_is_refused() -> None:
    class _Sloppy(_Bare):
        name = "sloppy-iso"
        supports_isolated_sessions = "yes"  # type: ignore[assignment]

    with pytest.raises(RuntimeError) as caught:
        register_sandbox(_Sloppy())
    assert "supports_isolated_sessions must be bool" in str(caught.value)


@pytest.mark.parametrize(
    "levels",
    [
        (),
        ["default"],
        ("root",),
        ("advanced", "advanced"),
        ("default", "privileged"),
    ],
)
def test_an_invalid_permission_level_capability_is_refused(levels: Any) -> None:
    class _Sloppy(_Bare):
        name = "sloppy-permission-levels"
        supported_permission_levels = levels

    with pytest.raises(RuntimeError, match="supported_permission_levels"):
        register_sandbox(_Sloppy())


def test_advanced_permission_requires_a_live_isolation_attestation() -> None:
    class _Unproved(_Bare):
        name = "unproved-advanced-level"
        supported_permission_levels = ("default", "advanced")

    with pytest.raises(RuntimeError, match="read_isolation_capability"):
        register_sandbox(_Unproved())


def test_a_fully_implemented_backend_registers() -> None:
    class _Whole(_Bare):
        name = "whole-iso"
        supports_isolated_sessions = True

        async def read_isolation_capability(
            self, sandbox_id: str
        ) -> SandboxIsolationCapability:
            return SandboxIsolationCapability(sandbox_id=sandbox_id, available=True)

        async def open_isolated_session(
            self,
            sandbox_id: str,
            *,
            workspace_dir: str,
            workspace_source_dir: str,
            uid: int | None = None,
            gid: int | None = None,
            share_net: bool = True,
            extra_writable: list[str] | None = None,
        ) -> SandboxIsolatedSession:
            _ = extra_writable
            return SandboxIsolatedSession(
                sandbox_id=sandbox_id,
                session_id="iso-1",
                uid=uid,
                gid=gid,
                workspace_dir=workspace_dir,
                workspace_source_dir=workspace_source_dir,
            )

        async def prepare_isolated_workspace(
            self, sandbox_id: str, *, workspace_dir: str, uid: int, gid: int
        ) -> None:
            return None

        async def run_in_isolated_session(
            self,
            sandbox_id: str,
            session_id: str,
            *,
            code: str,
            timeout_s: float | None = 60.0,
        ) -> tuple[int, str, str]:
            return (0, "", "")

        async def close_isolated_session(
            self, sandbox_id: str, session_id: str
        ) -> None:
            return None

    register_sandbox(_Whole())  # must not raise
    opened = asyncio.run(
        _Whole().open_isolated_session(
            "sbx-9",
            workspace_dir="/w",
            workspace_source_dir="/source",
            uid=2001,
            gid=2001,
        )
    )
    # The session id is the WHOLE handle a restarted host rebuilds from, so it
    # has to come back to the caller rather than living only inside the provider.
    assert opened.session_id == "iso-1"
    assert (opened.uid, opened.gid) == (2001, 2001)
