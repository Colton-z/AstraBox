"""Codex configuration acceptance from its native turn context."""

from __future__ import annotations

import time
from typing import Any

import httpx

from tests.e2e._engine_profile import current_profile
from tests.e2e._sandbox_helpers import (
    create_agent_variant_session,
    data,
    deadline_after,
    poll_until_agent_ready,
    send_and_follow,
    transcript_entries,
    wait_until_settled,
)


def verify_native_configuration(client: httpx.Client) -> None:
    """Exercise both JSON destinations in one real, independently owned turn.

    Config overrides stay in memory; Codex persists effective approval/effort
    in TurnContextItem only once a turn starts, so both nodes require one
    actual turn.
    """

    profile = current_profile()
    assert profile["engine_kind"] == "codex", profile["engine_kind"]
    created = create_agent_variant_session(
        client,
        name_suffix="Native JSON",
        engine_options={
            "config": {"approval_policy": "never"},
            "turn_start": {"effort": "low"},
        },
    )
    sid = str(created["session_id"])
    poll_until_agent_ready(client, sid)

    result = send_and_follow(
        client,
        sid,
        "For a hypothetical company's revenue analysis, explain in one sentence "
        "what year-over-year revenue growth measures. No external research is needed.",
        until="end",
    )
    assert result.error is None, result.error
    assert result.n_text_delta > 0 and result.text.strip(), result.response
    settled = wait_until_settled(client, sid)
    assert settled["last_response"]["response_id"] == result.response_id, settled
    assert settled["last_response"]["outcome"] == "completed", settled

    contexts: list[dict[str, Any]] = []
    deadline = deadline_after(30)
    while time.monotonic() < deadline:
        entries = [row["entry"] for row in transcript_entries(sid)]
        contexts = [entry["payload"] for entry in entries if entry.get("type") == "turn_context"]
        if contexts:
            break
        time.sleep(0.5)
    assert contexts, "Codex did not persist an effective turn context"
    for context in contexts:
        assert context["model"] == profile["model"], context
        assert context["approval_policy"] == "never", context
        assert context["effort"] == "low", context
