"""The background recovery read keeps one Agent SessionStore scope exact."""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.persistence.repository.transcript_entry_repository import (
    TranscriptEntryRepository,
)


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from astrabox.config.settings import get_settings

    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_background_recovery_reads_exact_main_and_child_scopes() -> None:
    repo = TranscriptEntryRepository()
    main = [{"type": "user", "uuid": "main", "text": "notification"}]
    wanted = [{"type": "assistant", "uuid": "wanted", "text": "recovered"}]
    await repo.append_entries(
        "project",
        "sdk-session",
        None,
        main,
        platform_session_id="platform-session",
    )
    await repo.append_entries(
        "project",
        "sdk-session",
        "subagents/agent-task-aa",
        wanted,
        platform_session_id="platform-session",
    )
    await repo.append_entries(
        "project",
        "sdk-session",
        "subagents/agent-task-other",
        [{"type": "assistant", "uuid": "other", "text": "wrong child"}],
        platform_session_id="platform-session",
    )

    entries = await repo.load_subpath_entries_by_platform_session(
        "platform-session",
        subpath="subagents/agent-task-aa",
    )
    main_entries = await repo.load_subpath_entries_by_platform_session(
        "platform-session",
        subpath=None,
    )

    assert entries == wanted
    assert main_entries == main
