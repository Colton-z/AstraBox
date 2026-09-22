"""Per-request / per-user context.

There is no API auth by default, so nothing populates a per-request user and there
is no cookie/token to verify. Identity is held in a task-local
:class:`contextvars.ContextVar`:

* callers (or tests) may bind a user via :func:`set_current_user_context`;
* :func:`get_current_user_context` **always returns a user** and never raises
  ``UNAUTHORIZED``. With nothing set on the context it yields a default identity
  built from the environment (:func:`_build_local_debug_user`).

The default resolver treats every request as a single local user; there is no auth
lookup to fail. An authenticated deployment selects a resolver through
``astrabox.web.identity``. It binds the resolved identity through the same
:func:`set_current_user_context` seam, so consumers only read
:func:`get_current_user_context` and :class:`UserContext` attributes.

``contextvars`` is task-local: each asyncio task / request that sets the context
sees its own value, and concurrent requests do not bleed into one another.
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token

from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)


#: The organization assigned when an identity resolver supplies no override.
#: Self-hosted instances default to one organization; override it with
#: ``ASTRABOX_DEFAULT_ORG``.
DEFAULT_ORG_ID = "default"

#: The one internal role that grants deployment-wide administration. Identity
#: providers map their configurable external group onto this fixed vocabulary;
#: resource policies and route gates consume the same value.
PLATFORM_ADMIN_ROLE = "admin"

#: OAuth scopes accepted by the HTTP identity gate. They are platform
#: vocabulary rather than provider vocabulary: an IdP grants the strings and
#: AstraBox decides which routes each string authorizes.
API_READ_SCOPE = "astrabox:read"
API_WRITE_SCOPE = "astrabox:write"
API_ADMIN_SCOPE = "astrabox:admin"


def default_org_id() -> str:
    """The deployment's default organization id (env-overridable)."""
    return str(os.getenv("ASTRABOX_DEFAULT_ORG", "")).strip() or DEFAULT_ORG_ID


class UserContext:
    """Identity bound to the current request/task.

    Carries ``user_id`` plus an optional ``display_name``, ``email`` and
    ``avatar_url``; consumers construct it by keyword or read these attributes.

    ``org_id`` and ``roles`` are the multi-user dimensions an auth-backed
    resolver fills from the IdP (group/claim mapping): ``org_id`` defaults to
    the deployment org, ``roles`` to empty. :data:`PLATFORM_ADMIN_ROLE` is what
    the admin-surface gate checks once a real identity is asserted. The no-auth
    resolver explicitly grants that role to its one trusted local identity.
    ``api_scopes`` is ``None`` for a browser identity and a concrete list for a
    scoped machine token, including an empty list when that token grants no
    AstraBox operation.
    """

    def __init__(
        self,
        user_id: str,
        display_name: str | None = None,
        email: str | None = None,
        avatar_url: str | None = None,
        org_id: str | None = None,
        roles: list[str] | None = None,
        api_scopes: list[str] | None = None,
    ) -> None:
        self.user_id = user_id
        self.display_name = display_name
        self.email = email
        self.avatar_url = avatar_url
        self.org_id = str(org_id or "").strip() or default_org_id()
        self.roles = [str(r).strip() for r in (roles or []) if str(r).strip()]
        self.api_scopes = (
            [str(scope).strip() for scope in api_scopes if str(scope).strip()]
            if api_scopes is not None
            else None
        )


#: Task-local current user. ``None`` means "nothing explicitly set" — readers then
#: materialize the default identity at read time (so an env override applies
#: without an import-time freeze). Set this via :func:`set_current_user_context`.
_CURRENT_USER: ContextVar[UserContext | None] = ContextVar(
    "astrabox_user_context", default=None
)


