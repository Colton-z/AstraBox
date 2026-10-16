"""Assistants: their definitions, what is bound to them, and the platform's bookkeeping.

An Assistant's authored fields change only through a compare-and-set on its
``version``: the service reads the Assistant, computes the change, and writes it
with the version it read. A write that lost to a concurrent one changes nothing
and returns ``False``, so two edits cannot both land. The child rows an
authored write replaces (Vault bindings, catalog MCP assignments, connectors)
are written in the same transaction as the version that covers them.

The platform's own fields (the main conversation, the durable workspace id, the
primary mark) are single conditional statements that move neither ``version``
nor ``updated_at``.

A deleted Assistant keeps its row; :meth:`AssistantCatalogRepository.delete_assistant`
says what goes with it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Select,
    Text,
    delete,
    exists,
    func,
    insert,
    literal,
    literal_column,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.sql import (
    ForeignKeyViolation,
    UniqueViolation,
    execute,
    fetch_all,
    fetch_one,
    fetch_value,
    to_iso,
    transaction,
)
from astrabox.persistence.sql.models.assistants import (
    Assistant,
    AssistantConnector,
    AssistantMcpAssignment,
    AssistantVaultBinding,
    AssistantWorkspace,
)

_ASSISTANTS = Assistant.__table__
_VAULTS = AssistantVaultBinding.__table__
_ASSIGNMENTS = AssistantMcpAssignment.__table__
_CONNECTORS = AssistantConnector.__table__
_WORKSPACES = AssistantWorkspace.__table__

#: Authored fields an update writes to the column of the same name.
_DEFINITION_COLUMNS = frozenset(
    {
        "display_name",
        "icon",
        "description",
        "permission_mode_default",
        "system",
        "timezone",
        "mcp_servers",
        "packages",
        "skills",
        "engine_options",
    }
)

#: Listing narrowings by workspace posture.
LIST_STATUSES = frozenset({"all", "ready", "dormant"})


class AssistantExists(Exception):
    """An Assistant with this id, or a live primary of this owner, already exists."""


class AssistantEnvironmentMissing(Exception):
    """The Environment an Assistant names does not exist."""


class AssistantVaultMissing(Exception):
    """A Credential Vault a binding names does not exist."""


class AssistantSessionMissing(Exception):
    """The Session recorded as an Assistant's main conversation does not exist."""


class AssistantSandboxRetained(Exception):
    """The Assistant's workspace names a box, so the Assistant cannot be deleted yet."""

    def __init__(self, sandbox_id: str) -> None:
        super().__init__(f"the workspace still names sandbox {sandbox_id!r}")
        self.sandbox_id = sandbox_id


def _model_name(override: Any) -> str | None:
    """The column an Assistant's ``model_config_override`` is stored in."""
    if not isinstance(override, Mapping):
        return None
    value = override.get("model_name")
    return str(value) if value is not None else None


def _child_columns() -> list[Any]:
    """The child rows of the Assistant in the enclosing query, aggregated in their order."""
    vault_ids = (
        select(func.jsonb_agg(aggregate_order_by(_VAULTS.c.vault_id, _VAULTS.c.position)))
        .where(_VAULTS.c.assistant_id == _ASSISTANTS.c.assistant_id)
        .scalar_subquery()
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
        .where(_ASSIGNMENTS.c.assistant_id == _ASSISTANTS.c.assistant_id)
        .scalar_subquery()
    )
    connectors = (
        select(
            func.jsonb_agg(
                aggregate_order_by(
                    func.jsonb_build_object(
                        literal_column("'connector_id'"),
                        _CONNECTORS.c.connector_id,
                        literal_column("'hub_server_id'"),
                        _CONNECTORS.c.hub_server_id,
                        literal_column("'turned_off'"),
                        _CONNECTORS.c.turned_off,
                    ),
                    _CONNECTORS.c.connector_id,
                )
            )
        )
        .where(_CONNECTORS.c.assistant_id == _ASSISTANTS.c.assistant_id)
        .scalar_subquery()
    )
    return [
        vault_ids.label("credential_vault_ids"),
        assignments.label("mcp_assignments"),
        connectors.label("connectors"),
    ]


