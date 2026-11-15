"""The signed-request scheme of inbound webhooks.

A caller signs each request with the trigger's secret:
``X-Webhook-Signature = Base64(HMAC-SHA256(secret, "<timestamp>.<sha256 hex of the body>"))``
with ``X-Webhook-Timestamp`` in unix epoch seconds. The signature binds the
timestamp and the exact body, so a captured pair cannot carry another body, and
the freshness window ends its use: the body becomes a message to an Agent.
Within the window the same request verifies again; a receiver that needs
exactly-once keeps a dedup key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from collections.abc import Mapping


def signature_window_seconds() -> int:
    """How far a signed request's timestamp may be from now (default 300 s)."""
    try:
        return max(1, int(os.getenv("ASTRABOX_WEBHOOK_HMAC_WINDOW_SECONDS", "300")))
    except ValueError:
        return 300


def webhook_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """The signature a caller holding ``secret`` sends for this timestamp and body."""
    body_digest = hashlib.sha256(raw_body or b"").hexdigest()
    signed = f"{timestamp}.{body_digest}"
    mac = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def signed_request_refusal(secret: str, headers: Mapping[str, str], raw_body: bytes) -> str | None:
    """Why a request is not a fresh one signed with ``secret``; ``None`` when it is.

    The stale timestamp is refused before the signature is compared, so a
    captured request is unusable once the window lapses.
    """
    timestamp = str(headers.get("x-webhook-timestamp") or "")
    signature = str(headers.get("x-webhook-signature") or "")
    if not timestamp or not signature:
        return "missing X-WEBHOOK-TIMESTAMP / X-WEBHOOK-SIGNATURE"
    window = signature_window_seconds()
    try:
        sent_at = float(timestamp)
    except ValueError:
        return "X-WEBHOOK-TIMESTAMP must be unix epoch seconds"
    if abs(time.time() - sent_at) > window:
        return f"webhook timestamp outside the {window}s freshness window"
    if not secret or not hmac.compare_digest(webhook_signature(secret, timestamp, raw_body), signature):
        return "invalid webhook signature"
    return None
