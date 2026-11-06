"""Transactional per-owner replay indexes; execution remains in SessionRuntime."""

from __future__ import annotations

import hashlib
import json
from itertools import groupby
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.common.utils.errors import APIError
from astrabox.persistence.sql.base import to_iso
from astrabox.persistence.sql.models.satori import SatoriEvent, SatoriFeed, SatoriPosition
from astrabox.persistence.sql.models.sessions import Session, SessionOutput, SessionResponse
from astrabox.persistence.sql.ops import fetch_all, transaction
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

    async def poll(self, user_id: str, agent_id: str, *, after: int | None) -> tuple[int, list[dict[str, Any]]]:
        """Commit event numbers with their positions under one feed lock, then replay.

        A disconnected feed catches up from immutable core output. State events
        are observations of the current core records; offline intermediate
        states coalesce. Neither a socket nor an application replica owns the
        sequence, and a rollback cannot leave a visible sequence gap.
        """
        fid = _feed_id(user_id, agent_id)

        async def capture(conn: AsyncConnection) -> tuple[int, list[dict[str, Any]]]:
            await conn.execute(insert_if_absent_primary_key(conn, _FEEDS, {
                "feed_id": fid, "user_id": user_id, "agent_id": agent_id,
            }))
            sn = int((await conn.execute(
                select(_FEEDS.c.last_sn).where(_FEEDS.c.feed_id == fid).with_for_update()
            )).scalar_one())
            if after is not None and (after < 0 or after > sn):
                raise APIError(code="INVALID_REQUEST", message="Satori sn is outside this identity's feed", status_code=400)
            sessions = (await conn.execute(select(
                _SESSIONS.c.session_id, _SESSIONS.c.state, _SESSIONS.c.runtime_status,
            ).where(
                _SESSIONS.c.user_id == user_id, _SESSIONS.c.agent_id == agent_id,
                _SESSIONS.c.deleted.is_(False), _SESSIONS.c.hidden.is_(False),
            ))).mappings().all()
            ids = [row["session_id"] for row in sessions]
            positions = {row["session_id"]: row for row in (await conn.execute(
                select(_POSITIONS).where(_POSITIONS.c.feed_id == fid)
            )).mappings()}
            missing = [sid for sid in ids if sid not in positions]
            if missing:
                await conn.execute(insert(_POSITIONS), [{"feed_id": fid, "session_id": sid} for sid in missing])
                positions.update({sid: {"output_seq": 0, "state_digest": None} for sid in missing})
            events: list[dict[str, Any]] = []

            def append(sid: str, descriptor: dict[str, Any]) -> None:
                nonlocal sn
                sn += 1
                events.append({"feed_id": fid, "sn": sn, "session_id": sid, "descriptor": descriptor})

            # Only unseen parts are read. The bounded batch is shared by all
            # conversations; no transcript scan or per-token payload copy.
            output = (await conn.execute(select(_OUTPUT).join(
                _POSITIONS, (_POSITIONS.c.session_id == _OUTPUT.c.session_id)
                & (_POSITIONS.c.feed_id == fid),
            ).where(
                _OUTPUT.c.session_id.in_(ids), _OUTPUT.c.seq > _POSITIONS.c.output_seq,
            ).order_by(_OUTPUT.c.session_id, _OUTPUT.c.seq).limit(500))).mappings().all() if ids else []
            for (sid, rid), group in groupby(output, lambda row: (row["session_id"], row["response_id"])):
                rows = list(group)
                descriptor = {"kind": "output", "response_id": rid, "first_seq": rows[0]["seq"], "through_seq": rows[-1]["seq"]}
                append(sid, descriptor)
                for row in rows:
                    kind = row["part"].get("type")
                    if rid and kind in {"start", "finish"}:
                        append(sid, {"kind": "message-created" if kind == "start" else "message-updated",
                                     "response_id": rid, "through_seq": row["seq"]})
                await conn.execute(update(_POSITIONS).where(
                    _POSITIONS.c.feed_id == fid, _POSITIONS.c.session_id == sid,
                ).values(output_seq=rows[-1]["seq"]))
                positions[sid] = {**positions[sid], "output_seq": rows[-1]["seq"]}

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
                    await conn.execute(update(_POSITIONS).where(
                        _POSITIONS.c.feed_id == fid, _POSITIONS.c.session_id == sid,
                    ).values(state_digest=digest))
            if events:
                await conn.execute(insert(_EVENTS), events)
                await conn.execute(update(_FEEDS).where(_FEEDS.c.feed_id == fid).values(last_sn=sn))
            if after is None:
                return sn, []
            rows = (await conn.execute(select(_EVENTS).join(
                _SESSIONS, _SESSIONS.c.session_id == _EVENTS.c.session_id,
            ).where(
                _EVENTS.c.feed_id == fid, _EVENTS.c.sn > after,
                _SESSIONS.c.user_id == user_id, _SESSIONS.c.agent_id == agent_id,
                _SESSIONS.c.deleted.is_(False), _SESSIONS.c.hidden.is_(False),
            ).order_by(_EVENTS.c.sn).limit(100))).mappings()
            return sn, [{**row, "created_at": to_iso(row["created_at"])} for row in rows]

        return await transaction(capture)
