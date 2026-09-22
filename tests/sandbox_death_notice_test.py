"""The optional box-scoped death-notice receiver and script contract.

``converge_dead_sandbox`` takes control-plane-confirmed evidence. A box saying
"I am being torn down" is not that on its own — the platform cannot see the
kubelet — so the notice has to carry enough for the platform to tell an
UNPLANNED death from a planned one it asked for itself.

It is scoped to the BOX, not to a conversation. The box is what knows, and one
box can carry many conversations; which sessions the news affects is the
platform's lookup, never the emitter's assertion. Under the shared-sandbox mode
the box could not answer that question anyway — a session's ``/tmp`` is private,
so a container-level termination trigger cannot read what the conversations
inside wrote.

These pin, on both ends of the wire:

* every session bound to the box converges, and a planned teardown on ONE of
  them does not stop the others — converging only the newest, or giving up on
  the first skip, would leave sessions pointing at a box that is gone.
* the token is the box's and is domain-separated from a session's, so neither
  can stand in for the other.
* the in-box half writes a notice an external termination trigger can execute.

OpenSandbox standard create installs no such trigger. These tests retain the
receiver and script as extension surfaces; pull-based lifecycle
probing is the built-in recovery path and is covered by the convergence tests.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from astrabox.api.routes.sandbox_callback import register_sandbox_callback_routes
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.sandbox_lifecycle import (
    SandboxLifecycleService,
    SandboxOwnerConvergence,
    planned_session_teardown_reason,
)
from astrabox.core.service.orchestrator.sandbox_runner import _write_death_notice
from astrabox.core.service.orchestrator.transcript_capability import (
    mint_sandbox_box_capability_token,
    mint_transcript_capability_token,
)

_SANDBOX_ID = "sbx-live-1"


class _FakeSessionsRepo:
    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        self._sessions = sessions
        self.reads = 0

    async def list_sessions_by_sandbox_id(self, sandbox_id: str) -> list[dict[str, Any]]:
        self.reads += 1
        return [dict(s) for s in self._sessions if s.get("sandbox_id") == sandbox_id]


@pytest.fixture(autouse=True)
def _signing_key() -> Iterator[None]:
    previous = os.environ.get("ASTRABOX_TRANSCRIPT_SIGNING_KEY")
    os.environ["ASTRABOX_TRANSCRIPT_SIGNING_KEY"] = "test-signing-key-for-notice"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("ASTRABOX_TRANSCRIPT_SIGNING_KEY", None)
        else:
            os.environ["ASTRABOX_TRANSCRIPT_SIGNING_KEY"] = previous


def _client(
    sessions: list[dict[str, Any]],
) -> tuple[TestClient, _FakeSessionsRepo, AsyncMock]:
    repo = _FakeSessionsRepo(sessions)
    agent_repo = SimpleNamespace()
    platform = SimpleNamespace(
        _sessions_repo=repo,
        _agent_repo=agent_repo,
        _assistant_workspace_service=SimpleNamespace(),
    )
    platform._sandbox_lifecycle_service = SandboxLifecycleService(
        platform_service=platform,
    )

    async def _converge(sandbox_id: str, **_kwargs: Any) -> SandboxOwnerConvergence:
        converged: list[str] = []
        ignored: dict[str, str] = {}
        for session in sessions:
            if str(session.get("sandbox_id") or "").strip() != sandbox_id:
                continue
            session_id = str(session.get("session_id") or "").strip()
            planned = planned_session_teardown_reason(session)
            if planned:
                ignored[session_id] = planned
            else:
                converged.append(session_id)
        return SandboxOwnerConvergence(
            sandbox_id=sandbox_id,
            converged_sessions=tuple(converged),
            ignored_sessions=ignored,
        )

    converge = AsyncMock(side_effect=_converge)
    app = FastAPI()
    with (
        patch(
            "astrabox.api.routes.sandbox_callback.get_platform_service",
            return_value=platform,
        ),
        patch.object(
            SandboxLifecycleService, "converge_dead_sandbox_owners", converge
        ),
    ):
        register_sandbox_callback_routes(app)
        return TestClient(app), repo, converge


def _url(sandbox_id: str = _SANDBOX_ID, *, token: str | None = None) -> str:
    tok = token if token is not None else mint_sandbox_box_capability_token(sandbox_id)
    return f"/api/v1/sbxcap/{tok}/api/v1/sandbox/{sandbox_id}/terminating"


def _session(session_id: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "session_id": session_id,
        "sandbox_id": _SANDBOX_ID,
        "state": SessionState.READY.value,
    }
    row.update(overrides)
    return row


def test_every_session_bound_to_the_box_converges() -> None:
    client, _repo, converge = _client(
        [_session("s1"), _session("s2"), _session("s3")]
    )
    with patch.object(
        SandboxLifecycleService, "converge_dead_sandbox_owners", converge
    ):
        response = client.post(_url(), json={})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["handled"] is True
    assert sorted(data["converged"]) == ["s1", "s2", "s3"]
    assert converge.await_count == 1


def test_a_planned_teardown_on_one_session_does_not_stop_the_others() -> None:
    """The skips are per session. Giving up on the first would leave live
    sessions pointing at a box that is gone."""
    client, _repo, converge = _client(
        [
            _session("parked", sandbox_parked_at="2026-08-03T00:00:00Z"),
            _session("ended", state=SessionState.TERMINATED.value),
            _session("deleted", state=SessionState.DELETED.value),
            _session("live"),
        ]
    )
    with patch.object(
        SandboxLifecycleService, "converge_dead_sandbox_owners", converge
    ):
        data = client.post(_url(), json={}).json()["data"]
    assert data["converged"] == ["live"]
    assert data["ignored"] == {
        "parked": "session_parked",
        "ended": "session_ended",
        "deleted": "session_ended",
    }
    assert converge.await_count == 1


def test_a_session_that_moved_to_another_box_is_simply_not_in_the_set() -> None:
    """No fence needed: the lookup is BY sandbox id, so a re-borrowed session
    cannot be reached by its old box's notice at all."""
    client, _repo, converge = _client(
        [_session("moved-on", sandbox_id="sbx-replacement"), _session("still-here")]
    )
    with patch.object(
        SandboxLifecycleService, "converge_dead_sandbox_owners", converge
    ):
        data = client.post(_url(), json={}).json()["data"]
    assert data["converged"] == ["still-here"]
    assert converge.await_count == 1


