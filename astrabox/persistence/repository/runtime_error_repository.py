"""Captured runtime errors for the admin errors page, one row per error signature."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from astrabox.persistence.sql import execute, fetch_all, to_iso
from astrabox.persistence.sql.models.platform import CapturedRuntimeError

#: An error that has not recurred for this long is deleted.
RUNTIME_ERROR_RETENTION = timedelta(days=7)

#: Most rows one retention pass deletes, so a pass is one short statement;
#: the rest go on the following passes.
RETENTION_BATCH = 1000

_ERRORS = CapturedRuntimeError.__table__


class RuntimeErrorRepository:
    """Counted ERROR records keyed by signature, newest sighting first."""

    async def record(
        self,
        *,
        signature: str,
        logger: str,
        severity: str,
        error_type: str | None,
        message: str,
        location: str,
        traceback: str | None,
        seen: datetime,
    ) -> None:
        """Count one sighting against ``signature``, creating its row on the first.

        One statement, so replicas recording the same new error at once each
        count, and the row keeps the earliest and latest sighting whatever
        order the sightings arrive in.
        """
        statement = insert(_ERRORS).values(
            signature=signature,
            logger=logger,
            severity=severity,
            error_type=error_type,
            message=message,
            location=location,
            traceback=traceback,
            count=1,
            first_seen=seen,
            last_seen=seen,
        )
        await execute(
            statement.on_conflict_do_update(
                index_elements=[_ERRORS.c.signature],
                set_={
                    "count": _ERRORS.c.count + 1,
                    "first_seen": func.least(_ERRORS.c.first_seen, statement.excluded.first_seen),
                    "last_seen": func.greatest(_ERRORS.c.last_seen, statement.excluded.last_seen),
                    "severity": statement.excluded.severity,
                    "message": statement.excluded.message,
                    "traceback": statement.excluded.traceback,
                },
            )
        )

    async def list_recent(self, limit: int) -> list[dict[str, Any]]:
        """The ``limit`` most recently seen errors, newest first."""
        rows = await fetch_all(
            select(_ERRORS)
            .order_by(_ERRORS.c.last_seen.desc(), _ERRORS.c.signature)
            .limit(limit)
        )
        return [
            {**row, "first_seen": to_iso(row["first_seen"]), "last_seen": to_iso(row["last_seen"])}
            for row in rows
        ]

    async def delete_expired(self) -> int:
        """Delete up to :data:`RETENTION_BATCH` errors not seen for the retention period.

        The oldest go first. Returns how many were deleted.
        """
        cutoff = datetime.now(timezone.utc) - RUNTIME_ERROR_RETENTION
        expired = (
            select(_ERRORS.c.signature)
            .where(_ERRORS.c.last_seen < cutoff)
            .order_by(_ERRORS.c.last_seen)
            .limit(RETENTION_BATCH)
            # Skips a row an arriving sighting is updating, and holds the rows
            # it takes, so a sighting is never counted into a row being deleted
            # and replicas running this together delete disjoint rows.
            .with_for_update(skip_locked=True)
        )
        return await execute(delete(_ERRORS).where(_ERRORS.c.signature.in_(expired)))
