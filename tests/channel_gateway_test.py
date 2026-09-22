"""Concrete channel providers over AstraBox's private adapter gateway."""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from websockets.asyncio.server import ServerConnection, serve

from astrabox.api.routes import deployments as deployment_routes
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.deployment_service import DeploymentService
from astrabox.core.service.orchestrator.channel_credentials import (
    ChannelCredentialService,
)
from astrabox.providers.channel_gateway import (
    ChannelGatewayClient,
    channel_gateway_base_url,
)
from astrabox.providers.channel_satori import (
    CHANNEL_PROVIDER_DEFINITIONS,
    GatewayChannelProvider,
)
from astrabox.seams.channel import (
    ChannelCallbackResponse,
    get_channel,
    register_channel,
    registered_channels,
)

_TOKEN = "channel-gateway-test-token-000000000000"


def _definition(name: str) -> dict[str, Any]:
    return next(
        dict(value)
        for value in CHANNEL_PROVIDER_DEFINITIONS
        if value.get("name") == name
    )


def _client(handler: Any) -> ChannelGatewayClient:
    if handler is None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("this test must not contact the channel gateway")

    return ChannelGatewayClient(
        base_url="http://127.0.0.1:8765",
        token=_TOKEN,
        transport=httpx.MockTransport(handler),
    )


def _message_event(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sn": 42,
        "type": "message-created",
        "timestamp": 1_700_000_000_000,
        "login": {
            "sn": 1,
            "platform": "telegram",
            "user": {"id": "bot-1"},
        },
        "channel": {"id": "room-1", "type": 1},
        "user": {"id": "user-1", "name": "Ada", "is_bot": False},
        "message": {
            "id": "msg-1",
            "content": (
                '<quote id="prior-1"><author id="bot-1"/></quote>'
                '<at id="bot-1"/> hello <img '
                'src="https://files.example.test/chart.png" title="chart"/>'
            ),
        },
    }
    payload.update(overrides)
    return payload


def test_catalog_registers_every_official_satori_adapter_product() -> None:
    expected = {str(value["name"]) for value in CHANNEL_PROVIDER_DEFINITIONS}
    assert len(expected) == 16
    assert expected <= set(registered_channels())
    assert "satori" in registered_channels()

    descriptor = get_channel("telegram").describe().to_dict()
    assert descriptor["scene"] == "channel:telegram"
    assert descriptor["setup_url"] == "https://t.me/BotFather"
    assert descriptor["callback_path"] is None
    assert descriptor["credential_fields"][0]["secret"] is True
    assert "token" not in descriptor


async def test_complete_binding_is_checked_by_the_authoritative_adapter_schema() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["header"] = request.headers["x-astrabox-gateway-token"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=captured["body"], request=request)

    provider = GatewayChannelProvider(_definition("telegram"), gateway=_client(handler))
    config, credentials = await provider.validate_configuration(
        config={}, credentials={"token": " bot-token "}
    )

    assert config == {}
    assert credentials == {"token": "bot-token"}
    assert captured == {
        "url": "http://127.0.0.1:8765/v1/providers/telegram/validate",
        "header": _TOKEN,
        "body": {"config": {}, "credentials": {"token": "bot-token"}},
    }


async def test_missing_named_credential_is_reported_as_a_credential_error() -> None:
    provider = GatewayChannelProvider(_definition("telegram"), gateway=_client(None))

    with pytest.raises(APIError) as raised:
        await provider.validate_configuration(config={}, credentials={})

    assert raised.value.code == "CHANNEL_CREDENTIALS_INVALID"


@pytest.mark.parametrize(
    "value",
    [
        "http://channels.example.test",
        "ftp://channels.example.test",
        "https://user:pass@channels.example.test",
        "https://channels.example.test?token=secret",
    ],
)
def test_external_gateway_address_rejects_unsafe_shapes(value: str) -> None:
    with pytest.raises(RuntimeError):
        channel_gateway_base_url(value)


