"""Agents: the authored definition and the runtime record, in typed tables.

An Agent row holds every authored field of the definition; the categories it
is listed under, the vaults bound to it, the MCP servers assigned to it from
extension catalogs and the connector servers it offers are rows of their own
tables, keyed by the Agent. Reads hand services the familiar document shape.

Every authored write goes through :meth:`AgentRepository.update_definition`,
which applies the change only while the Agent is at the version the writer
read and moves the version with it, so two writers that read the same
definition cannot both succeed.

The runtime state of the Agent's boxes is kept apart from the definition: one
``agent_runtimes`` row per Agent with its resident box, runtime generation and
client pool, and tables of their own for its prepared slots, box admissions,
displaced client pools, gateway keys and undestroyed sandboxes. Every runtime
write is one statement or a short transaction. A write that changes the
prepared slots or admits a conversation locks the runtime row first, and a
writer whose decision rests on what :meth:`AgentRepository.read_ledger`
returned names the revision it read, so a change made since sends it back to
read again.

Writes that acquire something for an Agent (a box, a seat, an admission, a
client pool, a published generation) apply only to a live Agent; writes that
release what an Agent holds apply to a deleted one too.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import (
    Boolean,
    Text,
    and_,
    any_,
    cast,
    case,
    column as sql_column,
    delete,
    false,
    func,
    insert,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql import ColumnElement, Select

from astrabox.persistence.repository.assistant_catalog_repository import _ChildObject, _OrderedChildren
from astrabox.persistence.sql.runtime_expressions import string_array_items
from astrabox.persistence.sql import (
    ForeignKeyViolation,
    execute,
    fetch_all,
    fetch_one,
    fetch_value,
    from_iso,
    to_iso,
    transaction,
)
from astrabox.persistence.sql.models.agents import (
    Agent,
    AgentBoxAdmission,
    AgentCategoryLink,
    AgentConnectorServer,
    AgentMCPAssignment,
    AgentPreparedSlot,
    AgentRetiringClientPool,
    AgentRuntime,
    AgentSlotGatewayEntry,
    AgentUndestroyedSandbox,
    AgentVaultBinding,
)
from astrabox.persistence.sql.models.sessions import Session
from astrabox.persistence.sql.statements import insert_if_absent_primary_key, upsert_primary_key
from astrabox.persistence.provider import get_persistence
from astrabox.seams.persistence_coordination import SessionNotice

_AGENTS = Agent.__table__
_SESSIONS = Session.__table__
_LINKS = AgentCategoryLink.__table__
_VAULTS = AgentVaultBinding.__table__
_ASSIGNMENTS = AgentMCPAssignment.__table__
_CONNECTORS = AgentConnectorServer.__table__
_RUNTIMES = AgentRuntime.__table__
_SLOTS = AgentPreparedSlot.__table__
_ADMISSIONS = AgentBoxAdmission.__table__
_RETIRING = AgentRetiringClientPool.__table__
_GATEWAY_ENTRIES = AgentSlotGatewayEntry.__table__
_UNDESTROYED = AgentUndestroyedSandbox.__table__

_LIVE = _AGENTS.c.state != "DELETED"

#: Where each Agent read carries the key the console list is ordered by.
NAME_SORT_KEY_FIELD = "_name_key"

#: Authored fields stored in a column of the same name.
_COLUMN_FIELDS = (
    "name",
    "description",
    "use_cases",
    "model",
    "system",
    "engine_options",
    "artifacts_enabled",
    "skills",
    "mcp_servers",
    "packages",
    "default_repo",
    "terminal_panel",
    "diff_panel",
    "exposure_mode",
    "idle_hibernate_seconds",
    "prepared_capacity",
    "enabled",
    "environment_name",
    "visibility",
    "admins",
    "allowed_user_ids",
)

#: Authored fields stored in tables of their own.
_CHILD_FIELDS = ("categories", "credential_vault_ids", "mcp_assignments", "connector_servers")

#: Every field an authoring write may change.
AUTHORED_FIELDS = frozenset((*_COLUMN_FIELDS, *_CHILD_FIELDS, "display_meta", "extension_catalog"))

#: Fields set once, when the Agent is created.
_CREATE_FIELDS = frozenset({"agent_id", "created_by", "org_id", "state", "version", "updated_by"})

#: Runtime record columns a read carries when set, by the key services read.
_RUNTIME_KEYS = {
    "sandbox_id": "sandbox_id",
    "sandbox_backend": "sandbox_backend",
    "resident_generation": "_resident_sandbox_generation",
    "runtime_generation": "_runtime_generation",
    "sandbox_generation": "_sandbox_generation",
    "client_pool_epoch": "_client_pool_epoch",
    "client_pool_name": "_client_pool_name",
    "client_pool_backend": "_client_pool_backend",
    "prewarm_revision": "_prewarm_revision",
    "prepared_runtime_generation": "_prepared_runtime_generation",
    "prepared_runtime_error": "_prepared_runtime_error",
    "last_conversation_uid": "conversation_uid_cursor",
    "workspace_id": "workspace_id",
}

_RUNTIME_COLUMNS = tuple(
    _RUNTIMES.c[name]
    for name in (
        *_RUNTIME_KEYS,
        "expires_at",
        "seats_per_box",
        "capacity_last_miss_at",
        "resources_operation_provider",
        "resources_operation_id",
        "resources_operation_deadline",
        "slots_revision",
        "admissions_revision",
    )
)

#: Slot manifest keys stored in a column of the same name; the rest of a
#: manifest is kept as given.
_SLOT_COLUMNS = (
    "slot_id",
    "state",
    "placement",
    "sandbox_id",
    "sandbox_backend",
    "runtime_generation",
    "claimed_session_id",
    "retire_reason",
)
_SLOT_TIMES = ("prepared_at", "claimed_at", "retiring_at")

#: Slot manifest keys a read omits when unset, as a manifest that never had them.
_OPTIONAL_SLOT_KEYS = ("runtime_generation", "claimed_session_id", "retire_reason", *_SLOT_TIMES)

#: Columns a read omits from the document when empty, as a document without
#: the key would.
_OPTIONAL_FIELDS = (
    "description",
    "system",
    "engine_options",
    "mcp_servers",
    "default_repo",
    "exposure_mode",
    "idle_hibernate_seconds",
    "environment_name",
    "org_id",
    "updated_by",
)

#: Foreign keys a write can name a missing row through, by the reference.
_REFERENCES = {
    "fk_agents_environment_name_environments": "environment",
    "fk_agent_category_links_category_id_agent_categories": "category",
    "fk_agent_vault_bindings_vault_id_vaults": "vault",
}


class AgentReferenceMissing(Exception):
    """A write named an Environment, category or vault that does not exist."""

    def __init__(self, reference: str) -> None:
        super().__init__(f"the Agent names a {reference} that does not exist")
        self.reference = reference


def agent_name_sort_key(name: Any) -> str:
    """The Agent list's order for ``name``: the name without its case.

    Stored with the row rather than computed in SQL, because ``str.casefold``
    and PostgreSQL's ``lower`` fold some characters differently, and a page that
    resumes after a key has to compare it the way the order was built.
    """

    return str(name or "").casefold()


class _ArrayMember(FunctionElement[bool]):
    """Exact string membership in the authored Agent access list."""

    type = Boolean()
    inherit_cache = True


@compiles(_ArrayMember, "postgresql")
def _pg_array_member(element: _ArrayMember, compiler: SQLCompiler, **kw: Any) -> str:
    values, value = element.clauses
    return compiler.process(value == any_(values), **kw)


@compiles(_ArrayMember, "mysql")
def _mysql_array_member(element: _ArrayMember, compiler: SQLCompiler, **kw: Any) -> str:
    values, value = element.clauses
    return compiler.process(func.json_contains(values, func.json_quote(value)), **kw)


def _ordered(column: Any, order: Any, owner: Any) -> Any:
    """One Agent's child values in their stored order (NULL for none)."""
    return (
        select(_OrderedChildren(cast(column, Text), order))
        .where(owner == _AGENTS.c.agent_id)
        .limit(1)
        .scalar_subquery()
    )


def _definition() -> Select[Any]:
    """Every Agent column with its child tables and runtime record, one row per Agent."""
    return select(
        _AGENTS,
        *_RUNTIME_COLUMNS,
        _ordered(_UNDESTROYED.c.sandbox_id, _UNDESTROYED.c.sandbox_id,
                 _UNDESTROYED.c.agent_id).label("undestroyed_sandbox_ids"),
        _ordered(_LINKS.c.category_id, _LINKS.c.position, _LINKS.c.agent_id).label("categories"),
        _ordered(_VAULTS.c.vault_id, _VAULTS.c.position, _VAULTS.c.agent_id).label("credential_vault_ids"),
        select(_OrderedChildren(_ChildObject(
            "provider", cast(_ASSIGNMENTS.c.provider, Text),
            "item_id", cast(_ASSIGNMENTS.c.item_id, Text),
        ), _ASSIGNMENTS.c.position))
        .where(_ASSIGNMENTS.c.agent_id == _AGENTS.c.agent_id)
        .limit(1).scalar_subquery().label("mcp_assignments"),
        select(_OrderedChildren(_ChildObject(
            "connector_id", cast(_CONNECTORS.c.connector_id, Text),
            "hub_server_id", cast(_CONNECTORS.c.hub_server_id, Text),
        ), _CONNECTORS.c.connector_id))
        .where(_CONNECTORS.c.agent_id == _AGENTS.c.agent_id)
        .limit(1).scalar_subquery().label("connector_servers"),
    ).select_from(_AGENTS.outerjoin(_RUNTIMES, _RUNTIMES.c.agent_id == _AGENTS.c.agent_id))


