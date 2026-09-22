"""Interaction broker relay.

Handles respond_to_interaction and get_pending_interaction_view
which are pure HTTP relays to the sandbox-side interaction broker.
"""

import base64
import json
import zlib
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.seams.sandbox import (
    sandbox_for_name,
)

logger = get_logger(__name__)


def encode_interaction_payload(payload: dict[str, Any]) -> str:
    """Encode interaction payload for GET URL path.

    The sandbox_ws_server uses websockets' process_request hook which only
    supports GET (no POST body reading).  Zlib compression keeps the URL
    well under nginx's default 8KB limit even for large tool inputs.
    """
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    compressed = zlib.compress(raw, level=9)
    if len(compressed) + 2 < len(raw):
        return "z:" + base64.urlsafe_b64encode(compressed).decode().rstrip("=")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


async def respond_to_interaction(
    session_id: str,
    *,
    interaction_id: str,
    payload: dict[str, Any],
    endpoint: str,
    backend: str,
) -> dict[str, Any]:
    body = {
        "interaction_id": interaction_id,
        **dict(payload or {}),
    }
    encoded = encode_interaction_payload(body)
    response = await sandbox_for_name(backend).build_dataplane(
        endpoint=endpoint, port=8000
    ).request(
        "GET", f"/interaction/{session_id}/respond/{encoded}", timeout=15.0
    )
    try:
        result = response.json()
    except Exception as exc:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"interaction broker returned invalid JSON: {exc}",
            status_code=502,
        ) from exc
    if response.status_code != 200 or not isinstance(result, dict) or not result.get("ok"):
        message = ""
        if isinstance(result, dict):
            message = str(result.get("error") or "").strip()
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=message or f"interaction broker respond failed: {response.status_code}",
            status_code=409 if response.status_code == 409 else 502,
        )
    return result


async def get_pending_interaction_view(
    session_id: str,
    *,
    endpoint: str,
    backend: str,
) -> dict[str, Any] | None:
    try:
        response = await sandbox_for_name(backend).build_dataplane(
            endpoint=endpoint, port=8000
        ).request(
            "GET", f"/interaction/{session_id}/pending", timeout=5.0
        )
    except Exception as exc:
        logger.warning(
            "pending interaction probe failed session=%s endpoint=%s err=%s",
            session_id,
            endpoint,
            exc,
        )
        return None
    if response.status_code != 200:
        logger.warning(
            "pending interaction probe returned status=%s session=%s endpoint=%s",
            response.status_code,
            session_id,
            endpoint,
        )
        return None
    try:
        result = response.json()
    except Exception as exc:
        logger.warning(
            "pending interaction probe returned invalid JSON session=%s endpoint=%s err=%s",
            session_id,
            endpoint,
            exc,
        )
        return None
    return result if isinstance(result, dict) else None
