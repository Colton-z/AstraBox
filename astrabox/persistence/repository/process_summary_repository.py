"""The labels the platform writes over a response's folded work.

A process summary is never part of the conversation the runtime sees, so it
lives beside the output log in ``process_summaries``, keyed by the response it
describes. The row is also the generation claim: :meth:`ProcessSummaryRepository.claim`
inserts it, and a row that already exists means another writer holds or
finished the label.

A writer that dies leaves its row ``generating``; the row carries
``expires_at``, and :meth:`ProcessSummaryRepository.read` reports a claim past
it as ``failed``, which a retry may reopen. A reader therefore sees a failure it
can retry instead of work that nothing is doing.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import and_, case, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from astrabox.common.logger.logger_factory import get_logger
from astrabox.persistence.sql import fetch_all, fetch_value
from astrabox.persistence.sql.models.sessions import ProcessSummary

logger = get_logger(__name__)

_SUMMARIES = ProcessSummary.__table__

#: How long a generation claim lasts. A label is one short completion; a claim
#: still open after this lost its writer.
CLAIM_LEASE_S = 60

_EXPIRED_ERROR = "process summary generation expired"


def _lease_end() -> Any:
    return func.now() + func.make_interval(0, 0, 0, 0, 0, 0, CLAIM_LEASE_S)


def _expired() -> Any:
    return and_(_SUMMARIES.c.status == "generating", _SUMMARIES.c.expires_at < func.now())


class ProcessSummaryRepository:
    async def claim(
        self,
        session_id: str,
        response_id: str,
        *,
        through_seq: int,
        response_completed: bool,
        retry_failed: bool = False,
    ) -> str | None:
        """Take the generation claim for one response's label, in one statement.

        Returns the claim token, which :meth:`finish` must present, or ``None``
        when the row exists and is not reopened. ``retry_failed`` reopens a
        label that failed, or whose writer's claim expired; a completed label
        is never reopened.
        """
        token = uuid.uuid4().hex
        values = {
            "status": "generating",
            "claim_token": token,
            "through_seq": int(through_seq),
            "response_completed": bool(response_completed),
            "summary": None,
            "error": None,
            "expires_at": _lease_end(),
        }
        statement = pg_insert(_SUMMARIES).values(session_id=session_id, response_id=response_id, **values)
        statement = statement.on_conflict_do_update(
            index_elements=["session_id", "response_id"],
            set_={**values, "updated_at": func.now()},
            where=and_(literal(bool(retry_failed)), or_(_SUMMARIES.c.status == "failed", _expired())),
        ).returning(_SUMMARIES.c.claim_token)
        value = await fetch_value(statement)
        return str(value) if value is not None else None

    async def finish(
        self,
        session_id: str,
        response_id: str,
        *,
        claim_token: str,
        status: str,
        summary: str | None,
        error: str | None,
    ) -> bool:
        """Record the outcome of the claim ``claim_token`` names; ``False`` when the row holds another claim.

        A writer whose claim expired and was reopened by a retry finds another
        token on the row and writes nothing: the retry's outcome stands.
        """
        written = await fetch_all(
            update(_SUMMARIES)
            .where(
                _SUMMARIES.c.session_id == session_id,
                _SUMMARIES.c.response_id == response_id,
                _SUMMARIES.c.status == "generating",
                _SUMMARIES.c.claim_token == claim_token,
            )
            .values(status=status, summary=summary, error=error, expires_at=None, updated_at=func.now())
            .returning(_SUMMARIES.c.response_id)
        )
        if not written:
            logger.warning(
                "process summary outcome discarded: claim no longer current session=%s response=%s",
                session_id,
                response_id,
            )
        return bool(written)

    async def read(self, session_id: str, response_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """The stored label of each response that has one.

        A claim past its expiry reads as ``failed``.
        """
        if not response_ids:
            return {}
        expired = _expired()
        rows = await fetch_all(
            select(
                _SUMMARIES.c.response_id,
                case((expired, literal("failed")), else_=_SUMMARIES.c.status).label("status"),
                _SUMMARIES.c.summary,
                case((expired, literal(_EXPIRED_ERROR)), else_=_SUMMARIES.c.error).label("error"),
                _SUMMARIES.c.through_seq,
                _SUMMARIES.c.response_completed,
            ).where(
                _SUMMARIES.c.session_id == session_id,
                _SUMMARIES.c.response_id.in_(list(response_ids)),
            )
        )
        return {str(row.pop("response_id")): row for row in rows}
