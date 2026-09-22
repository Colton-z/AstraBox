"""Deployment-local signing key shared by browser identity adapters."""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

SESSION_SECRET_ENV = "ASTRABOX_AUTH_SESSION_SECRET"
SESSION_KEY_FILENAME = "auth-session.key"

_cached_secret: str | None = None


def _key_path() -> Path:
    from astrabox.config.settings import get_settings

    return get_settings().resolved_state_dir() / SESSION_KEY_FILENAME


def read_session_signing_secret() -> str:
    """Read the shared key without creating deployment state."""

    configured = str(os.getenv(SESSION_SECRET_ENV) or "").strip()
    if configured:
        return configured
    path = _key_path()
    try:
        value = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise RuntimeError(f"browser session signing key is unavailable at {path}") from exc
    if not value:
        raise RuntimeError(f"browser session signing key at {path} is empty")
    return value


def session_signing_secret() -> str:
    """Return the configured key or create one persistent local key."""

    global _cached_secret
    if _cached_secret:
        return _cached_secret
    configured = str(os.getenv(SESSION_SECRET_ENV) or "").strip()
    if configured:
        _cached_secret = configured
        return configured
    path = _key_path()
    if path.exists():
        value = path.read_text(encoding="ascii").strip()
        if value:
            _cached_secret = value
            return value
    value = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="ascii")
    os.chmod(path, 0o600)
    from astrabox.common.logger.logger_factory import get_logger

    get_logger(__name__).info("browser session signing key generated at %s", path)
    _cached_secret = value
    return value


def reset_session_signing_cache() -> None:
    """Clear the process cache for configuration reloads and isolated tests."""

    global _cached_secret
    _cached_secret = None


__all__ = [
    "SESSION_KEY_FILENAME",
    "SESSION_SECRET_ENV",
    "read_session_signing_secret",
    "reset_session_signing_cache",
    "session_signing_secret",
]
