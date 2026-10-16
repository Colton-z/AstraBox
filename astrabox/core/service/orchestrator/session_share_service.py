"""Read-only share links for a session.

The owner enables, reads back and revokes the link (``session_shares``). A link
recipient reads the conversation with the token alone, through the same reads
the owner's page uses (:class:`~astrabox.core.service.orchestrator.session_runtime.views.SessionReads`),
without what only the owner may see. Disabled, expired and unknown links answer
alike with 404.
"""

from __future__ import annotations

import uuid
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import parse_iso, plus_seconds_iso, utcnow
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.session_file_service import FileDownload
from astrabox.core.service.orchestrator.session_runtime.views import SessionReads
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_share_repository import SessionShareRepository


def _link_view(share: dict[str, Any] | None) -> dict[str, Any]:
    """The link's configuration as its owner reads it back."""
    share = share or {}
    return {
        "enabled": bool(share.get("enabled")),
        "token": str(share.get("token") or ""),
        "expires_at": share.get("expires_at"),
        "allow_download": bool(share.get("allow_download")),
        "created_at": share.get("created_at"),
    }


class SessionShareService:
    def __init__(
        self,
        *,
        sessions: SessionRepository,
        shares: SessionShareRepository,
        reads: SessionReads,
        session_file_service: Any,
    ) -> None:
        self._sessions = sessions
        self._shares = shares
        self._reads = reads
        self._files = session_file_service

    async def _owned(self, user: UserContext, session_id: str) -> None:
        if await self._sessions.get_owned_session(session_id, str(user.user_id)) is None:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)

    async def create_session_share(
        self,
        user: UserContext,
        session_id: str,
        *,
        expires_in_seconds: int | None = None,
        allow_download: bool = False,
    ) -> dict[str, Any]:
        """Enable the session's link; a session shared before keeps its token."""
        await self._owned(user, session_id)
        share = await self._shares.share(
            session_id,
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            created_by=str(user.user_id),
            allow_download=allow_download,
            expires_at=plus_seconds_iso(expires_in_seconds) if expires_in_seconds else None,
        )
        return _link_view(share)

    async def revoke_session_share(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self._owned(user, session_id)
        await self._shares.revoke(session_id)
        return {"enabled": False}

    async def get_session_share(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self._owned(user, session_id)
        return _link_view(await self._shares.get(session_id))

    async def _resolve(self, token: str) -> dict[str, Any]:
        """The enabled, unexpired share ``token`` names; the token is the whole capability."""
        value = str(token or "").strip()
        share = await self._shares.resolve(value) if value else None
        if share is None or not share.get("enabled"):
            raise APIError(code="SHARE_NOT_FOUND", message="share link not found or revoked", status_code=404)
        expires_at = share.get("expires_at")
        if expires_at and parse_iso(str(expires_at)) <= utcnow():
            raise APIError(code="SHARE_EXPIRED", message="share link has expired", status_code=404)
        return share

    async def get_shared_session(self, token: str) -> dict[str, Any]:
        share = await self._resolve(token)
        return await self._reads.shared_session(
            str(share["session_id"]), allow_download=bool(share.get("allow_download"))
        )

    async def get_shared_messages(self, token: str, *, before: str | None = None, limit: int = 20) -> dict[str, Any]:
        share = await self._resolve(token)
        return await self._reads.shared_messages(str(share["session_id"]), before=before, limit=limit)

    async def get_shared_history_blocks(
        self, token: str, *, before: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        share = await self._resolve(token)
        return await self._reads.shared_history_blocks(str(share["session_id"]), before=before, limit=limit)

    async def get_shared_history_block_details(self, token: str, block_id: str, *, cursor: str) -> dict[str, Any]:
        share = await self._resolve(token)
        return await self._reads.shared_history_block_details(str(share["session_id"]), block_id, cursor=cursor)

    async def _shared_files_session(self, token: str) -> dict[str, Any]:
        share = await self._resolve(token)
        if not share.get("allow_download"):
            raise APIError(
                code="SHARE_DOWNLOAD_DISABLED",
                message="file access is not allowed for this share",
                status_code=403,
            )
        session = await self._sessions.get_session(str(share["session_id"]))
        if session is None:
            raise APIError(code="SHARE_NOT_FOUND", message="share link not found or revoked", status_code=404)
        return session

    async def list_shared_files(self, token: str, *, path: str | None = None) -> dict[str, Any]:
        session = await self._shared_files_session(token)
        return await self._files.list_entries_for_session(session, path=path)

    async def download_shared_file(self, token: str, *, path: str) -> FileDownload:
        session = await self._shared_files_session(token)
        return await self._files.download_file_for_session(session, path=path)
