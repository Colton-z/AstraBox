"""Concrete channel providers over AstraBox's private adapter gateway."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

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
    register_channel,
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
    service._deployment_repo.create = AsyncMock(side_effect=lambda _id, doc: doc)

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
