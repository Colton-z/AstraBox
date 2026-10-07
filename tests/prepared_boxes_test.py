"""A claimed conversation box is adopted by its Session or destroyed."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent import prepared_boxes as pb


class _FakeProvider:
    name = "open_sandbox"

    def __init__(self) -> None:
        self.destroyed: list[str] = []
        self.adopted: list[tuple[str, str, str]] = []

    async def confirm_destroyed(self, sandbox_id: str) -> Any:
        self.destroyed.append(sandbox_id)
        return SimpleNamespace(confirmed=True, detail="fake destroy")

    async def connect(self, sandbox_id: str) -> Any:
        return SimpleNamespace(sandbox_id=sandbox_id)

    async def adopt_sandbox_identity(
        self,
        sandbox: Any,
        *,
        session_id: str,
        assignment_id: str,
    ) -> None:
        self.adopted.append((sandbox.sandbox_id, session_id, assignment_id))


def _template(**overrides: Any) -> Any:
    return SimpleNamespace(
        agent_id="agent-1",
        engine_kind="codex",
        runtime_generation="fp-1",
        sandbox_backend="open_sandbox",
        model_config={},
        **overrides,
    )


def _box_manifest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slot_id": "slot-abc",
        "state": "claimed",
        "placement": pb.PLACEMENT_CONVERSATION_BOX,
        "engine_kind": "codex",
        "sandbox_id": "box-77",
        "sandbox_backend": "open_sandbox",
        "runtime_generation": "fp-1",
        "prepared_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


def test_an_unconfirmed_box_destroy_refuses_the_cold_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnconfirmedProvider(_FakeProvider):
        async def confirm_destroyed(self, sandbox_id: str) -> Any:
            self.destroyed.append(sandbox_id)
            return SimpleNamespace(confirmed=False, detail="control plane timed out")

    provider = _UnconfirmedProvider()
    monkeypatch.setattr(pb, "sandbox_for_name", lambda name: provider)

    with pytest.raises(APIError, match="cold fallback was refused"):
        asyncio.run(
            pb.discard_prepared_box("agent-1", _box_manifest(), reason="activation failed")
        )

    assert provider.destroyed == ["box-77"]


def test_adopting_a_claimed_box_repoints_its_reverse_lookup_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(pb, "sandbox_for_name", lambda name: provider)
    handle = asyncio.run(
        pb.adopt_claimed_box(
            _template(),
            _box_manifest(),
            session_id="session-1",
            assignment_id="assignment-1",
        )
    )
    assert handle.sandbox_id == "box-77"
    assert provider.adopted == [("box-77", "session-1", "assignment-1")]