def test_satori_event_projection_maps_into_the_channel_spine() -> None:
    provider = GatewayChannelProvider(_definition("telegram"), gateway=_client(None))
    payload = _message_event()
    inbound = provider._resolve_event(
        payload,
        raw_body=json.dumps(payload, sort_keys=True).encode(),
    )

    assert inbound.content == "hello [chart](https://files.example.test/chart.png)"
    assert inbound.dedup_key == "telegram:bot-1:message:msg-1"
    assert inbound.conversation_key == "telegram:bot-1:channel:room-1"
    assert inbound.reference == "telegram:bot-1:message:prior-1"
    assert inbound.participant == "telegram:user-1"
    assert inbound.attention is not None
    assert inbound.attention.is_direct_message is True
    assert inbound.attention.is_mention is True
    assert inbound.attention.is_reply_to_agent is True
    assert inbound.reply_context == {
        "platform": "telegram",
        "self_id": "bot-1",
        "channel_id": "room-1",
        "message_id": "msg-1",
    }


def test_control_events_and_bot_echoes_are_terminally_ignored() -> None:
    provider = GatewayChannelProvider(_definition("telegram"), gateway=_client(None))
    control = _message_event(type="login-updated")
    resolved = provider._resolve_event(control, raw_body=json.dumps(control).encode())
    assert resolved.ignore_reason == "channel event 'login-updated' is not message-created"
    assert resolved.dedup_key == "telegram:bot-1:event:login-updated:42"

    own = _message_event(user={"id": "bot-1", "is_bot": True})
    resolved = provider._resolve_event(own, raw_body=json.dumps(own).encode())
    assert resolved.ignore_reason == "message was emitted by the connected bot account"


async def test_source_identifies_with_sealed_credentials_and_acks_durable_cursor() -> None:
    received: list[dict[str, Any]] = []

    async def handler(connection: ServerConnection) -> None:
        assert connection.request.headers["x-astrabox-gateway-token"] == _TOKEN
        received.append(json.loads(await connection.recv()))
        await connection.send(json.dumps({"op": 4, "body": {"provider": "telegram"}}))
        await connection.send(json.dumps({"op": 0, "body": _message_event()}))
        received.append(json.loads(await connection.recv()))
        received.append(json.loads(await connection.recv()))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        gateway = ChannelGatewayClient(
            base_url=f"http://127.0.0.1:{port}", token=_TOKEN
        )
        provider = GatewayChannelProvider(_definition("telegram"), gateway=gateway)
        source = provider.open_source(
            binding={
                "deployment_id": "channel-1",
                "channel_config": {},
                "channel_credentials": {"token": "bot-token"},
                "callback_base_url": (
                    "https://agents.example.test/api/v1/deployments/"
                    "channel-1/callback"
                ),
                "source_cursor": 41,
            }
        )
        envelope = await anext(source)
        await envelope.ack({"status": "accepted"})
        await source.aclose()

    assert envelope.source_cursor == 42
    assert envelope.inbound.content.startswith("hello")
    assert received == [
        {
            "op": 3,
            "body": {
                "provider": "telegram",
                "config": {},
                "credentials": {"token": "bot-token"},
                "callback_base_url": (
                    "https://agents.example.test/api/v1/deployments/"
                    "channel-1/callback"
                ),
                "cursor": 41,
            },
        },
        {"op": 7, "body": {"cursor": 42}},
        {"op": 6, "body": {}},
    ]


async def test_delivery_returns_namespaced_platform_receipts() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"message_ids": ["reply-9"]},
            request=request,
        )

    provider = GatewayChannelProvider(_definition("telegram"), gateway=_client(handler))
    receipt = await provider.deliver_outbound(
        reply_context={
            "platform": "telegram",
            "self_id": "bot-1",
            "channel_id": "room-1",
            "message_id": "msg-1",
        },
        text="answer",
        binding={"deployment_id": "channel-1"},
    )
    assert receipt.message_ids == ["telegram:bot-1:message:reply-9"]
    assert captured["text"] == "answer"


async def test_callback_response_is_forwarded_byte_for_byte() -> None:
    body = b"official-platform-challenge"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["path"] == "/line"
        assert base64.b64decode(payload["body_base64"]) == b"challenge"
        return httpx.Response(
            200,
            json={
                "status": 201,
                "headers": {"content-type": "text/plain"},
                "body_base64": base64.b64encode(body).decode(),
            },
            request=request,
        )

    provider = GatewayChannelProvider(_definition("line"), gateway=_client(handler))
    response = await provider.forward_callback(
        deployment_id="channel-1",
        method="POST",
        path="/line",
        query="signature=ok",
        headers={"content-type": "text/plain"},
        raw_body=b"challenge",
    )
    assert response.status_code == 201
    assert response.headers == {"content-type": "text/plain"}
    assert response.body == body


