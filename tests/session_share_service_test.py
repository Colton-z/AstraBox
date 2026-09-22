"""``SessionShareService._resolve_shared_session`` — share-token validation.

A share link with a CORRUPTED ``expires_at`` (an unparseable ISO string) must
be treated as expired — fail CLOSED, deny access — rather than as "no evidence
of expiry, so allow". Failing open here is what a bare ``except Exception:
pass`` wrapped around both the parse/compare AND the intentional
``raise APIError(...)``, so a ``ValueError`` from ``parse_iso`` on a
malformed timestamp was silently swallowed and execution fell through as if
the link were still valid).

`asyncio_mode = "auto"` (see pyproject) runs these bare ``async def test_*``
coroutines directly — no decorator needed.
"""

from __future__ import annotations

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import plus_seconds_iso
from astrabox.core.service.orchestrator.session_share_service import (
    SessionShareService,
)


class _FakeSessionsRepo:
    def __init__(self, session: dict | None) -> None:
        self._session = session

    async def find_session_by_share_token(self, token: str) -> dict | None:
        return self._session


def _make_service(session: dict | None) -> SessionShareService:
    return SessionShareService(
        sessions_repo=_FakeSessionsRepo(session),
        session_service=None,
        session_kernel=None,
        session_file_service=None,
    )


def _session_with_share(**share_overrides: object) -> dict:
    share = {"enabled": True, "token": "tok", "allow_download": False}
    share.update(share_overrides)
    return {"session_id": "s1", "user_id": "owner", "share": share}


async def test_resolve_shared_session_allows_a_non_expired_link() -> None:
    session = _session_with_share(expires_at=plus_seconds_iso(3600))
    service = _make_service(session)

    resolved = await service._resolve_shared_session("tok")

    assert resolved is session


async def test_resolve_shared_session_allows_a_link_with_no_expiry() -> None:
    session = _session_with_share(expires_at=None)
    service = _make_service(session)

    resolved = await service._resolve_shared_session("tok")

    assert resolved is session


async def test_resolve_shared_session_denies_a_genuinely_expired_link() -> None:
    session = _session_with_share(expires_at=plus_seconds_iso(-3600))
    service = _make_service(session)

    with pytest.raises(APIError) as exc_info:
        await service._resolve_shared_session("tok")
    assert exc_info.value.code == "SHARE_EXPIRED"
    assert exc_info.value.status_code == 404


async def test_resolve_shared_session_fails_closed_on_a_corrupt_expiry() -> None:
    # Swallowing the parse failure and treating the link as "not expired"
    # hands out access on corrupt data. It is denied, same as a genuinely
    # expired link.
    session = _session_with_share(expires_at="not-a-real-timestamp")
    service = _make_service(session)

    with pytest.raises(APIError) as exc_info:
        await service._resolve_shared_session("tok")
    assert exc_info.value.code == "SHARE_EXPIRED"
    assert exc_info.value.status_code == 404


async def test_resolve_shared_session_rejects_missing_or_disabled_share() -> None:
    service = _make_service(None)
    with pytest.raises(APIError) as exc_info:
        await service._resolve_shared_session("tok")
    assert exc_info.value.code == "SHARE_NOT_FOUND"

    disabled_session = _session_with_share(enabled=False)
    service2 = _make_service(disabled_session)
    with pytest.raises(APIError) as exc_info2:
        await service2._resolve_shared_session("tok")
    assert exc_info2.value.code == "SHARE_NOT_FOUND"


async def test_resolve_shared_session_rejects_blank_token() -> None:
    service = _make_service(_session_with_share())
    with pytest.raises(APIError) as exc_info:
        await service._resolve_shared_session("   ")
    assert exc_info.value.code == "SHARE_NOT_FOUND"
