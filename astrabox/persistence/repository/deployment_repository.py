"""Deployments: the trigger bindings that start conversations on an Agent or an Assistant."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, delete, func, insert, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import ColumnElement

from astrabox.config.settings import get_settings, validate_environment_name
from astrabox.persistence.sql import ForeignKeyViolation, execute, fetch_all, fetch_one, to_iso, transaction
from astrabox.persistence.sql.models.deployments import Deployment

_DEPLOYMENTS = Deployment.__table__

#: Columns a Deployment always has; every other column is reported only when
#: the Deployment sets it, as the API has always shown absent settings.
_ALWAYS = frozenset({
    "deployment_id", "environment_name", "created_by", "enabled", "scene", "prompt_prefix",
    "created_at", "updated_at",
})

#: Fields a write may set, by the name services use. ``schedule`` maps to its
#: two columns; identity and scope are fixed at creation.
_WRITABLE = frozenset({
    "enabled", "scene", "prompt_prefix", "name", "schedule", "secret",
    "attention_policy", "response_instruction", "execution_mode",
    "execution_user_id", "allowed_participants", "channel_config",
    "credentials_configured", "callback_base_url",
})
_IDENTITY = frozenset({
    "deployment_id", "environment_name", "agent_id", "assistant_id", "created_by", "scope",
    "owner_user_id", "owner_org_id", "source_assistant_id",
})


class DeploymentTargetMissing(Exception):
    """The Agent or Assistant a new Deployment names does not exist."""


def _const(value: str) -> ColumnElement[str]:
    """A constant written into the SQL text rather than bound.

    A partial index applies only to a plan that can prove its predicate; a
    cached generic plan cannot see a bound value, so constants that select a
    partial index are written into the statement.
    """
    return literal(value, literal_execute=True)


def _view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    view: dict[str, Any] = {}
    for key, value in row.items():
        if key in ("schedule_cron", "schedule_timezone"):
            continue
        if key in ("created_at", "updated_at"):
            view[key] = to_iso(value)
        elif key in _ALWAYS or value is not None:
            view[key] = value
    if row.get("schedule_cron") is not None:
        view["schedule"] = {"cron": row["schedule_cron"], "timezone": row["schedule_timezone"]}
    return view


def _views(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [view for view in map(_view, rows) if view is not None]


def _columns(fields: dict[str, Any], *, allowed: frozenset[str]) -> dict[str, Any]:
    """Column values for ``fields``; a field no column holds is refused."""
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"not a Deployment field: {sorted(unknown)}")
    values = {key: value for key, value in fields.items() if key != "schedule"}
    if "schedule" in fields:
        schedule = fields["schedule"] or {}
        values["schedule_cron"] = schedule.get("cron")
        values["schedule_timezone"] = schedule.get("timezone")
    return values


def _scope(
    deployment_id: str, agent_id: str, assistant_id: str, owner_user_id: str, owner_org_id: str,
) -> ColumnElement[bool] | None:
    """The nested-resource fence shared by every management read and write.

    A Deployment belongs to one Agent or one Assistant, and a personal
    schedule also to its owner. They are part of the statement's condition,
    not checked by the service before an id-only write, so a race cannot
    turn the service's visibility check into an authorization gap.
    """
    wid = str(deployment_id or "").strip()
    aid = str(agent_id or "").strip()
    sid = str(assistant_id or "").strip()
    if not wid or bool(aid) == bool(sid) or bool(owner_user_id) != bool(owner_org_id):
        return None
    table = _DEPLOYMENTS.c
    return and_(
        table.deployment_id == wid,
        table.agent_id == aid if aid else table.assistant_id == sid,
        and_(
            table.scope == _const("personal"),
            table.owner_user_id == owner_user_id,
            table.owner_org_id == owner_org_id,
        ) if owner_user_id else table.scope.is_(None),
    )


class DeploymentRepository:
    """Trigger bindings that start conversations on an Agent or an Assistant.

    One row per binding, keyed by a random ``deployment_id``. Each binding
    names one target, ``agent_id`` or ``assistant_id``; its triggers start
    conversations on that target (see
    :mod:`astrabox.core.service.orchestrator.conversation_targets`). A deleted
    binding is gone: deletion removes the row and, with it, the binding's
    channel records.
    """

    def __init__(self, *, environment_name: str | None = None) -> None:
        self.environment_name = validate_environment_name(
            get_settings().environment_name if environment_name is None else environment_name
        )

    async def get_by_id(self, deployment_id: str) -> dict[str, Any] | None:
        wid = str(deployment_id or "").strip()
        if not wid:
            return None
        return _view(await fetch_one(select(_DEPLOYMENTS).where(_DEPLOYMENTS.c.deployment_id == wid)))

    async def get_for_deployment(
        self, deployment_id: str, agent_id: str, *, assistant_id: str = "", owner_user_id: str = "",
        owner_org_id: str = "",
    ) -> dict[str, Any] | None:
        """Return one Deployment only inside its Agent or Assistant scope."""
        fence = _scope(deployment_id, agent_id, assistant_id, owner_user_id, owner_org_id)
        if fence is None:
            return None
        return _view(await fetch_one(select(_DEPLOYMENTS).where(fence)))

    async def list_by_agent(self, agent_id: str) -> list[dict[str, Any]]:
        """An Agent's Deployments, oldest first; personal schedules are their owners'."""
        aid = str(agent_id or "").strip()
        if not aid:
            return []
        return _views(await fetch_all(
            select(_DEPLOYMENTS)
            .where(_DEPLOYMENTS.c.agent_id == aid, _DEPLOYMENTS.c.scope.is_(None))
            .order_by(_DEPLOYMENTS.c.created_at, _DEPLOYMENTS.c.deployment_id)
        ))

    async def list_by_assistant(self, assistant_id: str) -> list[dict[str, Any]]:
        """An Assistant's Deployments, oldest first."""
        sid = str(assistant_id or "").strip()
        if not sid:
            return []
        return _views(await fetch_all(
            select(_DEPLOYMENTS)
            .where(_DEPLOYMENTS.c.assistant_id == sid, _DEPLOYMENTS.c.scope.is_(None))
            .order_by(_DEPLOYMENTS.c.created_at, _DEPLOYMENTS.c.deployment_id)
        ))

    async def list_personal_schedules(
        self, owner_user_id: str, owner_org_id: str, *, source_assistant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return only the authenticated owner's personal schedules, oldest first.

        ``source_assistant_id`` narrows them to the ones created from one Assistant.
        """
        if not owner_user_id or not owner_org_id:
            return []
        table = _DEPLOYMENTS.c
        statement = select(_DEPLOYMENTS).where(
            table.scope == _const("personal"),
            table.scene == _const("schedule"),
            table.owner_user_id == owner_user_id,
            table.owner_org_id == owner_org_id,
        )
        if source_assistant_id is not None:
            statement = statement.where(table.source_assistant_id == source_assistant_id)
        return _views(await fetch_all(
            statement.order_by(table.created_at, table.deployment_id)
        ))

    async def list_active(self) -> list[dict[str, Any]]:
        """Every Deployment, oldest first."""
        return _views(await fetch_all(
            select(_DEPLOYMENTS).order_by(_DEPLOYMENTS.c.created_at, _DEPLOYMENTS.c.deployment_id)
        ))

    async def list_scheduled(self) -> list[dict[str, Any]]:
        """This Environment's scheduled Deployments, for its worker reconciliation."""
        return _views(await fetch_all(
            select(_DEPLOYMENTS).where(
                _DEPLOYMENTS.c.scene == _const("schedule"),
                _DEPLOYMENTS.c.environment_name == self.environment_name,
            )
        ))

    async def list_channel_bindings(self) -> list[dict[str, Any]]:
        """The identity and revision of every enabled channel binding.

        The channel source host compares these on every pass and reads a
        binding in full only when it starts that binding's consumer, so the
        pass reads three columns, not every row.
        """
        table = _DEPLOYMENTS.c
        rows = await fetch_all(
            select(table.deployment_id, table.scene, table.updated_at)
            .where(
                table.enabled, table.scene.like(_const("channel:%")),
                table.environment_name == self.environment_name,
            )
            .order_by(table.deployment_id)
        )
        return [{**row, "updated_at": to_iso(row["updated_at"])} for row in rows]

    async def advance_channel_source_cursor(
        self, deployment_id: str, *, scene: str, source_cursor: int
    ) -> bool:
        """Advance one binding's resumable source cursor monotonically.

        A cursor is delivery acknowledgement state, so a disabled or deleted
        binding cannot advance it. Concurrent replicas may observe the same
        event; the cursor only ever takes the larger value, and an event the
        cursor already passed is acknowledged as durable.
        """
        wid = str(deployment_id or "").strip()
        expected_scene = str(scene or "").strip()
        cursor_value = int(source_cursor)
        if not wid or not expected_scene.startswith("channel:") or cursor_value < 0:
            return False
        table = _DEPLOYMENTS.c
        advanced = await execute(
            update(_DEPLOYMENTS)
            .where(
                table.deployment_id == wid,
                table.environment_name == self.environment_name,
                table.scene == expected_scene,
                table.enabled,
            )
            .values(source_cursor=func.greatest(func.coalesce(table.source_cursor, -1), cursor_value))
        )
        return advanced == 1

    async def create(self, deployment_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        """Insert a new Deployment; returns the stored row.

        Raises :class:`DeploymentTargetMissing` when the Agent or Assistant it
        names does not exist.
        """
        values = _columns(
            {key: value for key, value in doc.items() if key != "deployment_id"},
            allowed=_WRITABLE | _IDENTITY,
        )
        if "environment_name" in values and values["environment_name"] != self.environment_name:
            raise ValueError("a Deployment must be created in this repository's Environment")
        values["environment_name"] = self.environment_name
        wid = str(deployment_id or "").strip()

        async def create(conn: AsyncConnection) -> dict[str, Any]:
            await conn.execute(
                insert(_DEPLOYMENTS)
                .values(deployment_id=wid, **values)
            )
            row = (await conn.execute(
                select(_DEPLOYMENTS).where(_DEPLOYMENTS.c.deployment_id == wid)
            )).one()
            stored = _view(dict(row._mapping))
            assert stored is not None
            return stored

        try:
            return await transaction(create)
        except ForeignKeyViolation as exc:
            raise DeploymentTargetMissing(str(exc.constraint or "")) from exc

    async def update_for_deployment(
        self,
        deployment_id: str,
        agent_id: str,
        updates: dict[str, Any],
        *,
        assistant_id: str = "",
        owner_user_id: str = "",
        owner_org_id: str = "",
    ) -> dict[str, Any] | None:
        """Atomically update and return one Deployment inside its scope.

        A missing or foreign row does not match and returns ``None``. Identity
        and scope fields can never be changed through this mutation, even if a
        caller includes them in ``updates``.
        """
        fence = _scope(deployment_id, agent_id, assistant_id, owner_user_id, owner_org_id)
        if fence is None:
            return None
        values = _columns(
            {key: value for key, value in dict(updates or {}).items() if key not in _IDENTITY},
            allowed=_WRITABLE,
        )

        async def change(conn: AsyncConnection) -> dict[str, Any] | None:
            changed = await conn.execute(
                update(_DEPLOYMENTS)
                .where(fence)
                .values(**values, updated_at=func.now())
            )
            if changed.rowcount != 1:
                return None
            row = (await conn.execute(select(_DEPLOYMENTS).where(fence))).one()
            return _view(dict(row._mapping))

        return await transaction(change)

    async def delete_for_deployment(
        self, deployment_id: str, agent_id: str, *, assistant_id: str = "", owner_user_id: str = "",
        owner_org_id: str = "",
    ) -> bool:
        """Atomically delete one Deployment inside its scope.

        The first matching delete returns ``True``; a duplicate, missing or
        foreign delete returns ``False`` and the service maps all three to
        404. The binding's channel records are deleted with it.
        """
        fence = _scope(deployment_id, agent_id, assistant_id, owner_user_id, owner_org_id)
        if fence is None:
            return False
        return await execute(delete(_DEPLOYMENTS).where(fence)) == 1

    async def delete_for_target(
        self, *, agent_id: str = "", assistant_id: str = "",
    ) -> list[dict[str, Any]]:
        """Delete every Deployment of one Agent or Assistant; returns the deleted rows.

        An Assistant's include the personal schedules created from it.
        """
        aid = str(agent_id or "").strip()
        sid = str(assistant_id or "").strip()
        if bool(aid) == bool(sid):
            raise ValueError("name exactly one Agent or Assistant")
        table = _DEPLOYMENTS.c
        target = (
            table.agent_id == aid
            if aid
            else or_(table.assistant_id == sid, table.source_assistant_id == sid)
        )

        async def remove(conn: AsyncConnection) -> list[dict[str, Any]]:
            rows = (await conn.execute(
                select(_DEPLOYMENTS).where(target)
                .order_by(table.deployment_id).with_for_update()
            )).all()
            if not rows:
                return []
            await conn.execute(delete(_DEPLOYMENTS).where(
                target, table.deployment_id.in_([row.deployment_id for row in rows]),
            ))
            return _views([dict(row._mapping) for row in rows])

        return await transaction(remove)
