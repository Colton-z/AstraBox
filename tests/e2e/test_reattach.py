"""Reattach a live Session after the deployed AstraBox server restarts.

This test deliberately restarts the deployment server selected by the shared
service-container handle. It does not construct a second host-only database or
callback topology; records stay in the deployment's PostgreSQL database and the
Agent workload stays in its real OpenSandbox runtime.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    create_session,
    delete_session,
    get_session,
    permission_mode,
    poll_until_agent_ready,
    stream_turn,
    tool_name,
    wait_for_file,
    workspace_path,
)
from tests.e2e._service_containers import (
    SERVER_CONTAINER_HANDLE,
    require_service_container,
)
from tests.e2e.conftest import _wait_health

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.backend_restart,
    pytest.mark.xdist_group("backend-restart"),
]


def _restart_server_container(container: str) -> None:
    result = subprocess.run(
        ["docker", "restart", "--time", "30", container],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"docker restart {container} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )


@pytest.fixture(scope="module")
def restarted_backend(
    e2e_base_url: str,
    e2e_auth_headers: dict[str, str],
) -> Iterator[dict[str, str]]:
    """Create a Session, restart its server, and retain the live Agent box."""
    server_container = require_service_container(SERVER_CONTAINER_HANDLE)

    session_id = ""
    try:
        with httpx.Client(
            base_url=e2e_base_url,
            headers=e2e_auth_headers,
            timeout=30.0,
        ) as client:
            created = create_session(client, permission_mode=permission_mode("alternate"))
            session_id = str(created.get("session_id") or "")
            assert session_id, f"no session_id: {created}"
            poll_until_agent_ready(client, session_id)
            sandbox_id = str(get_session(client, session_id).get("sandbox_id") or "")
            assert sandbox_id, "session has no sandbox_id after READY"
            alpha_path = workspace_path(client, session_id, "alpha.txt")
            first_turn = stream_turn(
                client,
                session_id,
                content=(
                    f"Use the {tool_name('write')} tool to create {alpha_path} containing the word "
                    "ALPHA, then say DONE."
                ),
            )
            assert first_turn.error is None, f"pre-restart turn errored: {first_turn.error}"
            assert wait_for_file(client, session_id, alpha_path, timeout=60.0) is not None

        _restart_server_container(server_container)
        _wait_health(e2e_base_url)
        yield {
            "url": e2e_base_url,
            "sid": session_id,
            "sandbox_id": sandbox_id,
            "alpha_path": alpha_path,
        }
    finally:
        if session_id:
            try:
                with httpx.Client(
                    base_url=e2e_base_url,
                    headers=e2e_auth_headers,
                    timeout=30.0,
                ) as client:
                    delete_session(client, session_id)
            except Exception:
                pass


def test_sandbox_and_session_survive_backend_restart(
    restarted_backend: dict[str, str],
    e2e_auth_headers: dict[str, str],
) -> None:
    session_id = restarted_backend["sid"]
    with httpx.Client(
        base_url=restarted_backend["url"],
        headers=e2e_auth_headers,
        timeout=30.0,
    ) as client:
        agents = client.get("/api/v1/agents")
        assert agents.status_code == 200, (
            "the Agent catalog must be available after restart while spare "
            f"runtime capacity is being prepared: HTTP {agents.status_code}"
        )
        detail = get_session(client, session_id)
        assert str(detail.get("sandbox_id") or "") == restarted_backend["sandbox_id"]
        content = wait_for_file(client, session_id, restarted_backend["alpha_path"])
        assert content is not None and b"ALPHA" in content, (
            "the original sandbox must still serve its pre-restart workspace file"
        )


def test_reattach_turn_reuses_sandbox_without_rebuild(
    restarted_backend: dict[str, str],
    e2e_auth_headers: dict[str, str],
) -> None:
    session_id = restarted_backend["sid"]
    with httpx.Client(
        base_url=restarted_backend["url"],
        headers=e2e_auth_headers,
        timeout=30.0,
    ) as client:
        beta_path = workspace_path(client, session_id, "beta.txt")
        second_turn = stream_turn(
            client,
            session_id,
            content=(
                f"Use the {tool_name('write')} tool to create {beta_path} containing the word BETA, "
                "then say DONE."
            ),
            timeout=240.0,
        )
        assert second_turn.error is None, (
            f"re-attached turn errored: {second_turn.error}"
        )
        assert wait_for_file(client, session_id, beta_path, timeout=60.0) is not None
        after = get_session(client, session_id)
        assert str(after.get("sandbox_id") or "") == restarted_backend["sandbox_id"]
