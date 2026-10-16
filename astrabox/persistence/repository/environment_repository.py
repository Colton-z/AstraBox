"""Environments in their typed table, with their credentials in the secret store.

An Environment row holds every field of the administrator's form except the two
credentials, ``provider_access.api_key`` and ``tracing.auth_token``. Each of
those is sealed in the deployment's secret store under the Environment's scope
(:func:`secret_scope`), and the row holds only the key it was stored under. A
write commits its references and sealed values together. Runtime reads select
the references and ciphertext in one snapshot before decrypting the values.

Reads hand services the familiar document shape. A credential that is set reads
as :data:`SECRET_MASK`; only :meth:`EnvironmentRepository.get_with_secrets`
returns the values, for the runtime that hands them to an engine.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.common.utils.errors import APIError
from astrabox.persistence.sql import ForeignKeyViolation, fetch_all, fetch_one, fetch_value, to_iso
from astrabox.persistence.sql import transaction
from astrabox.persistence.sql.models.agents import Agent
from astrabox.persistence.sql.models.assistants import Assistant
from astrabox.persistence.sql.models.environments import Environment
from astrabox.persistence.sql.models.vaults import Secret
from astrabox.persistence.sql.statements import upsert_primary_key
from astrabox.providers.secret_store import EncryptedCollectionSecretStore
from astrabox.seams.secrets import secret_store_for_name

T = TypeVar("T")

_ENVIRONMENTS = Environment.__table__

#: What a set credential reads as. A write that sends it back keeps the stored
#: value, which is how a form that loaded the masked document saves an
#: unrelated field without replacing the credential with the mask.
SECRET_MASK = "••••••••"

#: The form's top-level scalar fields and their columns.
_SCALAR_COLUMNS = {
    "display_name": "display_name",
    "description": "description",
    "engine_kind": "engine_kind",
    "sandbox_backend": "sandbox_backend",
    "runtime_template_name": "runtime_template_name",
    "idle_action": "idle_action",
    "sandbox_tenancy": "sandbox_tenancy",
    "sandbox_permission_level": "sandbox_permission_level",
    "endpoint_provider": "endpoint_provider",
}

#: The form's nested objects: each sub-field and its column. Credentials are
#: listed in :data:`_SECRET_REFERENCES` instead.
_OBJECT_COLUMNS = {
    "provider_access": {
        "base_url": "provider_base_url",
        "api_key_secret_name": "provider_api_key_secret_name",
    },
    "tracing": {
        "enabled": "tracing_enabled",
        "endpoint": "tracing_endpoint",
        "headers": "tracing_headers",
        "auth_token_secret_name": "tracing_auth_token_secret_name",
        "environment": "tracing_environment",
        "signals": "tracing_signals",
        "log_user_prompt": "tracing_log_user_prompt",
    },
}

#: Each credential, as (object, field, column holding its secret-store key).
_SECRET_REFERENCES = (
    ("provider_access", "api_key", "provider_api_key_ref"),
    ("tracing", "auth_token", "tracing_auth_token_ref"),
)


def secret_scope(name: str) -> str:
    """The secret-store scope holding one Environment's credentials."""
    return f"environment/{name}"


def _secret_store() -> EncryptedCollectionSecretStore:
    """The store that can commit Environment credentials with their references."""
    store = secret_store_for_name(None)
    if not isinstance(store, EncryptedCollectionSecretStore):
        raise APIError(
            code="ENVIRONMENT_SECRET_STORE_NOT_TRANSACTIONAL",
            message="Environment credentials require a secret store backed by the platform database",
            status_code=500,
        )
    return store


