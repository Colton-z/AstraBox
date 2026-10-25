"""Persistence for sessions: identity, ownership, lifecycle and the runtime binding.

Rows are handed to callers as dicts keyed by column name, with times as
ISO-8601 strings and derived keys: ``workspace_ref`` (the subject the session
runs as), ``vault_ids`` (its vault bindings in priority order), and ``agent_state``
(the referenced Agent's lifecycle).
Writes name columns; a key that is not a column raises ``KeyError``, so a write
to a field that does not exist fails where it is made.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    JSON,
    Update,
    and_,
    delete,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import Executable, Select

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.config.settings import get_settings, validate_environment_name
from astrabox.core.model import SessionState
from astrabox.persistence.repository.keyset import InvalidListCursor
from astrabox.persistence.sql import (
    UniqueViolation,
    execute,
    fetch_all,
    fetch_one,
    fetch_value,
    from_iso,
    to_iso,
    transaction,
)
from astrabox.persistence.sql.models.agents import Agent, AgentRuntime
from astrabox.persistence.sql.statements import insert_if_absent
from astrabox.persistence.sql.models.sessions import (
    PlatformMCPBinding,
    Session,
    SessionVaultBinding,
)
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.sql.runtime_expressions import json_array_matches, json_literal, ordered_values
from astrabox.seams.persistence_coordination import NoticeKind, SessionNotice

logger = get_logger(__name__)


_SESSIONS = Session.__table__
_VAULTS = SessionVaultBinding.__table__
_MCP_BINDINGS = PlatformMCPBinding.__table__

_COLUMNS = {column.name: column for column in _SESSIONS.columns}
_TIME_COLUMNS = {
    name for name, column in _COLUMNS.items() if column.type.python_type is datetime
}
_TERMINAL_STATES = (SessionState.TERMINATED.value, SessionState.DELETED.value)

#: ``vault_ids`` in priority order, read in the same statement as the row.
_VAULT_IDS = (
    select(ordered_values(_VAULTS.c.vault_id, _VAULTS.c.position))
    .where(_VAULTS.c.session_id == _SESSIONS.c.session_id)
    .limit(1)
    .scalar_subquery()
)


def _encode_session_list_cursor(*, updated_at: str, session_id: str) -> str:
    payload = {"updated_at": updated_at, "session_id": session_id}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_session_list_cursor(cursor: str | None) -> dict[str, str] | None:
    value = str(cursor or "").strip()
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise InvalidListCursor("invalid session list cursor") from exc
    if not isinstance(payload, dict):
        raise InvalidListCursor("invalid session list cursor")
    updated_at = str(payload.get("updated_at") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    if not updated_at or not session_id:
        raise InvalidListCursor("invalid session list cursor")
    return {"updated_at": updated_at, "session_id": session_id}


def session_view(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A stored row as callers see it: ISO times plus ``workspace_ref``."""
    if row is None:
        return None
    view = {key: (to_iso(value) if key in _TIME_COLUMNS else value) for key, value in row.items()}
    ref: dict[str, Any] = {"kind": view.get("workspace_kind"), "user_id": view.get("user_id")}
    for key in ("agent_id", "assistant_id", "engine_kind", "sandbox_id"):
        if view.get(key):
            ref[key] = view[key]
    view["workspace_ref"] = ref
    if view.get("vault_ids") is None:
        view["vault_ids"] = []
    return view


def _column_values(values: Mapping[str, Any]) -> dict[str, Any]:
    stored: dict[str, Any] = {}
    for key, value in values.items():
        if key not in _COLUMNS:
            raise KeyError(f"sessions has no column {key!r}")
        stored[key] = from_iso(value) if key in _TIME_COLUMNS else value
    return stored


