"""Reattaching to a running box proves again the permission level it was asked for.

A box that cannot prove the isolation an ``advanced`` level needs is closed
rather than used.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine import (  # noqa: F401
    deepseek_harness as _dsh_engine,  # module import registers the adapter
)
from astrabox.core.service.orchestrator.engine.provisioning import (
    connect_engine_sandbox,
)


@pytest.fixture(autouse=True)
def workspace_preparation_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep recipe tests outside in-box Plugin and Skill installation."""
    async def prepare(*_args: Any, **kwargs: Any) -> tuple[Any, str]:
        return kwargs["runtime_identity"], kwargs["cwd"]

    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.engine.provisioning.prepare_platform_workspace",
        prepare,
    )


class TestTheContractComposesWhatAdaptersUsedToForget:


    @pytest.mark.asyncio
    async def test_reattach_reproves_the_running_box_permission_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Reattached:
            sandbox_id = "box-1"

            def __init__(self) -> None:
                self.close_calls = 0

            async def close(self) -> None:
                self.close_calls += 1

        class ReattachManager:
            async def connect_sandbox_only(self, sandbox_id: str) -> Any:
                assert sandbox_id == "box-1"
                return sandbox

        class Provider:
            name = "capable"
            supported_permission_levels = ("default", "advanced")

            def __init__(self) -> None:
                self.probes: list[str] = []

            async def read_isolation_capability(self, sandbox_id: str) -> Any:
                self.probes.append(sandbox_id)
                return SimpleNamespace(available=True, detail=None)

        sandbox = Reattached()
        provider = Provider()
        import astrabox.seams.sandbox as sandbox_seam

        monkeypatch.setattr(sandbox_seam, "sandbox_for_sandbox", lambda _value: provider)
        result = await connect_engine_sandbox(
            ReattachManager(),
            sandbox_id="box-1",
            template=SimpleNamespace(sandbox_permission_level="advanced"),
        )

        assert result is sandbox
        assert provider.probes == ["box-1"]
        assert sandbox.close_calls == 0

    @pytest.mark.asyncio
    async def test_reattach_closes_a_box_that_cannot_prove_the_requested_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Reattached:
            sandbox_id = "box-1"

            def __init__(self) -> None:
                self.close_calls = 0

            async def close(self) -> None:
                self.close_calls += 1

        class ReattachManager:
            async def connect_sandbox_only(self, sandbox_id: str) -> Any:
                assert sandbox_id == "box-1"
                return sandbox

        async def unavailable(_sandbox_id: str) -> Any:
            return SimpleNamespace(available=False, detail="namespace creation denied")

        provider = SimpleNamespace(
            name="incapable",
            supported_permission_levels=("default", "advanced"),
            read_isolation_capability=unavailable,
        )
        sandbox = Reattached()
        import astrabox.seams.sandbox as sandbox_seam

        monkeypatch.setattr(sandbox_seam, "sandbox_for_sandbox", lambda _value: provider)
        with pytest.raises(APIError, match="namespace creation denied"):
            await connect_engine_sandbox(
                ReattachManager(),
                sandbox_id="box-1",
                template=SimpleNamespace(sandbox_permission_level="advanced"),
            )

        assert sandbox.close_calls == 1
