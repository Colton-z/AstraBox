"""Workspace view reconciliation only ever sees this installation's helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.providers.open_sandbox.workspace_route._mergerfs_docker import (
    DockerMounts,
)
from astrabox.providers.open_sandbox.workspace_route.mergerfs import (
    INSTALLATION,
    MANAGED,
    SANDBOX_BACKEND,
    STORAGE_ID,
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
