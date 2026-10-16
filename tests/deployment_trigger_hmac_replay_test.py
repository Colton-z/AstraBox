"""Webhook hmac scene — body binding + freshness window, against replay.

A signature over the timestamp alone lets a captured (timestamp, signature)
pair be replayed with ANY body, authorizing arbitrary agent prompts forever.
The signature here binds the body AND a freshness window bounds the
timestamp, so neither can be swapped in after capture.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.deployment_service import (
    DeploymentService,
    _hmac_signature,
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