def _is_local_mode() -> bool:
    value = str(os.getenv("ASTRABOX_LOCAL_MODE", "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _is_local_debug_user_enabled() -> bool:
    if not _is_local_mode():
        return False
    value = str(os.getenv("ASTRABOX_LOCAL_DEBUG_USER_ENABLED", "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _build_local_debug_user() -> UserContext:
    """Build the default identity from the environment.

    Pure-stdlib and env-driven: all inputs are plain env vars — no secret manager,
    no auth backend. Used by :func:`get_current_user_context` as the fallthrough
    when no user has been set on the context.
    """
    user_id = (
        str(os.getenv("ASTRABOX_LOCAL_USER_ID", "")).strip()
        or str(os.getenv("USER", "")).strip()
        or "local-user"
    )
    display_name = (
        str(os.getenv("ASTRABOX_LOCAL_DISPLAY_NAME", "")).strip() or user_id
    )
    email = str(os.getenv("ASTRABOX_LOCAL_EMAIL", "")).strip() or None
    avatar_url = str(os.getenv("ASTRABOX_LOCAL_AVATAR_URL", "")).strip() or None
    return UserContext(
        user_id=user_id,
        display_name=display_name,
        email=email,
        avatar_url=avatar_url,
    )


def _read_context_from_thread_local() -> UserContext | None:
    """Read the user bound to the current task, or ``None`` if none is set.

    Reads the local :data:`_CURRENT_USER` ContextVar. Contract is
    ``UserContext | None``.
    """
    return _CURRENT_USER.get()


def set_current_user_context(user: UserContext) -> Token[UserContext | None]:
    """Bind *user* to the current task and return a reset token.

    An auth-backed resolver sets the resolved identity here, and
    :func:`get_current_user_context` / :func:`_read_context_from_thread_local` then
    observe it for the duration of the task. Pass the returned token to
    :func:`reset_current_user_context` to restore the previous value.
    """
    return _CURRENT_USER.set(user)


def reset_current_user_context(token: Token[UserContext | None]) -> None:
    """Restore the context user to the value captured before ``token`` was issued."""
    _CURRENT_USER.reset(token)


def get_asserted_user_context() -> UserContext | None:
    """The identity a resolver asserted for this request, or ``None``.

    Unlike :func:`get_current_user_context` this never fabricates the default
    identity: an unstamped request reads as anonymous, so a capability route
    that authenticates its own callers can refuse it instead of serving the
    deployment's local administrator. The explicit local-debug knob still
    asserts its user — that posture is a deliberate operator choice.
    """
    if _is_local_debug_user_enabled():
        return _build_local_debug_user()
    return _read_context_from_thread_local()


async def get_current_user_context(request_or_ws: object | None = None) -> UserContext:
    """Return the current user — a default identity, never raises.

    Resolution order:

    1. If the local-debug user is explicitly enabled, return it (keeps HTTP and
       WebSocket consistent).
    2. If a user has been set on the context, return it.
    3. Otherwise return the default identity built from the environment
       (:func:`_build_local_debug_user`).

    Unauthenticated by default, so there is no cookie verification and this
    **always** yields a user (it never raises ``APIError(UNAUTHORIZED)``).
    ``request_or_ws`` is accepted for signature compatibility with the call sites
    and is otherwise unused; a resolver that derives identity from the request does
    so before calling this, via :func:`set_current_user_context`.
    """
    if _is_local_debug_user_enabled():
        user = _build_local_debug_user()
        logger.debug("local debug user enabled, use local debug user: %s", user.user_id)
        return user

    user = _read_context_from_thread_local()
    if user is not None:
        return user

    return _build_local_debug_user()


__all__ = [
    "UserContext",
    "DEFAULT_ORG_ID",
    "PLATFORM_ADMIN_ROLE",
    "API_READ_SCOPE",
    "API_WRITE_SCOPE",
    "API_ADMIN_SCOPE",
    "default_org_id",
    "get_asserted_user_context",
    "get_current_user_context",
    "set_current_user_context",
    "reset_current_user_context",
    "_build_local_debug_user",
    "_read_context_from_thread_local",
]
