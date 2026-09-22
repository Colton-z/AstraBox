"""The transcript store-sequence contract, across the seam that carries it.

``store_sequence`` is the durable transcript's position, and recovery reads it
back out of every mirrored entry as ``__astrabox_mirror_seq``. What makes it
trustworthy is that a re-delivered batch does not move it: the spooled sender in
the box retries a batch until the platform confirms it, so a re-delivery is
ordinary operation rather than an edge case, and a position that counted the
same entries twice would be a position no reader could use.

These cases run the whole path — the batch the in-box sender builds, the route
that receives it, the repository that assigns the numbers, and the recovery read
that hands them back — because each half is convincing alone and only the seam
between them can be wrong.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from astrabox.api.routes import transcript as transcript_routes
from astrabox.core.service.orchestrator import sandbox_runner
from astrabox.core.service.orchestrator.transcript_capability import (
    mint_transcript_capability_token,
)
from astrabox.persistence.repository.transcript_entry_repository import (
    TranscriptEntryRepository,
)


async def _empty_subkeys(_key: Any) -> list[str]:
    return []


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from astrabox.config.settings import get_settings

    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "unit-test-shared-secret")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


PROJECT = "proj"
SDK_SESSION = "sdk-sess"
PLATFORM_SESSION = "plat-sess"
KEY = {"project_key": PROJECT, "session_id": SDK_SESSION}

#: Entries carrying no ``uuid``. The SDK writes summaries, tags and mode markers
#: without one, so a batch may have nothing entry-level to recognise it by; the
#: batch's ``append_id`` is what identifies it.
NO_UUID_ENTRIES = [
    {"type": "summary", "summary": "first"},
    {"type": "summary", "summary": "second"},
]


class _Client:
    """The capability-scoped transcript API, as the in-box sender reaches it."""

    def __init__(self) -> None:
        app = FastAPI()
        transcript_routes._registered_on = None
        transcript_routes.register_transcript_routes(app)
        self._http = TestClient(app)
        token = mint_transcript_capability_token(PLATFORM_SESSION)
        self._base = f"/api/v1/sbxcap/{token}/api/v1/transcript/{PLATFORM_SESSION}"

    def post(self, operation: str, payload: dict[str, Any]) -> Any:
        return self._http.post(f"{self._base}/{operation}", json=payload)

    def append(
        self,
        entries: list[dict[str, Any]],
        *,
        append_id: str,
        key: dict[str, Any] | None = None,
        digest: str | None = None,
    ) -> Any:
        body = key if key is not None else KEY
        return self.post(
            "append",
            {
                "key": body,
                "entries": entries,
                "append_id": append_id,
                "payload_sha256": (
                    digest
                    if digest is not None
                    else transcript_routes.transcript_payload_digest(body, entries)
                ),
            },
        )


def _store_sequence(response: Any) -> int:
    assert response.status_code == 200, response.text
    return int(response.json()["data"]["store_sequence"])


# ── the guarantee: a re-delivered batch does not move the position ───────────


def test_redelivered_uuidless_batch_neither_duplicates_nor_advances() -> None:
    client = _Client()
    first = _store_sequence(client.append(NO_UUID_ENTRIES, append_id="batch-1"))
    second = _store_sequence(client.append(NO_UUID_ENTRIES, append_id="batch-1"))

    assert first == 2, "a first append of two entries puts the scope at 2"
    assert second == first, (
        "re-delivering a batch must return the position it already has; entries "
        "carrying no uuid are not exempt, because the batch is what is identified"
    )

    loaded = client.post("load", {"key": KEY}).json()["data"]
    assert loaded["entries"] == NO_UUID_ENTRIES, "each entry is stored exactly once"
    assert loaded["store_sequence"] == first, "load reports the same position"


def test_one_oversized_entry_round_trips_without_truncation() -> None:
    client = _Client()
    payload = "x" * 17_000_000
    entry = {"type": "e2e_large_entry", "payload": payload}

    stored = client.append([entry], append_id="oversized-entry")
    loaded = client.post("load", {"key": KEY})

    assert stored.status_code == 200, stored.text
    assert stored.json()["data"] == {"ok": True, "count": 1, "store_sequence": 1}
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["data"]["store_sequence"] == 1
    assert loaded.json()["data"]["entries"] == [entry]


def test_redelivered_mixed_batch_is_stored_exactly_once() -> None:
    client = _Client()
    mixed = [
        {"type": "user", "uuid": "u-1", "text": "hello"},
        {"type": "summary", "summary": "no uuid here"},
        {"type": "assistant", "uuid": "u-2", "text": "hi"},
    ]
    first = _store_sequence(client.append(mixed, append_id="batch-mixed"))
    second = _store_sequence(client.append(mixed, append_id="batch-mixed"))

    assert (first, second) == (3, 3)
    assert client.post("load", {"key": KEY}).json()["data"]["entries"] == mixed


def test_same_entries_under_a_new_append_id_append_again() -> None:
    client = _Client()
    first = _store_sequence(client.append(NO_UUID_ENTRIES, append_id="batch-1"))
    second = _store_sequence(client.append(NO_UUID_ENTRIES, append_id="batch-2"))

    assert second == first + len(NO_UUID_ENTRIES), (
        "a batch is identified by its append_id, never by its content: the SDK "
        "asking twice means it wants two"
    )
    loaded = client.post("load", {"key": KEY}).json()["data"]["entries"]
    assert loaded == NO_UUID_ENTRIES + NO_UUID_ENTRIES


def test_reused_append_id_with_different_entries_is_refused_whole() -> None:
    client = _Client()
    settled = _store_sequence(client.append(NO_UUID_ENTRIES, append_id="batch-1"))

    conflict = client.append([{"type": "summary", "summary": "different"}],
                             append_id="batch-1")
    assert conflict.status_code == 409, conflict.text

    loaded = client.post("load", {"key": KEY}).json()["data"]
    assert loaded["store_sequence"] == settled, "a refused append changes nothing"
    assert loaded["entries"] == NO_UUID_ENTRIES


def test_append_without_batch_identity_is_refused() -> None:
    client = _Client()
    missing_id = client.post("append", {"key": KEY, "entries": NO_UUID_ENTRIES})
    assert missing_id.status_code == 400, missing_id.text

    wrong_digest = client.append(NO_UUID_ENTRIES, append_id="b", digest="0" * 64)
    assert wrong_digest.status_code == 400, (
        "a body that does not match the digest it declares is not the batch the "
        "append_id promises"
    )
    assert client.post("load", {"key": KEY}).json()["data"]["entries"] is None


# ── arithmetic and boundaries ────────────────────────────────────────────────


async def test_sequences_are_one_based_and_an_empty_batch_does_not_advance() -> None:
    repo = TranscriptEntryRepository(collection_name=f"seq_{uuid.uuid4().hex[:8]}")

    assert await repo.current_sequence(PROJECT, SDK_SESSION, None) == 0, (
        "a scope nothing has written is at 0, which is a position, not an absence"
    )
    first = await repo.append_entries(
        PROJECT, SDK_SESSION, None, NO_UUID_ENTRIES, append_id="b1"
    )
    assert first == len(NO_UUID_ENTRIES), "the first append of k entries lands at k"

    unchanged = await repo.append_entries(PROJECT, SDK_SESSION, None, [], append_id="b2")
    assert unchanged == first, "an empty batch reports the position, it does not move it"


async def test_a_claim_survives_a_crash_between_claiming_and_writing() -> None:
    """The rows a claim promised are written by whichever delivery gets there.

    Claiming the range and writing the rows cannot be one atomic act, so a crash
    between them is possible and the re-delivery has to finish the job at the
    sequence the claim already fixed — not skip the rows, and not take a second
    range.
    """
    repo = TranscriptEntryRepository(collection_name=f"crash_{uuid.uuid4().hex[:8]}")
    await repo.append_entries(PROJECT, SDK_SESSION, None, [{"n": 0}], append_id="b0")

    crashed = repo._write_batch_rows

    async def _die_before_writing(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("runner died after the claim")

    repo._write_batch_rows = _die_before_writing  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="died after the claim"):
        await repo.append_entries(
            PROJECT, SDK_SESSION, None, [{"n": 1}, {"n": 2}], append_id="b1"
        )

    repo._write_batch_rows = crashed  # type: ignore[method-assign]
    resumed = await repo.append_entries(
        PROJECT, SDK_SESSION, None, [{"n": 1}, {"n": 2}], append_id="b1"
    )
    assert resumed == 3, "the re-delivery commits at the range the claim fixed"

    entries = await repo.load_entries(PROJECT, SDK_SESSION, None)
    assert entries == [{"n": 0}, {"n": 1}, {"n": 2}], (
        "no gap and no duplicate: the interrupted batch is stored exactly once"
    )


async def test_settled_claims_are_released_so_the_ledger_stays_bounded() -> None:
    repo = TranscriptEntryRepository(collection_name=f"ledger_{uuid.uuid4().hex[:8]}")
    for index in range(5):
        await repo.append_entries(
            PROJECT, SDK_SESSION, None, [{"n": index}], append_id=f"b{index}"
        )

    scope_collection = await repo._scope_collection()
    scope_id = repo._scope_id(None, PROJECT, SDK_SESSION, None)
    scope = await scope_collection.find_one({"_id": f"scope:{scope_id}"})

    assert scope is not None and scope["last_sequence"] == 5
    assert scope["claimed_batches"] == {}, (
        "a claim covers a batch whose rows may still be missing; keeping settled "
        "ones would grow the scope document with the session's whole history"
    )


async def test_a_scope_is_isolated_from_another_tenant_naming_it() -> None:
    """One tenant cannot claim sequence numbers inside another tenant's scope."""
    repo = TranscriptEntryRepository(collection_name=f"fence_{uuid.uuid4().hex[:8]}")
    victim = await repo.append_entries(
        PROJECT, SDK_SESSION, None, [{"type": "user", "text": "victim"}],
        append_id="collide", platform_session_id="victim-session",
    )
    attacker = await repo.append_entries(
        PROJECT, SDK_SESSION, None, [{"type": "user", "text": "attacker"}],
        append_id="collide", platform_session_id="attacker-session",
    )

    assert victim == attacker == 1, "each tenant counts its own scope from 1"
    assert await repo.load_entries(
        PROJECT, SDK_SESSION, None, platform_session_id="victim-session"
    ) == [{"type": "user", "text": "victim"}], (
        "the attacker's colliding append_id neither overwrote nor pre-empted the "
        "victim's entry"
    )


