"""Vault and credential metadata: the ``vaults`` and ``vault_credentials`` tables.

Only metadata lives here: display names, the key a credential is matched by,
delivery rules, refresh configuration and times. Secret values (tokens, secret
values, refresh material) live in the :class:`astrabox.seams.secrets.SecretStore`
under ``(vault/<vault_id>, <credential_id>/<field>)``. An operation that changes
a credential's secrets along with its metadata takes the secret-store change as
a callable (``secrets``) and runs it inside its own transaction, after its
statements have succeeded; a store that keeps its values in this database then
commits with the metadata or not at all.

The database enforces what the Vault service relies on:

* one active credential per Vault and key, through the partial unique indexes
  of :class:`astrabox.persistence.sql.models.vaults.VaultCredential`;
* at most ``max_active`` active credentials per Vault, counted while the
  Vault's row is locked, so concurrent creates cannot overshoot;
* a credential belongs to an existing Vault and is deleted with it.

Times are handed to the service as ISO-8601 strings.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from sqlalchemy import case, delete, func, insert, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import ColumnElement

from astrabox.persistence.sql import (
    ForeignKeyViolation,
    UniqueViolation,
    execute,
    fetch_all,
    fetch_one,
    from_iso,
    to_iso,
    transaction,
)
from astrabox.persistence.sql.models.vaults import Vault, VaultCredential
from astrabox.persistence.sql.statements import TimestampAfter

_VAULTS = Vault.__table__
_CREDENTIALS = VaultCredential.__table__

#: The partial unique indexes holding one active credential per Vault and key.
_KEY_INDEXES = frozenset(
    {
        "ix_vault_credentials_vault_id_mcp_server_url",
        "ix_vault_credentials_vault_id_url",
        "ix_vault_credentials_vault_id_secret_name",
    }
)

#: The secret-store side of a metadata change, run on the change's connection.
SecretChange = Callable[[AsyncConnection], Awaitable[None]]

#: A check that refuses, by raising, a destructive Vault change while the
#: Vault's row is locked.
VaultGuard = Callable[[], Awaitable[None]]


class VaultNotFound(Exception):
    """The Vault does not exist."""


class VaultArchived(Exception):
    """The Vault is archived and accepts no new credentials."""


class CredentialLimitReached(Exception):
    """The Vault already holds the maximum number of active credentials."""


class CredentialKeyTaken(Exception):
    """The Vault already holds an active credential with this key."""


class VaultInUse(Exception):
    """A row elsewhere still references the Vault, so it cannot be deleted."""


def new_credential_id() -> str:
    """A fresh credential id.

    Callers take it before creating the credential because its secrets are
    sealed under it, and sealing happens before the create's transaction opens.
    """
    return f"vcr_{uuid.uuid4().hex}"


def _vault_position(vault_ids: Sequence[str]) -> ColumnElement[int]:
    """Order credentials by the first occurrence of their Vault in the selection."""
    return case(
        *(
            (literal(vault_id, type_=_CREDENTIALS.c.vault_id.type), position)
            for position, vault_id in enumerate(dict.fromkeys(vault_ids))
        ),
        value=_CREDENTIALS.c.vault_id,
    )


def _vault_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "vault_id": row["vault_id"],
        "org_id": row["org_id"],
        "created_by": row["created_by"],
        "display_name": row["display_name"],
        "metadata": dict(row["metadata"] or {}),
        "archived_at": to_iso(row["archived_at"]),
        "created_at": to_iso(row["created_at"]),
        "updated_at": to_iso(row["updated_at"]),
    }


def _auth(row: Mapping[str, Any]) -> dict[str, Any]:
    """The credential's ``auth`` document: its type and each non-secret field it has."""
    auth: dict[str, Any] = {"type": row["auth_type"]}
    for field in ("mcp_server_url", "header_name", "url", "username", "secret_name", "delivery"):
        if row[field] is not None:
            auth[field] = row[field]
    if row["expires_at"] is not None:
        auth["expires_at"] = to_iso(row["expires_at"])
    if row["refresh_token_endpoint"] is not None:
        auth["refresh"] = {
            "token_endpoint": row["refresh_token_endpoint"],
            "client_id": row["refresh_client_id"],
            "auth_method": row["refresh_auth_method"],
            "scope": row["refresh_scope"],
        }
    if row["network_access"] is not None:
        networking: dict[str, Any] = {"type": row["network_access"]}
        if row["allowed_hosts"] is not None:
            networking["allowed_hosts"] = list(row["allowed_hosts"])
        auth["networking"] = networking
    if row["inject_header"] is not None:
        auth["injection_location"] = {"header": row["inject_header"], "body": row["inject_body"]}
    allowed_requests = {
        name: list(row[column])
        for name, column in (("methods", "allowed_methods"), ("paths", "allowed_paths"))
        if row[column] is not None
    }
    if allowed_requests:
        auth["allowed_requests"] = allowed_requests
    if row["allow_insecure_http"] is not None:
        auth["allow_insecure_http"] = row["allow_insecure_http"]
    return auth


