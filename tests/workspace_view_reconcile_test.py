"""A workspace view no sandbox ever mounted is released, and only then.

A view (helper + view volume on Docker; helper Pod, PV and PVC on Kubernetes)
is provisioned before its sandbox is created. When the create fails, nothing
records a sandbox for the view; a prewarm pool retrying once a minute then
piles up one helper per attempt. Reconciliation must release such a view once
no box can still claim it, and must not release a view a box still carries or
might still be created for.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from astrabox.core.service.orchestrator.runtime.storage import mounts
from astrabox.core.service.orchestrator.runtime.storage._mergerfs_docker import (
    DockerMounts,
    _docker_timestamp,
)
from astrabox.core.service.orchestrator.runtime.storage.mergerfs import (
    INSTALLATION,
    MANAGED,
    OWNER,
    SANDBOX_BACKEND,
    STORAGE_ID,
)
from astrabox.seams.sandbox import (
    SANDBOX_INSTALLATION_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_VALUE,
    SandboxCreateSpec,
    SandboxPage,
    sandbox_installation_id,
)
from astrabox.seams.storage import StorageMountPlan

_LONG_AGO = datetime.now(timezone.utc) - timedelta(hours=1)
_JUST_NOW = datetime.now(timezone.utc) - timedelta(seconds=30)


class _Router:
    """The workspace router's receipts, as the drivers report them."""

    def __init__(self) -> None:
        self.views: dict[str, dict[str, Any]] = {}
        self.released: list[str] = []
        self.refuse: set[str] = set()

    async def provision_mounts(
        self, assignment_id: str, backing: StorageMountPlan, *, sandbox_backend: str
    ) -> StorageMountPlan:
        self.views[assignment_id] = {"backend": sandbox_backend, "created_at": _LONG_AGO}
        return StorageMountPlan("view-volume", backing.mounts)

    async def attach_sandbox(
        self, assignment_id: str, *, sandbox_id: str, sandbox_backend: str
    ) -> None:
        self.views[assignment_id]["sandbox_id"] = sandbox_id

    async def attached_mounts(self) -> list[tuple[str, str, str]]:
        return [
            (assignment, view["backend"], view["sandbox_id"])
            for assignment, view in self.views.items()
            if "sandbox_id" in view
        ]

    async def unattached_mounts(self) -> list[tuple[str, str, datetime]]:
        return [
            (assignment, view["backend"], view["created_at"])
            for assignment, view in self.views.items()
            if "sandbox_id" not in view
        ]

    async def release_mounts(self, assignment_id: str) -> None:
        if assignment_id in self.refuse:
            raise RuntimeError("workspace PVC still has sandbox Pod consumers")
        self.released.append(assignment_id)
        del self.views[assignment_id]


class _Backend:
    """A lifecycle server whose inventory is ``boxes`` (their metadata)."""

    name = "open_sandbox"

    def __init__(self, *, fail_create: bool = False) -> None:
        self.boxes: list[Mapping[str, str]] = []
        self.fail_create = fail_create
        self.list_error: Exception | None = None

    async def find_sandbox_by_assignment(self, assignment_id: str) -> None:
        return None

    async def create_sandbox(self, spec: SandboxCreateSpec) -> Any:
        if self.fail_create:
            raise RuntimeError("Create sandbox failed: Egress sidecar container failed to start")
        self.boxes.append(
            {
                **spec.metadata,
                SANDBOX_MANAGED_BY_METADATA_KEY: SANDBOX_MANAGED_BY_METADATA_VALUE,
                SANDBOX_INSTALLATION_METADATA_KEY: await sandbox_installation_id(),
            }
        )
        return SimpleNamespace(sandbox_id=f"sb-{len(self.boxes)}")

    async def list_sandboxes(
        self, *, page: int = 1, page_size: int = 50, metadata: Mapping[str, str] | None = None
    ) -> SandboxPage:
        if self.list_error is not None:
            raise self.list_error
        matches = [
            box
            for box in self.boxes
            if all(box.get(key) == value for key, value in (metadata or {}).items())
        ]
        return SandboxPage(
            items=(),
            page=page,
            page_size=page_size,
            total_items=len(matches),
            total_pages=1,
            has_next_page=False,
        )


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> _Router:
    fake = _Router()
    monkeypatch.setattr(mounts, "workspace_router", fake)
    monkeypatch.setenv("ASTRABOX_SANDBOX_WORKSPACE_VOLUME", "astrabox-workspaces")

    async def _backing(assignment: str, planned: Any) -> StorageMountPlan:
        return StorageMountPlan("astrabox-workspaces", planned)

    monkeypatch.setattr(
        mounts, "storage_provider", lambda: SimpleNamespace(provision_mounts=_backing)
    )
    return fake


def _use(monkeypatch: pytest.MonkeyPatch, backend: _Backend) -> None:
    monkeypatch.setattr(mounts, "sandbox_for_name", lambda _name: backend)


def _spec(session_id: str) -> SandboxCreateSpec:
    return SandboxCreateSpec(
        session_id=session_id,
        assignment_id=f"assignment-{session_id}",
        resource_limits={"cpu": "1", "memory": "1Gi"},
        resource_requests={"cpu": "100m", "memory": "256Mi"},
        workspace_mounts=(("/workspace", f"sessions/{session_id}/workspace"),),
    )


