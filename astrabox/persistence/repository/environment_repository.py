"""The database's Environment binding, metadata and membership transactions."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.common.utils.errors import APIError
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.sql import fetch_all, fetch_one, fetch_value, to_iso, transaction
from astrabox.persistence.sql.models.environments import Environment, EnvironmentMember

_ENVIRONMENTS = Environment.__table__
_MEMBERS = EnvironmentMember.__table__


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: to_iso(value) if key in {"created_at", "updated_at"} else value
        for key, value in row.items() if key != "singleton"
    }


def _conflict() -> APIError:
    return APIError(
        code="ENVIRONMENT_VERSION_CONFLICT", status_code=409,
        message="the Environment changed; read its current version before saving",
    )


class EnvironmentRepository:
    """The local Environment; another Environment uses another database."""

    async def ensure_current(self, *, initial_local_user_id: str | None = None) -> None:
        name = get_persistence().environment_name

        async def create(connection: AsyncConnection) -> None:
            inserted = await get_persistence().statements.insert_if_absent(
                connection, _ENVIRONMENTS,
                {"singleton": True, "name": name, "display_name": name},
                index_columns=["singleton"],
            )
            owner = (await connection.execute(select(_ENVIRONMENTS.c.name))).scalar_one()
            if owner != name:
                raise RuntimeError(
                    f"database belongs to Environment {owner!r}, not {name!r}; "
                    "use this Environment's separate database and credentials"
                )
            if inserted and initial_local_user_id is not None:
                await connection.execute(insert(_MEMBERS).values(
                    environment_name=name, user_id=initial_local_user_id,
                    created_by=initial_local_user_id,
                ))

        await transaction(create)

    async def get(self, name: str) -> dict[str, Any] | None:
        if name != get_persistence().environment_name:
            return None
        row = await fetch_one(select(_ENVIRONMENTS).where(_ENVIRONMENTS.c.name == name))
        return _view(row) if row is not None else None

    async def has_member(self, name: str, user_id: str) -> bool:
        return bool(await fetch_value(select(_MEMBERS.c.user_id).where(
            _MEMBERS.c.environment_name == name, _MEMBERS.c.user_id == user_id,
        )))

    async def list_accessible(self, user_id: str, *, platform_admin: bool) -> list[dict[str, Any]]:
        statement = select(_ENVIRONMENTS).where(
            _ENVIRONMENTS.c.name == get_persistence().environment_name,
        )
        if not platform_admin:
            statement = statement.join(
                _MEMBERS, _MEMBERS.c.environment_name == _ENVIRONMENTS.c.name,
            ).where(_MEMBERS.c.user_id == user_id)
        rows = await fetch_all(statement.order_by(_ENVIRONMENTS.c.display_name, _ENVIRONMENTS.c.name))
        return [_view(row) for row in rows]

    async def member_user_ids(self, name: str) -> list[str]:
        rows = await fetch_all(select(_MEMBERS.c.user_id).where(
            _MEMBERS.c.environment_name == name,
        ).order_by(_MEMBERS.c.user_id))
        return [str(row["user_id"]) for row in rows]

    async def save(
        self, name: str, *, display_name: str, description: str | None,
        member_user_ids: list[str], expected_version: int, actor_id: str,
    ) -> dict[str, Any]:
        if name != get_persistence().environment_name:
            raise APIError(
                code="ENVIRONMENT_NOT_FOUND", status_code=404, message="Environment not found",
            )

        async def write(connection: AsyncConnection) -> dict[str, Any]:
            values = {
                "display_name": display_name, "description": description,
                "updated_by": actor_id,
            }
            changed = await connection.execute(update(_ENVIRONMENTS).where(
                _ENVIRONMENTS.c.name == name, _ENVIRONMENTS.c.version == expected_version,
            ).values(**values, version=expected_version + 1, updated_at=func.now()))
            if changed.rowcount != 1:
                raise _conflict()
            await connection.execute(delete(_MEMBERS).where(_MEMBERS.c.environment_name == name))
            if member_user_ids:
                await connection.execute(insert(_MEMBERS), [
                    {"environment_name": name, "user_id": user_id, "created_by": actor_id}
                    for user_id in sorted(set(member_user_ids))
                ])
            row = (await connection.execute(select(_ENVIRONMENTS).where(
                _ENVIRONMENTS.c.name == name,
            ))).mappings().one()
            return {**_view(dict(row)), "member_user_ids": sorted(set(member_user_ids))}

        return await transaction(write)