def _auth_columns(auth: Mapping[str, Any]) -> dict[str, Any]:
    """The columns that store ``auth``; a field it does not carry is stored as ``NULL``."""
    refresh = auth.get("refresh") or {}
    networking = auth.get("networking") or {}
    injection = auth.get("injection_location") or {}
    allowed_requests = auth.get("allowed_requests") or {}
    return {
        "auth_type": auth["type"],
        "mcp_server_url": auth.get("mcp_server_url"),
        "url": auth.get("url"),
        "secret_name": auth.get("secret_name"),
        "username": auth.get("username"),
        "header_name": auth.get("header_name"),
        "expires_at": from_iso(auth.get("expires_at")),
        "refresh_token_endpoint": refresh.get("token_endpoint"),
        "refresh_client_id": refresh.get("client_id"),
        "refresh_auth_method": refresh.get("auth_method"),
        "refresh_scope": refresh.get("scope"),
        "network_access": networking.get("type"),
        "allowed_hosts": networking.get("allowed_hosts"),
        "inject_header": injection.get("header"),
        "inject_body": injection.get("body"),
        "allowed_methods": allowed_requests.get("methods"),
        "allowed_paths": allowed_requests.get("paths"),
        "allow_insecure_http": auth.get("allow_insecure_http"),
        "delivery": auth.get("delivery"),
    }


def _credential_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "credential_id": row["credential_id"],
        "vault_id": row["vault_id"],
        "display_name": row["display_name"],
        "auth": _auth(row),
        "refresh_claimed_until": to_iso(row["refresh_claimed_until"]),
        "archived_at": to_iso(row["archived_at"]),
        "created_at": to_iso(row["created_at"]),
        "updated_at": to_iso(row["updated_at"]),
    }


