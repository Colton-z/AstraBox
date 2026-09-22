"""App-level extension points — routers, middlewares, lifespan hooks.

Downstream distributions extend the HTTP app without forking ``create_app``.
Installed packages register entry points in three groups. In-tree and vendored
adapters may additionally call :func:`register_extension_router` before app
creation; both paths feed the same router registry and duplicate names fail
closed.

``astrabox.api.routers``
    Each entry resolves to a ``fastapi.APIRouter`` (mounted via
    ``app.include_router``) or a callable ``register(app) -> None`` that adds
    its own routes. Loaded after every in-tree router and before the SPA
    catch-all, so: core routes always win a path collision (FastAPI matches
    in registration order), and extension routes are never shadowed by the
    console's ``/{full_path:path}`` fallback.

``astrabox.web.middlewares``
    Each entry resolves to an ASGI middleware class (added via
    ``app.add_middleware(cls)``) or a callable ``install(app) -> None`` that
    calls ``app.add_middleware(...)`` itself (for middlewares needing kwargs).
    Ordering contract, the security-relevant part: extension middlewares are
    installed first, so with Starlette's LIFO ``add_middleware`` semantics
    they sit innermost — every extension middleware runs behind the
    trusted-host gate (spoofed-Host requests never reach it) and behind web
    identity binding (``get_current_user_context`` is already resolved). An
    extension cannot wrap outside those two; a deployment that needs an
    outermost middleware owns its own ASGI wrapper around ``create_app``.

``astrabox.lifespan_hooks``
    Each entry resolves to an async context manager factory
    ``hook(app) -> AsyncContextManager[None]`` (write one with
    ``contextlib.asynccontextmanager``). Hooks enter after the core startup
    sequence (providers → schema → migrations → seed → recovery control
    plane) in entry-point order, and exit in reverse before core teardown.
    Startup failures are fail-loud (a broken hook aborts boot, matching the
    core steps' contract); teardown failures are logged, never raised.

All three loaders resolve their groups through
:func:`astrabox.providers._select_entry_points`, so the in-tree-vs-installed
resolution behaves exactly like every provider seam. Load errors name the
offending entry point and re-raise — a half-loaded extension surface must
never boot silently.
"""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

from fastapi import APIRouter, FastAPI

from astrabox.common.logger.logger_factory import get_logger
from astrabox.providers import _select_entry_points

logger = get_logger(__name__)

ROUTERS_GROUP = "astrabox.api.routers"
MIDDLEWARES_GROUP = "astrabox.web.middlewares"
LIFESPAN_HOOKS_GROUP = "astrabox.lifespan_hooks"
_REGISTERED_ROUTERS: dict[str, Any] = {}


def register_extension_router(name: str, target: Any) -> None:
    """Register a router adapter for source and vendored deployments."""

    key = str(name or "").strip().lower()
    if not key:
        raise RuntimeError("extension router name must be non-empty")
    if not isinstance(target, APIRouter) and not callable(target):
        raise RuntimeError(
            "extension router must be an APIRouter or register(app) callable"
        )
    existing = _REGISTERED_ROUTERS.get(key)
    if existing is not None and existing is not target:
        raise RuntimeError(f"extension router {key!r} is already registered")
    _REGISTERED_ROUTERS[key] = target


def _load_group(group: str) -> list[tuple[str, Any]]:
    from astrabox.providers import _check_seams_api_version

    loaded: list[tuple[str, Any]] = []
    for ep in sorted(_select_entry_points(group).values(), key=lambda e: e.name):
        try:
            target = ep.load()
        except Exception as exc:
            raise RuntimeError(
                f"extension entry point {ep.name!r} in group={group!r} failed to "
                f"load: {exc}"
            ) from exc
        # Same opt-in contract pin as every provider group: an extension that
        # declares seams_api_version is rejected at load time on mismatch,
        # not at first call as a runtime AttributeError.
        loaded.append((ep.name, _check_seams_api_version(target, group=group, name=ep.name)))
    return loaded


def include_extension_routers(app: FastAPI) -> None:
    """Mount every ``astrabox.api.routers`` entry (see module docstring)."""
    loaded = dict(_load_group(ROUTERS_GROUP))
    for name, target in _REGISTERED_ROUTERS.items():
        existing = loaded.get(name)
        if existing is not None and existing is not target:
            raise RuntimeError(
                f"router extension {name!r} is registered in-process and by "
                "entry point with different targets"
            )
        loaded[name] = target
    for name, target in sorted(loaded.items()):
        if isinstance(target, APIRouter):
            app.include_router(target)
        elif callable(target):
            # register(app)-style: the callable owns its own include calls.
            target(app)
        else:
            raise RuntimeError(
                f"router entry point {name!r} must resolve to an APIRouter or "
                f"a register(app) callable; got {type(target).__name__}"
            )
        logger.info("extension router mounted: %s", name)


def install_extension_middlewares(app: FastAPI) -> None:
    """Install every ``astrabox.web.middlewares`` entry (innermost — see
    module docstring for the ordering contract)."""
    for name, target in _load_group(MIDDLEWARES_GROUP):
        if isinstance(target, type):
            app.add_middleware(target)
        elif callable(target):
            target(app)
        else:
            raise RuntimeError(
                f"middleware entry point {name!r} must resolve to a middleware "
                f"class or an install(app) callable; got {type(target).__name__}"
            )
        logger.info("extension middleware installed: %s", name)


@contextlib.asynccontextmanager
async def extension_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Enter every ``astrabox.lifespan_hooks`` entry; exit in reverse.

    Fail-loud on startup (a broken hook aborts boot); teardown errors are
    swallowed with a log line, matching the core lifespan's shutdown contract.
    """
    async with contextlib.AsyncExitStack() as stack:
        for name, target in _load_group(LIFESPAN_HOOKS_GROUP):
            if not callable(target):
                raise RuntimeError(
                    f"lifespan entry point {name!r} must resolve to a callable "
                    f"hook(app) -> async context manager; got {type(target).__name__}"
                )
            cm = target(app)
            if not hasattr(cm, "__aenter__"):
                raise RuntimeError(
                    f"lifespan entry point {name!r} did not return an async "
                    "context manager (write it with "
                    "contextlib.asynccontextmanager)"
                )
            await stack.enter_async_context(_teardown_tolerant(cm, name))
            logger.info("extension lifespan hook entered: %s", name)
        yield


@contextlib.asynccontextmanager
async def _teardown_tolerant(cm: Any, name: str) -> AsyncIterator[None]:
    """Enter ``cm`` fail-loud; tolerate (log) its teardown failure."""
    await cm.__aenter__()
    try:
        yield
    finally:
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            logger.warning(
                "extension lifespan hook %r teardown failed (non-fatal)",
                name,
                exc_info=True,
            )