def _view(row: dict[str, Any]) -> dict[str, Any]:
    """The stored Environment as a document, with each set credential masked."""
    doc: dict[str, Any] = {"name": row["name"], "enabled": row["enabled"]}
    for key, column in _SCALAR_COLUMNS.items():
        if row[column] is not None:
            doc[key] = row[column]
    networking: dict[str, Any] = {"type": row["network_type"]}
    if row["network_allowed_hosts"] is not None:
        networking["allowed_hosts"] = list(row["network_allowed_hosts"])
    if row["network_allow_mcp_servers"] is not None:
        networking["allow_mcp_servers"] = row["network_allow_mcp_servers"]
    doc["networking"] = networking
    blocks: dict[str, dict[str, Any]] = {
        holder: {
            key: list(row[column]) if isinstance(row[column], list) else row[column]
            for key, column in fields.items()
            if row[column] is not None
        }
        for holder, fields in _OBJECT_COLUMNS.items()
    }
    for holder, field, column in _SECRET_REFERENCES:
        if row[column]:
            blocks[holder][field] = SECRET_MASK
    # Both objects are always present, so a client reads back what it wrote:
    # empty model access selects the deployment's own, empty tracing is off.
    doc.update(blocks)
    doc["created_at"] = to_iso(row["created_at"])
    doc["updated_at"] = to_iso(row["updated_at"])
    if row["updated_by"] is not None:
        doc["updated_by"] = row["updated_by"]
    return doc


def _columns(doc: dict[str, Any]) -> dict[str, Any]:
    """Every column but the credential references, from a whole document.

    A field the document leaves out is stored empty: a write replaces the
    Environment rather than merging into it.
    """
    values: dict[str, Any] = {
        column: doc.get(key) for key, column in _SCALAR_COLUMNS.items()
    }
    values["enabled"] = doc.get("enabled") is not False
    networking = doc["networking"]
    values["network_type"] = networking["type"]
    values["network_allowed_hosts"] = networking.get("allowed_hosts")
    values["network_allow_mcp_servers"] = networking.get("allow_mcp_servers")
    for holder, fields in _OBJECT_COLUMNS.items():
        block = doc.get(holder) if isinstance(doc.get(holder), dict) else {}
        for key, column in fields.items():
            values[column] = block.get(key)
    return values


