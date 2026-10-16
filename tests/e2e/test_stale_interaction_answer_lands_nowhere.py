"""An answer names the interaction it answers; a stale id resolves nothing.

When a client remembers which interaction it is answering instead of the
answer carrying that name, one pending gate is enough to hide the defect: with
a single outstanding interaction, a mis-recall still lands on the right one.
The failure needs a second interaction to exist, and then it approves a tool
the user never saw.

So this drives two gates in sequence, answers the first, and then replays the
FIRST id while the SECOND is pending. The replay must be refused as not open
and resolve nothing — the second interaction stays exactly where it was, its
tool does not run, and it runs only when it is answered on its own name.

    .venv/bin/python -m pytest tests/e2e/test_stale_interaction_answer_lands_nowhere.py -m e2e -s
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    answer_interaction,
    answer_interaction_refusal,
    assert_permission_mode_unavailable,
    contract_supported,
    create_session,
    decision,
    file_names,
    follow_reply,
    pending_interaction,
    permission_mode,
    poll_until_agent_ready,
    release_session,
    send_and_follow,
    tool_name,
    wait_for_file,
    wait_for_pending_interaction,
    wait_until_settled,
    workspace_path,
)

pytestmark = pytest.mark.e2e


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
            refused = answer_interaction_refusal(
                e2e_client, sid, "missing-interaction", {"decision": "approve"}
            )
            assert refused == (404, "INTERACTION_NOT_FOUND"), (
                f"an engine without interactions took an answer to nothing: {refused}"
            )
            assert pending_interaction(e2e_client, sid) is None
            return

        first_path = workspace_path(e2e_client, sid, "one.txt")
        second_path = workspace_path(e2e_client, sid, "two.txt")
        reply = send_and_follow(
            e2e_client,
            sid,
            f"Use the {tool_name('write')} tool to create {first_path} containing ONE. "
            f"Then use the {tool_name('write')} tool again to create {second_path} "
            "containing TWO. Then say DONE.",
            until="pause",
        )
        assert reply.error is None, f"the response failed before the first gate: {reply.error}"
        assert reply.paused, (
            f"no permission gate paused the response (finish={reply.finish_reason!r}); this "
            "test needs a session whose writes are gated"
        )

        first = wait_for_pending_interaction(e2e_client, sid, timeout=30.0)
        assert first is not None, f"the detail names no gate the stream paused on: {reply.interactions}"
        first_id = str(first.get("interaction_id") or "")
        assert first_id == reply.interactions[-1].get("interaction_id"), (first, reply.interactions)

        answer_interaction(e2e_client, sid, first_id, {"decision": decision("approve")})
        follow_reply(e2e_client, reply, until="pause")
        assert reply.paused, (
            "the second write did not raise its own gate, so there is no second "
            f"interaction for a stale answer to land on (finish={reply.finish_reason!r})"
        )
        second = wait_for_pending_interaction(e2e_client, sid, exclude_id=first_id, timeout=30.0)
        assert second is not None, f"the detail names no second gate: {reply.interactions}"
        second_id = str(second.get("interaction_id") or "")
        assert second_id and second_id != first_id, (
            f"expected a distinct second interaction, got {second_id!r}"
        )
        assert second_id == reply.interactions[-1].get("interaction_id"), (second, reply.interactions)

        # The replay: the FIRST id, answered again, while the SECOND waits.
        refused = answer_interaction_refusal(
            e2e_client, sid, first_id, {"decision": decision("approve")}
        )
        assert refused == (409, "INTERACTION_NOT_OPEN"), (
            "replaying an already-answered interaction id was not refused as not open: "
            f"{refused}"
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
        assert "two.txt" not in file_names(e2e_client, sid), (
            "the second write ran on the stale answer to the first"
        )

        # Answered on its own name, the second gate resolves normally.
        answer_interaction(e2e_client, sid, second_id, {"decision": decision("approve")})
        follow_reply(e2e_client, reply, until="end")
        assert reply.error is None, f"the response failed after the second answer: {reply.error}"
        wait_until_settled(e2e_client, sid)
        assert wait_for_file(e2e_client, sid, second_path, timeout=30.0) is not None, (
            "the second write did not run once its own interaction was answered"
        )
    finally:
        release_session(sid)
