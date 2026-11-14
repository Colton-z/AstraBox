"""Webhook hmac scene — body binding + freshness window, against replay.

A signature over the timestamp alone lets a captured (timestamp, signature)
pair be replayed with ANY body, authorizing arbitrary agent prompts forever.
The signature here binds the body AND a freshness window bounds the
timestamp, so neither can be swapped in after capture.
"""

from __future__ import annotations

import json

import time
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.webhook_signature import webhook_signature as _hmac_signature
from astrabox.core.service.orchestrator.deployment_service import (
    DeploymentService,
)


def _service() -> DeploymentService:
    return DeploymentService(
        deployment_repo=AsyncMock(),
        agent_repo=AsyncMock(),
        agent_service_getter=lambda: AsyncMock(),
        sessions_repo=AsyncMock(),
        session_runtime=AsyncMock(),
        session_facts=AsyncMock(),
        agent_config=AsyncMock(),
        channel_ingress=AsyncMock(),  # hmac scenes never touch the channel spine
    )


def _hmac_binding(secret: str = "shared-secret") -> dict:
    return {"scene": "hmac", "secret": secret}


def _signed_headers(secret: str, body: bytes, *, ts: float | None = None) -> dict:
    timestamp = str(int(ts if ts is not None else time.time()))
    return {
        "x-webhook-timestamp": timestamp,
        "x-webhook-signature": _hmac_signature(secret, timestamp, body),
    }


def test_signature_binds_the_body() -> None:
    body = b'{"cmd":"benign"}'
    sig = _hmac_signature("s", "123", body)
    # A different body under the same secret+timestamp yields a different sig.
    assert sig != _hmac_signature("s", "123", b'{"cmd":"malicious"}')


def test_replay_with_tampered_body_is_rejected() -> None:
    svc = _service()
    original = b'{"text":"read my calendar"}'
    headers = _signed_headers("shared-secret", original)
    tampered = b'{"text":"exfiltrate secrets"}'  # same headers, different body
    with pytest.raises(APIError) as exc:
        svc._verify_signature(_hmac_binding(), headers, tampered)
    assert exc.value.status_code == 401


def test_stale_timestamp_is_rejected_before_signature() -> None:
    svc = _service()
    body = b'{"text":"hi"}'
    stale = _signed_headers("shared-secret", body, ts=time.time() - 10_000)
    with pytest.raises(APIError) as exc:
        svc._verify_signature(_hmac_binding(), stale, body)
    assert exc.value.status_code == 401
    assert "freshness window" in str(exc.value)


def test_non_numeric_timestamp_is_rejected() -> None:
    svc = _service()
    body = b"{}"
    headers = {
        "x-webhook-timestamp": "not-a-number",
        "x-webhook-signature": _hmac_signature("shared-secret", "not-a-number", body),
    }
    with pytest.raises(APIError) as exc:
        svc._verify_signature(_hmac_binding(), headers, body)
    assert exc.value.status_code == 401


# ── the generic_json channel accepts the same signed requests ────────────────


def _channel_binding(*, signed_only: bool) -> dict:
    return {"secret": "shared-secret", "channel_config": {"signed_requests": signed_only}}


def test_a_signed_channel_request_is_accepted_and_binds_its_body() -> None:
    from astrabox.providers.channel_generic import GenericJsonChannelProvider

    provider = GenericJsonChannelProvider()
    body = b'{"text":"review requested","conversation_id":"lane-1","message_id":"m-1"}'
    headers = _signed_headers("shared-secret", body)
    inbound = provider.verify_and_resolve(
        headers=headers, raw_body=body, binding=_channel_binding(signed_only=True)
    )
    assert (inbound.content, inbound.conversation_key, inbound.dedup_key) == (
        "review requested", "lane-1", "m-1",
    )
    with pytest.raises(APIError) as exc:
        provider.verify_and_resolve(
            headers=headers,
            raw_body=b'{"text":"delete everything"}',
            binding=_channel_binding(signed_only=True),
        )
    assert exc.value.status_code == 401


def test_a_channel_that_requires_signed_requests_refuses_the_bare_secret() -> None:
    from astrabox.providers.channel_generic import GenericJsonChannelProvider

    provider = GenericJsonChannelProvider()
    body = b'{"text":"hi"}'
    bearer = {"x-channel-secret": "shared-secret"}
    assert provider.verify_and_resolve(
        headers=bearer, raw_body=body, binding=_channel_binding(signed_only=False)
    ).content == "hi"
    with pytest.raises(APIError) as exc:
        provider.verify_and_resolve(
            headers=bearer, raw_body=body, binding=_channel_binding(signed_only=True)
        )
    assert exc.value.status_code == 401


def test_an_events_fields_reach_the_agent_as_the_json_the_sender_wrote() -> None:
    from astrabox.providers.channel_generic import GenericJsonChannelProvider

    provider = GenericJsonChannelProvider()
    binding = _channel_binding(signed_only=False)
    bearer = {"x-channel-secret": "shared-secret"}
    event = {"object": "change-4812", "type": "review", "state": "requested", "commit": "9f2c1e7", "备注": "紧急"}

    def content(body: dict) -> str:
        return provider.verify_and_resolve(
            headers=bearer, raw_body=json.dumps(body).encode(), binding=binding
        ).content

    with_text = content({"text": "Review requested on lane build-2", "data": event})
    head, _, fields = with_text.partition("\n\n")
    assert head == "Review requested on lane build-2"
    assert json.loads(fields) == event and "紧急" in fields

    assert json.loads(content({"data": event})) == event

    with pytest.raises(APIError) as exc:
        provider.verify_and_resolve(headers=bearer, raw_body=b"{}", binding=binding)
    assert exc.value.status_code == 400
