"""The generic_json reference provider's inbound checks and the channel secrets.

* the provider's inbound auth refuses a bad secret or payload, and its reply
  context accepts only an HTTP(S) URL;
* a channel secret is returned once at create and never listed;
* who a binding's messages run as is validated against its provider.
"""

from __future__ import annotations

import json
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
    EXECUTION_LINKED,
    ChannelDescriptor,
    ChannelInbound,
    ChannelProvider,
    register_channel,
)


# ── generic_json provider ────────────────────────────────────────────────────

_BINDING = {"secret": "s3cret", "agent_id": "dep-1", "prompt_prefix": ""}


def _payload(**extra: Any) -> bytes:
    return json.dumps({"text": "hello agent", **extra}).encode()


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
    agent_repo.get_agent.return_value = {"created_by": "creator-1", "template_name": "t"}
    agent_service = AsyncMock()
    sessions_repo = AsyncMock()
    ingress = ChannelIngressService(
        deployment_repo=deployment_repo,
        agent_repo=agent_repo,
        agent_service_getter=lambda: agent_service,
        sessions_repo=sessions_repo,
        session_runtime=AsyncMock(),
        session_facts=AsyncMock(),
        spawn_background_task=lambda coro, name="": coro.close(),
    )
    return DeploymentService(
        deployment_repo=deployment_repo,
        agent_repo=agent_repo,
        agent_service_getter=lambda: agent_service,
        sessions_repo=sessions_repo,
        session_runtime=AsyncMock(),
        session_facts=AsyncMock(),
        agent_config=AsyncMock(),
        channel_ingress=ingress,
        **overrides,
    )


async def test_channel_secret_is_returned_once_and_never_listed() -> None:
    register_channel(_RecordingChannel())
    service = _service()
    stored = {
        "deployment_id": "wh-1",
        "agent_id": "dep-1",
        "scene": "channel:recorder",
        "secret": "do-not-list-this",
    }
    service._deployment_repo.create = AsyncMock(return_value=stored)
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


class _TicketChannel(_RecordingChannel):
    """A platform whose work belongs to a person: it runs only as linked accounts."""

    name = "tickets"

    def describe(self) -> ChannelDescriptor:
        return ChannelDescriptor(
            name=self.name, label="Tickets", execution_modes=(EXECUTION_LINKED,),
        )


async def test_who_a_channel_binding_runs_as_is_validated_against_its_provider() -> None:
    """A binding runs its messages as the Agent's owner, one chosen account, or
    each participant's linked account. Only an administrator chooses anything
    but the owner; a provider whose work belongs to a person allows only
    linked accounts, so no binding of it can run as the owner; and an
    Assistant always runs as its owner."""
    from astrabox.common.utils.user_context import UserContext

    register_channel(_RecordingChannel())
    register_channel(_TicketChannel())
    service = _service()
    service._deployment_repo.create = AsyncMock(side_effect=lambda _id, doc: doc)
    admin = UserContext(user_id="admin", roles=["admin"])
    owner = UserContext(user_id="owner")

    async def create(scene: str, actor: UserContext, **identity: Any) -> dict[str, Any]:
        return await service.create(
            agent_id="dep-1", creator_user_id=actor.user_id, scene=scene, actor=actor,
            **identity,
        )

    plain = await create("channel:recorder", owner)
    assert (plain["execution_mode"], plain["execution_user_id"]) == ("owner", "")
    chosen = await create(
        "channel:recorder", admin, execution_mode="account", execution_user_id="alice",
    )
    assert (chosen["execution_mode"], chosen["execution_user_id"]) == ("account", "alice")
    tickets = await create("channel:tickets", admin, execution_mode="linked")
    assert (tickets["execution_mode"], tickets["execution_user_id"]) == ("linked", "")
    refused = [
        ("channel:recorder", owner, {"execution_mode": "linked"}, 403),
        ("channel:recorder", admin, {"execution_mode": "account"}, 400),
        ("channel:recorder", admin, {"execution_user_id": "alice"}, 400),
        ("channel:recorder", admin, {"execution_mode": "linked", "execution_user_id": "a"}, 400),
        ("channel:recorder", admin, {"execution_mode": "everyone"}, 400),
        ("channel:tickets", admin, {}, 400),
        ("channel:tickets", admin, {"execution_mode": "owner"}, 400),
    ]
    for scene, actor, identity, status in refused:
        with pytest.raises(APIError) as error:
            await create(scene, actor, **identity)
        assert error.value.status_code == status, (scene, identity)
    with pytest.raises(APIError) as on_assistant:
        await service.create(
            assistant_id="as-1", creator_user_id="admin", scene="channel:recorder",
            actor=admin, execution_mode="linked", allowed_participants=["im:me"],
        )
    assert on_assistant.value.status_code == 400
