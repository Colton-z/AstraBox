"""The kernel background starter spawns every required liveness task.

With ``wal_autocheckpoint=0``, importing the checkpointer is insufficient: the
kernel must spawn it or SQLite's WAL grows without bound. These tests observe
task creation rather than module wiring.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import patch

from astrabox.core.service.orchestrator.session_kernel.service_mixins.bootstrap import (
    BootstrapWiringMixin,
)


class _Harness(BootstrapWiringMixin):
    def __init__(self) -> None:
        self._quiesced_reason = ""
        self._worker_id = "w-test"
        self._recovery_tasks: dict[str, Any] = {}
        self.spawned: list[str] = []

    def _spawn_background_task(self, coro: Any, *, name: str) -> asyncio.Task:
        self.spawned.append(name)
        task = asyncio.get_event_loop().create_task(_noop(), name=name)
        coro.close()
        return task

    def _ensure_reconcile_worker_started(self) -> None:
        self.spawned.append("reconcile")


async def _noop() -> None:
    await asyncio.Event().wait()


class BackgroundStarterWiringTests(unittest.IsolatedAsyncioTestCase):
    async def _started_names(self, backend: str) -> list[str]:
        harness = _Harness()
        with patch(
            "astrabox.core.service.orchestrator.session_kernel.service_mixins."
            "bootstrap.active_backend_name",
            return_value=backend,
        ):
            harness.ensure_background_tasks_started()
        for attr in ("_wal_checkpoint_task", "_loop_stall_task"):
            task = getattr(harness, attr, None)
            if isinstance(task, asyncio.Task):
                task.cancel()
        return harness.spawned

    async def test_sqlite_backend_starts_the_wal_checkpointer(self) -> None:
        names = await self._started_names("sqlite")
        self.assertIn("sqlite-wal-checkpointer", names)
        self.assertIn("event-loop-stall-watchdog", names)
        self.assertIn("reconcile", names)

    async def test_other_backends_start_no_dead_checkpointer(self) -> None:
        names = await self._started_names("mongo")
        self.assertNotIn("sqlite-wal-checkpointer", names)
        self.assertIn("event-loop-stall-watchdog", names)


if __name__ == "__main__":
    unittest.main()
