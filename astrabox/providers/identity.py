"""Web identity providers and the fail-loud resolver loader."""

from __future__ import annotations

import os
from importlib.metadata import entry_points
from typing import Any, Mapping

from astrabox.common.utils.user_context import (
    PLATFORM_ADMIN_ROLE,
    UserContext,
    _build_local_debug_user,
)

__all__ = [
    "LocalNoAuthWebIdentityResolver",
    "load_web_identity_resolver",
    "WEB_IDENTITY_GROUP",
    "DEFAULT_WEB_IDENTITY_NAME",
]

#: Entry-point group for the web/console identity seam (declared in pyproject.toml).
WEB_IDENTITY_GROUP = "astrabox.web.identity"
#: The built-in web resolver name.
DEFAULT_WEB_IDENTITY_NAME = "local"


class LocalNoAuthWebIdentityResolver:
    """Default web identity resolver — no API auth.

    Satisfies :class:`~astrabox.seams.identity.WebIdentityResolver`. Every
    request receives the deployment's one local user with the ``admin`` role.
    The no-auth installation is deliberately single-user and host-is-yours, so
    opening the admin routes while withholding service-level administrator
    permission would produce a console where only some features work.

    Swapping in an authenticated resolver at the ``astrabox.web.identity``
    group makes the middleware bind that resolver's identity and roles for
    every request instead.

    ``is_no_auth_default`` marks the open single-user posture. The identity
    middleware hard-gates the admin surface whenever the ACTIVE resolver does
    not carry this marker: with a real resolver configured, an anonymous
    result can never reach ``/api/v1/admin*`` — a resolver that returns None
    instead of raising must not silently reopen the admin console.
    """

    is_no_auth_default = True

    @staticmethod
    def login_url(next_url: str = "/") -> None:
        """Local mode has no login flow."""

        _ = next_url
        return None

    async def resolve(self, headers: Mapping[str, str]) -> UserContext:
        _ = headers
        local = _build_local_debug_user()
        return UserContext(
            user_id=local.user_id,
            display_name=local.display_name,
            email=local.email,
            avatar_url=local.avatar_url,
            org_id=local.org_id,
            roles=[PLATFORM_ADMIN_ROLE],
        )


#: Explicitly registered resolvers — the same register_*() composition-root
#: idiom every other seam has (sandbox/storage/engine/model/secrets/channel).
#: A vendored deployment that strips dist-info (entry-point tables gone)
#: registers its SSO resolver here from a bootstrap/lifespan hook instead of
#: forking the loader. Checked FIRST by the loaders below; last registration
#: wins, unknown names still fail loud.
_WEB_IDENTITY_RESOLVERS: dict[str, Any] = {}


def _register_identity(registry: dict[str, Any], name: str, resolver: Any) -> None:
    key = str(name or "").strip().lower()
    if not key:
        raise RuntimeError("identity resolver name must be non-empty")
    if resolver is None:
        raise RuntimeError(f"identity resolver {key!r} must not be None")
    for method in ("resolve", "login_url"):
        if not callable(getattr(resolver, method, None)):
            raise RuntimeError(
                f"identity resolver {key!r} must implement callable {method}()"
            )
    registry[key] = resolver


def register_web_identity_resolver(name: str, resolver: Any) -> None:
    """Register a web identity resolver instance (or class) under ``name``."""
    _register_identity(_WEB_IDENTITY_RESOLVERS, name, resolver)


def _instantiate(target: Any) -> Any:
    resolver = target() if isinstance(target, type) else target
    for method in ("resolve", "login_url"):
        if not callable(getattr(resolver, method, None)):
            raise RuntimeError(
                f"identity resolver {type(resolver).__name__!r} must implement "
                f"callable {method}()"
            )
    return resolver


def load_web_identity_resolver(name: str | None = None) -> Any:
    """Load + instantiate the configured web identity resolver, fail-loud.

    Reads the ``astrabox.web.identity`` entry-point group. The selected name
    comes from *name*, else ``ASTRABOX_WEB_IDENTITY`` env, else
    :data:`DEFAULT_WEB_IDENTITY_NAME` (``"local"`` — the no-auth default). An
    unknown non-default name raises ``RuntimeError`` listing the registered
    names; there is no silent fallback when another resolver is requested but
    absent."""
    wanted = str(
        name or os.getenv("ASTRABOX_WEB_IDENTITY", "") or DEFAULT_WEB_IDENTITY_NAME
    ).strip().lower()

    registered = _WEB_IDENTITY_RESOLVERS.get(wanted)
    if registered is not None:
        return _instantiate(registered)

    available = {ep.name: ep for ep in entry_points(group=WEB_IDENTITY_GROUP)}
    ep = available.get(wanted)
    if ep is None:
        if not available:
            # Editable-checkout bridge, covering EVERY in-tree resolver — not
            # just the default (mirrors the repository backend loader): a
            # source checkout / vendored tree with no dist-info resolves the
            # group empty even though these classes are right here, and
            # ``ASTRABOX_WEB_IDENTITY=jwt|trusted_header`` must work there
            # too. An unknown name still fails loud below.
            if wanted == DEFAULT_WEB_IDENTITY_NAME:
                return _instantiate(LocalNoAuthWebIdentityResolver)
            if wanted in ("trusted_header", "jwt"):
                from astrabox.providers import identity_sso

                builtin = {
                    "trusted_header": identity_sso.TrustedHeaderWebIdentityResolver,
                    "jwt": identity_sso.VerifiedJwtWebIdentityResolver,
                }[wanted]
                return _instantiate(builtin)
        raise RuntimeError(
            f"no web identity resolver named {wanted!r} in group={WEB_IDENTITY_GROUP!r} "
            f"(registered: {sorted(available)}); install its distribution or select "
            f"{DEFAULT_WEB_IDENTITY_NAME!r}"
        )
    return _instantiate(ep.load())
