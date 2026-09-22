from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.runtime.diagnostics import (
    _LIFECYCLE_DIAGNOSTICS_MAX_CHARS,
    collect_runtime_start_diagnostics,
    read_lifecycle_diagnostics,
)
from astrabox.seams.sandbox import SandboxDiagnostics


@pytest.mark.asyncio
async def test_runtime_failure_keeps_lifecycle_evidence_when_execd_is_dead() -> None:
    order: list[str] = []

    class Provider:
        name = "open_sandbox"

        async def read_diagnostics(self, sandbox_id: str, *, scope: str) -> SandboxDiagnostics:
            order.append(scope)
            assert sandbox_id == "sb-1"
            return SandboxDiagnostics(
                sandbox_id=sandbox_id,
                scope=scope,
                content_type="text/plain",
                text=(
                    "Phase: Running\nContainersReady: False"
                    if scope == "inspect"
                    else "execd exited unexpectedly"
                    if scope == "logs"
                    else "Scheduled"
                ),
            )

    class Commands:
        async def run(self, _command: str) -> Any:
            order.append("command")
            raise ConnectionError("execd is gone")

    sandbox = SimpleNamespace(id="sb-1", commands=Commands())
    detail = await collect_runtime_start_diagnostics(
        sandbox,
        session_id="session-1",
        exc=RuntimeError("bootstrap stream ended"),
        sandbox_provider=Provider(),
        get_underlying_sandbox_fn=lambda value: value,
        extract_sandbox_id_fn=lambda value: getattr(value, "id", ""),
    )

    assert set(order[:3]) == {"inspect", "events", "logs"}
    assert order[3:] == ["command"]
    assert detail is not None
    assert "lifecycle: inspect: Phase: Running ; ContainersReady: False" in detail
    assert "execd exited unexpectedly" in detail
    assert "no sandbox log hints" not in detail


@pytest.mark.asyncio
async def test_runtime_failure_still_uses_in_box_hint_when_lifecycle_report_fails() -> None:
    class Provider:
        name = "open_sandbox"

        async def read_diagnostics(self, _sandbox_id: str, *, scope: str) -> SandboxDiagnostics:
            assert scope in {"inspect", "events", "logs"}
            raise RuntimeError("control plane unavailable")

    result = SimpleNamespace(
        logs=SimpleNamespace(
            stdout=[SimpleNamespace(text="runner failed before initialize")],
            stderr=[],
        )
    )
    sandbox = SimpleNamespace(
        id="sb-2",
        commands=SimpleNamespace(run=lambda _command: _async_value(result)),
    )

    detail = await collect_runtime_start_diagnostics(
        sandbox,
        session_id="session-2",
        exc=RuntimeError("runner failed"),
        sandbox_provider=Provider(),
        get_underlying_sandbox_fn=lambda value: value,
        extract_sandbox_id_fn=lambda value: getattr(value, "id", ""),
    )

    assert detail == "runtime start failure diagnostics: in-box: runner failed before initialize"


@pytest.mark.asyncio
async def test_lifecycle_report_keeps_each_scope_when_summary_labels_would_mask_logs() -> None:
    class Provider:
        async def read_diagnostics(self, sandbox_id: str, *, scope: str) -> SandboxDiagnostics:
            text = {
                "inspect": "INSPECT_READY_FALSE\n" + ("label=value\n" * 1000),
                "events": ("old event\n" * 1000) + "LATEST_SCHEDULER_EVENT",
                "logs": ("old log\n" * 1000) + "LATEST_EXECD_FAILURE",
            }[scope]
            return SandboxDiagnostics(
                sandbox_id=sandbox_id,
                scope=scope,
                content_type="text/plain",
                text=text,
            )

    detail = await read_lifecycle_diagnostics(Provider(), "sb-3")

    assert len(detail) <= _LIFECYCLE_DIAGNOSTICS_MAX_CHARS
    assert detail.startswith("inspect: INSPECT_READY_FALSE")
    assert "events: ... " in detail
    assert "LATEST_SCHEDULER_EVENT" in detail
    assert "logs: ... " in detail
    assert detail.endswith("LATEST_EXECD_FAILURE")


async def _async_value(value: Any) -> Any:
    return value