def _view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One Agent as the document services read."""
    display_meta: dict[str, Any] = {"tags": list(row["tags"] or [])}
    for key in ("display_name", "icon"):
        if row[key] is not None:
            display_meta[key] = row[key]
    doc: dict[str, Any] = {
        "agent_id": row["agent_id"],
        "name": row["name"],
        NAME_SORT_KEY_FIELD: row["name_key"],
        "display_meta": display_meta,
        "use_cases": list(row["use_cases"] or []),
        "categories": list(row["categories"] or []),
        "model": row["model"],
        "artifacts_enabled": row["artifacts_enabled"],
        "skills": list(row["skills"] or []),
        "packages": list(row["packages"] or []),
        "terminal_panel": row["terminal_panel"],
        "diff_panel": row["diff_panel"],
        "prepared_capacity": row["prepared_capacity"],
        "enabled": row["enabled"],
        "visibility": row["visibility"],
        "admins": list(row["admins"] or []),
        "allowed_user_ids": list(row["allowed_user_ids"] or []),
        "credential_vault_ids": list(row["credential_vault_ids"] or []),
        "mcp_assignments": list(row["mcp_assignments"] or []),
        "extension_catalog": {
            "skill_ids": list(row["catalog_skill_ids"] or []),
            "skills": list(row["catalog_skills"] or []),
        },
        "version": row["version"],
        "state": row["state"],
        "created_by": row["created_by"],
        "created_at": to_iso(row["created_at"]),
        "updated_at": to_iso(row["updated_at"]),
    }
    for field in _OPTIONAL_FIELDS:
        if row[field] is not None:
            doc[field] = row[field]
    if row["connector_servers"]:
        doc["connector_servers"] = {
            item["connector_id"]: item["hub_server_id"] for item in row["connector_servers"]
        }
    for column, key in _RUNTIME_KEYS.items():
        if row[column] is not None:
            doc[key] = row[column]
    if row["expires_at"] is not None:
        doc["expires_at"] = to_iso(row["expires_at"])
    if row["undestroyed_sandbox_ids"]:
        doc["undestroyed_sandbox_ids"] = sorted(row["undestroyed_sandbox_ids"])
    doc["_seats_per_box"] = int(row["seats_per_box"] or 0)
    if row["capacity_last_miss_at"] is not None:
        doc["_prepared_capacity_last_miss_at"] = to_iso(row["capacity_last_miss_at"])
    if row["resources_operation_id"] is not None:
        doc["_prepared_resources_operation"] = {
            "provider": row["resources_operation_provider"],
            "operation_id": row["resources_operation_id"],
            "deadline": row["resources_operation_deadline"],
        }
    return doc


def _slot_json() -> ColumnElement[Any]:
    """The stored receipt and slot columns, projected without a native JSON merge."""
    pairs: list[Any] = ["manifest", _SLOTS.c.manifest]
    for name in _SLOT_COLUMNS:
        pairs += [name, cast(_SLOTS.c[name], Text)]
    for name in _SLOT_TIMES:
        pairs += [name, _SLOTS.c[name]]
    return _ChildObject(*pairs)


def _slot_manifest(item: Mapping[str, Any]) -> dict[str, Any]:
    manifest = {**item["manifest"], **{key: item[key] for key in _SLOT_COLUMNS}}
    for key in _SLOT_TIMES:
        value = from_iso(item[key])
        manifest[key] = value.astimezone(timezone.utc).isoformat(timespec="microseconds") if value else None
    for key in _OPTIONAL_SLOT_KEYS:
        if manifest.get(key) is None:
            manifest.pop(key, None)
    return manifest


def _slot_values(agent_id: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """A slot manifest as a row of ``agent_prepared_slots``."""
    values: dict[str, Any] = {
        "agent_id": agent_id,
        "manifest": {
            key: value
            for key, value in manifest.items()
            if key not in _SLOT_COLUMNS and key not in _SLOT_TIMES
        },
    }
    for key in _SLOT_COLUMNS:
        values[key] = manifest.get(key)
    for key in _SLOT_TIMES:
        values[key] = from_iso(manifest.get(key))
    return values


def _ledger() -> Select[Any]:
    """:func:`_definition` with the Agent's slots, admissions and displaced pools.

    One statement, so the revisions and the rows they count are read together.
    """
    slots = (
        select(_OrderedChildren(
            _slot_json(), _SLOTS.c.prepared_at.is_(None), _SLOTS.c.prepared_at, _SLOTS.c.slot_id,
        ))
        .where(_SLOTS.c.agent_id == _AGENTS.c.agent_id)
        .limit(1).scalar_subquery().label("prepared_slots")
    )
    admissions = (
        select(_OrderedChildren(_ChildObject(
            "sandbox_id", cast(_ADMISSIONS.c.sandbox_id, Text),
            "session_id", cast(_ADMISSIONS.c.session_id, Text),
            "at", _ADMISSIONS.c.admitted_at,
        ), _ADMISSIONS.c.admitted_at))
        .where(_ADMISSIONS.c.agent_id == _AGENTS.c.agent_id)
        .limit(1).scalar_subquery().label("box_admissions")
    )
    retiring = (
        select(_OrderedChildren(_ChildObject(
            "name", cast(_RETIRING.c.pool_name, Text), "backend", cast(_RETIRING.c.backend, Text),
        ), _RETIRING.c.recorded_at, _RETIRING.c.pool_name))
        .where(_RETIRING.c.agent_id == _AGENTS.c.agent_id)
        .limit(1).scalar_subquery().label("retiring_client_pools")
    )
    return _definition().add_columns(slots, admissions, retiring)


def _admission_time(value: str) -> float:
    at = from_iso(value)
    assert at is not None
    return at.timestamp()


def _ledger_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One Agent with its shared-box ledger, as :meth:`AgentRepository.read_ledger` returns it."""
    doc = _view(row)
    doc["_prepared_slots"] = [_slot_manifest(item) for item in row["prepared_slots"] or []]
    doc["box_admissions"] = [
        {**item, "at": _admission_time(item["at"])} for item in row["box_admissions"] or []
    ]
    doc["_retiring_client_pools"] = [dict(item) for item in row["retiring_client_pools"] or []]
    doc["_slots_revision"] = int(row["slots_revision"] or 0)
    doc["_admissions_revision"] = int(row["admissions_revision"] or 0)
    return doc


def _live(agent_id: str) -> Select[Any]:
    """``agent_id`` while it is not deleted, for a runtime write's condition."""
    return select(_AGENTS.c.agent_id).where(_AGENTS.c.agent_id == agent_id, _LIVE)


async def _definition_holds(
    conn: AsyncConnection, agent_id: str, *conditions: ColumnElement[bool]
) -> bool:
    """Whether the live Agent meets ``conditions``, keeping its definition so until commit."""
    row = (
        await conn.execute(
            select(_AGENTS.c.agent_id)
            .where(_AGENTS.c.agent_id == agent_id, _LIVE, *conditions)
            .with_for_update(read=True)
        )
    ).first()
    return row is not None


async def _admit(
    conn: AsyncConnection,
    agent_id: str,
    *,
    sandbox_id: str,
    session_id: str,
    young_since: datetime,
) -> None:
    """Charge ``session_id`` to a box of the Agent, forgetting admissions older than ``young_since``."""
    await conn.execute(
        delete(_ADMISSIONS).where(
            _ADMISSIONS.c.agent_id == agent_id, _ADMISSIONS.c.admitted_at < young_since
        )
    )
    await conn.execute(upsert_primary_key(conn, _ADMISSIONS, {
        "agent_id": agent_id, "sandbox_id": sandbox_id, "session_id": session_id,
        "admitted_at": datetime.now(timezone.utc),
    }, update_columns=["admitted_at"]))


def _operation_values(operation: Mapping[str, Any] | None) -> dict[str, Any]:
    if operation is None:
        return {
            "resources_operation_provider": None,
            "resources_operation_id": None,
            "resources_operation_deadline": None,
        }
    return {
        "resources_operation_provider": str(operation["provider"]),
        "resources_operation_id": str(operation["operation_id"]),
        "resources_operation_deadline": float(operation["deadline"]),
    }


