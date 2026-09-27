"""The watcher destroys boxes whose recorded network wiring is not the deployment's.

Such a box can reach neither the model nor the platform, and its egress policy
still exempts the sandbox edge's old bridge address, which Docker may since have
given to another sandbox. The sweep reads the whole inventory and leaves alone
what is not this platform's to judge.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import astrabox.core.service.orchestrator.runtime_manager as runtime_manager_module
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.seams.sandbox import (
    SANDBOX_INSTALLATION_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_VALUE,
)
from tests.conftest import TEST_SANDBOX_INSTALLATION_ID

_MANAGED = {
    SANDBOX_MANAGED_BY_METADATA_KEY: SANDBOX_MANAGED_BY_METADATA_VALUE,
    SANDBOX_INSTALLATION_METADATA_KEY: TEST_SANDBOX_INSTALLATION_ID,
}


class _Provider:
    """``honours_filter`` is whether the lifecycle server applies the metadata filter."""

    def __init__(
        self, pages: list[list[tuple[str, dict[str, str]]]], *, honours_filter: bool = False
    ) -> None:
        self.pages = pages
        self.honours_filter = honours_filter
        self.destroyed: list[str] = []

    async def list_sandboxes(
        self, *, page: int, page_size: int, metadata: dict[str, str] | None = None
    ) -> Any:
        items = tuple(
            SimpleNamespace(sandbox_id=sandbox_id, metadata=box)
            for sandbox_id, box in self.pages[page - 1]
            if not (self.honours_filter and metadata)
            or all(box.get(key) == value for key, value in metadata.items())
        )
        return SimpleNamespace(items=items, has_next_page=page < len(self.pages))

    def owns_unclaimed_sandbox(self, descriptor: Any) -> bool:
        return descriptor.metadata.get("pool") == "unclaimed"

    def created_with_current_network_wiring(self, descriptor: Any) -> bool:
        return descriptor.metadata.get("wiring") == "current"

    async def confirm_destroyed(self, sandbox_id: str) -> Any:
        self.destroyed.append(sandbox_id)
        return SimpleNamespace(confirmed=True, detail="")


async def test_stale_managed_boxes_are_destroyed_on_every_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider(
        [
            [
                ("sb-current", {**_MANAGED, "wiring": "current"}),
                ("sb-stale-1", {**_MANAGED, "wiring": "old"}),
                ("sb-foreign", {"wiring": "old"}),
            ],
            [
                ("sb-pool-member", {**_MANAGED, "wiring": "old", "pool": "unclaimed"}),
                ("sb-stale-2", {**_MANAGED, "wiring": "old"}),
            ],
        ]
    )
    monkeypatch.setattr(runtime_manager_module, "sandbox_for_name", lambda _name: provider)

    summary = await RemoteAgentRuntimeManager.retire_sandboxes_with_stale_network_wiring(
        SimpleNamespace(), page_size=3  # type: ignore[arg-type]
    )

    assert provider.destroyed == ["sb-stale-1", "sb-stale-2"]
    assert summary == {
        "stale_wiring_scanned": 5,
        "stale_wiring_retired": 2,
        "stale_wiring_failures": 0,
    }


@pytest.mark.parametrize("honours_filter", [True, False])
async def test_another_installations_boxes_are_never_retired(
    monkeypatch: pytest.MonkeyPatch, honours_filter: bool
) -> None:
    """Another installation on the daemon has its own edges, so every box it
    made records wiring that differs from this installation's, and works."""
    theirs = {**_MANAGED, SANDBOX_INSTALLATION_METADATA_KEY: "another-installation"}
    provider = _Provider(
        [
            [
                ("sb-theirs", {**theirs, "wiring": "old"}),
                ("sb-ours-stale", {**_MANAGED, "wiring": "old"}),
            ]
        ],
        honours_filter=honours_filter,
    )
    monkeypatch.setattr(runtime_manager_module, "sandbox_for_name", lambda _name: provider)

    await RemoteAgentRuntimeManager.retire_sandboxes_with_stale_network_wiring(
        SimpleNamespace(), page_size=3  # type: ignore[arg-type]
    )

    assert provider.destroyed == ["sb-ours-stale"]
