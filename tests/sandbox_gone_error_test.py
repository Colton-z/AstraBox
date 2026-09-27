"""SANDBOX_GONE says what happened to the request and what moves it on.

The code reaches callers on two paths. A message whose delivery finds its
sandbox gone is refused with a 409; the command was already accepted, so the
same message sent again under its client_message_id replays it and is not
answered. A file operation or an engine control that meets the dead box
receives the provider's 404 and keeps receiving it. Neither request succeeds
when sent again, so the row says ``retryable: false`` on both, and the texts
ask for what does move the conversation to a new sandbox: a new message.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from opensandbox.exceptions import SandboxApiException

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.turn_service import TurnService
from astrabox.providers.open_sandbox.sandbox import OpenSandboxSandboxProvider


def _service(ensured: SimpleNamespace) -> TurnService:
    class _RuntimeManager:
        async def maybe_renew_lease_on_activity(self, session_id: str) -> None:
            return None

    service = object.__new__(TurnService)
    service._runtime_manager = _RuntimeManager()  # type: ignore[assignment]

    async def _ensure(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return ensured

    service._ensure_runtime_for_session = _ensure  # type: ignore[method-assign]
    return service


async def _refusal(session: dict[str, Any], ensured: SimpleNamespace) -> APIError:
    with pytest.raises(APIError) as refused:
        await _service(ensured).ensure_runtime_for_input_delivery(
            session,
            user=SimpleNamespace(),
            command_id="command-1",
        )
    return refused.value


def _gone(detail: str) -> SimpleNamespace:
    return SimpleNamespace(runtime=None, sandbox_gone=True, error_text=detail)


async def test_a_refused_assistant_message_asks_for_a_new_message() -> None:
    """The Assistant's delivery does not replace the box, and says so."""

    refused = await _refusal(
        {"session_id": "conv-a", "session_kind": "assistant_chat"},
        _gone("open_sandbox connect: sandbox 'sb-1' no longer exists"),
    )

    envelope = refused.to_error_envelope()
    assert envelope["code"] == "SANDBOX_GONE"
    assert envelope["status_code"] == 409
    assert envelope["category"] == "runtime.sandbox"
    assert envelope["owner"] == "runtime"
    # The same message sent again replays the accepted command; a new message
    # is a different request.
    assert envelope["retryable"] is False
    assert envelope["user_message"] == (
        "the sandbox this conversation was on is gone, and this message was "
        "not delivered; send a new message to continue on a new sandbox"
    )
    assert refused.data["sandbox_gone"] is True
    assert refused.data["detail"] == "open_sandbox connect: sandbox 'sb-1' no longer exists"


async def test_a_refused_agent_message_says_its_new_sandbox_failed() -> None:
    """An Agent's delivery already tried a new sandbox; the reason travels along."""

    refused = await _refusal(
        {"session_id": "conv-b", "session_kind": "agent_chat"},
        _gone("sandbox create failed: image not found"),
    )

    assert refused.code == "SANDBOX_GONE"
    assert refused.status_code == 409
    assert refused.retryable is False
    assert refused.user_message == (
        "the sandbox this conversation was on is gone, and a new sandbox could "
        "not be prepared for this message; send a new message to try on a new "
        "sandbox"
    )
    assert refused.data["detail"] == "sandbox create failed: image not found"


async def test_a_runtime_that_did_not_attach_to_a_live_box_is_not_called_gone() -> None:
    """The refusal ends the message either way, so it is not retryable as sent."""

    refused = await _refusal(
        {"session_id": "conv-c", "session_kind": "assistant_chat"},
        SimpleNamespace(runtime=None, sandbox_gone=False, error_text="handshake timed out"),
    )

    envelope = refused.to_error_envelope()
    assert envelope["code"] == "INPUT_NOT_DELIVERED"
    assert envelope["status_code"] == 409
    assert envelope["category"] == "state"
    assert envelope["owner"] == "session"
    assert envelope["retryable"] is False
    assert envelope["user_message"] == (
        "this conversation's runtime could not be attached, and this message "
        "was not delivered; send a new message to try again"
    )
    assert refused.data == {
        "session_id": "conv-c",
        "sandbox_gone": False,
        "detail": "handshake timed out",
    }


async def test_the_provider_verdict_is_not_retryable_either() -> None:
    """A file operation or engine control receives this envelope unchanged.

    Sent again, such a request meets the same dead box until a message moves
    the conversation to a new sandbox, so it is not retryable as sent.
    """

    provider = object.__new__(OpenSandboxSandboxProvider)
    verdict = provider._api_error(
        SandboxApiException(message="not found", status_code=404),
        operation="connect",
        sandbox_id="sb-1",
        secret=None,
    )

    envelope = verdict.to_error_envelope()
    assert envelope["code"] == "SANDBOX_GONE"
    assert envelope["status_code"] == 404
    assert envelope["category"] == "runtime.sandbox"
    assert envelope["owner"] == "runtime"
    assert envelope["retryable"] is False
    assert envelope["user_message"] == "open_sandbox connect: sandbox 'sb-1' no longer exists"