def _operation_is(operation: Mapping[str, Any] | None) -> ColumnElement[bool]:
    if operation is None:
        return _RUNTIMES.c.resources_operation_id.is_(None)
    return and_(
        _RUNTIMES.c.resources_operation_provider == str(operation["provider"]),
        _RUNTIMES.c.resources_operation_id == str(operation["operation_id"]),
        _RUNTIMES.c.resources_operation_deadline == float(operation["deadline"]),
    )


#: The runtime record columns a generation fence compares, by the key a read
#: carries them under.
_GENERATION_FENCE = {
    "_prewarm_revision": _RUNTIMES.c.prewarm_revision,
    "_client_pool_epoch": _RUNTIMES.c.client_pool_epoch,
    "_runtime_generation": _RUNTIMES.c.runtime_generation,
    "_sandbox_generation": _RUNTIMES.c.sandbox_generation,
}


class _NothingChanged(Exception):
    """The row a short transaction meant to change was not as read; roll it back."""


def _column_values(fields: Mapping[str, Any]) -> dict[str, Any]:
    """The Agent columns ``fields`` sets, in their stored form."""
    values: dict[str, Any] = {}
    for field in _COLUMN_FIELDS:
        if field not in fields:
            continue
        value = fields[field]
        if field in ("use_cases", "admins", "allowed_user_ids", "skills", "packages"):
            value = list(value or [])
        elif field in ("artifacts_enabled", "enabled"):
            value = value is not False
        elif field in ("terminal_panel", "diff_panel"):
            value = bool(value)
        elif field == "prepared_capacity":
            value = int(value or 0)
        values[field] = value
    if "name" in values:
        values["name_key"] = agent_name_sort_key(values["name"])
    if "display_meta" in fields:
        display_meta = fields["display_meta"] if isinstance(fields["display_meta"], dict) else {}
        values["display_name"] = display_meta.get("display_name")
        values["icon"] = display_meta.get("icon")
        values["tags"] = list(display_meta.get("tags") or [])
    if "extension_catalog" in fields:
        catalog = (
            fields["extension_catalog"] if isinstance(fields["extension_catalog"], dict) else {}
        )
        values["catalog_skill_ids"] = list(catalog.get("skill_ids") or [])
        values["catalog_skills"] = list(catalog.get("skills") or [])
    return values


def _unique(values: Iterable[Any]) -> list[str]:
    seen: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _child_values(fields: Mapping[str, Any]) -> dict[str, Any]:
    """The child collections ``fields`` sets, in the form a read returns them."""
    values: dict[str, Any] = {}
    for field in ("categories", "credential_vault_ids"):
        if field in fields:
            values[field] = _unique(fields[field] or [])
    if "mcp_assignments" in fields:
        assignments: list[dict[str, str]] = []
        for entry in fields["mcp_assignments"] or []:
            if not isinstance(entry, dict):
                continue
            pair = {
                "provider": str(entry.get("provider") or "").strip().lower(),
                "item_id": str(entry.get("item_id") or "").strip(),
            }
            if all(pair.values()) and pair not in assignments:
                assignments.append(pair)
        values["mcp_assignments"] = assignments
    if "connector_servers" in fields:
        values["connector_servers"] = {
            str(name): str(server) for name, server in (fields["connector_servers"] or {}).items()
        }
    return values


async def _replace_children(conn: AsyncConnection, agent_id: str, children: Mapping[str, Any]) -> None:
    """Replace each child collection ``children`` names (as :func:`_child_values` forms it)."""
    if "categories" in children:
        await conn.execute(delete(_LINKS).where(_LINKS.c.agent_id == agent_id))
        if children["categories"]:
            await conn.execute(
                insert(_LINKS),
                [
                    {"agent_id": agent_id, "category_id": category_id, "position": position}
                    for position, category_id in enumerate(children["categories"])
                ],
            )
    if "credential_vault_ids" in children:
        await conn.execute(delete(_VAULTS).where(_VAULTS.c.agent_id == agent_id))
        if children["credential_vault_ids"]:
            await conn.execute(
                insert(_VAULTS),
                [
                    {"agent_id": agent_id, "vault_id": vault_id, "position": position}
                    for position, vault_id in enumerate(children["credential_vault_ids"])
                ],
            )
    if "mcp_assignments" in children:
        await conn.execute(delete(_ASSIGNMENTS).where(_ASSIGNMENTS.c.agent_id == agent_id))
        if children["mcp_assignments"]:
            await conn.execute(
                insert(_ASSIGNMENTS),
                [
                    {"agent_id": agent_id, "position": position, **assignment}
                    for position, assignment in enumerate(children["mcp_assignments"])
                ],
            )
    if "connector_servers" in children:
        await conn.execute(delete(_CONNECTORS).where(_CONNECTORS.c.agent_id == agent_id))
        if children["connector_servers"]:
            await conn.execute(
                insert(_CONNECTORS),
                [
                    {"agent_id": agent_id, "connector_id": name, "hub_server_id": server}
                    for name, server in children["connector_servers"].items()
                ],
            )


def _stored_children(row: Mapping[str, Any]) -> dict[str, Any]:
    """An Agent row's child collections in the form :func:`_child_values` gives."""
    return {
        "categories": list(row["categories"] or []),
        "credential_vault_ids": list(row["credential_vault_ids"] or []),
        "mcp_assignments": list(row["mcp_assignments"] or []),
        "connector_servers": {
            item["connector_id"]: item["hub_server_id"] for item in row["connector_servers"] or []
        },
    }


async def _read(conn: AsyncConnection, agent_id: str) -> dict[str, Any]:
    row = (await conn.execute(_definition().where(_AGENTS.c.agent_id == agent_id))).one()
    return _view(row._mapping)


def _missing_reference(exc: ForeignKeyViolation) -> AgentReferenceMissing:
    return AgentReferenceMissing(_REFERENCES.get(str(exc.constraint or ""), "reference"))


def visible_to(viewer: str, *, platform_admin: bool) -> ColumnElement[bool]:
    """The Agents ``viewer`` may see and use: ``agent_access.can_view_agent`` in SQL."""
    if platform_admin:
        return true()
    public = _AGENTS.c.visibility == "public"
    me = str(viewer or "").strip()
    if not me:
        return public
    return or_(
        public,
        _AGENTS.c.created_by == me,
        _ArrayMember(_AGENTS.c.admins, me),
        and_(_AGENTS.c.visibility == "allowlist", _ArrayMember(_AGENTS.c.allowed_user_ids, me)),
    )


def manageable_by(viewer: str, *, platform_admin: bool) -> ColumnElement[bool]:
    """The Agents ``viewer`` may manage: ``agent_access.can_manage_agent`` in SQL."""
    if platform_admin:
        return true()
    me = str(viewer or "").strip()
    if not me:
        return false()
    return or_(_AGENTS.c.created_by == me, _ArrayMember(_AGENTS.c.admins, me))