async def test_a_failed_create_leaves_no_view_after_reconciliation(
    monkeypatch: pytest.MonkeyPatch, router: _Router
) -> None:
    backend = _Backend(fail_create=True)
    _use(monkeypatch, backend)
    for attempt in range(3):
        with pytest.raises(RuntimeError, match="Egress sidecar"):
            await mounts.create_sandbox_with_storage(backend, _spec(f"s{attempt}"))
    assert len(router.views) == 3, "precondition: each failed create left its view"

    summary = await mounts.reconcile_workspace_mounts()

    assert router.views == {}
    assert summary["workspace_mounts_unclaimed_released"] == 3


async def test_a_view_a_box_carries_is_kept_even_without_its_receipt(
    monkeypatch: pytest.MonkeyPatch, router: _Router
) -> None:
    # The create succeeded but the attached receipt was never written: the box
    # still mounts the view, so the view stays until the box is gone.
    backend = _Backend()
    _use(monkeypatch, backend)
    await mounts.create_sandbox_with_storage(backend, _spec("live"))
    for view in router.views.values():
        view.pop("sandbox_id")

    await mounts.reconcile_workspace_mounts()
    assert router.released == []

    backend.boxes.clear()
    await mounts.reconcile_workspace_mounts()
    assert len(router.released) == 1


async def test_a_box_of_another_installation_does_not_keep_this_view(
    monkeypatch: pytest.MonkeyPatch, router: _Router
) -> None:
    backend = _Backend(fail_create=True)
    _use(monkeypatch, backend)
    with pytest.raises(RuntimeError):
        await mounts.create_sandbox_with_storage(backend, _spec("s"))
    (assignment,) = router.views
    backend.boxes.append(
        {
            OWNER: assignment,
            SANDBOX_MANAGED_BY_METADATA_KEY: SANDBOX_MANAGED_BY_METADATA_VALUE,
            SANDBOX_INSTALLATION_METADATA_KEY: "other",
        }
    )

    await mounts.reconcile_workspace_mounts()

    assert router.released == [assignment]


async def test_a_view_still_inside_its_create_window_is_kept(
    monkeypatch: pytest.MonkeyPatch, router: _Router
) -> None:
    # The lifecycle server may still create the box after its client gave up.
    backend = _Backend(fail_create=True)
    _use(monkeypatch, backend)
    with pytest.raises(RuntimeError):
        await mounts.create_sandbox_with_storage(backend, _spec("s"))
    for view in router.views.values():
        view["created_at"] = _JUST_NOW

    await mounts.reconcile_workspace_mounts()

    assert router.released == []


async def test_an_unanswered_inventory_releases_nothing(
    monkeypatch: pytest.MonkeyPatch, router: _Router
) -> None:
    backend = _Backend(fail_create=True)
    _use(monkeypatch, backend)
    with pytest.raises(RuntimeError):
        await mounts.create_sandbox_with_storage(backend, _spec("s"))
    backend.list_error = RuntimeError("lifecycle server unreachable")

    summary = await mounts.reconcile_workspace_mounts()

    assert router.released == []
    assert summary["workspace_mount_release_failures"] == 1


async def test_a_view_still_mounted_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch, router: _Router
) -> None:
    backend = _Backend(fail_create=True)
    _use(monkeypatch, backend)
    for session in ("a", "b"):
        with pytest.raises(RuntimeError):
            await mounts.create_sandbox_with_storage(backend, _spec(session))
    mounted, other = list(router.views)
    router.refuse.add(mounted)

    summary = await mounts.reconcile_workspace_mounts()

    assert router.released == [other]
    assert list(router.views) == [mounted]
    assert summary["workspace_mount_release_failures"] == 1


def test_docker_timestamps_keep_their_instant() -> None:
    assert _docker_timestamp("2026-09-23T10:11:12.123456789Z") == datetime(
        2026, 9, 23, 10, 11, 12, 123456, tzinfo=timezone.utc
    )
    assert _docker_timestamp("2026-09-23T10:11:12Z") == datetime(
        2026, 9, 23, 10, 11, 12, tzinfo=timezone.utc
    )


def test_docker_lists_only_this_installations_helpers_without_a_receipt() -> None:
    pytest.importorskip("docker", reason="the driver reads the Docker SDK's NotFound")

    class _Collection:
        def __init__(self, items: dict[str, Any]) -> None:
            self.items = items

        def get(self, name: str) -> Any:
            from docker.errors import NotFound

            if name not in self.items:
                raise NotFound(name)
            return self.items[name]

        def list(self, **kwargs: Any) -> list[Any]:
            wanted = dict(item.split("=", 1) for item in kwargs["filters"]["label"])
            return [
                item
                for item in self.items.values()
                if all(item.labels.get(key) == value for key, value in wanted.items())
            ]

    def helper(name: str, installation: str) -> Any:
        return SimpleNamespace(
            name=name,
            labels={
                MANAGED: "mergerfs",
                INSTALLATION: installation,
                STORAGE_ID: f"storage-of-{name}",
                SANDBOX_BACKEND: "open_sandbox",
            },
            attrs={"Created": "2026-09-23T10:11:12.5Z"},
        )

    driver = object.__new__(DockerMounts)
    driver.client = SimpleNamespace(
        containers=_Collection(
            {
                "astrabox-view-pending": helper("astrabox-view-pending", "this"),
                "astrabox-view-attached": helper("astrabox-view-attached", "this"),
                "astrabox-view-theirs": helper("astrabox-view-theirs", "other"),
            }
        ),
        volumes=_Collection({"astrabox-view-attached-receipt": SimpleNamespace()}),
    )

    assert driver.unattached("this") == [
        (
            "storage-of-astrabox-view-pending",
            "open_sandbox",
            datetime(2026, 9, 23, 10, 11, 12, 500000, tzinfo=timezone.utc),
        )
    ]
