"""Require persisted compute ownership before touching a physical runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.config.settings import get_settings


def require_execution_target(
    record: Mapping[str, Any], *, execution_target: str | None = None,
) -> None:
    """Refuse missing ownership or a runtime belonging to another physical target."""
    owner = record.get("execution_target")
    if not isinstance(owner, str) or not owner:
        raise APIError(
            code="EXECUTION_TARGET_MISSING", status_code=500,
            message="runtime has no persisted execution target",
        )
    current = get_settings().execution_target if execution_target is None else execution_target
    if owner != current:
        raise APIError(
            code="EXECUTION_TARGET_MISMATCH", status_code=409,
            message="runtime belongs to a different execution target",
            data={"execution_target": owner, "server_execution_target": current},
        )
