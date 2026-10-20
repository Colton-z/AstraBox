"""Reusable contract suite for a :class:`~astrabox.seams.scheduler.SchedulerProvider`.

A downstream binds it to its own provider and store::

    # your_plugin/tests/scheduler_conformance_test.py
    from astrabox.testing.scheduler_conformance import SchedulerProviderContractSuite

    class TestMongoScheduler(SchedulerProviderContractSuite):
        def make_provider(self, store: str) -> SchedulerProvider:
            ...  # a configured, not yet started provider over a fresh store
        async def fire_due_times(self, provider, deployment_id, start, end) -> None:
            ...  # fire every due time in (start, end) the way the engine does

SCOPE — one process. These cases drive the provider through the seam only:
dispatch to the handler, permanent and transient handler failures, the Run
ledger, the payload a Run captures and keeps through later edits, replay
provenance, one invocation per due time even when the same due time fires
twice, disabling and re-enabling, a refused change keeping the previous
schedule, and schedule removal. Guarantees
that need a second process — another replica firing the same due time, and an
invocation resumed after its process died — are part of the contract too
(:mod:`astrabox.seams.scheduler`), and a provider proves them on a real
multi-replica deployment of its own store.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from astrabox.seams.scheduler import (
    InvalidSchedule,
    RunInvocation,
    RunPermanentError,
    RunRecord,
    RunInputBinding,
    ScheduleDefinition,
    SchedulerProvider,
)

#: How long a case waits for a Run to reach the state it expects.
SETTLE_SECONDS = 30.0


class RecordingHandler:
    """A run handler that records every call and can be told to fail."""

    def __init__(self) -> None:
        self.session_calls: list[RunInvocation] = []
        self.input_calls: list[tuple[RunInvocation, str]] = []
        self.permanent_failure = False
        self.transient_failures = 0

    async def start_session(self, invocation: RunInvocation) -> str:
        self.session_calls.append(invocation)
        if self.permanent_failure:
            raise RunPermanentError("the Deployment no longer exists")
        if self.transient_failures > 0:
            self.transient_failures -= 1
            raise RuntimeError("the session store blinked")
        return f"session-{invocation.run_id}"

    async def admit_input(
        self, invocation: RunInvocation, *, session_id: str
    ) -> RunInputBinding:
        self.input_calls.append((invocation, session_id))
        return RunInputBinding(input_id=f"input-{invocation.run_id}")


class SchedulerProviderContractSuite:
    """Subclass and implement :meth:`make_provider` and :meth:`fire_due_times`."""

    def make_provider(self, store: str) -> SchedulerProvider:
        """A configured provider over a fresh store at path ``store``, not started."""
        raise NotImplementedError("bind the suite: override make_provider()")

    async def fire_due_times(
        self,
        provider: SchedulerProvider,
        deployment_id: str,
        start: datetime,
        end: datetime,
    ) -> None:
        """Fire every due time of ``deployment_id`` in ``(start, end)`` as the engine would.

        "As the engine would" includes what the engine skips: a disabled or
        removed schedule fires nothing.
        """
        raise NotImplementedError("bind the suite: override fire_due_times()")

    @pytest.fixture
    def handler(self) -> RecordingHandler:
        return RecordingHandler()

    @pytest.fixture
    async def provider(
        self, tmp_path: Path, handler: RecordingHandler
    ) -> AsyncIterator[SchedulerProvider]:
        provider = self.make_provider(str(tmp_path / "scheduler-store"))
        await provider.start(handler)
        try:
            yield provider
        finally:
            await provider.stop()

    @staticmethod
    async def _settled(
        provider: SchedulerProvider, run_id: str, *, states: tuple[str, ...]
    ) -> RunRecord:
        deadline = asyncio.get_running_loop().time() + SETTLE_SECONDS
        record: RunRecord | None = None
        while asyncio.get_running_loop().time() < deadline:
            record = await provider.get_run(run_id, execution_target="astrabox")
            if record is not None and record.state in states:
                return record
            await asyncio.sleep(0.2)
        raise AssertionError(f"Run {run_id} did not reach {states}; last record {record!r}")

    @staticmethod
    async def _scheduled_runs(provider: SchedulerProvider, deployment_id: str) -> list[RunRecord]:
        return [
            record
            for record in await provider.list_runs(deployment_id, limit=50, execution_target="astrabox")
            if record.invocation.trigger == "schedule"
        ]

    @staticmethod
    def _definition(deployment_id: str, **changes: Any) -> ScheduleDefinition:
        values: dict[str, Any] = {
            "deployment_id": deployment_id,
            "execution_target": "astrabox",
            "cron": "0 0 1 1 *",
            "timezone": "UTC",
            "enabled": True,
            "payload": {"agent_id": "agent-1", "input_text": "prepare the report"},
        }
        values.update(changes)
        return ScheduleDefinition(**values)

    async def test_a_manual_run_is_dispatched_through_the_handler(
        self, provider: SchedulerProvider, handler: RecordingHandler
    ) -> None:
        enqueued = await provider.enqueue(
            deployment_id="deployment-1",
            trigger="manual",
            payload={"agent_id": "agent-1", "input_text": "prepare the report"},
            execution_target="astrabox",
        )
        run_id = enqueued.invocation.run_id

        settled = await self._settled(provider, run_id, states=("DISPATCHED",))

        assert settled.session_id == f"session-{run_id}"
        assert settled.input_id == f"input-{run_id}"
        assert settled.invocation.trigger == "manual"
        assert settled.invocation.deployment_id == "deployment-1"
        assert settled.invocation.scheduled_for is None
        assert settled.invocation.payload["input_text"] == "prepare the report"
        # The input is admitted in the Session the first call returned, for the
        # same invocation.
        assert [call.run_id for call in handler.session_calls] == [run_id]
        assert handler.input_calls == [(handler.session_calls[0], f"session-{run_id}")]

    async def test_a_permanent_failure_is_recorded_without_a_retry(
        self, provider: SchedulerProvider, handler: RecordingHandler
    ) -> None:
        handler.permanent_failure = True

        enqueued = await provider.enqueue(
            deployment_id="deployment-1", trigger="manual", payload={"input_text": "x"},
            execution_target="astrabox",
        )
        settled = await self._settled(provider, enqueued.invocation.run_id, states=("FAILED",))

        assert settled.failed is True
        assert len(handler.session_calls) == 1
        assert handler.input_calls == []

    async def test_a_transient_failure_is_retried_with_the_same_invocation(
        self, provider: SchedulerProvider, handler: RecordingHandler
    ) -> None:
        handler.transient_failures = 1

        enqueued = await provider.enqueue(
            deployment_id="deployment-1", trigger="manual", payload={"input_text": "x"},
            execution_target="astrabox",
        )
        run_id = enqueued.invocation.run_id
        await self._settled(provider, run_id, states=("DISPATCHED",))

        assert [call.run_id for call in handler.session_calls] == [run_id, run_id]
        assert handler.session_calls[0] == handler.session_calls[1]

    async def test_a_replay_records_its_source_and_its_captured_payload(
        self, provider: SchedulerProvider
    ) -> None:
        source = await provider.enqueue(
            deployment_id="deployment-1",
            trigger="manual",
            payload={"input_text": "the original prompt"},
            execution_target="astrabox",
        )
        replay = await provider.enqueue(
            deployment_id="deployment-1",
            trigger="replay",
            payload={"input_text": "the original prompt"},
            replayed_from_run_id=source.invocation.run_id,
            execution_target="astrabox",
        )

        settled = await self._settled(
            provider, replay.invocation.run_id, states=("DISPATCHED",)
        )

        assert settled.invocation.trigger == "replay"
        assert settled.invocation.replayed_from_run_id == source.invocation.run_id
        assert settled.invocation.payload["input_text"] == "the original prompt"

    async def test_runs_are_listed_newest_first_and_per_deployment(
        self, provider: SchedulerProvider
    ) -> None:
        first = await provider.enqueue(
            deployment_id="deployment-1", trigger="manual", payload={"input_text": "a"},
            execution_target="astrabox",
        )
        await asyncio.sleep(0.01)
        second = await provider.enqueue(
            deployment_id="deployment-1", trigger="manual", payload={"input_text": "b"},
            execution_target="astrabox",
        )
        await provider.enqueue(
            deployment_id="deployment-2", trigger="manual", payload={"input_text": "c"},
            execution_target="astrabox",
        )

        listed = await provider.list_runs("deployment-1", limit=50, execution_target="astrabox")

        assert [record.invocation.run_id for record in listed] == [
            second.invocation.run_id,
            first.invocation.run_id,
        ]
        assert await provider.get_run("no-such-run", execution_target="astrabox") is None

    async def test_one_due_time_is_one_run_even_when_it_fires_twice(
        self, provider: SchedulerProvider
    ) -> None:
        await provider.apply(self._definition("deployment-1", cron="0 9 * * *"))
        start = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=2)

        await self.fire_due_times(provider, "deployment-1", start, end)
        await self.fire_due_times(provider, "deployment-1", start, end)

        scheduled = await self._scheduled_runs(provider, "deployment-1")
        assert len(scheduled) == 1
        assert scheduled[0].invocation.scheduled_for == datetime(
            2026, 1, 1, 9, 0, tzinfo=timezone.utc
        )
        assert scheduled[0].invocation.payload["input_text"] == "prepare the report"

    async def test_a_run_keeps_the_payload_it_was_created_with(
        self, provider: SchedulerProvider
    ) -> None:
        start = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        await provider.apply(
            self._definition(
                "deployment-1", cron="0 9 * * *", payload={"input_text": "first prompt"}
            )
        )
        await self.fire_due_times(provider, "deployment-1", start, start + timedelta(hours=2))

        await provider.apply(
            self._definition(
                "deployment-1", cron="0 9 * * *", payload={"input_text": "edited prompt"}
            )
        )
        next_day = start + timedelta(days=1)
        await self.fire_due_times(
            provider, "deployment-1", next_day, next_day + timedelta(hours=2)
        )

        scheduled = sorted(
            await self._scheduled_runs(provider, "deployment-1"),
            key=lambda record: record.invocation.scheduled_for or start,
        )
        assert [record.invocation.payload["input_text"] for record in scheduled] == [
            "first prompt",
            "edited prompt",
        ]

    async def test_a_disabled_schedule_does_not_fire_until_it_is_enabled(
        self, provider: SchedulerProvider
    ) -> None:
        start = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=2)
        await provider.apply(
            self._definition("deployment-1", cron="0 9 * * *", enabled=False)
        )

        await self.fire_due_times(provider, "deployment-1", start, end)
        assert await self._scheduled_runs(provider, "deployment-1") == []

        await provider.apply(self._definition("deployment-1", cron="0 9 * * *"))
        await self.fire_due_times(provider, "deployment-1", start, end)
        assert len(await self._scheduled_runs(provider, "deployment-1")) == 1

    async def test_an_invalid_expression_is_refused(
        self, provider: SchedulerProvider
    ) -> None:
        with pytest.raises(InvalidSchedule):
            await provider.apply(self._definition("deployment-1", cron="99 99 * * *"))

    async def test_a_refused_change_keeps_the_previous_schedule(
        self, provider: SchedulerProvider
    ) -> None:
        await provider.apply(self._definition("deployment-1", cron="0 9 * * *"))

        with pytest.raises(InvalidSchedule):
            await provider.apply(self._definition("deployment-1", cron="99 99 * * *"))
        start = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        await self.fire_due_times(provider, "deployment-1", start, start + timedelta(hours=2))

        scheduled = await self._scheduled_runs(provider, "deployment-1")
        assert [record.invocation.scheduled_for for record in scheduled] == [
            datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
        ]

    async def test_reconcile_removes_schedules_no_definition_names(
        self, provider: SchedulerProvider
    ) -> None:
        await provider.apply(self._definition("deployment-1"))
        await provider.apply(self._definition("deployment-orphan"))

        await provider.reconcile([self._definition("deployment-1", enabled=False)])
        start = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        for deployment_id in ("deployment-1", "deployment-orphan"):
            await self.fire_due_times(
                provider, deployment_id, start, start + timedelta(days=400)
            )

        # The orphan is gone and the listed schedule is kept, disabled.
        assert await provider.list_runs("deployment-orphan", limit=50, execution_target="astrabox") == []
        assert await self._scheduled_runs(provider, "deployment-1") == []

    async def test_delete_is_idempotent(self, provider: SchedulerProvider) -> None:
        await provider.apply(self._definition("deployment-1"))

        await provider.delete("deployment-1", execution_target="astrabox")
        await provider.delete("deployment-1", execution_target="astrabox")


__all__ = ["RecordingHandler", "SchedulerProviderContractSuite"]
