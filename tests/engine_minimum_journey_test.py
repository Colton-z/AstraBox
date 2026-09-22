"""The platform enforces the minimum journey every Agent program must provide."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.base import (
    EngineInputCommand,
    EngineTurnReceipt,
)
from astrabox.core.service.orchestrator.engine.emissions import (
    EngineTurnEmission,
    emission_from_translated_frame,
)
from astrabox.core.service.orchestrator.engine.input_delivery import (
    input_response_message_id,
)
from astrabox.core.service.orchestrator.engine.frame_scope import (
    public_engine_frame_payload,
)
from astrabox.core.service.orchestrator.engine_turn import (
    iter_engine_client_events,
)
from astrabox.seams import sandbox as sandbox_seam
from astrabox.seams.sandbox import register_sandbox


_INPUT_ID = "00000000-0000-0000-0000-000000000001"


class _Client:
    def __init__(
        self,
        events: list[dict[str, Any]],
        *,
        engine_session_key: str | None = None,
        learn_key_on_stream: str | None = None,
    ) -> None:
        self._events = events
        self._engine_session_key = engine_session_key
        self._learn_key_on_stream = learn_key_on_stream
        self.cancelled = 0

    @property
    def engine_session_key(self) -> str | None:
        return self._engine_session_key

    async def begin_delivery(
        self,
        command: EngineInputCommand,
        *,
        consumption_confirmed: bool = False,
    ) -> EngineTurnReceipt:
        assert command.input_id == _INPUT_ID
        assert command.content == "hello"
        assert consumption_confirmed is False
        return EngineTurnReceipt(
            engine_turn_id=command.command_id,
            engine_session_key=self._engine_session_key,
            started_at_monotonic_ns=1,
            input_id=command.input_id,
            input_consumed=consumption_confirmed,
        )

    async def iter_turn_events(
        self, receipt: EngineTurnReceipt
    ) -> AsyncIterator[EngineTurnEmission]:
        assert receipt.engine_turn_id == "command-1"
        if self._learn_key_on_stream is not None:
            self._engine_session_key = self._learn_key_on_stream
        for event in self._events:
            yield emission_from_translated_frame(event)

    async def cancel_turn(self, receipt: EngineTurnReceipt) -> bool:
        assert receipt.engine_turn_id == "command-1"
        self.cancelled += 1
        return True


class _BadReceiptClient(_Client):
    def __init__(
        self,
        *,
        input_id: str,
        input_consumed: bool,
    ) -> None:
        super().__init__([], engine_session_key="native-session-1")
        self._receipt_input_id = input_id
        self._receipt_input_consumed = input_consumed

    async def begin_delivery(
        self,
        command: EngineInputCommand,
        *,
        consumption_confirmed: bool = False,
    ) -> EngineTurnReceipt:
        return EngineTurnReceipt(
            engine_turn_id=command.command_id,
            engine_session_key=self.engine_session_key,
            started_at_monotonic_ns=1,
            input_id=self._receipt_input_id,
            input_consumed=self._receipt_input_consumed,
        )


class _Repo:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def update_session(
        self, session_id: str, updates: dict[str, Any]
    ) -> None:
        assert session_id == "session-1"
        self.updates.append(dict(updates))


@pytest.fixture(autouse=True)
def _sandbox_without_turn_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_seam, "_BACKENDS", {})
    register_sandbox(SimpleNamespace(name="journey-test"))  # type: ignore[arg-type]


def _consumed() -> dict[str, Any]:
    return {
        "type": "data-input-consumed",
        "data": {
            "inputId": _INPUT_ID,
            "responseMessageId": input_response_message_id(_INPUT_ID),
            "content": "hello",
        },
    }


async def _drive(client: _Client) -> tuple[list[dict[str, Any]], _Repo]:
    repo = _Repo()
    runtime = SimpleNamespace(
        lock=asyncio.Lock(),
        current_task=None,
        engine_client=client,
        engine_kind="test-engine",
        sandbox_id="sandbox-1",
        sandbox=None,
        interrupting=False,
        conversation_bound=True,
    )
    events = [
        event
        async for event in iter_engine_client_events(
            session={
                "session_id": "session-1",
                "user_id": "owner-1",
                "sandbox_id": "sandbox-1",
                "sandbox_backend": "journey-test",
            },
            session_id="session-1",
            effective_content="hello",
            turn_id="turn-1",
            runtime=runtime,
            interaction_permission_mode=None,
            on_query_committed=None,
            emit_timing=lambda *args, **kwargs: None,
            client_message_id="message-1",
            delivery_command={
                "command_id": "command-1",
                "session_id": "session-1",
                "sequence": 1,
                "input_id": _INPUT_ID,
                "content": "hello",
                "client_message_id": "message-1",
                "consumption_confirmed": False,
            },
            sessions_repo=repo,  # type: ignore[arg-type]
            broker=object(),  # type: ignore[arg-type]
        )
    ]
    return events, repo


async def test_content_evidence_does_not_fail_an_identity_matched_boundary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Identity judges the boundary; content is evidence, never the verdict.

    A vendor may legally rewrite the input it consumed (slash expansion,
    attachment wrapping), so a consumed frame whose inputId matches the
    durable head must cross the boundary even when its content differs —
    with both sides logged as evidence.
    """
    rewritten = {
        "type": "data-input-consumed",
        "data": {
            "inputId": _INPUT_ID,
            "responseMessageId": input_response_message_id(_INPUT_ID),
            "content": "hello, expanded by the vendor",
        },
    }
    client = _Client(
        [
            rewritten,
            {"type": "text-delta", "id": "text-1", "delta": "hi"},
            {"type": "result", "finishReason": "stop"},
        ],
        engine_session_key="native-session-1",
    )

    with caplog.at_level("WARNING"):
        events, _repo = await _drive(client)

    assert any(event["type"] == "result" for event in events)
    assert any(
        "consumed input content differs" in record.message for record in caplog.records
    )


