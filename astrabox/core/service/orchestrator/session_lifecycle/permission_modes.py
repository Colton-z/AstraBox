"""Which permission modes a session may use, in its engine's own vocabulary.

A session's mode is stored on its row and applied by its reader (see
``SessionRepository.set_permission_mode``); these functions only decide
whether a requested mode is one the engine offers.
"""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.capabilities import (
    capabilities_for_engine_kind,
    default_permission_mode_for_engine,
    require_session_kind,
)
from astrabox.core.service.orchestrator.engine_kind_utils import resolve_session_engine_kind


def _requested(value: str | None) -> str | None:
    raw = str(value or "").strip()
    return raw or None


def resolve_initial_mode(
    requested_mode: str | None, *, session_kind: str | None, engine_kind: str
) -> str | None:
    """The mode a new session starts in: the requested one, or the engine's default for its kind.

    Registration decides here, because no engine has connected yet.
    """
    requested = _requested(requested_mode)
    capabilities = capabilities_for_engine_kind(engine_kind)
    if not capabilities.permission_modes:
        if requested is not None:
            raise APIError(
                code="ENGINE_CAPABILITY_UNAVAILABLE",
                message=f"engine_kind={engine_kind!r} has no permission-mode capability",
                status_code=400,
            )
        return None
    effective = requested or default_permission_mode_for_engine(engine_kind, require_session_kind(session_kind))
    if effective is None:
        raise APIError(
            code="ENGINE_PERMISSION_MODE_REQUIRED",
            message=f"engine_kind={engine_kind!r} requires an explicit permission mode for this session kind",
            status_code=400,
        )
    if effective not in capabilities.permission_modes:
        raise APIError(
            code="INVALID_REQUEST",
            message=(
                f"permission_mode={effective!r} is not supported by engine_kind={engine_kind!r}; "
                f"available={list(capabilities.permission_modes)!r}"
            ),
            status_code=400,
        )
    return effective


def validate_mode_for_session(value: str, *, session: dict[str, Any]) -> str:
    """A requested mode, checked against the manifest the session's engine reported at startup.

    The connected engine's manifest is the authority once there is one: a
    registration table would reject a mode the vendor has just added.
    """
    desired = _requested(value)
    if desired is None:
        raise APIError(code="INVALID_REQUEST", message="permission_mode is required", status_code=400)
    engine_kind = resolve_session_engine_kind(session)
    stored = session.get("engine_capabilities")
    if not isinstance(stored, dict) or str(stored.get("engine_kind") or "") != engine_kind:
        raise APIError(
            code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
            message="session has no verified engine capability manifest for its engine",
            status_code=502,
        )
    modes = stored.get("permission_modes")
    if not isinstance(modes, list) or not modes:
        raise APIError(
            code="ENGINE_CAPABILITY_UNAVAILABLE",
            message=f"engine_kind={engine_kind!r} has no permission-mode capability",
            status_code=400,
        )
    if desired not in modes:
        raise APIError(
            code="INVALID_REQUEST",
            message=(
                f"permission_mode={desired!r} is not supported by engine_kind={engine_kind!r}; "
                f"available={list(modes)!r}"
            ),
            status_code=400,
        )
    return desired
