"""Codex configuration acceptance from its actual catalog and native turn context."""

from __future__ import annotations

import copy
import json
import shlex
import time
import uuid
from typing import Any

import httpx

from tests.e2e._engine_profile import current_profile
from tests.e2e._sandbox_helpers import (
    create_agent_variant_session,
    data,
    get_admin_session_detail,
    poll_until_agent_ready,
    stream_turn,
    wait_until_settled,
)
from tests.e2e.test_terminal import _run_terminal
from tests.e2e.test_transcript_mirror import _query_mirror_docs


def verify_native_configuration(client: httpx.Client) -> None:
    """Exercise all three JSON destinations in one real, independently owned turn.

    The catalog is written at app-server startup. Config overrides stay in
    memory; Codex 0.153.4 persists effective approval/effort in TurnContextItem
    only once a turn starts, so those two nodes require one actual turn.
    """

    profile = current_profile()
    assert profile["engine_kind"] == "codex", profile["engine_kind"]
    agent = data(client.get(f"/api/v1/agents/{profile['agent_id']}"))
    catalog = copy.deepcopy(agent["engine_options"]["model_catalog"])
    models = catalog["models"]
    selected = [item for item in models if item.get("slug") == profile["model"]]
    assert len(selected) == 1, "the live Codex catalog must name the selected model exactly once"
    selected[0]["display_name"] = f"Native JSON catalog {uuid.uuid4().hex[:12]}"
    created = create_agent_variant_session(
        client,
        name_suffix="Native JSON",
        engine_options={
            "model_catalog": catalog,
            "config": {"approval_policy": "never"},
            "turn_start": {"effort": "low"},
        },
    )
    sid = str(created["session_id"])
    poll_until_agent_ready(client, sid)
    identity = get_admin_session_detail(client, sid).get("runtime_identity") or {}
    home = str(identity.get("home_dir") or "")
    assert home, identity
    stdout, stderr, code, events = _run_terminal(
        client, sid, "cat " + shlex.quote(f"{home}/.codex/models.json")
    )
    assert code == 0, (stderr, events)
    assert json.loads(stdout) == catalog, "the serving Codex home did not receive the authored catalog"

    result = stream_turn(
        client,
        sid,
        content=(
            "For a hypothetical company's revenue analysis, explain in one sentence "
            "what year-over-year revenue growth measures. No external research is needed."
        ),
    )
    assert result.error is None, result.error
    assert result.n_text_delta > 0 and result.text.strip(), result
    wait_until_settled(client, sid)

    contexts: list[dict[str, Any]] = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        entries = [json.loads(doc["entry_json"]) for doc in _query_mirror_docs(sid)]
        contexts = [entry["payload"] for entry in entries if entry.get("type") == "turn_context"]
        if contexts:
            break
        time.sleep(0.5)
    assert contexts, "Codex did not persist an effective turn context"
    for context in contexts:
        assert context["model"] == profile["model"], context
        assert context["approval_policy"] == "never", context
        assert context["effort"] == "low", context
