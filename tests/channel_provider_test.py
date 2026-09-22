"""Channel seam + generic_json reference provider + webhook-spine wiring.

Pins the incubated channel contract (see ``astrabox/seams/channel.py``):

* registry semantics (fail-loud lookup, scene parsing);
* the ``generic_json`` reference provider's inbound auth (constant-time
  secret, both header shapes), payload mapping, and callback-url validation;
* the webhook service's channel branch: scene gating at create time
  (unknown provider → 400), provider-delegated verification at trigger time,
  prompt_prefix application, and outbound delivery of the finished turn's
  last assistant message through ``deliver_outbound``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.channel_ingress_service import (
    ChannelIngressService,
)
from astrabox.core.service.orchestrator.deployment_service import DeploymentService
from astrabox.providers.channel_generic import GenericJsonChannelProvider
from astrabox.seams.channel import (
    ChannelInbound,
    ChannelProvider,
    channel_scene_name,
    get_channel,
    register_channel,
)


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from astrabox.config.settings import get_settings

    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

# ── seam registry ────────────────────────────────────────────────────────────


def test_channel_scene_name_parsing() -> None:
    assert channel_scene_name("channel:generic_json") == "generic_json"
    assert channel_scene_name("channel:SLACK") == "slack"
    assert channel_scene_name("hmac") is None
    assert channel_scene_name("channel:") is None


def test_get_channel_fails_loud_on_unknown_name() -> None:
    with pytest.raises(RuntimeError, match="no channel provider named"):
        get_channel("never-registered")


def test_builtin_generic_json_is_registered() -> None:
    import astrabox.providers.channel_generic  # noqa: F401 — registration side effect

    assert isinstance(get_channel("generic_json"), GenericJsonChannelProvider)


def test_generic_json_descriptor_separates_product_label_from_registry_key() -> None:
    descriptor = GenericJsonChannelProvider().describe().to_dict()

    assert descriptor["name"] == "generic_json"
    assert descriptor["scene"] == "channel:generic_json"
    assert descriptor["label"] == "Generic JSON webhook"


# ── generic_json provider ────────────────────────────────────────────────────

_BINDING = {"secret": "s3cret", "agent_id": "dep-1", "prompt_prefix": ""}


def _payload(**extra: Any) -> bytes:
    return json.dumps({"text": "hello agent", **extra}).encode()


def test_generic_json_accepts_both_secret_header_shapes() -> None:
    provider = GenericJsonChannelProvider()
    for headers in (
        {"x-channel-secret": "s3cret"},
        {"authorization": "Bearer s3cret"},
    ):
        inbound = provider.verify_and_resolve(
            headers=headers, raw_body=_payload(), binding=_BINDING
        )
        assert inbound.content == "hello agent"
        assert inbound.reply_context is None


def test_generic_json_rejects_bad_secret_and_bad_payload() -> None:
    provider = GenericJsonChannelProvider()
    with pytest.raises(APIError) as unauthorized:
        provider.verify_and_resolve(
            headers={"x-channel-secret": "wrong"}, raw_body=_payload(), binding=_BINDING
        )
    assert unauthorized.value.status_code == 401

    good = {"x-channel-secret": "s3cret"}
    with pytest.raises(APIError) as not_json:
        provider.verify_and_resolve(headers=good, raw_body=b"not json", binding=_BINDING)
    assert not_json.value.status_code == 400

    with pytest.raises(APIError) as no_text:
        provider.verify_and_resolve(
            headers=good, raw_body=json.dumps({"reply": {}}).encode(), binding=_BINDING
        )
    assert no_text.value.status_code == 400


def test_generic_json_reply_context_requires_http_url() -> None:
    provider = GenericJsonChannelProvider()
    good = {"x-channel-secret": "s3cret"}
    inbound = provider.verify_and_resolve(
        headers=good,
        raw_body=_payload(reply={"callback_url": "https://im.example/cb"}),
        binding=_BINDING,
    )
    assert inbound.reply_context == {"callback_url": "https://im.example/cb"}

    with pytest.raises(APIError) as bad_url:
        provider.verify_and_resolve(
            headers=good,
            raw_body=_payload(reply={"callback_url": "ftp://x"}),
            binding=_BINDING,
        )
    assert bad_url.value.status_code == 400


# ── webhook-service channel branch ───────────────────────────────────────────


class _RecordingChannel(ChannelProvider):
    name = "recorder"

    def __init__(self) -> None:
        self.delivered: list[tuple[dict[str, Any], str]] = []

    def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
        assert headers.get("x-ok") == "1", "provider must receive the raw headers"
        return ChannelInbound(
            content="mapped message",
            reply_context={"chat": "c-9"},
            ack_extra={"challenge": "echo-me"},
        )

    async def deliver_outbound(self, *, reply_context, text, binding) -> None:
        _ = binding
        self.delivered.append((reply_context, text))


class _FakeKernel:
    """Journal + per-turn assistant text, faked together (see the reliability
    test's twin): ``stream`` records the accepted command and the turn's
    assistant message so attach queries and turn-bound delivery reads work."""

    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []
        self.turn_texts: dict[tuple[str, str], str] = {}

    async def find_command_by_client_message_id(
        self, session_id: str, *, client_message_id: str
    ) -> dict[str, Any] | None:
        for c in self.commands:
            if (
                c["session_id"] == session_id
                and c["payload"]["client_message_id"] == client_message_id
            ):
                return c
        return None

    async def get_assistant_message_for_turn(
        self, session_id: str, *, turn_id: str
    ) -> dict[str, Any] | None:
        text = self.turn_texts.get((session_id, turn_id))
        return {"content": text} if text is not None else None

    async def resume_command_stream(
        self, user: Any, session_id: str, *, command_id: str
    ) -> Any:
        command = next(
            row for row in self.commands
            if row["session_id"] == session_id and row["causation_id"] == command_id
        )
        if (session_id, command["turn_id"]) not in self.turn_texts:
            raise RuntimeError("fixture command has no settled output")
        yield {"type": "finish"}

    def stream(self, user: Any, session_id: str, content: str, **kwargs: Any) -> Any:
        async def _agen() -> Any:
            command_id, turn_id = f"cmd-{uuid.uuid4().hex[:6]}", f"turn-{uuid.uuid4().hex[:6]}"
            self.commands.append(
                {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "causation_id": command_id,
                    "payload": {
                        "client_message_id": str(kwargs.get("client_message_id") or ""),
                        "content": content,
                    },
                }
            )
            self.turn_texts[(session_id, turn_id)] = "final assistant reply"
            yield {"type": "finish"}

        return _agen()


def _service(**overrides: Any) -> DeploymentService:
    webhook_row = {
        "deployment_id": "wh-1",
        "agent_id": "dep-1",
        "scene": "channel:recorder",
        "prompt_prefix": "PREFIX",
        "secret": "irrelevant",
        "enabled": True,
    }
    deployment_repo = AsyncMock()
    deployment_repo.get_by_id.return_value = webhook_row
    agent_repo = AsyncMock()
    agent_repo.get_agent.return_value = {"user_id": "creator-1", "template_name": "t"}
    agent_service = AsyncMock()
    agent_service.start_conversation.return_value = {"session_id": "sess-1"}
    sessions_repo = AsyncMock()
    sessions_repo.get_session.return_value = {"state": "READY"}
    kernel = _FakeKernel()

    spawned: list[Any] = []

    def _spawn(coro: Any, name: str = "") -> None:
        spawned.append(coro)

    ingress = ChannelIngressService(
        deployment_repo=deployment_repo,
        agent_repo=agent_repo,
        agent_service_getter=lambda: agent_service,
        stream_message_events_ds=kernel.stream,
        resume_command_stream=kernel.resume_command_stream,
        sessions_repo=sessions_repo,
        session_events_repo=kernel,
        message_view=kernel,
        session_detail_getter=AsyncMock(
            return_value={
                "state": "READY",
                "current_turn_id": None,
                "pending_interaction": None,
            }
        ),
        supersede_pending_interaction=AsyncMock(),
        spawn_background_task=_spawn,
    )
    service = DeploymentService(
        deployment_repo=deployment_repo,
        agent_repo=agent_repo,
        agent_service_getter=lambda: agent_service,
        stream_message_events_ds=kernel.stream,
        dispatch_turn_input=AsyncMock(),
        sessions_repo=sessions_repo,
        spawn_background_task=_spawn,
        agent_config=AsyncMock(),
        channel_ingress=ingress,
        **overrides,
    )
    service._test_spawned = spawned  # type: ignore[attr-defined]
    service._test_kernel = kernel  # type: ignore[attr-defined]
    return service


async def test_channel_trigger_maps_verifies_fires_and_delivers() -> None:
    provider = _RecordingChannel()
    register_channel(provider)

    service = _service()
    response = await service.trigger(
        "wh-1", headers={"x-ok": "1"}, raw_body=b"{}"
    )
    assert response["status"] == "accepted"
    assert response["session_id"] == "sess-1"
    assert response["challenge"] == "echo-me", "ack_extra must merge into the response"

    # Run the spawned background task to completion: readiness wait + drain +
    # outbound delivery of the last assistant message.
    spawned = service._test_spawned  # type: ignore[attr-defined]
    assert len(spawned) == 1
    await asyncio.wait_for(spawned[0], timeout=5)
    assert provider.delivered == [({"chat": "c-9"}, "final assistant reply")]


async def test_channel_create_rejects_unknown_provider() -> None:
    service = _service()
    with pytest.raises(APIError) as exc:
        await service.create(
            agent_id="dep-1",
            creator_user_id="u1",
            scene="channel:not-installed",
        )
    assert exc.value.status_code == 400
    assert "not-installed" in str(exc.value)


async def test_channel_create_accepts_registered_provider() -> None:
    register_channel(_RecordingChannel())
    service = _service()
    service._deployment_repo.upsert = AsyncMock(side_effect=lambda _id, doc: doc)
    created = await service.create(
        agent_id="dep-1", creator_user_id="u1", scene="channel:recorder"
    )
    assert created["scene"] == "channel:recorder"
    assert created["secret"]


async def test_channel_secret_is_returned_once_and_never_listed() -> None:
    register_channel(_RecordingChannel())
    service = _service()
    stored = {
        "deployment_id": "wh-1",
        "agent_id": "dep-1",
        "scene": "channel:recorder",
        "secret": "do-not-list-this",
    }
    service._deployment_repo.upsert = AsyncMock(return_value=stored)
    service._deployment_repo.list_by_agent = AsyncMock(return_value=[stored])

    created = await service.create(
        agent_id="dep-1",
        creator_user_id="u1",
        scene="channel:recorder",
        secret="do-not-list-this",
    )
    listed = await service.list_for_agent("dep-1")

    assert created["secret"] == "do-not-list-this"
    assert "secret" not in listed[0]
