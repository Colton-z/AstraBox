"""Two conversations of one Agent cohabit one box without sharing state.

The Agent-shared tenancy's whole claim, asserted against the live engine
rather than against the placement bookkeeping: green placement rows prove a
conversation was ASSIGNED a home in the shared box, not that its engine can
answer from it — every one of the shared-tenancy defects found on the way
here (five of them wore the same shape) reported READY on a conversation
that could not talk. So the criterion is the product's own: both
conversations complete a real model turn, in the same box, as different
accounts, and neither can see the other's words.

The spec selects the deployment's agent-tenancy Environment for the lane's
engine and exact image, then creates its own Agent. The deployment supplies
the complete isolation configuration; the test exercises it without copying
and maintaining a second Environment recipe.
"""

from __future__ import annotations

import concurrent.futures
import json
import shlex
import subprocess
import time
import uuid

import httpx
import pytest

from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import (
    ADMISSION_GRACE_SECONDS,
)
from tests.e2e._sandbox_helpers import (
    READY_TIMEOUT_S,
    assert_release_matches_the_box,
    current_profile,
    data,
    environment_with_tenancy,
    get_admin_session_detail,
    get_session,
    poll_until_agent_ready,
    release_agent,
    release_session,
    run_terminal,
    stream_turn,
    wait_until_settled,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container

pytestmark = pytest.mark.e2e


def _start_conversation(client: httpx.Client, agent_id: str) -> str:
    started = data(client.post(f"/api/v1/agents/{agent_id}/conversations", json={}))
    sid = str(started.get("session_id") or "").strip()
    assert sid, f"conversation has no session_id: {started}"
    release_session(sid)
    return sid


def _answered(result, marker: str, sid: str) -> str:
    assert result.error is None, (
        f"conversation {sid} could not take its turn: {result.error}"
    )
    assert marker in result.text, (
        f"conversation {sid} never said its marker: {result.text!r}"
    )
    return result.text


def _create_shared_agent(e2e_client: httpx.Client, *, prewarm: bool = False) -> str:
    environment_name = environment_with_tenancy(e2e_client, "agent")
    profile = current_profile()
    canonical = data(
        e2e_client.get(f"/api/v1/agents/{profile['agent_id']}")
    )
    created = data(
        e2e_client.post(
            "/api/v1/agents",
            json={
                "name": f"Cohabitation {uuid.uuid4().hex[:8]}",
                "model": profile["model"],
                "environment_name": environment_name,
                "prewarm_enabled": prewarm,
                "engine_options": canonical.get("engine_options") or {},
                **(
                    {"system": canonical["system"]}
                    if isinstance(canonical.get("system"), str)
                    and canonical["system"].strip()
                    else {}
                ),
            },
        )
    )
    agent_id = str(created.get("agent_id") or "").strip()
    assert agent_id, f"variant Agent has no agent_id: {created}"
    release_agent(agent_id)
    return agent_id


def _wait_prepared(client: httpx.Client, agent_id: str, *, enabled: bool) -> dict:
    deadline = time.monotonic() + READY_TIMEOUT_S
    last: dict = {}
    while time.monotonic() < deadline:
        last = data(client.get(f"/api/v1/agents/{agent_id}/prepared-runtime"))
        assert not last.get("last_error"), f"Agent preparation failed: {last}"
        if enabled and last.get("ready") and last.get("sandbox_id"):
            return last
        if not enabled and not last.get("state") and not last.get("client_pool_name"):
            return last
        time.sleep(0.3)
    pytest.fail(f"Agent preparation did not settle: {last}")


def _prepared_isolation(agent_id: str) -> str:
    program = """
import asyncio, json, sys
from astrabox.deploy.onebox import ensure_database_wiring
from astrabox.persistence.repository.agent_repository import AgentRepository
async def main():
    ensure_database_wiring()
    row = await AgentRepository().get_agent(sys.argv[1])
    manifest = (row or {}).get('_prepared_slot') or {}
    print(json.dumps({'isolated_session_id': manifest.get('isolated_session_id')}))
asyncio.run(main())
"""
    result = subprocess.run(
        ["docker", "exec", require_service_container(SERVER_CONTAINER_HANDLE),
         "python", "-c", program, agent_id],
        capture_output=True, text=True, check=True, timeout=30,
    )
    identity = json.loads(result.stdout)
    isolated_id = str(identity.get("isolated_session_id") or "")
    assert isolated_id, "the prepared shared slot has no physical isolated session"
    return isolated_id


def _assert_private_runtime_files(client: httpx.Client, sid: str) -> None:
    identity = get_admin_session_detail(client, sid).get("runtime_identity") or {}
    home = str(identity.get("home_dir") or "")
    assert home, f"Session {sid} has no runtime home"
    spool = shlex.quote(f"{home}/.astrabox-spool")
    out, err, rc, events = run_terminal(
        client, sid,
        f"test -d {spool} && test -w {spool} && "
        f"find {shlex.quote(home)} -maxdepth 1 -type f -name '.astrabox-*.log' "
        "| grep -q . && "
        "find /workspace -maxdepth 1 "
        "\\( -name '.astrabox-*' -o -name 'Downloads' \\) -print",
    )
    assert rc == 0, f"Runtime spool and launch logs must live in private Home: {err}, {events}"
    assert not out.strip(), f"Platform artifacts polluted the user's Workspace: {out}"


def test_two_conversations_share_the_box_and_nothing_else(
    e2e_client: httpx.Client,
) -> None:
    agent_id = _create_shared_agent(e2e_client)

    started = time.monotonic()
    first_sid = _start_conversation(e2e_client, agent_id)
    second_sid = _start_conversation(e2e_client, agent_id)
    # Both placements must complete before either turn: starting a
    # conversation returns while provisioning is in flight, and a send
    # into CREATING answers SESSION_BUSY rather than queueing.
    poll_until_agent_ready(e2e_client, first_sid)
    poll_until_agent_ready(e2e_client, second_sid)

    # Both turns in flight AT ONCE: concurrency is part of the claim.
    marker_a, marker_b = uuid.uuid4().hex[:10], uuid.uuid4().hex[:10]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(
            stream_turn,
            e2e_client,
            first_sid,
            content=f"Reply with just COHAB-{marker_a}",
        )
        future_b = pool.submit(
            stream_turn,
            e2e_client,
            second_sid,
            content=f"Reply with just COHAB-{marker_b}",
        )
        settled_a = future_a.result()
        settled_b = future_b.result()
    wait_until_settled(e2e_client, first_sid)
    wait_until_settled(e2e_client, second_sid)

    text_a = _answered(settled_a, marker_a, first_sid)
    text_b = _answered(settled_b, marker_b, second_sid)
    _assert_private_runtime_files(e2e_client, first_sid)
    _assert_private_runtime_files(e2e_client, second_sid)
    # Neither answer may carry the sibling's marker — one engine serving
    # both conversations from one account would leak exactly this way.
    assert marker_b not in text_a and marker_a not in text_b

    # One box, two accounts: the platform's own rows plus the boxes' own
    # word for it, because the rows alone are the bookkeeping this spec
    # exists to distrust.
    first, second = (
        get_session(e2e_client, first_sid),
        get_session(e2e_client, second_sid),
    )
    assert first.get("sandbox_id") and (
        first.get("sandbox_id") == second.get("sandbox_id")
    ), (
        "conversations of one Agent did not share a box: "
        f"{first.get('sandbox_id')!r} vs {second.get('sandbox_id')!r}"
    )
    who_a, _err_a, _rc_a, _ = run_terminal(
        e2e_client, first_sid, "printf '%s' \"$USER:$HOSTNAME\""
    )
    who_b, _err_b, _rc_b, _ = run_terminal(
        e2e_client, second_sid, "printf '%s' \"$USER:$HOSTNAME\""
    )
    user_a, _, host_a = who_a.strip().partition(":")
    user_b, _, host_b = who_b.strip().partition(":")
    assert host_a and host_a == host_b, (
        f"terminals disagree about the box: {who_a!r} vs {who_b!r}"
    )
    assert user_a != user_b, (
        f"both conversations run as one account: {who_a!r} vs {who_b!r}"
    )

    # Ending one conversation must not end its sibling's box. This is THE
    # place that assertion can be made honestly — the box provably holds
    # two conversations right now — where the archive suite's conversation
    # is its box's only occupant and reclamation is the contract there.
    archived = data(
        e2e_client.post(f"/api/v1/sessions/{first_sid}/archive")
    )
    assert archived.get("killed") is False, (
        "archiving one cohabiting conversation killed the shared box "
        f"its sibling is still working in: {archived}"
    )
    who_b_after, _err_after, _rc_after, _ = run_terminal(
        e2e_client, second_sid, "printf '%s' \"$USER:$HOSTNAME\""
    )
    assert who_b_after.strip() == who_b.strip(), (
        "the surviving conversation lost its box after its sibling was "
        f"archived: {who_b!r} -> {who_b_after!r}"
    )
    data(e2e_client.post(f"/api/v1/admin/sessions/{second_sid}/evict-runtime"))
    last_archived = data(e2e_client.post(f"/api/v1/sessions/{second_sid}/archive"))
    assert time.monotonic() - started < ADMISSION_GRACE_SECONDS, (
        "the sibling admission expired before prompt reclamation was exercised"
    )
    assert last_archived.get("killed") is True, (
        f"the departed sibling kept the last conversation's empty box: {last_archived}"
    )
    assert_release_matches_the_box(
        e2e_client, last_archived, sandbox_id=str(first["sandbox_id"]),
        operation="last shared archive",
    )


def test_evicted_last_shared_conversation_reclaims_box(
    e2e_client: httpx.Client,
) -> None:
    """A claimed prewarm slot leaves no stale owner after cache eviction."""

    started = time.monotonic()
    agent_id = _create_shared_agent(e2e_client, prewarm=True)
    prepared = _wait_prepared(e2e_client, agent_id, enabled=True)
    isolated_id = _prepared_isolation(agent_id)
    sid = _start_conversation(e2e_client, agent_id)
    poll_until_agent_ready(e2e_client, sid)
    session = get_admin_session_detail(e2e_client, sid)
    assert session.get("sandbox_id") == prepared["sandbox_id"]
    assert (session.get("runtime_identity") or {}).get("isolated_session_id") == isolated_id
    marker = uuid.uuid4().hex[:10]
    result = stream_turn(e2e_client, sid, content=f"Reply with just RELEASE-{marker}")
    _answered(result, marker, sid)
    wait_until_settled(e2e_client, sid)
    _assert_private_runtime_files(e2e_client, sid)
    sandbox_id = str(get_session(e2e_client, sid).get("sandbox_id") or "")
    assert sandbox_id, "the shared conversation has no live box"

    agent = data(e2e_client.get(f"/api/v1/agents/{agent_id}"))
    data(e2e_client.put(
        f"/api/v1/agents/{agent_id}",
        json={
            "name": agent["name"],
            "model": agent["model"],
            "environment_name": agent["environment_name"],
            "version": agent["version"],
            "prewarm_enabled": False,
        },
    ))
    _wait_prepared(e2e_client, agent_id, enabled=False)
    out, _err, rc, _ = run_terminal(e2e_client, sid, f"printf '%s' '{marker}'")
    assert rc == 0 and marker in out, "retiring spare capacity damaged the claimed session"
    assert get_session(e2e_client, sid).get("sandbox_id") == sandbox_id
    evicted = data(e2e_client.post(f"/api/v1/admin/sessions/{sid}/evict-runtime"))
    assert evicted.get("evicted") == sid
    archived = data(e2e_client.post(f"/api/v1/sessions/{sid}/archive"))
    assert time.monotonic() - started < ADMISSION_GRACE_SECONDS, (
        "the prepared admission expired before prompt reclamation was exercised"
    )
    assert archived.get("killed") is True, (
        "the last shared conversation left an empty box after its cached "
        f"runtime was evicted: {archived}"
    )
    assert_release_matches_the_box(
        e2e_client, archived, sandbox_id=sandbox_id, operation="archive after eviction"
    )
