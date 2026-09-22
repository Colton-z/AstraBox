"""Request models shared by Agent and Assistant conversation routes."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from astrabox.common.utils.errors import APIError


_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class StartConversationRequest(BaseModel):
    """Conversation start body.

    Credentials are deliberately absent. They are bound to the managed Agent or
    Assistant configuration; accepting a user-supplied Vault id here would let a
    conversation replace that management decision. Keeping an empty, strict
    model lets ``POST`` without a body and ``{}`` work while unknown fields fail.
    """

    model_config = ConfigDict(extra="forbid")


def conversation_idempotency_key(raw: str | None) -> str | None:
    """Validate the standard retry key used by conversation-create POSTs.

    The key is opaque to callers and never becomes a credential.  Keeping its
    alphabet and length bounded makes it safe to persist as command metadata
    and rejects accidental whole-token/header dumps at the API boundary.
    """

    value = str(raw or "").strip()
    if not value:
        return None
    if _IDEMPOTENCY_KEY_RE.fullmatch(value) is None:
        raise APIError(
            code="INVALID_IDEMPOTENCY_KEY",
            message="Idempotency-Key must be 1-128 ASCII letters, digits, '.', '_', ':' or '-'",
            status_code=400,
        )
    return value


__all__ = ["StartConversationRequest", "conversation_idempotency_key"]
