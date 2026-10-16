"""A dedicated cold replacement restores the conversation and its mounted files.

The conversation's own box is reclaimed while the conversation is at rest. Its
next message borrows a new box that is not prepared ahead of time; that box
resumes the engine's own session (the same native session key) from the
transcript the platform kept, and mounts the same persistent workspace, so the
file written before the reclaim is still there.
"""

from __future__ import annotations

import json
import shlex
import time
import uuid

import httpx
import pytest

from astrabox.core.service.orchestrator.engine.capabilities import capabilities_for_engine_kind
from astrabox.providers import register_builtin_providers
from tests.e2e._engine_profile import current_profile
from tests.e2e._sandbox_helpers import (
    data,
    environment_with_tenancy,
    get_admin_session_detail,
    poll_until_agent_ready,
    release_agent,
    release_session,
    run_terminal,
    send_and_follow,
    wait_until_settled,
)

pytestmark = pytest.mark.e2e


def test_cold_rebuild_restores_existing_conversation(e2e_client: httpx.Client) -> None:
    profile = current_profile()
    register_builtin_providers()
    supports_skills = capabilities_for_engine_kind(
        profile["engine_kind"]
    ).extensions.skills_target is not None
    source = data(e2e_client.get(f"/api/v1/agents/{profile['agent_id']}"))
    agent = data(e2e_client.post("/api/v1/agents", json={
        "name": f"cold-resume-{uuid.uuid4().hex[:10]}",
        "model": source["model"],
        "engine_options": source.get("engine_options") or {},
        "environment_name": environment_with_tenancy(e2e_client, "conversation"),
        "prepared_capacity": 0,
        "skills": (["https://github.com/anthropics/skills.git@"
                    "34040c9c568585f6929bedeaad110ad08f079624#skills/skill-creator"]
                   if supports_skills else []),
    }))
    release_agent(agent["agent_id"])
    session = data(e2e_client.post(
        f"/api/v1/agents/{agent['agent_id']}/conversations", json={}
    ))
    sid = session["session_id"]
    release_session(sid)
    poll_until_agent_ready(e2e_client, sid)
    marker = "PROJECT-" + uuid.uuid4().hex
    initial = send_and_follow(
        e2e_client,
        sid,
        f"Our research project label is {marker}. Acknowledge the label briefly. Do not use tools.",
        until="end",
    )
    assert initial.error is None and marker in initial.text, initial.response
    wait_until_settled(e2e_client, sid)
    out, err, rc, _ = run_terminal(e2e_client, sid,
        f"printf %s {shlex.quote(marker)} > /workspace/resume-proof.txt; "
        'find -L "$HOME" -name SKILL.md -type f -print -quit')
    assert rc == 0, (out, err, rc)
    if supports_skills:
        assert "SKILL.md" in out, (out, err, rc)
    old = get_admin_session_detail(e2e_client, sid)
    assert old.get("engine_session_key")
    removed = data(e2e_client.post(f"/api/v1/sessions/{sid}/sandbox/terminate", timeout=60))
    assert removed["killed"] is True and removed["sandbox_id"] == old["sandbox_id"]
    start = time.monotonic()
    reply = send_and_follow(
        e2e_client,
        sid,
        "What is our research project label? Reply with the label only. Do not use tools.",
        until="end",
    )
    elapsed = time.monotonic() - start
    assert reply.error is None and marker in reply.text, reply.response
    settled = wait_until_settled(e2e_client, sid)
    assert settled["last_response"]["response_id"] == reply.response_id, settled
    assert settled["last_response"]["outcome"] == "completed", settled
    new = get_admin_session_detail(e2e_client, sid)
    assert new["sandbox_id"] != old["sandbox_id"]
    assert new["engine_session_key"] == old["engine_session_key"]
    assert new["workspace_id"] == old["workspace_id"]
    out, err, rc, _ = run_terminal(e2e_client, sid, "cat /workspace/resume-proof.txt")
    assert rc == 0 and out.strip() == marker, (out, err, rc)
    print("COLD_RESUME=" + json.dumps({
        "session_id": sid, "old_sandbox": old["sandbox_id"],
        "new_sandbox": new["sandbox_id"], "request_to_finished_seconds": elapsed,
    }), flush=True)
