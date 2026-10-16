"""Assistant defaults: the layers of what every Assistant beneath them inherits.

Each row is one layer: the Environment and model new Assistants use,
instructions, and the Skills, MCP servers, packages, native options and
Credential Vaults provided to them. ``scope`` names the layer;
``deployment`` covers the whole deployment. Assistants read their layers when
their runtime view is resolved.

A save replaces the layer and is a compare-and-set on its ``version``, so a
save computed from a layer that another save has since replaced changes
nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import delete, func, insert, literal_column, select, update
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.sql import ForeignKeyViolation, fetch_all, fetch_one, to_iso, transaction
from astrabox.persistence.sql.models.assistants import (
    AssistantDefaultMcpAssignment,
    AssistantDefaults,
    AssistantDefaultVaultBinding,
)

DEPLOYMENT_SCOPE = "deployment"

_LAYERS = AssistantDefaults.__table__
_VAULTS = AssistantDefaultVaultBinding.__table__
_ASSIGNMENTS = AssistantDefaultMcpAssignment.__table__

#: The constraint PostgreSQL names when a layer's Environment does not exist,
#: as ``astrabox.persistence.sql.base.NAMING_CONVENTION`` names it.
_ENVIRONMENT_FK = "fk_assistant_defaults_environment_name_environments"

#: Layer fields stored in the column of the same name.
_LAYER_COLUMNS = (
    "environment_name",
    "model_name",
    "instructions",
    "skills",
    "mcp_servers",
    "packages",
    "engine_options",
    "extension_catalog",
)


class AssistantDefaultsEnvironmentMissing(Exception):
    """The Environment a layer names does not exist."""


class AssistantDefaultsVaultMissing(Exception):
    """A Credential Vault a layer binds does not exist."""


def _layers() -> Any:
    vault_ids = (
        select(func.jsonb_agg(aggregate_order_by(_VAULTS.c.vault_id, _VAULTS.c.position)))
        .where(_VAULTS.c.scope == _LAYERS.c.scope)
        .scalar_subquery()
        .label("credential_vault_ids")
    )
    assignments = (
        select(
            func.jsonb_agg(
                aggregate_order_by(
                    func.jsonb_build_object(
                        literal_column("'provider'"),
                        _ASSIGNMENTS.c.provider,
                        literal_column("'item_id'"),
                        _ASSIGNMENTS.c.item_id,
                    ),
                    _ASSIGNMENTS.c.position,
                )
            )
        )
        .where(_ASSIGNMENTS.c.scope == _LAYERS.c.scope)
        .scalar_subquery()
        .label("mcp_assignments")
    )
    return select(_LAYERS, vault_ids, assignments)


def _view(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "scope": row["scope"],
        **{field: row[field] for field in _LAYER_COLUMNS},
        "credential_vault_ids": list(row["credential_vault_ids"] or []),
        "mcp_assignments": list(row["mcp_assignments"] or []),
        "version": row["version"],
        "updated_by": row["updated_by"],
        "created_at": to_iso(row["created_at"]),
        "updated_at": to_iso(row["updated_at"]),
    }


class AssistantDefaultsRepository:
    """The defaults layers, their bound Vaults and catalog MCP assignments."""

    async def get_layer(self, scope: str) -> dict[str, Any] | None:
        return _view(await fetch_one(_layers().where(_LAYERS.c.scope == scope)))

    async def put_layer(
        self,
        scope: str,
        fields: Mapping[str, Any],
        *,
        expected_version: int | None,
        updated_by: str,
    ) -> bool:
        """Replace the layer with ``fields`` if it is still at ``expected_version``.

        ``expected_version`` is ``None`` for a layer that did not exist when it
        was read. ``False`` when another save got there first; nothing is
        written then. Raises :class:`AssistantDefaultsEnvironmentMissing` or
        :class:`AssistantDefaultsVaultMissing` for a reference that does not
        exist.
        """
        columns = {field: fields.get(field) for field in _LAYER_COLUMNS}
        vault_ids: Sequence[str] = list(fields.get("credential_vault_ids") or [])
        assignments: Sequence[Mapping[str, Any]] = list(fields.get("mcp_assignments") or [])

        async def work(conn: AsyncConnection) -> bool:
            if expected_version is None:
                saved = await conn.execute(
                    pg_insert(_LAYERS)
                    .values(scope=scope, **columns, updated_by=updated_by)
                    .on_conflict_do_nothing(index_elements=["scope"])
                    .returning(_LAYERS.c.scope)
                )
            else:
                saved = await conn.execute(
                    update(_LAYERS)
                    .where(_LAYERS.c.scope == scope, _LAYERS.c.version == expected_version)
                    .values(
                        **columns,
                        updated_by=updated_by,
                        version=_LAYERS.c.version + 1,
                        updated_at=func.now(),
                    )
                    .returning(_LAYERS.c.scope)
                )
            if saved.first() is None:
                return False
            await conn.execute(delete(_VAULTS).where(_VAULTS.c.scope == scope))
            await conn.execute(delete(_ASSIGNMENTS).where(_ASSIGNMENTS.c.scope == scope))
            if vault_ids:
                await conn.execute(
                    insert(_VAULTS),
                    [
                        {"scope": scope, "position": position, "vault_id": vault_id}
                        for position, vault_id in enumerate(vault_ids)
                    ],
                )
            if assignments:
                await conn.execute(
                    insert(_ASSIGNMENTS),
                    [
                        {
                            "scope": scope,
                            "position": position,
                            "provider": str(item["provider"]),
                            "item_id": str(item["item_id"]),
                        }
                        for position, item in enumerate(assignments)
                    ],
                )
            return True

        try:
            return await transaction(work)
        except ForeignKeyViolation as exc:
            if exc.constraint == _ENVIRONMENT_FK:
                raise AssistantDefaultsEnvironmentMissing(str(columns["environment_name"])) from exc
            raise AssistantDefaultsVaultMissing(",".join(vault_ids)) from exc

    async def list_by_credential_vault_id(self, vault_id: str) -> list[dict[str, Any]]:
        """The layers that bind this Credential Vault: their scope and bound Vaults."""
        rows = await fetch_all(
            _layers()
            .where(_LAYERS.c.scope.in_(select(_VAULTS.c.scope).where(_VAULTS.c.vault_id == vault_id)))
            .order_by(_LAYERS.c.scope)
        )
        return [
            {"scope": view["scope"], "credential_vault_ids": view["credential_vault_ids"]}
            for view in map(_view, rows)
            if view is not None
        ]

    async def list_by_environment(self, name: str) -> list[dict[str, Any]]:
        """The layers that name this Environment."""
        rows = await fetch_all(
            select(_LAYERS.c.scope).where(_LAYERS.c.environment_name == name).order_by(_LAYERS.c.scope)
        )
        return [dict(row) for row in rows]
