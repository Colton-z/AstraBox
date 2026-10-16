"""Per-viewer artifact budgets: room token buckets, sample and Agent-run reservations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import Table, and_, case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.repository._artifact_repository import every, one, run
from astrabox.persistence.sql import to_iso, transaction
from astrabox.persistence.sql.models.artifacts import (
    ArtifactBudget,
    ArtifactRunReservation,
    ArtifactSampleReservation,
)
from astrabox.persistence.sql.statements import insert_if_absent_primary_key

_BUDGETS = cast(Table, ArtifactBudget.__table__)
_SAMPLES = cast(Table, ArtifactSampleReservation.__table__)
_RUNS = cast(Table, ArtifactRunReservation.__table__)


class ArtifactBudgetRepository:
    """Budget rows keyed by a key derived from the viewer, so every replica shares them."""

    async def take_room_token(
        self,
        budget_key: str,
        *,
        now: datetime,
        burst: int,
        per_second: float,
        conn: AsyncConnection,
    ) -> bool:
        """Spend one token of the viewer's room bucket; False when it is empty.

        The bucket holds up to ``burst`` tokens and refills at ``per_second``.
        First use creates the row; refill and spend hold its exclusive lock
        on the caller's transaction, serializing writes from every replica.
        """
        await run(
            insert_if_absent_primary_key(conn, _BUDGETS, {
                "budget_key": budget_key, "kind": "room", "tokens": float(burst), "updated_at": now,
            }),
            conn,
        )
        row = await one(
            select(_BUDGETS)
            .where(_BUDGETS.c.budget_key == budget_key)
            .with_for_update(key_share=True),
            conn,
        )
        assert row is not None
        elapsed = max(0.0, (now - row["updated_at"]).total_seconds())
        refilled = min(float(burst), row["tokens"] + elapsed * per_second)
        if refilled < 1:
            return False
        await run(
            update(_BUDGETS)
            .where(_BUDGETS.c.budget_key == budget_key)
            .values(tokens=refilled - 1, updated_at=now),
            conn,
        )
        return True

    async def prune_room_tokens(self, *, before: datetime, limit: int) -> int:
        """Delete up to ``limit`` room buckets last spent before ``before``.

        ``before`` must be at least a full refill ago: such a bucket is full,
        the same as having no row.
        """
        async def prune(conn: AsyncConnection) -> int:
            idle = await every(
                select(_BUDGETS.c.budget_key)
                .where(_BUDGETS.c.kind == "room", _BUDGETS.c.updated_at < before)
                .limit(limit)
                .with_for_update(skip_locked=True),
                conn,
            )
            if not idle:
                return 0
            return await run(
                delete(_BUDGETS).where(_BUDGETS.c.budget_key.in_([row["budget_key"] for row in idle])),
                conn,
            )

        return await transaction(prune)

    async def lock(self, budget_key: str, kind: str, *, conn: AsyncConnection) -> None:
        """Hold the viewer's ``kind`` budget until ``conn`` commits, creating it on first use.

        Concurrent first uses do not collide: the insert of a row another
        transaction is creating waits for it and then does nothing.
        """
        await run(
            insert_if_absent_primary_key(conn, _BUDGETS, {"budget_key": budget_key, "kind": kind}),
            conn,
        )
        await one(
            select(_BUDGETS.c.budget_key)
            .where(_BUDGETS.c.budget_key == budget_key)
            .with_for_update(key_share=True),
            conn,
        )

    # ── sample reservations ──────────────────────────────────────────────

    async def sample_counts(
        self, budget_key: str, *, now: datetime, window_start: datetime, conn: AsyncConnection
    ) -> tuple[int, int]:
        """``(active, recent)``: unreleased unexpired reservations, and those started since ``window_start``."""
        row = await one(
            select(
                func.count(case(
                    (and_(_SAMPLES.c.released_at.is_(None), _SAMPLES.c.expires_at > now), 1)
                )).label("active"),
                func.count(case((_SAMPLES.c.created_at > window_start, 1))).label("recent"),
            ).where(_SAMPLES.c.budget_key == budget_key),
            conn,
        )
        assert row is not None
        return int(row["active"]), int(row["recent"])

    async def prune_samples(
        self, budget_key: str, *, now: datetime, window_start: datetime, conn: AsyncConnection
    ) -> None:
        """Delete the viewer's reservations that neither hold a slot nor count toward the window."""
        await run(
            delete(_SAMPLES).where(
                _SAMPLES.c.budget_key == budget_key,
                _SAMPLES.c.created_at <= window_start,
                (_SAMPLES.c.released_at.is_not(None)) | (_SAMPLES.c.expires_at <= now),
            ),
            conn,
        )

    async def add_sample(
        self,
        budget_key: str,
        reservation_id: str,
        *,
        created_at: datetime,
        expires_at: datetime,
        conn: AsyncConnection,
    ) -> None:
        await run(
            _SAMPLES.insert().values(
                reservation_id=reservation_id,
                budget_key=budget_key,
                created_at=created_at,
                expires_at=expires_at,
            ),
            conn,
        )

    async def release_sample(self, reservation_id: str, *, now: datetime) -> None:
        """Free the reservation's concurrency slot; it still counts toward its minute.

        Releasing twice, or after the reservation expired and was deleted,
        changes nothing.
        """
        await run(
            update(_SAMPLES)
            .where(_SAMPLES.c.reservation_id == reservation_id, _SAMPLES.c.released_at.is_(None))
            .values(released_at=now),
            None,
        )

    # ── Agent-run reservations ───────────────────────────────────────────

    async def run_session_ids(self, budget_key: str) -> list[str]:
        """The Sessions of the viewer's reservations that have one."""
        rows = await every(
            select(_RUNS.c.session_id).where(
                _RUNS.c.budget_key == budget_key, _RUNS.c.session_id.is_not(None)
            ),
            None,
        )
        return [row["session_id"] for row in rows]

    async def runs(self, budget_key: str, *, conn: AsyncConnection) -> list[dict[str, Any]]:
        """The viewer's reservations: ``{reservation_id, artifact_id, session_id, created_at}``."""
        rows = await every(
            select(_RUNS.c.reservation_id, _RUNS.c.artifact_id, _RUNS.c.session_id, _RUNS.c.created_at)
            .where(_RUNS.c.budget_key == budget_key)
            .order_by(_RUNS.c.created_at),
            conn,
        )
        return [{**row, "created_at": to_iso(row["created_at"])} for row in rows]

    async def add_run(
        self,
        budget_key: str,
        reservation_id: str,
        *,
        artifact_id: str,
        created_at: datetime,
        conn: AsyncConnection,
    ) -> None:
        await run(
            _RUNS.insert().values(
                reservation_id=reservation_id,
                budget_key=budget_key,
                artifact_id=artifact_id,
                created_at=created_at,
            ),
            conn,
        )

    async def settle_run(self, reservation_id: str, session_id: str) -> None:
        """Attach the conversation the reservation started; its state decides activity from now on."""
        await run(
            update(_RUNS).where(_RUNS.c.reservation_id == reservation_id).values(session_id=session_id),
            None,
        )

    async def drop_runs(
        self, reservation_ids: list[str], *, conn: AsyncConnection | None = None
    ) -> None:
        if reservation_ids:
            await run(delete(_RUNS).where(_RUNS.c.reservation_id.in_(reservation_ids)), conn)