async def test_channel_credentials_are_encrypted_and_hydrated_only_at_use() -> None:
    store = AsyncMock()
    service = ChannelCredentialService(store=store)
    store.get.return_value = json.dumps({"token": "sealed-token"})
    provider = GatewayChannelProvider(_definition("telegram"), gateway=_client(None))

    await service.put("channel-1", {"token": "sealed-token"})
    put = store.put.await_args.kwargs
    assert put["scope"] == "deployment/channel-1"
    assert json.loads(put["value"]) == {"token": "sealed-token"}

    hydrated = await service.hydrate(
        {"deployment_id": "channel-1", "channel_config": {}}, provider
    )
    assert hydrated["channel_credentials"] == {"token": "sealed-token"}


async def test_create_seals_named_credentials_and_never_returns_them() -> None:
    from tests.channel_provider_test import _service

    definition = {**_definition("telegram"), "name": "managed-test"}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(200, json=payload, request=request)

    register_channel(GatewayChannelProvider(definition, gateway=_client(handler)))
    credentials = AsyncMock()
    service: DeploymentService = _service(channel_credentials=credentials)
    service._deployment_repo.upsert = AsyncMock(side_effect=lambda _id, doc: doc)

    created = await service.create(
        agent_id="agent-1",
        creator_user_id="owner-1",
        scene="channel:managed-test",
        credentials={"token": "platform-secret"},
        callback_base_url="https://agents.example.test",
    )

    deployment_id = created["deployment_id"]
    credentials.put.assert_awaited_once_with(
        deployment_id, {"token": "platform-secret"}
    )
    assert created["credentials_configured"] is True
    assert "secret" not in created
    assert "platform-secret" not in json.dumps(created)


async def test_callback_provider_derives_one_public_url_from_the_request_origin() -> None:
    from tests.channel_provider_test import _service

    definition = {**_definition("line"), "name": "managed-callback-test"}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(200, json=payload, request=request)

    register_channel(GatewayChannelProvider(definition, gateway=_client(handler)))
    service: DeploymentService = _service(channel_credentials=AsyncMock())
    service._deployment_repo.upsert = AsyncMock(side_effect=lambda _id, doc: doc)
    created = await service.create(
        agent_id="agent-1",
        creator_user_id="owner-1",
        scene="channel:managed-callback-test",
        credentials={"token": "line-token", "secret": "line-secret"},
        callback_base_url="https://agents.example.test/",
    )
    assert created["callback_base_url"] == (
        "https://agents.example.test/api/v1/deployments/"
        f"{created['deployment_id']}/callback"
    )


async def test_public_callback_route_preserves_body_status_and_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock()
    service.forward_channel_callback.return_value = ChannelCallbackResponse(
        status_code=202,
        headers={
            "content-type": "text/plain",
            "x-platform-response": "ok",
            "connection": "close",
        },
        body=b"challenge accepted",
    )
    monkeypatch.setattr(deployment_routes, "get_platform_service", lambda: service)
    monkeypatch.setattr(deployment_routes, "_registered_on", None)
    app = FastAPI()
    deployment_routes.register_deployment_routes(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://agents.example.test",
    ) as client:
        response = await client.post(
            "/api/v1/deployments/channel-1/callback/line?signature=ok",
            headers={"authorization": "Bearer homeserver-proof"},
            content=b"platform challenge",
        )

    assert response.status_code == 202
    assert response.content == b"challenge accepted"
    assert response.headers["x-platform-response"] == "ok"
    assert response.headers.get("connection") != "close"
    service.forward_channel_callback.assert_awaited_once()
    call = service.forward_channel_callback.await_args
    assert call.args == ("channel-1",)
    assert call.kwargs["path"] == "/line"
    assert call.kwargs["query"] == "signature=ok"
    assert call.kwargs["headers"]["authorization"] == "Bearer homeserver-proof"
    assert call.kwargs["raw_body"] == b"platform challenge"
