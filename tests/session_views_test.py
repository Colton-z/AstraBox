"""The session reads a page makes, from what the session's reader recorded.

The conversations are written with the Claude Agent SDK's own message types and
recorded through the runner journal, the Claude runtime client and a session
reader (``tests/_claude_session.py``).
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ToolUseBlock,
    UserMessage,
)

import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401 - registers the adapter
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.session_runtime.feed import SessionFeed
from astrabox.core.service.orchestrator.session_runtime.process_summaries import ProcessSummaries
from astrabox.core.service.orchestrator.session_runtime.service import SessionRuntimeService
from astrabox.core.service.orchestrator.session_runtime.views import SessionReads
from astrabox.persistence.repository.process_summary_repository import ProcessSummaryRepository
from astrabox.persistence.repository.session_read_repository import SessionReadRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository
from tests._claude_session import RunnerJournal, accept, create_session, record

pytestmark = pytest.mark.usefixtures("database")

_USER = UserContext(user_id="u1")
_NATIVE = "native-session-1"
_MODEL = "claude-sonnet-4-5"


def _reads() -> SessionReads:
    facts = SessionRuntimeRepository()
    reads = SessionReadRepository()
    return SessionReads(
        sessions=SessionRepository(),
        facts=facts,
        reads=reads,
        runtime=SessionRuntimeService(
            sessions=SessionRepository(), repo=facts, feed=SessionFeed(), host=cast(Any, None)
        ),
        summaries=ProcessSummaries(
            facts=facts,
            reads=reads,
            repo=ProcessSummaryRepository(),
            model=cast(Any, None),
            enabled=False,
            spawn=lambda coroutine, **_: pytest.fail("these reads start no label"),
        ),
    )


def _interaction_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    return [part["data"] for part in message["parts"] if part["type"] == "data-interaction"]


async def test_an_open_plan_confirmation_leaves_the_server_only_as_its_public_half() -> None:
    """The Claude adapter's plan confirmation carries the replies it sends back
    to Claude for each choice (``reply``, ``comment_prefix``); they are the
    adapter's to send, not the page's to see, on every read that shows the
    confirmation. A share link's reader also does not see the tool input, nor
    who wrote each message."""
    session_id = await create_session()
    journal = RunnerJournal(session_id)
    asking = await accept(session_id, "Plan the config change")
    plan = {"plan": "1. Read pyproject.toml\n2. Bump the version"}
    await journal.write(
        UserMessage(content="Plan the config change", uuid=asking),
        AssistantMessage(
            content=[ToolUseBlock(id="toolu_plan", name="ExitPlanMode", input=plan)],
            model=_MODEL,
            session_id=_NATIVE,
        ),
    )
    await journal.gate("gate-plan", tool_use_id="toolu_plan", tool_name="ExitPlanMode", tool_input=plan)
    await record(session_id, journal)
    reads = _reads()
    queued = await accept(session_id, "Also update the changelog")

    detail = await reads.detail(_USER, session_id)
    listed = await reads.sessions_page(_USER, limit=10, cursor=None)
    owner_page = await reads.messages(_USER, session_id, before=None, limit=50)
    shared_page = await reads.shared_messages(session_id, before=None, limit=50)
    shared_session = await reads.shared_session(session_id, allow_download=False)

    asked = detail["pending_interaction"]
    assert (asked["interaction_id"], asked["presentation"], asked["raw_input"]) == ("gate-plan", "decision", plan)
    assert asked["response_id"] == owner_page["messages"][1]["id"]
    for options in (
        asked["options"],
        owner_page["pending_interaction"]["options"],
        _interaction_parts(owner_page["messages"][1])[0]["options"],
        shared_page["pending_interaction"]["options"],
        _interaction_parts(shared_page["messages"][1])[0]["options"],
        shared_session["pending_interaction"]["options"],
    ):
        assert [option["id"] for option in options] == ["approve", "revise", "reject"]
        assert all("reply" not in option and "comment_prefix" not in option for option in options)
    assert "raw_input" not in shared_page["pending_interaction"]
    assert "raw_input" not in _interaction_parts(shared_page["messages"][1])[0]
    assert "raw_input" not in shared_session["pending_interaction"]
    assert "author_user_id" not in shared_page["messages"][0]["metadata"]
    assert owner_page["messages"][0]["metadata"]["author_user_id"] == "u1"
    assert "output_seq" not in shared_page

    assert detail["state"] == "WAITING_INPUT" == listed["sessions"][0]["state"]
    assert [response["status"] for response in detail["current_responses"]] == ["waiting"]
    assert [held["input_id"] for held in detail["held_inputs"]] == [queued]
    assert detail["output_seq"] == owner_page["output_seq"] > 0
    assert detail["engine_available"] is True
    assert "current_turn_id" not in detail and "delivery_state" not in detail