def _condition(key: str, value: Any) -> ColumnElement[bool]:
    """One ``expected`` entry of :meth:`SessionRepository.compare_and_update_session`."""
    if key not in _COLUMNS:
        raise KeyError(f"sessions has no column {key!r}")
    column = _COLUMNS[key]
    if isinstance(value, dict) and set(value) == {"$in"}:
        return or_(*(_condition(key, item) for item in value["$in"]))
    if isinstance(value, dict) and set(value) == {"$exists"} and value["$exists"] is False:
        return column.is_(None)
    if isinstance(value, dict) and set(value) == {"$ne"}:
        expected = from_iso(value["$ne"]) if key in _TIME_COLUMNS else value["$ne"]
        if isinstance(column.type, JSON) and expected is not None:
            return column.is_distinct_from(json_literal(expected))
        return column.is_distinct_from(expected)
    if value is None:
        return column.is_(None)
    if key in _TIME_COLUMNS:
        return column == from_iso(value)
    if isinstance(column.type, JSON):
        return column == json_literal(value)
    return column == value


def _selected() -> Select[Any]:
    agent_state = (
        select(Agent.state).where(Agent.agent_id == _SESSIONS.c.agent_id).scalar_subquery()
    )
    return select(_SESSIONS, _VAULT_IDS.label("vault_ids"), agent_state.label("agent_state"))


async def _one(where: ColumnElement[bool]) -> dict[str, Any] | None:
    return session_view(await fetch_one(_selected().where(where)))


async def _many(statement: Select[Any]) -> list[dict[str, Any]]:
    return [view for view in map(session_view, await fetch_all(statement)) if view is not None]


def _live() -> ColumnElement[bool]:
    return _SESSIONS.c.deleted.is_(False)


def _bound_and_open() -> ColumnElement[bool]:
    return and_(
        _live(),
        _SESSIONS.c.runtime_unavailable.is_(False),
        func.coalesce(_SESSIONS.c.sandbox_id, "") != "",
        _SESSIONS.c.state.notin_(_TERMINAL_STATES),
    )


async def _retained_matches(
    conn: AsyncConnection, retained: list[Any], record: dict[str, Any],
) -> list[bool]:
    """Compare complete JSON values using native structural equality."""
    if not retained:
        return []
    matches = (await conn.execute(select(
        json_array_matches(json_literal(retained), json_literal(dict(record)))
    ))).scalar_one()
    return [bool(matched) for matched in matches]


async def _changed(statement: Update, session_id: str, notify: NoticeKind | None) -> bool:
    """Run one Session update, enqueuing its notice only if it matched."""
    if notify is None:
        return await execute(statement) > 0

    async def write(conn: AsyncConnection) -> bool:
        changed = (await conn.execute(statement)).rowcount > 0
        if changed:
            await get_persistence().coordination.notify(
                conn, SessionNotice(session_id=session_id, kind=notify, seq=0),
            )
        return changed

    return await transaction(write)


async def _notify_rows(statement: Executable, kind: NoticeKind) -> bool:
    """Enqueue a notice for each Session id yielded by this statement."""
    async def write(conn: AsyncConnection) -> bool:
        ids = (await conn.execute(statement)).scalars().all()
        for session_id in ids:
            await get_persistence().coordination.notify(
                conn, SessionNotice(session_id=str(session_id), kind=kind, seq=0),
            )
        return bool(ids)

    return await transaction(write)


def _workspace_columns(payload: Mapping[str, Any]) -> dict[str, Any]:
    ref = payload.get("workspace_ref")
    ref = ref if isinstance(ref, Mapping) else {}
    kind = str(ref.get("kind") or ("assistant" if payload.get("assistant_id") else "agent"))
    return {
        "workspace_kind": kind,
        "agent_id": payload.get("agent_id") or ref.get("agent_id"),
        "assistant_id": payload.get("assistant_id") or ref.get("assistant_id"),
    }


