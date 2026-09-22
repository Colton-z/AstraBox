"""SessionStore conformance — the storage contract the translation shell needs.

Runs the Claude Agent SDK's own conformance suite (14 behavioral contracts:
append/load round-trip, ordering, key isolation, listing, cascade delete,
subkeys) against :class:`TranscriptSessionStore`, the host-side protocol view
of the durable transcript repository. Resume/fork/subagent restoration all go
through this contract, so a drift here is a broken resume on a real box, not
a style issue.

``list_session_summaries`` is not implemented by the adapter (no summary
sidecar in the repository); the suite skips that contract automatically and
the SDK falls back to ``list_sessions`` + ``load``.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from claude_agent_sdk.testing import run_session_store_conformance

from astrabox.persistence.repository.transcript_entry_repository import (
    TranscriptEntryRepository,
)
from astrabox.persistence.transcript_session_store import TranscriptSessionStore


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from astrabox.config.settings import get_settings

    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_transcript_session_store_conformance() -> None:
    # The suite calls the factory once per contract and requires each store to
    # start empty; a distinct collection per call provides that isolation.
    counter = itertools.count()

    def make_store() -> TranscriptSessionStore:
        repo = TranscriptEntryRepository(
            collection_name=f"session_store_conformance_{next(counter)}"
        )
        return TranscriptSessionStore(repo)

    await run_session_store_conformance(make_store)
