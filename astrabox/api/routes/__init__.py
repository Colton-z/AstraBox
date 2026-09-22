"""Resource-router package for the AstraBox HTTP surface.

Each sibling module owns one resource family and is mounted by
:func:`astrabox.api.app.create_app`, either as a plain ``APIRouter`` or via the
idempotent ``register_*_routes(app) -> None`` convention. This package does
**not** eagerly import any submodule — each is imported by ``create_app`` exactly
where it is mounted, so importing this package stays cheap and side-effect
free (the clean-boot invariant).

Cross-cutting trace binding + the ``traceparent`` response header are NOT a
route concern: they ride one app-level ASGI middleware
(:class:`astrabox.web.traceparent_middleware.TraceparentMiddleware`), so the
resource routers carry no trace ``route_class``.

:mod:`astrabox.api.routes._shared` holds the remaining cross-cutting
error-mapping machinery (the ``_svc``/``_resolve_user`` accessors, the app-level
exception-handler coroutines, and the small JSON error-response helpers) that
most sibling route modules build on. :mod:`astrabox.api.routes.http_common`
holds the lower-level trace-header and storage-error-mapping utilities shared by
``_shared``, the middleware, and route modules alike. Both are leaves with
respect to the rest of this package — neither imports from any other module in
``routes/`` — which keeps this package's internal dependency graph acyclic.
"""

from __future__ import annotations

__all__: list[str] = []