class EnvironmentRepository:
    """Administrator-managed Environments, keyed by name.

    Besides the sandbox runtime fields an Environment carries ``provider_access``,
    the model-provider access shared by the Agents that select it
    (``docs/domain-model.md``).
    """

    async def list_all(self) -> list[dict[str, Any]]:
        """Every Environment by name, credentials masked."""
        rows = await fetch_all(select(_ENVIRONMENTS).order_by(_ENVIRONMENTS.c.name))
        return [_view(row) for row in rows]

    async def count_all(self) -> int:
        return int(await fetch_value(select(func.count()).select_from(_ENVIRONMENTS)) or 0)

    async def get_any_by_name(self, name: str) -> dict[str, Any] | None:
        """One Environment, enabled or not, credentials masked."""
        row = await fetch_one(select(_ENVIRONMENTS).where(_ENVIRONMENTS.c.name == name))
        return _view(row) if row is not None else None

    async def get_with_secrets(self, name: str) -> dict[str, Any] | None:
        """One Environment with its credentials' values, for the runtime that uses them.

        Raises ``ENVIRONMENT_SECRET_MISSING`` when the row names a credential the
        secret store does not hold: the Environment says to use a key that is
        missing, and running without it would silently change which credential
        the engine presents.
        """
        scope = secret_scope(name)
        statement = select(_ENVIRONMENTS)
        for _holder, _field, column in _SECRET_REFERENCES:
            secret = Secret.__table__.alias(column)
            statement = statement.outerjoin(
                secret,
                and_(secret.c.scope == scope, secret.c.key == _ENVIRONMENTS.c[column]),
            ).add_columns(secret.c.sealed.label(f"{column}_sealed"))
        row = await fetch_one(statement.where(_ENVIRONMENTS.c.name == name))
        if row is None:
            return None
        doc = _view(row)
        store = _secret_store() if any(row[column] for _h, _f, column in _SECRET_REFERENCES) else None
        for holder, field, column in _SECRET_REFERENCES:
            key = row[column]
            if not key:
                continue
            sealed = row[f"{column}_sealed"]
            if sealed is None:
                raise APIError(
                    code="ENVIRONMENT_SECRET_MISSING",
                    message=(
                        f"environment {name!r} names a stored {holder}.{field} that is "
                        "missing from the secret store; enter the credential again"
                    ),
                    status_code=500,
                )
            assert store is not None
            value = await store.unseal(scope=scope, key=key, sealed=sealed)
            doc[holder] = {**doc[holder], field: value}
        return doc

    async def put(
        self, name: str, doc: dict[str, Any], *, updated_by: str | None,
    ) -> dict[str, Any]:
        """Create or replace one Environment; returns it with credentials masked.

        ``doc`` is the whole Environment, already normalized and validated. A
        credential given as a value is stored anew, given as :data:`SECRET_MASK`
        keeps the stored one, and left empty or out removes it. Credential values
        and references are replaced in the same transaction.
        """
        store: EncryptedCollectionSecretStore | None = None
        scope = secret_scope(name)
        values = _columns(doc)
        submitted: dict[str, str | None] = {}
        sealed: dict[str, str] = {}
        for holder, field, column in _SECRET_REFERENCES:
            block = doc.get(holder) if isinstance(doc.get(holder), dict) else {}
            value = str(block.get(field) or "").strip()
            if not value:
                submitted[column] = None
                continue
            if value == SECRET_MASK:
                continue
            if store is None:
                store = _secret_store()
            key = f"{holder}.{field}/{uuid.uuid4().hex}"
            sealed[key] = await store.seal(scope=scope, key=key, value=value)
            submitted[column] = key

        async def replace(conn: AsyncConnection) -> dict[str, Any]:
            # The upsert locks an existing row and serializes concurrent first
            # creates too, before either write changes this scope's secrets.
            await conn.execute(
                upsert_primary_key(
                    conn,
                    _ENVIRONMENTS,
                    {"name": name, **values, "updated_by": updated_by},
                    update_columns=[*values, "updated_by"],
                    update_values={"updated_at": func.now()},
                )
            )
            stored = (await conn.execute(
                select(_ENVIRONMENTS).where(_ENVIRONMENTS.c.name == name)
            )).one()._mapping
            references = {
                column: submitted[column] if column in submitted else stored[column]
                for _holder, _field, column in _SECRET_REFERENCES
            }
            replaced = [
                str(stored[column])
                for _holder, _field, column in _SECRET_REFERENCES
                if stored[column] and stored[column] != references[column]
            ]
            if sealed or any(stored[column] for _h, _f, column in _SECRET_REFERENCES):
                await (store or _secret_store()).apply(conn, scope=scope, put=sealed, remove=replaced)
            await conn.execute(
                update(_ENVIRONMENTS)
                .where(_ENVIRONMENTS.c.name == name)
                .values(**references)
            )
            row = (await conn.execute(
                select(_ENVIRONMENTS).where(_ENVIRONMENTS.c.name == name)
            )).one()
            return dict(row._mapping)

        return _view(await transaction(replace))

    async def delete_after_reference_check(
        self, name: str, check_references: Callable[[AsyncConnection], Awaitable[None]],
    ) -> None:
        """Delete an Environment that ``check_references`` finds unused, atomically.

        One transaction locks the row, runs ``check_references`` on the same
        connection, and deletes the row. An owner that binds the Environment
        takes a key-share lock on the row through its foreign key, so a binding either commits before
        the check reads, or waits and then fails to find the Environment.
        ``check_references`` raises to refuse. Raises ``ENVIRONMENT_NOT_FOUND``
        for a missing Environment, and ``ENVIRONMENT_IN_USE`` when an owner the
        check could not see still references it.
        """

        async def remove(conn: AsyncConnection) -> None:
            held = await conn.execute(
                select(*(_ENVIRONMENTS.c[column] for _h, _f, column in _SECRET_REFERENCES))
                .where(_ENVIRONMENTS.c.name == name)
                .with_for_update()
            )
            row = held.first()
            if row is None:
                raise APIError(
                    code="ENVIRONMENT_NOT_FOUND", message="environment not found", status_code=404,
                )
            await check_references(conn)
            await conn.execute(
                update(Agent.__table__)
                .where(Agent.environment_name == name, Agent.state == "DELETED")
                .values(environment_name=None)
            )
            await conn.execute(
                update(Assistant.__table__)
                .where(Assistant.environment_name == name, Assistant.deleted.is_(True))
                .values(environment_name=None)
            )
            await conn.execute(delete(_ENVIRONMENTS).where(_ENVIRONMENTS.c.name == name))
            if any(row):
                await _secret_store().apply(conn, scope=secret_scope(name), purge=True)

        try:
            await transaction(remove)
        except ForeignKeyViolation as exc:
            raise APIError(
                code="ENVIRONMENT_IN_USE",
                message="Environment is still referenced; move or delete what uses it first.",
                status_code=409,
            ) from exc
