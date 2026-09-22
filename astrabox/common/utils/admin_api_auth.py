"""Admin-API access control.

Admin-API routes are open when ``ASTRABOX_ADMIN_API_TOKEN`` is unset. Setting
the token requires a matching bearer credential on every route. Request
identity is independent and comes from the shared ``astrabox.web.identity``
seam.
"""

from __future__ import annotations

from fastapi import Request


def bootstrap_admin_api_registry() -> None:
    """No-op because bearer configuration is read directly from the environment."""


async def require_admin_api_bearer(request: Request) -> None:
    """Optional hard credential for the automation surface.

    Open by default (single-tenant; the host is yours). Setting
    ``ASTRABOX_ADMIN_API_TOKEN`` locks every admin-api route behind
    ``Authorization: Bearer <token>`` with a constant-time compare.
    """
    import hmac
    import os

    expected = str(os.getenv("ASTRABOX_ADMIN_API_TOKEN", "") or "").strip()
    if not expected:
        return
    supplied = str(request.headers.get("authorization") or "").strip()
    prefix, _, token = supplied.partition(" ")
    if prefix.lower() != "bearer" or not hmac.compare_digest(
        token.strip(), expected
    ):
        from astrabox.common.utils.errors import APIError

        raise APIError(
            code="ADMIN_API_TOKEN_REQUIRED",
            message="admin api requires a valid bearer token",
            status_code=401,
        )
