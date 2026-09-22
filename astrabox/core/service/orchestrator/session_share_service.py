"""Read-only share links for a session.

Share-token issuance/reuse, enabled/expiry validation, and a non-authoritative
UserContext placeholder for the token-is-the-capability read path. Every
session_kernel call this service makes goes through the kernel's public
get_session/get_messages API — no private reach-ins.
"""

from __future__ import annotations

import uuid
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import parse_iso, plus_seconds_iso, utcnow, utcnow_iso
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.session_public_projection import (
    project_public_message_page,
    project_shared_session,
)


class SessionShareService:
    def __init__(
        self,
        *,
        sessions_repo: Any,
        session_service: Any,
        session_kernel: Any,
        session_file_service: Any,
    ) -> None:
        self._sessions_repo = sessions_repo
        self._session_service = session_service
        self._session_kernel = session_kernel
        self._session_file_service = session_file_service

    @staticmethod
    def _share_public_view(session: dict[str, Any]) -> dict[str, Any]:
        """The share-config subset safe to return to the session owner."""
        share = session.get("share") if isinstance(session.get("share"), dict) else {}
        return {
            "enabled": bool(share.get("enabled")),
            "token": str(share.get("token") or ""),
            "expires_at": share.get("expires_at"),
            "allow_download": bool(share.get("allow_download")),
            "created_at": share.get("created_at"),
        }

    async def create_session_share(
        self,
        user: UserContext,
        session_id: str,
        *,
        expires_in_seconds: int | None = None,
        allow_download: bool = False,
    ) -> dict[str, Any]:
        """Owner-only: enable a read-only share link for a session.

        Reuses an existing token if one is already present (so re-sharing
        doesn't invalidate links already handed out); only (re)sets expiry /
        download flag. The token is the access grant for the public share
        endpoints, which verify it without requiring a viewer login.
        """
        session = await self._session_service.must_get_owned_session(user, session_id)
        existing = session.get("share") if isinstance(session.get("share"), dict) else {}
        token = str(existing.get("token") or "") or uuid.uuid4().hex + uuid.uuid4().hex
        share = {
            "enabled": True,
            "token": token,
            "created_by": user.user_id,
            "created_at": existing.get("created_at") or utcnow_iso(),
            "updated_at": utcnow_iso(),
            "allow_download": bool(allow_download),
            "expires_at": plus_seconds_iso(expires_in_seconds) if expires_in_seconds else None,
        }
        await self._sessions_repo.update_session(session_id, {"share": share})
        return {
            "enabled": True,
            "token": token,
            "expires_at": share["expires_at"],
            "allow_download": share["allow_download"],
            "created_at": share["created_at"],
        }

    async def revoke_session_share(self, user: UserContext, session_id: str) -> dict[str, Any]:
        session = await self._session_service.must_get_owned_session(user, session_id)
        share = session.get("share") if isinstance(session.get("share"), dict) else {}
        share = {**share, "enabled": False, "updated_at": utcnow_iso()}
        await self._sessions_repo.update_session(session_id, {"share": share})
        return {"enabled": False}

    async def get_session_share(self, user: UserContext, session_id: str) -> dict[str, Any]:
        session = await self._session_service.must_get_owned_session(user, session_id)
        return self._share_public_view(session)

    async def _resolve_shared_session(self, token: str) -> dict[str, Any]:
        """Resolve a session by share token; enforce enabled + not-expired.

        Identity-independent: the token is the capability. Disabled, expired,
        and unknown links all return the same generic 404 response.
        """
        tok = str(token or "").strip()
        if not tok:
            raise APIError(code="SHARE_NOT_FOUND", message="invalid share link", status_code=404)
        session = await self._sessions_repo.find_session_by_share_token(tok)
        share = session.get("share") if session and isinstance(session.get("share"), dict) else None
        if not session or not share or not share.get("enabled"):
            raise APIError(code="SHARE_NOT_FOUND", message="share link not found or revoked", status_code=404)
        expires_at = str(share.get("expires_at") or "").strip()
        if expires_at:
            try:
                expired = utcnow() > parse_iso(expires_at)
            except Exception:
                # Fail closed: an unparseable expiry is not evidence the link
                # is still valid — deny rather than silently granting access.
                expired = True
            if expired:
                raise APIError(code="SHARE_EXPIRED", message="share link has expired", status_code=404)
        return session

    @staticmethod
    def _shared_viewer_placeholder(session: dict[str, Any]) -> UserContext:
        """A non-authoritative UserContext for the prefetched read path.

        The share read path is identity-independent: the token is the capability.
        The kernel's get_session/get_messages only consult ``user`` in their
        non-prefetched ``else`` branch (``must_get_owned_session``);
        because ``get_shared_session`` always passes ``session=`` here, this
        value is never read. The placeholder still carries the session's own
        owner so the signature is satisfied without inventing a fake viewer.
        """
        owner = str(session.get("user_id") or "")
        return UserContext(user_id=owner)

    async def get_shared_session(self, token: str) -> dict[str, Any]:
        session = await self._resolve_shared_session(token)
        data = await self._session_kernel.get_session(
            self._shared_viewer_placeholder(session),
            str(session.get("session_id") or ""),
            session=session,
        )
        share = session.get("share") or {}
        return project_shared_session(
            data,
            allow_download=bool(share.get("allow_download")),
        )

    async def get_shared_messages(
        self, token: str, *, before: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        session = await self._resolve_shared_session(token)
        page = await self._session_kernel.get_messages(
            self._shared_viewer_placeholder(session),
            str(session.get("session_id") or ""),
            before=before,
            limit=limit,
            session=session,
        )
        return project_public_message_page(page, shared=True)

    async def list_shared_files(self, token: str, *, path: str | None = None) -> dict[str, Any]:
        session = await self._resolve_shared_session(token)
        if not bool((session.get("share") or {}).get("allow_download")):
            raise APIError(code="SHARE_DOWNLOAD_DISABLED", message="file access is not allowed for this share", status_code=403)
        return await self._session_file_service.list_entries_for_session(session, path=path)

    async def get_shared_history_blocks(
        self, token: str, *, before: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        session = await self._resolve_shared_session(token)
        page = await self._session_kernel.get_history_blocks(
            self._shared_viewer_placeholder(session),
            str(session.get("session_id") or ""),
            before=before, limit=limit, session=session,
        )
        return project_public_message_page(page, shared=True)

    async def get_shared_history_block_details(
        self, token: str, block_id: str, *, cursor: str
    ) -> dict[str, Any]:
        session = await self._resolve_shared_session(token)
        page = await self._session_kernel.get_history_block_details(
            self._shared_viewer_placeholder(session),
            str(session.get("session_id") or ""),
            block_id=block_id, cursor=cursor, session=session,
        )
        return {
            "messages": project_public_message_page(page, shared=True)["messages"],
            "has_more": bool(page.get("has_more")),
        }

    async def download_shared_file(self, token: str, *, path: str) -> tuple[bytes, str]:
        session = await self._resolve_shared_session(token)
        if not bool((session.get("share") or {}).get("allow_download")):
            raise APIError(code="SHARE_DOWNLOAD_DISABLED", message="file download is not allowed for this share", status_code=403)
        return await self._session_file_service.download_file_for_session(session, path=path)
