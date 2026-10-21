"""Require persisted compute ownership before touching a physical runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.config.settings import get_settings


def require_environment_ownership(
    record: Mapping[str, Any], *, environment_name: str | None = None,
) -> None:
    """Refuse missing ownership or a runtime belonging to another Environment."""
    owner = record.get("environment_name")
    if not isinstance(owner, str) or not owner:
        raise APIError(
            code="ENVIRONMENT_MISSING", status_code=500,
            message="runtime has no persisted Environment",
        )
    current = get_settings().environment_name if environment_name is None else environment_name
    if owner != current:
        raise APIError(
            code="ENVIRONMENT_MISMATCH", status_code=409,
            message="runtime belongs to a different Environment",
            data={"environment_name": owner, "server_environment_name": current},
        )