class VaultRepository:
    """Vault and credential rows; every change spanning statements is one transaction."""

    # ── vaults ──────────────────────────────────────────────────────────────

    async def create_vault(self, doc: dict[str, Any]) -> dict[str, Any]:
        vault_id = str(doc.get("vault_id") or f"vlt_{uuid.uuid4().hex}")

        async def work(conn: AsyncConnection) -> dict[str, Any]:
            await conn.execute(
                insert(_VAULTS).values(
                    vault_id=vault_id,
                    org_id=doc["org_id"],
                    created_by=doc["created_by"],
                    display_name=doc["display_name"],
                    metadata=dict(doc.get("metadata") or {}),
                )
            )
            row = await conn.execute(select(_VAULTS).where(_VAULTS.c.vault_id == vault_id))
            return dict(row.mappings().one())

        row = await transaction(work)
        return _vault_view(row)

    async def get_vault(self, vault_id: str) -> dict[str, Any] | None:
        row = await fetch_one(select(_VAULTS).where(_VAULTS.c.vault_id == vault_id))
        return _vault_view(row) if row is not None else None

    async def get_vaults(self, vault_ids: Sequence[str]) -> list[dict[str, Any]]:
        """The Vaults among ``vault_ids`` that exist, in no particular order."""
        if not vault_ids:
            return []
        rows = await fetch_all(select(_VAULTS).where(_VAULTS.c.vault_id.in_(vault_ids)))
        return [_vault_view(row) for row in rows]

    async def list_vaults(self, *, org_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """The organization's newest Vaults first."""
        rows = await fetch_all(
            select(_VAULTS)
            .where(_VAULTS.c.org_id == org_id)
            .order_by(_VAULTS.c.created_at.desc(), _VAULTS.c.vault_id.desc())
            .limit(limit)
        )
        return [_vault_view(row) for row in rows]

    async def archive_vault(
        self,
        vault_id: str,
        *,
        org_id: str,
        ensure_unbound: VaultGuard,
        secrets: SecretChange,
    ) -> dict[str, Any] | None:
        """Archive the Vault and its active credentials; ``None`` when the organization has no such Vault.

        The Vault's row is locked ``FOR UPDATE`` first and ``ensure_unbound``
        runs while it is held: a binding written afterwards waits for the
        archive to commit, and its check then sees the Vault archived. An
        archived Vault keeps the time it was first archived.
        """

        async def work(conn: AsyncConnection) -> dict[str, Any] | None:
            locked = await conn.execute(
                select(_VAULTS.c.vault_id)
                .where(_VAULTS.c.vault_id == vault_id, _VAULTS.c.org_id == org_id)
                .with_for_update()
            )
            if locked.first() is None:
                return None
            await ensure_unbound()
            await conn.execute(
                update(_CREDENTIALS)
                .where(_CREDENTIALS.c.vault_id == vault_id, _CREDENTIALS.c.archived_at.is_(None))
                .values(archived_at=func.now(), updated_at=func.now())
            )
            await conn.execute(
                update(_VAULTS)
                .where(_VAULTS.c.vault_id == vault_id)
                .values(
                    archived_at=func.coalesce(_VAULTS.c.archived_at, func.now()),
                    updated_at=func.now(),
                )
            )
            archived = await conn.execute(select(_VAULTS).where(_VAULTS.c.vault_id == vault_id))
            row = dict(archived.mappings().one())
            await secrets(conn)
            return row

        row = await transaction(work)
        return _vault_view(row) if row is not None else None

    async def delete_vault(
        self,
        vault_id: str,
        *,
        org_id: str,
        ensure_unbound: VaultGuard,
        secrets: SecretChange,
    ) -> bool:
        """Delete the Vault and its credentials; ``False`` when the organization has no such Vault.

        ``ensure_unbound`` runs under the Vault's row lock as in
        :meth:`archive_vault`. Raises :class:`VaultInUse` when a binding row
        still references the Vault: its foreign key refuses the delete, and the
        transaction keeps the credentials and secrets.
        """

        async def work(conn: AsyncConnection) -> bool:
            locked = await conn.execute(
                select(_VAULTS.c.vault_id)
                .where(_VAULTS.c.vault_id == vault_id, _VAULTS.c.org_id == org_id)
                .with_for_update()
            )
            if locked.first() is None:
                return False
            await ensure_unbound()
            await conn.execute(delete(_VAULTS).where(_VAULTS.c.vault_id == vault_id))
            await secrets(conn)
            return True

        try:
            return await transaction(work)
        except ForeignKeyViolation as exc:
            raise VaultInUse(vault_id) from exc

    # ── credentials ─────────────────────────────────────────────────────────

    async def create_credential(
        self, doc: dict[str, Any], *, max_active: int, secrets: SecretChange
    ) -> dict[str, Any]:
        """Insert an active credential, and its secrets through ``secrets``.

        The Vault's row is locked ``FOR NO KEY UPDATE`` before its active
        credentials are counted, so concurrent creates in one Vault count one
        after another. Raises :class:`VaultNotFound`, :class:`VaultArchived`,
        :class:`CredentialLimitReached` or :class:`CredentialKeyTaken`; nothing
        is written then.
        """
        vault_id = str(doc["vault_id"])
        values = {
            "credential_id": str(doc["credential_id"]),
            "vault_id": vault_id,
            "display_name": doc.get("display_name"),
            **_auth_columns(doc["auth"]),
        }

        async def work(conn: AsyncConnection) -> dict[str, Any]:
            vault = (
                await conn.execute(
                    select(_VAULTS.c.archived_at)
                    .where(_VAULTS.c.vault_id == vault_id)
                    .with_for_update(key_share=True)
                )
            ).first()
            if vault is None:
                raise VaultNotFound(vault_id)
            if vault.archived_at is not None:
                raise VaultArchived(vault_id)
            active = (
                await conn.execute(
                    select(func.count())
                    .select_from(_CREDENTIALS)
                    .where(_CREDENTIALS.c.vault_id == vault_id, _CREDENTIALS.c.archived_at.is_(None))
                )
            ).scalar_one()
            if active >= max_active:
                raise CredentialLimitReached(vault_id)
            await conn.execute(insert(_CREDENTIALS).values(**values))
            inserted = await conn.execute(
                select(_CREDENTIALS).where(_CREDENTIALS.c.credential_id == values["credential_id"])
            )
            row = dict(inserted.mappings().one())
            await secrets(conn)
            return row

        try:
            row = await transaction(work)
        except UniqueViolation as exc:
            if exc.constraint in _KEY_INDEXES:
                raise CredentialKeyTaken(vault_id) from exc
            raise
        return _credential_view(row)

    async def get_credential(self, credential_id: str) -> dict[str, Any] | None:
        row = await fetch_one(
            select(_CREDENTIALS).where(_CREDENTIALS.c.credential_id == credential_id)
        )
        return _credential_view(row) if row is not None else None

    async def list_credentials(
        self,
        vault_ids: Sequence[str],
        *,
        active_only: bool = False,
        auth_types: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Credentials of ``vault_ids``, in that Vault order and by creation within a Vault.

        ``auth_types`` keeps only those types when given.
        """
        if not vault_ids:
            return []
        statement = select(_CREDENTIALS).where(_CREDENTIALS.c.vault_id.in_(vault_ids))
        if active_only:
            statement = statement.where(_CREDENTIALS.c.archived_at.is_(None))
        if auth_types:
            statement = statement.where(_CREDENTIALS.c.auth_type.in_(auth_types))
        rows = await fetch_all(
            statement.order_by(
                _vault_position(vault_ids),
                _CREDENTIALS.c.created_at,
                _CREDENTIALS.c.credential_id,
            )
        )
        return [_credential_view(row) for row in rows]

    async def find_mcp_credentials(
        self, vault_ids: Sequence[str], server_urls: Sequence[str]
    ) -> list[dict[str, Any]]:
        """For each server URL, the active credential of the first Vault in ``vault_ids`` that has one.

        One statement for every URL and Vault; a URL no Vault matches is absent.
        URLs compare as stored, so callers pass normalized URLs.
        """
        if not vault_ids or not server_urls:
            return []
        ranked = (
            select(
                _CREDENTIALS,
                func.row_number().over(
                    partition_by=_CREDENTIALS.c.mcp_server_url,
                    order_by=_vault_position(vault_ids),
                ).label("vault_rank"),
            )
            .where(
                _CREDENTIALS.c.vault_id.in_(vault_ids),
                _CREDENTIALS.c.mcp_server_url.in_(server_urls),
                _CREDENTIALS.c.archived_at.is_(None),
            )
            .subquery()
        )
        rows = await fetch_all(
            select(*(ranked.c[column.name] for column in _CREDENTIALS.c))
            .where(ranked.c.vault_rank == 1)
            .order_by(ranked.c.mcp_server_url)
        )
        return [_credential_view(row) for row in rows]

    async def update_credential(
        self,
        credential_id: str,
        *,
        vault_id: str,
        updates: Mapping[str, Any],
        secrets: SecretChange,
    ) -> dict[str, Any] | None:
        """Apply ``display_name`` and ``auth`` from ``updates`` to an active credential.

        ``None``, with nothing written, when the Vault holds no such active
        credential.
        """
        values: dict[str, Any] = {}
        if "display_name" in updates:
            values["display_name"] = updates["display_name"]
        if "auth" in updates:
            values.update(_auth_columns(updates["auth"]))

        async def work(conn: AsyncConnection) -> dict[str, Any] | None:
            updated = await conn.execute(
                update(_CREDENTIALS)
                .where(
                    _CREDENTIALS.c.credential_id == credential_id,
                    _CREDENTIALS.c.vault_id == vault_id,
                    _CREDENTIALS.c.archived_at.is_(None),
                )
                .values(**values, updated_at=func.now())
            )
            if updated.rowcount == 0:
                return None
            row = await conn.execute(
                select(_CREDENTIALS).where(_CREDENTIALS.c.credential_id == credential_id)
            )
            await secrets(conn)
            return dict(row.mappings().one())

        row = await transaction(work)
        return _credential_view(row) if row is not None else None

    async def archive_credential(
        self, credential_id: str, *, vault_id: str, secrets: SecretChange
    ) -> dict[str, Any] | None:
        """Archive the credential, keeping the time it was first archived.

        ``None``, with nothing written, when the Vault holds no such credential.
        """

        async def work(conn: AsyncConnection) -> dict[str, Any] | None:
            archived = await conn.execute(
                update(_CREDENTIALS)
                .where(
                    _CREDENTIALS.c.credential_id == credential_id,
                    _CREDENTIALS.c.vault_id == vault_id,
                )
                .values(
                    archived_at=func.coalesce(_CREDENTIALS.c.archived_at, func.now()),
                    updated_at=func.now(),
                )
            )
            if archived.rowcount == 0:
                return None
            row = await conn.execute(
                select(_CREDENTIALS).where(_CREDENTIALS.c.credential_id == credential_id)
            )
            await secrets(conn)
            return dict(row.mappings().one())

        row = await transaction(work)
        return _credential_view(row) if row is not None else None

    async def delete_credential(
        self, credential_id: str, *, vault_id: str, secrets: SecretChange
    ) -> bool:
        """Delete the credential; ``False``, with nothing written, when the Vault holds no such credential."""

        async def work(conn: AsyncConnection) -> bool:
            deleted = await conn.execute(
                delete(_CREDENTIALS)
                .where(
                    _CREDENTIALS.c.credential_id == credential_id,
                    _CREDENTIALS.c.vault_id == vault_id,
                )
            )
            if deleted.rowcount == 0:
                return False
            await secrets(conn)
            return True

        return await transaction(work)

    # ── OAuth refresh claim ─────────────────────────────────────────────────

    async def claim_oauth_refresh(
        self, credential_id: str, *, expires_at: str, lease_s: float
    ) -> dict[str, Any] | None:
        """Take the right to refresh the credential's access token, by compare-and-set.

        The claim succeeds only while the credential is active, still expires
        at ``expires_at`` (the expiry the caller judged expired) and holds no
        unexpired claim. It lasts ``lease_s`` seconds by the database clock.
        Returns the claimed credential, whose ``refresh_claimed_until``
        identifies the claim, or ``None``.
        """

        async def work(conn: AsyncConnection) -> dict[str, Any] | None:
            claimed = await conn.execute(
                update(_CREDENTIALS)
                .where(
                    _CREDENTIALS.c.credential_id == credential_id,
                    _CREDENTIALS.c.archived_at.is_(None),
                    _CREDENTIALS.c.expires_at == from_iso(expires_at),
                    or_(
                        _CREDENTIALS.c.refresh_claimed_until.is_(None),
                        _CREDENTIALS.c.refresh_claimed_until <= func.now(),
                    ),
                )
                .values(refresh_claimed_until=TimestampAfter(func.now(), lease_s))
            )
            if claimed.rowcount == 0:
                return None
            row = await conn.execute(
                select(_CREDENTIALS).where(_CREDENTIALS.c.credential_id == credential_id)
            )
            return dict(row.mappings().one())

        row = await transaction(work)
        return _credential_view(row) if row is not None else None

    async def complete_oauth_refresh(
        self,
        credential_id: str,
        *,
        claim: str,
        expires_at: str | None,
        secrets: SecretChange,
    ) -> dict[str, Any] | None:
        """Record a refreshed token under ``claim`` and release the claim.

        ``secrets`` stores the new token values in the same transaction.
        ``None``, with nothing written, when the row does not carry ``claim``
        (it lapsed and another caller claimed) or the credential was archived
        or deleted meanwhile.
        """

        async def work(conn: AsyncConnection) -> dict[str, Any] | None:
            refreshed = await conn.execute(
                update(_CREDENTIALS)
                .where(
                    _CREDENTIALS.c.credential_id == credential_id,
                    _CREDENTIALS.c.refresh_claimed_until == from_iso(claim),
                    _CREDENTIALS.c.archived_at.is_(None),
                )
                .values(
                    expires_at=from_iso(expires_at),
                    refresh_claimed_until=None,
                    updated_at=func.now(),
                )
            )
            if refreshed.rowcount == 0:
                return None
            row = await conn.execute(
                select(_CREDENTIALS).where(_CREDENTIALS.c.credential_id == credential_id)
            )
            await secrets(conn)
            return dict(row.mappings().one())

        row = await transaction(work)
        return _credential_view(row) if row is not None else None

    async def release_oauth_refresh(self, credential_id: str, *, claim: str) -> None:
        """Give ``claim`` up without a result, so the next caller may refresh."""
        await execute(
            update(_CREDENTIALS)
            .where(
                _CREDENTIALS.c.credential_id == credential_id,
                _CREDENTIALS.c.refresh_claimed_until == from_iso(claim),
            )
            .values(refresh_claimed_until=None)
        )
