"""Model accounts and the sign-ins that link them: the ``model_accounts`` and
``model_account_authorizations`` tables.

Only metadata lives here: the identity the vendor validated, the state, and the
renewal bookkeeping. The tokens live in the
:class:`astrabox.seams.secrets.SecretStore` under ``model-account/<account_id>``.
An operation that changes an account's tokens along with its row takes the
secret-store change as a callable (``secrets``) and runs it inside its own
transaction, after its statements have succeeded; a store that keeps its values
in this database then commits with the row or not at all.

The database enforces what the model-account service relies on: one row per
owner and ChatGPT registration (subject and issued client id), and at most one
default account per owner and vendor
(:class:`astrabox.persistence.sql.models.model_accounts.ModelAccount`).

Times are handed to the service as ISO-8601 strings.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.sql import (
    UniqueViolation,
    execute,
    fetch_all,
    fetch_one,
    from_iso,
    to_iso,
    transaction,
)
from astrabox.persistence.sql.models.model_accounts import (
    ModelAccount,
    ModelAccountAuthorization,
)
from astrabox.persistence.sql.statements import TimestampAfter

_ACCOUNTS = ModelAccount.__table__
_AUTHORIZATIONS = ModelAccountAuthorization.__table__

#: The partial unique index holding one default account per owner.
_DEFAULT_INDEX = "ix_model_accounts_owner_default"

STATUS_ACTIVE = "active"
STATUS_SIGN_IN_REQUIRED = "sign_in_required"

#: The secret-store side of a row change, run on the change's connection.
SecretChange = Callable[[AsyncConnection], Awaitable[None]]


def new_account_id() -> str:
    """A fresh account id.

    Callers take it before creating the account because its tokens are sealed
    under it, and sealing happens before the create's transaction opens.
    """
    return f"mact_{uuid.uuid4().hex}"


def _account_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "account_id": row["account_id"],
        "org_id": row["org_id"],
        "owner_user_id": row["owner_user_id"],
        "vendor": row["vendor"],
        "label": row["label"],
        "email": row["email"],
        "subject": row["subject"],
        "issued_client_id": row["issued_client_id"],
        "scopes": row["scopes"],
        "status": row["status"],
        "is_default": bool(row["is_default"]),
        "access_expires_at": to_iso(row["access_expires_at"]),
        "refresh_claimed_until": to_iso(row["refresh_claimed_until"]),
        "last_error_code": row["last_error_code"],
        "last_error_at": to_iso(row["last_error_at"]),
        "shared": bool(row["shared"]),
        "usage_limited_until": to_iso(row["usage_limited_until"]),
        "pool_last_used_at": to_iso(row["pool_last_used_at"]),
        "created_at": to_iso(row["created_at"]),
        "updated_at": to_iso(row["updated_at"]),
    }


def _authorization_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "authorization_id": row["authorization_id"],
        "owner_user_id": row["owner_user_id"],
        "account_id": row["account_id"],
        "client_id": row["client_id"],
        "redirect_uri": row["redirect_uri"],
        "state": row["state"],
        "nonce": row["nonce"],
        "code_verifier": row["code_verifier"],
        "expires_at": to_iso(row["expires_at"]),
    }


async def _account_row(conn: AsyncConnection, account_id: str) -> dict[str, Any]:
    row = await conn.execute(select(_ACCOUNTS).where(_ACCOUNTS.c.account_id == account_id))
    return dict(row.mappings().one())


class ModelAccountRepository:
    """Account and sign-in rows; every change spanning statements is one transaction."""

    # ── accounts ────────────────────────────────────────────────────────────

    async def get_account(self, account_id: str) -> dict[str, Any] | None:
        row = await fetch_one(select(_ACCOUNTS).where(_ACCOUNTS.c.account_id == account_id))
        return _account_view(row) if row is not None else None

    async def get_default_account(self, owner_user_id: str, *, vendor: str) -> dict[str, Any] | None:
        row = await fetch_one(
            select(_ACCOUNTS).where(
                _ACCOUNTS.c.owner_user_id == owner_user_id,
                _ACCOUNTS.c.vendor == vendor,
                _ACCOUNTS.c.is_default.is_(True),
            )
        )
        return _account_view(row) if row is not None else None

    async def find_registration(
        self, *, owner_user_id: str, subject: str, issued_client_id: str
    ) -> dict[str, Any] | None:
        """The owner's account for one registration at the vendor, if linked."""
        row = await fetch_one(
            select(_ACCOUNTS).where(
                _ACCOUNTS.c.owner_user_id == owner_user_id,
                _ACCOUNTS.c.subject == subject,
                _ACCOUNTS.c.issued_client_id == issued_client_id,
            )
        )
        return _account_view(row) if row is not None else None

    async def list_accounts(self, owner_user_id: str) -> list[dict[str, Any]]:
        """The owner's accounts, oldest first."""
        rows = await fetch_all(
            select(_ACCOUNTS)
            .where(_ACCOUNTS.c.owner_user_id == owner_user_id)
            .order_by(_ACCOUNTS.c.created_at, _ACCOUNTS.c.account_id)
        )
        return [_account_view(row) for row in rows]

    async def create_account(
        self,
        values: Mapping[str, Any],
        *,
        access_expires_at: str | None,
        secrets: SecretChange,
    ) -> dict[str, Any]:
        """Insert an active account, and its secrets through ``secrets``.

        The account becomes its owner's default of its vendor when the owner
        has none. The owner's rows are locked first, so a concurrent change of
        the default waits for this insert; two concurrent first accounts both
        find no default, the unique index refuses the second, and that one is
        inserted again as not the default.
        """
        owner_user_id = str(values["owner_user_id"])
        vendor = str(values["vendor"])
        row_values = {
            **values,
            "status": STATUS_ACTIVE,
            "access_expires_at": from_iso(access_expires_at),
        }

        async def work(conn: AsyncConnection, *, may_default: bool) -> dict[str, Any]:
            owned = await conn.execute(
                select(_ACCOUNTS.c.is_default, _ACCOUNTS.c.vendor)
                .where(_ACCOUNTS.c.owner_user_id == owner_user_id)
                .with_for_update()
            )
            has_default = any(bool(row.is_default) and row.vendor == vendor for row in owned)
            await conn.execute(
                insert(_ACCOUNTS).values(
                    **row_values, is_default=may_default and not has_default
                )
            )
            row = await _account_row(conn, str(values["account_id"]))
            await secrets(conn)
            return row

        try:
            row = await transaction(lambda conn: work(conn, may_default=True))
        except UniqueViolation as exc:
            if exc.constraint != _DEFAULT_INDEX:
                raise
            row = await transaction(lambda conn: work(conn, may_default=False))
        return _account_view(row)

    async def record_sign_in(
        self,
        account_id: str,
        *,
        owner_user_id: str,
        email: str | None,
        scopes: str,
        access_expires_at: str,
        secrets: SecretChange,
    ) -> dict[str, Any] | None:
        """Make the account active with the tokens of a new sign-in.

        Clears the last renewal error and any refresh claim: a renewal still
        running presents a refresh token this sign-in replaced, and its result
        is refused. ``None``, with nothing written, when the owner has no such
        account.
        """

        async def work(conn: AsyncConnection) -> dict[str, Any] | None:
            updated = await conn.execute(
                update(_ACCOUNTS)
                .where(
                    _ACCOUNTS.c.account_id == account_id,
                    _ACCOUNTS.c.owner_user_id == owner_user_id,
                )
                .values(
                    email=email,
                    scopes=scopes,
                    status=STATUS_ACTIVE,
                    access_expires_at=from_iso(access_expires_at),
                    refresh_claimed_until=None,
                    last_error_code=None,
                    last_error_at=None,
                    updated_at=func.now(),
                )
            )
            if updated.rowcount == 0:
                return None
            row = await _account_row(conn, account_id)
            await secrets(conn)
            return row

        row = await transaction(work)
        return _account_view(row) if row is not None else None

    async def update_account(
        self,
        account_id: str,
        *,
        owner_user_id: str,
        label: str | None = None,
        is_default: bool | None = None,
        shared: bool | None = None,
    ) -> dict[str, Any] | None:
        """Rename the account, make it (or stop it being) the owner's default of its vendor, share it or stop sharing it.

        ``None`` leaves that field as it is. The owner's rows are locked first,
        so two concurrent changes of the default apply one after the other.
        ``None``, with nothing written, when the owner has no such account.
        """

        async def work(conn: AsyncConnection) -> dict[str, Any] | None:
            owned = await conn.execute(
                select(_ACCOUNTS.c.account_id, _ACCOUNTS.c.vendor)
                .where(_ACCOUNTS.c.owner_user_id == owner_user_id)
                .with_for_update()
            )
            vendors = {row.account_id: row.vendor for row in owned}
            if account_id not in vendors:
                return None
            if is_default:
                await conn.execute(
                    update(_ACCOUNTS)
                    .where(
                        _ACCOUNTS.c.owner_user_id == owner_user_id,
                        _ACCOUNTS.c.vendor == vendors[account_id],
                        _ACCOUNTS.c.account_id != account_id,
                        _ACCOUNTS.c.is_default.is_(True),
                    )
                    .values(is_default=False, updated_at=func.now())
                )
            values: dict[str, Any] = {}
            if label is not None:
                values["label"] = label
            if is_default is not None:
                values["is_default"] = is_default
            if shared is not None:
                values["shared"] = shared
            if values:
                await conn.execute(
                    update(_ACCOUNTS)
                    .where(_ACCOUNTS.c.account_id == account_id)
                    .values(**values, updated_at=func.now())
                )
            return await _account_row(conn, account_id)

        row = await transaction(work)
        return _account_view(row) if row is not None else None

    async def replace_secret(
        self, account_id: str, *, owner_user_id: str, secrets: SecretChange
    ) -> dict[str, Any] | None:
        """Write the account's new secret through ``secrets`` and mark it updated.

        ``None``, with nothing written, when the owner has no such account.
        """

        async def work(conn: AsyncConnection) -> dict[str, Any] | None:
            updated = await conn.execute(
                update(_ACCOUNTS)
                .where(
                    _ACCOUNTS.c.account_id == account_id,
                    _ACCOUNTS.c.owner_user_id == owner_user_id,
                )
                .values(updated_at=func.now())
            )
            if updated.rowcount == 0:
                return None
            row = await _account_row(conn, account_id)
            await secrets(conn)
            return row

        row = await transaction(work)
        return _account_view(row) if row is not None else None

    async def delete_account(
        self, account_id: str, *, owner_user_id: str, secrets: SecretChange
    ) -> bool:
        """Delete the account and, through ``secrets``, its tokens.

        Its sign-ins in progress go with it. ``False``, with nothing written,
        when the owner has no such account.
        """

        async def work(conn: AsyncConnection) -> bool:
            deleted = await conn.execute(
                delete(_ACCOUNTS).where(
                    _ACCOUNTS.c.account_id == account_id,
                    _ACCOUNTS.c.owner_user_id == owner_user_id,
                )
            )
            if deleted.rowcount == 0:
                return False
            await secrets(conn)
            return True

        return await transaction(work)

    # ── the team pool ───────────────────────────────────────────────────────

    async def list_pool_accounts(
        self, *, org_id: str, excluding_owner: str, vendor: str
    ) -> list[dict[str, Any]]:
        """The active accounts of ``vendor`` other owners of ``org_id`` shared with the team, oldest first."""
        rows = await fetch_all(
            select(_ACCOUNTS)
            .where(
                _ACCOUNTS.c.org_id == org_id,
                _ACCOUNTS.c.owner_user_id != excluding_owner,
                _ACCOUNTS.c.vendor == vendor,
                _ACCOUNTS.c.shared.is_(True),
                _ACCOUNTS.c.status == STATUS_ACTIVE,
            )
            .order_by(_ACCOUNTS.c.created_at, _ACCOUNTS.c.account_id)
        )
        return [_account_view(row) for row in rows]

    async def limit_usage(self, account_id: str, *, until: str) -> None:
        """Keep the account from being chosen for a conversation until ``until``."""
        await execute(
            update(_ACCOUNTS)
            .where(_ACCOUNTS.c.account_id == account_id)
            .values(usage_limited_until=from_iso(until), updated_at=func.now())
        )

    async def record_pool_use(self, account_id: str) -> None:
        """Note that the pool gave the account to another member's conversation now."""
        await execute(
            update(_ACCOUNTS)
            .where(_ACCOUNTS.c.account_id == account_id)
            .values(pool_last_used_at=func.now())
        )

    # ── refresh claim ───────────────────────────────────────────────────────

    async def claim_refresh(
        self, account_id: str, *, expires_at: str | None, lease_s: float
    ) -> dict[str, Any] | None:
        """Take the right to renew the account's access token, by compare-and-set.

        The claim succeeds only while the account is active, its access token
        still expires at ``expires_at`` (the expiry the caller judged due for
        renewal) and it holds no unexpired claim. It lasts ``lease_s`` seconds
        by the database clock. Returns the claimed account, whose
        ``refresh_claimed_until`` identifies the claim, or ``None``.
        """

        async def work(conn: AsyncConnection) -> dict[str, Any] | None:
            claimed = await conn.execute(
                update(_ACCOUNTS)
                .where(
                    _ACCOUNTS.c.account_id == account_id,
                    _ACCOUNTS.c.status == STATUS_ACTIVE,
                    _ACCOUNTS.c.access_expires_at.is_(None)
                    if expires_at is None
                    else _ACCOUNTS.c.access_expires_at == from_iso(expires_at),
                    or_(
                        _ACCOUNTS.c.refresh_claimed_until.is_(None),
                        _ACCOUNTS.c.refresh_claimed_until <= func.now(),
                    ),
                )
                .values(refresh_claimed_until=TimestampAfter(func.now(), lease_s))
            )
            if claimed.rowcount == 0:
                return None
            return await _account_row(conn, account_id)

        row = await transaction(work)
        return _account_view(row) if row is not None else None

    async def complete_refresh(
        self,
        account_id: str,
        *,
        claim: str,
        access_expires_at: str,
        scopes: str,
        secrets: SecretChange,
    ) -> dict[str, Any] | None:
        """Record a renewed token set under ``claim`` and release the claim.

        ``secrets`` stores the new tokens in the same transaction. ``None``,
        with nothing written, when the row does not carry ``claim`` (it lapsed
        and another caller claimed, or a new sign-in replaced the tokens) or
        the account was deleted meanwhile.
        """

        async def work(conn: AsyncConnection) -> dict[str, Any] | None:
            refreshed = await conn.execute(
                update(_ACCOUNTS)
                .where(
                    _ACCOUNTS.c.account_id == account_id,
                    _ACCOUNTS.c.refresh_claimed_until == from_iso(claim),
                )
                .values(
                    access_expires_at=from_iso(access_expires_at),
                    scopes=scopes,
                    refresh_claimed_until=None,
                    updated_at=func.now(),
                )
            )
            if refreshed.rowcount == 0:
                return None
            row = await _account_row(conn, account_id)
            await secrets(conn)
            return row

        row = await transaction(work)
        return _account_view(row) if row is not None else None

    async def require_sign_in(
        self,
        account_id: str,
        *,
        claim: str,
        error_code: str,
        secrets: SecretChange,
    ) -> dict[str, Any] | None:
        """Record under ``claim`` that the vendor refused renewal for good.

        The account needs a new sign-in: it loses its access token expiry,
        keeps ``error_code`` and the time, and ``secrets`` removes the unusable
        tokens in the same transaction. The claim is released. ``None``, with
        nothing written, when the row does not carry ``claim``.
        """

        async def work(conn: AsyncConnection) -> dict[str, Any] | None:
            marked = await conn.execute(
                update(_ACCOUNTS)
                .where(
                    _ACCOUNTS.c.account_id == account_id,
                    _ACCOUNTS.c.refresh_claimed_until == from_iso(claim),
                )
                .values(
                    status=STATUS_SIGN_IN_REQUIRED,
                    access_expires_at=None,
                    refresh_claimed_until=None,
                    last_error_code=error_code,
                    last_error_at=func.now(),
                    updated_at=func.now(),
                )
            )
            if marked.rowcount == 0:
                return None
            row = await _account_row(conn, account_id)
            await secrets(conn)
            return row

        row = await transaction(work)
        return _account_view(row) if row is not None else None

    async def release_refresh(self, account_id: str, *, claim: str) -> None:
        """Give ``claim`` up without a result, so the next caller may renew."""
        await execute(
            update(_ACCOUNTS)
            .where(
                _ACCOUNTS.c.account_id == account_id,
                _ACCOUNTS.c.refresh_claimed_until == from_iso(claim),
            )
            .values(refresh_claimed_until=None)
        )

    # ── sign-ins in progress ────────────────────────────────────────────────

    async def create_authorization(
        self, values: Mapping[str, Any], *, ttl_s: float
    ) -> dict[str, Any]:
        """Insert a sign-in that expires ``ttl_s`` seconds from now by the database clock.

        Sign-ins that expired before it are deleted in the same transaction.
        """
        authorization_id = str(values["authorization_id"])

        async def work(conn: AsyncConnection) -> dict[str, Any]:
            await conn.execute(
                delete(_AUTHORIZATIONS).where(_AUTHORIZATIONS.c.expires_at <= func.now())
            )
            await conn.execute(
                insert(_AUTHORIZATIONS).values(
                    **values, expires_at=TimestampAfter(func.now(), ttl_s)
                )
            )
            row = await conn.execute(
                select(_AUTHORIZATIONS).where(
                    _AUTHORIZATIONS.c.authorization_id == authorization_id
                )
            )
            return dict(row.mappings().one())

        return _authorization_view(await transaction(work))

    async def take_authorization(
        self, authorization_id: str, *, owner_user_id: str
    ) -> dict[str, Any] | None:
        """Delete and return the owner's unexpired sign-in, which is used once.

        ``None`` when the owner has no such sign-in or it has expired; an
        expired one is deleted as well.
        """

        async def work(conn: AsyncConnection) -> dict[str, Any] | None:
            found = await conn.execute(
                select(_AUTHORIZATIONS, (_AUTHORIZATIONS.c.expires_at > func.now()).label("live"))
                .where(
                    _AUTHORIZATIONS.c.authorization_id == authorization_id,
                    _AUTHORIZATIONS.c.owner_user_id == owner_user_id,
                )
                .with_for_update()
            )
            row = found.mappings().first()
            if row is None:
                return None
            await conn.execute(
                delete(_AUTHORIZATIONS).where(
                    _AUTHORIZATIONS.c.authorization_id == authorization_id
                )
            )
            return dict(row) if row["live"] else None

        row = await transaction(work)
        return _authorization_view(row) if row is not None else None


__all__ = [
    "ModelAccountRepository",
    "STATUS_ACTIVE",
    "STATUS_SIGN_IN_REQUIRED",
    "SecretChange",
    "new_account_id",
]
