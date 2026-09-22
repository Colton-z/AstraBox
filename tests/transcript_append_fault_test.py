"""The E2E transcript-append fault seam and the retry contract it exercises."""

from __future__ import annotations

import asyncio
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterator
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib import request as urllib_request

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from astrabox.api.routes import transcript as transcript_routes
from astrabox.common.fault_injection import clear_fault_hooks
from astrabox.core.service.orchestrator import sandbox_runner
from astrabox.core.service.orchestrator.transcript_capability import (
    mint_transcript_capability_token,
)
from astrabox.testing import e2e_faults


PLATFORM_SESSION = "platform-session"
KEY = {"project_key": "project", "session_id": "sdk-session"}


@pytest.fixture(autouse=True)
def _isolated_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    from astrabox.config.settings import get_settings

    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "unit-test-shared-secret")
    monkeypatch.setenv(
        "ASTRABOX_E2E_TURN_TERMINAL_DROP_FAULT_FILE",
        str(tmp_path / "faults.json"),
    )
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    monkeypatch.delenv("ASTRABOX_E2E_FAULTS", raising=False)
    clear_fault_hooks()
    transcript_routes._registered_on = None
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    transcript_routes._registered_on = None
    clear_fault_hooks()


class _TranscriptClient:
    def __init__(self, platform_session: str = PLATFORM_SESSION) -> None:
        app = FastAPI()
        transcript_routes.register_transcript_routes(app)
        self._http = TestClient(app)
        token = mint_transcript_capability_token(platform_session)
        self._base = (
            f"/api/v1/sbxcap/{token}/api/v1/transcript/{platform_session}"
        )

    def post(self, operation: str, payload: dict[str, Any]) -> Any:
        return self._http.post(f"{self._base}/{operation}", json=payload)

    def append(
        self,
        entries: list[dict[str, Any]],
        *,
        append_id: str,
    ) -> Any:
        return self.post(
            "append",
            {
                "key": KEY,
                "entries": entries,
                "append_id": append_id,
                "payload_sha256": transcript_routes.transcript_payload_digest(
                    KEY, entries
                ),
            },
        )


def test_capability_store_lists_child_transcripts_for_sdk_resume() -> None:
    client = _TranscriptClient()
    child_key = {**KEY, "subpath": "subagents/agent-child"}
    child_entries = [{"type": "assistant", "uuid": "child-entry"}]
    appended = client.post(
        "append",
        {
            "key": child_key,
            "entries": child_entries,
            "append_id": "child-batch",
            "payload_sha256": transcript_routes.transcript_payload_digest(
                child_key, child_entries
            ),
        },
    )
    assert appended.status_code == 200, appended.text

    listed = client.post("list-subkeys", {"key": KEY})

    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["subkeys"] == ["subagents/agent-child"]


def test_capability_subkey_route_cannot_enumerate_another_session() -> None:
    victim = _TranscriptClient("victim-platform-session")
    attacker = _TranscriptClient("attacker-platform-session")
    child_key = {**KEY, "subpath": "subagents/agent-victim"}
    child_entries = [{"type": "assistant", "uuid": "victim-child"}]
    appended = victim.post(
        "append",
        {
            "key": child_key,
            "entries": child_entries,
            "append_id": "victim-child-batch",
            "payload_sha256": transcript_routes.transcript_payload_digest(
                child_key, child_entries
            ),
        },
    )
    assert appended.status_code == 200, appended.text

    listed = attacker.post("list-subkeys", {"key": KEY})

    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["subkeys"] == []


async def test_in_box_http_target_exposes_the_capability_subkey_route() -> None:
    client = _TranscriptClient()
    child_key = {**KEY, "subpath": "subagents/agent-child"}
    child_entries = [{"type": "assistant", "uuid": "child-entry"}]
    appended = client.post(
        "append",
        {
            "key": child_key,
            "entries": child_entries,
            "append_id": "child-batch",
            "payload_sha256": transcript_routes.transcript_payload_digest(
                child_key, child_entries
            ),
        },
    )
    assert appended.status_code == 200, appended.text
    paths: list[str] = []

    class _Target(sandbox_runner._HttpStoreTarget):
        async def _post(
            self, path: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            paths.append(path)
            response = client.post(path.rsplit("/", 1)[-1], payload)
            assert response.status_code == 200, response.text
            return response.json()

    _flush, _load, list_subkeys, _sequence = _Target(
        "http://platform.invalid", {}
    ).bind(PLATFORM_SESSION)

    assert await list_subkeys(KEY) == ["subagents/agent-child"]
    assert paths == [
        f"/api/v1/transcript/{PLATFORM_SESSION}/list-subkeys"
    ]


def _write_fault(base: Path, *, count: int, session_id: str) -> Path:
    fault_dir = Path(f"{base}.d")
    fault_dir.mkdir(parents=True)
    path = fault_dir / "transcript-append.json"
    path.write_text(
        json.dumps(
            {
                "faults": {"transcript_append_5xx": count},
                "match": {"session_id": session_id},
            }
        ),
        encoding="utf-8",
    )
    return path


def _fault_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_fault_rewrite_is_readable_across_container_uids(tmp_path: Path) -> None:
    target = tmp_path / "shared-fault.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os,sys; "
                "from astrabox.testing.e2e_faults import _atomic_write; "
                "os.umask(0o077); "
                "_atomic_write(sys.argv[1], {'faults': {'drop': 1}}); "
                "print(oct(os.stat(sys.argv[1]).st_mode & 0o777))"
            ),
            str(target),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "0o644"