def test_a_box_no_session_is_bound_to_is_not_an_error() -> None:
    """The ordinary case for an unclaimed prewarm box, and for one whose
    conversations already moved on."""
    client, _repo, converge = _client([])
    with patch.object(
        SandboxLifecycleService, "converge_dead_sandbox_owners", converge
    ):
        response = client.post(_url(), json={})
    assert response.status_code == 200
    assert response.json()["data"]["converged"] == []
    assert converge.await_count == 1


def test_a_forged_token_is_refused_before_the_sessions_are_read() -> None:
    client, repo, converge = _client([_session("s1")])
    with patch.object(
        SandboxLifecycleService, "converge_dead_sandbox_owners", converge
    ):
        response = client.post(_url(token="not-a-real-token"), json={})
    assert response.status_code == 403
    assert repo.reads == 0
    assert converge.await_count == 0


def test_another_box_token_cannot_converge_this_box() -> None:
    client, repo, converge = _client([_session("s1")])
    foreign = mint_sandbox_box_capability_token("some-other-box")
    with patch.object(
        SandboxLifecycleService, "converge_dead_sandbox_owners", converge
    ):
        response = client.post(_url(token=foreign), json={})
    assert response.status_code == 403
    assert repo.reads == 0


def test_a_session_token_is_not_a_box_token() -> None:
    """Domain separation. Without it the two are the same construction over
    different ids, so a transcript token would authorize tearing a box down."""
    assert mint_sandbox_box_capability_token(_SANDBOX_ID) != (
        mint_transcript_capability_token(_SANDBOX_ID)
    )
    client, repo, converge = _client([_session("s1")])
    session_token = mint_transcript_capability_token(_SANDBOX_ID)
    with patch.object(
        SandboxLifecycleService, "converge_dead_sandbox_owners", converge
    ):
        response = client.post(_url(token=session_token), json={})
    assert response.status_code == 403
    assert repo.reads == 0


# ── the in-box half ──────────────────────────────────────────────────────────


@pytest.fixture
def _notice_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "astrabox_death_notice.sh"
    with patch(
        "astrabox.core.service.orchestrator.sandbox_runner.DEATH_NOTICE_SCRIPT",
        str(path),
    ):
        yield path


def _run_notice(script: Path, tmp_path: Path) -> list[str]:
    """Run the script as an external termination hook would; return curl's argv.

    Reading the text would only show the URL appears somewhere in it. What
    matters is what a shell DOES with it, so the shell runs it against a curl
    that records instead of sending.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    recorded = tmp_path / "argv"
    shim = bin_dir / "curl"
    shim.write_text(
        '#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done > ' f'"{recorded}"\n',
        encoding="utf-8",
    )
    shim.chmod(0o700)
    result = subprocess.run(
        [str(script)],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return recorded.read_text(encoding="utf-8").splitlines()


def test_the_written_notice_posts_the_box_url(
    _notice_path: Path, tmp_path: Path
) -> None:
    url = f"https://platform.example/api/v1/sbxcap/tok-abc/api/v1/sandbox/{_SANDBOX_ID}/terminating"
    _write_death_notice({"url": url})
    # A termination trigger runs it directly, so the box's uid must execute it.
    assert _notice_path.stat().st_mode & 0o100
    argv = _run_notice(_notice_path, tmp_path)
    assert "POST" in argv
    assert url in argv
    # The address IS the message: the box is identified by the URL it was
    # given, and asserts nothing about which conversations are inside it.
    assert json.dumps({}) in argv or "{}" in argv


def test_hostile_values_stay_arguments_and_never_become_commands(
    _notice_path: Path, tmp_path: Path
) -> None:
    hostile_url = "https://x/'; touch " + str(tmp_path / "pwned") + "; echo '"
    _write_death_notice({"url": hostile_url})
    argv = _run_notice(_notice_path, tmp_path)
    assert hostile_url in argv
    assert not (tmp_path / "pwned").exists()


@pytest.mark.parametrize("notice", [None, {}, {"url": "  "}, "not-a-dict"])
def test_a_notice_without_an_address_writes_nothing(
    notice: Any, _notice_path: Path
) -> None:
    _write_death_notice(notice)
    # Better no notice at all — which the probe sweep covers — than one the
    # box believes it sent.
    assert not _notice_path.exists()