# ── the recovery watermark this position is read back as ─────────────────────


async def test_recovery_watermark_is_unmoved_by_a_redelivery() -> None:
    repo = TranscriptEntryRepository(collection_name=f"mirror_{uuid.uuid4().hex[:8]}")
    entries = [{"type": "summary", "summary": "x"}, {"type": "summary", "summary": "y"}]
    await repo.append_entries(
        PROJECT, SDK_SESSION, None, entries,
        append_id="b1", platform_session_id=PLATFORM_SESSION,
    )

    def watermark(rows: list[dict[str, Any]]) -> int:
        return max(int(row["__astrabox_mirror_seq"]) for row in rows)

    before = await repo.load_recovery_entries_by_platform_session(PLATFORM_SESSION)
    await repo.append_entries(
        PROJECT, SDK_SESSION, None, entries,
        append_id="b1", platform_session_id=PLATFORM_SESSION,
    )
    after = await repo.load_recovery_entries_by_platform_session(PLATFORM_SESSION)

    assert len(before) == len(after) == 2
    assert watermark(before) == watermark(after) == 2, (
        "the recovery watermark is the store position seen from the read side; a "
        "re-delivery must not advance it"
    )


# ── the in-box sender and the platform, held to one another ──────────────────


def test_the_digest_definitions_on_both_sides_of_the_box_agree() -> None:
    """The sender cannot import the platform's copy, so a test holds them equal.

    ``sandbox_runner`` and ``astrabox-transcript-mirror`` each run inside a sandbox
    image and may import nothing from the host package, so the digest exists
    three times. Definitions that must produce the same string are exactly what
    drifts silently.
    """
    import importlib.util
    from importlib.machinery import SourceFileLoader
    from pathlib import Path

    program = (
        Path(__file__).resolve().parents[1]
        / "astrabox/core/service/orchestrator/runtime/astrabox-transcript-mirror"
    )
    spec = importlib.util.spec_from_loader(
        "_transcript_mirror_digest", SourceFileLoader("_transcript_mirror_digest", str(program))
    )
    assert spec and spec.loader
    transcript_mirror_prog = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transcript_mirror_prog)

    for key, entries in (
        (KEY, NO_UUID_ENTRIES),
        ({"project_key": "p", "session_id": "s", "subpath": "agent-1"}, []),
        ({"session_id": "s", "project_key": "p"}, [{"b": 1, "a": {"z": 0, "y": "ü"}}]),
        # The Codex mirror relays rollout lines verbatim, so the entries it
        # digests are the vendor's shapes, not the SDK's.
        (
            {"project_key": "/workspace", "session_id": "s", "subpath": "codex/a.jsonl"},
            [{"type": "response_item", "payload": {"role": "user", "text": "ü"}}],
        ),
    ):
        platform = transcript_routes.transcript_payload_digest(key, entries)
        assert sandbox_runner.transcript_payload_digest(key, entries) == platform
        assert transcript_mirror_prog.transcript_payload_digest(key, entries) == platform


