"""``/api/v1/user/current`` — the resolved-identity echo endpoint.

Mounted by :func:`astrabox.api.app.create_app` via ``include_router``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.user_context import PLATFORM_ADMIN_ROLE
from astrabox.api.routes._shared import _resolve_user

router = APIRouter()


class CurrentUser(BaseModel):
    """The identity the request resolved to, as the console reads it.

    The three optional fields are what an identity provider filled in; the
    no-auth resolver asserts a user id and roles alone, so they arrive as null
    rather than being absent. The obligations that come with declaring a
    response model are in :mod:`astrabox.api.routes.response_envelope`.
    """

    model_config = ConfigDict(extra="allow")

    user_id: str
    display_name: str | None
    email: str | None
    avatar_url: str | None
    roles: list[str]
    is_admin: bool


@router.get(
    "/api/v1/user/current",
    response_model=ApiEnvelope[CurrentUser],
    response_model_exclude_unset=True,
)
async def get_current_user(request: Request):
    user = await _resolve_user(request)
    return success_response(
        {
            "user_id": user.user_id,
            "display_name": user.display_name,
            "email": user.email,
            "avatar_url": user.avatar_url,
            "roles": list(user.roles),
            # A UI capability, not an authorization decision: the backend
            # still enforces every admin route. Publishing the computed answer
            # keeps clients from re-encoding the internal role vocabulary.
            "is_admin": PLATFORM_ADMIN_ROLE in user.roles,
        }
    )
