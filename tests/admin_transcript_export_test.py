"""The transcript export: what an operator downloads has to resume in Claude Code.

Every assertion here is about that one outcome. The SDK's contract is that a
``SessionStoreEntry`` is one JSONL line, opaque, in append order, and that a
transcript restored under ``~/.claude/projects/`` resumes by SDK session id. So
the export is wrong if it reshapes an entry, reorders them, names the file after
the platform's id, or folds a subagent's separate transcript into the main one —
and each of those is a case below.
"""

from __future__ import annotations

import json
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
SUB_ENTRIES = [{"type": "assistant", "uuid": "s-1", "message": {"content": "sub work"}}]


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
        self, limit=200, *, skip=0, template_names=None, agent_id=None, since=None, until=None
    ):
        self.queries.append(
            {"limit": limit, "skip": skip, "template_names": template_names,
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
SUB_SCOPE = {"project_key": PROJECT_KEY, "session_id": SDK_ID, "subpath": "subagents/agent-7"}


async def test_entries_are_one_jsonl_line_each_in_append_order() -> None:
    svc = _service(scopes=[MAIN_SCOPE], entries={None: MAIN_ENTRIES})

    files = await svc.admin_session_transcript_files(SimpleNamespace(user_id="u"), PLATFORM_ID)

    assert len(files) == 1
    lines = files[0]["jsonl"].decode("utf-8").splitlines()
    assert len(lines) == len(MAIN_ENTRIES)
    assert [json.loads(line) for line in lines] == MAIN_ENTRIES


async def test_an_entry_is_never_reshaped() -> None:
    """The store contract is deep-equal round-trip, so nothing may be added.

    A summary field, a turn stamp, an envelope — any of them makes the line
    something the SDK did not write, and the file stops being the SDK's.
    """
    svc = _service(scopes=[MAIN_SCOPE], entries={None: MAIN_ENTRIES})

    files = await svc.admin_session_transcript_files(SimpleNamespace(user_id="u"), PLATFORM_ID)

    for line, original in zip(files[0]["jsonl"].decode().splitlines(), MAIN_ENTRIES):
        assert json.loads(line).keys() == original.keys()


async def test_the_file_is_named_for_the_sdk_session_not_the_platform_one() -> None:
    """`--resume` takes the SDK's id; a file named for the platform id is one Claude Code cannot find."""
    svc = _service(scopes=[MAIN_SCOPE], entries={None: MAIN_ENTRIES})

    files = await svc.admin_session_transcript_files(SimpleNamespace(user_id="u"), PLATFORM_ID)

    assert files[0]["sdk_session_id"] == SDK_ID
    assert SDK_ID in files[0]["path"]
    assert PLATFORM_ID not in files[0]["path"]


async def test_a_subagent_transcript_is_its_own_file_at_the_sdk_path() -> None:
    """Separate SessionStore keys are separate files on disk, and stay separate.

    Folding them into one list — which the recovery read does, by seq — puts a
    subagent's lines inside the main conversation, and the main transcript stops
    being loadable.
    """
    svc = _service(
        scopes=[MAIN_SCOPE, SUB_SCOPE],
        entries={None: MAIN_ENTRIES, "subagents/agent-7": SUB_ENTRIES},
    )

    files = await svc.admin_session_transcript_files(SimpleNamespace(user_id="u"), PLATFORM_ID)

    assert sorted(f["path"] for f in files) == sorted(
        [f"{PROJECT_KEY}/{SDK_ID}.jsonl", f"{PROJECT_KEY}/subagents/agent-7.jsonl"]
    )
    main = next(f for f in files if f["subpath"] is None)
    assert b"sub work" not in main["jsonl"]


async def test_a_session_with_no_mirrored_transcript_yields_no_files() -> None:
    """Not an empty file. A sandbox that never started wrote nothing, and a
    zero-byte `.jsonl` in an archive reads as a session that did."""
    svc = _service(scopes=[], entries={})

    files = await svc.admin_session_transcript_files(SimpleNamespace(user_id="u"), PLATFORM_ID)

    assert files == []


async def test_an_unknown_session_is_404_not_an_empty_export() -> None:
    svc = _service(scopes=[MAIN_SCOPE], entries={None: MAIN_ENTRIES}, session={})
    svc._sessions_repo = _SessionsRepo(None)  # type: ignore[attr-defined]

    with pytest.raises(APIError) as excinfo:
        await svc.admin_session_transcript_files(SimpleNamespace(user_id="u"), PLATFORM_ID)

    assert excinfo.value.status_code == 404


async def test_the_agent_gate_is_enforced_before_any_transcript_is_read() -> None:
    svc = _service(scopes=[MAIN_SCOPE], entries={None: MAIN_ENTRIES}, can_manage=False)

    with pytest.raises(APIError) as excinfo:
        await svc.admin_session_transcript_files(SimpleNamespace(user_id="u"), PLATFORM_ID)

    assert excinfo.value.status_code == 403
    assert svc._transcript_entries_repo.loaded == []  # type: ignore[attr-defined]


async def test_batch_walks_pages_and_skips_what_has_no_transcript() -> None:
    """A generator over a PAGED walk, so neither the rows nor the archive is resident.

    The shape this replaces read one capped list and built the whole tar in
    memory. At a hundred agents and a thousand conversations a day, a cap is not
    a detail: an operator asking for one agent's day would have got its first
    five hundred rows and no sign of the rest.
    """
    svc = _service(scopes=[MAIN_SCOPE], entries={None: MAIN_ENTRIES})
    svc._sessions_repo = _SessionsRepo(  # type: ignore[attr-defined]
        {"session_id": PLATFORM_ID, "agent_id": "ag-1"},
        rows=[
            {"session_id": PLATFORM_ID, "agent_id": "ag-1"},
            {"session_id": "plat-gone", "agent_id": "ag-1"},
            {"session_id": "", "agent_id": "ag-1"},
        ],
    )

    async def _manageable(user: Any):
        return {"Claude Code"}

    svc._manageable_template_names = _manageable  # type: ignore[assignment]

    groups = [g async for g in svc.admin_iter_batch_transcript_files(SimpleNamespace(user_id="u"))]

    # plat-gone 404s on its per-session read; the "" row is unusable. Neither
    # may end the archive.
    assert [g["session_id"] for g in groups] == [PLATFORM_ID]


async def test_batch_pushes_the_filters_into_the_query() -> None:
    """The narrowing is a query term, not a pass over the result.

    Filtering one fetched page could produce a partial export, so the repository
    applies the filters before pagination.
    """
    svc = _service(scopes=[MAIN_SCOPE], entries={None: MAIN_ENTRIES})
    repo = _SessionsRepo(
        {"session_id": PLATFORM_ID, "agent_id": "ag-1"},
        rows=[{"session_id": PLATFORM_ID, "agent_id": "ag-1"}],
    )
    svc._sessions_repo = repo  # type: ignore[attr-defined]

    async def _manageable(user: Any):
        return {"Claude Code"}

    svc._manageable_template_names = _manageable  # type: ignore[assignment]

    _ = [
        g
        async for g in svc.admin_iter_batch_transcript_files(
            SimpleNamespace(user_id="u"),
            agent_id="ag-1",
            since="2026-08-01T00:00:00+00:00",
            until="2026-08-07T23:59:59+00:00",
        )
    ]

    q = repo.queries[0]
    assert q["agent_id"] == "ag-1"
    assert q["since"] == "2026-08-01T00:00:00+00:00"
    assert q["until"] == "2026-08-07T23:59:59+00:00"
    # The owner scope rides in the same query, never as a later comprehension.
    assert q["template_names"] == ["Claude Code"]