def _assistants() -> Select[Any]:
    return select(_ASSISTANTS, *_child_columns())


def _record(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A stored Assistant as services and the API read it."""
    if row is None:
        return None
    connectors = row["connectors"] or []
    servers = {
        str(item["connector_id"]): str(item["hub_server_id"])
        for item in connectors
        if item.get("hub_server_id") is not None
    }
    turned_off = sorted(str(item["connector_id"]) for item in connectors if item.get("turned_off"))
    model_name = row["model_name"]
    return {
        "assistant_id": row["assistant_id"],
        "owner_id": row["owner_id"],
        "org_id": row["org_id"],
        "primary": row["is_primary"],
        "display_name": row["display_name"],
        "icon": row["icon"],
        "description": row["description"],
        "engine_kind": row["engine_kind"],
        "environment_name": row["environment_name"],
        "permission_mode_default": row["permission_mode_default"],
        "system": row["system"],
        "timezone": row["timezone"],
        "model_config_override": {"model_name": model_name} if model_name is not None else None,
        "mcp_servers": row["mcp_servers"],
        "packages": row["packages"],
        "skills": row["skills"],
        "engine_options": row["engine_options"],
        "extension_catalog": row["extension_catalog"],
        "mcp_assignments": list(row["mcp_assignments"] or []),
        "connector_servers": servers or None,
        "connectors_off": turned_off or None,
        "credential_vault_ids": list(row["credential_vault_ids"] or []),
        "credentials_updated_by": row["credentials_updated_by"],
        "credentials_updated_at": to_iso(row["credentials_updated_at"]),
        "main_session_id": row["main_session_id"],
        "workspace_id": row["workspace_id"],
        "version": row["version"],
        "deleted": row["deleted"],
        "created_at": to_iso(row["created_at"]),
        "updated_at": to_iso(row["updated_at"]),
    }


def _listed(row: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """A listed Assistant and its workspace's posture, ``None`` when it has none."""
    record = _record(row)
    assert record is not None
    workspace = (
        {"state": row["workspace_state"], "current_sandbox_id": row["workspace_sandbox_id"]}
        if row["workspace_state"] is not None
        else None
    )
    return record, workspace


def _owned(owner_id: str) -> Select[Any]:
    """The owner's live Assistants joined with their workspaces' posture."""
    return (
        select(
            _ASSISTANTS,
            *_child_columns(),
            _WORKSPACES.c.state.label("workspace_state"),
            _WORKSPACES.c.current_sandbox_id.label("workspace_sandbox_id"),
        )
        .select_from(
            _ASSISTANTS.outerjoin(
                _WORKSPACES, _WORKSPACES.c.assistant_id == _ASSISTANTS.c.assistant_id
            )
        )
        .where(_ASSISTANTS.c.owner_id == owner_id, _ASSISTANTS.c.deleted.is_(False))
    )


def _version_cas(
    assistant_id: str,
    *,
    expected_version: int,
    touch: bool,
    values: Mapping[str, Any] | None = None,
) -> Any:
    """Move a live Assistant from ``expected_version`` to the next, writing ``values``.

    ``touch`` moves ``updated_at`` too. The statement returns the id only when
    the Assistant was still at that version.
    """
    changes: dict[str, Any] = {**(values or {}), "version": _ASSISTANTS.c.version + 1}
    if touch:
        changes["updated_at"] = func.now()
    return (
        update(_ASSISTANTS)
        .where(
            _ASSISTANTS.c.assistant_id == assistant_id,
            _ASSISTANTS.c.version == expected_version,
            _ASSISTANTS.c.deleted.is_(False),
        )
        .values(**changes)
        .returning(_ASSISTANTS.c.assistant_id)
    )


async def _moved(conn: AsyncConnection, statement: Any) -> bool:
    return (await conn.execute(statement)).first() is not None


class AssistantCatalogRepository:
    """Assistants, their bound Vaults, catalog selections and connectors."""

    async def create_assistant(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Insert a new Assistant; the stored record.

        Raises :class:`AssistantExists` when the id is taken or, for a primary
        Assistant, when the owner already has a live one, and
        :class:`AssistantEnvironmentMissing` when the Environment does not exist.
        """
        values = {
            "assistant_id": str(payload["assistant_id"]),
            "owner_id": str(payload["owner_id"]),
            "org_id": payload.get("org_id"),
            "is_primary": bool(payload.get("primary")),
            "display_name": str(payload["display_name"]),
            "icon": payload.get("icon"),
            "description": payload.get("description"),
            "engine_kind": str(payload["engine_kind"]),
            "environment_name": str(payload["environment_name"]),
            "permission_mode_default": payload.get("permission_mode_default"),
            "system": payload.get("system"),
            "timezone": payload.get("timezone"),
            "model_name": _model_name(payload.get("model_config_override")),
            "mcp_servers": payload.get("mcp_servers"),
            "packages": payload.get("packages"),
            "skills": payload.get("skills"),
            "engine_options": payload.get("engine_options"),
        }
        try:
            row = await fetch_one(insert(_ASSISTANTS).values(**values).returning(_ASSISTANTS))
        except UniqueViolation as exc:
            raise AssistantExists(values["assistant_id"]) from exc
        except ForeignKeyViolation as exc:
            raise AssistantEnvironmentMissing(values["environment_name"]) from exc
        assert row is not None
        stored = _record({**row, "credential_vault_ids": [], "mcp_assignments": [], "connectors": []})
        assert stored is not None
        return stored

    async def get_assistant(self, assistant_id: str) -> dict[str, Any] | None:
        """A live Assistant by id."""
        return _record(
            await fetch_one(
                _assistants().where(
                    _ASSISTANTS.c.assistant_id == assistant_id, _ASSISTANTS.c.deleted.is_(False)
                )
            )
        )

    async def list_assistants_by_credential_vault_id(self, vault_id: str) -> list[dict[str, Any]]:
        """Every live Assistant that binds the Credential Vault."""
        target = str(vault_id or "").strip()
        if not target:
            return []
        rows = await fetch_all(
            _assistants()
            .where(
                _ASSISTANTS.c.deleted.is_(False),
                _ASSISTANTS.c.assistant_id.in_(
                    select(_VAULTS.c.assistant_id).where(_VAULTS.c.vault_id == target)
                ),
            )
            .order_by(_ASSISTANTS.c.assistant_id)
        )
        return [record for record in map(_record, rows) if record is not None]

    async def list_assistants_by_environment(self, name: str) -> list[dict[str, Any]]:
        """Every Assistant that names the Environment, deleted ones included.

        A deleted Assistant still names its Environment while conversations it
        left behind may hold compute, which the Environment's cleanup check reads.
        """
        rows = await fetch_all(
            select(
                _ASSISTANTS.c.assistant_id, _ASSISTANTS.c.display_name, _ASSISTANTS.c.deleted
            )
            .where(_ASSISTANTS.c.environment_name == name)
            .order_by(_ASSISTANTS.c.assistant_id)
        )
        return [dict(row) for row in rows]

    async def list_catalog_selections(self) -> list[dict[str, Any]]:
        """Each live Assistant that holds a catalog selection: id, name and the selection."""
        assigned = exists().where(_ASSIGNMENTS.c.assistant_id == _ASSISTANTS.c.assistant_id)
        rows = await fetch_all(
            _assistants()
            .where(
                _ASSISTANTS.c.deleted.is_(False),
                or_(assigned, _ASSISTANTS.c.extension_catalog.is_not(None)),
            )
            .order_by(_ASSISTANTS.c.assistant_id)
        )
        return [
            {
                "assistant_id": record["assistant_id"],
                "display_name": record["display_name"],
                "mcp_assignments": record["mcp_assignments"],
                "extension_catalog": record["extension_catalog"],
            }
            for record in map(_record, rows)
            if record is not None
        ]

    async def list_owner_assistants(
        self, owner_id: str
    ) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        """Every live Assistant the owner has, the most recently edited first, with its workspace."""
        rows = await fetch_all(
            _owned(str(owner_id or "")).order_by(
                _ASSISTANTS.c.updated_at.desc(), _ASSISTANTS.c.assistant_id.desc()
            )
        )
        return [_listed(row) for row in rows]

    async def list_owner_assistants_page(
        self,
        owner_id: str,
        *,
        after: tuple[datetime, str] | None,
        limit: int,
        query: str = "",
        status: str = "all",
    ) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        """One page of :meth:`list_owner_assistants`, narrowed, after ``after``.

        ``after`` is the ``(updated_at, assistant_id)`` of the last row already
        read. ``query`` matches the display name, id, Environment and engine
        case-insensitively; ``status`` keeps Assistants whose workspace is
        ``ready`` or not (``dormant``). Both narrow in the query, so the page is
        the next ``limit`` matching rows.
        """
        if status not in LIST_STATUSES:
            raise ValueError(f"status must be one of {sorted(LIST_STATUSES)}")
        statement = _owned(str(owner_id or ""))
        needle = str(query or "").strip()
        if needle:
            statement = statement.where(
                or_(
                    *(
                        column.icontains(needle, autoescape=True)
                        for column in (
                            _ASSISTANTS.c.display_name,
                            _ASSISTANTS.c.assistant_id,
                            _ASSISTANTS.c.environment_name,
                            _ASSISTANTS.c.engine_kind,
                        )
                    )
                )
            )
        if status == "ready":
            statement = statement.where(_WORKSPACES.c.state == "READY")
        elif status == "dormant":
            statement = statement.where(_WORKSPACES.c.state.is_distinct_from("READY"))
        if after is not None:
            updated_at, assistant_id = after
            statement = statement.where(
                tuple_(_ASSISTANTS.c.updated_at, _ASSISTANTS.c.assistant_id)
                < tuple_(
                    literal(updated_at, _ASSISTANTS.c.updated_at.type),
                    literal(assistant_id, Text),
                )
            )
        rows = await fetch_all(
            statement.order_by(
                _ASSISTANTS.c.updated_at.desc(), _ASSISTANTS.c.assistant_id.desc()
            ).limit(max(1, int(limit)))
        )
        return [_listed(row) for row in rows]

    async def count_owner_assistants(self, owner_id: str) -> tuple[int, int]:
        """How many live Assistants the owner has, and how many of their workspaces are READY."""
        row = await fetch_one(
            select(
                func.count().label("total"),
                func.count().filter(_WORKSPACES.c.state == "READY").label("ready"),
            )
            .select_from(
                _ASSISTANTS.outerjoin(
                    _WORKSPACES, _WORKSPACES.c.assistant_id == _ASSISTANTS.c.assistant_id
                )
            )
            .where(_ASSISTANTS.c.owner_id == str(owner_id or ""), _ASSISTANTS.c.deleted.is_(False))
        )
        assert row is not None
        return int(row["total"]), int(row["ready"])

    async def count_owner_primaries(self, owner_id: str) -> int:
        """How many primary Assistants the owner has had, deleted ones included."""
        return int(
            await fetch_value(
                select(func.count()).where(
                    _ASSISTANTS.c.owner_id == str(owner_id or ""), _ASSISTANTS.c.is_primary.is_(True)
                )
            )
            or 0
        )

    async def get_owner_primary(self, owner_id: str) -> dict[str, Any] | None:
        """The owner's live primary Assistant."""
        return _record(
            await fetch_one(
                _assistants()
                .where(
                    _ASSISTANTS.c.owner_id == str(owner_id or ""),
                    _ASSISTANTS.c.is_primary.is_(True),
                    _ASSISTANTS.c.deleted.is_(False),
                )
                .order_by(_ASSISTANTS.c.created_at, _ASSISTANTS.c.assistant_id)
                .limit(1)
            )
        )

    async def mark_earliest_primary(self, owner_id: str) -> str | None:
        """Mark the owner's earliest live Assistant primary; its id, ``None`` when there is none.

        Raises :class:`AssistantExists` when another Assistant of the owner
        became the live primary first.
        """
        earliest = _ASSISTANTS.alias("earliest")
        first = (
            select(earliest.c.assistant_id)
            .where(earliest.c.owner_id == str(owner_id or ""), earliest.c.deleted.is_(False))
            .order_by(earliest.c.created_at, earliest.c.assistant_id)
            .limit(1)
            .scalar_subquery()
        )
        try:
            row = await fetch_one(
                update(_ASSISTANTS)
                .where(_ASSISTANTS.c.assistant_id == first, _ASSISTANTS.c.deleted.is_(False))
                .values(is_primary=True)
                .returning(_ASSISTANTS.c.assistant_id)
            )
        except UniqueViolation as exc:
            raise AssistantExists(owner_id) from exc
        return str(row["assistant_id"]) if row is not None else None

    async def update_definition(
        self,
        assistant_id: str,
        *,
        expected_version: int,
        values: Mapping[str, Any],
        touch: bool,
    ) -> bool:
        """Write authored ``values`` if the Assistant is still at ``expected_version``.

        ``touch`` moves ``updated_at``, for a write that changed a value.
        ``False`` when a concurrent write moved the version first, or the
        Assistant is gone; nothing is written then.
        """
        changes: dict[str, Any] = {
            key: value for key, value in values.items() if key in _DEFINITION_COLUMNS
        }
        if "model_config_override" in values:
            changes["model_name"] = _model_name(values["model_config_override"])
        unknown = set(values) - _DEFINITION_COLUMNS - {"model_config_override"}
        if unknown:
            raise ValueError(f"not authored Assistant fields: {sorted(unknown)}")

        statement = _version_cas(
            assistant_id, expected_version=expected_version, touch=touch, values=changes
        )
        return await fetch_one(statement) is not None

    async def set_vault_bindings(
        self,
        assistant_id: str,
        *,
        expected_version: int,
        vault_ids: Sequence[str],
        updated_by: str,
        touch: bool,
    ) -> bool:
        """Replace the bound Vaults, in order, if the Assistant is still at ``expected_version``.

        Raises :class:`AssistantVaultMissing` when a Vault does not exist.
        """

        async def work(conn: AsyncConnection) -> bool:
            if not await _moved(
                conn,
                _version_cas(
                    assistant_id,
                    expected_version=expected_version,
                    touch=touch,
                    values={
                        "credentials_updated_by": updated_by,
                        "credentials_updated_at": func.now(),
                    },
                ),
            ):
                return False
            await conn.execute(delete(_VAULTS).where(_VAULTS.c.assistant_id == assistant_id))
            if vault_ids:
                await conn.execute(
                    insert(_VAULTS),
                    [
                        {"assistant_id": assistant_id, "position": position, "vault_id": vault_id}
                        for position, vault_id in enumerate(vault_ids)
                    ],
                )
            return True

        try:
            return await transaction(work)
        except ForeignKeyViolation as exc:
            raise AssistantVaultMissing(",".join(vault_ids)) from exc

    async def set_extension_selection(
        self,
        assistant_id: str,
        *,
        expected_version: int,
        extension_catalog: Mapping[str, Any] | None,
        mcp_assignments: Sequence[Mapping[str, Any]],
    ) -> bool:
        """Replace the catalog selection if the Assistant is still at ``expected_version``."""

        async def work(conn: AsyncConnection) -> bool:
            if not await _moved(
                conn,
                _version_cas(
                    assistant_id,
                    expected_version=expected_version,
                    touch=True,
                    values={"extension_catalog": extension_catalog},
                ),
            ):
                return False
            await conn.execute(
                delete(_ASSIGNMENTS).where(_ASSIGNMENTS.c.assistant_id == assistant_id)
            )
            if mcp_assignments:
                await conn.execute(
                    insert(_ASSIGNMENTS),
                    [
                        {
                            "assistant_id": assistant_id,
                            "position": position,
                            "provider": str(item["provider"]),
                            "item_id": str(item["item_id"]),
                        }
                        for position, item in enumerate(mcp_assignments)
                    ],
                )
            return True

        return await transaction(work)

    async def set_connectors(
        self,
        assistant_id: str,
        *,
        expected_version: int,
        servers: Mapping[str, str],
        turned_off: Sequence[str],
    ) -> bool:
        """Replace the connectors if the Assistant is still at ``expected_version``.

        ``servers`` maps each connector the Assistant offers to the hub's
        virtual server; ``turned_off`` names the apps its owner turned off.
        """
        off = set(turned_off)
        rows = [
            {
                "assistant_id": assistant_id,
                "connector_id": connector_id,
                "hub_server_id": servers.get(connector_id),
                "turned_off": connector_id in off,
            }
            for connector_id in sorted(set(servers) | off)
        ]

        async def work(conn: AsyncConnection) -> bool:
            if not await _moved(
                conn, _version_cas(assistant_id, expected_version=expected_version, touch=True)
            ):
                return False
            await conn.execute(delete(_CONNECTORS).where(_CONNECTORS.c.assistant_id == assistant_id))
            if rows:
                await conn.execute(insert(_CONNECTORS), rows)
            return True

        return await transaction(work)

    async def set_main_session(self, assistant_id: str, session_id: str) -> bool:
        """Record the Assistant's main conversation.

        Raises :class:`AssistantSessionMissing` when the Session does not exist.
        """
        try:
            changed = await execute(
                update(_ASSISTANTS)
                .where(_ASSISTANTS.c.assistant_id == assistant_id, _ASSISTANTS.c.deleted.is_(False))
                .values(main_session_id=session_id)
            )
        except ForeignKeyViolation as exc:
            raise AssistantSessionMissing(session_id) from exc
        return changed == 1

    async def mint_workspace_id(self, assistant_id: str, candidate: str) -> str | None:
        """The Assistant's durable workspace id, recording ``candidate`` when it has none.

        One statement: concurrent first mounts all receive the id the first of
        them recorded. ``None`` when the Assistant does not exist.
        """
        row = await fetch_one(
            update(_ASSISTANTS)
            .where(_ASSISTANTS.c.assistant_id == assistant_id, _ASSISTANTS.c.deleted.is_(False))
            .values(workspace_id=func.coalesce(_ASSISTANTS.c.workspace_id, candidate))
            .returning(_ASSISTANTS.c.workspace_id)
        )
        return str(row["workspace_id"]) if row is not None else None

    async def delete_assistant(self, assistant_id: str, *, owner_id: str) -> bool:
        """Delete the owner's Assistant together with its workspace and Vault bindings.

        One transaction marks the row deleted, removes the workspace (its
        profiles with it) and the Vault bindings, under the workspace's row
        lock. A workspace that still names a box is the only record of that box,
        so the delete is refused with :class:`AssistantSandboxRetained` and
        nothing changes. ``False`` when the owner has no such live Assistant.

        A workspace insert waits on this transaction (it shares-locks the live
        Assistant) and then finds the Assistant deleted, so no workspace can
        appear under a deleted Assistant.
        """

        async def work(conn: AsyncConnection) -> bool:
            deleted = await conn.execute(
                update(_ASSISTANTS)
                .where(
                    _ASSISTANTS.c.assistant_id == assistant_id,
                    _ASSISTANTS.c.owner_id == owner_id,
                    _ASSISTANTS.c.deleted.is_(False),
                )
                .values(deleted=True)
                .returning(_ASSISTANTS.c.assistant_id)
            )
            if deleted.first() is None:
                return False
            held = (
                await conn.execute(
                    select(_WORKSPACES.c.current_sandbox_id)
                    .where(_WORKSPACES.c.assistant_id == assistant_id)
                    .with_for_update()
                )
            ).first()
            if held is not None and held.current_sandbox_id is not None:
                raise AssistantSandboxRetained(str(held.current_sandbox_id))
            await conn.execute(delete(_WORKSPACES).where(_WORKSPACES.c.assistant_id == assistant_id))
            await conn.execute(delete(_VAULTS).where(_VAULTS.c.assistant_id == assistant_id))
            return True

        return await transaction(work)
