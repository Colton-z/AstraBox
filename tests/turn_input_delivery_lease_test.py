"""Input delivery renews after a cold process attaches the transport."""

from __future__ import annotations

from types import SimpleNamespace

from astrabox.core.service.orchestrator.turn_service import TurnService


async def test_transport_attach_is_followed_by_lease_renewal_before_dispatch() -> None:
    calls: list[str] = []
    runtime = object()

    class _RuntimeManager:
        async def maybe_renew_lease_on_activity(self, session_id: str) -> None:
            calls.append(f"renew:{session_id}")

    service = object.__new__(TurnService)
    service._runtime_manager = _RuntimeManager()  # type: ignore[assignment]

    async def _ensure(*_args: object, **_kwargs: object) -> SimpleNamespace:
        calls.append("attach")
        return SimpleNamespace(runtime=runtime)

    service._ensure_runtime_for_session = _ensure  # type: ignore[method-assign]

    returned = await service.ensure_runtime_for_input_delivery(
        {"session_id": "session-cold-attach"},
        user=SimpleNamespace(),
        command_id="command-1",
    )

    assert returned is runtime
    assert calls == [
        "renew:session-cold-attach",
        "attach",
        "renew:session-cold-attach",
    ]
