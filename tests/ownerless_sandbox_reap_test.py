"""The ownerless reap gives back only boxes this installation created.

Two installations on one Docker daemon, or in one Kubernetes namespace, list
each other's boxes from the shared control plane, and neither database has a
row for the other's boxes. "No row names it" therefore means "ownerless" only
for a box this installation created; another installation's box must survive
every reap however long it has run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pytest

from astrabox.core.service.orchestrator import runtime_manager as runtime_manager_module
from astrabox.persistence.repository import agent_repository
from astrabox.seams.sandbox import (
    SANDBOX_INSTALLATION_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_VALUE,
    SANDBOX_SESSION_ID_METADATA_KEY,
    SandboxDescriptor,
    SandboxPage,
    sandbox_installation_id,
)
from astrabox.seams.sandbox_disposal import SandboxDestruction

_OTHER_INSTALLATION = "the-other-installation"
_LONG_AGO = datetime.now(timezone.utc) - timedelta(hours=6)


def _box(sandbox_id: str, installation: str) -> SandboxDescriptor:
    return SandboxDescriptor(
        sandbox_id=sandbox_id,
        state="Running",
        created_at=_LONG_AGO,
        metadata={
            SANDBOX_MANAGED_BY_METADATA_KEY: SANDBOX_MANAGED_BY_METADATA_VALUE,
            SANDBOX_INSTALLATION_METADATA_KEY: installation,
            SANDBOX_SESSION_ID_METADATA_KEY: f"session-of-{sandbox_id}",
        },
        session_id=f"session-of-{sandbox_id}",
    )


class _SharedControlPlane:
    """One lifecycle server's view of a daemon both installations create on.

    ``honours_filter`` is whether the server applies a list call's metadata
    filter. The reap must not depend on it: a filter narrows what comes back,
    the metadata on each box is what licenses destroying it.
    """

    name = "open_sandbox"

    def __init__(self, boxes: list[SandboxDescriptor], *, honours_filter: bool) -> None:
        self.boxes = boxes
        self.honours_filter = honours_filter
        self.destroyed: list[str] = []

    async def list_sandboxes(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        metadata: Mapping[str, str] | None = None,
    ) -> SandboxPage:
        items = [
            box
            for box in self.boxes
            if not (self.honours_filter and metadata)
            or all(box.metadata.get(key) == value for key, value in metadata.items())
        ]
        start = (page - 1) * page_size
        return SandboxPage(
            items=tuple(items[start : start + page_size]),
            page=page,
            page_size=page_size,
            total_items=len(items),
            total_pages=max(1, -(-len(items) // page_size)),
            has_next_page=start + page_size < len(items),
        )

    def owns_unclaimed_sandbox(self, descriptor: SandboxDescriptor) -> bool:
        return False

    async def confirm_destroyed(self, sandbox_id: str) -> SandboxDestruction:
        self.destroyed.append(sandbox_id)
        self.boxes = [box for box in self.boxes if box.sandbox_id != sandbox_id]
        return SandboxDestruction.confirmed_gone(sandbox_id, detail="removed")


class _NoRows:
    """This installation's database: it names none of the listed boxes."""

    async def find_session_by_sandbox_id(self, sandbox_id: str) -> dict[str, Any] | None:
        return None

    async def list_agents_by_sandbox_id(self, sandbox_id: str) -> list[dict[str, Any]]:
        return []


@pytest.mark.parametrize("honours_filter", [True, False])
async def test_reap_gives_back_only_this_installations_ownerless_box(
    monkeypatch: pytest.MonkeyPatch, honours_filter: bool
) -> None:
    ours = _box("ours", await sandbox_installation_id())
    theirs = _box("theirs", _OTHER_INSTALLATION)
    control_plane = _SharedControlPlane([theirs, ours], honours_filter=honours_filter)
    monkeypatch.setattr(runtime_manager_module, "sandbox_for_name", lambda _name: control_plane)
    monkeypatch.setattr(agent_repository, "AgentRepository", _NoRows)
    manager = runtime_manager_module.RemoteAgentRuntimeManager(sessions_repo=_NoRows())

    async def _no_occupants(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(manager, "agent_box_has_other_occupants", _no_occupants)

    for _tick in range(3):
        await manager.reap_ownerless_sandboxes(backend="fake")

    assert control_plane.destroyed == ["ours"]
