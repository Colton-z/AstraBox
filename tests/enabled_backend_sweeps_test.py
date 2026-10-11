"""Background inventory and workspace-mount sweeps cover each enabled backend.

A deployment can enable several sandbox backends and install more than it
enables. The sweeps that reclaim boxes nobody owns, retire boxes with stale
network wiring and release workspace views have to visit every enabled backend
— none of them may be skipped because another one failed — and none of the
installed backends this deployment does not run, whose control plane is not
configured and would fail every tick.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.expiration_watcher import (
    sweep_each_inventory_backend,
)
from astrabox.core.service.orchestrator.runtime.storage import mounts
from astrabox.seams import sandbox as sandbox_seam
from astrabox.seams.sandbox import (
    SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
    register_sandbox,
    set_default_sandbox_backend,
    set_enabled_sandbox_backends,
)


class _Route:
    """A workspace route holding attached receipts ``(assignment, backend, box)``."""

    def __init__(
        self, attached: list[tuple[str, str, str]], *, broken: bool = False
    ) -> None:
        self.attached = attached
        self.broken = broken
        self.released: list[str] = []

    async def attached_mounts(self) -> list[tuple[str, str, str]]:
        if self.broken:
            raise RuntimeError("the route's driver cannot be reached")
        return list(self.attached)

    async def unattached_mounts(self) -> list[tuple[str, str, datetime]]:
        return []

    async def release_mounts(self, assignment_id: str) -> bool:
        self.released.append(assignment_id)
        return True


class _Backend:
    def __init__(
        self, name: str, *, lists: bool = True, route: _Route | None = None
    ) -> None:
        self.name = name
        self.supports_inventory_listing = lists
        self._route = route

    def workspace_route(self) -> _Route | None:
        return self._route

    async def probe(self, sandbox_id: str) -> Any:
        return SimpleNamespace(probe_status=SANDBOX_LIFECYCLE_PROBE_NOT_FOUND)

    async def list_sandboxes(self, **_kwargs: Any) -> Any:
        raise AssertionError("listing is not part of these cases")

    async def describe_sandbox(self, sandbox_id: str) -> Any:
        raise AssertionError("describing is not part of these cases")


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_seam, "_BACKENDS", {})
    monkeypatch.setattr(sandbox_seam, "_ENABLED_BACKENDS", None)
    monkeypatch.setattr(sandbox_seam, "_DEFAULT_BACKEND", "")


def _install(*backends: _Backend, enabled: list[str]) -> None:
    for backend in backends:
        register_sandbox(backend)  # type: ignore[arg-type]
    set_default_sandbox_backend(enabled[0])
    set_enabled_sandbox_backends(enabled)


async def test_every_enabled_backend_is_swept_even_after_one_fails() -> None:
    _install(
        _Backend("airgap"),
        _Backend("cloud"),
        _Backend("listless", lists=False),
        _Backend("open_sandbox"),
        enabled=["airgap", "cloud", "listless"],
    )
    swept: list[str] = []

    async def sweep(*, backend: str) -> dict[str, int]:
        swept.append(backend)
        if backend == "airgap":
            raise RuntimeError("airgap's control plane is unreachable")
        return {"ownerless_scanned": 2, "ownerless_reaped": 1}

    summary = await sweep_each_inventory_backend(
        sweep, label="ownerless reap", closing=lambda: False
    )

    # `airgap` failing does not cost `cloud` its sweep; `listless` cannot list
    # its boxes, and `open_sandbox` is installed but not enabled.
    assert swept == ["airgap", "cloud"]
    assert summary == {"cloud.ownerless_scanned": 2, "cloud.ownerless_reaped": 1}


async def test_each_backend_reports_its_own_counters() -> None:
    _install(_Backend("airgap"), _Backend("cloud"), enabled=["airgap", "cloud"])

    async def sweep(*, backend: str) -> dict[str, int]:
        retired = 1 if backend == "airgap" else 0
        return {"stale_wiring_scanned": 3, "stale_wiring_retired": retired}

    summary = await sweep_each_inventory_backend(
        sweep, label="stale-wiring retirement", closing=lambda: False
    )

    assert summary == {
        "airgap.stale_wiring_scanned": 3,
        "airgap.stale_wiring_retired": 1,
        "cloud.stale_wiring_scanned": 3,
        "cloud.stale_wiring_retired": 0,
    }


async def test_a_failure_while_closing_propagates() -> None:
    _install(_Backend("airgap"), enabled=["airgap"])

    async def sweep(*, backend: str) -> dict[str, int]:
        raise RuntimeError("cancelled underneath")

    with pytest.raises(RuntimeError, match="cancelled underneath"):
        await sweep_each_inventory_backend(sweep, label="ownerless reap", closing=lambda: True)


async def test_each_enabled_route_is_reconciled_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_WORKSPACE_VOLUME", "astrabox-workspaces")
    broken = _Route([], broken=True)
    healthy = _Route(
        [
            ("view-cloud", "cloud", "box-gone"),
            # A receipt naming an installed backend this deployment does not enable:
            # nothing here can prove that box is gone, so its view is kept.
            ("view-retired", "open_sandbox", "box-unknown"),
        ]
    )
    unrouted = _Route([("view-ignored", "open_sandbox", "box-x")])
    _install(
        _Backend("airgap", route=broken),
        _Backend("cloud", route=healthy),
        _Backend("open_sandbox", route=unrouted),
        enabled=["airgap", "cloud"],
    )

    summary = await mounts.reconcile_workspace_mounts()

    assert healthy.released == ["view-cloud"]
    assert unrouted.released == []
    assert summary["workspace_mounts_released"] == 1
    # One failure for the route that could not answer, one for the receipt
    # naming the disabled backend.
    assert summary["workspace_mount_release_failures"] == 2
