from __future__ import annotations

from typing import Any


def api_retry_payload(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Return the visible fields of one Claude ``system/api_retry`` event.

    Runner serialization wraps the SDK SystemMessage once, while stored SDK
    content can already be the inner ``type=system`` object. Both are decoded
    here at the Claude-owned boundary; the public transport never receives the
    raw vendor envelope.
    """

    subtype = str(raw.get("subtype") or "").strip().lower()
    payload = raw.get("data")
    candidate = dict(payload) if isinstance(payload, dict) else dict(raw)
    candidate_subtype = str(candidate.get("subtype") or "").strip().lower()
    if subtype != "api_retry" and candidate_subtype != "api_retry":
        return None

    visible: dict[str, Any] = {}
    for key in ("attempt", "max_retries", "error_status"):
        value = candidate.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            visible[key] = value
    error = candidate.get("error")
    if isinstance(error, str) and error:
        visible["error"] = error
    return visible
