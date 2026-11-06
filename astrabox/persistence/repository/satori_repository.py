"""Transactional per-owner replay indexes; execution remains in SessionRuntime."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from itertools import groupby
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.common.utils.errors import APIError
from astrabox.persistence.sql.base import to_iso
from astrabox.persistence.sql.models.satori import SatoriEvent, SatoriFeed, SatoriPosition
from astrabox.persistence.sql.models.sessions import Session, SessionOutput, SessionResponse
from astrabox.persistence.sql.ops import fetch_all, fetch_value, transaction
from astrabox.persistence.sql.statements import insert_if_absent_primary_key

_FEEDS = SatoriFeed.__table__
_POSITIONS = SatoriPosition.__table__
_EVENTS = SatoriEvent.__table__
_SESSIONS = Session.__table__
_OUTPUT = SessionOutput.__table__
_RESPONSES = SessionResponse.__table__


def _feed_id(user_id: str, agent_id: str) -> str:
    return hashlib.sha256(json.dumps([user_id, agent_id], ensure_ascii=False).encode()).hexdigest()


class SatoriRepository:
    async def channels(self, user_id: str, agent_id: str, *, after: str = "", limit: int = 100) -> list[dict[str, Any]]:
        return await fetch_all(select(_SESSIONS.c.session_id, _SESSIONS.c.title).where(
            _SESSIONS.c.user_id == user_id, _SESSIONS.c.agent_id == agent_id,
            _SESSIONS.c.deleted.is_(False), _SESSIONS.c.hidden.is_(False),
            _SESSIONS.c.session_id > after,
        ).order_by(_SESSIONS.c.session_id).limit(limit))

    async def capture(self, user_id: str, agent_id: str) -> int:
        """Number this identity's new events and return the feed's last event number.

        The reading takes no lock and writes nothing, so an identity with
        nothing new touches no row; only the first capture of a feed creates
        it. A capture that found events commits them together with their
        positions in one short transaction, guarded by a compare-and-set of
        the feed's number from the value it read first. Every position write
        moves that number, so a capture that lost to another connection or
        replica writes nothing, and the winner's events cover what it saw.
        Neither a socket nor an application replica owns the sequence, and a
        rollback cannot leave a visible sequence gap.
        """
        fid = _feed_id(user_id, agent_id)
        seen = await transaction(lambda conn: self._read(conn, fid, user_id, agent_id))
        if seen is None:
            await transaction(lambda conn: conn.execute(insert_if_absent_primary_key(conn, _FEEDS, {
                "feed_id": fid, "user_id": user_id, "agent_id": agent_id,
            })))
            seen = await transaction(lambda conn: self._read(conn, fid, user_id, agent_id))
            assert seen is not None
        if not seen.events:
            return seen.sn

        async def commit(conn: AsyncConnection) -> bool:
            # Taken first: a concurrent capture waits here only for this short write.
            won = (await conn.execute(update(_FEEDS).where(
                _FEEDS.c.feed_id == fid, _FEEDS.c.last_sn == seen.sn,
            ).values(last_sn=seen.events[-1]["sn"]))).rowcount == 1
            if not won:
                return False
            if seen.missing:
                await conn.execute(insert(_POSITIONS), [{"feed_id": fid, "session_id": sid} for sid in seen.missing])
            for sid, values in seen.moved.items():
                await conn.execute(update(_POSITIONS).where(
                    _POSITIONS.c.feed_id == fid, _POSITIONS.c.session_id == sid,
                ).values(**values))
            await conn.execute(insert(_EVENTS), seen.events)
            return True

        if await transaction(commit):
            return int(seen.events[-1]["sn"])
        return await self.last_sn(user_id, agent_id)

    async def last_sn(self, user_id: str, agent_id: str) -> int:
        """The feed's last committed event number; 0 before its first capture."""
        value = await fetch_value(select(_FEEDS.c.last_sn).where(_FEEDS.c.feed_id == _feed_id(user_id, agent_id)))
        return int(value or 0)

    async def events(self, user_id: str, agent_id: str, *, after: int, limit: int = 100) -> list[dict[str, Any]]:
        """Committed events after ``after``, oldest first, for conversations the identity still sees.

        Captures commit in number order under the feed's compare-and-set, so a
        read sees a prefix of the sequence and never a gap.
        """
        rows = await fetch_all(select(_EVENTS).join(
            _SESSIONS, _SESSIONS.c.session_id == _EVENTS.c.session_id,
        ).where(
            _EVENTS.c.feed_id == _feed_id(user_id, agent_id), _EVENTS.c.sn > after,
            _SESSIONS.c.user_id == user_id, _SESSIONS.c.agent_id == agent_id,
            _SESSIONS.c.deleted.is_(False), _SESSIONS.c.hidden.is_(False),
        ).order_by(_EVENTS.c.sn).limit(limit))
        return [{**row, "created_at": to_iso(row["created_at"])} for row in rows]

    async def _read(self, conn: AsyncConnection, fid: str, user_id: str, agent_id: str) -> _Seen | None:
        """What a capture would commit, read without locks; None before the feed exists.

        The feed's number is read before the positions, so a capture committed
        in between moves the number this capture's commit compares against.
        """
        last = (await conn.execute(select(_FEEDS.c.last_sn).where(_FEEDS.c.feed_id == fid))).scalar_one_or_none()
        if last is None:
            return None
        sn = int(last)
        seen = _Seen(sn=sn)
        sessions = (await conn.execute(select(
            _SESSIONS.c.session_id, _SESSIONS.c.state, _SESSIONS.c.runtime_status,
        ).where(
            _SESSIONS.c.user_id == user_id, _SESSIONS.c.agent_id == agent_id,
            _SESSIONS.c.deleted.is_(False), _SESSIONS.c.hidden.is_(False),
        ))).mappings().all()
        ids = [row["session_id"] for row in sessions]
        positions: dict[str, dict[str, Any]] = {row["session_id"]: dict(row) for row in (await conn.execute(
            select(_POSITIONS).where(_POSITIONS.c.feed_id == fid)
        )).mappings()}
        for sid in ids:
            if sid not in positions:
                seen.missing.append(sid)
                positions[sid] = {"output_seq": 0, "state_digest": None}

        def append(sid: str, descriptor: dict[str, Any]) -> None:
            nonlocal sn
            sn += 1
            seen.events.append({"feed_id": fid, "sn": sn, "session_id": sid, "descriptor": descriptor})

        # Only unseen parts are read. The bounded batch is shared by all
        # conversations; no transcript scan or per-token payload copy. A new
        # conversation has no position row yet, so its parts all count as unseen.
        output = (await conn.execute(select(_OUTPUT).outerjoin(
            _POSITIONS, (_POSITIONS.c.session_id == _OUTPUT.c.session_id)
            & (_POSITIONS.c.feed_id == fid),
        ).where(
            _OUTPUT.c.session_id.in_(ids), _OUTPUT.c.seq > func.coalesce(_POSITIONS.c.output_seq, 0),
        ).order_by(_OUTPUT.c.session_id, _OUTPUT.c.seq).limit(500))).mappings().all() if ids else []
        for (sid, rid), group in groupby(output, lambda row: (row["session_id"], row["response_id"])):
            rows = list(group)
            append(sid, {"kind": "output", "response_id": rid, "first_seq": rows[0]["seq"], "through_seq": rows[-1]["seq"]})
            for row in rows:
                kind = row["part"].get("type")
                if rid and kind in {"start", "finish"}:
                    append(sid, {"kind": "message-created" if kind == "start" else "message-updated",
                                 "response_id": rid, "through_seq": row["seq"]})
            positions[sid] = {**positions[sid], "output_seq": rows[-1]["seq"]}
            seen.moved.setdefault(sid, {})["output_seq"] = rows[-1]["seq"]

        latest = select(_RESPONSES.c.session_id, func.max(_RESPONSES.c.seq).label("seq")).where(
            _RESPONSES.c.session_id.in_(ids),
        ).group_by(_RESPONSES.c.session_id).subquery()
        responses = {row["session_id"]: dict(row) for row in (await conn.execute(select(
            _RESPONSES.c.session_id, _RESPONSES.c.response_id, _RESPONSES.c.status,
            _RESPONSES.c.outcome, _RESPONSES.c.native_reason, _RESPONSES.c.error,
        ).join(latest, (_RESPONSES.c.session_id == latest.c.session_id) & (_RESPONSES.c.seq == latest.c.seq)))).mappings()} if ids else {}
        output_heads = dict((await conn.execute(select(
            _OUTPUT.c.session_id, func.max(_OUTPUT.c.seq),
        ).where(_OUTPUT.c.session_id.in_(ids)).group_by(_OUTPUT.c.session_id))).all()) if ids else {}
        for session in sessions:
            sid = session["session_id"]
            if positions[sid]["output_seq"] < output_heads.get(sid, 0):
                continue
            state = {**dict(session), "response": responses.get(sid)}
            digest = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
            if digest != positions[sid]["state_digest"]:
                append(sid, {"kind": "state", "state": state})
                seen.moved.setdefault(sid, {})["state_digest"] = digest
        return seen


@dataclass
class _Seen:
    """A capture's reading: the feed number it started from and what it would commit."""

    sn: int
    events: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    moved: dict[str, dict[str, Any]] = field(default_factory=dict)