def test_the_body_the_in_box_sender_builds_is_accepted_by_the_route() -> None:
    """What the box sends is what the platform requires — proven by sending it."""
    sent: list[dict[str, Any]] = []

    class _CapturingTarget(sandbox_runner._HttpStoreTarget):
        async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            sent.append({"path": path, "payload": payload})
            return {"data": {"ok": True, "count": len(payload["entries"]),
                             "store_sequence": len(payload["entries"])}}

    flush, _load, _list_subkeys, _sequence = _CapturingTarget(
        "http://platform", {}
    ).bind(
        PLATFORM_SESSION
    )
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        flush(KEY, NO_UUID_ENTRIES, "batch-from-the-box")
    )

    assert sent[0]["path"].endswith(f"/api/v1/transcript/{PLATFORM_SESSION}/append")
    client = _Client()
    response = client.post("append", sent[0]["payload"])
    assert response.status_code == 200, response.text
    assert response.json()["data"]["store_sequence"] == 2


async def test_the_sender_refuses_a_position_that_moved_backwards() -> None:
    """A store that answers lower has lost entries the box was told were stored."""
    positions = iter([5, 3])

    class _RewindingTarget(sandbox_runner._HttpStoreTarget):
        async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            return {"data": {"ok": True, "store_sequence": next(positions)}}

    flush, _load, _list_subkeys, _sequence = _RewindingTarget(
        "http://platform", {}
    ).bind(
        PLATFORM_SESSION
    )
    await flush(KEY, NO_UUID_ENTRIES, "batch-1")
    with pytest.raises(RuntimeError, match="moved backwards"):
        await flush(KEY, NO_UUID_ENTRIES, "batch-2")


