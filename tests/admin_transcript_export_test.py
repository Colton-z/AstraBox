"""An operator's transcript export answers to the Agent's management gate before it
reads any transcript.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.admin_service import AdminService

PLATFORM_ID = "plat-1111"
SDK_ID = "1eca6b76-655d-49c5-b8e8-90d39873f437"
PROJECT_KEY = "-home-agent-workspace"

MAIN_ENTRIES = [
    {"type": "user", "uuid": "u-1", "message": {"role": "user", "content": "hi"}},
    {"type": "assistant", "uuid": "a-1", "message": {"role": "assistant", "content": "hello"}},
    # A shape nothing in this repo models: it must survive untouched.
    {"type": "x-unknown-kind", "uuid": "z-1", "nested": {"deep": [1, {"k": None}]}},
]


class _TranscriptRepo:
    def __init__(self, scopes: list[dict[str, Any]], entries: dict[str | None, list[dict]]):
        self._scopes = scopes
        self._entries = entries
        self.loaded: list[tuple[str, str, str | None]] = []

    async def list_scopes_by_platform_session(self, platform_session_id: str):
        return list(self._scopes) if platform_session_id == PLATFORM_ID else []

    async def load_entries(self, project_key, session_id, subpath, *, platform_session_id=None):
        self.loaded.append((project_key, session_id, subpath))
        return list(self._entries.get(subpath) or [])


class _SessionsRepo:
    def __init__(self, session: dict[str, Any] | None, rows: list[dict[str, Any]] | None = None):
        self._session = session
        self._rows = rows or []
        self.queries: list[dict[str, Any]] = []

    async def get_session(self, session_id: str):
        return self._session if self._session and session_id == PLATFORM_ID else None

    async def list_all_sessions(
        self, limit=200, *, skip=0, agent_ids=None, agent_id=None, since=None, until=None
    ):
        self.queries.append(
            {"limit": limit, "skip": skip, "agent_ids": agent_ids,
             "agent_id": agent_id, "since": since, "until": until}
        )
        rows = self._rows
        if agent_id:
            rows = [r for r in rows if r.get("agent_id") == agent_id]
        return rows[skip : skip + limit]


def _service(*, scopes, entries, session=None, can_manage=True) -> AdminService:
    svc = AdminService.__new__(AdminService)
    svc._sessions_repo = _SessionsRepo(  # type: ignore[attr-defined]
        session if session is not None else {"session_id": PLATFORM_ID, "agent_id": "ag-1"}
    )
    svc._transcript_entries_repo = _TranscriptRepo(scopes, entries)  # type: ignore[attr-defined]

    async def _assert(user: Any, row: Any) -> None:
        if not can_manage:
            raise APIError(code="FORBIDDEN", message="nope", status_code=403)

    svc._assert_can_manage_session = _assert  # type: ignore[assignment]
    return svc


MAIN_SCOPE = {"project_key": PROJECT_KEY, "session_id": SDK_ID, "subpath": None}


async def test_the_agent_gate_is_enforced_before_any_transcript_is_read() -> None:
    svc = _service(scopes=[MAIN_SCOPE], entries={None: MAIN_ENTRIES}, can_manage=False)

    with pytest.raises(APIError) as excinfo:
        await svc.admin_session_transcript_files(SimpleNamespace(user_id="u"), PLATFORM_ID)

    assert excinfo.value.status_code == 403
    assert svc._transcript_entries_repo.loaded == []  # type: ignore[attr-defined]