def test_disabled_gate_never_consumes_or_rejects_an_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "faults.json"
    fault_path = _write_fault(base, count=1, session_id=PLATFORM_SESSION)
    before = fault_path.read_bytes()

    def _unexpected_file_probe(_path: str) -> bool:
        raise AssertionError("a disabled fault handler must not probe the filesystem")

    with monkeypatch.context() as no_io:
        no_io.setattr(e2e_faults.os.path, "isfile", _unexpected_file_probe)
        assert not e2e_faults.maybe_consume_transcript_append_5xx(
            session_id=PLATFORM_SESSION,
            append_id="batch-disabled",
            entry_count=1,
        )

    response = _TranscriptClient().append(
        [{"type": "user", "text": "still delivered"}],
        append_id="batch-disabled",
    )
    assert response.status_code == 200, response.text
    assert fault_path.read_bytes() == before, (
        "without ASTRABOX_E2E_FAULTS the declaration is neither read nor consumed"
    )


def test_counted_fault_matches_one_session_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRABOX_E2E_FAULTS", "1")
    fault_path = _write_fault(
        tmp_path / "faults.json", count=2, session_id=PLATFORM_SESSION
    )

    assert not e2e_faults.maybe_consume_transcript_append_5xx(
        session_id="another-session", append_id="foreign", entry_count=7
    )
    for _ in range(2):
        assert e2e_faults.maybe_consume_transcript_append_5xx(
            session_id=PLATFORM_SESSION,
            append_id="batch-retried",
            entry_count=2,
        )
    assert not e2e_faults.maybe_consume_transcript_append_5xx(
        session_id=PLATFORM_SESSION,
        append_id="batch-retried",
        entry_count=2,
    )

    payload = _fault_payload(fault_path)
    assert fault_path.stat().st_mode & 0o777 == 0o644
    assert payload["faults"]["transcript_append_5xx"] == 0
    assert payload["consumed"] == [
        {
            "fault": "transcript_append_5xx",
            "session_id": PLATFORM_SESSION,
            "append_id": "batch-retried",
            "entry_count": 2,
        },
        {
            "fault": "transcript_append_5xx",
            "session_id": PLATFORM_SESSION,
            "append_id": "batch-retried",
            "entry_count": 2,
        },
    ], "each temporary failure must leave observable batch identity evidence"


async def test_spooled_batch_retries_injected_5xx_and_lands_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRABOX_E2E_FAULTS", "1")
    fault_path = _write_fault(
        tmp_path / "faults.json", count=3, session_id=PLATFORM_SESSION
    )
    e2e_faults.install_e2e_fault_hooks()
    client = _TranscriptClient()
    entries = [
        {"type": "summary", "summary": "first"},
        {"type": "summary", "summary": "second"},
    ]
    attempts: list[tuple[int, str]] = []
    fault_envelopes: list[dict[str, Any]] = []

    def _urlopen(request: Any, timeout: float) -> BytesIO:
        assert timeout == 30
        assert isinstance(request.data, bytes)
        payload = json.loads(request.data)
        response = client._http.post(
            urlsplit(request.full_url).path,
            content=request.data,
            headers=dict(request.header_items()),
        )
        attempts.append((response.status_code, str(payload["append_id"])))
        if response.status_code >= 400:
            envelope = response.json()
            fault_envelopes.append(envelope)
            raise HTTPError(
                str(response.url),
                response.status_code,
                response.reason_phrase,
                response.headers,
                BytesIO(response.content),
            )
        return BytesIO(response.content)

    monkeypatch.setattr(urllib_request, "urlopen", _urlopen)
    capability_base = client._base.split("/api/v1/transcript/", 1)[0]
    flush, load, list_subkeys, _sequence = sandbox_runner._HttpStoreTarget(
        f"http://platform.invalid{capability_base}", {}
    ).bind(PLATFORM_SESSION)

    store = sandbox_runner.SpoolSessionStore(
        tmp_path / "spool",
        flush_fn=flush,
        load_fn=load,
        list_subkeys_fn=list_subkeys,
        retry_delay_s=0.01,
    )
    await store.append(KEY, entries)
    store.start_flusher()
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while store.pending_batch_count() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
    finally:
        await store.stop_flusher()

    assert store.pending_batch_count() == 0, (
        "a transient host failure must leave the batch queued until a retry lands it"
    )
    assert [status for status, _append_id in attempts] == [503, 503, 503, 200]
    assert len({append_id for _status, append_id in attempts}) == 1, (
        "every retry must carry the append_id persisted with the spooled batch"
    )
    assert [envelope["code"] for envelope in fault_envelopes] == [
        "E2E_TRANSCRIPT_APPEND_FAULT"
    ] * 3
    assert all(envelope["error"]["retryable"] is True for envelope in fault_envelopes), (
        "the real fault response must identify a temporary persistence outage"
    )

    loaded = client.post("load", {"key": KEY}).json()["data"]
    assert loaded["entries"] == entries, "the temporarily rejected batch is not lost"
    assert loaded["store_sequence"] == len(entries), (
        "redelivery stores every entry once rather than advancing the mirror twice"
    )
    consumed = _fault_payload(fault_path)["consumed"]
    assert len(consumed) == 3
    assert {item["append_id"] for item in consumed} == {attempts[0][1]}