class SessionRepository:
    """Session rows. Reads hide soft-deleted rows unless they say otherwise."""

    def __init__(self, *, environment_name: str | None = None) -> None:
        self.environment_name = validate_environment_name(
            get_settings().environment_name if environment_name is None else environment_name
        )

    def _runtime_owner(self, session_id: str) -> ColumnElement[bool]:
        return and_(
            _SESSIONS.c.session_id == session_id,
            _SESSIONS.c.environment_name == self.environment_name,
        )

    async def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Insert a session; ``payload`` names columns plus ``workspace_ref`` and ``vault_ids``.

        Creating an id that already exists returns the existing row: the id is
        derived from an idempotency key when the caller has one.
        """
        values = dict(payload)
        if "environment_name" in values and values["environment_name"] != self.environment_name:
            raise ValueError("a Session must be created in this repository's Environment")
        values["environment_name"] = self.environment_name
        vault_ids = [str(v) for v in values.pop("vault_ids", None) or [] if str(v or "").strip()]
        workspace = _workspace_columns(values)
        values.pop("workspace_ref", None)
        values.update(workspace)
        stored = _column_values(values)
        session_id = str(stored["session_id"])

        async def write(conn: AsyncConnection) -> None:
            if stored.get("agent_id"):
                await insert_if_absent(
                    conn, AgentRuntime.__table__,
                    {"agent_id": stored["agent_id"], "environment_name": self.environment_name},
                    index_columns=["agent_id", "environment_name"],
                )
            await conn.execute(insert(_SESSIONS).values(**stored))
            if vault_ids:
                await conn.execute(
                    insert(_VAULTS),
                    [
                        {"session_id": session_id, "position": index, "vault_id": vault_id}
                        for index, vault_id in enumerate(vault_ids)
                    ],
                )

        try:
            await transaction(write)
        except UniqueViolation:
            logger.info("session %s already exists; returning it", session_id)
        created = await self.get_session_including_deleted(session_id)
        if created is None:
            raise RuntimeError(f"session {session_id!r} was not stored")
        if created["environment_name"] != self.environment_name:
            raise APIError(
                code="ENVIRONMENT_MISMATCH", status_code=409,
                message="existing Session belongs to a different Environment",
                data={"environment_name": created["environment_name"],
                      "server_environment_name": self.environment_name},
            )
        return created

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        return await _one(and_(_SESSIONS.c.session_id == session_id, _live()))

    async def get_session_including_deleted(self, session_id: str) -> dict[str, Any] | None:
        """One exact id including soft-deleted rows, for create-idempotency checks.

        A caller retrying a create must not reuse a deleted session's identity
        for a different subject, so this read sees tombstones; user-facing
        reads never do.
        """
        return await _one(_SESSIONS.c.session_id == session_id)

    async def get_owned_session(self, session_id: str, user_id: str) -> dict[str, Any] | None:
        """The user-facing accessor: the owner is part of the query, so a
        wrong-owner lookup returns ``None`` with nothing to compare afterwards."""
        owner = str(user_id or "").strip()
        if not owner:
            return None
        return await _one(
            and_(_SESSIONS.c.session_id == session_id, _SESSIONS.c.user_id == owner, _live())
        )

    async def get_owner(self, session_id: str) -> str | None:
        """The owning user of a live session, the one value the send path checks."""
        value = await fetch_value(
            select(_SESSIONS.c.user_id).where(_SESSIONS.c.session_id == session_id, _live())
        )
        return str(value) if value else None

    async def find_session_by_sandbox_id(self, sandbox_id: str) -> dict[str, Any] | None:
        """The newest live session bound to, or starting up on, ``sandbox_id``.

        A startup allocation is a durable address before the session is READY,
        so it counts as a binding for backend resolution.
        """
        target = str(sandbox_id or "").strip()
        if not target:
            return None
        allocation = _SESSIONS.c.startup_allocation
        return session_view(
            await fetch_one(
                _selected()
                .where(
                    _live(),
                    _SESSIONS.c.environment_name == self.environment_name,
                    or_(
                        and_(
                            _SESSIONS.c.sandbox_id == target,
                            func.coalesce(_SESSIONS.c.sandbox_backend, "") != "",
                        ),
                        and_(
                            allocation["sandbox_id"].as_string() == target,
                            func.coalesce(allocation["sandbox_backend"].as_string(), "") != "",
                        ),
                    ),
                )
                .order_by(_SESSIONS.c.updated_at.desc())
                .limit(1)
            )
        )

    # -- startup allocations -------------------------------------------------

    async def record_startup_allocation(
        self,
        session_id: str,
        allocation: dict[str, Any],
        *,
        replaces: dict[str, Any] | None = None,
        owner_expected: dict[str, Any] | None = None,
    ) -> bool:
        """Persist the one unadopted sandbox allocation for a session's startup.

        A different existing allocation is a conflict unless the caller names
        it exactly as ``replaces`` after handing that resource off. The write
        compares the current allocation, so the last durable address survives
        an ownership handoff.
        """
        normalized = dict(allocation)
        target = str(normalized.get("sandbox_id") or "").strip()
        if not target:
            raise ValueError("startup allocation requires sandbox_id")
        current = await self.get_session(session_id)
        if current is None:
            raise RuntimeError(f"cannot record startup allocation: session {session_id!r} is missing")
        if current["environment_name"] != self.environment_name:
            raise ValueError("startup allocation belongs to a different Environment")
        if owner_expected and any(current.get(key) != value for key, value in owner_expected.items()):
            raise RuntimeError(f"startup allocation owner changed: {session_id!r}")
        existing = current.get("startup_allocation")
        if isinstance(existing, dict):
            if existing == normalized:
                return True
            if existing != replaces:
                raise RuntimeError(
                    "session already names a different startup allocation "
                    f"session={session_id!r} existing={existing.get('sandbox_id')!r} new={target!r}"
                )
        elif replaces is not None:
            raise RuntimeError(f"startup allocation handoff lost its previous record: {session_id!r}")
        recorded = await self.compare_and_update_session(
            session_id,
            expected={**(owner_expected or {}), "startup_allocation": existing,
                      "environment_name": self.environment_name},
            updates={"startup_allocation": normalized},
        )
        if recorded:
            return True
        latest = await self.get_session(session_id) or {}
        if latest.get("startup_allocation") == normalized and all(
            latest.get(key) == value for key, value in (owner_expected or {}).items()
        ):
            return True
        raise RuntimeError(
            f"startup allocation write lost its session fence session={session_id!r} sandbox={target!r}"
        )

    async def retain_startup_allocation(self, session_id: str, record: dict[str, Any]) -> None:
        """Keep one orphan allocation's exact scope without touching the current one."""
        async def write(conn: AsyncConnection) -> None:
            row = (await conn.execute(
                select(_SESSIONS.c.retained_startup_allocations)
                .where(self._runtime_owner(session_id)).with_for_update()
            )).first()
            if row is None:
                raise RuntimeError(f"cannot retain allocation for missing session {session_id!r}")
            retained = list(row.retained_startup_allocations)
            if not any(await _retained_matches(conn, retained, record)):
                await conn.execute(
                    update(_SESSIONS).where(self._runtime_owner(session_id))
                    .values(retained_startup_allocations=[*retained, dict(record)])
                )

        await transaction(write)

    async def clear_retained_startup_allocation(self, session_id: str, record: dict[str, Any]) -> None:
        """Remove one exact receipt after its native-save or resource-release obligation settles."""
        async def write(conn: AsyncConnection) -> None:
            row = (await conn.execute(
                select(_SESSIONS.c.retained_startup_allocations)
                .where(self._runtime_owner(session_id)).with_for_update()
            )).first()
            if row is None:
                return
            retained = list(row.retained_startup_allocations)
            matches = await _retained_matches(conn, retained, record)
            await conn.execute(
                update(_SESSIONS).where(self._runtime_owner(session_id))
                .values(retained_startup_allocations=[
                    value for value, matched in zip(retained, matches) if not matched
                ])
            )

        await transaction(write)

    async def clear_startup_allocation(self, session_id: str, *, allocation: dict[str, Any]) -> bool:
        """Drop the session's reference to one exact released or adopted allocation.

        The statement compares the whole record, so two isolated allocations in
        one shared box never clear each other; a different current allocation,
        or a missing session, already satisfies the postcondition.
        """
        if not str(allocation.get("sandbox_id") or "").strip():
            raise ValueError("clearing a startup allocation requires sandbox_id")
        await execute(
            update(_SESSIONS)
            .where(
                self._runtime_owner(session_id),
                _SESSIONS.c.startup_allocation == json_literal(dict(allocation)),
            )
            .values(startup_allocation=None)
        )
        return True

    async def list_startup_allocation_candidates(
        self, *, after_session_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """One page of unfinished startups or active/retained allocations."""
        conditions: list[ColumnElement[bool]] = [
            _SESSIONS.c.environment_name == self.environment_name,
            or_(
                and_(_live(), _SESSIONS.c.state == SessionState.CREATING.value),
                _SESSIONS.c.startup_allocation.isnot(None),
                _SESSIONS.c.retained_startup_allocations != json_literal([]),
            )
        ]
        after = str(after_session_id or "").strip()
        if after:
            conditions.append(_SESSIONS.c.session_id > after)
        return await _many(
            _selected()
            .where(*conditions)
            .order_by(_SESSIONS.c.session_id)
            .limit(max(1, min(int(limit or 50), 10_000)))
        )

    async def list_startup_allocations_on_sandbox(self, sandbox_id: str) -> list[dict[str, Any]]:
        """Every row whose active startup allocation is on ``sandbox_id``.

        The box-occupancy check needs the whole set: a startup it missed would
        be a conversation joining a box that is then destroyed.
        """
        target = str(sandbox_id or "").strip()
        if not target:
            return []
        return await _many(
            _selected().where(
                _SESSIONS.c.startup_allocation["sandbox_id"].as_string() == target,
                _SESSIONS.c.environment_name == self.environment_name,
            )
        )

    # -- sandbox sweeps -----------------------------------------------------

    async def list_sessions_by_sandbox_id(self, sandbox_id: str) -> list[dict[str, Any]]:
        """Every live session bound to one sandbox, newest first.

        A box can serve many conversations; when it dies all of them lose their
        binding at once, so whoever converges on that needs the whole set.
        """
        target = str(sandbox_id or "").strip()
        if not target:
            return []
        return await _many(
            _selected()
            .where(
                _SESSIONS.c.sandbox_id == target, _live(),
                _SESSIONS.c.environment_name == self.environment_name,
            )
            .order_by(_SESSIONS.c.updated_at.desc())
        )

    async def list_dead_binding_probe_candidates(
        self, *, now_iso: str, after_session_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """One page of sessions whose binding may name a dead sandbox.

        A session still bound past its lease, or whose engine transport
        detached (``sandbox_liveness_suspect_at``), is asked about; sessions
        with a live lease, already converged, or ended are not. Rows come in
        ``session_id`` order after ``after_session_id``.
        """
        now = from_iso(now_iso)
        conditions: list[ColumnElement[bool]] = [
            _SESSIONS.c.environment_name == self.environment_name,
            _bound_and_open(),
            or_(
                _SESSIONS.c.expires_at.is_(None),
                _SESSIONS.c.expires_at <= now,
                _SESSIONS.c.sandbox_liveness_suspect_at.isnot(None),
            ),
        ]
        after = str(after_session_id or "").strip()
        if after:
            conditions.append(_SESSIONS.c.session_id > after)
        return await _many(
            _selected()
            .where(*conditions)
            .order_by(_SESSIONS.c.session_id)
            .limit(max(1, min(int(limit or 50), 500)))
        )

    async def list_idle_reclaim_candidates(
        self, *, now_iso: str, after_session_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """One page of sessions holding a live, unparked sandbox.

        The complement of :meth:`list_dead_binding_probe_candidates` split on
        the lease, so a box is never both suspected dead and reclaimed for
        idleness in one tick. Whether a session is idle is decided by the
        caller (per-agent windows and the runtime's reported status).
        """
        now = from_iso(now_iso)
        conditions: list[ColumnElement[bool]] = [
            _SESSIONS.c.environment_name == self.environment_name,
            _bound_and_open(),
            _SESSIONS.c.expires_at > now,
            _SESSIONS.c.sandbox_parked_at.is_(None),
        ]
        after = str(after_session_id or "").strip()
        if after:
            conditions.append(_SESSIONS.c.session_id > after)
        return await _many(
            _selected()
            .where(*conditions)
            .order_by(_SESSIONS.c.session_id)
            .limit(max(1, min(int(limit or 50), 500)))
        )

    # -- lists -------------------------------------------------------------

    async def list_user_sessions(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return await _many(
            _selected()
            .where(_SESSIONS.c.user_id == user_id, _live(), _SESSIONS.c.hidden.is_(False))
            .order_by(_SESSIONS.c.updated_at.desc(), _SESSIONS.c.session_id.desc())
            .limit(limit)
        )

    async def list_user_sessions_page(
        self, user_id: str, *, limit: int = 50, cursor: str | None = None,
        agent_ids: list[str] | None = None, query: str = "",
    ) -> dict[str, Any]:
        """One page of the user's visible sessions, newest activity first."""
        page_limit = max(1, min(int(limit or 50), 100))
        conditions: list[ColumnElement[bool]] = [
            _SESSIONS.c.user_id == user_id,
            _live(),
            _SESSIONS.c.hidden.is_(False),
        ]
        if agent_ids is not None:
            conditions.append(_SESSIONS.c.agent_id.in_(agent_ids))
        if query.strip():
            conditions.append(_SESSIONS.c.title.icontains(query.strip(), autoescape=True))
        position = _decode_session_list_cursor(cursor)
        if position is not None:
            at = from_iso(position["updated_at"])
            conditions.append(
                or_(
                    _SESSIONS.c.updated_at < at,
                    and_(
                        _SESSIONS.c.updated_at == at,
                        _SESSIONS.c.session_id < position["session_id"],
                    ),
                )
            )
        rows = await _many(
            _selected()
            .where(*conditions)
            .order_by(_SESSIONS.c.updated_at.desc(), _SESSIONS.c.session_id.desc())
            .limit(page_limit + 1)
        )
        has_more = len(rows) > page_limit
        visible = rows[:page_limit]
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = _encode_session_list_cursor(
                updated_at=str(last["updated_at"]), session_id=str(last["session_id"])
            )
        return {"sessions": visible, "has_more": has_more, "next_cursor": next_cursor}

    @staticmethod
    def _admin_conditions(
        *,
        agent_ids: list[str] | None,
        agent_id: str | None,
        since: str | None,
        until: str | None,
    ) -> list[ColumnElement[bool]]:
        """The admin listing's conditions, shared by list and count.

        ``agent_ids`` scopes the listing to the Agents an administrator
        manages: ``[]`` matches nothing, ``None`` is the global admin surface.
        The window is over when a conversation started.
        """
        conditions: list[ColumnElement[bool]] = [_live()]
        if agent_ids is not None:
            conditions.append(_SESSIONS.c.agent_id.in_(list(agent_ids)))
            conditions.append(_SESSIONS.c.workspace_kind == "agent")
        if agent_id:
            conditions.append(_SESSIONS.c.agent_id == agent_id)
        if since:
            conditions.append(_SESSIONS.c.created_at >= from_iso(since))
        if until:
            conditions.append(_SESSIONS.c.created_at <= from_iso(until))
        return conditions

    async def list_all_sessions(
        self,
        limit: int = 200,
        *,
        skip: int = 0,
        agent_ids: list[str] | None = None,
        agent_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        state: str | None = None,
    ) -> list[dict[str, Any]]:
        """One page of sessions across all users (admin use), newest activity first."""
        conditions = self._admin_conditions(
            agent_ids=agent_ids, agent_id=agent_id, since=since, until=until
        )
        if state:
            conditions.append(_SESSIONS.c.state == state)
        return await _many(
            _selected()
            .where(*conditions)
            .order_by(_SESSIONS.c.updated_at.desc(), _SESSIONS.c.session_id.desc())
            .offset(skip)
            .limit(limit)
        )

    async def count_session_totals(self, *, agent_ids: list[str] | None = None) -> dict[str, Any]:
        """All scoped sessions and the modelled states among them, in one query."""
        rows = await fetch_all(
            select(_SESSIONS.c.state, func.count().label("count"))
            .where(*self._admin_conditions(agent_ids=agent_ids, agent_id=None, since=None, until=None))
            .group_by(_SESSIONS.c.state)
        )
        modelled = {state.value for state in SessionState}
        total = sum(int(row["count"]) for row in rows)
        by_state = {str(row["state"]): int(row["count"]) for row in rows if row["state"] in modelled}
        return {"total": total, "by_state": by_state}

    async def count_all_sessions(
        self,
        *,
        agent_ids: list[str] | None = None,
        agent_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        state: str | None = None,
    ) -> int:
        """How many sessions match, counted at the source rather than from a page."""
        conditions = self._admin_conditions(
            agent_ids=agent_ids, agent_id=agent_id, since=since, until=until
        )
        if state:
            conditions.append(_SESSIONS.c.state == state)
        return int(await fetch_value(select(func.count()).select_from(_SESSIONS).where(*conditions)) or 0)

    async def find_sessions_by_agent_id(self, agent_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        return await _many(
            _selected()
            .where(_live(), _SESSIONS.c.agent_id == agent_id)
            .order_by(_SESSIONS.c.created_at.desc())
            .limit(limit)
        )

    async def list_assistant_conversations(self, user_id: str, assistant_id: str) -> list[dict[str, Any]]:
        """The owner's visible conversations with one Assistant."""
        return await _many(
            _selected().where(
                _SESSIONS.c.user_id == user_id,
                _SESSIONS.c.assistant_id == assistant_id,
                _SESSIONS.c.hidden.is_(False),
                _live(),
            )
        )

    async def list_agent_runtime_references(
        self, agent_ids: list[str], *, connection: AsyncConnection | None = None
    ) -> list[dict[str, Any]]:
        """Every session of these Agents that may still hold runtime resources.

        Includes hidden and deleted rows: they protect runtime configuration
        until their resources are confirmed released.
        """
        if not agent_ids:
            return []
        return await self._references(_SESSIONS.c.agent_id.in_(list(agent_ids)), connection)

    async def list_assistant_runtime_references(
        self, assistant_ids: list[str], *, connection: AsyncConnection | None = None
    ) -> list[dict[str, Any]]:
        """Every session of these Assistants, including their workspaces' bootstrap sessions."""
        if not assistant_ids:
            return []
        ids = list(assistant_ids)
        condition = and_(
            _SESSIONS.c.workspace_kind == "assistant",
            or_(_SESSIONS.c.assistant_id.in_(ids), _SESSIONS.c.owner_id.in_(ids)),
        )
        rows = await self._references(condition, connection)
        for row in rows:
            row["assistant_id"] = row.get("assistant_id") or row.get("owner_id")
        return rows

    async def _references(
        self, condition: ColumnElement[bool], connection: AsyncConnection | None
    ) -> list[dict[str, Any]]:
        statement = select(
            _SESSIONS.c.session_id,
            _SESSIONS.c.environment_name,
            _SESSIONS.c.agent_id,
            _SESSIONS.c.assistant_id,
            _SESSIONS.c.owner_id,
            _SESSIONS.c.state,
            _SESSIONS.c.sandbox_id,
            _SESSIONS.c.undestroyed_sandbox_ids,
            _SESSIONS.c.startup_allocation,
            _SESSIONS.c.retained_startup_allocations,
        ).where(condition)
        if connection is not None:
            return [dict(row._mapping) for row in (await connection.execute(statement)).all()]
        return await fetch_all(statement)

    # -- writes ------------------------------------------------------------

    async def update_session(
        self,
        session_id: str,
        updates: dict[str, Any],
        *,
        touch_updated_at: bool = True,
        notify: NoticeKind | None = None,
    ) -> bool:
        """Set columns on one session (deleted or not); whether the row exists.

        With ``notify``, the same transaction enqueues that session's notice.
        """
        if "environment_name" in updates:
            raise ValueError("a Session's Environment is immutable")
        values = _column_values(updates)
        if touch_updated_at:
            values["updated_at"] = func.now()
        if not values:
            statement = select(_SESSIONS.c.session_id).where(_SESSIONS.c.session_id == session_id)
            if notify is not None:
                return await _notify_rows(statement, notify)
            return bool(await fetch_all(statement))
        return await _changed(
            update(_SESSIONS).where(_SESSIONS.c.session_id == session_id).values(**values), session_id, notify
        )

    async def compare_and_update_session(
        self,
        session_id: str,
        *,
        expected: dict[str, Any],
        updates: dict[str, Any],
        touch_updated_at: bool = True,
        notify: NoticeKind | None = None,
    ) -> bool:
        """Set columns only while the live row still holds ``expected``.

        Each expected value is an equality, ``None`` (the column is null),
        ``{"$in": [...]}`` (any of those values, including SQL null),
        ``{"$exists": False}`` (the same) or ``{"$ne": value}``. Returns whether
        the row matched. With ``notify``, a matching row sends that session
        notice from the same transaction, so the notice exists only if the
        change committed; with no ``updates`` it announces the row as it is.
        """
        if "environment_name" in updates:
            raise ValueError("a Session's Environment is immutable")
        values = _column_values(updates)
        if touch_updated_at:
            values["updated_at"] = func.now()
        conditions = [_SESSIONS.c.session_id == session_id, _live()]
        conditions.extend(_condition(key, value) for key, value in expected.items())
        if not values:
            statement = select(_SESSIONS.c.session_id).where(*conditions)
            if notify is not None:
                return await _notify_rows(statement, notify)
            return bool(await fetch_all(statement))
        return await _changed(update(_SESSIONS).where(*conditions).values(**values), session_id, notify)

    async def set_permission_mode(self, session_id: str, mode: str) -> bool:
        """Store the live session's permission mode; its reader applies it to the runtime."""
        return await _changed(
            update(_SESSIONS)
            .where(_SESSIONS.c.session_id == session_id, _live())
            .values(permission_mode=mode),
            session_id, "permission",
        )


    async def archive_session(self, session_id: str, user_id: str) -> bool:
        return await self.update_session(
            session_id, {"hidden": True, "archived_at": utcnow_iso(), "archived_by": user_id}
        )

    async def soft_delete(
        self, session_id: str, user_id: str, *,
        expected: dict[str, Any] | None = None, updates: dict[str, Any] | None = None,
    ) -> bool:
        """Retire bindings, mark deleted and remove routes while expected fields match."""
        if updates and "environment_name" in updates:
            raise ValueError("a Session's Environment is immutable")
        conditions = [_SESSIONS.c.session_id == session_id]
        conditions.extend(_condition(key, value) for key, value in (expected or {}).items())
        values = _column_values(updates or {})
        values.update(
            deleted=True, state=SessionState.DELETED.value,
            deleted_at=func.now(), deleted_by=user_id, updated_at=func.now(),
        )

        async def write(conn: AsyncConnection) -> bool:
            result = await conn.execute(
                update(_SESSIONS)
                .where(*conditions)
                .values(**values)
            )
            if not result.rowcount:
                return False
            await conn.execute(delete(_MCP_BINDINGS).where(_MCP_BINDINGS.c.session_id == session_id))
            return True

        return await transaction(write)
