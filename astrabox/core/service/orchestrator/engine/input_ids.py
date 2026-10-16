"""The identities an engine adapter derives from a platform input id."""

from __future__ import annotations

import uuid


def input_response_message_id(input_id: str) -> str:
    """The stable UI message id of the response that answers one platform input."""
    try:
        normalized = uuid.UUID(str(input_id or "").strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("engine input_id must be a valid UUID") from exc
    return str(uuid.uuid5(normalized, "astrabox-sdk-response"))
