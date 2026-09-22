"""Transcript access is fenced by the token-authorized platform session.

The capability token authorizes the URL ``session_id`` while transcript project,
SDK session, and subpath keys come from attacker-controlled request data. Every
tenant sandbox holds a valid token for its own session, so body keys alone
cannot define repository scope. Load, list, append idempotency, and subkey
enumeration must include ``platform_session_id`` so a valid foreign token cannot
read, enumerate, inject into, or pre-empt another tenant's transcript.
"""

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


async def _seed_victim(repo: TranscriptEntryRepository) -> None:
    # The victim's sandbox (platform session "victim-sess") writes its
    # transcript under its own SDK project key.
    await repo.append_entries(
        "victim-project", "victim-sdk-sess", None,
        [{"type": "user", "text": "victim secret"}],
        platform_session_id="victim-sess",
    )


async def test_foreign_project_key_with_own_fence_reads_nothing() -> None:
    repo = TranscriptEntryRepository()
    await _seed_victim(repo)

    # Attacker holds a valid token for "attacker-sess". It puts the victim's
    # project_key/session_id in the body — but the load is fenced to the
    # attacker's platform session, so it sees nothing.
    stolen = await repo.load_entries(
        "victim-project", "victim-sdk-sess", None,
        platform_session_id="attacker-sess",
    )
    assert stolen is None, "cross-tenant read must return nothing under the fence"


async def test_own_data_still_loads_under_the_fence() -> None:
    repo = TranscriptEntryRepository()
    await _seed_victim(repo)

    # The victim's own sandbox (fenced to victim-sess) still reads its data —
    # resume is unbroken.
    own = await repo.load_entries(
        "victim-project", "victim-sdk-sess", None,
        platform_session_id="victim-sess",
    )
    assert own == [{"type": "user", "text": "victim secret"}]


async def test_list_sessions_cannot_enumerate_across_tenants() -> None:
    repo = TranscriptEntryRepository()
    await _seed_victim(repo)

    # Attacker enumerates the victim's project_key, fenced to its own session.
    stolen = await repo.list_sessions(
        "victim-project", platform_session_id="attacker-sess"
    )
    assert stolen == [], "cross-tenant enumeration must be empty under the fence"

    # The victim enumerates its own (fenced to victim-sess) and sees its session.
    own = await repo.list_sessions("victim-project", platform_session_id="victim-sess")
    assert any(s.get("session_id") == "victim-sdk-sess" for s in own)


async def test_list_subkeys_cannot_enumerate_across_tenants() -> None:
    repo = TranscriptEntryRepository()
    await repo.append_entries(
        "victim-project",
        "victim-sdk-sess",
        "subagents/agent-victim",
        [{"type": "assistant", "text": "victim child transcript"}],
        platform_session_id="victim-sess",
    )

    stolen = await repo.list_subkeys(
        "victim-project",
        "victim-sdk-sess",
        platform_session_id="attacker-sess",
    )
    assert stolen == [], "a valid foreign capability must not enumerate child runs"

    own = await repo.list_subkeys(
        "victim-project",
        "victim-sdk-sess",
        platform_session_id="victim-sess",
    )
    assert own == ["subagents/agent-victim"]


async def test_append_pollution_is_contained_by_the_read_fence() -> None:
    repo = TranscriptEntryRepository()
    await _seed_victim(repo)

    # Attacker (platform session "attacker-sess") appends under the victim's
    # project_key — the entry is tagged with the ATTACKER's platform session
    # (the append path stamps the token-authorized path session), so the
    # victim's fenced read never surfaces it.
    await repo.append_entries(
        "victim-project", "victim-sdk-sess", None,
        [{"type": "user", "text": "attacker injected"}],
        platform_session_id="attacker-sess",
    )
    victim_view = await repo.load_entries(
        "victim-project", "victim-sdk-sess", None,
        platform_session_id="victim-sess",
    )
    assert victim_view == [{"type": "user", "text": "victim secret"}], (
        "attacker's injected entry must not appear in the victim's fenced view"
    )


async def test_cross_tenant_uuid_preemption_does_not_drop_victim_entries() -> None:
    """A tenant's sequence space includes platform_session_id, because scoping on
    just (project_key, session_id, subpath) — all attacker-echoable — would let
    an attacker PRE-INSERT a row carrying the victim's scope, silently dropping
    the victim's own append of that entry as one already stored (cross-tenant
    write-denial). The read is fenced the same way."""
    repo = TranscriptEntryRepository()

    # Attacker pre-empts: victim's body scope, victim's (predictable) uuid,
    # but the append is stamped with the ATTACKER's token-authorized session.
    await repo.append_entries(
        "victim-project", "victim-sdk-sess", None,
        [{"type": "user", "uuid": "uuid-1", "text": "attacker pre-emption"}],
        platform_session_id="attacker-sess",
    )

    # The victim's legitimate append of the SAME uuid must still land.
    await repo.append_entries(
        "victim-project", "victim-sdk-sess", None,
        [{"type": "user", "uuid": "uuid-1", "text": "victim real entry"}],
        platform_session_id="victim-sess",
    )

    victim_view = await repo.load_entries(
        "victim-project", "victim-sdk-sess", None,
        platform_session_id="victim-sess",
    )
    assert victim_view == [
        {"type": "user", "uuid": "uuid-1", "text": "victim real entry"}
    ], "the victim's append must not be pre-empted by a foreign-tenant uuid row"


async def test_same_tenant_retry_of_one_batch_stores_it_once() -> None:
    """A retry inside one tenant is still a retry: the batch lands exactly once.

    The fence narrows a batch's identity to one tenant's scope, and must not
    cost the idempotency that identity exists for: a sender retries a batch
    until the platform confirms it, so the store has to recognise the batch it
    already holds.
    """
    repo = TranscriptEntryRepository()
    entry = {"type": "user", "uuid": "uuid-9", "text": "once"}
    sequences = [
        await repo.append_entries(
            "proj", "sdk-sess", None, [entry],
            append_id="retried-batch", platform_session_id="sess-A",
        )
        for _ in range(3)  # original + two retries
    ]
    rows = await repo.load_entries(
        "proj", "sdk-sess", None, platform_session_id="sess-A"
    )
    assert rows == [entry], "a retried batch must resolve to one row"
    assert sequences == [1, 1, 1], "and must not advance the tenant's position"
