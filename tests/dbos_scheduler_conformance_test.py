"""The built-in DBOS scheduler provider meets the scheduler contract on SQLite.

Binds :class:`astrabox.testing.scheduler_conformance.SchedulerProviderContractSuite`
to :class:`astrabox.providers.scheduler_dbos.DbosSchedulerProvider` over a fresh
SQLite file per case, the way a downstream binds its own provider. DBOS fires a
due time with the same deterministic id whether its scheduler thread or a
backfill fires it, so a backfill stands in for the engine reaching that time.
The scheduler thread runs only for an active schedule, and a backfill does not
look, so the binding skips a paused or missing schedule itself.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

from dbos import DBOS

from astrabox.providers.scheduler_dbos import (
    SCHEDULE_NAME_PREFIX,
    DbosSchedulerProvider,
)
from astrabox.seams.scheduler import SchedulerProvider
from astrabox.testing.scheduler_conformance import SchedulerProviderContractSuite


class TestDbosSchedulerProvider(SchedulerProviderContractSuite):
    def make_provider(self, store: str) -> SchedulerProvider:
        provider = DbosSchedulerProvider()
        provider.configure(
            repository_backend=SimpleNamespace(
                sql_database_url=lambda: f"sqlite+aiosqlite:///{store}.sqlite"
            )
        )
        return provider

    async def fire_due_times(
        self,
        provider: SchedulerProvider,
        deployment_id: str,
        start: datetime,
        end: datetime,
    ) -> None:
        name = f"{SCHEDULE_NAME_PREFIX}{deployment_id}"
        schedule = await DBOS.get_schedule_async(name)
        if schedule is None or schedule.get("status", "ACTIVE") != "ACTIVE":
            return
        await asyncio.to_thread(DBOS.backfill_schedule, name, start, end)