class AgentRepository:
    """Agents: their authored definition and their runtime record."""

    # ── definition ─────────────────────────────────────────────────────────

    async def create_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Store a new Agent; raises :class:`AgentReferenceMissing` for an unknown reference."""
        unknown = sorted(set(payload) - AUTHORED_FIELDS - _CREATE_FIELDS - {"created_at", "updated_at"})
        if unknown:
            raise ValueError(f"an Agent has no fields {unknown}")
        agent_id = str(payload["agent_id"])
        values = {
            **_column_values(payload),
            **{key: payload[key] for key in _CREATE_FIELDS if key in payload},
        }
        for key in ("created_at", "updated_at"):
            if payload.get(key):
                values[key] = from_iso(payload[key])

        async def create(conn: AsyncConnection) -> dict[str, Any]:
            await conn.execute(insert(_AGENTS).values(**values))
            await conn.execute(insert(_RUNTIMES).values(agent_id=agent_id))
            await _replace_children(conn, agent_id, _child_values(payload))
            return await _read(conn, agent_id)

        try:
            return await transaction(create)
        except ForeignKeyViolation as exc:
            raise _missing_reference(exc) from exc

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        """One Agent, or ``None`` when there is none or it is deleted."""
        row = await fetch_one(_definition().where(_AGENTS.c.agent_id == agent_id, _LIVE))
        return _view(row) if row is not None else None

    async def has_agents(self) -> bool:
        """Whether any Agent that is not deleted exists."""
        return bool(await fetch_value(select(select(_AGENTS.c.agent_id).where(_LIVE).exists())))

    async def update_definition(
        self,
        agent_id: str,
        *,
        expected_version: int,
        changes: dict[str, Any],
        updated_by: str | None,
    ) -> dict[str, Any] | None:
        """Apply authored ``changes`` while the Agent is at ``expected_version``.

        A change moves the version by one and stamps ``updated_at`` and
        ``updated_by``; changes that leave every stored value as it is write
        nothing. Returns the Agent as stored afterwards, or ``None`` when its
        version moved since it was read or it is deleted. Raises
        :class:`AgentReferenceMissing` when a change names an Environment,
        category or vault that does not exist.
        """
        unknown = sorted(set(changes) - AUTHORED_FIELDS)
        if unknown:
            raise ValueError(f"not authored Agent fields: {unknown}")
        values = _column_values(changes)
        children = _child_values(changes)

        async def change(conn: AsyncConnection) -> dict[str, Any] | None:
            version = await conn.scalar(
                select(_AGENTS.c.version)
                .where(_AGENTS.c.agent_id == agent_id, _LIVE)
                .with_for_update()
            )
            if version is None or version != expected_version:
                return None
            # Lock only the definition, including on dialects without FOR UPDATE OF.
            stored = (await conn.execute(
                _definition().where(_AGENTS.c.agent_id == agent_id)
            )).mappings().one()
            changed_children = {
                field: value
                for field, value in children.items()
                if _stored_children(stored)[field] != value
            }
            if not changed_children and all(
                stored[column] == value for column, value in values.items()
            ):
                return _view(stored)
            await conn.execute(
                update(_AGENTS)
                .where(_AGENTS.c.agent_id == agent_id)
                .values(
                    **values,
                    version=_AGENTS.c.version + 1,
                    updated_at=func.now(),
                    updated_by=updated_by,
                )
            )
            await _replace_children(conn, agent_id, changed_children)
            return await _read(conn, agent_id)

        try:
            return await transaction(change)
        except ForeignKeyViolation as exc:
            raise _missing_reference(exc) from exc

    async def mark_deleting(self, agent_id: str) -> bool:
        """Record that the Agent's deletion has started."""
        return (
            await execute(
                update(_AGENTS)
                .where(_AGENTS.c.agent_id == agent_id, _LIVE)
                .values(state="DELETING")
            )
        ) == 1

    async def soft_delete(self, agent_id: str) -> bool:
        """Hide the Agent; its row stays for the conversations that name it."""
        async def write(conn: AsyncConnection) -> bool:
            result = await conn.execute(
                update(_AGENTS)
                .where(_AGENTS.c.agent_id == agent_id, _LIVE)
                .values(state="DELETED")
            )
            if result.rowcount != 1:
                return False
            sessions = await conn.execute(
                select(_SESSIONS.c.session_id).where(
                    _SESSIONS.c.agent_id == agent_id, _SESSIONS.c.deleted.is_(False)
                )
            )
            for session_id in sessions.scalars().all():
                await get_persistence().coordination.notify(
                    conn, SessionNotice(session_id=str(session_id), kind="session", seq=0),
                )
            return True

        return await transaction(write)

    # ── lists ──────────────────────────────────────────────────────────────

    async def list_visible_agents(
        self, viewer: str, *, platform_admin: bool
    ) -> list[dict[str, Any]]:
        """Every live Agent ``viewer`` may see, the most recently edited first."""
        rows = await fetch_all(
            _definition()
            .where(_LIVE, visible_to(viewer, platform_admin=platform_admin))
            .order_by(_AGENTS.c.updated_at.desc(), _AGENTS.c.agent_id.desc())
        )
        return [_view(row) for row in rows]

    async def list_manageable_agents(
        self, viewer: str, *, platform_admin: bool
    ) -> list[dict[str, Any]]:
        """Every live Agent ``viewer`` may manage, the most recently edited first."""
        rows = await fetch_all(
            _definition()
            .where(_LIVE, manageable_by(viewer, platform_admin=platform_admin))
            .order_by(_AGENTS.c.updated_at.desc(), _AGENTS.c.agent_id.desc())
        )
        return [_view(row) for row in rows]

    async def manageable_agent_ids(self, viewer: str, *, platform_admin: bool) -> list[str]:
        """The ids of the live Agents ``viewer`` may manage, sorted."""
        rows = await fetch_all(
            select(_AGENTS.c.agent_id)
            .where(_LIVE, manageable_by(viewer, platform_admin=platform_admin))
            .order_by(_AGENTS.c.agent_id)
        )
        return [str(row["agent_id"]) for row in rows]

    async def count_visible_agents(
        self, viewer: str, *, platform_admin: bool
    ) -> tuple[int, int]:
        """How many live Agents ``viewer`` may see, and how many of them are enabled."""
        row = await fetch_one(
            select(
                func.count().label("total"),
                func.count(case((_AGENTS.c.enabled, 1))).label("enabled"),
            ).where(_LIVE, visible_to(viewer, platform_admin=platform_admin))
        )
        return (int(row["total"]), int(row["enabled"])) if row is not None else (0, 0)

    async def list_visible_agents_page(
        self,
        viewer: str,
        *,
        platform_admin: bool,
        after: tuple[str, str] | None,
        limit: int,
        needle: str = "",
        enabled: bool | None = None,
    ) -> list[dict[str, Any]]:
        """One page of the live Agents ``viewer`` may see, by name regardless of case.

        Rows come in ``(name_key, agent_id)`` order after ``after``.
        ``needle`` keeps the Agents whose name, display name or model contains
        it, ignoring case; ``enabled`` keeps the enabled or the disabled ones.
        """
        conditions: list[ColumnElement[bool]] = [
            _LIVE,
            visible_to(viewer, platform_admin=platform_admin),
        ]
        if after is not None:
            name_key, agent_id = after
            conditions.append(
                or_(
                    _AGENTS.c.name_key > name_key,
                    and_(_AGENTS.c.name_key == name_key, _AGENTS.c.agent_id > agent_id),
                )
            )
        wanted = str(needle or "").strip().lower()
        if wanted:
            conditions.append(
                or_(
                    *(
                        func.lower(cast(func.coalesce(column, ""), Text)).contains(wanted, autoescape=True)
                        for column in (_AGENTS.c.name, _AGENTS.c.display_name, _AGENTS.c.model)
                    )
                )
            )
        if enabled is not None:
            conditions.append(_AGENTS.c.enabled.is_(enabled))
        rows = await fetch_all(
            _definition()
            .where(*conditions)
            .order_by(_AGENTS.c.name_key, _AGENTS.c.agent_id)
            .limit(max(1, min(int(limit or 200), 10_000)))
        )
        return [_view(row) for row in rows]

    async def visible_tag_counts(
        self, viewer: str, *, platform_admin: bool
    ) -> list[dict[str, Any]]:
        """The tags of the live Agents ``viewer`` may see, most used first."""
        items = string_array_items(_AGENTS.c.tags).table_valued(sql_column("value", Text))
        tags = (
            select(_AGENTS.c.agent_id, items.c.value.label("tag"))
            .select_from(_AGENTS.join(items, true()))
            .where(_LIVE, visible_to(viewer, platform_admin=platform_admin))
            .subquery()
        )
        count = func.count(func.distinct(tags.c.agent_id))
        rows = await fetch_all(
            select(tags.c.tag, count.label("agents"))
            .group_by(tags.c.tag)
            .order_by(count.desc(), tags.c.tag)
        )
        return [{"tag": row["tag"], "agents": int(row["agents"])} for row in rows]

    async def list_live_agents_page(
        self, *, after_agent_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """One page of the live Agents, in ``agent_id`` order after ``after_agent_id``."""
        conditions: list[ColumnElement[bool]] = [_LIVE]
        if after_agent_id:
            conditions.append(_AGENTS.c.agent_id > after_agent_id)
        rows = await fetch_all(
            _definition()
            .where(*conditions)
            .order_by(_AGENTS.c.agent_id)
            .limit(max(1, min(int(limit or 200), 10_000)))
        )
        return [_view(row) for row in rows]

    async def list_agents_by_ids(self, agent_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Map each live ``agent_id`` to its Agent; unknown and deleted ids are absent."""
        ids = _unique(agent_ids)
        if not ids:
            return {}
        rows = await fetch_all(_definition().where(_AGENTS.c.agent_id.in_(ids), _LIVE))
        return {str(row["agent_id"]): _view(row) for row in rows}

    async def list_agents_by_credential_vault_id(self, vault_id: str) -> list[dict[str, Any]]:
        """Every live Agent a vault is bound to."""
        target = str(vault_id or "").strip()
        if not target:
            return []
        bound = select(_VAULTS.c.agent_id).where(_VAULTS.c.vault_id == target)
        rows = await fetch_all(
            _definition()
            .where(_AGENTS.c.agent_id.in_(bound), _LIVE)
            .order_by(_AGENTS.c.agent_id)
        )
        return [_view(row) for row in rows]

    async def list_catalog_selections(self) -> list[dict[str, Any]]:
        """Every live Agent holding a catalog selection, enabled or not."""
        assigned = select(_ASSIGNMENTS.c.agent_id)
        rows = await fetch_all(
            _definition()
            .where(
                _LIVE,
                or_(
                    _AGENTS.c.agent_id.in_(assigned),
                    _AGENTS.c.catalog_skill_ids != [],
                    _AGENTS.c.catalog_skills != [],
                ),
            )
            .order_by(_AGENTS.c.agent_id)
        )
        return [_view(row) for row in rows]

    async def list_agents_by_environment(
        self, name: str, *, conn: AsyncConnection
    ) -> list[dict[str, Any]]:
        """Every Agent naming an Environment, deleted ones included, on ``conn``.

        Each carries ``holds_compute``: whether the Agent's runtime record
        still names a box, a client pool, a prepared slot or a sandbox its
        deletion could not confirm destroyed.
        """
        owner = _AGENTS.c.agent_id
        holds_compute = or_(
            _RUNTIMES.c.sandbox_id.is_not(None),
            _RUNTIMES.c.client_pool_name.is_not(None),
            select(_SLOTS.c.slot_id).where(_SLOTS.c.agent_id == owner).exists(),
            select(_RETIRING.c.pool_name).where(_RETIRING.c.agent_id == owner).exists(),
            select(_UNDESTROYED.c.sandbox_id).where(_UNDESTROYED.c.agent_id == owner).exists(),
        ).label("holds_compute")
        rows = (
            await conn.execute(
                _definition()
                .add_columns(holds_compute)
                .where(_AGENTS.c.environment_name == name)
                .order_by(_AGENTS.c.agent_id)
            )
        ).all()
        return [
            {**_view(row._mapping), "holds_compute": bool(row._mapping["holds_compute"])}
            for row in rows
        ]

    async def list_agent_ids_by_environment(
        self, name: str, *, after_agent_id: str | None = None, limit: int = 200
    ) -> list[str]:
        """One page of the live Agents naming an Environment, in ``agent_id`` order."""
        conditions: list[ColumnElement[bool]] = [_LIVE, _AGENTS.c.environment_name == name]
        if after_agent_id:
            conditions.append(_AGENTS.c.agent_id > after_agent_id)
        rows = await fetch_all(
            select(_AGENTS.c.agent_id)
            .where(*conditions)
            .order_by(_AGENTS.c.agent_id)
            .limit(max(1, min(int(limit or 200), 10_000)))
        )
        return [str(row["agent_id"]) for row in rows]

    async def total_prepared_capacity(self, *, excluding_agent_id: str = "") -> int:
        """Sum of ``prepared_capacity`` over Agents not being deleted, optionally leaving one out."""
        return int(
            await fetch_value(
                select(func.coalesce(func.sum(_AGENTS.c.prepared_capacity), 0)).where(
                    # The prepared-capacity sweep's predicate, so its index serves the sum.
                    _AGENTS.c.prepared_capacity > 0,
                    _AGENTS.c.state == "ACTIVE",
                    _AGENTS.c.agent_id != excluding_agent_id,
                )
            )
            or 0
        )

    # ── the Agent's runtime record ─────────────────────────────────────────

    async def allocate_conversation_uid(self, agent_id: str, *, first: int, last: int) -> int | None:
        """Hand the next POSIX owner to one of a live Agent's conversations.

        An atomic update and locked readback give concurrent conversations
        their own number, without handing any number out twice. The first number is
        ``first`` (also past a counter left below it); ``None`` when the Agent
        is not live or the next number would pass ``last``.
        """
        following = func.greatest(
            func.coalesce(_RUNTIMES.c.last_conversation_uid, first - 1) + 1, first
        )
        async def allocate(conn: AsyncConnection) -> int | None:
            changed = await conn.execute(
                update(_RUNTIMES)
                .where(
                    _RUNTIMES.c.agent_id == agent_id,
                    _RUNTIMES.c.agent_id.in_(_live(agent_id)),
                    following <= last,
                )
                .values(last_conversation_uid=following)
            )
            if changed.rowcount != 1:
                return None
            return int((await conn.execute(
                select(_RUNTIMES.c.last_conversation_uid).where(_RUNTIMES.c.agent_id == agent_id)
            )).scalar_one())

        return await transaction(allocate)

    async def claim_workspace_id(self, agent_id: str, minted: str) -> str | None:
        """The live Agent's durable workspace id, setting ``minted`` when it has none.

        The write lock covers readback, so concurrent box creators all mount
        the root the first of them recorded. ``None`` when the Agent is not live.
        """
        async def claim(conn: AsyncConnection) -> str | None:
            changed = await conn.execute(
                update(_RUNTIMES)
                .where(_RUNTIMES.c.agent_id == agent_id, _RUNTIMES.c.agent_id.in_(_live(agent_id)))
                .values(workspace_id=func.coalesce(_RUNTIMES.c.workspace_id, minted))
            )
            if changed.rowcount != 1:
                return None
            return str((await conn.execute(
                select(_RUNTIMES.c.workspace_id).where(_RUNTIMES.c.agent_id == agent_id)
            )).scalar_one())

        return await transaction(claim)

    async def record_capacity_miss(self, agent_id: str) -> None:
        """Record that a new conversation of the Agent found no prepared unit."""
        await execute(
            update(_RUNTIMES)
            .where(_RUNTIMES.c.agent_id == agent_id)
            .values(capacity_last_miss_at=func.now())
        )

    async def record_seats_per_box(self, agent_id: str, seats: int) -> bool:
        """Record how many seats the Agent's latest pool box held; whether it changed."""
        return (
            await execute(
                update(_RUNTIMES)
                .where(_RUNTIMES.c.agent_id == agent_id, _RUNTIMES.c.seats_per_box != seats)
                .values(seats_per_box=seats)
            )
        ) == 1

    async def record_undestroyed_sandbox(self, agent_id: str, sandbox_id: str) -> None:
        """Keep the name of a sandbox the Agent's teardown could not confirm destroyed."""
        async def record(conn: AsyncConnection) -> None:
            await conn.execute(insert_if_absent_primary_key(conn, _UNDESTROYED, {
                "agent_id": agent_id, "sandbox_id": sandbox_id,
            }))

        await transaction(record)

    # ── gateway keys of claimed prepared slots ─────────────────────────────

    async def record_gateway_entry(
        self,
        agent_id: str,
        *,
        slot_id: str,
        session_id: str,
        endpoint_provider: str | None,
    ) -> str:
        """Record that ``session_id`` owns the gateway key of a claimed slot.

        Returns the Session that owns the entry: ``session_id`` once it is
        recorded (again, when it already was), another Session when that one
        recorded the slot first.
        """
        async def record(conn: AsyncConnection) -> str:
            await conn.execute(upsert_primary_key(conn, _GATEWAY_ENTRIES, {
                "agent_id": agent_id, "slot_id": slot_id, "session_id": session_id,
                "endpoint_provider": endpoint_provider,
            }, update_columns=[], update_values={
                "endpoint_provider": case(
                    (_GATEWAY_ENTRIES.c.session_id == session_id, endpoint_provider),
                    else_=_GATEWAY_ENTRIES.c.endpoint_provider,
                ),
            }))
            owner = (await conn.execute(select(_GATEWAY_ENTRIES.c.session_id).where(
                _GATEWAY_ENTRIES.c.agent_id == agent_id,
                _GATEWAY_ENTRIES.c.slot_id == slot_id,
            ))).scalar_one()
            return str(owner)

        return await transaction(record)

    async def list_gateway_entries(self, agent_id: str) -> list[dict[str, Any]]:
        """The Agent's gateway-key entries: ``slot_id``, ``session_id``, ``endpoint_provider``."""
        rows = await fetch_all(
            select(
                _GATEWAY_ENTRIES.c.slot_id,
                _GATEWAY_ENTRIES.c.session_id,
                _GATEWAY_ENTRIES.c.endpoint_provider,
            )
            .where(_GATEWAY_ENTRIES.c.agent_id == agent_id)
            .order_by(_GATEWAY_ENTRIES.c.recorded_at, _GATEWAY_ENTRIES.c.slot_id)
        )
        return [dict(row) for row in rows]

    async def remove_gateway_entry(self, agent_id: str, *, slot_id: str, session_id: str) -> bool:
        """Remove one entry while ``session_id`` still owns it; whether this call removed it."""
        return (
            await execute(
                delete(_GATEWAY_ENTRIES).where(
                    _GATEWAY_ENTRIES.c.agent_id == agent_id,
                    _GATEWAY_ENTRIES.c.slot_id == slot_id,
                    _GATEWAY_ENTRIES.c.session_id == session_id,
                )
            )
        ) == 1

    # ── the shared-box ledger ──────────────────────────────────────────────

    async def read_ledger(self, agent_id: str) -> dict[str, Any] | None:
        """A live Agent with its shared-box ledger, read in one statement.

        :meth:`get_agent` plus ``_prepared_slots`` (slot manifests, oldest
        first), ``box_admissions`` (``sandbox_id``, ``session_id`` and ``at`` in
        epoch seconds), ``_retiring_client_pools`` and the revisions a fenced
        write names: ``_slots_revision`` and ``_admissions_revision``. ``None``
        when the Agent is missing or deleted.
        """
        row = await fetch_one(_ledger().where(_AGENTS.c.agent_id == agent_id, _LIVE))
        return _ledger_view(row) if row is not None else None

    async def list_slots(self, agent_id: str) -> list[dict[str, Any]]:
        """The Agent's slot manifests, oldest first, deleted Agent or not."""
        rows = await fetch_all(
            select(_slot_json().label("manifest"))
            .where(_SLOTS.c.agent_id == agent_id)
            .order_by(_SLOTS.c.prepared_at.is_(None), _SLOTS.c.prepared_at, _SLOTS.c.slot_id)
        )
        return [_slot_manifest(row["manifest"]) for row in rows]

    async def find_agent_by_sandbox_id(self, sandbox_id: str) -> dict[str, Any] | None:
        """The live Agent whose resident box is ``sandbox_id``, if any."""
        target = str(sandbox_id or "").strip()
        if not target:
            return None
        row = await fetch_one(
            _definition()
            .where(_RUNTIMES.c.sandbox_id == target, _LIVE)
            .order_by(_AGENTS.c.agent_id)
            .limit(1)
        )
        return _view(row) if row is not None else None

    async def list_agents_by_sandbox_id(self, sandbox_id: str) -> list[dict[str, Any]]:
        """Every live Agent whose resident box is ``sandbox_id``."""
        target = str(sandbox_id or "").strip()
        if not target:
            return []
        rows = await fetch_all(
            _definition()
            .where(_RUNTIMES.c.sandbox_id == target, _LIVE)
            .order_by(_AGENTS.c.agent_id)
        )
        return [_view(row) for row in rows]

    async def list_agents_with_resident_boxes(
        self, *, after_agent_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """One page of the Agents, deleted ones included, naming a resident box.

        Rows come in ``agent_id`` order after ``after_agent_id``; a page shorter
        than ``limit`` is the last.
        """
        conditions: list[ColumnElement[bool]] = [_RUNTIMES.c.sandbox_id.is_not(None)]
        if after_agent_id:
            conditions.append(_RUNTIMES.c.agent_id > after_agent_id)
        rows = await fetch_all(
            _definition()
            .where(*conditions)
            .order_by(_RUNTIMES.c.agent_id)
            .limit(max(1, min(int(limit or 200), 10_000)))
        )
        return [_view(row) for row in rows]

    async def list_agents_with_prepared_capacity(
        self, *, after_agent_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """One page of the active Agents that asked for prepared capacity, with their ledgers.

        The warm-capacity sweep reads this rather than the resident-box list:
        an Agent whose box expired or whose slot build failed has no box to be
        listed by, and it is exactly the Agent the sweep exists to rebuild.
        """
        conditions: list[ColumnElement[bool]] = [
            _AGENTS.c.prepared_capacity > 0,
            _AGENTS.c.state == "ACTIVE",
        ]
        if after_agent_id:
            conditions.append(_AGENTS.c.agent_id > after_agent_id)
        rows = await fetch_all(
            _ledger()
            .where(*conditions)
            .order_by(_AGENTS.c.agent_id)
            .limit(max(1, min(int(limit or 200), 10_000)))
        )
        return [_ledger_view(row) for row in rows]

    async def list_dead_binding_probe_candidates(
        self,
        *,
        now_iso: str,
        after_agent_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """One page of the live Agents whose resident box lease is unknown or past."""
        conditions: list[ColumnElement[bool]] = [
            _LIVE,
            _RUNTIMES.c.sandbox_id.is_not(None),
            or_(_RUNTIMES.c.expires_at.is_(None), _RUNTIMES.c.expires_at <= from_iso(now_iso)),
        ]
        if after_agent_id:
            conditions.append(_RUNTIMES.c.agent_id > after_agent_id)
        rows = await fetch_all(
            _definition()
            .where(*conditions)
            .order_by(_RUNTIMES.c.agent_id)
            .limit(max(1, min(int(limit or 50), 500)))
        )
        return [_view(row) for row in rows]

    async def realign_resident_lease(
        self,
        agent_id: str,
        *,
        sandbox_id: str,
        observed_expires_at: str | None,
        expires_at: str | None,
    ) -> bool:
        """Record the resident box's lease end while the box and the end read are unchanged."""
        return (
            await execute(
                update(_RUNTIMES)
                .where(
                    _RUNTIMES.c.agent_id == agent_id,
                    _RUNTIMES.c.agent_id.in_(_live(agent_id)),
                    _RUNTIMES.c.sandbox_id == sandbox_id,
                    _RUNTIMES.c.expires_at.is_not_distinct_from(from_iso(observed_expires_at)),
                )
                .values(expires_at=from_iso(expires_at))
            )
        ) == 1

    async def claim_resident_box(
        self,
        agent_id: str,
        *,
        observed: Mapping[str, Any],
        admissions_revision: int,
        sandbox_id: str,
        sandbox_backend: str,
        resident_generation: str | None,
        session_id: str,
        young_since: datetime,
    ) -> bool:
        """Make ``sandbox_id`` the live Agent's resident box and admit the claiming conversation.

        Applies only while the runtime record's ``sandbox_id``,
        ``sandbox_backend``, ``runtime_generation`` and ``sandbox_generation``
        are as given in ``observed`` (a key left out is not compared) and no
        conversation was admitted since ``admissions_revision``: the claim's
        capacity verdict counted exactly those admissions. The claiming
        conversation is charged in the same transaction, so the box is never
        published with nothing recorded against it.
        """
        conditions: list[ColumnElement[bool]] = [
            _RUNTIMES.c.agent_id == agent_id,
            _RUNTIMES.c.agent_id.in_(_live(agent_id)),
            _RUNTIMES.c.admissions_revision == admissions_revision,
        ]
        for key in ("sandbox_id", "sandbox_backend", "runtime_generation", "sandbox_generation"):
            if key in observed:
                conditions.append(_RUNTIMES.c[key].is_not_distinct_from(observed[key] or None))
        values: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "sandbox_backend": sandbox_backend,
            "expires_at": None,
            "admissions_revision": _RUNTIMES.c.admissions_revision + 1,
        }
        if resident_generation:
            values["resident_generation"] = resident_generation

        async def claim(conn: AsyncConnection) -> bool:
            claimed = await conn.execute(
                update(_RUNTIMES).where(*conditions).values(**values)
            )
            if claimed.rowcount != 1:
                return False
            await _admit(
                conn, agent_id, sandbox_id=sandbox_id, session_id=session_id, young_since=young_since
            )
            return True

        return await transaction(claim)

    async def admit_to_box(
        self,
        agent_id: str,
        *,
        sandbox_id: str,
        session_id: str,
        admissions_revision: int,
        young_since: datetime,
    ) -> bool:
        """Charge a conversation to a box of the live Agent, unless another was admitted since.

        ``False`` when an admission landed after ``admissions_revision`` was
        read: the caller asks the box about its capacity again with that
        admission counted, so "room for one more" is answered once per asker,
        not once per reading.
        """

        async def admit(conn: AsyncConnection) -> bool:
            fenced = await conn.execute(
                update(_RUNTIMES)
                .where(
                    _RUNTIMES.c.agent_id == agent_id,
                    _RUNTIMES.c.agent_id.in_(_live(agent_id)),
                    _RUNTIMES.c.admissions_revision == admissions_revision,
                )
                .values(admissions_revision=_RUNTIMES.c.admissions_revision + 1)
            )
            if fenced.rowcount != 1:
                return False
            await _admit(
                conn, agent_id, sandbox_id=sandbox_id, session_id=session_id, young_since=young_since
            )
            return True

        return await transaction(admit)

    async def release_box_admission(self, agent_id: str, *, sandbox_id: str, session_id: str) -> None:
        """Withdraw one conversation's admission to a box, deleted Agent or not."""
        await execute(
            delete(_ADMISSIONS).where(
                _ADMISSIONS.c.agent_id == agent_id,
                _ADMISSIONS.c.sandbox_id == sandbox_id,
                _ADMISSIONS.c.session_id == session_id,
            )
        )

    async def clear_stale_resident(
        self,
        agent_id: str,
        *,
        sandbox_id: str,
        sandbox_backend: str,
        resident_generation: str | None,
    ) -> bool:
        """Stop naming a resident box built for an older generation, while it is as read."""
        return (
            await execute(
                update(_RUNTIMES)
                .where(
                    _RUNTIMES.c.agent_id == agent_id,
                    _RUNTIMES.c.agent_id.in_(_live(agent_id)),
                    _RUNTIMES.c.sandbox_id == sandbox_id,
                    _RUNTIMES.c.sandbox_backend == sandbox_backend,
                    _RUNTIMES.c.resident_generation.is_not_distinct_from(
                        resident_generation or None
                    ),
                )
                .values(
                    sandbox_id=None, sandbox_backend=None, resident_generation=None, expires_at=None
                )
            )
        ) == 1

    async def clear_resident_sandbox_binding(
        self, agent_id: str, *, sandbox_id: str, sandbox_backend: str | None = None,
    ) -> bool:
        """Clear a confirmed-gone resident box, deleted Agent or not; whether it was cleared.

        The expected binding fences a replacement made during the provider
        probe. The same transaction withdraws every slot still waiting in the
        box: each keeps its cleanup address for ordinary retirement, and a slot
        a conversation already claimed keeps its hand-off.
        """
        if not agent_id or not sandbox_id:
            raise ValueError("agent_id and sandbox_id are required for binding cleanup")
        conditions: list[ColumnElement[bool]] = [
            _RUNTIMES.c.agent_id == agent_id,
            _RUNTIMES.c.sandbox_id == sandbox_id,
        ]
        if sandbox_backend:
            conditions.append(_RUNTIMES.c.sandbox_backend == sandbox_backend)

        async def clear(conn: AsyncConnection) -> bool:
            held = (
                await conn.execute(
                    select(_RUNTIMES.c.sandbox_backend).where(*conditions).with_for_update()
                )
            ).first()
            if held is None:
                return False
            await conn.execute(
                update(_RUNTIMES)
                .where(_RUNTIMES.c.agent_id == agent_id)
                .values(
                    sandbox_id=None,
                    sandbox_backend=None,
                    resident_generation=None,
                    expires_at=None,
                    slots_revision=_RUNTIMES.c.slots_revision + 1,
                )
            )
            await conn.execute(
                update(_SLOTS)
                .where(
                    _SLOTS.c.agent_id == agent_id,
                    _SLOTS.c.sandbox_id == sandbox_id,
                    _SLOTS.c.sandbox_backend == held._mapping["sandbox_backend"],
                    _SLOTS.c.placement == "shared_slot",
                    _SLOTS.c.state == "prepared",
                )
                .values(
                    state="retiring",
                    retire_reason="resident sandbox is confirmed gone",
                    retiring_at=datetime.now(timezone.utc),
                )
            )
            return True

        return await transaction(clear)

    async def box_has_recorded_occupants(
        self, sandbox_id: str, *, excluding: str, young_since: datetime,
    ) -> bool:
        """Whether a live Agent's ledger places anything other than ``excluding`` in a box.

        A slot in the box occupies it whatever its state, and so does an
        admission recorded at or after ``young_since``; ``excluding`` names the
        Session or slot whose own record does not count.
        """
        owners = select(_AGENTS.c.agent_id).where(_LIVE)
        slots = select(_SLOTS.c.slot_id).where(
            _SLOTS.c.sandbox_id == sandbox_id,
            _SLOTS.c.slot_id != excluding,
            _SLOTS.c.agent_id.in_(owners),
        )
        admissions = select(_ADMISSIONS.c.session_id).where(
            _ADMISSIONS.c.sandbox_id == sandbox_id,
            _ADMISSIONS.c.session_id != excluding,
            _ADMISSIONS.c.admitted_at >= young_since,
            _ADMISSIONS.c.agent_id.in_(owners),
        )
        return bool(await fetch_value(select(or_(slots.exists(), admissions.exists()))))

    # ── prepared slots ─────────────────────────────────────────────────────

    async def adopt_pool_box(
        self,
        agent_id: str,
        *,
        seats: list[dict[str, Any]],
        resident: tuple[str, str, str | None],
    ) -> bool:
        """Enter the seats of a box taken from the client pool in the live Agent's ledger.

        ``resident`` is the box as ``(sandbox_id, backend, generation)``; it
        becomes the Agent's resident box when the Agent names none. ``False``
        when the Agent is not live.
        """
        named = _RUNTIMES.c.sandbox_id.is_not(None)
        sandbox_id, backend, generation = resident

        async def adopt(conn: AsyncConnection) -> bool:
            adopted = await conn.execute(
                update(_RUNTIMES)
                .where(_RUNTIMES.c.agent_id == agent_id, _RUNTIMES.c.agent_id.in_(_live(agent_id)))
                # MySQL evaluates assignments in order. Set sandbox_id last,
                # so every binding field tests whether the old box was named.
                .ordered_values(
                    *((column, case((named, _RUNTIMES.c[column]), else_=value))
                      for column, value in (
                          ("sandbox_backend", backend), ("resident_generation", generation),
                          ("expires_at", None), ("sandbox_id", sandbox_id),
                      )),
                    ("slots_revision", _RUNTIMES.c.slots_revision + 1),
                )
            )
            if adopted.rowcount != 1:
                return False
            await conn.execute(insert(_SLOTS), [_slot_values(agent_id, seat) for seat in seats])
            return True

        return await transaction(adopt)

    async def apply_slot_claims(
        self,
        agent_id: str,
        *,
        slots_revision: int,
        admissions_revision: int,
        claims: list[tuple[str, str]],
        withdrawals: list[tuple[str, str]],
        young_since: datetime,
    ) -> dict[str, dict[str, Any]] | None:
        """Hand prepared slots to Sessions and withdraw those without room, in one transaction.

        ``claims`` pairs a slot with the Session it goes to, ``withdrawals`` a
        slot with why it is withdrawn. A claimed slot's admission moves to its
        Session in the same transaction, so the preparation's admission never
        protects a placement a Session now owns. Returns each Session's claimed
        manifest, or ``None`` when the Agent is not live or its slots or
        admissions changed since the revisions were read: every decision here
        rests on them.
        """

        async def apply(conn: AsyncConnection) -> dict[str, dict[str, Any]] | None:
            fenced = await conn.execute(
                update(_RUNTIMES)
                .where(
                    _RUNTIMES.c.agent_id == agent_id,
                    _RUNTIMES.c.agent_id.in_(_live(agent_id)),
                    _RUNTIMES.c.slots_revision == slots_revision,
                    _RUNTIMES.c.admissions_revision == admissions_revision,
                )
                .values(
                    slots_revision=_RUNTIMES.c.slots_revision + 1,
                    admissions_revision=_RUNTIMES.c.admissions_revision + 1,
                )
            )
            if fenced.rowcount != 1:
                return None
            now = datetime.now(timezone.utc)
            for slot_id, reason in withdrawals:
                await conn.execute(
                    update(_SLOTS)
                    .where(
                        _SLOTS.c.agent_id == agent_id,
                        _SLOTS.c.slot_id == slot_id,
                        _SLOTS.c.state == "prepared",
                    )
                    .values(state="retiring", retire_reason=reason, retiring_at=now)
                )
            assigned: dict[str, dict[str, Any]] = {}
            for slot_id, session_id in claims:
                claimed = await conn.execute(
                    update(_SLOTS)
                    .where(
                        _SLOTS.c.agent_id == agent_id,
                        _SLOTS.c.slot_id == slot_id,
                        _SLOTS.c.state == "prepared",
                    )
                    .values(state="claimed", claimed_session_id=session_id, claimed_at=now)
                )
                if claimed.rowcount != 1:
                    raise RuntimeError(
                        f"prepared slot {slot_id!r} of Agent {agent_id!r} changed "
                        "under its revision fence"
                    )
                stored = (await conn.execute(select(_slot_json()).where(
                    _SLOTS.c.agent_id == agent_id, _SLOTS.c.slot_id == slot_id,
                ))).scalar_one()
                manifest = _slot_manifest(stored)
                await conn.execute(
                    delete(_ADMISSIONS).where(
                        _ADMISSIONS.c.agent_id == agent_id,
                        _ADMISSIONS.c.sandbox_id == manifest["sandbox_id"],
                        _ADMISSIONS.c.session_id == slot_id,
                    )
                )
                await _admit(
                    conn,
                    agent_id,
                    sandbox_id=str(manifest["sandbox_id"]),
                    session_id=session_id,
                    young_since=young_since,
                )
                assigned[session_id] = manifest
            return assigned

        return await transaction(apply)

    async def retire_slot(
        self,
        agent_id: str,
        slot_id: str,
        *,
        state: str,
        claimed_session_id: str | None,
        reason: str,
    ) -> bool:
        """Withdraw a slot while it is in ``state`` for ``claimed_session_id``; whether it was."""
        return await self._change_slot(
            agent_id,
            slot_id,
            state=state,
            claimed_session_id=claimed_session_id,
            change=update(_SLOTS).values(
                state="retiring", retire_reason=reason, retiring_at=datetime.now(timezone.utc)
            ),
        )

    async def remove_slot(
        self, agent_id: str, slot_id: str, *, state: str, claimed_session_id: str | None,
    ) -> bool:
        """Remove a slot while it is in ``state`` for ``claimed_session_id``; whether it was."""
        return await self._change_slot(
            agent_id,
            slot_id,
            state=state,
            claimed_session_id=claimed_session_id,
            change=delete(_SLOTS),
        )

    async def _change_slot(
        self,
        agent_id: str,
        slot_id: str,
        *,
        state: str,
        claimed_session_id: str | None,
        change: Any,
    ) -> bool:
        """Apply ``change`` to one slot as read, moving the slots revision with it.

        A slot never returns to a state it left, and a claim never changes
        hands, so its state and claiming Session identify the version read.
        """

        async def apply(conn: AsyncConnection) -> bool:
            locked = await conn.execute(
                update(_RUNTIMES)
                .where(_RUNTIMES.c.agent_id == agent_id)
                .values(slots_revision=_RUNTIMES.c.slots_revision + 1)
            )
            if locked.rowcount != 1:
                return False
            changed = await conn.execute(
                change.where(
                    _SLOTS.c.agent_id == agent_id,
                    _SLOTS.c.slot_id == slot_id,
                    _SLOTS.c.state == state,
                    _SLOTS.c.claimed_session_id.is_not_distinct_from(claimed_session_id or None),
                )
            )
            if changed.rowcount != 1:
                raise _NothingChanged()
            return True

        try:
            return await transaction(apply)
        except _NothingChanged:
            return False

    # ── runtime generation, client pool and prepared capacity ─────────────

    async def publish_runtime_generation(
        self,
        agent_id: str,
        *,
        observed: Mapping[str, Any],
        runtime_generation: str,
        sandbox_generation: str,
        client_pool_epoch: str,
    ) -> bool:
        """Publish a runtime generation while the ledger is as ``observed``.

        ``observed`` is a :meth:`read_ledger` result. Its generations, client
        pool epoch, resident box and slots revision must all be unchanged, so a
        slot added or a box claimed after the read sends the caller back to
        retire what it missed and read again.
        """
        fence = {
            "_runtime_generation": _RUNTIMES.c.runtime_generation,
            "_sandbox_generation": _RUNTIMES.c.sandbox_generation,
            "_client_pool_epoch": _RUNTIMES.c.client_pool_epoch,
            "sandbox_id": _RUNTIMES.c.sandbox_id,
            "sandbox_backend": _RUNTIMES.c.sandbox_backend,
            "_resident_sandbox_generation": _RUNTIMES.c.resident_generation,
        }
        return (
            await execute(
                update(_RUNTIMES)
                .where(
                    _RUNTIMES.c.agent_id == agent_id,
                    _RUNTIMES.c.agent_id.in_(_live(agent_id)),
                    _RUNTIMES.c.slots_revision == int(observed["_slots_revision"]),
                    *(
                        column.is_not_distinct_from(observed.get(key) or None)
                        for key, column in fence.items()
                    ),
                )
                .values(
                    runtime_generation=runtime_generation,
                    sandbox_generation=sandbox_generation,
                    client_pool_epoch=client_pool_epoch,
                )
            )
        ) == 1

    async def record_client_pool(
        self, agent_id: str, *, client_pool_epoch: str, name: str, backend: str,
    ) -> Literal["recorded", "superseded", "missing"]:
        """Record a client pool as the live Agent's before anything starts it.

        The pool it displaces is kept for retirement in the same transaction.
        ``"superseded"`` when the Agent's pool epoch is not
        ``client_pool_epoch``; ``"missing"`` when the Agent is not live.
        """

        async def record(conn: AsyncConnection) -> Literal["recorded", "superseded", "missing"]:
            row = (
                await conn.execute(
                    select(
                        _RUNTIMES.c.client_pool_epoch,
                        _RUNTIMES.c.client_pool_name,
                        _RUNTIMES.c.client_pool_backend,
                    )
                    .where(
                        _RUNTIMES.c.agent_id == agent_id,
                        _RUNTIMES.c.agent_id.in_(_live(agent_id)),
                    )
                    .with_for_update()
                )
            ).first()
            if row is None:
                return "missing"
            current = row._mapping
            if current["client_pool_epoch"] != client_pool_epoch:
                return "superseded"
            if (current["client_pool_name"], current["client_pool_backend"]) == (name, backend):
                return "recorded"
            if current["client_pool_name"] is not None:
                await conn.execute(
                    insert_if_absent_primary_key(conn, _RETIRING, {
                        "agent_id": agent_id, "pool_name": current["client_pool_name"],
                        "backend": current["client_pool_backend"],
                    })
                )
            await conn.execute(
                update(_RUNTIMES)
                .where(_RUNTIMES.c.agent_id == agent_id)
                .values(client_pool_name=name, client_pool_backend=backend)
            )
            return "recorded"

        return await transaction(record)

    async def clear_client_pool(self, agent_id: str, *, name: str, backend: str) -> bool:
        """Stop naming a retired client pool and its epoch while it is the one recorded."""
        return (
            await execute(
                update(_RUNTIMES)
                .where(
                    _RUNTIMES.c.agent_id == agent_id,
                    _RUNTIMES.c.client_pool_name == name,
                    _RUNTIMES.c.client_pool_backend == backend,
                )
                .values(client_pool_name=None, client_pool_backend=None, client_pool_epoch=None)
            )
        ) == 1

    async def list_retiring_client_pools(self, agent_id: str) -> list[dict[str, str]]:
        """The client pools newer ones displaced, oldest first: ``name`` and ``backend``."""
        rows = await fetch_all(
            select(_RETIRING.c.pool_name, _RETIRING.c.backend)
            .where(_RETIRING.c.agent_id == agent_id)
            .order_by(_RETIRING.c.recorded_at, _RETIRING.c.pool_name)
        )
        return [{"name": str(row["pool_name"]), "backend": str(row["backend"])} for row in rows]

    async def remove_retiring_client_pool(self, agent_id: str, name: str) -> bool:
        """Forget a displaced client pool once it is retired."""
        return (
            await execute(
                delete(_RETIRING).where(
                    _RETIRING.c.agent_id == agent_id, _RETIRING.c.pool_name == name
                )
            )
        ) == 1

    async def request_prewarm_refresh(
        self,
        agent_id: str,
        *,
        version: int,
        prepared_capacity: int,
        enabled: bool,
        prewarm_revision: str | None,
        new_revision: str,
    ) -> bool:
        """Record an explicit rebuild request while the Agent is as the requester read it."""

        async def request(conn: AsyncConnection) -> bool:
            if not await _definition_holds(
                conn,
                agent_id,
                _AGENTS.c.version == version,
                _AGENTS.c.prepared_capacity == prepared_capacity,
                _AGENTS.c.enabled.is_(enabled),
            ):
                return False
            changed = await conn.execute(
                update(_RUNTIMES)
                .where(
                    _RUNTIMES.c.agent_id == agent_id,
                    _RUNTIMES.c.prewarm_revision.is_not_distinct_from(prewarm_revision or None),
                )
                .values(prewarm_revision=new_revision, prepared_runtime_error=None)
            )
            return changed.rowcount == 1

        return await transaction(request)

    async def record_prepared_runtime(
        self,
        agent_id: str,
        *,
        version: int,
        prewarm_revision: str | None,
        generation: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Record what a reconciliation prepared, or why it failed.

        Applies only while the definition is at ``version`` and the rebuild
        request is ``prewarm_revision``: a reconciliation that started before
        an edit does not report for the edited Agent. ``error`` records a
        failure and keeps the generation prepared before; without it
        ``generation`` is recorded and the error cleared.
        """
        values: dict[str, Any] = (
            {"prepared_runtime_error": error}
            if error is not None
            else {"prepared_runtime_error": None, "prepared_runtime_generation": generation}
        )

        async def record(conn: AsyncConnection) -> bool:
            if not await _definition_holds(conn, agent_id, _AGENTS.c.version == version):
                return False
            changed = await conn.execute(
                update(_RUNTIMES)
                .where(
                    _RUNTIMES.c.agent_id == agent_id,
                    _RUNTIMES.c.prewarm_revision.is_not_distinct_from(prewarm_revision or None),
                )
                .values(**values)
            )
            return changed.rowcount == 1

        return await transaction(record)

    async def swap_prepared_operation(
        self,
        agent_id: str,
        *,
        expected: Mapping[str, Any] | None,
        replacement: Mapping[str, Any] | None,
        generation: Mapping[str, Any] | None = None,
    ) -> bool:
        """Replace the Agent's resource synchronization ``expected`` with ``replacement``.

        An operation is ``provider``, ``operation_id`` and ``deadline``;
        ``None`` is none. ``generation`` additionally requires the live Agent
        to still be at its ``version``, ``_prewarm_revision``,
        ``_client_pool_epoch``, ``_runtime_generation`` and
        ``_sandbox_generation``, as a new operation must be.
        """

        async def swap(conn: AsyncConnection) -> bool:
            conditions: list[ColumnElement[bool]] = [
                _RUNTIMES.c.agent_id == agent_id,
                _operation_is(expected),
            ]
            if generation is not None:
                if not await _definition_holds(
                    conn, agent_id, _AGENTS.c.version == generation["version"]
                ):
                    return False
                conditions.extend(
                    column.is_not_distinct_from(generation.get(key) or None)
                    for key, column in _GENERATION_FENCE.items()
                )
            changed = await conn.execute(
                update(_RUNTIMES).where(*conditions).values(**_operation_values(replacement))
            )
            return changed.rowcount == 1

        return await transaction(swap)
