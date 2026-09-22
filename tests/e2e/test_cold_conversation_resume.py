"""A dedicated cold replacement restores the conversation and its mounted files."""

from __future__ import annotations

import json
import shlex
import time
import uuid

import httpx
import pytest

from astrabox.bootstrap import bootstrap
from astrabox.core.service.orchestrator.engine.capabilities import capabilities_for_engine_kind
from tests.e2e._engine_profile import current_profile
from tests.e2e._sandbox_helpers import (
    data,
    environment_with_tenancy,
    get_admin_session_detail,
    poll_until_agent_ready,
    release_agent,
    release_session,
    run_terminal,
    stream_turn,
    wait_until_settled,
)

pytestmark = pytest.mark.e2e


def test_cold_rebuild_restores_existing_conversation(e2e_client: httpx.Client) -> None:
    profile = current_profile()
    bootstrap()
    supports_skills = "skills" in capabilities_for_engine_kind(
        profile["engine_kind"]
    ).configuration_inputs
    source = data(e2e_client.get(f"/api/v1/agents/{profile['agent_id']}"))
    agent = data(e2e_client.post("/api/v1/agents", json={
        "name": f"cold-resume-{uuid.uuid4().hex[:10]}",
        "model": source["model"],
        "engine_options": source.get("engine_options") or {},
        "environment_name": environment_with_tenancy(e2e_client, "conversation"),
        "prewarm_enabled": False,
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
    initial = stream_turn(e2e_client, sid, content=(
        f"Our research project label is {marker}. Acknowledge the label briefly. Do not use tools."
    ))
    assert initial.error is None and marker in initial.text, initial
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
    reply = stream_turn(e2e_client, sid, content=(
        "What is our research project label? Reply with the label only. Do not use tools."
    ))
    elapsed = time.monotonic() - start
    assert reply.error is None and marker in reply.text, reply
    wait_until_settled(e2e_client, sid)
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
