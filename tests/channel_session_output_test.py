"""Durable output delivery does not depend on an inbound work item."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401 (registers the native message reader)

from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator.channel_ingress_service import ChannelIngressService
from astrabox.core.service.orchestrator.session_kernel.service_mixins.session_output import SessionOutputSubscriptionMixin
from astrabox.core.service.orchestrator.session_message_view import SessionMessageView
from astrabox.persistence.repository.channel_repository import ChannelRepository
from astrabox.persistence.repository.session_event_repository import SessionEventRepository
from astrabox.seams.channel import ChannelDeliveryHandle, ChannelProvider, register_channel


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class Destination(ChannelProvider):
    name = "session-output-test"

    def __init__(self):
        self.delivered: list[tuple[dict[str, Any], str]] = []

    def verify_and_resolve(self, **kwargs):
        raise AssertionError("output subscription must not fabricate an inbound message")

    async def deliver_outbound(self, *, reply_context, text, binding):
        self.delivered.append((reply_context, text))


async def subscribe(repo, *, deployment="binding", boundary=0, streaming=False):
    await repo.subscribe_to_output(
        session_id="session", deployment_id=deployment, channel_name=Destination.name,
        reply_context={"destination": deployment}, conversation_key="conversation",
        after_seq=boundary, streaming=streaming,
    )


async def frame(journal, identity, payload):
    sequence = await journal.allocate_session_frame_seq("session")
    await journal.append_frame({
        "session_id": "session", "turn_id": identity, "command_id": identity,
        "frame_seq": sequence, "scope": "turn", "payload": payload,
    })
    return {"event_seq": sequence}


async def response(journal, identity, text):
    for payload in [
        {"type": "start", "messageId": identity, "messageMetadata": {"turn_id": identity}},
        {"type": "text-start", "id": identity},
        {"type": "text-delta", "id": identity, "delta": text},
        {"type": "text-end", "id": identity},
        {"type": "finish", "finishReason": "stop"},
    ]:
        end = await frame(journal, identity, payload)
    return end


class SilentBroker:
    async def subscribe(self, session_id):
        return asyncio.Queue()

    async def unsubscribe(self, session_id, queue):
        pass


class Output(SessionOutputSubscriptionMixin):
    def __init__(self, journal):
        self._session_events_repo = journal
        self._message_view = SessionMessageView(journal)
        self._broker = SilentBroker()
        self._sessions_repo = AsyncMock()
        self._turn_service = AsyncMock()
        self._poll_interval_s = 0.01
        self._turn_terminal_settle_retry_window_s = 0.1
        self._turn_terminal_settle_retry_delay_s = 0.01


def service(repo, journal):
    tasks = []
    sessions = AsyncMock()
    sessions.get_session.return_value = {"state": "READY"}
    deployments = AsyncMock()
    deployments.get_by_id.return_value = {"scene": "channel:" + Destination.name}
    output = Output(journal)
    forbidden = AsyncMock(side_effect=AssertionError("delivery cannot dispatch an input"))
    spine = ChannelIngressService(
        deployment_repo=deployments, agent_repo=AsyncMock(), agent_service_getter=forbidden,
        stream_message_events_ds=forbidden, resume_command_stream=forbidden,
        sessions_repo=sessions, session_events_repo=journal, message_view=output._message_view,
        read_session_output=output.read_session_output,
        session_output_cursor=output.session_output_cursor,
        session_detail_getter=forbidden, supersede_pending_interaction=forbidden,
        spawn_background_task=lambda coro, **kwargs: tasks.append(asyncio.create_task(coro)),
        channel_repo=repo,
    )
    return spine, tasks


async def test_consecutive_outputs_reach_a_custom_provider_without_any_inbound():
    repo, journal = ChannelRepository(), SessionEventRepository()
    destination = Destination()
    register_channel(destination)
    await subscribe(repo)
    for index in range(3):
        await response(journal, f"response-{index}", f"tick {index}")
        spine, tasks = service(repo, journal)
        assert await spine.project_session_outputs() == 1
        await spine.sweep_pending_outbox()
        await asyncio.gather(*tasks)
    assert destination.delivered == [({"destination": "binding"}, f"tick {i}") for i in range(3)]
    assert await repo.list_recoverable_inbound() == []
    assert await spine.project_session_outputs() == 0


async def test_crash_after_staging_before_cursor_commit_replays_one_delivery(monkeypatch):
    repo, journal = ChannelRepository(), SessionEventRepository()
    destination = Destination()
    register_channel(destination)
    await subscribe(repo)
    await response(journal, "response", "durable reply")
    spine, _ = service(repo, journal)
    advance = repo.advance_output_subscription
    monkeypatch.setattr(repo, "advance_output_subscription", AsyncMock(side_effect=RuntimeError("crash")))
    await spine.project_session_outputs()
    assert (await repo.list_output_subscriptions())[0]["after_seq"] == 0
    staged = await repo.list_pending_outbox()
    assert len(staged) == 1
    monkeypatch.setattr(repo, "advance_output_subscription", advance)
    successor, tasks = service(repo, journal)
    assert await successor.project_session_outputs() == 1
    assert await repo.list_pending_outbox() == staged
    await successor.sweep_pending_outbox()
    await asyncio.gather(*tasks)
    assert destination.delivered == [({"destination": "binding"}, "durable reply")]


async def test_replayed_cursor_and_concurrent_projectors_keep_delivered_response_closed():
    repo, journal = ChannelRepository(), SessionEventRepository()
    destination = Destination()
    register_channel(destination)
    await subscribe(repo)
    await response(journal, "response", "one reply")
    first, tasks = service(repo, journal)
    second, _ = service(repo, journal)
    await asyncio.gather(first.project_session_outputs(), second.project_session_outputs())
    await first.sweep_pending_outbox()
    await asyncio.gather(*tasks)
    subscription = (await repo.list_output_subscriptions())[0]
    assert await repo.advance_output_subscription(
        subscription["_id"], expected_seq=subscription["after_seq"], sequence=0,
    )
    await second.project_session_outputs()
    assert await repo.list_pending_outbox() == []
    assert destination.delivered == [({"destination": "binding"}, "one reply")]


async def test_subscription_retains_destination_and_cursor_across_later_inputs():
    repo, journal = ChannelRepository(), SessionEventRepository()
    old = await response(journal, "old", "old reply")
    await subscribe(repo, boundary=old["event_seq"])
    before = await repo.list_output_subscriptions()
    await subscribe(repo, boundary=999)
    assert await repo.list_output_subscriptions() == before
    await subscribe(repo, deployment="second-binding", boundary=old["event_seq"])
    await response(journal, "new", "new reply")
    spine, _ = service(repo, journal)
    assert await spine.project_session_outputs() == 2
    rows = await repo.list_pending_outbox()
    assert len(rows) == 2
    assert {row["turn_id"] for row in rows} == {"new"}
    assert {row["deployment_id"] for row in rows} == {"binding", "second-binding"}
    assert all("work_item_id" not in row for row in rows)


async def test_later_response_waits_for_the_earlier_delivery_across_workers():
    repo, journal = ChannelRepository(), SessionEventRepository()
    await subscribe(repo)
    await response(journal, "first", "first reply")
    await response(journal, "second", "second reply")
    spine, _ = service(repo, journal)
    await spine.project_session_outputs()
    rows = {row["turn_id"]: row for row in await repo.list_pending_outbox()}
    other = ChannelRepository()
    assert await other.claim_outbox_for_delivery(rows["second"]["_id"], owner="other") is None
    generation = await repo.claim_outbox_for_delivery(rows["first"]["_id"], owner="first")
    assert generation is not None
    assert await other.claim_outbox_for_delivery(rows["second"]["_id"], owner="other") is None
    assert await repo.mark_outbox_delivered(rows["first"]["_id"], owner="first", generation=generation)
    assert await other.claim_outbox_for_delivery(rows["second"]["_id"], owner="other") is not None


async def test_custom_streaming_provider_observes_live_output_and_journal_completion():
    progress = asyncio.Event()
    received = []

    class Handle(ChannelDeliveryHandle):
        async def emit(self, event):
            received.append(event)
            if event.type == "progress":
                progress.set()

    class StreamingDestination(Destination):
        supports_streaming_delivery = True

        async def open_delivery(self, *, reply_context, prior_aliases, binding):
            return Handle()

    repo, journal = ChannelRepository(), SessionEventRepository()
    register_channel(StreamingDestination())
    await subscribe(repo, streaming=True)
    await frame(journal, "reply", {"type": "start", "messageId": "reply", "messageMetadata": {"turn_id": "reply"}})
    await frame(journal, "reply", {"type": "text-start", "id": "text"})
    await frame(journal, "reply", {"type": "text-delta", "id": "text", "delta": "live text"})
    spine, tasks = service(repo, journal)
    await spine.project_session_outputs()
    await spine.sweep_pending_outbox()
    try:
        await asyncio.wait_for(progress.wait(), timeout=5)
        assert [event.type for event in received] == ["turn_started", "progress"]
        assert received[-1].text == "live text"
        await frame(journal, "reply", {"type": "text-end", "id": "text"})
        await frame(journal, "reply", {"type": "finish", "finishReason": "stop"})
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        assert [event.type for event in received] == ["turn_started", "progress", "settled"]
        assert received[-1].text == "live text"
        await spine.project_session_outputs()
        assert await repo.list_pending_outbox() == []
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_two_replies_in_one_turn_keep_distinct_delivery_identities():
    repo, journal = ChannelRepository(), SessionEventRepository()
    destination = Destination()
    register_channel(destination)
    await subscribe(repo)
    for identity in ["first", "second"]:
        await frame(journal, "one-turn", {"type": "start", "messageId": identity})
        await frame(journal, "one-turn", {"type": "text-start", "id": identity})
        await frame(journal, "one-turn", {"type": "text-delta", "id": identity, "delta": identity})
        await frame(journal, "one-turn", {"type": "text-end", "id": identity})
        await frame(journal, "one-turn", {"type": "finish", "finishReason": "stop"})
    spine, tasks = service(repo, journal)
    assert await spine.project_session_outputs() == 2
    rows = await repo.list_pending_outbox()
    assert {row["response_id"] for row in rows} == {"first", "second"}
    for row in sorted(rows, key=lambda row: row["response_seq"]):
        await spine._deliver_outbox_row(row)
    assert destination.delivered == [({"destination": "binding"}, text) for text in ["first", "second"]]


async def test_partial_reply_replay_preserves_text_across_frame_pages():
    journal = SessionEventRepository()
    await frame(journal, "reply", {"type": "start", "messageId": "reply"})
    await frame(journal, "reply", {"type": "text-start", "id": "text"})
    for _ in range(510):
        await frame(journal, "reply", {"type": "text-delta", "id": "text", "delta": "x"})
    partial = await Output(journal).read_session_output("session", after_seq=0)
    assert partial.after_seq == 0
    assert [(reply.text, reply.complete) for reply in partial.responses] == [("x" * 510, False)]
    await frame(journal, "reply", {"type": "text-end", "id": "text"})
    end = await frame(journal, "reply", {"type": "finish", "finishReason": "stop"})
    resumed = await Output(journal).read_session_output("session", after_seq=partial.after_seq)
    assert resumed.after_seq == end["event_seq"]
    assert [(reply.response_id, reply.text, reply.complete) for reply in resumed.responses] == [("reply", "x" * 510, True)]


async def test_startup_display_message_is_preserved_without_sending_an_agent_reply():
    journal = SessionEventRepository()
    event = await journal.append_event({
        "session_id": "session", "channel": "conversation", "event_type": "engine.message",
        "payload": {"engine_kind": "claude_code", "message": {
            "__sdk_type": "HookEventMessage", "subtype": "hook_response",
            "hook_event_name": "SessionStart", "uuid": "startup",
            "data": {"output": '{"systemMessage":"startup display"}'},
        }},
    })
    output = Output(journal)
    batch = await output.read_session_output("session", after_seq=0)
    assert batch.responses == []
    assert batch.after_seq == event["event_seq"]
    display = await output._message_view.resident_message_frames("session", after_seq=0)
    assert display[0]["payload"]["data"]["content"] == "startup display"


async def test_subscription_between_start_and_first_text_rebuilds_the_reply():
    journal = SessionEventRepository()
    await frame(journal, "reply", {"type": "start", "messageId": "reply"})
    boundary = await Output(journal).read_session_output("session", after_seq=0)
    assert boundary.responses == []
    await frame(journal, "reply", {"type": "text-start", "id": "text"})
    await frame(journal, "reply", {"type": "text-delta", "id": "text", "delta": "complete reply"})
    await frame(journal, "reply", {"type": "text-end", "id": "text"})
    await frame(journal, "reply", {"type": "finish", "finishReason": "stop"})
    resumed = await Output(journal).read_session_output("session", after_seq=boundary.after_seq)
    assert [(reply.response_id, reply.text, reply.complete) for reply in resumed.responses] == [
        ("reply", "complete reply", True),
    ]