# ── the spool: the batch identity has to survive what the batch survives ─────


async def test_a_respooled_batch_keeps_the_append_id_it_was_written_with(
    tmp_path: Any,
) -> None:
    flushed: list[str] = []

    async def _flush(_key: Any, _entries: Any, append_id: str) -> None:
        flushed.append(append_id)

    async def _load(_key: Any) -> None:
        return None

    spool = tmp_path / "spool"
    first = sandbox_runner.SpoolSessionStore(
        spool,
        flush_fn=_flush,
        load_fn=_load,
        list_subkeys_fn=_empty_subkeys,
    )
    await first.append(KEY, NO_UUID_ENTRIES)
    spooled = json.loads(next(spool.glob("*.batch.json")).read_text())

    # The runner dies before flushing; a fresh one takes over the same directory.
    restarted = sandbox_runner.SpoolSessionStore(
        spool,
        flush_fn=_flush,
        load_fn=_load,
        list_subkeys_fn=_empty_subkeys,
    )
    assert await restarted.flush_once() == 1
    assert flushed == [spooled["append_id"]], (
        "the id is minted with the fsync'd batch, so a restart re-sends the same "
        "batch rather than inventing a second one"
    )


async def test_a_permanently_refused_batch_stops_the_flusher_loudly(
    tmp_path: Any,
) -> None:
    """A verdict on the request is not something to retry until someone notices.

    A box whose image predates a wire change is refused identically forever. The
    spool exists to ride out an unreachable platform, not to loop on a rejection
    — a loop there produces one log line every couple of seconds and a box that
    keeps serving turns while its transcript silently stops being mirrored.
    """
    attempts: list[str] = []

    async def _refuse(_key: Any, _entries: Any, append_id: str) -> None:
        attempts.append(append_id)
        raise sandbox_runner._PermanentFlushRejection("400: append_id is required")

    async def _load(_key: Any) -> None:
        return None

    store = sandbox_runner.SpoolSessionStore(
        tmp_path / "spool",
        flush_fn=_refuse,
        load_fn=_load,
        list_subkeys_fn=_empty_subkeys,
        retry_delay_s=0.01,
    )
    await store.append(KEY, NO_UUID_ENTRIES)
    store.start_flusher()
    await asyncio.sleep(0.1)

    assert len(attempts) == 1, "a refused batch must be attempted once, not on a loop"
    assert store.permanent_rejection is not None, "and the refusal must be visible"
    assert store.pending_batch_count() == 1, (
        "the batch stays spooled: it was never delivered, and dropping it to keep "
        "the queue moving would trade a visible stall for silent transcript loss"
    )
    await store.stop_flusher()


