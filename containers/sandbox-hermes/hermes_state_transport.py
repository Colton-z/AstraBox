"""Transport opaque Hermes snapshots through the platform-issued capability."""

from __future__ import annotations

import base64
import binascii
import hashlib
from http.client import HTTPException
import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _post(target: dict[str, Any], operation: str, fields: dict[str, Any]) -> dict[str, Any]:
    base_url = target.get("base_url")
    owner = target.get("owner")
    if not isinstance(base_url, str) or not base_url or not isinstance(owner, dict):
        raise RuntimeError("Invalid runtime-state target")
    try:
        request = Request(
            base_url.rstrip("/") + "/" + operation,
            data=json.dumps({"owner": owner, **fields}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
    except (TypeError, ValueError):
        raise RuntimeError("Invalid runtime-state request") from None
    try:
        with urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError("Unexpected runtime-state HTTP status")
            body = response.read()
    except HTTPError as exc:
        status = exc.code
        exc.close()
        message = f"Runtime-state {operation} request failed: HTTP {status}"
        if status in (408, 429) or 500 <= status < 600:
            raise OSError(message) from None
        raise RuntimeError(message) from None
    except (OSError, HTTPException):
        raise OSError(f"Runtime-state {operation} request failed") from None
    except (TypeError, ValueError):
        raise RuntimeError("Invalid runtime-state request") from None
    try:
        envelope = json.loads(body)
    except (ValueError, UnicodeError):
        raise RuntimeError(f"Invalid runtime-state {operation} response encoding") from None
    if not isinstance(envelope, dict) or envelope.get("code") != "OK":
        raise RuntimeError(f"Invalid runtime-state {operation} response")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid runtime-state {operation} response data")
    return data


def load_snapshot(target: dict[str, Any]) -> dict[str, Any]:
    """Return snapshot_id and verified payload bytes, or explicit None values.

    Only a valid platform response declaring no saved snapshot returns None.
    Transient request failures raise OSError; permanent HTTP, malformed response,
    and integrity failures raise RuntimeError. Neither includes the capability
    URL or snapshot content.
    The platform-issued owner is forwarded unchanged.
    """
    data = _post(target, "load", {})
    if not {"snapshot_id", "payload_b64", "sha256", "size"}.issubset(data):
        raise RuntimeError("Incomplete runtime-state load response")
    snapshot_id = data["snapshot_id"]
    encoded = data["payload_b64"]
    digest = data["sha256"]
    size = data["size"]
    if type(size) is not int or size < 0:
        raise RuntimeError("Invalid runtime-state snapshot size")
    if snapshot_id is None:
        if encoded is not None or digest is not None or size != 0:
            raise RuntimeError("Inconsistent empty runtime-state response")
        return {"snapshot_id": None, "payload": None}
    if (
        not isinstance(snapshot_id, str)
        or not snapshot_id
        or not isinstance(encoded, str)
        or not isinstance(digest, str)
    ):
        raise RuntimeError("Invalid runtime-state snapshot metadata")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise RuntimeError("Invalid runtime-state snapshot encoding") from None
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
        raise RuntimeError("Runtime-state snapshot integrity mismatch")
    return {"snapshot_id": snapshot_id, "payload": payload}


def save_snapshot(target: dict[str, Any], payload: bytes, expected_snapshot_id: str | None) -> str:
    """Save snapshot bytes against the caller's predecessor and return its ID.

    The platform owns identity and conflict decisions. A conflict or any other
    permanent rejection raises RuntimeError. Transient request failures raise
    OSError. This function does not retry or treat failure as an absent snapshot.
    """
    if not isinstance(payload, bytes):
        raise RuntimeError("Runtime-state snapshot payload must be bytes")
    if expected_snapshot_id is not None and (
        not isinstance(expected_snapshot_id, str) or not expected_snapshot_id
    ):
        raise RuntimeError("Invalid runtime-state predecessor")
    data = _post(
        target,
        "save",
        {
            "payload_b64": base64.b64encode(payload).decode("ascii"),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "expected_snapshot_id": expected_snapshot_id,
        },
    )
    snapshot_id = data.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise RuntimeError("Invalid runtime-state save response")
    return snapshot_id