async def test_raw_vendor_event_uses_the_private_diagnostic_lane() -> None:
    client = _Client(
        [
            _consumed(),
            {
                "type": "data-raw-event",
                "data": {
                    "event_type": "test.engine",
                    "subtype": "telemetry.added",
                    "raw": {"nativeSessionId": "private-session"},
                },
            },
            {"type": "result", "finishReason": "stop"},
        ],
        engine_session_key="native-session-1",
    )

    events, _repo = await _drive(client)

    diagnostic = next(event for event in events if event["type"] == "engine_diagnostic")
    assert diagnostic["raw"] == {"nativeSessionId": "private-session"}
    assert not any(
        event.get("type") == "ai_sdk_frame"
        and (event.get("frame") or {}).get("type") == "data-raw-event"
        for event in events
    )


async def test_public_adapter_extension_is_marked_for_durable_projection() -> None:
    client = _Client(
        [
            _consumed(),
            {
                "type": "data-engine-metrics",
                "id": "metrics-1",
                "data": {"newVendorMetric": 7},
            },
            {"type": "result", "finishReason": "stop"},
        ],
        engine_session_key="native-session-1",
    )

    events, _repo = await _drive(client)

    extension = next(
        event
        for event in events
        if event.get("type") == "ai_sdk_frame"
        and (event.get("frame") or {}).get("type") == "data-engine-metrics"
    )
    stored = extension["frame"]
    assert stored["__engine_public_ui"] is True
    assert public_engine_frame_payload(
        stored,
        frame_seq=1,
        scope="turn",
    ) == {
        "type": "data-engine-metrics",
        "id": "metrics-1",
        "data": {"newVendorMetric": 7},
    }


async def test_terminal_vendor_fields_use_the_private_diagnostic_lane() -> None:
    client = _Client(
        [
            _consumed(),
            {
                "type": "result",
                "finishReason": "stop",
                "__engine_terminal_reason": "completed",
                "vendorFutureField": {"nativeControl": "private"},
            },
        ],
        engine_session_key="native-session-1",
    )

    events, _repo = await _drive(client)

    diagnostic = next(event for event in events if event["type"] == "engine_diagnostic")
    assert diagnostic["event_type"] == "engine.terminal"
    assert diagnostic["subtype"] == "completed"
    assert diagnostic["raw"] == {
        "vendorFutureField": {"nativeControl": "private"}
    }
    assert not any(
        event.get("type") == "ai_sdk_frame"
        and (event.get("frame") or {}).get("vendorFutureField")
        for event in events
    )


async def test_a_fresh_engine_persists_its_native_session_before_output() -> None:
    client = _Client(
        [
            _consumed(),
            {"type": "text-delta", "id": "text-1", "delta": "hi"},
            {"type": "result", "finishReason": "stop"},
        ],
        learn_key_on_stream="native-session-1",
    )

    events, repo = await _drive(client)

    assert repo.updates == [{"engine_session_key": "native-session-1"}]
    result = next(event for event in events if event["type"] == "result")
    assert result["data"]["session_id"] == "native-session-1"
    assert client.cancelled == 0


@pytest.mark.parametrize(
    ("engine_events", "message"),
    [
        (
            [{"type": "text-delta", "id": "text-1", "delta": "too early"}],
            "before consuming the durable FIFO head",
        ),
        (
            [
                {
                    "type": "data-input-consumed",
                    "data": {
                        "inputId": "another-input",
                        "responseMessageId": "another-response",
                        "content": "hello",
                    },
                }
            ],
            "does not identify the durable FIFO head exactly",
        ),
    ],
)
async def test_engine_output_cannot_overtake_the_fifo_head(
    engine_events: list[dict[str, Any]], message: str
) -> None:
    client = _Client(engine_events, engine_session_key="native-session-1")

    events, _repo = await _drive(client)

    error = next(event for event in events if event["type"] == "error")
    assert message in error["message"]
    assert client.cancelled == 1


@pytest.mark.parametrize(
    ("client", "message"),
    [
        (
            _BadReceiptClient(
                input_id="00000000-0000-0000-0000-000000000099",
                input_consumed=False,
            ),
            "does not identify the durable FIFO head exactly",
        ),
        (
            _BadReceiptClient(input_id=_INPUT_ID, input_consumed=True),
            "consumption state disagrees with durable FIFO evidence",
        ),
    ],
)
async def test_engine_receipt_must_match_the_fifo_offer(
    client: _BadReceiptClient,
    message: str,
) -> None:
    events, _repo = await _drive(client)

    error = next(event for event in events if event["type"] == "error")
    assert message in error["message"]


async def test_a_settled_turn_without_a_native_resume_key_is_rejected() -> None:
    client = _Client([_consumed(), {"type": "result", "finishReason": "stop"}])

    events, repo = await _drive(client)

    error = next(event for event in events if event["type"] == "error")
    assert "without exposing a durable native conversation key" in error["message"]
    assert repo.updates == []
    assert client.cancelled == 1
