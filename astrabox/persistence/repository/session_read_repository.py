"""Page reads of a session's recorded output beyond the conversation itself.

:class:`~astrabox.persistence.repository.session_runtime_repository.SessionRuntimeRepository`
reads the output log the way a conversation page and a follower read it. These
statements serve the page's other surfaces from the same rows:

* where each response's parts end, so a page pinned to an output position can
  tell a response that had ended by then from one that was still open;
* the responses that ended inside a window of the log, and the parts of one
  type those responses wrote (the file changes a Diff panel shows);
* the child-run facts the runtime reported (``data-subagent`` parts);
* the inputs one response answers, and the interaction a page asks its
  reader to answer;
* the runtime's own transcripts of its sub-agents, the SessionStore scopes
  below the conversation's main transcript.

Every statement only reads. Rows are returned as dicts keyed by column name.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, case, func, literal, select

from astrabox.persistence.sql import fetch_all, fetch_one, to_iso
from astrabox.persistence.sql.runtime_expressions import json_without_key
from astrabox.persistence.sql.models.sessions import (
    SessionInput,
    SessionInteraction,
    SessionOutput,
    SessionResponse,
    SessionResponseInput,
    TranscriptEntry,
    TranscriptScope,
)

_OUTPUT = SessionOutput.__table__
_RESPONSES = SessionResponse.__table__
_RESPONSE_INPUTS = SessionResponseInput.__table__
_INPUTS = SessionInput.__table__
_INTERACTIONS = SessionInteraction.__table__
_SCOPES = TranscriptScope.__table__
_ENTRIES = TranscriptEntry.__table__

#: The part type an engine adapter reports a child run's lifecycle and messages with.
CHILD_RUN_PART = "data-subagent"


def _row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: (to_iso(value) if isinstance(value, datetime) else value) for key, value in row.items()}


class SessionReadRepository:
    async def last_output_seqs(self, session_id: str, response_ids: Sequence[str]) -> dict[str, int]:
        """The ``seq`` of each response's last stored part; a response with none is absent."""
        if not response_ids:
            return {}
        rows = await fetch_all(
            select(_OUTPUT.c.response_id, func.max(_OUTPUT.c.seq).label("last"))
            .where(_OUTPUT.c.session_id == session_id, _OUTPUT.c.response_id.in_(list(response_ids)))
            .group_by(_OUTPUT.c.response_id)
        )
        return {str(row["response_id"]): int(row["last"]) for row in rows}

    async def responses_ended_between(
        self, session_id: str, *, after_seq: int, through_seq: int
    ) -> list[str]:
        """Ended responses whose last part lies in ``(after_seq, through_seq]``, in log order.

        A response's last part is the one its end was recorded with: the reader
        commits the parts that close a response in the batch that ends it.
        """
        in_window = (
            select(_OUTPUT.c.response_id)
            .where(
                _OUTPUT.c.session_id == session_id,
                _OUTPUT.c.seq > after_seq,
                _OUTPUT.c.seq <= through_seq,
                _OUTPUT.c.response_id.isnot(None),
            )
            .distinct()
        )
        last = (
            select(_OUTPUT.c.response_id, func.max(_OUTPUT.c.seq).label("last"))
            .where(_OUTPUT.c.session_id == session_id, _OUTPUT.c.response_id.in_(in_window))
            .group_by(_OUTPUT.c.response_id)
            .having(func.max(_OUTPUT.c.seq) <= through_seq)
            .subquery("last")
        )
        rows = await fetch_all(
            select(last.c.response_id)
            .join(
                _RESPONSES,
                and_(_RESPONSES.c.session_id == session_id, _RESPONSES.c.response_id == last.c.response_id),
            )
            .where(_RESPONSES.c.status == "ended")
            .order_by(last.c.last)
        )
        return [str(row["response_id"]) for row in rows]

    async def parts_of_type(
        self, session_id: str, part_type: str, *, response_ids: Sequence[str], through_seq: int
    ) -> list[dict[str, Any]]:
        """These responses' parts of one type through ``through_seq``, in log order."""
        if not response_ids:
            return []
        return await fetch_all(
            select(_OUTPUT.c.seq, _OUTPUT.c.response_id, _OUTPUT.c.part)
            .where(
                _OUTPUT.c.session_id == session_id,
                _OUTPUT.c.response_id.in_(list(response_ids)),
                _OUTPUT.c.seq <= through_seq,
                _OUTPUT.c.part["type"].as_string() == part_type,
            )
            .order_by(_OUTPUT.c.seq)
        )

    async def child_run_parts(
        self, session_id: str, *, engine_ref: str | None = None, with_content: bool = True
    ) -> list[dict[str, Any]]:
        """The session's child-run parts in log order.

        ``engine_ref`` keeps one child's parts. Without ``with_content`` a
        message part carries no ``content``: a list of child runs needs to know
        that a child spoke and under which parent, not what it said, and the
        console polls that list while children run.
        """
        data = _OUTPUT.c.part["data"]
        conditions: list[ColumnElement[bool]] = [
            _OUTPUT.c.session_id == session_id,
            _OUTPUT.c.part["type"].as_string() == CHILD_RUN_PART,
        ]
        if engine_ref is not None:
            conditions.append(data["engineRef"].as_string() == engine_ref)
        projected: Any = data
        if not with_content:
            projected = case(
                (data["kind"].as_string() == "message", json_without_key(data, literal("content"))),
                else_=data,
            )
        rows = await fetch_all(
            select(
                _OUTPUT.c.seq,
                _OUTPUT.c.part["id"].as_string().label("part_id"),
                projected.label("data"),
            )
            .where(*conditions)
            .order_by(_OUTPUT.c.seq)
        )
        return [{"seq": int(row["seq"]), "id": row["part_id"], "data": row["data"]} for row in rows]

    async def inputs_for_response(self, session_id: str, response_id: str) -> list[dict[str, Any]]:
        """The inputs a response answers, in acceptance order."""
        rows = await fetch_all(
            select(_INPUTS)
            .join(
                _RESPONSE_INPUTS,
                and_(
                    _RESPONSE_INPUTS.c.input_id == _INPUTS.c.input_id,
                    _RESPONSE_INPUTS.c.session_id == session_id,
                    _RESPONSE_INPUTS.c.response_id == response_id,
                ),
            )
            .order_by(_INPUTS.c.seq)
        )
        return [_row(row) for row in rows]

    async def waiting_interaction(self, session_id: str) -> dict[str, Any] | None:
        """The oldest open interaction nobody has answered yet."""
        row = await fetch_one(
            select(_INTERACTIONS)
            .where(
                _INTERACTIONS.c.session_id == session_id,
                _INTERACTIONS.c.status == "open",
                _INTERACTIONS.c.answer.is_(None),
            )
            .order_by(_INTERACTIONS.c.opened_at)
            .limit(1)
        )
        return _row(row) if row is not None else None

    async def child_transcript_scopes(self, session_id: str) -> list[dict[str, Any]]:
        """The runtime's sub-agent transcripts for the session, each with its entries in order.

        A scope is ``{"project_key", "session_id", "subpath", "entries"}``,
        where ``session_id`` is the runtime's own conversation id and
        ``entries`` are the SessionStore entries as the runtime wrote them. The
        main transcript (``subpath`` ``""``) is not included.
        """
        scopes = await fetch_all(
            select(_SCOPES.c.scope_id, _SCOPES.c.project_key, _SCOPES.c.native_session_id, _SCOPES.c.subpath)
            .where(_SCOPES.c.platform_session_id == session_id, _SCOPES.c.subpath != "")
            .order_by(_SCOPES.c.subpath)
        )
        if not scopes:
            return []
        entries = await fetch_all(
            select(_ENTRIES.c.scope_id, _ENTRIES.c.entry_json)
            .where(_ENTRIES.c.scope_id.in_([row["scope_id"] for row in scopes]))
            .order_by(_ENTRIES.c.scope_id, _ENTRIES.c.seq)
        )
        by_scope: dict[str, list[dict[str, Any]]] = {}
        for row in entries:
            by_scope.setdefault(str(row["scope_id"]), []).append(json.loads(row["entry_json"]))
        return [
            {
                "project_key": row["project_key"],
                "session_id": row["native_session_id"],
                "subpath": row["subpath"],
                "entries": by_scope.get(str(row["scope_id"]), []),
            }
            for row in scopes
        ]