def test_only_a_verdict_on_the_request_is_treated_as_permanent() -> None:
    """5xx and the retry-me 4xx stay transient — those are what the spool is for."""
    from urllib.error import HTTPError

    permanent = {400: True, 403: True, 404: True, 409: True, 422: True,
                 408: False, 429: False, 500: False, 502: False, 503: False}
    for code, is_permanent in permanent.items():
        error = HTTPError("http://platform/x", code, "reason", {}, None)  # type: ignore[arg-type]
        classified = sandbox_runner._classify_flush_error(error)
        assert isinstance(classified, sandbox_runner._PermanentFlushRejection) is is_permanent, (
            f"HTTP {code} must be {'permanent' if is_permanent else 'transient'}: "
            "a retryable code looping is the spool working, a verdict looping is "
            "the failure hiding itself"
        )


async def test_a_restarted_spool_files_new_batches_behind_the_unflushed_ones(
    tmp_path: Any,
) -> None:
    """Filenames order the queue, so the counter behind them must resume."""
    order: list[list[dict[str, Any]]] = []

    async def _flush(_key: Any, entries: list[dict[str, Any]], _append_id: str) -> None:
        order.append(entries)

    async def _load(_key: Any) -> None:
        return None

    spool = tmp_path / "spool"
    first = sandbox_runner.SpoolSessionStore(
        spool,
        flush_fn=_flush,
        load_fn=_load,
        list_subkeys_fn=_empty_subkeys,
    )
    await first.append(KEY, [{"n": 1}])
    await first.append(KEY, [{"n": 2}])

    restarted = sandbox_runner.SpoolSessionStore(
        spool,
        flush_fn=_flush,
        load_fn=_load,
        list_subkeys_fn=_empty_subkeys,
    )
    await restarted.append(KEY, [{"n": 3}])
    await restarted.flush_once()

    assert order == [[{"n": 1}], [{"n": 2}], [{"n": 3}]], (
        "a counter restarting from zero would file the new batch ahead of the "
        "unflushed ones and send the transcript out of order"
    )
