"""The model-provider accounts users link, and the sign-ins that link them."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base
from astrabox.persistence.sql.schema import (
    MYSQL_TABLE_OPTIONS,
    boolean_checks,
    conditional_unique,
)
from astrabox.persistence.sql.types import KeyText, payload_text


class ModelAccount(Base):
    """One user's account at a model vendor, whose plan pays for their model requests.

    The tokens live in the configured secret store under the scope
    ``model-account/<account_id>``; this row holds the identity the vendor
    validated, the state, and the renewal bookkeeping.

    An account is one registration at the vendor: the client id the vendor
    issued for it and the subject its validated ID token named. The same
    vendor account linked twice is two registrations with two issued client
    ids, so the unique key spans both.

    An owner has at most one default account; the partial unique index holds
    that against concurrent changes. Deleting the default account leaves the
    owner without one until they choose another.

    ``refresh_claimed_until`` is the refresh claim: a replica renewing the
    access token holds it until then, and no other replica may renew the token
    meanwhile, because the vendor rotates the refresh token on every renewal.
    """

    __tablename__ = "model_accounts"
    __table_args__ = (
        CheckConstraint("vendor IN ('chatgpt_plan')", name="known_vendor"),
        CheckConstraint("status IN ('active', 'sign_in_required')", name="known_status"),
        UniqueConstraint("owner_user_id", "subject", "issued_client_id"),
        *conditional_unique(
            "ix_model_accounts_owner_default", "owner_user_id",
            predicate=text("is_default"), marker="_default_slot",
        ),
        Index(None, "owner_user_id", "created_at"),
        *boolean_checks("is_default"),
        MYSQL_TABLE_OPTIONS,
    )

    account_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(payload_text())
    owner_user_id: Mapped[str] = mapped_column(KeyText(512))
    vendor: Mapped[str] = mapped_column(payload_text())
    label: Mapped[str] = mapped_column(payload_text())
    #: The email the validated ID token carried; a label for people, not an identifier.
    email: Mapped[str | None] = mapped_column(payload_text())
    #: The validated ID token's ``sub``.
    subject: Mapped[str] = mapped_column(KeyText(512))
    issued_client_id: Mapped[str] = mapped_column(KeyText(512))
    #: The granted scopes, space-separated as the token endpoint returned them.
    scopes: Mapped[str] = mapped_column(payload_text())
    status: Mapped[str] = mapped_column(payload_text())
    is_default: Mapped[bool] = mapped_column(server_default=text("false"))
    #: When the stored access token expires; ``NULL`` once the tokens are cleared.
    access_expires_at: Mapped[datetime | None]
    refresh_claimed_until: Mapped[datetime | None]
    #: The vendor's error code from the renewal that required a new sign-in.
    last_error_code: Mapped[str | None] = mapped_column(payload_text())
    last_error_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ModelAccountAuthorization(Base):
    """A sign-in started and not yet completed; used once, then deleted.

    ``state``, ``nonce`` and ``code_verifier`` never leave the server: the
    authorization URL carries only the state, the nonce and the verifier's
    challenge. ``client_id`` is the one the authorization request named, and
    ``account_id`` the account a reauthorization signs in to again (``NULL``
    for a new registration).
    """

    __tablename__ = "model_account_authorizations"
    __table_args__ = (
        Index(None, "expires_at"),
        MYSQL_TABLE_OPTIONS,
    )

    authorization_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(KeyText(512))
    account_id: Mapped[str | None] = mapped_column(
        KeyText(128), ForeignKey("model_accounts.account_id", ondelete="CASCADE")
    )
    client_id: Mapped[str] = mapped_column(payload_text())
    redirect_uri: Mapped[str] = mapped_column(payload_text())
    state: Mapped[str] = mapped_column(payload_text())
    nonce: Mapped[str] = mapped_column(payload_text())
    code_verifier: Mapped[str] = mapped_column(payload_text())
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
