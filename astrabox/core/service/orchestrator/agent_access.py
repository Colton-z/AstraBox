"""Per-agent access control: visibility + management authorization.

Each Agent carries access-control fields written only through its dedicated
access operation:

- ``created_by``: the creator's ``user_id``. Stamped once on insert and
  never accepted from a client.
- ``admins``: list of user_ids the creator/admins granted co-management.
- ``visibility``: one of ``public`` / ``private`` / ``allowlist``.
- ``allowed_user_ids``: list of user_ids granted *use* access when visibility is
  ``allowlist`` (ignored otherwise).

Two decisions, both keyed on the viewer's ``user_id`` (``UserContext.user_id``):

- **can_view**: may this user see/use the agent (list it, start sessions with it)?
- **can_manage**: may this user manage the agent — view every one of its
  conversations and edit its access-control config? (creator + admins)

This module is pure (no I/O) so it is the single, unit-testable source of truth
for both the list/create paths and the session-authorization path. An unset
``visibility`` normalizes to ``public``; an absent ``created_by`` means nobody
manages the Agent and never grants access implicitly.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import PLATFORM_ADMIN_ROLE

VISIBILITY_PUBLIC = "public"
VISIBILITY_PRIVATE = "private"
VISIBILITY_ALLOWLIST = "allowlist"
VISIBILITY_VALUES = (VISIBILITY_PUBLIC, VISIBILITY_PRIVATE, VISIBILITY_ALLOWLIST)

# Access-control keys an editor may set on an Agent. ``created_by`` is absent by
# design — it is stamped once on insert and never accepted from a client payload.
ACCESS_CONTROL_KEYS = ("admins", "visibility", "allowed_user_ids")


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _norm_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [s for s in (_norm(v) for v in value) if s]


def normalize_visibility(raw: Any) -> str:
    """Map a stored visibility value to a known mode.

    An unset/blank/unknown value resolves to ``public``, the default for a
    document that carries no visibility field.
    """
    v = _norm(raw).lower()
    return v if v in VISIBILITY_VALUES else VISIBILITY_PUBLIC


def agent_created_by(template_doc: dict[str, Any]) -> str:
    return _norm(template_doc.get("created_by"))


def agent_admins(template_doc: dict[str, Any]) -> list[str]:
    return _norm_list(template_doc.get("admins"))


def agent_allowed_user_ids(template_doc: dict[str, Any]) -> list[str]:
    return _norm_list(template_doc.get("allowed_user_ids"))


def is_platform_admin(viewer_roles: Sequence[str]) -> bool:
    """True if the viewer holds the deployment's ``admin`` role.

    This is the role an IdP grants through group mapping (see
    ``UserContext.roles``), not membership of one agent's own ``admins`` list.
    The two are separate powers: :func:`can_manage_agent` grants management on
    either one, and neither implies the other.
    """
    return any(_norm(role) == PLATFORM_ADMIN_ROLE for role in viewer_roles or ())


def can_manage_agent(
    template_doc: dict[str, Any],
    viewer_user_id: str,
    viewer_roles: Sequence[str],
) -> bool:
    """True if the viewer may edit this agent.

    Manage = view all conversations under the agent + edit its harness and
    access-control config. Three ways to hold it: the deployment's ``admin``
    role, being the creator, or being named in the agent's own ``admins``.

    System-created seeded Agents carry ``created_by="system"`` and may have no
    Agent-level administrators. ``viewer_roles`` is therefore required so every
    caller supplies the platform-role context that can authorize their
    management; the type checker rejects calls that omit it.
    """
    if is_platform_admin(viewer_roles):
        return True
    me = _norm(viewer_user_id)
    if not me:
        return False
    return me == agent_created_by(template_doc) or me in agent_admins(template_doc)


def can_view_agent(
    template_doc: dict[str, Any],
    viewer_user_id: str,
    viewer_roles: Sequence[str],
) -> bool:
    """True if the viewer may see/use this agent.

    public      -> everyone
    private     -> platform admins + creator + agent admins
    allowlist   -> the above + allowed_user_ids
    """
    mode = normalize_visibility(template_doc.get("visibility"))
    if mode == VISIBILITY_PUBLIC:
        return True
    if can_manage_agent(template_doc, viewer_user_id, viewer_roles):
        return True
    if mode == VISIBILITY_ALLOWLIST:
        me = _norm(viewer_user_id)
        return bool(me) and me in agent_allowed_user_ids(template_doc)
    # private
    return False


def _invalid(message: str) -> APIError:
    return APIError(code="INVALID_REQUEST", message=message, status_code=400)


def sanitize_access_control_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize the closed, dedicated access request.

    Unknown fields fail instead of being ignored. Returns the normalized access
    fields suitable to merge into the stored document.
    """
    unknown = sorted(set(payload) - set(ACCESS_CONTROL_KEYS))
    if unknown:
        raise _invalid(
            f"agent access contains unsupported fields: {', '.join(unknown)}"
        )

    out: dict[str, Any] = {}
    if "visibility" in payload:
        v = _norm(payload.get("visibility")).lower()
        if v not in VISIBILITY_VALUES:
            raise _invalid(f"visibility must be one of {list(VISIBILITY_VALUES)}")
        out["visibility"] = v
    for key in ("admins", "allowed_user_ids"):
        if key in payload:
            raw = payload.get(key)
            if not isinstance(raw, list) or any(
                not isinstance(user_id, str) or not user_id.strip()
                for user_id in raw
            ):
                raise _invalid(
                    f"{key} must be a list of non-empty user-id strings"
                )
            out[key] = _norm_list(raw)
    return out
