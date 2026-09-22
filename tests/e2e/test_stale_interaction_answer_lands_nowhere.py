"""An answer names the interaction it answers; a stale id resolves nothing.

When a client remembers which interaction it is answering instead of the
answer carrying that name, one pending gate is enough to hide the defect: with
a single outstanding interaction, a mis-recall still lands on the right one.
The failure needs a second interaction to exist, and then it approves a tool
the user never saw.

So this drives two gates in sequence, answers the first, and then replays the
FIRST id while the SECOND is pending. The replay must resolve nothing — the
second interaction stays exactly where it was, and the tool behind it runs only
when it is answered on its own name.

    .venv/bin/python -m pytest tests/e2e/test_stale_interaction_answer_lands_nowhere.py -m e2e -s
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    assert_permission_mode_unavailable,
    contract_supported,
    create_session,
    decision,
    delete_session,
    pending_interaction,
    permission_mode,
    poll_until_agent_ready,
    respond_interaction,
    stream_turn,
    tool_name,
    wait_for_pending_interaction,
    wait_until_settled,
    workspace_path,
)

pytestmark = pytest.mark.e2e


def _answer_raw(
    client: httpx.Client, sid: str, interaction_id: str, answer: dict
) -> tuple[int, str]:
    """POST an answer and report ``(status, error_code)`` without raising."""
    resp = client.post(
        f"/api/v1/sessions/{sid}/interaction-respond",
        json={"interaction_id": interaction_id, "answer": answer},
    )
    if resp.status_code < 400:
        return resp.status_code, ""
    try:
        return resp.status_code, str(resp.json().get("code") or "")
    except Exception:
        return resp.status_code, resp.text[:200]


def test_replaying_the_first_answer_does_not_resolve_the_second(
    e2e_client: httpx.Client,
) -> None:
    created = create_session(e2e_client, permission_mode=permission_mode("gated"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id: {created}"
    poll_until_agent_ready(e2e_client, sid)
    try:
        if not contract_supported("tool_approval"):
            assert_permission_mode_unavailable(e2e_client, sid)
            status, code = _answer_raw(
                e2e_client,
                sid,
                "missing-interaction",
                {"decision": "approve"},
            )
            assert status in (400, 409) and code, (
                "an engine without interactions accepted a stale answer: "
                f"status={status} code={code!r}"
            )
            assert pending_interaction(e2e_client, sid) is None
            return

        first_path = workspace_path(e2e_client, sid, "one.txt")
        second_path = workspace_path(e2e_client, sid, "two.txt")
        paused = stream_turn(
            e2e_client,
            sid,
            content=(
                f"Use the {tool_name('write')} tool to create {first_path} containing ONE. "
                f"Then use the {tool_name('write')} tool again to create {second_path} "
                "containing TWO. Then say DONE."
            ),
        )
        assert paused.error is None, f"turn errored before the first gate: {paused.error}"

        first = wait_for_pending_interaction(e2e_client, sid, timeout=30.0)
        assert first is not None, (
            f"no permission gate surfaced (finish={paused.finish_reason!r}); this test "
            "needs a session whose Writes are gated"
        )
        first_id = str(first.get("interaction_id") or "")
        assert first_id, f"the first gate carries no id: {first}"

        respond_interaction(e2e_client, sid, first_id, {"decision": decision("approve")})

        second = wait_for_pending_interaction(
            e2e_client, sid, exclude_id=first_id, timeout=60.0
        )
        assert second is not None, (
            "the second Write did not raise its own gate, so there is no second "
            "interaction for a stale answer to land on"
        )
        second_id = str(second.get("interaction_id") or "")
        assert second_id and second_id != first_id, (
            f"expected a distinct second interaction, got {second_id!r}"
        )

        # The replay: the FIRST id, answered again, while the SECOND waits.
        status, code = _answer_raw(
            e2e_client, sid, first_id, {"decision": decision("approve")}
        )
        assert status != 200 or code, (
            "replaying an already-answered interaction id was accepted as if it "
            "named the pending one"
        )

        still = pending_interaction(e2e_client, sid)
        assert still is not None, (
            "the stale answer resolved the pending interaction — the answer was "
            "applied by position rather than by the interaction it names"
        )
        assert str(still.get("interaction_id") or "") == second_id, (
            f"the pending interaction changed after a stale answer: expected "
            f"{second_id!r}, found {still.get('interaction_id')!r}"
        )

        # Answered on its own name, the second gate resolves normally.
        respond_interaction(e2e_client, sid, second_id, {"decision": decision("approve")})
        wait_until_settled(e2e_client, sid, timeout=180.0)
    finally:
        delete_session(e2e_client, sid)
