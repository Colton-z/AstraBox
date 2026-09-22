"""Non-secret credential-delivery facts exposed to the console.

The console needs to explain where a saved value is used, but it must never
learn the value itself.  This module reports only deployment mode and delivery
mechanisms.  Human-facing wording stays in the frontend translation catalogue.
"""

from __future__ import annotations

import os
from typing import Any

from astrabox.common.utils.settings import load_astrabox_settings


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def deployment_mode() -> str:
    """Return the audience profile used only to choose explanatory copy.

    A non-local identity resolver means the deployment is prepared for more
    than the single local user, even if a developer also left local mode on.
    The profile does not enable, disable, or enforce Credential Vault.
    """

    identity = str(os.getenv("ASTRABOX_WEB_IDENTITY") or "local").strip().lower()
    if identity != "local":
        return "team"
    if _enabled(os.getenv("ASTRABOX_LOCAL_MODE")):
        return "local_development"
    return "trusted_private"


def credential_delivery_overview(*, settings: Any | None = None) -> dict[str, str]:
    """Describe credential paths without resolving or returning a secret."""

    current = settings or load_astrabox_settings()
    protected_model_delivery = bool(
        getattr(current, "sandbox_credential_vault_enabled", False)
    )
    return {
        "deployment_mode": deployment_mode(),
        "model_credentials": (
            "egress_placeholder" if protected_model_delivery else "sandbox_environment"
        ),
        "mcp_credentials": (
            "egress_injection" if protected_model_delivery else "unavailable"
        ),
        # This credential type is defined by outbound placeholder substitution;
        # with protected delivery off, attaching one is refused before a
        # sandbox is allocated rather than putting its real value in the box.
        "environment_credentials": (
            "egress_placeholder" if protected_model_delivery else "unavailable"
        ),
    }


__all__ = [
    "credential_delivery_overview",
    "deployment_mode",
]
