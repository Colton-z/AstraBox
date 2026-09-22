"""Issue, list, revoke and verify the API keys an MCP client holds.

The facade at ``/api/v1/mcp`` accepts a user's browser session or a provider
access token, and neither is something a machine client can obtain in a
deployment with no identity provider. This is the credential that deployment can
issue itself.

Design, including why the token is a row rather than a signature:
`docs/maintainers/mcp-client-tokens.md`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import plus_seconds_iso, utcnow_iso
from astrabox.common.utils.user_context import UserContext
from astrabox.persistence.repository.mcp_client_token_repository import (
    MCPClientTokenRepository,
)

#: What the secret looks like on the wire. The prefix is what lets the facade
#: tell an AstraBox key from a provider access token without trying both.
SECRET_PREFIX = "astrabox_mcp_"

#: Reads what the deployment holds; spends nothing.
SCOPE_READ = "read"
#: Everything `read` can do, plus starting conversations and answering them.
SCOPE_CONVERSE = "converse"
SCOPES = (SCOPE_READ, SCOPE_CONVERSE)

#: Tools a `read` key may call. Everything else needs `converse` — the split is
#: "reports" against "spends", which is the line an operator recognises.
READ_ONLY_TOOLS = frozenset({"list_agents", "get_status"})

_MAX_NAME_LENGTH = 200


def digest_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class MCPClientTokenService:
    def __init__(self, repo: MCPClientTokenRepository | None = None) -> None:
        self._repo = repo or MCPClientTokenRepository()

    # ── management, by the signed-in user ───────────────────────────────────
    async def issue(
        self,
        user: UserContext,
        *,
        name: str,
        scope: str,
        expires_in_days: int | None = None,
    ) -> dict[str, Any]:
        """Mint one key. The secret is in this answer and in no later one."""
        clean_name = str(name or "").strip()
        if not clean_name or len(clean_name) > _MAX_NAME_LENGTH:
            raise APIError(
                code="INVALID_REQUEST",
                message=f"name is required and at most {_MAX_NAME_LENGTH} characters",
                status_code=400,
            )
        if scope not in SCOPES:
            raise APIError(
                code="INVALID_REQUEST",
                message=f"scope must be one of {', '.join(SCOPES)}",
                status_code=400,
            )
        expires_at = None
        if expires_in_days is not None:
            days = int(expires_in_days)
            if days <= 0:
                raise APIError(
                    code="INVALID_REQUEST",
                    message="expires_in_days must be positive; omit it for no expiry",
                    status_code=400,
                )
            expires_at = plus_seconds_iso(days * 86_400)

        secret = SECRET_PREFIX + secrets.token_urlsafe(32)
        row = await self._repo.create(
            user_id=user.user_id,
            org_id=getattr(user, "org_id", None),
            name=clean_name,
            scope=scope,
            secret_digest=digest_secret(secret),
            expires_at=expires_at,
        )
        return {**public_view(row), "secret": secret}

    async def list(self, user: UserContext) -> list[dict[str, Any]]:
        return [public_view(row) for row in await self._repo.list_for_user(user.user_id)]

    async def revoke(self, user: UserContext, token_id: str) -> None:
        deleted = await self._repo.delete(user_id=user.user_id, token_id=str(token_id or ""))
        if not deleted:
            # Not 403: answering "forbidden" for someone else's id confirms it
            # exists. A caller may only learn about its own keys.
            raise APIError(
                code="MCP_TOKEN_NOT_FOUND",
                message="no such MCP token",
                status_code=404,
            )

    # ── verification, on every facade call ──────────────────────────────────
    async def resolve(self, secret: str) -> tuple[UserContext, str]:
        """Return the identity and scope a secret carries, or refuse.

        Revoked, malformed and never-issued all answer `UNAUTHORIZED`: they give
        a caller the same instruction, and telling them apart tells whoever is
        probing which one they hold. Expiry keeps its own code because its
        instruction differs — acquire a new key and reconnect.
        """
        presented = str(secret or "").strip()
        row = await self._repo.find_by_digest(digest_secret(presented))
        # Compared by digest under a constant-time equality even though the
        # lookup already matched: the stored digest is the authority, and a
        # timing-visible confirmation step is not worth the shortcut.
        if not isinstance(row, dict) or not hmac.compare_digest(
            str(row.get("secret_digest") or ""), digest_secret(presented)
        ):
            raise APIError(
                code="UNAUTHORIZED",
                message="MCP token is not valid",
                status_code=401,
            )
        expires_at = str(row.get("expires_at") or "")
        if expires_at and expires_at <= utcnow_iso():
            raise APIError(
                code="TOKEN_EXPIRED",
                message="MCP token expired; issue a new one and reconnect",
                status_code=401,
            )
        await self._repo.touch(str(row.get("token_id") or ""))
        user = UserContext(
            user_id=str(row.get("user_id") or ""),
            org_id=str(row.get("org_id") or "") or None,
        )
        scope = str(row.get("scope") or SCOPE_READ)
        return user, scope


def public_view(row: dict[str, Any]) -> dict[str, Any]:
    """Every field but the digest — a key is never readable after issuance."""
    return {
        "token_id": str(row.get("token_id") or ""),
        "name": str(row.get("name") or ""),
        "scope": str(row.get("scope") or ""),
        "expires_at": row.get("expires_at"),
        "created_at": row.get("created_at"),
        "last_used_at": row.get("last_used_at"),
    }


def require_scope_for_tool(scope: str | None, tool_name: str) -> None:
    """Refuse a tool the key's scope does not cover.

    ``scope`` is None for a caller who authenticated as themselves through the
    browser session or the identity provider — that is the full identity, not a
    narrowed one, so nothing is withheld from it.
    """
    if scope is None or scope == SCOPE_CONVERSE:
        return
    if scope == SCOPE_READ and tool_name in READ_ONLY_TOOLS:
        return
    raise APIError(
        code="MCP_TOKEN_SCOPE_INSUFFICIENT",
        message=(
            f"this MCP token has scope '{scope}'; '{tool_name}' needs "
            f"'{SCOPE_CONVERSE}'"
        ),
        status_code=403,
    )
